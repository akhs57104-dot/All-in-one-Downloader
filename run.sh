#!/usr/bin/env bash
# Keep the app running: restart gunicorn whenever it exits, so a crash or an
# OOM kill does not take the site down. Run it detached and log to a file:
#
#   PORT=12000 nohup ./run.sh > /tmp/techarslan.out 2>&1 &
#
set -u
cd "$(dirname "$0")"
export PORT="${PORT:-12000}"
LOG="${LOG:-/tmp/techarslan.log}"

while true; do
  echo "[run.sh] $(date -Is) starting gunicorn on :$PORT" >>"$LOG"
  python3 -m gunicorn --workers 1 --threads 8 --timeout 300 \
    --graceful-timeout 30 --bind "0.0.0.0:$PORT" \
    --access-logfile - --error-logfile - wsgi:app >>"$LOG" 2>&1
  code=$?
  echo "[run.sh] $(date -Is) gunicorn exited (code $code); restarting in 3s" >>"$LOG"
  sleep 3
done
