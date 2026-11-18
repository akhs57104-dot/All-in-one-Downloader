#!/usr/bin/env bash
# One-shot local host: install anything missing, keep the app up, and keep a
# public Cloudflare quick tunnel pointed at it.
#
#   ./serve.sh          # foreground supervisor
#   nohup ./serve.sh &  # detached
#
# The public URL is written to /tmp/techarslan-url.txt and reprinted whenever
# the tunnel is recreated (a quick-tunnel URL dies with the tunnel, so a
# restart gets a new one). Nothing here needs an account.
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-12000}"
LOCAL="http://127.0.0.1:${PORT}"
APP_LOG="${APP_LOG:-/tmp/techarslan.log}"
TUNNEL_LOG="${TUNNEL_LOG:-/tmp/techarslan-tunnel.log}"
URL_FILE="${URL_FILE:-/tmp/techarslan-url.txt}"
STABLE_URL_FILE="${STABLE_URL_FILE:-/tmp/techarslan-stable-url.txt}"
CLOUDFLARED="${CLOUDFLARED:-cloudflared}"
STAMP="$(date -Is)"

log() { echo "[serve.sh] $(date -Is) $*"; }

# --- dependencies -----------------------------------------------------------
if ! python3 -c "import flask, requests, yt_dlp, gunicorn" >/dev/null 2>&1; then
  log "installing Python dependencies"
  python3 -m pip install -q -r requirements.txt >>"$APP_LOG" 2>&1 \
    || python3 -m pip install -q --user -r requirements.txt >>"$APP_LOG" 2>&1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  log "ffmpeg missing; installing (best effort)"
  { command -v apt-get >/dev/null 2>&1 && sudo apt-get install -y -qq ffmpeg; } >>"$APP_LOG" 2>&1 || true
fi

if ! command -v "$CLOUDFLARED" >/dev/null 2>&1; then
  if [ ! -x /tmp/cloudflared ]; then
    log "downloading cloudflared"
    curl -sL --max-time 240 \
      https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
      -o /tmp/cloudflared && chmod +x /tmp/cloudflared
  fi
  CLOUDFLARED=/tmp/cloudflared
fi

# --- supervisors ------------------------------------------------------------
app_up() { curl -sf -o /dev/null --max-time 5 "$LOCAL/healthz"; }

start_app() {
  log "starting the app on :$PORT"
  PORT="$PORT" nohup ./run.sh >>"$APP_LOG" 2>&1 &
  for _ in $(seq 1 40); do app_up && return 0; sleep 1; done
  return 1
}

start_tunnel() {
  : >"$TUNNEL_LOG"
  log "opening a quick tunnel to $LOCAL"
  nohup "$CLOUDFLARED" tunnel --url "$LOCAL" --no-autoupdate \
    >>"$TUNNEL_LOG" 2>&1 &
  for _ in $(seq 1 60); do
    local url
    url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)
    if [ -n "$url" ]; then
      printf '%s\n' "$url" >"$URL_FILE"
      log "temporary public URL: $url"
      log "note: a quick-tunnel URL changes every time the tunnel restarts"
      return 0
    fi
    sleep 1
  done
  return 1
}

log "starting (port $PORT, stamp $STAMP)"
app_up || start_app || log "the app did not come up; will keep retrying"
start_tunnel || log "the tunnel did not come up; will keep retrying"
# The sandbox's own hostname is stable and outlives the tunnel, so it is the
# preferred address; the quick-tunnel URL is written to a second file for
# callers that cannot reach the hostname. Both serve the same local port.
if [ -n "${PUBLIC_URL:-}" ]; then
  printf '%s\n' "$PUBLIC_URL" >"$STABLE_URL_FILE"
  log "stable public URL: $PUBLIC_URL"
fi

# Keep both alive. The app is restarted by run.sh itself, so here we only need
# to notice when the tunnel goes away and hand out a fresh URL.
while true; do
  if ! pgrep -f "cloudflared tunnel --url $LOCAL" >/dev/null 2>&1; then
    log "the tunnel went away; reopening"
    start_tunnel || true
  fi
  app_up || { log "the app stopped answering; restarting"; start_app || true; }
  sleep 20
done
