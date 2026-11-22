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
    req.add_header("User-Agent", "techarslan-deploy/1.0")
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
    data = gql(token, """
        query {
          me {
            id
            name
            email
            workspaces { id name }
          }
        }
    """)
    me = data.get("me") or {}
    workspaces = me.get("workspaces") or []
    if not workspaces:
        raise GraphQLError("this account has no workspace; finish signup in the browser first")
    return {"me": me, "workspace": workspaces[0], "all": workspaces}


def find_project(token: str, workspace_id: str, name: str):
    data = gql(token, """
        query($workspaceId: String!) {
          projects(workspaceId: $workspaceId) { edges { node { id name } } }
        }
    """, {"workspaceId": workspace_id})
    edges = (data.get("projects") or {}).get("edges") or []
    for edge in edges:
        node = edge.get("node") or {}
        if (node.get("name") or "").lower() == name.lower():
            return node
    return None


def create_project(token: str, workspace_id: str, name: str) -> dict:
    data = gql(token, """
        mutation($input: ProjectCreateInput!) {
          projectCreate(input: $input) {
            id
            name
            environments { edges { node { id name } } }
          }
        }
    """, {"input": {
        "name": name,
        "workspaceId": workspace_id,
        "description": "TeraBox and 156-site social downloader",
        "isPublic": False,
        "prDeploys": False,
    }})
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


def domain(token: str, project_id: str, env_id: str, service_id: str) -> str | None:
    data = gql(token, """
        query($projectId: String!, $environmentId: String!, $serviceId: String!) {
          serviceInstance(projectId: $projectId, environmentId: $environmentId,
                          serviceId: $serviceId) {
            domains { serviceDomains { domain } customDomains { domain } }
          }
        }
    """, {"projectId": project_id, "environmentId": env_id, "serviceId": service_id})
    inst = data.get("serviceInstance") or {}
    doms = inst.get("domains") or {}
    for key in ("serviceDomains", "customDomains"):
        for entry in doms.get(key) or []:
            if entry.get("domain"):
                return entry["domain"]
    return None


def run(token: str, dry_run: bool) -> int:
    ctx = get_workspace(token)
    ws = ctx["workspace"]
    print(f"[railway] signed in as {ctx['me'].get('email') or ctx['me'].get('name')}")
    print(f"[railway] workspace: {ws['name']} ({ws['id']})")

    existing = find_project(token, ws["id"], PROJECT_NAME)
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
        project = create_project(token, ws["id"], PROJECT_NAME)
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

    url = domain(token, project_id, env_id, service_id)
    if url:
        print(f"[railway] live URL: https://{url}")
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
