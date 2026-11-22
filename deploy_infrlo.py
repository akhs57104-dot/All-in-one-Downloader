#!/usr/bin/env python3
"""Deploy this app to Infrlo (infrlo.com) through its console API.

Infrlo's terms have no downloader clause - only malware, spam, illegal content
and DDoS - so unlike SnapDeploy and Railway it does not refuse this app by
category. The free plan is one app, 512 MB, 0.5 vCPU, 3 GB disk, no card.

What the console does, and what this script mirrors:

  1. POST /auth/csrf-token        -> csrf_token, then sent as X-CSRF-Token
  2. POST /api/apps               -> create the app, returns its id
  3. GET  /api/apps/<id>/deployments

Auth is a session cookie from Google sign-in, so this script cannot log in by
itself. It takes the cookie from `INFRLO_COOKIE`, which you copy out of the
browser after signing in at https://dash.infrlo.com.

    INFRLO_COOKIE='...' python3 deploy_infrlo.py --dry-run
    INFRLO_COOKIE='...' python3 deploy_infrlo.py

No Dockerfile is used. Infrlo detects Python from requirements.txt, runs
`pip install -r requirements.txt`, then runs the run command. ffmpeg is not in
the base image, so the build command installs it; without it the app still
works but the "merge video and audio" option reports that ffmpeg is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# The console is served from dash.infrlo.com but its API lives on this host,
# read out of the console bundle as the axios baseURL.
BASE = "https://haa.infrlo.com"

REPO = "akhs57104-dot/All-in-one-Downloader"
BRANCH = "main"
APP_NAME = "tech-arslan-software-solutions"

# Infrlo's own example runs gunicorn on :8000 and says nothing about PORT, so
# bind 8000. One worker with threads is required: the app keeps resolved HLS
# playlists in process memory, so a second worker would miss its own tokens.
BUILD_CMD = "pip install -r requirements.txt && (apt-get install -y ffmpeg || sudo apt-get install -y ffmpeg || true)"
RUN_CMD = "python3 -m gunicorn --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:8000 wsgi:app"

ENV_VARS = {"TERABOX_FALLBACK": "on", "PYTHONUNBUFFERED": "1"}


def _request(method: str, path: str, *, cookie: str, body: dict | None = None,
             csrf: str | None = None, timeout: int = 90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "techarslan-deploy/1.0")
    req.add_header("Cookie", cookie)
    # The console sends this on every write; without it the API rejects the call.
    if csrf:
        req.add_header("X-CSRF-Token", csrf)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "null"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw[:400]
    except urllib.error.URLError as exc:
        return 0, f"connection failed: {exc.reason}"


def deploy(cookie: str, dry_run: bool) -> int:
    status, csrf_body = _request("GET", "/auth/csrf-token", cookie=cookie)
    if status == 0:
        print(f"[infrlo] cannot reach {BASE}: {csrf_body}")
        print("[infrlo] the console API is down; infrlo.com still serves. Retry later.")
        return 1
    if status in (401, 403):
        print(f"[infrlo] auth rejected (HTTP {status}); the cookie is missing or expired")
        return 1
    csrf = csrf_body.get("csrf_token") if isinstance(csrf_body, dict) else None
    if not csrf:
        print(f"[infrlo] no csrf_token in response: {csrf_body}")
        return 1
    print("[infrlo] csrf ok")

    status, me = _request("GET", "/api/me", cookie=cookie)
    if status != 200:
        print(f"[infrlo] /api/me failed (HTTP {status}): {me}")
        return 1
    user = me.get("user") if isinstance(me, dict) else None
    email = (user or {}).get("email") or (me.get("email") if isinstance(me, dict) else None)
    print(f"[infrlo] signed in as {email or '(unknown)'}")

    status, apps = _request("GET", "/api/apps", cookie=cookie)
    existing = apps if isinstance(apps, list) else (apps.get("apps") or []) if isinstance(apps, dict) else []
    print(f"[infrlo] existing apps: {len(existing)}")
    for app in existing:
        print(f"    - {app.get('name')} ({app.get('status') or app.get('state') or '?'})")

    payload = {
        "name": APP_NAME,
        "source_type": "account",
        "repo": REPO,
        "branch": BRANCH,
        "build_cmd": BUILD_CMD,
        "run_cmd": RUN_CMD,
        "env_vars": ENV_VARS,
    }
    print(f"[infrlo] POST /api/apps")
    print(json.dumps(payload, indent=2))

    if dry_run:
        print(f"[infrlo] live URL will be https://{APP_NAME}.infrlo.com")
        return 0

    status, created = _request("POST", "/api/apps", cookie=cookie, body=payload, csrf=csrf)
    if status == 403:
        print(f"[infrlo] app limit reached (free plan is one app): {created}")
        return 1
    if status not in (200, 201):
        print(f"[infrlo] create failed (HTTP {status}): {created}")
        return 1

    app_id = created.get("id") if isinstance(created, dict) else None
    print(f"[infrlo] created app id={app_id}")
    print(f"[infrlo] live URL: https://{APP_NAME}.infrlo.com")
    if app_id:
        status, deployments = _request("GET", f"/api/apps/{app_id}/deployments", cookie=cookie)
        print(f"[infrlo] deployments (HTTP {status}): {json.dumps(deployments)[:500]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="show the payload and the target URL, change nothing")
    args = parser.parse_args()

    cookie = os.environ.get("INFRLO_COOKIE", "").strip()
    if not cookie:
        print("[infrlo] set INFRLO_COOKIE to the cookie from dash.infrlo.com")
        print("         DevTools -> Application -> Cookies -> copy the session cookie")
        return 2
    return deploy(cookie, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
