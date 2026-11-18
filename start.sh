#!/usr/bin/env bash
# Start gunicorn in a way that works on any host and any gunicorn version.
#
# One worker with threads: resolved HLS playlists and the TeraBox helper cache
# live in process memory, so a token built by one worker has to be read by the
# same worker later. Multiple processes would break that.
#
# --no-control-socket is passed only when the installed gunicorn understands it
# (>= 23). Older versions exit with "unrecognized arguments: --no-control-socket",
# which a host reports as a crash.
set -u

PORT="${PORT:-12000}"

if command -v gunicorn >/dev/null 2>&1; then
  GUNICORN=(gunicorn)
elif python3 -c "import gunicorn" >/dev/null 2>&1; then
  GUNICORN=(python3 -m gunicorn)
else
  echo "[start.sh] gunicorn is not installed" >&2
  exit 127
fi

args=(
  --workers 1
  --threads 8
  --timeout 300
  --graceful-timeout 30
  --bind "0.0.0.0:${PORT}"
  --access-logfile -
  --error-logfile -
)

control="default"
if "${GUNICORN[@]}" --help 2>&1 | grep -q -- "--no-control-socket"; then
  args+=(--no-control-socket)
  control="disabled"
fi

echo "[start.sh] ${GUNICORN[*]} on 0.0.0.0:${PORT} (control socket ${control})"
exec "${GUNICORN[@]}" "${args[@]}" wsgi:app
