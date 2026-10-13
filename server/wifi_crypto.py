"""Wi-Fi 管理消息的端到端加密：中转加密，设备解密；中间的电视服务和明文 TCP 只见密文。

设计（docs/superpowers/specs/2026-10-13-wifi-setup-design.md 第 4 节）：

- 设备第一次开机生成一对 X25519 密钥（私钥只在设备 NVS 里），公钥在 HELLO 里报（"wkey"），
  电视服务第一次见到时记在数据库里（首次信任，和设备密钥一样）；之后换了公钥不认，直到「重新配对」。
- 中转有自己的一对 X25519 密钥（私钥在中转的密钥文件里），公钥经 CONFIG 的 "wifi_key" 给设备，
  设备第一次见到时记下（同一个服务器地址下首次信任）。
- 共享密钥 = X25519(一方私钥, 另一方公钥)，两边各算各的，线上从不出现。
  消息密钥 = HKDF-SHA256(salt = 设备公钥 ‖ 中转公钥, ikm = 共享密钥, info = INFO)，32 字节。
- 每条消息 AES-256-GCM，12 字节随机 nonce，附加数据 AAD。明文 JSON 里带只增不减的 seq，设备拒绝
  不比上一条大的（防重放）。

为什么不用 HKDF(SHA-256(device_secret))：device_secret 在每次 HELLO 里明文过线（多用户设计 8.1），
看得见这条 TCP 的人也就算得出这把钥匙。

只用标准库：服务端约定不装第三方包。这里的 AES、GCM、X25519 是照 FIPS 197、SP 800-38D、
RFC 7748 写的教科书实现，只给几十字节的配网消息用，不追求速度；X25519 用 Python 整数，
不保证常数时间——中转只在主人点按钮时算一次，攻击者没法让它算成千上万次来测时间。
测试向量见 tests/test_wifi_crypto.py；设备一侧（mbedTLS）用同一组向量（tests/test_av_wifi_crypto.c）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

VERSION = 1
INFO = b"ai-passport-tv wifi v1"
AAD = b"avwifi1"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
# 明文上限：设备一侧的解密缓冲按这个分配（av_wifi_crypto.h 的 AV_WIFI_PLAIN_MAX）。
PLAIN_MAX = 320


class CryptoError(ValueError):
    """解不开、格式不对、钥匙不对：一律同一个异常，消息里不带任何密钥材料。"""


# ---- base64url（不带 =），设备一侧同一种写法 ----

def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    if not isinstance(text, str) or not text or len(text) % 4 == 1:
        raise CryptoError("bad base64")
    if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in text):
        raise CryptoError("bad base64")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise CryptoError("bad base64") from None


# ---- X25519（RFC 7748 第 5 节） ----

_P = 2 ** 255 - 19
_A24 = 121665


def _clamp(scalar: bytes) -> int:
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return int.from_bytes(k, "little")


def x25519(scalar: bytes, u: bytes) -> bytes:
    if len(scalar) != 32 or len(u) != 32:
        raise CryptoError("X25519 takes 32-byte inputs")
    k = _clamp(scalar)
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % _P, (x2 - z2) % _P
        aa, bb = a * a % _P, b * b % _P
        e = (aa - bb) % _P
        c, d = (x3 + z3) % _P, (x3 - z3) % _P
        da, cb = d * a % _P, c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


BASE_POINT = (9).to_bytes(32, "little")


def public_key(private: bytes) -> bytes:
    return x25519(private, BASE_POINT)


def new_private_key() -> bytes:
    return secrets.token_bytes(32)


# ---- HKDF-SHA256（RFC 5869） ----

def hkdf_sha256(salt: bytes, ikm: bytes, info: bytes, length: int = KEY_BYTES) -> bytes:
    if length > 255 * 32:
        raise CryptoError("HKDF output too long")
    prk = hmac.new(salt or b"\0" * 32, ikm, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


# ---- AES（FIPS 197，只要加密方向：GCM 只用正向分组） ----

def _sbox() -> list[int]:
    box = [0] * 256
    p = q = 1
    while True:
        # p 乘 3，q 除以 3（GF(2^8) 里），q 走遍所有非零元素的逆。
        p ^= ((p << 1) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1 | q >> 7) & 0xFF) ^ ((q << 2 | q >> 6) & 0xFF) ^ \
            ((q << 3 | q >> 5) & 0xFF) ^ ((q << 4 | q >> 4) & 0xFF)
        box[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    box[0] = 0x63
    return box


_SBOX = _sbox()


def _xtime(b: int) -> int:
    return ((b << 1) ^ (0x1B if b & 0x80 else 0)) & 0xFF


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) != 32:
        raise CryptoError("AES-256 needs a 32-byte key")
    words = [list(key[i:i + 4]) for i in range(0, 32, 4)]
    rcon = 1
    for i in range(8, 60):
        temp = list(words[i - 1])
        if i % 8 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % 8 == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([a ^ b for a, b in zip(words[i - 8], temp)])
    return [sum(words[r * 4:r * 4 + 4], []) for r in range(15)]


def _encrypt_block(round_keys: list[list[int]], block: bytes) -> bytes:
    s = [b ^ k for b, k in zip(block, round_keys[0])]
    for rnd in range(1, 15):
        s = [_SBOX[b] for b in s]
        # ShiftRows：状态按列存放，s[c*4+r]。
        s = [s[((c + r) % 4) * 4 + r] for c in range(4) for r in range(4)]
        if rnd != 14:
            mixed = []
            for c in range(4):
                a = s[c * 4:c * 4 + 4]
                t = a[0] ^ a[1] ^ a[2] ^ a[3]
                mixed += [a[r] ^ t ^ _xtime(a[r] ^ a[(r + 1) % 4]) for r in range(4)]
            s = mixed
        s = [b ^ k for b, k in zip(s, round_keys[rnd])]
    return bytes(s)


# ---- GCM（SP 800-38D），只用 96 位 IV ----

_R = 0xE1 << 120


def _gf_mult(x: int, y: int) -> int:
    z, v = 0, y
    for i in range(127, -1, -1):
        if (x >> i) & 1:
            z ^= v
        v = (v >> 1) ^ _R if v & 1 else v >> 1
    return z


def _ghash(h: int, aad: bytes, ciphertext: bytes) -> int:
    y = 0
    for data in (aad, ciphertext):
        for i in range(0, len(data), 16):
            block = data[i:i + 16].ljust(16, b"\0")
            y = _gf_mult(y ^ int.from_bytes(block, "big"), h)
    lengths = (len(aad) * 8).to_bytes(8, "big") + (len(ciphertext) * 8).to_bytes(8, "big")
    return _gf_mult(y ^ int.from_bytes(lengths, "big"), h)


def _ctr(round_keys, counter: int, data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 16):
        stream = _encrypt_block(round_keys, counter.to_bytes(16, "big"))
        out += bytes(a ^ b for a, b in zip(data[i:i + 16], stream))
        counter = (counter & ~0xFFFFFFFF) | ((counter + 1) & 0xFFFFFFFF)
    return bytes(out)


def gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """密文 ‖ 16 字节标签。"""
    if len(nonce) != NONCE_BYTES:
        raise CryptoError("GCM nonce must be 12 bytes")
    rk = _expand_key(key)
    h = int.from_bytes(_encrypt_block(rk, b"\0" * 16), "big")
    j0 = int.from_bytes(nonce + b"\0\0\0\1", "big")
    ciphertext = _ctr(rk, j0 + 1, plaintext)
    tag = _ghash(h, aad, ciphertext) ^ int.from_bytes(_encrypt_block(rk, j0.to_bytes(16, "big")), "big")
    return ciphertext + tag.to_bytes(16, "big")


def gcm_decrypt(key: bytes, nonce: bytes, sealed: bytes, aad: bytes) -> bytes:
    if len(nonce) != NONCE_BYTES or len(sealed) < TAG_BYTES:
        raise CryptoError("bad GCM input")
    rk = _expand_key(key)
    ciphertext, tag = sealed[:-TAG_BYTES], sealed[-TAG_BYTES:]
    h = int.from_bytes(_encrypt_block(rk, b"\0" * 16), "big")
    j0 = int.from_bytes(nonce + b"\0\0\0\1", "big")
    want = _ghash(h, aad, ciphertext) ^ int.from_bytes(_encrypt_block(rk, j0.to_bytes(16, "big")), "big")
    if not hmac.compare_digest(want.to_bytes(16, "big"), tag):
        raise CryptoError("authentication failed")
    return _ctr(rk, j0 + 1, ciphertext)


# ---- 配网消息 ----

def message_key(own_private: bytes, peer_public: bytes, device_public: bytes, server_public: bytes) -> bytes:
    shared = x25519(own_private, peer_public)
    if not any(shared):
        # 对方给的是小子群上的点：共享密钥全零，谁都算得出来。
        raise CryptoError("weak public key")
    return hkdf_sha256(device_public + server_public, shared, INFO)


def seal(server_private: bytes, device_public: bytes, plaintext: bytes, nonce: bytes | None = None) -> dict:
    """中转一侧：给设备的一条消息，放进 SWITCH {"wifi": …}。nonce 只在测试里指定。"""
    if len(plaintext) > PLAIN_MAX:
        raise CryptoError("message too long")
    server_public = public_key(server_private)
    key = message_key(server_private, device_public, device_public, server_public)
    nonce = nonce if nonce is not None else os.urandom(NONCE_BYTES)
    return {"v": VERSION, "n": b64e(nonce), "c": b64e(gcm_encrypt(key, nonce, plaintext, AAD))}


def open_sealed(device_private: bytes, server_public: bytes, envelope: dict) -> bytes:
    """设备一侧的 Python 版本（测试用，和固件对照）。"""
    if not isinstance(envelope, dict) or envelope.get("v") != VERSION:
        raise CryptoError("unknown envelope")
    device_public = public_key(device_private)
    key = message_key(device_private, server_public, device_public, server_public)
    return gcm_decrypt(key, b64d(envelope.get("n")), b64d(envelope.get("c")), AAD)


def plaintext(seq: int, op: str, **fields) -> bytes:
    body = {"seq": seq, "op": op}
    body.update({k: v for k, v in fields.items() if v is not None})
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > PLAIN_MAX:
        raise CryptoError("message too long")
    return raw


# ---- 中转的密钥文件 ----

def load_or_create_key(path: Path | str) -> bytes:
    """中转的 X25519 私钥：32 字节原样存在文件里（0600）；没有就生成一个。

    换了这把钥匙，已经记住旧公钥的设备会拒绝新消息，要在设备上重新配网（进入配网模式保存一次）
    才会改记新的；所以部署时把它和数据库一起备份。"""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    if len(raw) == 32:
        return raw
    if raw:
        raise CryptoError(f"{path}: not a 32-byte key")
    raw = new_private_key()
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    os.replace(tmp, path)
    return raw
