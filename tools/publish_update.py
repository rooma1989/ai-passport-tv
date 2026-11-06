#!/usr/bin/env python3
"""Publish a PC client update: the signed manifest the installed clients check
(docs/superpowers/specs/2026-11-05-pc-client-updates-design.md, section 5).

    python3 tools/publish_update.py keygen                       once: the update-signing key, prints the public key
    python3 tools/publish_update.py v0.3.9-beta [--dry-run] [--skip-qiniu] [--files <folder>]

After the CI build has put the installers on the GitHub release:

1. download SHA256SUMS-desktop and the installers from the release (or take them
   from --files) and check every one against the sums;
2. take this version's Chinese changelog entries as the notes (or --notes);
3. write rootv-update.json and sign it (rootv-update.json.sig) with the key in
   ~/.config/xiaozhi-tv/update-signing.key, after checking it matches the
   public key built into server/selfhost/updater.py;
4. upload the manifest and the signature to the GitHub release (gh);
5. upload the installers to Qiniu (rootv/<version>/), then the signature and
   last the manifest (rootv/latest/), and refresh the CDN copies of the two.

Qiniu settings come from ~/.config/xiaozhi-tv/qiniu.json (never from the
repository): {"access_key", "secret_key", "bucket", "domain": "https://…",
"up_host": "https://upload.qiniup.com", "prefix": "rootv"}. The domain plus the
prefix is what server/selfhost/updater.py's MIRROR must say.

Keys are never printed. Standard library only (plus the gh command for GitHub).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import ed25519  # noqa: E402
from server.selfhost import updater  # noqa: E402
from tools.console import use_utf8  # noqa: E402

use_utf8()    # 提示是中文，Windows 控制台默认编码打不出来

REPO = "rooma1989/ai-passport-tv"
KEY = Path("~/.config/xiaozhi-tv/update-signing.key")
QINIU = Path("~/.config/xiaozhi-tv/qiniu.json")
SUMS = "SHA256SUMS-desktop"
CHANGELOG = ROOT / "docs" / "CHANGELOG.zh_CN.md"
NOTES_MAX = 4000
_TAG = re.compile(r"^v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")


class PublishError(Exception):
    pass


# ---- keys ----

def keygen(out: Path) -> int:
    out = out.expanduser()
    if out.exists():
        print(f"{out} 已经有了，不覆盖", file=sys.stderr)
        return 1
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(secret.hex() + "\n")
    print(ed25519.public_key(secret).hex())
    return 0


def read_key(path: Path) -> bytes:
    try:
        secret = bytes.fromhex(path.expanduser().read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        raise PublishError(f"读不了更新签名密钥 {path}（先 keygen）") from None
    if len(secret) != 32:
        raise PublishError("更新签名密钥格式不对")
    return secret


# ---- the manifest ----

def platform_of(name: str, version: str) -> str | None:
    """RooTV-<ver>[-lite]-macos-arm64.dmg → macos-arm64(-lite); -windows-x64-setup.exe → windows-x64;
    -windows-x64.zip → windows-x64-portable; rootv-<ver>-linux-x86_64.tar.gz → linux-x86_64."""
    found = re.match(rf"^(?:RooTV|rootv)-{re.escape(version)}(-lite)?-"
                     r"(macos-arm64|macos-x86_64|windows-x64|linux-x86_64)(-setup)?\.(dmg|exe|zip|tar\.gz)$", name)
    if not found:
        return None
    lite, key, setup, ext = found.groups()
    if key == "windows-x64":
        if setup and ext == "exe":
            pass
        elif not setup and ext == "zip":
            key = "windows-x64-portable"
        else:
            return None
    return key + ("-lite" if lite else "")


def read_sums(text: str) -> dict[str, str]:
    sums = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and re.match(r"^[0-9a-f]{64}$", parts[0]):
            sums[parts[1].lstrip("*")] = parts[0]
    return sums


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def notes_for(version: str, changelog: str) -> str:
    """This version's section of the Chinese changelog ("## v<ver>" or "## <ver>"), else "## 未发布"; plain text."""
    sections = re.split(r"(?m)^## ", changelog)
    chosen = ""
    for section in sections[1:]:
        title = section.splitlines()[0].strip()
        if title in (f"v{version}", version):
            chosen = section
            break
    if not chosen:
        chosen = next((s for s in sections[1:] if s.splitlines()[0].strip() == "未发布"), "")
    body = "\n".join(chosen.splitlines()[1:]).strip()
    body = re.sub(r"\*\*(.+?)\*\*", r"\1", body)
    body = re.sub(r"`([^`]+)`", r"\1", body)
    if len(body) > NOTES_MAX:
        cut = body.rfind("\n", 0, NOTES_MAX - 1)
        body = body[:cut if cut > 0 else NOTES_MAX - 1].rstrip() + "\n…"
    return body


