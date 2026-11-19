#!/usr/bin/env python3
"""Deploy this app to alwaysdata through its REST API.

alwaysdata's Free plan is 0 EUR/month for life, never asks for a card, and has a
"Python WSGI" site type that matches `wsgi.py` exactly. Their API can create that
site, so the only manual step left is creating the account itself (which needs an
email verification that only the account owner can receive).

    ALWAYSDATA_TOKEN=xxx python3 deploy_alwaysdata.py --dry-run
    ALWAYSDATA_TOKEN=xxx python3 deploy_alwaysdata.py

The token is read from the environment only. Get it from Profile -> Access
tokens after signing up at https://www.alwaysdata.com/en/register/ (the Free
offer has no card field).

Authentication is HTTP Basic with the token as the username and an empty
password - note the trailing colon, which the API requires:

    curl --basic --user APIKEY: https://api.alwaysdata.com/v1/account/

The app code itself is pushed over SSH/SFTP, not by this script: the Free plan
gives an SSH account, and `deploy_alwaysdata.sh` does that part with rsync.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.alwaysdata.com/v1"
SITE_TYPE = "wsgi"
# alwaysdata stops an idle application after max_idle_time seconds (default
# 1800). Raising it keeps the first visit after a quiet spell fast; the app is
# cheap to keep resident and the free plan does not bill CPU while it idles.
MAX_IDLE_TIME = 21600


def _request(method: str, path: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    # Basic auth with an empty password: the trailing colon is mandatory.
    cred = base64.b64encode(f"{token}:".encode()).decode()
    req.add_header("Authorization", f"Basic {cred}")
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


def get_account(token: str) -> dict:
    """The hosting account the token belongs to."""
    status, accounts = _request("GET", "/account/", token)
    if status != 200:
        raise SystemExit(f"could not read the account (HTTP {status}): {accounts}")
    rows = accounts if isinstance(accounts, list) else [accounts]
    if not rows:
        raise SystemExit("the token has no account attached")
    acct = rows[0]
    print(f"[deploy] account {acct.get('name')} (id {acct.get('id')})")
    return acct


def site_body(account_name: str) -> dict:
    """A Python WSGI site serving /home/<account>/techarslan/wsgi.py.

    `addresses` is a list of plain hostnames, not objects, and a site has no
    `name` field - `annotation` is the human-readable label.
    """
    host = f"{account_name}.alwaysdata.net"
    return {
        "type": SITE_TYPE,
        "addresses": [host],
        "path": "/techarslan/wsgi.py",
        "environment": "TERABOX_FALLBACK=on",
        "max_idle_time": MAX_IDLE_TIME,
        "ssl_force": True,
        "annotation": "Tech Arslan Software Solutions",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the request only")
    parser.add_argument("--site-type", default=SITE_TYPE, help="site type to create")
    args = parser.parse_args()

    token = os.environ.get("ALWAYSDATA_TOKEN", "").strip()
    if not token:
        print(
            "ALWAYSDATA_TOKEN is not set.\n"
            "  1. Sign up (Free, no card): https://www.alwaysdata.com/en/register/\n"
            "  2. Profile -> Access tokens -> create one\n"
            "  3. ALWAYSDATA_TOKEN=xxx python3 deploy_alwaysdata.py --dry-run",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        status, _ = _request("GET", "/account/", token)
        print(f"[deploy] auth check: HTTP {status}")
        body = site_body("<account>")
        body["type"] = args.site_type
        print("[deploy] POST /site/")
        print(json.dumps(body, indent=2))
        return 0

    account = get_account(token)
    body = site_body(account["name"])
    body["type"] = args.site_type

    status, sites = _request("GET", "/site/", token)
    existing = None
    if status == 200:
        for site in sites if isinstance(sites, list) else []:
            if body["addresses"][0] in (site.get("addresses") or []):
                existing = site
                break

    if existing:
        print(f"[deploy] site {body['addresses'][0]} already exists (id {existing['id']}), updating")
        status, res = _request("PATCH", f"/site/{existing['id']}/", token, body)
    else:
        print(f"[deploy] creating site {body['addresses'][0]}")
        status, res = _request("POST", "/site/", token, body)

    if status not in (200, 201):
        print(f"[deploy] failed (HTTP {status}): {res}", file=sys.stderr)
        return 1

    print(f"[deploy] site ready: https://{body['addresses'][0]}")
    print("[deploy] next: push the code with ./deploy_alwaysdata.sh, then open the URL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
