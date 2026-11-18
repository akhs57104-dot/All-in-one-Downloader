#!/usr/bin/env python3
"""Create this app on Render through the Render REST API.

Render's web dashboard cannot be driven by an agent: it sits behind a login and
an hCaptcha. Its API can, given an API key. That key belongs to the account
owner, so it is read from the environment and never written to disk.

    RENDER_API_KEY=rnd_xxx python3 deploy_render.py --dry-run   # show the calls
    RENDER_API_KEY=rnd_xxx python3 deploy_render.py             # deploy

Get the key from https://dashboard.render.com/u/settings#api-keys
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.render.com/v1"
REPO = "https://github.com/akhs57104-dot/All-in-one-Downloader"
BRANCH = "main"
SERVICE_NAME = "tech-arslan-software-solutions"
REGION = "oregon"
PLAN = "free"


def _request(method: str, path: str, key: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def pick_owner(key: str) -> str:
    """The workspace id the service is created in."""
    if os.environ.get("RENDER_OWNER_ID"):
        return os.environ["RENDER_OWNER_ID"]
    status, owners = _request("GET", "/owners?limit=20", key)
    if status != 200 or not owners:
        raise SystemExit(f"could not list workspaces (HTTP {status}): {owners}")
    # Each entry is {"owner": {...}, "cursor": "..."}; the first is the default.
    first = owners[0]
    owner = first.get("owner", first)
    print(f"[deploy] using workspace {owner.get('name')} ({owner.get('id')})")
    return owner["id"]


def service_body(owner_id: str) -> dict:
    return {
        "type": "web_service",
        "name": SERVICE_NAME,
        "ownerId": owner_id,
        "repo": REPO,
        "branch": BRANCH,
        "autoDeploy": "yes",
        "serviceDetails": {
            "env": "docker",
            "plan": PLAN,
            "region": REGION,
            "healthCheckPath": "/healthz",
            "envSpecificDetails": {
                "dockerfilePath": "./Dockerfile",
                "dockerContext": ".",
            },
        },
        "envVars": [{"key": "TERABOX_FALLBACK", "value": "on"}],
    }


def explain_failure(status: int, payload) -> None:
    if status == 401:
        print("[deploy] 401 - the API key is wrong or was revoked.")
    elif status == 402:
        print("[deploy] 402 - Render wants payment details on this account. "
              "The free plan itself does not require a card, so this usually "
              "means a paid plan was selected, or the account is flagged.")
    elif status == 409:
        print("[deploy] 409 - a service with this name already exists. "
              "Change SERVICE_NAME or delete the old service.")
    else:
        print(f"[deploy] HTTP {status}: {payload}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the requests without sending them")
    ap.add_argument("--watch", action="store_true",
                    help="poll until the first deploy finishes")
    args = ap.parse_args()

    if args.dry_run:
        owner = os.environ.get("RENDER_OWNER_ID", "<from GET /owners>")
        print(f"POST {API}/services")
        print(json.dumps(service_body(owner), indent=2))
        return 0

    key = os.environ.get("RENDER_API_KEY", "").strip()
    if not key:
        print("RENDER_API_KEY is not set. Create one at "
              "https://dashboard.render.com/u/settings#api-keys", file=sys.stderr)
        return 2

    status, created = _request("POST", "/services", key,
                               service_body(pick_owner(key)))
    if status != 201:
        explain_failure(status, created)
        return 1

    service = created.get("service", created)
    sid = service.get("id")
    print(f"[deploy] created {service.get('name')} ({sid})")
    print(f"[deploy] dashboard: https://dashboard.render.com/web/{sid}")

    if not args.watch:
        return 0

    # The public hostname appears once the first deploy finishes.
    for _ in range(60):
        status, payload = _request("GET", f"/services/{sid}", key)
        if status == 200:
            svc = payload.get("service", payload)
            host = svc.get("serviceDetails", {}).get("url") or svc.get("url")
            if host:
                print(f"[deploy] live at {host}")
                return 0
        time.sleep(10)
    print("[deploy] still building; check the dashboard for the URL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
