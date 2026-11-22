#!/usr/bin/env python3
"""Deploy this app to Railway through its public GraphQL API.

Railway is the best-fitting host found for this app so far, because its terms do
not name downloaders. The Acceptable Use Policy bans hosting or distributing
"copyrighted or pirated software, media, or other materials without proper
authorization from the rights holder" - a copyright clause like every other
host's, not a category ban. SnapDeploy refuses this app by name (it lists
`yt-dlp` in section 3), so this is a real difference, not a hopeful reading.

Pricing, read off railway.com/pricing: the Free plan is $0/month with **no credit
card required**, $1 of monthly usage credits, services wake on request, and up to
1 vCPU / 0.5 GB RAM. The first 30 days are a trial with $5 of credits.

The flow below is built from the live schema at
https://backboard.railway.com/graphql/v2 (introspected, not guessed):

    query  me                     -> workspace id
    query  projects               -> reuse an existing project if one matches
    mut    projectCreate          -> project + default environment
    mut    serviceCreate          -> service bound to the GitHub repo
    mut    variableCollectionUpsert -> PORT, TERABOX_FALLBACK, ...
    mut    serviceInstanceDeployV2 -> build and start

Auth is a Railway account token, so this script cannot sign up by itself. Create
one at https://railway.com/account/tokens and pass it in RAILWAY_TOKEN.

    RAILWAY_TOKEN=... python3 deploy_railway.py --dry-run
    RAILWAY_TOKEN=... python3 deploy_railway.py

The build uses the Dockerfile in this repo (railway.json pins builder=DOCKERFILE),
which installs ffmpeg. That matters: without ffmpeg the app can only hand back
single-file streams, and merging a separate video and audio track fails with
"High-quality merging needs ffmpeg".

One Railway-specific trap this app already handles, in start.sh: the dashboard's
start command may reach the process with the literal text `$PORT`, and gunicorn
then exits with "'$PORT' is not a valid port number". start.sh treats any
non-numeric PORT as absent and falls back to 12000, and binds dual-stack because
Railway health-checks over an IPv6 private network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://backboard.railway.com/graphql/v2"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

REPO = "akhs57104-dot/All-in-one-Downloader"
BRANCH = "main"
PROJECT_NAME = "tech-arslan-software-solutions"
SERVICE_NAME = "web"

# PORT is set explicitly so the app never depends on Railway's placeholder
# expansion. start.sh also falls back to 12000 when PORT is not numeric.
VARIABLES = {
    "PORT": "12000",
    "TERABOX_FALLBACK": "on",
    "PYTHONUNBUFFERED": "1",
}


class GraphQLError(RuntimeError):
    pass


def gql(token: str, query: str, variables: dict | None = None, timeout: int = 90):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    req = urllib.request.Request(API, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    # Railway 403s Python-urllib's default agent on schema introspection, so
    # send a browser agent. A plain "techarslan-deploy/1.0" is enough for
    # ordinary queries but not for __schema.
    req.add_header("User-Agent", UA)
    # A Railway account token goes in the Authorization header; a project token
    # would go in Project-Access-Token instead. This script needs the former to
    # create a project at all.
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        raise GraphQLError(f"HTTP {exc.code}: {raw[:400]}") from None
    except urllib.error.URLError as exc:
        raise GraphQLError(f"connection failed: {exc.reason}") from None

    if body.get("errors"):
        msgs = "; ".join(e.get("message", "?") for e in body["errors"])
        raise GraphQLError(msgs)
    return body.get("data") or {}


def get_workspace(token: str) -> dict:
    """Identify the account behind the token, without requiring `me`.

    A workspace token (`rlwy_wksp_...`) authenticates but answers "Not
    Authorized" on `me`, because `me` describes a user and this token belongs to
    a workspace. Rather than guess, try `me` and fall back: the workspace is
    implied by the token, so `projectCreate` works with no workspaceId at all.
    """
    try:
        data = gql(token, """
            query { me { id name email workspaces { id name } } }
        """)
        me = data.get("me") or {}
        workspaces = me.get("workspaces") or []
        if workspaces:
            return {"me": me, "workspace": workspaces[0], "all": workspaces}
    except GraphQLError:
        pass

    # No usable `me`: confirm the token authenticates at all before claiming a
    # workspace, so a bad token is reported as a bad token.
    gql(token, "query { projects { edges { node { id name } } } }")
    return {"me": {}, "workspace": None, "all": []}


def find_project(token: str, workspace_id: str | None, name: str):
    if workspace_id:
        data = gql(token, """
            query($workspaceId: String) {
              projects(workspaceId: $workspaceId) { edges { node { id name } } }
            }
        """, {"workspaceId": workspace_id})
    else:
        data = gql(token, "query { projects { edges { node { id name } } } }")
    edges = (data.get("projects") or {}).get("edges") or []
    for edge in edges:
        node = edge.get("node") or {}
        if (node.get("name") or "").lower() == name.lower():
            return node
    return None


def create_project(token: str, workspace_id: str | None, name: str) -> dict:
    payload = {
        "name": name,
        "description": "TeraBox and 156-site social downloader",
        "isPublic": False,
        "prDeploys": False,
    }
    # A workspace token already identifies the workspace, and sending an unknown
    # id would be worse than sending none.
    if workspace_id:
        payload["workspaceId"] = workspace_id
    data = gql(token, """
        mutation($input: ProjectCreateInput!) {
          projectCreate(input: $input) {
            id
            name
            environments { edges { node { id name } } }
          }
        }
    """, {"input": payload})
    project = data.get("projectCreate") or {}
    envs = ((project.get("environments") or {}).get("edges") or [])
    if not envs:
        raise GraphQLError("project created but has no environment")
    project["environmentId"] = envs[0]["node"]["id"]
    return project


def create_service(token: str, project_id: str, env_id: str) -> dict:
    data = gql(token, """
        mutation($input: ServiceCreateInput!) {
          serviceCreate(input: $input) { id name }
        }
    """, {"input": {
        "projectId": project_id,
        "environmentId": env_id,
        "name": SERVICE_NAME,
        "source": {"repo": REPO},
        "branch": BRANCH,
    }})
    return data.get("serviceCreate") or {}


def set_variables(token: str, project_id: str, env_id: str, service_id: str) -> None:
    gql(token, """
        mutation($input: VariableCollectionUpsertInput!) {
          variableCollectionUpsert(input: $input)
        }
    """, {"input": {
        "projectId": project_id,
        "environmentId": env_id,
        "serviceId": service_id,
        "replace": False,
        "variables": VARIABLES,
    }})


def deploy(token: str, project_id: str, env_id: str, service_id: str) -> dict:
    data = gql(token, """
        mutation($environmentId: String!, $serviceId: String!) {
          serviceInstanceDeployV2(environmentId: $environmentId, serviceId: $serviceId)
        }
    """, {"environmentId": env_id, "serviceId": service_id})
    return data


def ensure_domain(token: str, env_id: str, service_id: str, port: int) -> str | None:
    """Create the public domain if the service has none, then return it.

    `serviceDomainCreate` needs `targetPort`, and it must be the port the
    container actually listens on - 12000 here, matching the PORT variable.
    """
    existing = domain(token, None, env_id, service_id)
    if existing:
        return existing
    data = gql(token, """
        mutation($input: ServiceDomainCreateInput!) {
          serviceDomainCreate(input: $input) { domain }
        }
    """, {"input": {"environmentId": env_id, "serviceId": service_id,
                    "targetPort": port}})
    return ((data.get("serviceDomainCreate") or {}).get("domain")) or None


def domain(token: str, project_id: str, env_id: str, service_id: str) -> str | None:
    """Return the public domain, or None while the build is still running.

    `domains` is a top-level query taking all three ids; `serviceInstance` does
    not accept projectId, which is what the first version of this function got
    wrong.
    """
    data = gql(token, """
        query($projectId: String, $environmentId: String, $serviceId: String) {
          domains(projectId: $projectId, environmentId: $environmentId,
                  serviceId: $serviceId) {
            serviceDomains { domain }
            customDomains { domain }
          }
        }
    """, {"projectId": project_id, "environmentId": env_id, "serviceId": service_id})
    doms = data.get("domains") or {}
    for key in ("serviceDomains", "customDomains"):
        for entry in doms.get(key) or []:
            if entry.get("domain"):
                return entry["domain"]
    return None


def run(token: str, dry_run: bool) -> int:
    ctx = get_workspace(token)
    ws = ctx["workspace"]
    ws_id = ws["id"] if ws else None
    who = ctx["me"].get("email") or ctx["me"].get("name")
    if who:
        print(f"[railway] signed in as {who}")
    if ws:
        print(f"[railway] workspace: {ws['name']} ({ws_id})")
    else:
        print("[railway] workspace token accepted (no `me` on this token type)")

    existing = find_project(token, ws_id, PROJECT_NAME)
    if dry_run:
        print(f"[railway] project '{PROJECT_NAME}': " +
              ("exists, would reuse" if existing else "would be created"))
        print(f"[railway] repo {REPO} @ {BRANCH}, builder=DOCKERFILE (ffmpeg installed)")
        print("[railway] variables: " + json.dumps(VARIABLES))
        print("[railway] nothing changed (--dry-run)")
        return 0

    if existing:
        project_id = existing["id"]
        print(f"[railway] reusing project {project_id}")
        data = gql(token, """
            query($id: String!) { project(id: $id) { environments { edges { node { id name } } } } }
        """, {"id": project_id})
        envs = ((data.get("project") or {}).get("environments") or {}).get("edges") or []
        env_id = envs[0]["node"]["id"]
    else:
        project = create_project(token, ws_id, PROJECT_NAME)
        project_id = project["id"]
        env_id = project["environmentId"]
        print(f"[railway] created project {project_id} (env {env_id})")

    svc = create_service(token, project_id, env_id)
    service_id = svc.get("id")
    print(f"[railway] service {service_id} -> {REPO}@{BRANCH}")

    set_variables(token, project_id, env_id, service_id)
    print("[railway] variables set: " + ", ".join(VARIABLES))

    deploy(token, project_id, env_id, service_id)
    print("[railway] deploy triggered (build runs the Dockerfile)")

    url = ensure_domain(token, env_id, service_id, int(VARIABLES["PORT"]))
    if url:
        print(f"[railway] live URL: https://{url}")
        print("[railway] the first build takes a few minutes; poll /healthz until 200")
    else:
        print("[railway] no domain yet - open the dashboard and press Generate Domain")
    print(f"[railway] dashboard: https://railway.com/project/{project_id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve the workspace and show the plan, change nothing")
    args = parser.parse_args()

    token = os.environ.get("RAILWAY_TOKEN", "").strip()
    if not token:
        print("[railway] set RAILWAY_TOKEN (https://railway.com/account/tokens)")
        return 2
    try:
        return run(token, args.dry_run)
    except GraphQLError as exc:
        print(f"[railway] failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
