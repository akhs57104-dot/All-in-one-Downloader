#!/usr/bin/env python3
"""
TeraBox Share Downloader - single-file Flask web app.

Resolves a public TeraBox share link to its files and streams them back.

TeraBox's public share API has changed a lot. What this app does, in order:

  1. Load the share page to pick up the anonymous session cookies and the
     page's `jsToken`.
  2. GET /api/shorturlinfo to read the share record (shareid, uk, sign,
     timestamp) and the file list. This works anonymously.
  3. GET /api/sharedownload to mint a direct `dlink` for a chosen file.
  4. Stream the dlink through this server, forwarding cookies and Range
     headers so seeking and resuming work.

Step 3's newer sibling, POST /share/download, is CAPTCHA-walled for anonymous
clients ("need verify_v2"). Step 4 can also answer 403 "user not exists" when
TeraBox demands a logged-in session for the file; in that case set the
TERABOX_COOKIE environment variable to a browser session cookie (see README)
and the download is retried with it.

Run:  python3 terabox_app.py   (serves on PORT, default 12000)
"""

import json
import os
import re
import urllib.parse

from flask import Flask, Response, jsonify, request

# TeraBox fronts its API with a WAF that can reject Python's TLS fingerprint
# before it ever reads the headers. curl_cffi impersonates a real browser
# handshake and avoids that; plain requests is kept as a fallback so the app
# still runs without the extra dependency.
try:
    from curl_cffi import requests as _http
    _IMPERSONATE = "chrome124"
except ImportError:  # pragma: no cover - depends on the environment
    import requests as _http
    _IMPERSONATE = None

app = Flask(__name__)

# ---------------------------------------------------------------------------
# TeraBox client
# ---------------------------------------------------------------------------

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Share hosts accepted as input. A host matches when it equals one of these or
# is a subdomain of it.
SHARE_DOMAINS = (
    "terabox.com", "terabox.app", "terabox.club", "teraboxapp.com",
    "teraboxlink.com", "teraboxshare.com", "terasharelink.com",
    "terasharefile.com", "terafileshare.com", "terabox1.com", "terabox2.com",
    "1024terabox.com", "1024tera.com", "4funbox.com", "4funbox.co",
    "mirrobox.com", "nephobox.com", "momerybox.com", "tibibox.com",
    "freeterabox.com", "gibibox.com", "box-links.com", "dubox.com",
)

# Mirrors tried in order when the share's own host refuses a request.
MIRROR_ORIGINS = (
    "https://www.terabox.com",
    "https://www.1024terabox.com",
    "https://www.teraboxapp.com",
    "https://www.terabox.app",
    "https://www.1024tera.com",
)

APP_ID = "250528"
CHANNEL = "dubox"


class TeraBoxError(Exception):
    """Raised with a message that is safe to show to the user."""


def extract_share_id(url: str) -> str:
    """Pull the `surl` out of a share URL, in TeraBox's `1`-prefixed form."""
    parsed = urllib.parse.urlparse(url.strip())
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("surl"):
        surl = query["surl"][0]
        return surl if surl.startswith("1") else "1" + surl

    match = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        raise TeraBoxError("Could not find a share id in that link.")
    surl = match.group(1)
    return surl if surl.startswith("1") else "1" + surl


def is_allowed_share_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in SHARE_DOMAINS)


