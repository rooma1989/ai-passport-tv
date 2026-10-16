# syntax=docker/dockerfile:1
# Xiaozhi TV self-hosted server ("小智电视自建服务器"), phase S1 of
# docs/superpowers/specs/2026-10-15-self-hosting-design.md.
#
# Build from the repository root (the build context is the whole checkout, cut
# down to an allow-list by /.dockerignore):
#
#   docker build -f packaging/docker/Dockerfile -t xiaozhi-tv .
#
# CI builds linux/amd64 and linux/arm64 (.github/workflows/build-image.yml) and
# checks that the image holds no channel list, cookies or keys
# (tools/selfhost_content_check.py).
#
# Not built on the developer's machine: Docker is not installed there. Written
# from the documentation and checked by reading; the first real build is CI's.

FROM python:3.12-slim-bookworm

# Debian's dynamically linked ffmpeg, not a static build: the static one failed
# to resolve host names on the cloud host (docs/boards/xingzhi-cube-154.md).
# tini is PID 1 and forwards docker stop's SIGTERM to the supervisor; tzdata so
# TZ works (nightly backups and logs use local time).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY LICENSE /app/LICENSE
COPY packaging/docker/NOTICE /app/NOTICE
COPY server /app/server
# The device's help QR code opens /TV/ on the household server (server/front.py).
COPY packaging/help /app/packaging/help
# The web flasher at /flash/, and the firmware it writes at /flash/firmware/: the
# latest firmware release's files, put into packaging/docker/firmware/ by the
# image workflow (tools/firmware_manifest.py install). A local build without
# them has a flasher that says there is no firmware; the data folder's
# firmware/ (./run.sh --fetch-firmware, or the release zip unpacked) wins anyway.
COPY packaging/flash /app/packaging/flash
COPY packaging/docker/firmware /app/firmware
# Byte-compiled now, because the root filesystem is read-only at run time.
RUN python3 -m compileall -q /app/server

# XIAOZHI_TV_CONTAINER: the supervisor cannot see the LAN address from inside a
# bridged container and says so in its banner. HOME: the user id comes from
# compose (PUID/PGID) and has no home directory in the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XIAOZHI_TV_CONTAINER=1 \
    XIAOZHI_TV_FIRMWARE_DIR=/app/firmware \
    HOME=/tmp \
    TZ=Asia/Shanghai

VOLUME /data
# 8080 web pages (LAN), 8098 Xingzhi Cube devices, 8097 AI Passport devices.
# 8081 (pages for a tunnel) is reached on the compose network only.
EXPOSE 8080 8097 8098

# An unprivileged user by default; compose overrides it with PUID:PGID so the
# data folder a NAS user created is writable.
USER 1000:1000

HEALTHCHECK --interval=30s --timeout=6s --start-period=40s --retries=3 \
    CMD ["python3", "-m", "server.selfhost", "health"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "server.selfhost", "--data", "/data"]
