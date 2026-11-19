#!/usr/bin/env bash
# One-shot local host: install anything missing, keep the app up, and keep a
# public URL pointed at it.
#
#   ./serve.sh          # foreground supervisor
#   nohup ./serve.sh &  # detached
#
# Two tunnels run, because they fail in different ways:
#
#   localtunnel  https://<SUBDOMAIN>.loca.lt  - the subdomain is *requested*, so
#                the URL survives a restart. Visitors see a one-time
#                "click to continue" page the first time they open it.
#   cloudflared  https://<words>.trycloudflare.com - no interstitial at all, but
#                the hostname is new every restart.
#
# The chosen URL is written to $URL_FILE. Nothing here needs an account.
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-12000}"
LOCAL="http://127.0.0.1:${PORT}"
APP_LOG="${APP_LOG:-/tmp/techarslan.log}"
TUNNEL_LOG="${TUNNEL_LOG:-/tmp/techarslan-tunnel.log}"
LT_LOG="${LT_LOG:-/tmp/techarslan-lt.log}"
URL_FILE="${URL_FILE:-/tmp/techarslan-url.txt}"
STABLE_URL_FILE="${STABLE_URL_FILE:-/tmp/techarslan-stable-url.txt}"
CLOUDFLARED="${CLOUDFLARED:-cloudflared}"
SUBDOMAIN="${SUBDOMAIN:-tech-arslan-software-solutions}"
LT_URL="https://${SUBDOMAIN}.loca.lt"
CF_URL_FILE="${CF_URL_FILE:-/tmp/techarslan-cf-url.txt}"
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
      printf '%s\n' "$url" >"$CF_URL_FILE"
      log "cloudflare URL (no interstitial, changes on restart): $url"
      return 0
    fi
    sleep 1
  done
  return 1
}

start_localtunnel() {
  : >"$LT_LOG"
  log "requesting $LT_URL"
  nohup npx --yes localtunnel --port "$PORT" --subdomain "$SUBDOMAIN" \
    >>"$LT_LOG" 2>&1 &
  for _ in $(seq 1 60); do
    if grep -q "your url is:" "$LT_LOG"; then
      printf '%s\n' "$LT_URL" >"$URL_FILE"
      log "stable URL (survives restarts): $LT_URL"
      return 0
    fi
    sleep 1
  done
  return 1
}

lt_up() {
  curl -s -o /dev/null -H "bypass-tunnel-reminder: true" \
    --max-time 15 "$LT_URL/healthz"
}

log "starting (port $PORT, stamp $STAMP)"
app_up || start_app || log "the app did not come up; will keep retrying"
start_localtunnel || log "localtunnel did not come up; falling back to cloudflared"
start_tunnel || log "the tunnel did not come up; will keep retrying"
# The sandbox's own hostname is stable and outlives the tunnels, so it is
# recorded separately for callers that can reach it directly.
if [ -n "${PUBLIC_URL:-}" ]; then
  printf '%s\n' "$PUBLIC_URL" >"$STABLE_URL_FILE"
  log "stable public URL: $PUBLIC_URL"
fi

# Keep the app and both tunnels alive. The app is restarted by run.sh itself, so
# here we only need to notice when a tunnel goes away and re-request it.
while true; do
  if ! pgrep -f "cloudflared tunnel --url $LOCAL" >/dev/null 2>&1; then
    log "the cloudflare tunnel went away; reopening"
    start_tunnel || true
  fi
  if ! pgrep -f "localtunnel --port $PORT" >/dev/null 2>&1; then
    log "the localtunnel went away; re-requesting $LT_URL"
    start_localtunnel || true
  fi
  app_up || { log "the app stopped answering; restarting"; start_app || true; }
  if lt_up; then
    printf '%s\n' "$LT_URL" >"$URL_FILE"
  elif [ -s "$CF_URL_FILE" ]; then
    cp "$CF_URL_FILE" "$URL_FILE"
  fi
  sleep 20
done
