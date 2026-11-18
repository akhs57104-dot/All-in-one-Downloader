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

# A host whose private network is IPv6 (Railway is) health-checks the container
# over that network. gunicorn bound to 0.0.0.0 answers IPv4 only, so the check
# gets "connection refused" and the deploy is marked failed even though the
# process is healthy. Bind dual-stack when the kernel allows it.
BIND_ADDR="0.0.0.0"
if python3 - "${PORT}" <<'PY' 2>/dev/null
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
try:
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    s.bind(("::", port))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
then
  BIND_ADDR="[::]"
fi

args=(
  --workers 1
  --threads 8
  --timeout 300
  --graceful-timeout 30
  --bind "${BIND_ADDR}:${PORT}"
  --access-logfile -
  --error-logfile -
)

control="default"
if "${GUNICORN[@]}" --help 2>&1 | grep -q -- "--no-control-socket"; then
  args+=(--no-control-socket)
  control="disabled"
fi

echo "[start.sh] ${GUNICORN[*]} on ${BIND_ADDR}:${PORT} (control socket ${control})"
exec "${GUNICORN[@]}" "${args[@]}" wsgi:app