def build_manifest(version: str, folder: Path, notes: str, date: str) -> tuple[dict, list[Path]]:
    sums_path = folder / SUMS
    try:
        sums = read_sums(sums_path.read_text(encoding="utf-8"))
    except OSError:
        raise PublishError(f"没有 {SUMS}") from None
    files, paths = {}, []
    for name, digest in sorted(sums.items()):
        key = platform_of(name, version)
        if key is None:
            continue
        path = folder / name
        if not path.is_file():
            raise PublishError(f"{SUMS} 里有 {name}，但文件不在")
        if sha256(path) != digest:
            raise PublishError(f"{name} 和 {SUMS} 对不上")
        files[key] = {"name": name, "size": path.stat().st_size, "sha256": digest}
        paths.append(path)
    if not files:
        raise PublishError(f"{SUMS} 里没有 {version} 的安装包")
    manifest = {"version": version, "date": date, "notes": notes, "files": files}
    updater.check_manifest(manifest)
    return manifest, paths


def sign(manifest: dict, secret: bytes) -> tuple[bytes, bytes]:
    data = json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    return data, (ed25519.sign(secret, data).hex() + "\n").encode("ascii")


# ---- GitHub ----

def gh(args: list[str], run=subprocess.run) -> None:
    done = run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise PublishError(f"gh {' '.join(args[:2])} 失败：{(done.stderr or '').strip()[-300:]}")


def fetch_release(tag: str, folder: Path, run=subprocess.run) -> None:
    gh(["release", "download", tag, "--repo", REPO, "--dir", str(folder), "--clobber",
        "--pattern", SUMS, "--pattern", "RooTV-*", "--pattern", "rootv-*"], run)


# ---- Qiniu (form upload and CDN refresh, standard library) ----

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def upload_token(access: str, secret: str, bucket: str, key: str, now: float, ttl: int = 3600) -> str:
    policy = _b64(json.dumps({"scope": f"{bucket}:{key}", "deadline": int(now) + ttl}).encode())
    signature = _b64(hmac.new(secret.encode(), policy.encode(), hashlib.sha1).digest())
    return f"{access}:{signature}:{policy}"


def qbox_auth(access: str, secret: str, path: str) -> str:
    signature = _b64(hmac.new(secret.encode(), (path + "\n").encode(), hashlib.sha1).digest())
    return f"QBox {access}:{signature}"


def multipart(fields: dict, name: str, data: bytes, mime: str = "application/octet-stream") -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                 f"Content-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Qiniu:
    def __init__(self, settings: dict, opener=None, clock=time.time):
        for field in ("access_key", "secret_key", "bucket", "domain"):
            if not isinstance(settings.get(field), str) or not settings[field]:
                raise PublishError(f"qiniu.json 缺 {field}")
        self.s = settings
        self.prefix = str(settings.get("prefix") or "rootv").strip("/")
        self.up = str(settings.get("up_host") or "https://upload.qiniup.com").rstrip("/")
        self.domain = settings["domain"].rstrip("/")
        self.open = opener or urllib.request.urlopen
        self.clock = clock

    @property
    def mirror(self) -> str:
        return f"{self.domain}/{self.prefix}"

    def put(self, key: str, data: bytes, name: str, mime: str = "application/octet-stream") -> None:
        """表单上传；mime 是七牛存下的文件类型（官网的 HTML、CSS、JSON 要对，浏览器才不会当成下载）。"""
        token = upload_token(self.s["access_key"], self.s["secret_key"], self.s["bucket"], key, self.clock())
        body, kind = multipart({"token": token, "key": key}, name, data, mime)
        request = urllib.request.Request(self.up, data=body, headers={"Content-Type": kind}, method="POST")
        try:
            with self.open(request, timeout=600) as answer:
                answer.read()
        except OSError as error:
            raise PublishError(f"上传 {key} 到七牛失败：{type(error).__name__}") from None

    def refresh(self, urls: list[str]) -> None:
        path = "/v2/tune/refresh"
        body = json.dumps({"urls": urls}).encode()
        request = urllib.request.Request("https://fusion.qiniuapi.com" + path, data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": qbox_auth(self.s["access_key"],
                                                                             self.s["secret_key"], path)})
        try:
            with self.open(request, timeout=60) as answer:
                answer.read()
        except OSError as error:
            # 刷新不了只是要等 CDN 的缓存过期（客户端每天才查一次），不算发版失败。
            print(f"七牛 CDN 刷新没成（{type(error).__name__}），等缓存自己过期")


