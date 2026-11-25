#!/usr/bin/env bash
# Put a static ffmpeg and ffprobe in bin/ for hosts where neither is installed
# and there is no root to add them. The app looks in bin/ before PATH, so a run
# of this script is all a fresh container needs.
#
#   ./tools/fetch-ffmpeg.sh
#
# The build is johnvansickle.com's static release, which has no shared library
# dependencies and works on any glibc-based image.
set -u
cd "$(dirname "$0")/.." || exit 1

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) ASSET="ffmpeg-release-amd64-static" ;;
  aarch64|arm64) ASSET="ffmpeg-release-arm64-static" ;;
  *)
    echo "[fetch-ffmpeg] unsupported architecture: $ARCH" >&2
    exit 1
    ;;
esac

if [ -x bin/ffmpeg ] && [ -x bin/ffprobe ]; then
  echo "[fetch-ffmpeg] bin/ffmpeg already present: $(bin/ffmpeg -version 2>/dev/null | head -1)"
  exit 0
fi

URL="https://johnvansickle.com/ffmpeg/releases/${ASSET}.tar.xz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[fetch-ffmpeg] downloading $URL"
if ! curl -fsSL --max-time 600 -o "$TMP/ffmpeg.tar.xz" "$URL"; then
  echo "[fetch-ffmpeg] download failed" >&2
  exit 1
fi

tar -xf "$TMP/ffmpeg.tar.xz" -C "$TMP" || exit 1
SRC="$(find "$TMP" -maxdepth 1 -type d -name 'ffmpeg-*-static' | head -1)"
if [ -z "$SRC" ]; then
  echo "[fetch-ffmpeg] archive layout not recognised" >&2
  exit 1
fi

mkdir -p bin
cp "$SRC/ffmpeg" "$SRC/ffprobe" bin/
chmod +x bin/ffmpeg bin/ffprobe
echo "[fetch-ffmpeg] installed: $(bin/ffmpeg -version 2>/dev/null | head -1)"
