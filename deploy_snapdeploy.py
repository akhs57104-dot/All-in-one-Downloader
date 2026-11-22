#!/usr/bin/env python3
"""Deploy this app to SnapDeploy through its mobile API.

WARNING: SnapDeploy's Terms of Service name `yt-dlp` explicitly, under
"3. Video Piracy & Copyright Infringement Tools", as a blocked video downloader.
A deploy of this app was accepted and then failed with "Your deployment was
blocked because it violates SnapDeploy's Acceptable Use Policy." The script is
kept so the finding is reproducible and so the API plumbing is reusable for an
app that does not bundle a downloader - but it will not get this app live.

SnapDeploy's free tier is a Small container (512 MB, 0.25 vCPU), no card, and it
builds the bundled Dockerfile, so `ffmpeg` comes along. The app idles at ~75 MB,
well inside 512 MB.

    SNAPDEPLOY_API_KEY=sd_pat_... python3 deploy_snapdeploy.py --dry-run
    SNAPDEPLOY_API_KEY=sd_pat_... python3 deploy_snapdeploy.py

The token comes from https://snapdeploy.dev/api-keys and is read from the
environment only. Scopes nest: `read` < `deploy` < `manage`. Deploying needs
`deploy`.

The API is undocumented in prose, so the paths, the `X-API-Key` header and the
payloads below were read out of the official MCP client (`@snapdeploy/mcp`,
dist/tools.js) rather than guessed:

    GET  /api/mobile/bootstrap              plan, containers, quota, deploy cap
    GET  /api/mobile/github/status          is a GitHub account connected
    GET  /api/mobile/deploy-cap             deploys left in the rolling window
    POST /api/mobile/deploy                 {repo, name, branch, env, port, size}
    GET  /api/mobile/containers/<id>        status, URL, port
    GET  /api/mobile/containers/<id>/deployments?limit=N   build log tail
    GET  /api/mobile/deployments/<id>       one deployment in detail

The container name becomes the URL: <name>.containers.snapdeploy.app

SnapDeploy builds from the GitHub repository, not from this working tree, so
only pushed commits get deployed. The app code on `main` is identical to the
local tree, which is why the default branch works even while README and deploy
script changes sit unpushed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SNAPDEPLOY_BASE_URL", "https://snapdeploy.dev").rstrip("/")
REPO = "akhs57104-dot/All-in-one-Downloader"
CONTAINER = "tech-arslan-software-solutions"
PORT = 12000
SIZE = "small"


def _request(method: str, path: str, key: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("X-API-Key", key)
    req.add_header("Accept", "application/json")
    # Cloudflare in front of snapdeploy.dev answers 403 (error 1010) to
    # "Python-urllib/x.y", so the default UA has to be replaced.
    req.add_header("User-Agent", "techarslan-deploy/1.0")
    # Sent so the platform can attribute connector traffic; harmless otherwise.
    req.add_header("X-SnapDeploy-Client", "mcp/openhands")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def show_cap(key: str) -> dict | None:
    status, cap = _request("GET", "/api/mobile/deploy-cap", key)
    if status != 200:
        print(f"[deploy] could not read the deploy cap (HTTP {status}): {cap}")
        return None
    if isinstance(cap, dict):
        print(
            f"[deploy] deploys left: {cap.get('remaining', '?')}"
            f" / {cap.get('dailyLimit', '?')} (rolling 12h)"
        )
    return cap if isinstance(cap, dict) else None


def find_container(key: str, name: str) -> dict | None:
    status, boot = _request("GET", "/api/mobile/bootstrap", key)
    if status != 200:
        return None
    for row in (boot or {}).get("containers") or []:
        if (row.get("name") or "").lower() == name.lower():
            return row
    return None


def wait_for(key: str, name: str, seconds: int = 420) -> dict | None:
    """Poll the container until it is RUNNING or FAILED."""
    deadline = time.time() + seconds
    seen = None
    while time.time() < deadline:
        time.sleep(6)
        row = find_container(key, name)
        if not row:
            continue
        state = row.get("status")
        if state != seen:
            print(f"[deploy] status: {state}")
            seen = state
        if state in ("RUNNING", "FAILED"):
            return row
    return find_container(key, name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the request only")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--name", default=CONTAINER)
    parser.add_argument("--branch", default=None, help="default: the repo's default branch")
    parser.add_argument("--size", default=SIZE, choices=["small", "medium", "large"])
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-wait", action="store_true", help="do not poll for the build")
    args = parser.parse_args()

    key = os.environ.get("SNAPDEPLOY_API_KEY", "").strip()
    if not key:
        print(
            "SNAPDEPLOY_API_KEY is not set.\n"
            "  1. Sign up (free, no card): https://snapdeploy.dev/register\n"
            "  2. Create a token with the `deploy` scope: https://snapdeploy.dev/api-keys\n"
            "  3. SNAPDEPLOY_API_KEY=sd_pat_... python3 deploy_snapdeploy.py --dry-run",
            file=sys.stderr,
        )
        return 2

    body = {
        "repo": args.repo,
        "name": args.name.lower(),
        "branch": args.branch,
        "env": {"TERABOX_FALLBACK": "on", "PORT": str(args.port)},
        "port": args.port,
        "size": args.size,
    }
    body = {k: v for k, v in body.items() if v is not None}

    if args.dry_run:
        status, _ = _request("GET", "/api/mobile/bootstrap", key)
        print(f"[deploy] auth check: HTTP {status}")
        show_cap(key)
        print("[deploy] POST /api/mobile/deploy")
        print(json.dumps(body, indent=2))
        print(f"[deploy] live URL will be https://{body['name']}.containers.snapdeploy.app")
        return 0

    existing = find_container(key, body["name"])
    if existing:
        print(f"[deploy] container {body['name']} already exists ({existing.get('status')}), redeploying")

    cap = show_cap(key)
    if cap and cap.get("capped") and (cap.get("remaining") or 0) <= 0:
        print(
            "[deploy] the rolling deploy limit is used up; a $1 Sprint Pack lifts it "
            "for 24h: https://snapdeploy.dev/store?buy=sd_sprint_pack",
            file=sys.stderr,
        )
        return 1

    print(f"[deploy] deploying {body['repo']} as {body['name']} ({args.size})")
    status, res = _request("POST", "/api/mobile/deploy", key, body)
    if status not in (200, 201):
        # The platform answers 402 for a size the account is not entitled to and
        # 429 when the deploy cap is hit; both carry their own next step.
        print(f"[deploy] failed (HTTP {status}): {json.dumps(res)[:600]}", file=sys.stderr)
        return 1

    print(f"[deploy] accepted: {json.dumps(res)[:300]}")

    if args.no_wait:
        return 0

    row = wait_for(key, body["name"])
    if not row:
        print("[deploy] the container never appeared; check the dashboard", file=sys.stderr)
        return 1

    state = row.get("status")
    url = row.get("url") or f"https://{body['name']}.containers.snapdeploy.app"
    print(f"[deploy] status {state} - {url}")

    if state != "RUNNING":
        dep_id = row.get("lastDeploymentId") or row.get("deploymentId")
        if dep_id:
            _, dep = _request("GET", f"/api/mobile/deployments/{dep_id}", key)
            tail = (dep or {}).get("buildLogTail") or (dep or {}).get("errorMessage")
            if tail:
                print(f"[deploy] build log tail:\n{tail}", file=sys.stderr)
        return 1

    show_cap(key)
    print("[deploy] done. First load after 15 idle minutes wakes the container in ~60s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