def read_qiniu(path: Path) -> dict:
    try:
        settings = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PublishError(f"读不了七牛设置 {path}（或者加 --skip-qiniu）") from None
    if not isinstance(settings, dict):
        raise PublishError("qiniu.json 格式不对")
    return settings


# ---- publish ----

def publish(tag: str, args, run=subprocess.run, qiniu_opener=None, today=None) -> int:
    found = _TAG.match(tag)
    if not found:
        raise PublishError("标签要像 v0.3.9-beta")
    version = found.group(1)
    secret = read_key(args.key)
    public = ed25519.public_key(secret).hex()
    if public != updater.PUBLIC_KEY:
        raise PublishError("这把签名密钥和 server/selfhost/updater.py 里的 PUBLIC_KEY 不是一对：客户端不会认")
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(args.files) if args.files else Path(tmp)
        if not args.files:
            print(f"从 GitHub 下载 {tag} 的安装包…")
            fetch_release(tag, folder, run)
        notes = args.notes if getattr(args, "notes", None) else notes_for(version, CHANGELOG.read_text(encoding="utf-8"))
        date = today or time.strftime("%Y-%m-%d")
        manifest, paths = build_manifest(version, folder, notes, date)
        data, sig = sign(manifest, secret)
        out = Path(tmp) / "out"
        out.mkdir()
        (out / updater.MANIFEST).write_bytes(data)
        (out / updater.SIGNATURE).write_bytes(sig)
        updater.verify_manifest(data, sig, updater.PUBLIC_KEY)
        print(f"清单：{version}，{len(manifest['files'])} 个平台（{', '.join(sorted(manifest['files']))}），"
              f"更新说明 {len(notes)} 字")
        if args.dry_run:
            print("--dry-run：不上传")
            return 0
        gh(["release", "upload", tag, str(out / updater.MANIFEST), str(out / updater.SIGNATURE),
            "--repo", REPO, "--clobber"], run)
        print("GitHub：清单和签名传好了")
        if args.skip_qiniu:
            return 0
        store = Qiniu(read_qiniu(args.qiniu), opener=qiniu_opener)
        for path in paths:
            print(f"七牛：上传 {path.name}…")
            store.put(f"{store.prefix}/{version}/{path.name}", path.read_bytes(), path.name)
        store.put(f"{store.prefix}/latest/{updater.SIGNATURE}", sig, updater.SIGNATURE)
        store.put(f"{store.prefix}/latest/{updater.MANIFEST}", data, updater.MANIFEST)
        store.refresh([f"{store.mirror}/latest/{updater.MANIFEST}", f"{store.mirror}/latest/{updater.SIGNATURE}"])
        print(f"七牛：传好了（{store.mirror}/latest/）")
        if not args.no_site:
            import publish_site
            with tempfile.TemporaryDirectory() as site_tmp:
                import build_site
                publish_site.publish(store, build_site.build(Path(site_tmp)))
        if updater.MIRROR.rstrip("/") != store.mirror:
            print(f"注意：客户端里的 MIRROR 是「{updater.MIRROR}」，不是 {store.mirror}：下一版要改 "
                  "server/selfhost/updater.py 的 MIRROR，客户端才会先试七牛")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="publish_update.py")
    parser.add_argument("tag", help="keygen，或者要发的版本标签（v0.3.9-beta）")
    parser.add_argument("--key", type=Path, default=KEY)
    parser.add_argument("--qiniu", type=Path, default=QINIU)
    parser.add_argument("--files", help="用这个文件夹里的安装包和 SHA256SUMS-desktop，不从 GitHub 下载")
    parser.add_argument("--dry-run", action="store_true", help="只生成、检查清单，不上传")
    parser.add_argument("--skip-qiniu", action="store_true", help="只发 GitHub")
    parser.add_argument("--no-site", action="store_true", help="不顺带更新七牛上的官网")
    parser.add_argument("--notes", help="这一版的更新说明（不给就从 docs/CHANGELOG.zh_CN.md 取）")
    args = parser.parse_args(argv)
    if args.tag == "keygen":
        return keygen(args.key)
    try:
        return publish(args.tag, args)
    except (PublishError, updater.UpdateError) as error:
        print(f"没发出去：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
