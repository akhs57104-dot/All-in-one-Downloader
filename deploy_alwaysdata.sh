#!/usr/bin/env bash
# Push this app to an alwaysdata account and install its dependencies.
#
#   ALWAYSDATA_ACCOUNT=myaccount ./deploy_alwaysdata.sh
#
# alwaysdata's Free plan gives an SSH/SFTP account, so the code goes up over
# rsync and the virtualenv is built on the host. Run this after
# `deploy_alwaysdata.py` has created the WSGI site, so the site already points at
# ~/techarslan/wsgi.py when the files land.
#
# SSH needs a key on the account. If `ssh` cannot log in, the script stops and
# tells you which key to add rather than leaving a half-uploaded tree behind.
set -uo pipefail
cd "$(dirname "$0")"

ACCOUNT="${ALWAYSDATA_ACCOUNT:-}"
REMOTE_DIR="${REMOTE_DIR:-techarslan}"
SSH_HOST="${SSH_HOST:-ssh-${ACCOUNT}.alwaysdata.net}"

log() { echo "[deploy] $(date -Is) $*"; }

if [ -z "$ACCOUNT" ]; then
  echo "ALWAYSDATA_ACCOUNT is not set. Example:" >&2
  echo "  ALWAYSDATA_ACCOUNT=myaccount ./deploy_alwaysdata.sh" >&2
  exit 2
fi

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh is not installed; install openssh-client first." >&2
  exit 127
fi

log "checking SSH access to $SSH_HOST"
if ! ssh -o BatchMode=yes -o ConnectTimeout=20 "$ACCOUNT@$SSH_HOST" true 2>/dev/null; then
  cat >&2 <<EOF
SSH login failed for $ACCOUNT@$SSH_HOST.

Add a public key under Profile -> SSH keys in the alwaysdata admin, or run
`ssh-copy-id $ACCOUNT@$SSH_HOST` once by hand, then re-run this script.
EOF
  exit 1
fi

log "uploading to ~/$REMOTE_DIR"
# --delete keeps the remote tree identical, so a file removed here disappears
# there too. .git and the local caches stay behind.
rsync -az --delete \
  --exclude '.git' --exclude '__pycache__' --exclude '.venv' \
  --exclude '*.pyc' --exclude '.env' \
  ./ "$ACCOUNT@$SSH_HOST:$REMOTE_DIR/"

log "installing dependencies"
ssh "$ACCOUNT@$SSH_HOST" bash -lc "'
  set -e
  cd ~/$REMOTE_DIR
  python -m venv venv 2>/dev/null || true
  . venv/bin/activate
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
  python -c \"from wsgi import app; print(\\\"wsgi:app loads\\\")\"
'"

log "done. Site: https://${ACCOUNT}.alwaysdata.net"
log "ffmpeg is not bundled on this host; see README for the static build."
