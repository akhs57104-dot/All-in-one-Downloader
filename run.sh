#!/usr/bin/env bash
# Keep the app running: restart gunicorn whenever it exits, so a crash or an
# OOM kill does not take the site down. Run it detached and log to a file:
#
#   PORT=12000 nohup ./run.sh > /tmp/techarslan.out 2>&1 &
#
set -u
cd "$(dirname "$0")"
export PORT="${PORT:-12000}"
# A start command that kept the literal text "$PORT" would otherwise reach
# gunicorn and fail with "'$PORT' is not a valid port number". Fall back to the
# default instead. start.sh applies the same check for direct invocations.
case "$PORT" in
  ''|*[!0-9]*)
    echo "[run.sh] PORT is not numeric ('$PORT'); falling back to 12000" >&2
    PORT=12000
    ;;
esac
LOG="${LOG:-/tmp/techarslan.log}"

while true; do
  echo "[run.sh] $(date -Is) starting gunicorn on :$PORT" >>"$LOG"
  ./start.sh >>"$LOG" 2>&1
  code=$?
  echo "[run.sh] $(date -Is) gunicorn exited (code $code); restarting in 3s" >>"$LOG"
  sleep 3
done