def _find_js_token(text: str) -> str:
    """Extract the page jsToken.

    TeraBox has shipped several shapes; the percent-encoded `fn("...")`
    trampoline is the long-standing one.
    """
    patterns = (
        r"fn%28%22([A-Fa-f0-9]{30,})%22%29",
        r'fn\("([A-Fa-f0-9]{30,})"\)',
        r'"jsToken"\s*:\s*"([A-Fa-f0-9]{30,})"',
        r"jsToken\s*[=:]\s*[\"']([A-Fa-f0-9]{30,})[\"']",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def _request_session():
    if _IMPERSONATE:
        session = _http.Session(impersonate=_IMPERSONATE)
    else:
        session = _http.Session()
    session.headers.update({
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def _request_error():
    """The exception class raised by the active HTTP client."""
    return getattr(_http, "RequestException", Exception)


def _normalize_cookie(raw: str) -> str:
    """Accept either a full cookie string or a bare `ndus` value."""
    value = (raw or "").strip()
    if not value:
        return ""
    if "=" not in value:
        return f"ndus={value}"
    return value


def _bootstrap(session, surl: str) -> dict:
    """Load the share page; return the jsToken and referer to reuse."""
    origin = MIRROR_ORIGINS[0]
    share_url = f"{origin}/s/{surl}"
    last_error = None
    for candidate in MIRROR_ORIGINS:
        try:
            resp = session.get(f"{candidate}/s/{surl}", timeout=25, allow_redirects=True)
            if resp.status_code == 200 and "terabox" in resp.text.lower():
                return {
                    "jsToken": _find_js_token(resp.text),
                    "referer": f"{candidate}/sharing/link?surl={surl.lstrip('1')}",
                    "origin": candidate,
                }
            last_error = f"share page returned status {resp.status_code}"
        except _request_error() as exc:
            last_error = str(exc)
    raise TeraBoxError(f"Could not open the share page ({last_error}).")


def _get_share_info(session, surl: str, context: dict) -> dict:
    """Read the share record and file list from /api/shorturlinfo."""
    params = {
        "app_id": APP_ID,
        "web": "1",
        "channel": CHANNEL,
        "clienttype": "0",
        "jsToken": context.get("jsToken", ""),
        "shorturl": surl,
        "root": "1",
    }
    headers = {"Referer": context["referer"]}
    last_error = "unknown error"
    for origin in _origins_first(context["origin"]):
        try:
            resp = session.get(
                f"{origin}/api/shorturlinfo",
                params=params, headers=headers, timeout=25,
            )
            data = resp.json()
        except (_request_error(), ValueError) as exc:
            last_error = str(exc)
            continue
        errno = data.get("errno")
        if errno == 0 and data.get("list"):
            return data
        if errno in (105, 460030):
            last_error = "TeraBox is rate-limiting this server; wait a minute and retry."
        elif errno == -9:
            raise TeraBoxError("This share is password protected.")
        elif errno:
            last_error = data.get("errmsg") or f"errno {errno}"
        else:
            last_error = "the share listed no files"
    raise TeraBoxError(f"Could not read the share ({last_error}).")


def _origins_first(origin: str):
    """Yield `origin` first, then the other mirrors."""
    yield origin
    for other in MIRROR_ORIGINS:
        if other != origin:
            yield other


def _make_dlink(session, surl: str, info: dict, fs_id, context: dict) -> str:
    """Mint a direct download link for one file via /api/sharedownload."""
    params = {
        "app_id": APP_ID,
        "web": "1",
        "channel": CHANNEL,
        "clienttype": "0",
        "jsToken": context.get("jsToken", ""),
        "shareid": info["shareid"],
        "uk": info["uk"],
        "sign": info.get("sign", ""),
        "timestamp": info.get("timestamp", ""),
        "fid_list": json.dumps([int(fs_id)]),
        "primaryid": info["shareid"],
        "product": "share",
        "nozip": "0",
        "type": "download",
        "extra": json.dumps(
            {"sekey": urllib.parse.unquote(info.get("randsk") or "")},
            separators=(",", ":"),
        ),
    }
    headers = {"Referer": context["referer"]}
    last_error = "unknown error"
    for origin in _origins_first(context["origin"]):
        try:
            resp = session.get(
                f"{origin}/api/sharedownload",
                params=params, headers=headers, timeout=25,
            )
            data = resp.json()
        except (_request_error(), ValueError) as exc:
            last_error = str(exc)
            continue
        if data.get("errno") == 0 and data.get("list"):
            dlink = data["list"][0].get("dlink")
            if dlink:
                return dlink
        last_error = data.get("errmsg") or f"errno {data.get('errno')}"
    raise TeraBoxError(f"Could not create a download link ({last_error}).")


def _sizes(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def resolve_share(link: str) -> dict:
    """Resolve a share link into metadata plus a file list."""
    link = (link or "").strip()
    if not link:
        raise TeraBoxError("Please paste a TeraBox link.")
    if not is_allowed_share_url(link):
        raise TeraBoxError("That does not look like a TeraBox share link.")

    surl = extract_share_id(link)
    session = _request_session()
    context = _bootstrap(session, surl)
    info = _get_share_info(session, surl, context)

    files = []
    for item in info.get("list", []):
        if str(item.get("isdir")) == "1":
            continue
        files.append({
            "fs_id": item.get("fs_id"),
            "name": item.get("server_filename") or "download",
            "size": int(item.get("size") or 0),
            "size_text": _sizes(int(item.get("size") or 0)),
            "path": item.get("path", ""),
            "category": str(item.get("category", "")),
        })

    if not files:
        raise TeraBoxError("This share contains no downloadable files.")

    return {
        "surl": surl,
        "shareid": info.get("shareid"),
        "uk": info.get("uk"),
        "sign": info.get("sign"),
        "timestamp": info.get("timestamp"),
        "randsk": info.get("randsk"),
        "origin": context["origin"],
        "referer": context["referer"],
        "jsToken": context.get("jsToken", ""),
        "share_username": info.get("share_username", ""),
        "files": files,
    }


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    link = (request.json or {}).get("link", "")
    try:
        return jsonify({"ok": True, "share": resolve_share(link)})
    except TeraBoxError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        return jsonify({"ok": False, "error": f"Unexpected error: {exc}"}), 500


@app.route("/api/download")
def api_download():
    """Stream one file: resolve a fresh dlink, then proxy the bytes."""
    link = request.args.get("link", "")
    fs_id = request.args.get("fs_id", "")
    try:
        share = resolve_share(link)
    except TeraBoxError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    chosen = next((f for f in share["files"] if str(f["fs_id"]) == str(fs_id)), None)
    if chosen is None:
        chosen = share["files"][0]

    session = _request_session()
    cookie = _normalize_cookie(os.environ.get("TERABOX_COOKIE", ""))
    if cookie:
        session.headers["Cookie"] = cookie
    context = {"jsToken": share["jsToken"], "referer": share["referer"],
               "origin": share["origin"]}
    try:
        dlink = _make_dlink(session, share["surl"], share, chosen["fs_id"], context)
    except TeraBoxError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    upstream_headers = {"User-Agent": BROWSER_UA, "Referer": share["referer"]}
    if request.headers.get("Range"):
        upstream_headers["Range"] = request.headers["Range"]
    if cookie:
        upstream_headers["Cookie"] = cookie

    upstream = session.get(dlink, headers=upstream_headers, stream=True, timeout=60)

    if upstream.status_code not in (200, 206):
        upstream.close()
        if upstream.status_code == 403 and not cookie:
            return jsonify({
                "ok": False,
                "error": (
                    "TeraBox now refuses downloads for anonymous sessions "
                    "(it answers 403 'user not exists'). Set the TERABOX_COOKIE "
                    "environment variable to a logged-in TeraBox cookie, then "
                    "restart the app and try again."
                ),
            }), 403
        return jsonify({
            "ok": False,
            "error": f"TeraBox returned status {upstream.status_code} for the file.",
        }), 502

    filename = urllib.parse.quote(chosen["name"])
    out_headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
        "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
    }
    if upstream.headers.get("Content-Length"):
        out_headers["Content-Length"] = upstream.headers["Content-Length"]
    if upstream.headers.get("Content-Range"):
        out_headers["Content-Range"] = upstream.headers["Content-Range"]
    out_headers["Accept-Ranges"] = "bytes"

    def stream():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(stream(), status=upstream.status_code, headers=out_headers)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TeraBox Downloader</title>
<style>
  :root { --bg:#f0f4f8; --card:#fff; --primary:#007bff; --text:#22313f; --muted:#6b7c8f; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         background:var(--bg); color:var(--text); display:flex; justify-content:center; padding:32px 16px; }
  .app { width:100%; max-width:680px; }
  h1 { font-size:1.5rem; margin:0 0 4px; color:var(--primary); }
  .sub { color:var(--muted); font-size:.85rem; margin-bottom:20px; }
  .card { background:var(--card); border-radius:12px; padding:20px; box-shadow:0 1px 3px rgba(16,42,67,.12); margin-bottom:16px; }
  label { display:block; font-size:.8rem; font-weight:600; margin-bottom:6px; }
  input[type=text] { width:100%; padding:11px 12px; border:1px solid #cbd6e2; border-radius:8px; font-size:.95rem; }
  input[type=text]:focus { outline:none; border-color:var(--primary); box-shadow:0 0 0 3px rgba(0,123,255,.15); }
  .row { display:flex; gap:10px; margin-top:12px; }
  button { border:0; border-radius:8px; padding:11px 18px; font-size:.9rem; font-weight:600; cursor:pointer; }
  .primary { background:var(--primary); color:#fff; flex:1; }
  .primary:disabled { background:#9cc4ec; cursor:default; }
  .ghost { background:#e7eef7; color:var(--text); }
  #status { font-size:.85rem; color:var(--muted); min-height:20px; margin-top:10px; }
  .error { color:#c0392b; }
  .files { margin-top:16px; }
  .file { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:11px 12px;
          border:1px solid #e2eaf2; border-radius:8px; margin-bottom:8px; }
  .file .meta { min-width:0; }
  .file .name { font-size:.9rem; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .file .size { font-size:.78rem; color:var(--muted); }
  .file a { flex-shrink:0; text-decoration:none; background:var(--primary); color:#fff; padding:8px 14px;
            border-radius:8px; font-size:.82rem; font-weight:600; }
  .hidden { display:none; }
  .note { background:#fff7e6; border:1px solid #ffe0a3; color:#7a5b12; border-radius:10px;
          padding:12px 14px; font-size:.82rem; line-height:1.45; margin-bottom:16px; }
  .note code { background:#fdecc8; padding:1px 5px; border-radius:4px; font-size:.8rem; }
</style>
</head>
<body>
<div class="app">
  <h1>TeraBox Downloader</h1>
  <div class="sub">Paste a public TeraBox share link to list its files and download them.</div>

  <div id="note" class="note">
    Listing files works anonymously. TeraBox now refuses the actual download
    unless the server is signed in, so set <code>TERABOX_COOKIE</code> to a
    logged-in TeraBox cookie before starting the app. See the README.
  </div>

  <div class="card">
    <label for="link">TeraBox share link</label>
    <input id="link" type="text" placeholder="https://www.terabox.com/s/xxxxxxxxxxxxxxxxxxxxxx" autocomplete="off">
    <div class="row">
      <button id="fetch" class="primary">Fetch files</button>
      <button id="clear" class="ghost">Clear</button>
    </div>
    <div id="status"></div>
  </div>

  <div id="filesCard" class="card hidden">
    <label id="filesTitle">Files</label>
    <div id="files" class="files"></div>
  </div>
</div>

<script>
const linkEl = document.getElementById('link');
const fetchBtn = document.getElementById('fetch');
const clearBtn = document.getElementById('clear');
const statusEl = document.getElementById('status');
const filesCard = document.getElementById('filesCard');
const filesEl = document.getElementById('files');
const filesTitle = document.getElementById('filesTitle');

function setStatus(msg, isError) {
  statusEl.textContent = msg || '';
  statusEl.className = isError ? 'error' : '';
}

function renderFiles(share) {
  filesEl.innerHTML = '';
  filesTitle.textContent = share.files.length === 1
    ? 'File' : `Files (${share.files.length})`;
  for (const f of share.files) {
    const row = document.createElement('div');
    row.className = 'file';
    const meta = document.createElement('div');
    meta.className = 'meta';
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = f.name;
    const size = document.createElement('div');
    size.className = 'size';
    size.textContent = f.size_text;
    meta.append(name, size);
    const a = document.createElement('a');
    a.textContent = 'Download';
    a.href = '/api/download?link=' + encodeURIComponent(linkEl.value.trim()) +
             '&fs_id=' + encodeURIComponent(f.fs_id);
    row.append(meta, a);
    filesEl.append(row);
  }
  filesCard.classList.remove('hidden');
}

async function fetchFiles() {
  const link = linkEl.value.trim();
  if (!link) { setStatus('Please paste a link first.', true); return; }
  fetchBtn.disabled = true;
  filesCard.classList.add('hidden');
  setStatus('Fetching file list...');
  try {
    const resp = await fetch('/api/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ link })
    });
    const data = await resp.json();
    if (!data.ok) { setStatus(data.error, true); return; }
    setStatus('Share resolved.');
    renderFiles(data.share);
  } catch (err) {
    setStatus('Request failed: ' + err.message, true);
  } finally {
    fetchBtn.disabled = false;
  }
}

fetchBtn.addEventListener('click', fetchFiles);
linkEl.addEventListener('keydown', e => { if (e.key === 'Enter') fetchFiles(); });
clearBtn.addEventListener('click', () => {
  linkEl.value = '';
  filesCard.classList.add('hidden');
  setStatus('');
  linkEl.focus();
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "12000"))
    app.run(host="0.0.0.0", port=port)
