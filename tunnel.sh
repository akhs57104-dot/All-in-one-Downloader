#!/usr/bin/env bash
# Publish the local server on a public URL without a hosting account.
#
# Starts the app if it is not already up, then opens a Cloudflare quick tunnel
# to it and prints the https://*.trycloudflare.com link. Quick tunnels need no
# login; the link lives only as long as this script does, so run it under
# nohup or a process manager.
set -uo pipefail

PORT="${PORT:-12000}"
LOCAL="http://127.0.0.1:${PORT}"
LOG="${TUNNEL_LOG:-/tmp/techarslan-tunnel.log}"
CLOUDFLARED="${CLOUDFLARED:-cloudflared}"

if ! command -v "$CLOUDFLARED" >/dev/null; then
  echo "cloudflared not found. Install it, then re-run." >&2
  echo "  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared && chmod +x cloudflared" >&2
  exit 1
fi

if ! curl -sf -o /dev/null --max-time 5 "$LOCAL/healthz"; then
  echo "Starting the app on port $PORT ..."
  PORT="$PORT" nohup ./run.sh >/tmp/techarslan.out 2>&1 &
  for _ in $(seq 1 30); do
    curl -sf -o /dev/null --max-time 3 "$LOCAL/healthz" && break
    sleep 1
  done
fi

echo "Opening a quick tunnel to $LOCAL ..."
"$CLOUDFLARED" tunnel --url "$LOCAL" --no-autoupdate >"$LOG" 2>&1 &
TUNNEL_PID=$!
trap 'kill "$TUNNEL_PID" 2>/dev/null' EXIT

for _ in $(seq 1 60); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1)
  [ -n "$URL" ] && break
  sleep 1
done

if [ -z "${URL:-}" ]; then
  echo "The tunnel did not come up. See $LOG." >&2
  exit 1
fi

echo
echo "Public URL: $URL"
echo "Keep this script running; the URL is gone once it stops."
wait "$TUNNEL_PID"
