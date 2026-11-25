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

Because that cookie is now required for step 4, two anonymous fallbacks sit
behind it, tried in order:

  * third-party resolvers (playterabox.com, then iteraplay.com) that hold their
    own TeraBox accounts. playterabox returns a full-length, Range-capable link
    plus a complete HLS playlist, so download and full-length playback both work
    with no quota; iteraplay is a rate-limited secondary. Both see every link
    (TERABOX_FALLBACK=off disables them). TeraBox never gives an anonymous
    visitor the original file, so a helper's copy is a transcoded MPEG-TS:
    playback carries the full duration, but a download arrives as `.ts`;
  * TeraBox's own /share/streaming transcode, which needs no cookie but is only
    a fixed ~30-second preview, so it is used for playback, never a download.

Run:  python3 terabox_app.py   (serves on PORT, default 12000)
"""

import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import zipfile

from flask import Flask, Response, jsonify, request, send_file
from markupsafe import escape

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


def _request_link() -> str:
    """The `link` field of the JSON body, as a string, or ''.

    The body is attacker-controlled: it may be absent, a list, or carry a
    non-string `link`, and a bare `(... or "").strip()` turns those into a 500.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return ""
    link = body.get("link")
    return link.strip() if isinstance(link, str) else ""


# Some sandboxes (Railway, Fly) start with no writable temp dir, so
# tempfile.mkstemp() raises and gunicorn dies while booting the worker.
# Pick the first writable candidate up front and point tempfile at it.
def _ensure_writable_tempdir():
    candidates = [tempfile.tempdir, os.environ.get("TMPDIR"), "/tmp",
                  os.path.join(os.getcwd(), ".tmp")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".writable-probe")
            with open(probe, "w"):
                pass
            os.remove(probe)
        except OSError:
            continue
        os.environ["TMPDIR"] = candidate
        tempfile.tempdir = candidate
        return candidate
    return None


_ensure_writable_tempdir()

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

VIDEO_EXTS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv",
    ".wmv", ".mpg", ".mpeg", ".m2ts", ".3gp",
}

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

def _is_video(name: str) -> bool:
    dot = name.lower().rfind(".")
    return dot != -1 and name.lower()[dot:] in VIDEO_EXTS

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
        name = item.get("server_filename") or "download"
        size = int(item.get("size") or 0)
        files.append({
            "id": str(item.get("fs_id")),
            "fs_id": item.get("fs_id"),
            "name": name,
            "size": size,
            "size_text": _sizes(size),
            "is_video": _is_video(name),
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
        "source": "terabox",
        "files": files,
    }

# ---------------------------------------------------------------------------
# Helper services (anonymous full-length fallback)
# ---------------------------------------------------------------------------
#
# TeraBox hands a shared file to an anonymous visitor only as a transcoded
# MPEG-TS stream, never the original MP4 — and only through third-party
# resolvers that hold their own TeraBox account. playterabox.com is the primary
# one: it returns a full-length link (Range-capable) plus a complete HLS
# playlist, both of which we proxy. iteraplay.com is a secondary, quota-limited
# source. They are tried in order, disclose each link to that service, and their
# answers are cached briefly. Set TERABOX_FALLBACK=off to disable them all.
#
# Each resolved file is described as:
#   {"dl": download_url, "hls": playlist_url_or_None, "size": bytes}

ITERAPLAY_HOME = "https://iteraplay.com/"
ITERAPLAY_ENDPOINT = "https://iteraplay.com/api/download"

PLAYTERABOX_HOME = "https://playterabox.com/"
PLAYTERABOX_ENDPOINT = "https://playterabox.com/api/fetch-video"
# The site signs its own requests with this fixed salt; a fresh MD5 per call is
# what it expects, not a secret of ours.
PLAYTERABOX_SALT = "T9do@SM1?xGn5"
PLAYTERABOX_UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36")

# surl -> (resolved_at, provider, {filename: info}); the helpers' links expire
# in minutes, so this only spares a repeat click, it never outlives the link.
_FALLBACK_CACHE: dict[str, tuple[float, str, dict[str, dict]]] = {}
_FALLBACK_TTL = 240

def _fallback_enabled() -> bool:
    return os.environ.get("TERABOX_FALLBACK", "on").strip().lower() not in ("off", "0", "no", "false")

def _playterabox_files(surl: str) -> dict[str, dict]:
    """Resolve via playterabox.com (full length, no quota)."""
    stamp = int(time.time())
    token = hashlib.md5(f"{PLAYTERABOX_SALT}{stamp}/api/stream.php".encode()).hexdigest()
    try:
        resp = _request_session().post(
            f"{PLAYTERABOX_ENDPOINT}?token={token}&t={stamp}",
            json={"url": f"https://www.terabox.com/s/{surl}"},
            headers={"accept": "*/*", "content-type": "application/json",
                     "origin": PLAYTERABOX_HOME.rstrip("/"),
                     "referer": PLAYTERABOX_HOME,
                     "user-agent": PLAYTERABOX_UA},
            timeout=60)
    except _request_error() as exc:
        raise TeraBoxError(f"playterabox.com is not responding ({exc}).") from exc
    try:
        data = resp.json()
    except ValueError:
        raise TeraBoxError("playterabox.com sent an unreadable reply.")
    if resp.status_code >= 400 or data.get("status") != "success":
        note = data.get("message") or data.get("status") or resp.status_code
        raise TeraBoxError(f"playterabox.com could not open that link ({note}).")

    files = {}
    for entry in data.get("list") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        dl = str(entry.get("download_link") or entry.get("normal_dlink") or "").strip()
        if not (name and dl):
            continue
        hls = str(entry.get("stream_url") or "").strip()
        if hls.startswith("/"):
            hls = PLAYTERABOX_HOME.rstrip("/") + hls
        files[name] = {"dl": dl, "hls": hls or None,
                       "size": int(entry.get("size") or 0)}
    if not files:
        raise TeraBoxError("playterabox.com found no downloadable file behind that link.")
    return files

def _iteraplay_files(surl: str) -> dict[str, dict]:
    """Resolve via iteraplay.com (full quality, but a small guest quota)."""
    session = _request_session()
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": ITERAPLAY_HOME.rstrip("/"),
        "referer": ITERAPLAY_HOME,
    }
    # An optional `login_token` raises the helper's guest quota; it is the
    # operator's own account, never someone else's.
    token = os.environ.get("TERABOX_FALLBACK_TOKEN", "").strip()
    cookies = {"login_token": token, "remember_me": "true"} if token else None
    try:
        session.get(ITERAPLAY_HOME, cookies=cookies, timeout=25)
        resp = session.post(ITERAPLAY_ENDPOINT,
                            json={"url": f"https://www.terabox.com/s/{surl}"},
                            headers=headers, cookies=cookies, timeout=60)
    except _request_error() as exc:
        raise TeraBoxError(f"iteraplay.com is not responding ({exc}).") from exc

    try:
        data = resp.json()
    except ValueError:
        raise TeraBoxError("iteraplay.com sent an unreadable reply.")

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    if data.get("error") == "usage_limit" or resp.status_code == 429:
        limit = usage.get("limit")
        allowance = f" ({limit} resolutions per 6 hours)" if limit else ""
        raise TeraBoxError(f"iteraplay.com has used up its free quota{allowance}.")
    if resp.status_code >= 400 or data.get("status") != "success":
        note = data.get("message") or data.get("status") or resp.status_code
        raise TeraBoxError(f"iteraplay.com could not open that link ({note}).")

    files = {}
    for entry in data.get("list") or []:
        if not isinstance(entry, dict) or entry.get("is_dir"):
            continue
        name = str(entry.get("name") or "").strip()
        dl = str(entry.get("normal_dlink") or "").strip()
        if name and dl:
            files[name] = {"dl": dl, "hls": None,
                           "size": int(entry.get("size") or 0)}
    if not files:
        raise TeraBoxError("iteraplay.com found no downloadable file behind that link.")
    return files

_HELPER_PROVIDERS = (
    ("playterabox", _playterabox_files),
    ("iteraplay", _iteraplay_files),
)

def _fallback_files(surl: str) -> tuple[str, dict[str, dict]]:
    """Return `(provider, {filename: info})` from the first helper that works."""
    cached = _FALLBACK_CACHE.get(surl)
    if cached and (time.time() - cached[0]) < _FALLBACK_TTL:
        return cached[1], cached[2]

    errors = []
    for name, resolver in _HELPER_PROVIDERS:
        try:
            files = resolver(surl)
        except TeraBoxError as exc:
            errors.append(exc)
            continue
        _FALLBACK_CACHE[surl] = (time.time(), name, files)
        return name, files
    if not errors:
        raise TeraBoxError("No helper service could resolve that link.")
    # A quota notice is more actionable than a generic failure, so prefer it.
    quota = next((e for e in errors if "quota" in str(e).lower()), None)
    raise quota or errors[0]

def resolve_share_fallback(link: str) -> dict:
    """Resolve a share using a helper service alone (used when TeraBox
    listing is rate-limited)."""
    surl = extract_share_id(link)
    provider, files = _fallback_files(surl)
    out = []
    for name, info in files.items():
        size = info.get("size") or 0
        out.append({"id": name, "fs_id": None, "name": name, "size": size,
                    "size_text": _human_size(size), "is_video": _is_video(name)})
    return {"surl": surl, "source": provider, "share_username": "", "files": out}

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.2f} GB"


# ---------------------------------------------------------------------------
# TeraBox native transcode (HLS)
# ---------------------------------------------------------------------------
#
# TeraBox will transcode a shared video for anonymous playback through
# /share/streaming, which answers an unencrypted M3U8. It needs no cookie and no
# third party and is not rate-limited, but it is only a preview: measured across
# many shares it always yields three ~10s segments and then ENDLIST, however long
# the file is, and `start_ply` slides that window rather than extending it. So it
# is offered for playback only, never as a download. Segments are proxied through
# this server so the browser is same-origin and the CDN's CORS does not matter.

_HLS_STORE: dict[str, tuple[float, list, str]] = {}
_HLS_TTL = 1800


def _hls_playlist(share: dict, chosen: dict):
    """Fetch the 480p M3U8 for one file. Returns (segment_urls, referer)."""
    session = _request_session()
    params = {
        "app_id": APP_ID, "web": "1", "channel": CHANNEL, "clienttype": "0",
        "uk": share["uk"], "shareid": share["shareid"], "fid": chosen["fs_id"],
        "sign": share["sign"], "timestamp": share["timestamp"],
        "type": "M3U8_AUTO_480", "esl": "1", "isplayer": "1",
    }
    try:
        resp = session.get(f"{share['origin']}/share/streaming", params=params,
                           headers={"Referer": share["referer"]}, timeout=30)
    except _request_error() as exc:
        raise TeraBoxError(f"TeraBox streaming is unreachable ({exc}).") from exc

    text = resp.text or ""
    if not text.startswith("#EXTM3U"):
        raise TeraBoxError("TeraBox did not offer a stream for this file.")
    segments = [line.strip() for line in text.splitlines()
                if line.strip().startswith("http")]
    if not segments:
        raise TeraBoxError("TeraBox returned an empty stream for this file.")
    return segments, share["referer"]

# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    # `__SPONSOR_*__` are substituted rather than templated: the page is a plain
    # string and keeping it that way avoids a second escaping layer.
    page = (INDEX_HTML
            .replace("__SPONSOR_NAME__", escape(SPONSOR_NAME))
            .replace("__SPONSOR_URL__", escape(SPONSOR_URL))
            .replace("__SPONSOR_TAGLINE__", escape(SPONSOR_TAGLINE))
            .replace("__PLAYLIST_BATCH_LIMIT__", str(PLAYLIST_BATCH_LIMIT)))
    return Response(page, mimetype="text/html")

@app.route("/healthz")
def healthz():
    """Cheap liveness probe for the host's health check.

    `/` renders the ~88 KB home page; a health check should not pay for that on
    every poll. This answers as soon as the worker can serve a request, which is
    what tells the host the deploy is up.
    """
    return jsonify({"ok": True})


PRIVACY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy - Tech Arslan Software Solutions</title>
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
max-width:720px;margin:0 auto;padding:28px 20px;line-height:1.65;color:#1f2430}
h1{font-size:26px;margin-bottom:4px}h2{font-size:18px;margin-top:28px}
.updated{color:#6b7280;font-size:14px}code{background:#f3f4f6;padding:1px 5px;border-radius:4px}</style>
</head><body>
<h1>Privacy Policy</h1>
<p class="updated">Tech Arslan Software Solutions &middot; last updated 17 November 2026</p>

<h2>What this app does</h2>
<p>Tech Arslan is a downloader. You paste or share a public link, the app asks our
server to resolve it, and the file is downloaded to your device's Downloads folder
by Android's own download manager.</p>

  <h2>What we do not collect</h2>
  <p>We do not ask for your name, email address, phone number or contacts. We do not
  read your files, your photos or your SMS. The app requests only two Android
  permissions: internet access, and the ability to check whether a network
  connection exists.</p>

<h2>What is processed</h2>
<p>The link you submit is sent to our server so it can be resolved, and is held in
memory only for the length of that request. We do not keep a history of your links
on the server. A copy of your recent links may be stored <em>on your own device</em>
so the app can show them to you; clearing the app's data removes them.</p>
<p>Like any web service, our host records standard request logs (IP address, time,
requested path) for security and troubleshooting. These are retained by the hosting
provider on a short rolling basis.</p>

<h2>Third parties</h2>
<p>Resolving a link necessarily contacts the service that hosts the file (for
example TeraBox, or the social platform you pasted a link from). Those services
receive the request as they would if you opened the link in a browser, and their
own privacy policies apply. We do not sell data, and we do not share data with
advertisers.</p>

<h2>Children</h2>
<p>The app is not directed at children under 13 and we do not knowingly process
their data.</p>

<h2>Your control</h2>
  <p>Uninstalling the app removes everything it stored locally. There is no sign-up and
  no account, so there is nothing further for us to delete on your behalf.</p>

<h2>Contact</h2>
<p>Questions about this policy: <a href="mailto:support@techarslan.example">
support@techarslan.example</a></p>
</body></html>
"""


@app.route("/download-apk")
def download_apk():
    """Serve the built APK, so the phone can install it without a file transfer.

    This is direct distribution, which sidesteps the Play review risk entirely.
    The newest file in `dist/` wins, so dropping a new APK there is the whole
    release process for sideloading.
    """
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")
    try:
        candidates = [f for f in os.listdir(folder) if f.endswith(".apk")]
    except OSError:
        candidates = []
    if not candidates:
        return jsonify({"ok": False, "error": "No APK has been built yet."}), 404
    # A debug build is newer than the release it sits next to but must never be
    # handed out: it is debuggable and signed with the throwaway debug key.
    releases = [f for f in candidates if "debug" not in f.lower()]
    pool = releases or candidates
    newest = max(pool, key=lambda f: os.path.getmtime(os.path.join(folder, f)))
    return send_file(os.path.join(folder, newest),
                     mimetype="application/vnd.android.package-archive",
                     as_attachment=True, download_name=newest)


@app.route("/privacy")
def privacy():
    """Public privacy policy. Play Console requires a reachable URL."""
    return Response(PRIVACY_HTML, mimetype="text/html")


@app.route("/api/app-config")
def app_config():
    """Serve the backend-switch file, so an installed app has a second source.

    The Android client prefers this route over GitHub raw because it is on the
    same host the app is already talking to, and because GitHub raw can be
    rate-limited or blocked on some networks. Serving the file from here also
    means the switch works even if the repository is made private later.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "api_config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": f"Config unavailable: {exc}"}), 500
    return jsonify({"ok": True, "config": data}), 200


@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    link = _request_link()
    if not link:
        return jsonify({"ok": False, "error": "Please paste a TeraBox link."}), 400
    if not is_allowed_share_url(link):
        return jsonify({"ok": False, "error": "That does not look like a TeraBox share link."}), 400

    try:
        return jsonify({"ok": True, "share": resolve_share(link)})
    except TeraBoxError as primary:
        # TeraBox listing failed (often a soft rate limit). Fall back to the
        # helper service so the user still gets a working file list.
        if _fallback_enabled():
            try:
                return jsonify({"ok": True, "share": resolve_share_fallback(link),
                                "note": str(primary)})
            except TeraBoxError as secondary:
                return jsonify({"ok": False, "error": str(secondary)}), 400
        return jsonify({"ok": False, "error": str(primary)}), 400
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        return jsonify({"ok": False, "error": f"Unexpected error: {exc}"}), 500

# ---------------------------------------------------------------------------
# Sponsor branding
# ---------------------------------------------------------------------------
#
# Every file this app hands out should carry the sponsor. Three layers, because
# no single one covers every path:
#
#   1. Filename  - works for every download, costs nothing, already applied by
#                  `_branded()`. Survives being copied off the phone.
#   2. Metadata  - title/artist/comment tags written with `-c copy`, so the file
#                  is remuxed without re-encoding: a 2 GB video gains tags in a
#                  few seconds and loses no quality. Visible in VLC, MX Player,
#                  Windows Explorer and any "file info" screen.
#   3. On-screen - a sponsor badge in the app's own player, so the branding is
#                  seen during playback as well as in the file.
#
# What is deliberately *not* done: burning a logo into the pixels. That needs a
# full re-encode, which on a free host means minutes of CPU per file and would
# make a 2 GB download impractical. Set SPONSOR_BURN_IN=1 to opt in anyway on a
# machine with the CPU to spare.

SPONSOR_NAME = os.environ.get("SPONSOR_NAME", "Tech Arslan Software Solutions")
SPONSOR_URL = os.environ.get("SPONSOR_URL", "https://github.com/akhs57104-dot/All-in-one-Downloader")
SPONSOR_TAGLINE = os.environ.get("SPONSOR_TAGLINE", "Downloaded with Tech Arslan Downloader")
SPONSOR_BURN_IN = os.environ.get("SPONSOR_BURN_IN", "").lower() in {"1", "true", "yes", "on"}

BRAND_SUFFIX = "Powered by Tech Arslan Software Solutions"


def _branded(filename: str) -> str:
    """Append the developer branding to a download's name, keeping the extension."""
    if BRAND_SUFFIX in filename:
        return filename
    stem, dot, ext = filename.rpartition(".")
    if dot and stem and len(ext) <= 5 and ext.isalnum():
        return f"{stem} - {BRAND_SUFFIX}.{ext}"
    return f"{filename} - {BRAND_SUFFIX}"


def _sponsor_tags() -> list[str]:
    """ffmpeg metadata arguments carrying the sponsor credit."""
    return [
        "-metadata", f"title={SPONSOR_TAGLINE}",
        "-metadata", f"artist={SPONSOR_NAME}",
        "-metadata", f"album={SPONSOR_NAME}",
        "-metadata", f"comment={SPONSOR_TAGLINE} - {SPONSOR_URL}",
        "-metadata", f"copyright={SPONSOR_NAME}",
        "-metadata", f"encoded_by={SPONSOR_NAME}",
    ]


def _apply_sponsor_metadata(path: str, timeout: int = 180) -> str:
    """Write sponsor tags into a finished media file, without re-encoding.

    Returns the path to use. On any failure the original is returned untouched,
    because a download with no tags is far better than a failed download.
    """
    if not _ffmpeg_available():
        return path
    stem, dot, ext = path.rpartition(".")
    if not dot:
        return path
    tagged = f"{stem}.tagged.{ext}"
    cmd = [_ffmpeg_path(), "-y", "-loglevel", "error", "-i", path, "-c", "copy",
           # Without an explicit map, ffmpeg keeps only one stream per type and
           # silently drops extra audio tracks. The `?` suffix makes each map
           # optional so a file lacking subtitles does not fail the remux.
           "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
           "-map_metadata", "-1", *_sponsor_tags()]
    if ext.lower() in {"mp4", "m4v", "mov"}:
        # Put the index at the front so the file streams before it is fully
        # downloaded, which is what makes playback start instantly on a phone.
        cmd += ["-movflags", "+faststart"]
    cmd.append(tagged)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode == 0 and os.path.exists(tagged) and os.path.getsize(tagged) > 0:
            os.replace(tagged, path)
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        if os.path.exists(tagged):
            os.remove(tagged)
    except OSError:
        pass
    return path


# A download is buffered and tagged only when it is small enough to be worth the
# disk churn. Above this, the file streams straight through with filename
# branding, because a free host has neither the disk nor the time to spool a
# multi-gigabyte video twice.
SPONSOR_TAG_LIMIT = int(os.environ.get("SPONSOR_TAG_LIMIT_MB", "300")) * 1024 * 1024


def _tagged_download(upstream, filename: str, content_type: str | None,
                     prefix: bytes = b""):
    """Buffer a video, write sponsor tags, and stream it from disk.

    Returns a Response, or None when this download should be proxied instead
    (too large, unknown size, not a video, or a ranged/resume request, all of
    which need byte-exact passthrough rather than a rewritten file).

    `prefix` carries bytes the caller already pulled off the stream while
    sniffing the container, so they are written back before the rest. The
    upstream's Content-Length still covers the whole file, so it is unchanged.
    """
    if not _ffmpeg_available() or SPONSOR_TAG_LIMIT <= 0:
        return None
    if request.headers.get("Range"):
        return None
    try:
        length = int(upstream.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return None
    if length <= 0 or length > SPONSOR_TAG_LIMIT:
        return None
    if not (content_type or "").startswith("video/"):
        return None

    workdir = tempfile.mkdtemp(prefix="brand-")
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        ext, stem = "mp4", filename
    path = os.path.join(workdir, f"media.{ext}")
    try:
        with open(path, "wb") as fh:
            if prefix:
                fh.write(prefix)
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    fh.write(chunk)
    except Exception:  # noqa: BLE001 - fall back to a plain proxy
        shutil.rmtree(workdir, ignore_errors=True)
        return None
    finally:
        upstream.close()

    _apply_sponsor_metadata(path)

    def body():
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    quoted = urllib.parse.quote(_branded(filename))
    return Response(body(), headers={
        "Content-Type": content_type or "video/mp4",
        "Content-Length": str(os.path.getsize(path)),
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
    })


def _proxy_stream(upstream, *, as_attachment: bool, filename: str,
                  content_type: str | None = None,
                  prefix: bytes = b"") -> Response:
    """Relay an upstream response, preserving Range/length semantics."""
    headers = {
        "Content-Type": content_type or upstream.headers.get(
            "Content-Type", "application/octet-stream"),
        "Accept-Ranges": "bytes",
    }
    if as_attachment:
        quoted = urllib.parse.quote(_branded(filename))
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quoted}"
    for name in ("Content-Length", "Content-Range"):
        if upstream.headers.get(name):
            headers[name] = upstream.headers[name]

    def body():
        try:
            if prefix:
                yield prefix
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(body(), status=upstream.status_code, headers=headers)

def _terabox_dlink(share: dict, chosen: dict):
    """Resolve a TeraBox dlink for one file, or return None if unavailable."""
    session = _request_session()
    cookie = _normalize_cookie(os.environ.get("TERABOX_COOKIE", ""))
    if cookie:
        session.headers["Cookie"] = cookie
    context = {"jsToken": share["jsToken"], "referer": share["referer"],
               "origin": share["origin"]}
    return session, cookie, _make_dlink(session, share["surl"], share,
                                        chosen["fs_id"], context)

def _helper_entry(link: str, fs_id: str, name_hint: str) -> tuple[str, dict]:
    """Find one file's helper entry by name. Returns `(name, info)`.

    The frontend passes the file's name (`name_hint`); the numeric `fs_id` is
    useless to a helper, which keys its list by filename.
    """
    wanted = name_hint or (fs_id[5:] if fs_id.startswith("name:") else "")
    _provider, files = _fallback_files(extract_share_id(link))
    if wanted and wanted in files:
        return wanted, files[wanted]
    if wanted:
        # Fall back to a case-insensitive match before giving up.
        for name, info in files.items():
            if name.lower() == wanted.lower():
                return name, info
    name = next(iter(files))
    return name, files[name]

def _serve(link: str, fs_id: str, *, mode: str):
    """Serve one file, whole or as a playlist.

    `mode` is one of:
      "download" — the full file as an attachment. Uses TeraBox's own dlink when
                   a cookie is set (the original MP4); otherwise a helper's
                   full-length stream, which is a transcoded MPEG-TS, so the
                   name is given a .ts extension.
      "hls"      — a same-origin M3U8 playlist for playback. Prefers a helper's
                   complete playlist (full length); falls back to TeraBox's own
                   transcode, which is only a 30-second preview.
    """
    if not link:
        return jsonify({"ok": False, "error": "Missing link."}), 400

    name_hint = request.args.get("name", "").strip()
    terabox_error = None
    helper_error = None

    # TeraBox's own dlink is the only route that yields the original MP4, and it
    # needs a signed-in cookie, so it is tried first and only when one is set.
    if mode == "download" and os.environ.get("TERABOX_COOKIE") \
            and is_allowed_share_url(link) and not fs_id.startswith("name:"):
        try:
            share = resolve_share(link)
            chosen = next((f for f in share["files"]
                           if str(f["fs_id"]) == str(fs_id)), None) or share["files"][0]
            session, cookie, dlink = _terabox_dlink(share, chosen)
        except TeraBoxError as exc:
            terabox_error = exc
        else:
            headers = {"User-Agent": BROWSER_UA, "Referer": share["referer"],
                       "Cookie": cookie}
            if request.headers.get("Range"):
                headers["Range"] = request.headers["Range"]
            upstream = session.get(dlink, headers=headers, stream=True, timeout=60)
            if upstream.status_code in (200, 206):
                tagged = _tagged_download(
                    upstream, chosen["name"],
                    upstream.headers.get("Content-Type"))
                if tagged is not None:
                    return tagged
                return _proxy_stream(upstream, as_attachment=True,
                                     filename=chosen["name"])
            upstream.close()
            terabox_error = TeraBoxError(
                f"TeraBox returned status {upstream.status_code} for the file.")

    if _fallback_enabled():
        try:
            name, info = _helper_entry(link, fs_id, name_hint)
        except TeraBoxError as exc:
            helper_error = exc
        else:
            if mode == "download":
                # The helper's container varies: some files come back as the
                # original MP4, others as a transcoded MPEG-TS. Sniff the first
                # chunk so the name and type match the bytes actually sent.
                headers = {"User-Agent": BROWSER_UA}
                if request.headers.get("Range"):
                    headers["Range"] = request.headers["Range"]
                upstream = _request_session().get(info["dl"], headers=headers,
                                                  stream=True, timeout=60)
                if upstream.status_code in (200, 206):
                    head = next(upstream.iter_content(chunk_size=65536), b"") or b""
                    kind = _sniff_media(head)
                    if kind:
                        filename = _with_ext(name, kind[0])
                        content_type = kind[1]
                    else:
                        filename = name
                        content_type = upstream.headers.get(
                            "Content-Type", "application/octet-stream")
                    if kind:
                        tagged = _tagged_download(upstream, filename,
                                                  content_type, prefix=head)
                        if tagged is not None:
                            return tagged
                    return _proxy_stream(upstream, as_attachment=True,
                                         filename=filename,
                                         content_type=content_type,
                                         prefix=head)
                upstream.close()
                helper_error = TeraBoxError(
                    f"The helper returned status {upstream.status_code} for the file.")
            elif info.get("hls"):
                playlist = _fetch_playlist(info["hls"])
                if playlist is not None:
                    return playlist

    # Fall back to TeraBox's own transcode. It is a fixed 30-second preview, so
    # it is only ever offered for playback, never as a download.
    if mode == "hls":
        try:
            preview = _terabox_preview(link, fs_id)
        except TeraBoxError as exc:
            terabox_error = terabox_error or exc
        else:
            if preview is not None:
                return preview

    error = helper_error or terabox_error
    if mode == "download":
        return jsonify({"ok": False, "error": str(error) if error
                        else "No download route is available for that file."}), 400
    return jsonify({"ok": False, "error": str(error) if error
                    else "No stream is available for that file."}), 400

def _sniff_media(head: bytes) -> tuple[str, str] | None:
    """Identify a media container from its first bytes.

    Returns `(extension, content_type)`, or None when the bytes are not a
    container we recognise, so the caller can fall back to the upstream's own
    headers. TeraBox helpers serve both the original MP4 and a transcoded
    MPEG-TS depending on the file, so the container cannot be assumed.
    """
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"qt  ",):
            return ".mov", "video/quicktime"
        return ".mp4", "video/mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm", "video/webm"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return ".avi", "video/x-msvideo"
    # MPEG-TS: 0x47 sync byte every 188 bytes. Check a run of packets so a
    # stray 0x47 in unrelated data cannot match.
    if head[:1] == b"\x47" and len(head) >= 188 * 3:
        if all(head[i] == 0x47 for i in range(0, 188 * 3, 188)):
            return ".ts", "video/mp2t"
    return None


def _with_ext(name: str, ext: str) -> str:
    """Swap a filename's extension, keeping the stem."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem}{ext}"

def _fetch_playlist(url: str):
    """Proxy a helper's HLS playlist through this server, or return None."""
    try:
        resp = _request_session().get(url, headers={"Referer": PLAYTERABOX_HOME},
                                      timeout=60)
    except _request_error():
        return None
    return _store_playlist(resp.text or "", PLAYTERABOX_HOME)

def _store_playlist(text: str, referer: str):
    """Store an upstream M3U8 and return it rewritten to same-origin segment URLs."""
    if not text.startswith("#EXTM3U"):
        return None
    segments = [line.strip() for line in text.splitlines()
                if line.strip().startswith("http")]
    if not segments:
        return None
    token = secrets.token_urlsafe(12)
    _HLS_STORE[token] = (time.time(), segments, referer)

    out, index = [], 0
    for line in text.splitlines():
        if line.strip().startswith("http"):
            # Root-relative so the player uses whatever host it reached us on
            # (works behind the proxy) and never a hardcoded origin.
            out.append(f"/api/hls/{token}/{index}.ts")
            index += 1
        else:
            out.append(line)
    if "#EXT-X-ENDLIST" not in text:
        out.append("#EXT-X-ENDLIST")
    return Response("\n".join(out) + "\n",
                    headers={"Content-Type": "application/vnd.apple.mpegurl",
                             "Cache-Control": "no-store"})

def _serve_segments(segments: list, referer: str):
    """Build a same-origin playlist from a bare list of segment URLs."""
    token = secrets.token_urlsafe(12)
    _HLS_STORE[token] = (time.time(), segments, referer)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:15",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(len(segments)):
        lines.append("#EXTINF:10,")
        lines.append(f"/api/hls/{token}/{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return Response("\n".join(lines) + "\n",
                    headers={"Content-Type": "application/vnd.apple.mpegurl",
                             "Cache-Control": "no-store"})

def _terabox_preview(link: str, fs_id: str):
    """TeraBox's own 30-second transcode as a same-origin playlist, or None."""
    if not is_allowed_share_url(link) or fs_id.startswith("name:"):
        return None
    share = resolve_share(link)
    chosen = next((f for f in share["files"]
                   if str(f["fs_id"]) == str(fs_id)), None) or share["files"][0]
    if not chosen.get("is_video"):
        return None
    segments, referer = _hls_playlist(share, chosen)
    return _serve_segments(segments, referer)


@app.route("/api/hls/<token>/<int:index>.ts")
def api_hls_segment(token: str, index: int):
    """Proxy one HLS segment (same-origin so the browser can play it)."""
    record = _HLS_STORE.get(token)
    if not record or (time.time() - record[0]) > _HLS_TTL:
        return jsonify({"ok": False, "error": "This stream has expired; reopen the video."}), 410
    _, segments, referer = record
    if index < 0 or index >= len(segments):
        return jsonify({"ok": False, "error": "No such segment."}), 404
    try:
        upstream = _request_session().get(segments[index],
                                          headers={"Referer": referer},
                                          stream=True, timeout=60)
    except _request_error() as exc:
        return jsonify({"ok": False, "error": f"Segment fetch failed: {exc}"}), 502
    relay = _proxy_stream(upstream, as_attachment=False, filename=f"{index}.ts")
    relay.headers["Content-Type"] = "video/mp2t"  # CDN sends application/octet-stream
    return relay


@app.route("/api/download")
def api_download():
    """Download one file as an attachment."""
    return _serve(request.args.get("link", ""), request.args.get("fs_id", ""),
                  mode="download")


@app.route("/api/stream")
def api_stream():
    """Alias for /api/hls, kept so older links keep working."""
    return _serve(request.args.get("link", ""), request.args.get("fs_id", ""),
                  mode="hls")


@app.route("/api/prepare")
def api_prepare():
    """Resolve (and cache) a file's helper entry, so the following /api/hls
    starts immediately. Returns JSON, never bytes.

    The helper services take several seconds to answer; doing that work here,
    behind a spinner, stops the player from giving up while the real playlist is
    still being arranged.
    """
    link = request.args.get("link", "")
    fs_id = request.args.get("fs_id", "")
    name_hint = request.args.get("name", "").strip()
    if not link:
        return jsonify({"ok": False, "error": "Missing link."}), 400

    if not _fallback_enabled():
        return jsonify({"ok": True, "provider": "terabox"})
    try:
        name, info = _helper_entry(link, fs_id, name_hint)
    except TeraBoxError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "provider": "helper", "name": name,
                    "hls": bool(info.get("hls"))})


@app.route("/api/hls")
def api_hls():
    """HLS transcode for playback when the direct route is unavailable."""
    return _serve(request.args.get("link", ""), request.args.get("fs_id", ""),
                  mode="hls")

# ---------------------------------------------------------------------------
# Social media downloader (YouTube, Instagram, Facebook, TikTok, X, ...)
#
# yt-dlp does the extracting. It returns a list of formats; some are
# "progressive" (video+audio in one file) and can be streamed straight to the
# browser, while others are video-only or audio-only and must be merged first.
# Merging needs ffmpeg and takes time, so it happens on demand: the page lists
# the formats, and only when the user asks for one does the server download the
# pieces, mux them, and hand back the finished file.
# ---------------------------------------------------------------------------

# Each entry: key, display name, brand colour, 2-letter badge, and a host matcher.
# yt-dlp backs every entry; the list drives the badge and host detection, and
# its generic extractor still covers sites that are not named here.
SOCIAL_PLATFORMS = (
    # --- Global social & short video ---
    ("youtube",     "YouTube",       "#ff0033", "YT", ("youtube.com", "youtu.be", "youtube-nocookie.com")),
    ("instagram",   "Instagram",     "#e1306c", "IG", ("instagram.com", "instagr.am")),
    ("facebook",    "Facebook",      "#1877f2", "FB", ("facebook.com", "fb.watch", "fb.com")),
    ("tiktok",      "TikTok",        "#00f2ea", "TT", ("tiktok.com",)),
    ("twitter",     "X (Twitter)",   "#e7e9ea", "X",  ("twitter.com", "x.com", "t.co")),
    ("whatsapp",    "WhatsApp",      "#25d366", "WA", ("whatsapp.com", "wa.me")),
    ("snapchat",    "Snapchat",      "#fffc00", "SC", ("snapchat.com",)),
    ("linkedin",    "LinkedIn",      "#0a66c2", "IN", ("linkedin.com", "lnkd.in")),
    ("pinterest",   "Pinterest",     "#e60023", "PT", ("pinterest.com", "pin.it")),
    ("reddit",      "Reddit",        "#ff4500", "RD", ("reddit.com", "redd.it")),
    ("threads",     "Threads",       "#e7e9ea", "TH", ("threads.net", "threads.com")),
    ("telegram",    "Telegram",      "#2aabee", "TG", ("t.me", "telegram.me", "telegram.org")),
    ("bluesky",     "Bluesky",       "#0085ff", "BS", ("bsky.app", "bsky.social")),
    ("mastodon",    "Mastodon",      "#6364ff", "MO", ("mastodon.social", "mastodon.online", "mstdn.social")),
    ("truthsocial", "Truth Social",  "#5442ba", "TS", ("truthsocial.com",)),
    ("gettr",       "GETTR",         "#ff0000", "GT", ("gettr.com",)),
    ("parler",      "Parler",        "#be1e2d", "PR", ("parler.com",)),
    ("gab",         "Gab",           "#21cf7a", "GB", ("gab.com", "gab.ai")),
    ("locals",      "Locals",        "#ff6b00", "LC", ("locals.com",)),
    ("mewe",        "MeWe",          "#0076d3", "ME", ("mewe.com",)),
    ("minds",       "Minds",         "#f2f2f2", "MN", ("minds.com",)),
    ("wechat",      "WeChat",        "#07c160", "WC", ("wechat.com", "mp.weixin.qq.com")),
    ("line",        "LINE",          "#00c300", "LN", ("line.me",)),
    ("viber",       "Viber",         "#7360f2", "VB", ("viber.com",)),
    ("signal",      "Signal",        "#3a76f0", "SG", ("signal.org",)),

    # --- Video platforms ---
    ("vimeo",       "Vimeo",         "#1ab7ea", "VM", ("vimeo.com",)),
    ("dailymotion", "Dailymotion",   "#00aaff", "DM", ("dailymotion.com", "dai.ly")),
    ("twitch",      "Twitch",        "#9146ff", "TW", ("twitch.tv",)),
    ("kick",        "Kick",          "#53fc18", "KK", ("kick.com",)),
    ("trovo",       "Trovo",         "#1eea6c", "TO", ("trovo.live",)),
    ("dlive",       "DLive",         "#ffd800", "DL", ("dlive.tv",)),
    ("rumble",      "Rumble",        "#85c742", "RU", ("rumble.com",)),
    ("odysee",      "Odysee",        "#ef1970", "OD", ("odysee.com", "lbry.tv")),
    ("dtube",       "DTube",         "#1f8ceb", "DT", ("d.tube",)),
    ("peertube",    "PeerTube",      "#f1680d", "PE", ("peertube.tv", "framatube.org")),
    ("bitchute",    "BitChute",      "#d40000", "BT", ("bitchute.com",)),
    ("brighteon",   "Brighteon",     "#f5a623", "BN", ("brighteon.com",)),
    ("metacafe",    "Metacafe",      "#f7c600", "MC", ("metacafe.com",)),
    ("break",       "Break",         "#00a0e9", "BK", ("break.com",)),
    ("veoh",        "Veoh",          "#5e6f8f", "VH", ("veoh.com",)),
    ("clipfish",    "Clipfish",      "#e2001a", "CF", ("clipfish.de",)),
    ("myvideo",     "MyVideo",       "#f7b500", "MV", ("myvideo.de",)),
    ("vevo",        "Vevo",          "#ff0033", "VO", ("vevo.com",)),
    ("streamable",  "Streamable",    "#0f90fa", "ST", ("streamable.com",)),
    ("archive",     "Internet Archive","#000000","AR", ("archive.org",)),
    ("loom",        "Loom",          "#625df5", "LO", ("loom.com",)),
    ("wistia",      "Wistia",        "#54bbff", "WI", ("wistia.com", "wi.st")),
    ("vidyard",     "Vidyard",       "#3f3fbf", "VY", ("vidyard.com",)),
    ("bilibili",    "Bilibili",      "#fb7299", "BI", ("bilibili.com", "b23.tv")),
    ("niconico",    "Niconico",      "#252525", "NC", ("nicovideo.jp", "nico.ms")),
    ("abema",       "ABEMA",         "#00a2ff", "AB", ("abema.tv",)),
    ("dmm",         "DMM",           "#1a1a1a", "DD", ("dmm.co.jp", "dmm.com")),
    ("youku",       "Youku",         "#1b9cfc", "YK", ("youku.com", "youku.tv")),
    ("tudou",       "Tudou",         "#ff6600", "TD", ("tudou.com",)),
    ("iqiyi",       "iQIYI",         "#00cc36", "IQ", ("iqiyi.com", "iq.com")),
    ("douyin",      "Douyin",        "#fe2c55", "DY", ("douyin.com",)),
    ("weibo",       "Weibo",         "#e6162d", "WB", ("weibo.com", "weibo.cn")),
    ("miaopai",     "Miaopai",       "#ffcc00", "MP", ("miaopai.com",)),
    ("huya",        "Huya",          "#ff7d00", "HY", ("huya.com",)),
    ("douyu",       "Douyu",         "#ff5d23", "DO", ("douyu.com", "douyu.tv")),
    ("acfun",       "AcFun",         "#fd4c5b", "AC", ("acfun.cn",)),
    ("xigua",       "Xigua",         "#f04142", "XG", ("ixigua.com", "xigua.com")),
    ("tencent",     "Tencent Video", "#0052d9", "TX", ("v.qq.com",)),
    ("mgtv",        "MGTV",          "#ff6d00", "MG", ("mgtv.com",)),
    ("naver",       "Naver TV",      "#03c75a", "NV", ("naver.com", "tv.naver.com")),
    ("kakao",       "Kakao TV",      "#ffcd00", "KA", ("kakao.com", "tv.kakao.com")),
    ("afreeca",     "AfreecaTV",     "#00b1e5", "AF", ("afreecatv.com", "soop.co.kr")),

    # --- Music & audio ---
    ("soundcloud",  "SoundCloud",    "#ff5500", "SC", ("soundcloud.com",)),
    ("mixcloud",    "Mixcloud",      "#5000ff", "MX", ("mixcloud.com",)),
    ("bandcamp",    "Bandcamp",      "#629aa9", "BC", ("bandcamp.com",)),
    ("audiomack",   "Audiomack",     "#ffa200", "AM", ("audiomack.com",)),
    ("deezer",      "Deezer",        "#a238ff", "DE", ("deezer.com",)),
    ("tidal",       "Tidal",         "#000000", "TI", ("tidal.com",)),
    ("audius",      "Audius",        "#7e1bcc", "AU", ("audius.co",)),
    ("jamendo",     "Jamendo",       "#f6a623", "JM", ("jamendo.com",)),
    ("lastfm",      "Last.fm",       "#d51007", "LF", ("last.fm",)),
    ("iheart",      "iHeartRadio",   "#c6002b", "IH", ("iheart.com", "iheartradio.com")),
    ("siriusxm",    "SiriusXM",      "#000000", "SX", ("siriusxm.com",)),
    ("pandora",     "Pandora",       "#3668ff", "PD", ("pandora.com",)),
    ("napster",     "Napster",       "#0066ff", "NP", ("napster.com",)),
    ("tumblr",      "Tumblr",        "#36465d", "TU", ("tumblr.com",)),

    # --- Images & art ---
    ("imgur",       "Imgur",         "#1bb76e", "IM", ("imgur.com",)),
    ("flickr",      "Flickr",        "#0063dc", "FL", ("flickr.com",)),
    ("unsplash",    "Unsplash",      "#000000", "UN", ("unsplash.com",)),
    ("500px",       "500px",         "#0099e5", "5P", ("500px.com",)),
    ("deviantart",  "DeviantArt",    "#05cc47", "DA", ("deviantart.com",)),
    ("pixiv",       "Pixiv",         "#0096fa", "PX", ("pixiv.net",)),
    ("behance",     "Behance",       "#1769ff", "BE", ("behance.net",)),
    ("dribbble",    "Dribbble",      "#ea4c89", "DR", ("dribbble.com",)),
    ("artstation",  "ArtStation",    "#13aff0", "AS", ("artstation.com",)),
    ("giphy",       "Giphy",         "#000000", "GY", ("giphy.com",)),
    ("tenor",       "Tenor",         "#0073ff", "TN", ("tenor.com",)),

    # --- Short video apps ---
    ("likee",       "Likee",         "#ff0050", "LK", ("likee.video",)),
    ("kwai",        "Kwai",          "#ff5000", "KW", ("kwai.com", "kwai-video.com")),
    ("triller",     "Triller",       "#ff2b2b", "TR", ("triller.co",)),
    ("coub",        "Coub",          "#f56c2d", "CO", ("coub.com",)),
    ("clash",       "Clash",         "#f5c400", "CL", ("clashapp.co",)),
    ("funimate",    "Funimate",      "#ff2f92", "FM", ("funimate.com",)),
    ("tiki",        "Tiki",          "#ff4b4b", "TK", ("tiki.vn",)),
    ("moj",         "Moj",           "#000000", "MJ", ("mojvideo.com",)),
    ("bigo",        "Bigo Live",     "#00e0ff", "BO", ("bigo.tv",)),
    ("younow",      "YouNow",        "#1b9cfc", "YN", ("younow.com",)),
    ("9gag",        "9GAG",          "#000000", "9G", ("9gag.com",)),

    # --- Anime & streaming ---
    ("crunchyroll", "Crunchyroll",   "#f47521", "CR", ("crunchyroll.com",)),
    ("funimation",  "Funimation",    "#5b0bb5", "FN", ("funimation.com",)),
    ("hidive",      "HIDIVE",        "#00aeef", "HD", ("hidive.com",)),
    ("vrv",         "VRV",           "#f96854", "VR", ("vrv.co",)),

    # --- News, sports & entertainment ---
    ("bbc",         "BBC",           "#bb1919", "BB", ("bbc.com", "bbc.co.uk")),
    ("cnn",         "CNN",           "#cc0000", "CN", ("cnn.com",)),
    ("bloomberg",   "Bloomberg",     "#000000", "BL", ("bloomberg.com",)),
    ("reuters",     "Reuters",       "#ff8000", "RE", ("reuters.com",)),
    ("aljazeera",   "Al Jazeera",    "#fa9000", "AJ", ("aljazeera.com",)),
    ("msnbc",       "MSNBC",         "#1e4d90", "MS", ("msnbc.com",)),
    ("cbs",         "CBS",           "#1c60a5", "CB", ("cbsnews.com", "cbs.com")),
    ("nbc",         "NBC",           "#7d5ebe", "NB", ("nbc.com", "nbcnews.com")),
    ("pbs",         "PBS",           "#2638c4", "PB", ("pbs.org",)),
    ("hbo",         "HBO / Max",     "#000000", "HB", ("hbo.com", "max.com")),
    ("discovery",   "Discovery",     "#00a0df", "DC", ("discovery.com",)),
    ("natgeo",      "Nat Geo",       "#ffcc00", "NG", ("nationalgeographic.com",)),
    ("mtv",         "MTV",           "#ff0000", "MT", ("mtv.com",)),
    ("comedycentral","Comedy Central","#f9d423","CM", ("cc.com", "comedycentral.com")),
    ("nick",        "Nickelodeon",   "#ff8000", "NK", ("nick.com", "nickelodeon.com")),
    ("cartoonnetwork","Cartoon Network","#000000","CT",("cartoonnetwork.com",)),
    ("buzzfeed",    "BuzzFeed",      "#ee3322", "BZ", ("buzzfeed.com",)),
    ("vice",        "Vice",          "#000000", "VC", ("vice.com",)),
    ("vox",         "Vox",           "#000000", "VX", ("vox.com",)),
    ("nowthis",     "NowThis",       "#e62b1e", "NT", ("nowthisnews.com",)),
    ("ted",         "TED",           "#e62b1e", "TE", ("ted.com",)),
    ("nba",         "NBA",           "#1d428a", "NA", ("nba.com",)),
    ("nfl",         "NFL",           "#013369", "NF", ("nfl.com",)),
    ("mlb",         "MLB",           "#041e42", "ML", ("mlb.com",)),
    ("nhl",         "NHL",           "#000000", "NH", ("nhl.com",)),
    ("espn",        "ESPN",          "#c8102e", "ES", ("espn.com", "espn.co.uk")),
    ("dazn",        "DAZN",          "#0b0b0b", "DN", ("dazn.com",)),

    # --- Learning & courses ---
    ("khanacademy","Khan Academy",   "#14bf96", "KH", ("khanacademy.org",)),
    ("coursera",    "Coursera",      "#0056d2", "CS", ("coursera.org",)),
    ("udemy",       "Udemy",         "#a435f0", "UD", ("udemy.com",)),
    ("edx",         "edX",           "#02262b", "EX", ("edx.org",)),
    ("skillshare",  "Skillshare",    "#00ff84", "SK", ("skillshare.com",)),
    ("masterclass", "MasterClass",   "#000000", "MA", ("masterclass.com",)),
    ("pluralsight", "Pluralsight",   "#f15b2a", "PL", ("pluralsight.com",)),
    ("udacity",     "Udacity",       "#02b3e4", "UC", ("udacity.com",)),
    ("futurelearn", "FutureLearn",   "#de00a5", "FU", ("futurelearn.com",)),
    ("codecademy",  "Codecademy",    "#1f4056", "CC", ("codecademy.com",)),

    # --- Creator, community & commerce ---
    ("patreon",     "Patreon",       "#f96854", "PA", ("patreon.com",)),
    ("substack",    "Substack",      "#ff6719", "SS", ("substack.com",)),
    ("medium",      "Medium",        "#000000", "MD", ("medium.com",)),
    ("wordpress",   "WordPress",     "#21759b", "WP", ("wordpress.com", "wordpress.org")),
    ("blogger",     "Blogger",       "#ff5722", "BG", ("blogspot.com", "blogger.com")),
    ("amazon",      "Amazon",        "#ff9900", "AZ", ("amazon.com", "amazon.in", "amazon.co.uk")),
    ("ebay",        "eBay",          "#e53238", "EB", ("ebay.com",)),
    ("etsy",        "Etsy",          "#f16521", "ET", ("etsy.com",)),
    ("aliexpress",  "AliExpress",    "#ff4747", "AX", ("aliexpress.com",)),
    ("alibaba",     "Alibaba",       "#ff6a00", "AL", ("alibaba.com",)),
    ("shopify",     "Shopify",       "#95bf47", "SH", ("shopify.com",)),

    ("other",       "Other link",    "#8b93a7", "?",  ()),
)

_SOCIAL_BY_KEY = {p[0]: p for p in SOCIAL_PLATFORMS}


# Guidance shown when a platform is selected: where to copy the link from, and
# how to use the tool. Platforms without an entry fall back to the template.
SOCIAL_HELP = {
    "youtube": ("Open the video, tap Share, then Copy link. The address bar works too.",
                "Paste it above and press Get video. Pick a quality: 360p plays in the "
                "browser, higher qualities download as an MP4."),
    "instagram": ("Open the post or Reel, tap the \u22ef menu, then Copy link. For Stories, "
                  "use the share sheet and Copy link.",
                  "Public posts resolve without an account. Private or age-gated posts need "
                  "a signed-in session (set SOCIAL_COOKIES)."),
    "facebook": ("On the video, tap Share, then Copy link. In the app, the \u22ef menu also "
                 "has Copy link.",
                 "Public videos work directly. Private groups and friends-only posts need a "
                 "signed-in session (set SOCIAL_COOKIES)."),
    "tiktok": ("Open the video, tap Share, then Copy link. The app gives you a short "
               "vm.tiktok.com link, which works here.",
               "Paste it and press Get video. TikTok serves single-file MP4s, so they usually "
               "play right in the browser."),
    "twitter": ("Open the post, tap Share, then Copy link. x.com and twitter.com links both "
                "work.",
                "Paste it and press Get video. Videos with sound play directly; GIFs come "
                "through as short MP4s."),
    "whatsapp": ("Open the video message, tap the \u22ef menu, then Forward or Copy link.",
                 "Only links that are publicly reachable work; media sent inside an end-to-end "
                 "chat is not addressable, so download it from the chat instead."),
    "snapchat": ("Open the Spotlight or Story, tap Share, then Copy link.",
                 "Public Spotlight posts resolve. Private snaps are not downloadable."),
    "linkedin": ("On the post, tap \u22ef then Copy link to post.",
                 "Public posts work. Posts that require sign-in need SOCIAL_COOKIES."),
    "pinterest": ("Open the pin, tap Share, then Copy link.",
                  "Video pins resolve. Image pins come back as a still."),
    "reddit": ("On the post, tap Share, then Copy link. redd.it short links work too.",
               "Direct video posts and Reddit-hosted video play in the browser. Posts that "
               "link out to another host are fetched from that host instead."),
    "threads": ("On the post, tap the share icon, then Copy link.",
                "Public posts work. Sign-in-only posts need SOCIAL_COOKIES."),
    "telegram": ("Open the message, tap \u22ef, then Copy Link. Public t.me channel links work.",
                 "Only public channels and groups are reachable; private chats are not."),
    "vimeo": ("Open the video, tap Share, then Copy link.",
              "Public videos resolve. Password-protected or private videos need SOCIAL_COOKIES."),
    "dailymotion": ("Open the video, tap Share, then Copy link. dai.ly short links work.",
                    "Dailymotion often serves HLS, so the tool rewrites the playlist and plays "
                    "it in the browser; the download is saved as an MP4."),
    "twitch": ("For a live stream, copy the channel URL. For a past broadcast, open the VOD "
               "and tap Share, then Copy link.",
               "VODs download fine. A live stream can only be captured while it is running."),
    "soundcloud": ("Open the track, tap Share, then Copy link.",
                   "Tracks download as audio (MP3/M4A), not video."),
    "bilibili": ("Open the video, tap Share, then Copy link. b23.tv short links work.",
                 "Some videos are region-locked or need a signed-in session; set "
                 "SOCIAL_COOKIES for those."),
    "rumble": ("Open the video, tap Share, then Copy link.",
               "Rumble videos resolve to a single MP4 and usually play in the browser."),
    "odysee": ("Open the video, tap Share, then Copy link.",
               "LBRY/Odysee videos resolve directly."),
    "streamable": ("Open the video, tap Share, then Copy link.",
                   "Streamable serves a single MP4, so playback works in the browser."),
    "imgur": ("Open the post, tap \u22ef, then Copy link.",
              "Video posts resolve. Single images come back as a still."),
    "kick": ("Open the channel or VOD, tap Share, then Copy link.",
             "VODs download. Live streams are only captured while running."),
    "archive": ("Open the item page and copy the address from the browser bar.",
                "Internet Archive items often expose several files; the tool lists the "
                "playable video or audio."),
    "tumblr": ("On the post, tap the share icon, then Copy link.",
               "Public posts work. Blog-visible-only posts need SOCIAL_COOKIES."),
    "9gag": ("Open the post, tap Share, then Copy link.",
             "Video posts resolve; image posts come back as a still."),
    "patreon": ("Open the post, tap Share, then Copy link.",
                "Only public posts resolve; patron-only posts need SOCIAL_COOKIES."),
    "bbc": ("Open the programme page and copy the address from the browser bar.",
            "iPlayer content may be geo-restricted and need a signed-in session."),
    "other": ("Copy the video page's address from your browser bar, or use the site's own Share "
              "and Copy link option.",
              "Any link yt-dlp understands works here, even for sites not listed above. Paste "
              "it and press Get video; if the site refuses anonymous access, it will say so."),
}

_HELP_DEFAULT = (
    "Open the video on {name}, tap the Share button, then choose Copy link. You can also "
    "copy the address straight from your browser's address bar.",
    "Paste the link in the box above and press Get video. Play it in the browser when a "
    "single-file stream is offered, otherwise pick a quality to download it as an MP4.",
)


def social_help(key: str, name: str) -> dict:
    where, how = SOCIAL_HELP.get(key, _HELP_DEFAULT)
    return {"where": where.format(name=name), "how": how.format(name=name)}


def _host_matches(host: str, roots) -> bool:
    host = (host or "").lower().lstrip(".")
    return any(host == r or host.endswith("." + r) for r in roots)


def detect_platform(url: str) -> str:
    """Return the platform key for a URL, or 'other'."""
    host = urllib.parse.urlparse(url).hostname or ""
    for key, _name, _color, _badge, roots in SOCIAL_PLATFORMS:
        if roots and _host_matches(host, roots):
            return key
    return "other"


FEATURED_KEYS = (
    "youtube", "instagram", "facebook", "tiktok", "twitter", "whatsapp",
    "snapchat", "linkedin", "pinterest", "reddit", "threads", "telegram",
    "vimeo", "dailymotion", "twitch", "soundcloud",
)


def social_platform_list() -> list:
    return [{"key": k, "name": n, "color": c, "badge": b,
             "featured": k in FEATURED_KEYS,
             "help": social_help(k, n)}
            for k, n, c, b, _r in SOCIAL_PLATFORMS]


def _ytdlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def _ffmpeg_path() -> str | None:
    """Locate ffmpeg, preferring the copy shipped beside the app.

    A hosted container often has no ffmpeg on PATH and no root to install one,
    so `bin/ffmpeg` next to this file is checked first.
    """
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "ffmpeg")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return shutil.which("ffmpeg")


def _ffmpeg_available() -> bool:
    return _ffmpeg_path() is not None


# yt-dlp picks its own YouTube player clients. Pinning them here used to cut
# the format list down hard (a pinned android/ios/web/tv_embedded set returned
# 1 format where the default returns 24+), so the list is left to yt-dlp.
_YT_IMPERSONATE = "chrome"


@functools.lru_cache(maxsize=1)
def _impersonate_target():
    """A curl_cffi impersonation target, or None when it is unavailable.

    yt-dlp wants an `ImpersonateTarget` instance here, not a bare string; a
    string trips an assertion inside yt-dlp. curl_cffi ships with the app, so
    this normally succeeds and gives every extractor a real Chrome TLS
    fingerprint, which is what unblocks the bot-checked sites. Cached: probing
    the handlers means building a YoutubeDL, which is too costly per request.
    """
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except ImportError:
        return None
    target = ImpersonateTarget(_YT_IMPERSONATE)
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True}) as probe:
            for handler in probe._request_director.handlers.values():
                if hasattr(handler, "is_supported_target") and handler.is_supported_target(target):
                    return target
    except Exception:  # noqa: BLE001 - impersonation is a best-effort upgrade
        return None
    return None


def _base_ydl_opts(*, quiet: bool = True, impersonate: bool = True) -> dict:
    opts = {
        "quiet": quiet,
        "no_warnings": quiet,
        "noplaylist": True,
        "nocheckcertificate": True,
        "extract_flat": False,
        "skip_download": True,
        "socket_timeout": 30,
        "retries": 2,
        "http_headers": {"User-Agent": BROWSER_UA},
    }
    if impersonate:
        target = _impersonate_target()
        if target is not None:
            opts["impersonate"] = target
    # YouTube refuses requests from hosting-provider addresses whatever player
    # client is used, so the only real cure is a different egress address.
    proxy = os.environ.get("SOCIAL_PROXY", "").strip()
    if proxy and not _is_loopback_proxy(proxy):
        opts["proxy"] = proxy
    ffmpeg = _ffmpeg_path()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    return opts


def _is_loopback_proxy(proxy: str) -> bool:
    """True for a proxy pointing back at this box.

    Such a proxy cannot change the egress address, so setting it would only add a
    hop: yt-dlp would talk to itself and every download would time out.
    """
    host = proxy.split("://", 1)[-1].split("@")[-1]
    if host.startswith("["):  # [::1]:8080 keeps its colon inside the brackets
        host = host[1:host.index("]")] if "]" in host else host
    else:
        host = host.split(":")[0]
    return host.lower() in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _cookie_file() -> str:
    """Write any configured cookies to a temp file and return its path.

    Sites like Instagram and Facebook often refuse anonymous requests. A user
    can drop a Netscape cookies.txt in via SOCIAL_COOKIES and the extractor
    picks it up. Empty means none are configured.
    """
    raw = os.environ.get("SOCIAL_COOKIES", "").strip()
    if not raw:
        return ""
    if os.path.exists(raw):
        return raw
    path = os.path.join(tempfile.gettempdir(), "social_cookies.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw.replace("\\n", "\n"))
        return path
    except OSError:
        return ""


def _run_extract(url: str, opts: dict):
    """One yt-dlp run. Returns `(info, cookies)`."""
    import yt_dlp

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        cookies = {c.name: c.value for c in ydl.cookiejar}
    return info, cookies


# Sites fail for different reasons, and the reasons point at different fixes: a
# bot check wants a real browser fingerprint, a stale player client wants a
# different one, and a broken extractor wants the generic one. Trying them in
# order means a single bad guess no longer ends the download.
_EXTRACT_STRATEGIES = ("default", "no-impersonate", "alt-clients", "generic")


def _strategy_opts(strategy: str) -> dict:
    """yt-dlp options for one extraction strategy."""
    if strategy == "no-impersonate":
        # A few sites reject curl_cffi's TLS fingerprint outright; the plain
        # urllib handler is the fallback that works there.
        return _base_ydl_opts(impersonate=False)
    if strategy == "alt-clients":
        # YouTube rotates what each player client is allowed to see. The default
        # set can come back empty while these still answer.
        opts = _base_ydl_opts()
        opts["extractor_args"] = {
            "youtube": {"player_client": ["android_vr", "ios", "tv", "web_safari"]}
        }
        return opts
    if strategy == "generic":
        return dict(_base_ydl_opts(), force_generic_extractor=True)
    return _base_ydl_opts()


def _extract_info(url: str) -> tuple:
    """Resolve a URL, walking a chain of strategies until one works.

    Returns `(info, cookies)` with `info["_source"]` naming the strategy that
    succeeded and `info["_attempts"]` listing what was tried, so the UI can say
    which source answered instead of silently pretending nothing went wrong.
    """
    cookie = _cookie_file()
    attempts = []
    last_exc = None
    last_cookies = {}

    for strategy in _EXTRACT_STRATEGIES:
        # Building the options is itself fallible (probing curl_cffi, an unknown
        # extractor arg), so it happens inside the guarded block: a strategy that
        # cannot even be configured must not end the chain.
        try:
            opts = _strategy_opts(strategy)
            if cookie:
                opts["cookiefile"] = cookie
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            attempts.append((strategy, str(exc)[:160]))
            continue
        # One extra pass per strategy: extraction hiccups (an empty format list
        # from a CDN blip) look identical to a real refusal on the first try.
        for attempt in range(2):
            try:
                info, cookies = _run_extract(url, opts)
            except Exception as exc:  # noqa: BLE001 - reported to the user
                last_exc = exc
                attempts.append((strategy, str(exc)[:160]))
                if attempt == 0 and _is_transient(exc):
                    time.sleep(1.2)
                    continue
                break
            if isinstance(info, dict):
                info["_source"] = strategy
                info["_attempts"] = attempts
                return info, cookies
            last_exc = SocialError("That link has no playable media.")
            attempts.append((strategy, "no playable media"))
            break

    # Every extractor gave up, but the page may still carry the media in its own
    # HTML (Pinterest's videoUrls, a bare <video src>). Scrape before failing.
    scraped = _html_media_fallback(url)
    if scraped is not None:
        scraped["_source"] = "html-scrape"
        scraped["_attempts"] = attempts
        return scraped, last_cookies

    attempts.append(("html-scrape", "no media found in the page"))
    message = _with_platform_hint(url, _friendly_download_error(last_exc)
                                  if last_exc else "That link could not be read.")
    err = SocialError(message)
    err.attempts = attempts
    raise err from last_exc


def _is_transient(exc: Exception) -> bool:
    """Whether a failed extraction is worth one immediate retry."""
    text = str(exc).lower()
    markers = ("no video formats found", "unable to extract", "temporarily",
               "timed out", "connection reset", "empty response", "http error 5")
    return any(m in text for m in markers)


def _friendly_download_error(exc: Exception) -> str:
    """Turn a yt-dlp failure into something a user can act on."""
    text = str(exc)
    low = text.lower()
    # YouTube answers 403, an empty HLS body, or "Requested format is not
    # available" for every video when the request comes from a hosting provider,
    # no matter which player client is tried. The generic cookie advice sends
    # people to a fix that cannot work, so this case is named before the cookie
    # case below catches "sign in" wording.
    hosting_block = ("403" in low or "has not made this content available" in low
                     or "requested format is not available" in low)
    if hosting_block:
        return ("The site refused this download. YouTube blocks servers like this "
                "one outright, so its links cannot be fetched here even though other "
                "sites work. Set SOCIAL_PROXY to route through a proxy, or use a "
                "different link.")
    if "private" in low or "login" in low or "authentication" in low:
        return "This post is private, so it cannot be fetched without an account."
    if "sign in" in low or "bot" in low or "cookies" in low:
        return ("This site wants a signed-in session. Instagram, Facebook, "
                "Reddit and Vimeo often do. Add a cookies.txt via the "
                "SOCIAL_COOKIES setting and try again.")
    if "unsupported url" in low:
        return "That link is not from a supported site."
    if "no video could be found" in low:
        # X/Twitter serves video only to a logged-in session, and a video-less
        # post looks the same to yt-dlp, so both possibilities are named.
        return ("No video was found at that link. It may have no video, or the "
                "site may want a signed-in session - X/Twitter does. Add "
                "cookies via SOCIAL_COOKIES for those.")
    if "no video" in low or "no media" in low or "no formats" in low:
        return "No downloadable video or audio was found at that link."
    if "not available" in low or "removed" in low or "deleted" in low:
        return "That post is unavailable; it may have been removed."
    if "404" in low or "not found" in low:
        return ("That post was not found. Check the link, or it may have been "
                "removed.")
    if "timed out" in low or "timeout" in low:
        return "The site took too long to answer. Please try again."
    # Strip yt-dlp's "ERROR: " prefix and any extractor tag for readability.
    clean = re.sub(r"^ERROR:\s*", "", text)
    return clean[:300]


class SocialError(Exception):
    """Raised with a message that is safe to show to the user."""


def _html_title(html: str) -> str:
    for pat in (r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:title["\']',
                r'<title[^>]*>([^<]+)</title>'):
        m = re.search(pat, html, re.I)
        if m:
            return _html_unescape(m.group(1)).strip()
    return ""


def _html_og_image(html: str) -> str:
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
                  html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
                      html, re.I)
    return _html_unescape(m.group(1)).strip() if m else ""


def _html_unescape(text: str) -> str:
    return (text.replace("&amp;", "&").replace("&#x2F;", "/")
            .replace("&quot;", '"').replace("&#39;", "'"))


_HTML_IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|avif)(\?|$)", re.I)
# Only URLs that name a media file are trusted. Sites also embed their *player*
# page URL in the same payloads (Vimeo's player.vimeo.com/video/<id> is HTML,
# not a stream), and handing that to the browser yields an HTML file.
_HTML_MEDIA_EXT = re.compile(r"\.(mp4|m4v|webm|mkv|mov|m3u8|mp3|m4a|aac|ogg|opus|wav)(\?|$)", re.I)


def _html_media_fallback(url: str):
    """Scrape a page's HTML for playable media, yt-dlp-shaped, or None.

    A site's extractor can fail (or 404 on an unauth API call) while the page
    itself still carries the media URL - Pinterest keeps plain MP4 and m3u8
    links in a `videoUrls` list. This is the last resort before giving up.
    """
    try:
        resp = _request_session().get(url, timeout=30)
    except Exception:  # noqa: BLE001 - a failed scrape is just "no fallback"
        return None
    if resp.status_code >= 400:
        return None
    html = resp.text or ""
    if not html:
        return None
    # JSON embedded in <script> escapes its slashes: https:\/\/host\/file.mp4
    text = html.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")

    found = []  # (url, is_hls)

    # Pinterest: "videos":{"videoUrls":["....mp4","....m3u8"]}
    for block in re.findall(r'"videoUrls"\s*:\s*\[(.*?)\]', text, re.S):
        for u in re.findall(r'"(https?://[^"]+)"', block):
            found.append((u, ".m3u8" in u.lower()))
    # Threads / Instagram-style payloads
    for u in re.findall(r'"video_url"\s*:\s*"(https?://[^"]+)"', text):
        found.append((u, ".m3u8" in u.lower()))
    # <meta property="og:video"> and twitter:player:stream
    for tag in re.findall(r"<meta\b[^>]*>", text, re.I):
        if re.search(r"og:video|twitter:player:stream", tag, re.I):
            m = re.search(r'content=["\']([^"\']+)["\']', tag, re.I)
            if m:
                found.append((m.group(1), ".m3u8" in m.group(1).lower()))
    # <video src> / <source src>
    for u in re.findall(r'<(?:video|source)\b[^>]*\bsrc=["\']([^"\']+)["\']', text, re.I):
        found.append((u, ".m3u8" in u.lower()))
    # JSON-LD contentUrl
    for u in re.findall(r'"contentUrl"\s*:\s*"(https?://[^"]+)"', text):
        found.append((u, ".m3u8" in u.lower()))

    formats, seen = [], set()
    for raw, is_hls in found:
        u = _html_unescape(raw)
        if not u.lower().startswith("http") or u in seen:
            continue
        if _HTML_IMAGE_EXT.search(u):
            continue
        # A DASH manifest (.mpd) is not something this app can stream or merge.
        if u.lower().split("?")[0].endswith(".mpd"):
            continue
        # Require a media extension: player page URLs look like streams but are
        # HTML, and downloading one just saves a web page.
        if not _HTML_MEDIA_EXT.search(u):
            continue
        seen.add(u)
        # Pinterest names the tier in the path: /videos/mc/720p/<hash>.mp4
        height = None
        m = re.search(r"/(\d{3,4})p/", u)
        if m:
            height = int(m.group(1))
        label = f"{height}p MP4" if height else ("HLS MP4" if is_hls else "MP4")
        formats.append({
            "format_id": f"html-{len(formats)}",
            "url": u,
            "ext": "mp4",
            "format_note": label,
            "protocol": "m3u8_native" if is_hls else "https",
            # Reported as a progressive track: these URLs carry both streams.
            "vcodec": "avc1", "acodec": "aac",
            "width": None, "height": height, "fps": None, "abr": None,
            "filesize": None,
        })
    if not formats:
        return None
    # A plain MP4 plays and downloads more simply than an HLS manifest, so it
    # sorts first.
    formats.sort(key=lambda f: 1 if f["protocol"].startswith("m3u8") else 0)
    return {
        "title": _html_title(text) or "Video",
        "thumbnail": _html_og_image(text),
        "formats": formats,
        "extractor": "html-fallback",
    }


# Threads answers every crawler with its login page, so no extractor can read a
# post without a session. Say that plainly instead of "unsupported site".
_PLATFORM_HINTS = {
    "threads": ("Threads only serves posts to a signed-in session. Add a "
                "cookies.txt via the SOCIAL_COOKIES setting and try again."),
}


def _with_platform_hint(url: str, message: str) -> str:
    hint = _PLATFORM_HINTS.get(detect_platform(url))
    return hint or message


def _pick_thumbnail(info: dict) -> str:
    thumb = info.get("thumbnail")
    if thumb:
        return thumb
    thumbs = info.get("thumbnails") or []
    for t in reversed(thumbs):
        if t.get("url"):
            return t["url"]
    return ""


def _label_format(f: dict, short: int | None) -> str:
    """A short human label such as '720p MP4' or 'Audio MP3'."""
    ext = (f.get("ext") or "").upper()
    if f.get("vcodec") == "none":
        abr = f.get("abr")
        return f"Audio {ext}" + (f" {int(abr)}kbps" if abr else "")
    if short:
        label = f"{short}p"
    else:
        # Some sites name their tiers instead of giving a height (Facebook's
        # "sd" and "hd"), so fall back to that name.
        label = (f.get("format_note") or f.get("format_id") or "Video")
        label = str(label).upper() if len(str(label)) <= 4 else str(label).title()
    fps = f.get("fps")
    if fps and fps >= 50:
        label += f" {int(fps)}fps"
    return f"{label} {ext}".strip()


def _sort_formats(formats: list) -> list:
    """Progressive first (they play immediately), then by quality, best last."""
    def key(f):
        return (1 if f["progressive"] else 0,
                1 if f["audio_only"] else 0,
                f.get("height") or 0,
                f.get("abr") or 0)
    return sorted(formats, key=key)


def social_extract(url: str) -> dict:
    """Resolve a social URL into metadata plus a list of offered formats."""
    if not _ytdlp_available():
        raise SocialError("The downloader engine (yt-dlp) is not installed on "
                          "the server.")
    info, cookies = _extract_info(url)

    raw = [f for f in (info.get("formats") or []) if f.get("url")]
    if not raw and info.get("url"):
        raw = [info]

    formats = []
    for f in raw:
        if (f.get("ext") or "") in ("mhtml", "storyboard"):
            continue
        vcodec, acodec = f.get("vcodec"), f.get("acodec")
        # A codec of the string "none" means that half is missing; an absent
        # codec (None) means the extractor did not say, which still counts as
        # present - Facebook reports its progressive MP4s that way.
        has_video = vcodec != "none"
        has_audio = acodec != "none"
        if not has_video and not has_audio:
            continue
        if (f.get("protocol") or "") == "mhtml":
            continue

        width, height = f.get("width"), f.get("height")
        # Quality is the short side: for portrait clips the height is the long
        # side, so 540p would otherwise read as 1024p.
        short = min(width, height) if (width and height) else (height or width)
        progressive = has_video and has_audio
        is_audio = not has_video
        size = f.get("filesize") or f.get("filesize_approx")
        resolution = f"{width}x{height}" if (width and height) else ""
        formats.append({
            "format_id": str(f.get("format_id")),
            "label": _label_format(f, short),
            "ext": f.get("ext") or "",
            "height": short,
            "resolution": resolution,
            "fps": f.get("fps"),
            "abr": f.get("abr"),
            "size": size,
            "progressive": progressive,
            "audio_only": is_audio,
            "kind": "audio" if is_audio else "video",
            "playable": progressive,
        })

    if not formats:
        raise SocialError("No downloadable video or audio was found at that link.")

    formats = _sort_formats(formats)
    # One entry per distinct quality, preferring a progressive file when both a
    # progressive and a merged-only version exist for the same quality.
    best_by_quality: dict = {}
    for f in formats:
        # Height is None on sites that name their tiers (Facebook's sd/hd), so
        # fall back to the label there to keep those tiers apart.
        quality = f["height"] if f["height"] else f["label"]
        key = (f["kind"], quality, round(f["abr"] or 0, -1))
        current = best_by_quality.get(key)
        if current is None or (f["progressive"] and not current["progressive"]):
            best_by_quality[key] = f
    deduped = _sort_formats(list(best_by_quality.values()))

    # Best of each kind. The list is sorted worst-first, so the last match wins.
    best_progressive = next((f for f in reversed(deduped) if f["progressive"]), None)
    best_video = next((f for f in reversed(deduped) if f["kind"] == "video"), None)
    best_audio = next((f for f in reversed(deduped) if f["kind"] == "audio"), None)

    default = best_progressive or best_video or (deduped[-1] if deduped else None)

    return {
        "url": url,
        "platform": detect_platform(url),
        "source": info.get("_source") or "default",
        "attempts": _attempts_public(info.get("_attempts")),
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
        "duration": info.get("duration"),
        "duration_text": _duration_text(info.get("duration")),
        "thumbnail": _pick_thumbnail(info),
        "is_live": bool(info.get("is_live")),
        "formats": deduped,
        "default_format": default["format_id"] if default else None,
        "can_play": bool(best_progressive),
        "play_format": best_progressive["format_id"] if best_progressive else None,
        "best_video": best_video["format_id"] if best_video else None,
        "best_audio": best_audio["format_id"] if best_audio else None,
        "ffmpeg": _ffmpeg_available(),
    }


_SOURCE_LABELS = {
    "default": "primary extractor",
    "no-impersonate": "plain HTTP client",
    "alt-clients": "alternate player clients",
    "generic": "generic extractor",
    "html-scrape": "page HTML",
}


def _attempts_public(attempts) -> list:
    """Turn the internal attempt log into what the UI shows."""
    out = []
    for strategy, reason in (attempts or []):
        out.append({
            "source": strategy,
            "label": _SOURCE_LABELS.get(strategy, strategy),
            "reason": reason,
        })
    return out


def _duration_text(seconds) -> str:
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# Direct media URLs expire, and merging can take minutes, so an entry is kept
# for a while: the page resolves a link, then asks for a specific format, then
# downloads the result. Keyed by a random token so nothing user-specific leaks.
_SOCIAL_STORE: dict = {}
_SOCIAL_TTL = 3600
_SOCIAL_LOCK = threading.Lock()


def _social_store_put(payload: dict) -> str:
    token = secrets.token_urlsafe(12)
    with _SOCIAL_LOCK:
        now = time.time()
        for key, (at, _v) in list(_SOCIAL_STORE.items()):
            if now - at > _SOCIAL_TTL:
                _SOCIAL_STORE.pop(key, None)
        _SOCIAL_STORE[token] = (now, payload)
    return token


def _social_store_get(token: str):
    record = _SOCIAL_STORE.get(token)
    if not record or (time.time() - record[0]) > _SOCIAL_TTL:
        return None
    return record[1]


def _format_for_url(url: str, format_id: str):
    """Resolve one format into a spec the stream and download routes can use.

    A single-file (or audio-only) format is handed over directly, an HLS
    playlist is proxied, and a video-only track is paired with audio and merged.
    """
    info, cookies = _extract_info(url)
    formats = info.get("formats") or ([info] if info.get("url") else [])
    chosen = next((f for f in formats if str(f.get("format_id")) == str(format_id)), None)
    if chosen is None and formats:
        chosen = formats[-1]
    if chosen is None:
        raise SocialError("That format is no longer available; fetch the link again.")

    has_video = chosen.get("vcodec") != "none"
    has_audio = chosen.get("acodec") != "none"
    progressive = has_video and has_audio

    protocol = (chosen.get("protocol") or "").lower()
    if protocol.startswith("m3u8"):
        # An HLS manifest: the URL is a playlist, not a file, so it is rewritten
        # to same-origin segment URLs before the browser sees it.
        return {
            "hls": True,
            "format_id": str(chosen.get("format_id")),
            "url": chosen["url"],
            "headers": chosen.get("http_headers") or {},
            "cookies": cookies,
            "ext": "mp4",
            "title": info.get("title") or "video",
            "is_audio": not has_video,
        }

    if progressive or not has_video:
        # Single file (video+audio, or an audio track): hand it over directly.
        return {
            "direct": True,
            "url": chosen["url"],
            "headers": chosen.get("http_headers") or {},
            "cookies": cookies,
            "ext": chosen.get("ext") or "mp4",
            "title": info.get("title") or "video",
            "is_audio": not has_video,
        }

    # Video-only: pair it with the best audio and merge. Needs ffmpeg.
    if not _ffmpeg_available():
        raise SocialError("High-quality merging needs ffmpeg, which is not "
                          "installed on the server. Pick a single-file format.")
    audio = None
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") != "none":
            if audio is None or (f.get("abr") or 0) > (audio.get("abr") or 0):
                audio = f
    return {
        "direct": False,
        "video": chosen,
        "audio": audio,
        "cookies": cookies,
        "ext": "mp4",
        "title": info.get("title") or "video",
    }


# HLS playlists proxied for the social downloader. Each entry keeps the segment
# URLs together with the headers/cookies they need, since some CDNs sign each
# segment against the session that produced the manifest.
_SOCIAL_HLS: dict = {}
_SOCIAL_HLS_TTL = 1800


def _prune_hls():
    """Drop expired HLS playlists so the cache cannot grow without bound."""
    cutoff = time.time() - _SOCIAL_HLS_TTL
    for token in [t for t, rec in _SOCIAL_HLS.items() if rec[0] < cutoff]:
        _SOCIAL_HLS.pop(token, None)


def _social_hls_playlist(url: str, headers: dict, cookies: dict, depth: int = 0):
    """Fetch an HLS manifest and return it rewritten to same-origin segments."""
    try:
        resp = _request_session().get(url, headers=headers,
                                      cookies=cookies or None, timeout=60)
    except _request_error() as exc:
        raise SocialError(f"The stream could not be fetched ({exc}).") from exc
    text = resp.text or ""
    if not text.startswith("#EXTM3U"):
        raise SocialError("The source did not return a valid stream.")

    lines = [ln.strip() for ln in text.splitlines()]
    # A master playlist points at variant playlists; follow the first one.
    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines) and depth < 3:
        variant = next((ln for ln in lines
                        if ln and not ln.startswith("#")), None)
        if variant:
            return _social_hls_playlist(urllib.parse.urljoin(url, variant),
                                        headers, cookies, depth + 1)

    segments = [urllib.parse.urljoin(url, ln) for ln in lines
                if ln and not ln.startswith("#")]
    if not segments:
        raise SocialError("The stream had no playable segments.")

    token = secrets.token_urlsafe(12)
    _prune_hls()
    _SOCIAL_HLS[token] = (time.time(), segments, headers, cookies)

    out = []
    index = 0
    for ln in lines:
        if ln and not ln.startswith("#"):
            out.append(f"/api/social/hls/{token}/{index}.ts")
            index += 1
        else:
            out.append(ln)
    if "#EXT-X-ENDLIST" not in text:
        out.append("#EXT-X-ENDLIST")
    return Response("\n".join(out) + "\n",
                    headers={"Content-Type": "application/vnd.apple.mpegurl",
                             "Cache-Control": "no-store"})


def _simple_cookie(name: str, value: str, domain: str = ""):
    """A cookielib cookie for one name/value pair."""
    import http.cookiejar as cookiejar
    domain = domain.lstrip(".") or ""
    return cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=bool(domain),
        domain_initial_dot=False, path="/", path_specified=True,
        secure=False, expires=None, discard=True, comment=None,
        comment_url=None, rest={}, rfc2109=False)


def _failover_download(page_url: str, filename: str):
    """Download through yt-dlp when a resolved CDN link has gone stale.

    Returns a streaming response, or None when even this source refuses, so the
    caller can report one clear error instead of two half-truths.
    """
    workdir = tempfile.mkdtemp(prefix="failover-")
    try:
        path = _download_playlist_item(page_url, workdir)
        path = _apply_sponsor_metadata(path)
        branded = _branded(filename)
        size = os.path.getsize(path)

        def stream_and_cleanup():
            try:
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(256 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

        return Response(stream_and_cleanup(), headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
            "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(branded)}",
        })
    except Exception:  # noqa: BLE001 - the caller falls back to its own error
        shutil.rmtree(workdir, ignore_errors=True)
        return None


def _selector_for_spec(spec: dict) -> str:
    """A yt-dlp format selector for a resolved spec."""
    if spec.get("video"):
        video = spec["video"]
        audio = spec.get("audio")
        if audio is not None:
            return f"{video.get('format_id')}+{audio.get('format_id')}"
        return str(video.get("format_id"))
    return spec.get("format_id") or "best"


def _download_to_file(page_url: str, selector: str, workdir: str,
                      cookies: dict) -> str:
    """Download a format (merging audio when needed) into `workdir`.

    yt-dlp drives the download so HLS playlists and DASH pairs are handled the
    same way; ffmpeg does the muxing for separate video and audio tracks.
    """
    import yt_dlp

    opts = _base_ydl_opts(quiet=True)
    opts["skip_download"] = False
    opts["outtmpl"] = os.path.join(workdir, "media.%(ext)s")
    opts["merge_output_format"] = "mp4"
    if selector:
        opts["format"] = selector
    cookie = _cookie_file()
    if cookie:
        opts["cookiefile"] = cookie

    # A format that was listed at resolve time can be gone by the time the file
    # is fetched (a live stream ended, a CDN trimmed its ladder). Retrying with a
    # looser selector is cheaper than failing the download the user already chose.
    attempts = [selector] if selector else [None]
    if selector:
        attempts += [a for a in ("bv*+ba/b", "b", None) if a != selector]

    last_exc = None
    for choice in attempts:
        if choice:
            opts["format"] = choice
        else:
            opts.pop("format", None)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # Carry over the cookies gathered while extracting, since the
                # signed media URL is only valid alongside them.
                for name, value in (cookies or {}).items():
                    ydl.cookiejar.set_cookie(_simple_cookie(name, value))
                ydl.extract_info(page_url, download=True)
        except Exception as exc:  # noqa: BLE001 - try the next selector
            last_exc = exc
            for name in os.listdir(workdir):
                try:
                    os.remove(os.path.join(workdir, name))
                except OSError:
                    pass
            continue

        for name in sorted(os.listdir(workdir)):
            if name.startswith("media.") and os.path.getsize(os.path.join(workdir, name)) > 0:
                return os.path.join(workdir, name)

    if last_exc is not None:
        raise last_exc
    raise SocialError("The file could not be produced.")


# A playlist link is resolved flat and fast: entry URLs and titles only, no
# per-video extraction, so a 200-item list comes back in seconds instead of
# minutes. The user then picks what to download.
# Each item is downloaded in full before the zip is sent, so this is really a
# cap on how long one request may run.
# A zip is streamed only once every file in it exists, so one request has to stay
# open for the whole batch inside the server's 300s timeout. 40 items fits: at the
# measured ~5s per track a batch finishes well before the timeout, and it is far
# enough that a typical mix playlist goes in one go.
PLAYLIST_BATCH_LIMIT = 40

_PLAYLIST_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com",
                   "soundcloud.com", "vimeo.com", "dailymotion.com")


def _looks_like_playlist(url: str) -> bool:
    """Cheap check for a URL that names a collection rather than one video."""
    parts = urllib.parse.urlparse(url)
    host = (parts.hostname or "").lower()
    if not any(_host_matches(host, (h,)) for h in _PLAYLIST_HOSTS):
        return False
    query = urllib.parse.parse_qs(parts.query)
    path = parts.path.lower()
    if "list" in query and query["list"]:
        return True
    if host.endswith("soundcloud.com") and "/sets/" in path:
        return True
    if "vimeo.com" in host and ("/album/" in path or "/channels/" in path
                                or "/showcase/" in path):
        return True
    if "dailymotion.com" in host and path.startswith("/playlist/"):
        return True
    return False


def _title_from_url(url: str) -> str:
    """A readable name from a URL slug, used when flat entries carry no title."""
    path = urllib.parse.urlparse(url or "").path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    slug = urllib.parse.unquote(slug)
    slug = re.sub(r"[-_]+", " ", slug).strip()
    # An API endpoint ends in a bare numeric id (`/tracks/295`); showing "295"
    # as a title is worse than showing nothing and letting the caller fall back.
    if not slug or slug.isdigit():
        return ""
    return slug[:1].upper() + slug[1:]


def _playlist_entries(url: str) -> dict:
    """Flat-extract a playlist into entries the UI can list and download."""
    import yt_dlp

    opts = _base_ydl_opts(quiet=True)
    # Flat extraction skips per-video work; in_playlist keeps the entries in the
    # order the playlist author chose.
    opts.update({"extract_flat": "in_playlist", "noplaylist": False,
                 "playlistend": 500, "skip_download": True})
    cookie = _cookie_file()
    if cookie:
        opts["cookiefile"] = cookie

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not isinstance(info, dict):
        raise SocialError("That playlist could not be read.")
    raw = [e for e in (info.get("entries") or []) if e]
    if not raw:
        raise SocialError("That playlist is empty or private.")

    entries = []
    for index, e in enumerate(raw, start=1):
        page = e.get("url") or e.get("webpage_url") or ""
        # Flat entries sometimes carry only an id; rebuild the watch URL so the
        # download step has something to resolve.
        if page and not page.lower().startswith("http"):
            page = f"https://www.youtube.com/watch?v={page}"
        entries.append({
            "index": index,
            "id": str(e.get("id") or index),
            "title": (e.get("title") or _title_from_url(page)
                      or f"Track {e.get('id') or index}"),
            "url": page,
            "duration": e.get("duration"),
            "duration_text": _duration_text(e.get("duration")),
            "uploader": e.get("uploader") or e.get("channel") or "",
        })

    return {
        "url": url,
        "title": info.get("title") or info.get("album") or "Playlist",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "count": len(entries),
        "truncated": len(raw) >= 500,
        "entries": entries,
    }


@app.route("/api/social/playlist", methods=["POST"])
def api_social_playlist():
    """List the items in a playlist link."""
    link = _request_link()
    if not link:
        return jsonify({"ok": False, "error": "Please paste a link first."}), 400
    if not re.match(r"^https?://", link, re.I):
        return jsonify({"ok": False, "error": "That does not look like a link."}), 400
    try:
        playlist = _playlist_entries(link)
    except SocialError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        return jsonify({"ok": False,
                        "error": f"That playlist could not be read: {exc}"}), 500
    return jsonify({"ok": True, "playlist": playlist})


@app.route("/api/social/playlist/download")
def api_social_playlist_download():
    """Download selected playlist items and hand them over as one zip."""
    link = request.args.get("link", "").strip()
    wanted = request.args.get("items", "").strip()
    if not link:
        return jsonify({"ok": False, "error": "Missing playlist link."}), 400

    try:
        playlist = _playlist_entries(link)
    except SocialError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    selected = playlist["entries"]
    if wanted:
        try:
            # Accepts "1,3,5" or ranges like "1-5"; anything unparsable is
            # ignored rather than failing the whole request.
            keep = set()
            for part in wanted.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    lo, _, hi = part.partition("-")
                    if lo.isdigit() and hi.isdigit():
                        keep.update(range(int(lo), int(hi) + 1))
                elif part.isdigit():
                    keep.add(int(part))
            selected = [e for e in playlist["entries"] if e["index"] in keep]
        except (TypeError, ValueError):
            selected = playlist["entries"]
    if not selected:
        return jsonify({"ok": False, "error": "No items were selected."}), 400
    # A zip is only sent once every file inside it exists, so the request has to
    # stay open for the whole batch. Capping the batch keeps it inside the
    # server's 300s request timeout instead of dying at item 40 with nothing to
    # show; the caller is told to send another batch for the rest.
    if len(selected) > PLAYLIST_BATCH_LIMIT:
        return jsonify({
            "ok": False,
            "error": (f"That is {len(selected)} items, and one zip holds at most "
                      f"{PLAYLIST_BATCH_LIMIT}. Download the rest in another batch."),
            "limit": PLAYLIST_BATCH_LIMIT,
            "selected": len(selected),
        }), 400

    # A free host cannot hold a whole playlist in memory, so each file is written
    # to disk and zipped from there; the zip is streamed as it is produced.
    workdir = tempfile.mkdtemp(prefix="playlist-")
    zip_path = os.path.join(workdir, "playlist.zip")
    failures = []

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
            for entry in selected:
                item_dir = tempfile.mkdtemp(dir=workdir, prefix="item-")
                try:
                    path = _download_playlist_item(entry["url"], item_dir)
                    name = _unique_zip_name(archive, entry, path)
                    archive.write(path, arcname=name)
                except Exception as exc:  # noqa: BLE001 - one bad item is not fatal
                    # The raw yt-dlp text ("HTTP Error 403: Forbidden") tells a user
                    # nothing about whether to retry or give up, so each failure is
                    # recorded with the same advice the single-item path gives.
                    failures.append({"title": entry["title"],
                                     "reason": _friendly_download_error(exc)[:160]})
                finally:
                    shutil.rmtree(item_dir, ignore_errors=True)

            if failures:
                # The failures travel with the zip so the user is not left
                # wondering why a 20-item playlist produced 18 files.
                archive.writestr("_failed_items.txt", _failure_report(failures))

        if not archive_file_count(zip_path):
            # The per-item reasons are the only thing that tells a user whether to
            # retry, sign in, or pick different items, so they travel with the error.
            first = failures[0] if failures else {}
            detail = f" First failure: {first.get('title')}: {first.get('reason')}." if first else ""
            raise SocialError(
                f"None of the {len(selected)} selected items could be downloaded."
                f"{detail}")

        name = _branded(_safe_filename(playlist["title"], "playlist") + ".zip")
        size = os.path.getsize(zip_path)

        def stream_and_cleanup():
            try:
                with open(zip_path, "rb") as fh:
                    while True:
                        chunk = fh.read(256 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

        return Response(stream_and_cleanup(), headers={
            "Content-Type": "application/zip",
            "Content-Length": str(size),
            "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}",
            # A zip holding fewer files than the user picked looks like a
            # failure, so the counts are exposed for the page to report.
            "X-Playlist-Items": str(len(selected) - len(failures)),
            "X-Playlist-Failures": str(len(failures)),
            "Access-Control-Expose-Headers": "X-Playlist-Items, X-Playlist-Failures",
        })
    except SocialError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({"ok": False, "error": f"Unexpected error: {exc}"}), 500


def _download_playlist_item(page_url: str, workdir: str) -> str:
    """Download one playlist entry, trying selectors that fit what it offers.

    A single selector is not enough: `best` resolves to a legacy single-file
    choice that modern YouTube does not serve, and a merge selector fails on
    sites that only publish one combined file. Each attempt is a different shape
    of source, and the first one that produces a file wins.
    """
    last_exc = None
    for selector in ("bv*+ba/b", "b", None):
        try:
            return _download_to_file(page_url, selector, workdir, None)
        except Exception as exc:  # noqa: BLE001 - try the next shape
            last_exc = exc
            # Clear any partial file so the next attempt starts clean.
            for name in os.listdir(workdir):
                try:
                    os.remove(os.path.join(workdir, name))
                except OSError:
                    pass
    raise last_exc or SocialError("That item could not be downloaded.")


def archive_file_count(zip_path: str) -> int:
    """How many real media files a zip holds (ignoring the failure report)."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            return sum(1 for n in archive.namelist() if not n.startswith("_"))
    except zipfile.BadZipFile:
        return 0


def _failure_report(failures: list) -> str:
    lines = ["These items could not be downloaded:", ""]
    for f in failures:
        lines.append(f"- {f['title']}: {f['reason']}")
    return "\n".join(lines) + "\n"


def _unique_zip_name(archive, entry: dict, path: str) -> str:
    """A readable, collision-free name inside the zip."""
    ext = os.path.splitext(path)[1] or ".mp4"
    base = _safe_filename(f"{entry['index']:03d} - {entry['title']}", "item")
    name = base + ext
    seen = set(archive.namelist())
    counter = 2
    while name in seen:
        name = f"{base} ({counter}){ext}"
        counter += 1
    return name


def _safe_filename(text: str, fallback: str) -> str:
    clean = re.sub(r"[^\w\-. ]+", "_", text or "").strip(" ._")
    return (clean or fallback)[:120]


@app.route("/api/social/platforms")
def api_social_platforms():
    """List the supported platforms, for the home page grid."""
    return jsonify({
        "ok": True,
        "platforms": social_platform_list(),
        "engine": _ytdlp_available(),
        "ffmpeg": _ffmpeg_available(),
    })


@app.route("/api/social/resolve", methods=["POST"])
def api_social_resolve():
    """Resolve a social link into metadata plus the formats on offer."""
    link = _request_link()
    if not link:
        return jsonify({"ok": False, "error": "Please paste a link first."}), 400
    if not re.match(r"^https?://", link, re.I):
        return jsonify({"ok": False, "error": "That does not look like a link."}), 400
    try:
        media = social_extract(link)
    except SocialError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        return jsonify({"ok": False, "error": f"Unexpected error: {exc}"}), 500

    media["token"] = _social_store_put({"url": link, "media": media})
    return jsonify({"ok": True, "media": media})


@app.route("/api/social/stream")
def api_social_stream():
    """Stream one format for in-page playback (Range-capable)."""
    url = request.args.get("link", "").strip()
    format_id = request.args.get("format", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Missing link."}), 400
    try:
        spec = _format_for_url(url, format_id)
    except SocialError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if not spec.get("direct") and not spec.get("hls"):
        return jsonify({"ok": False, "error": "This quality must be downloaded, "
                        "not streamed. Use the Download button."}), 400

    if spec.get("hls"):
        try:
            return _social_hls_playlist(spec["url"], spec["headers"], spec["cookies"])
        except SocialError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    headers = dict(spec.get("headers") or {})
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    try:
        upstream = _request_session().get(spec["url"], headers=headers,
                                          cookies=spec.get("cookies") or None,
                                          stream=True, timeout=60)
    except _request_error() as exc:
        return jsonify({"ok": False, "error": f"Stream fetch failed: {exc}"}), 502
    if upstream.status_code not in (200, 206):
        upstream.close()
        return jsonify({"ok": False, "error": f"The source returned status "
                        f"{upstream.status_code}."}), 400

    ctype = "audio/mpeg" if spec.get("is_audio") else "video/mp4"
    return _proxy_stream(upstream, as_attachment=False,
                         filename=f"stream.{spec['ext']}", content_type=ctype)


@app.route("/api/social/download")
def api_social_download():
    """Hand back a file: a single progressive stream, or a freshly merged MP4."""
    url = request.args.get("link", "").strip()
    format_id = request.args.get("format", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Missing link."}), 400
    try:
        spec = _format_for_url(url, format_id)
    except SocialError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    safe = re.sub(r"[^\w\-. ]+", "_", spec.get("title") or "video").strip() or "video"
    safe = safe[:120]

    if spec.get("direct") and not spec.get("hls"):
        headers = dict(spec.get("headers") or {})
        if request.headers.get("Range"):
            headers["Range"] = request.headers["Range"]
        failure = None
        try:
            upstream = _request_session().get(spec["url"], headers=headers,
                                              cookies=spec.get("cookies") or None,
                                              stream=True, timeout=60)
            if upstream.status_code in (200, 206):
                filename = f"{safe}.{spec['ext']}"
                tagged = _tagged_download(
                    upstream, filename,
                    upstream.headers.get("Content-Type") or "video/mp4")
                if tagged is not None:
                    return tagged
                return _proxy_stream(upstream, as_attachment=True, filename=filename)
            upstream.close()
            failure = f"the source returned status {upstream.status_code}"
        except _request_error() as exc:
            failure = str(exc)
        # A signed CDN link goes stale between resolving and downloading, so a
        # 403 here is routine rather than a real failure. Re-pull the file with
        # yt-dlp, which negotiates its own fresh link.
        retried = _failover_download(url, f"{safe}.mp4")
        if retried is not None:
            return retried
        return jsonify({"ok": False,
                        "error": f"Download failed: {failure}. Every source "
                                 f"refused this file."}), 502

    # Anything that is not a single ready file - a video-only stream, an HLS
    # playlist, or a separate audio track - is downloaded and muxed to MP4.
    selector = _selector_for_spec(spec)
    workdir = tempfile.mkdtemp(prefix="social-")
    try:
        try:
            path = _download_to_file(url, selector, workdir, spec.get("cookies"))
        except SocialError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SocialError(_friendly_download_error(exc)) from exc

        filename = f"{safe}.mp4"
        branded = _branded(filename)
        path = _apply_sponsor_metadata(path)

        def stream_and_cleanup():
            try:
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(256 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

        size = os.path.getsize(path)
        return Response(stream_and_cleanup(), headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
            "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(branded)}",
        })
    except SocialError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({"ok": False, "error": f"Unexpected error: {exc}"}), 500


@app.route("/api/social/hls/<token>/<int:index>.ts")
def api_social_hls_segment(token: str, index: int):
    """Proxy one HLS segment (same-origin so the browser can play it)."""
    record = _SOCIAL_HLS.get(token)
    if not record or (time.time() - record[0]) > _SOCIAL_HLS_TTL:
        return jsonify({"ok": False, "error": "This stream has expired; reopen the video."}), 410
    _, segments, headers, cookies = record
    if index < 0 or index >= len(segments):
        return jsonify({"ok": False, "error": "No such segment."}), 404
    try:
        upstream = _request_session().get(segments[index], headers=headers,
                                          cookies=cookies or None,
                                          stream=True, timeout=60)
    except _request_error() as exc:
        return jsonify({"ok": False, "error": f"Segment fetch failed: {exc}"}), 502
    relay = _proxy_stream(upstream, as_attachment=False, filename=f"{index}.ts")
    relay.headers["Content-Type"] = "video/mp2t"  # CDNs often send octet-stream
    return relay


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>All-in-One Video Downloader - Tech Arslan Software Solutions</title>
<style>
  :root{
    /* Professional navy/slate palette with a single confident blue accent. */
    --bg:#0b1220; --bg-2:#0f172a; --surface:#141d31; --surface-2:#1b2740;
    --border:#22304b; --border-2:#33456a;
    --text:#e8eef8; --muted:#9aa9c2; --muted-2:#6e7f9c;
    --primary:#2f6bff; --primary-2:#4f8cff; --accent:#38bdf8;
    --success:#22c55e; --danger:#ef4444; --warn:#f59e0b;
    --radius:16px; --radius-sm:11px;
    --shadow:0 20px 44px -24px rgba(2,8,23,.85);
    --shadow-sm:0 10px 24px -18px rgba(2,8,23,.9);
    --focus:0 0 0 3px rgba(47,107,255,.35);
  }
  *{box-sizing:border-box;}
  html{scroll-behavior:smooth;}
  html,body{height:100%;}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Helvetica,Arial,sans-serif;
       color:var(--text);background:
         radial-gradient(1100px 520px at 12% -8%,rgba(47,107,255,.16),transparent 62%),
         radial-gradient(820px 460px at 100% 2%,rgba(56,189,248,.10),transparent 58%),
         var(--bg);
       display:flex;flex-direction:column;align-items:center;padding:0 0 56px;-webkit-font-smoothing:antialiased;
       text-rendering:optimizeLegibility;}
  .app{width:100%;max-width:820px;padding:24px 16px 0;}

  /* sponsor badge: the on-screen credit shown during playback */
  .sponsor-badge{position:absolute;top:12px;right:14px;z-index:4;
    display:flex;align-items:center;gap:7px;padding:6px 11px;border-radius:999px;
    text-decoration:none;font-size:.7rem;font-weight:700;
    color:rgba(232,238,248,.9);background:rgba(10,16,28,.62);
    border:1px solid rgba(255,255,255,.14);backdrop-filter:blur(8px);
    -webkit-backdrop-filter:blur(8px);opacity:.72;
    transition:opacity .25s ease,transform .2s ease,background .2s ease;}
  .sponsor-badge:hover{opacity:1;transform:translateY(-1px);
    background:rgba(10,16,28,.88);border-color:rgba(56,189,248,.5);}
  .sb-dot{width:6px;height:6px;border-radius:50%;background:var(--accent);flex-shrink:0;
    box-shadow:0 0 0 0 rgba(56,189,248,.7);animation:sbPulse 2.6s ease-out infinite;}
  @keyframes sbPulse{0%{box-shadow:0 0 0 0 rgba(56,189,248,.6);}
    70%{box-shadow:0 0 0 7px rgba(56,189,248,0);}100%{box-shadow:0 0 0 0 rgba(56,189,248,0);}}
  .sb-text{white-space:nowrap;max-width:230px;overflow:hidden;text-overflow:ellipsis;}
  @media (max-width:560px){.sponsor-badge{top:8px;right:8px;font-size:.64rem;padding:5px 9px;}
    .sb-text{max-width:150px;}}

  /* entrance: content settles in instead of snapping into place */
  @keyframes fadeUp{from{opacity:0;transform:translateY(10px);}to{opacity:1;transform:none;}}
  .app > *{animation:fadeUp .42s cubic-bezier(.22,1,.36,1) both;}
  .app > *:nth-child(2){animation-delay:.05s;}
  .app > *:nth-child(3){animation-delay:.09s;}
  .app > *:nth-child(4){animation-delay:.13s;}

  /* tactile press feedback on every actionable control */
  .btn:active,.icon-btn:active,.chip:active,.tab:active{transform:scale(.975);}
  .btn,.icon-btn,.chip,.tab{transition:transform .13s ease,background .16s ease,
    border-color .16s ease,color .16s ease,box-shadow .18s ease;}

  /* visible keyboard focus, which a dark theme otherwise swallows */
  :where(a,button,input,select,textarea,[tabindex]):focus-visible{
    outline:2px solid var(--accent);outline-offset:2px;border-radius:8px;}

  @media (prefers-reduced-motion:reduce){
    *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;
      transition-duration:.01ms!important;scroll-behavior:auto!important;}
  }

  /* topbar: full-width sticky bar so the brand stays visible while scrolling */
  .topbar{position:sticky;top:0;z-index:20;width:100%;padding:14px 16px;border-bottom:1px solid var(--border);
        background:linear-gradient(180deg,rgba(11,18,32,.97),rgba(11,18,32,.84));
        backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);}
  .topbar-inner{width:100%;max-width:820px;margin:0 auto;display:flex;align-items:center;
        justify-content:space-between;gap:16px;}
  .brand{display:flex;align-items:center;gap:13px;min-width:0;}
  .logo{width:46px;height:46px;flex-shrink:0;border-radius:13px;display:flex;align-items:center;justify-content:center;
        color:#fff;position:relative;overflow:hidden;
        background:linear-gradient(140deg,#2f6bff 0%,#4f8cff 58%,#38bdf8 132%);
        box-shadow:0 12px 26px -14px rgba(47,107,255,.95),inset 0 1px 0 rgba(255,255,255,.3);}
  .logo svg{width:26px;height:26px;display:block;position:relative;z-index:1;}
  .logo::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(255,255,255,.2),transparent 62%);}
  .brandname{font-size:1.14rem;font-weight:800;letter-spacing:-.3px;line-height:1.18;
        background:linear-gradient(92deg,#ffffff,#d7e3ff 62%,#bae6fd);
        -webkit-background-clip:text;background-clip:text;color:transparent;}
  .brandtag{display:flex;align-items:center;gap:6px;color:var(--muted-2);font-size:.7rem;font-weight:700;
        letter-spacing:1.1px;text-transform:uppercase;margin-top:4px;}
  .brandtag::before{content:"";width:14px;height:2px;border-radius:2px;background:linear-gradient(90deg,var(--primary),var(--accent));}
  .brandside{display:flex;align-items:center;gap:10px;}
  .prod-pill{display:inline-flex;align-items:center;gap:7px;font-size:.72rem;font-weight:700;color:#d7e3ff;
        border:1px solid rgba(47,107,255,.42);background:rgba(47,107,255,.13);border-radius:999px;padding:6px 13px;
        white-space:nowrap;}
  .prod-pill .dot{width:6px;height:6px;border-radius:50%;background:var(--success);box-shadow:0 0 8px var(--success);}
  .icon-btn{background:var(--surface);border:1px solid var(--border);color:var(--muted);width:38px;height:38px;
            border-radius:11px;cursor:pointer;font-size:1rem;font-weight:700;display:flex;align-items:center;
            justify-content:center;transition:.15s;}
  .icon-btn:hover{color:var(--text);border-color:var(--border-2);background:var(--surface-2);}
  .icon-btn:focus-visible,.btn:focus-visible,.tab:focus-visible,.chip:focus-visible,.act:focus-visible,
  .plat:focus-visible,.cbtn:focus-visible{outline:none;box-shadow:var(--focus);}

  /* footer */
  .sitefoot{margin-top:34px;width:100%;max-width:820px;border:1px solid var(--border);border-radius:var(--radius);
        background:linear-gradient(180deg,var(--surface),var(--bg-2));box-shadow:var(--shadow);overflow:hidden;}
  .foot-top{display:flex;flex-wrap:wrap;gap:24px;justify-content:space-between;padding:22px 22px 20px;}
  .foot-brand{display:flex;align-items:center;gap:12px;min-width:210px;}
  .foot-brand .logo{width:40px;height:40px;border-radius:12px;}
  .foot-brand .logo svg{width:22px;height:22px;}
  .foot-name{font-size:.96rem;font-weight:800;letter-spacing:-.2px;color:var(--text);}
  .foot-tag{color:var(--muted-2);font-size:.74rem;margin-top:2px;letter-spacing:.4px;}
  .foot-dev{display:flex;flex-direction:column;gap:2px;padding:11px 15px;border-radius:12px;
        border:1px solid rgba(47,107,255,.35);background:linear-gradient(180deg,rgba(47,107,255,.13),rgba(47,107,255,.05));
        min-width:210px;}
  .foot-dev .devlabel{font-size:.62rem;font-weight:800;text-transform:uppercase;letter-spacing:1.1px;color:var(--accent);}
  .foot-dev .devname{font-size:1.02rem;font-weight:800;letter-spacing:-.2px;color:var(--text);}
  .foot-dev .devorg{font-size:.74rem;color:var(--muted);}
  .foot-dev .devsub{font-size:.68rem;color:var(--muted-2);margin-top:2px;}
  .foot-badges{display:flex;flex-wrap:wrap;gap:7px;align-items:flex-start;max-width:260px;}
  .fchip{font-size:.68rem;font-weight:600;color:var(--muted);border:1px solid var(--border);background:var(--surface-2);
        border-radius:999px;padding:5px 11px;white-space:nowrap;}
  .foot-bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;
        padding:13px 22px;border-top:1px solid var(--border);background:rgba(8,13,24,.55);}
  .foot-bar .copy{color:var(--muted-2);font-size:.72rem;}
  .foot-bar .made{color:var(--muted-2);font-size:.72rem;display:flex;align-items:center;gap:6px;}
  .foot-bar .made strong{color:var(--muted);font-weight:700;}

  .card{background:linear-gradient(180deg,var(--surface),var(--bg-2));border:1px solid var(--border);
        border-radius:var(--radius);padding:18px;box-shadow:var(--shadow-sm);margin-bottom:16px;}
  label{display:block;font-size:.78rem;font-weight:600;color:var(--muted);margin-bottom:8px;letter-spacing:.2px;}
  .input-wrap{display:flex;gap:8px;}
  input[type=text]{flex:1;min-width:0;padding:12px 13px;border:1px solid var(--border);border-radius:var(--radius-sm);
       font-size:.92rem;background:var(--bg-2);color:var(--text);transition:.15s;}
  input[type=text]::placeholder{color:var(--muted-2);}
  input[type=text]:focus{outline:none;border-color:var(--primary);box-shadow:var(--focus);}
  .row{display:flex;gap:10px;margin-top:12px;}
  button{font-family:inherit;}
  .btn{border:1px solid transparent;border-radius:var(--radius-sm);padding:12px 18px;font-size:.88rem;font-weight:600;
       cursor:pointer;display:inline-flex;align-items:center;gap:8px;justify-content:center;transition:.15s;white-space:nowrap;}
  .btn svg{width:16px;height:16px;fill:currentColor;flex-shrink:0;}
  .primary{background:linear-gradient(135deg,var(--primary),var(--primary-2));color:#fff;flex:1;
           box-shadow:0 12px 26px -16px rgba(47,107,255,.95);}
  .primary:hover{filter:brightness(1.08);}
  .primary:disabled{opacity:.55;cursor:default;filter:none;box-shadow:none;}
  .ghost{background:var(--surface-2);color:var(--text);border-color:var(--border);}
  .ghost:hover{border-color:var(--border-2);background:#20304d;}
  .sm{padding:9px 13px;font-size:.8rem;border-radius:9px;}
  .status{font-size:.83rem;color:var(--muted);min-height:18px;margin-top:11px;}
  .status.error{color:var(--danger);}

  .note{background:linear-gradient(180deg,rgba(56,189,248,.08),rgba(56,189,248,.02));
        border:1px solid rgba(56,189,248,.26);color:#bfe6f6;border-radius:var(--radius);
        padding:14px 16px;font-size:.79rem;line-height:1.55;margin-bottom:16px;}
  .note code{background:rgba(56,189,248,.14);color:#d9f6ff;padding:1px 6px;border-radius:5px;font-size:.76rem;}

  /* tabs */
  .tabs{display:flex;gap:6px;background:var(--surface);border:1px solid var(--border);padding:6px;
        border-radius:var(--radius);margin-bottom:16px;box-shadow:var(--shadow-sm);}
  .tab{flex:1;border:0;background:transparent;color:var(--muted);padding:11px 12px;border-radius:10px;
       font-size:.86rem;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;transition:.15s;}
  .tab:hover{color:var(--text);background:rgba(255,255,255,.03);}
  .tab.active{background:linear-gradient(135deg,var(--primary),var(--primary-2));color:#fff;
              box-shadow:0 10px 22px -16px rgba(47,107,255,.95);}
  .count{background:rgba(255,255,255,.16);color:inherit;border-radius:999px;padding:1px 8px;font-size:.72rem;font-weight:700;}
  .tab:not(.active) .count{background:var(--surface-2);color:var(--muted);}

  .pane-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap;}
  .filters{display:flex;gap:6px;}
  .chip{background:var(--surface);border:1px solid var(--border);color:var(--muted);padding:7px 13px;
        border-radius:999px;font-size:.78rem;font-weight:600;cursor:pointer;transition:.15s;}
  .chip:hover{color:var(--text);}
  .chip.active{background:rgba(47,107,255,.18);border-color:var(--primary);color:#c3d5ff;}

  /* lists */
  .list{display:flex;flex-direction:column;gap:9px;}
  .item{display:flex;align-items:center;gap:13px;padding:12px 13px;background:linear-gradient(180deg,var(--surface),var(--bg-2));
        border:1px solid var(--border);border-radius:var(--radius-sm);transition:.15s;}
  .item:hover{border-color:var(--border-2);}
  .thumb{width:44px;height:44px;flex-shrink:0;border-radius:11px;display:flex;align-items:center;justify-content:center;
         background:linear-gradient(135deg,rgba(47,107,255,.25),rgba(56,189,248,.18));color:#bcd2ff;}
  .thumb svg{width:20px;height:20px;fill:currentColor;}
  .thumb.played{background:linear-gradient(135deg,rgba(34,197,94,.28),rgba(34,197,94,.12));color:#86efac;}
  .thumb.downloaded{background:linear-gradient(135deg,rgba(47,107,255,.28),rgba(56,189,248,.16));color:#93c5fd;}
  .meta{min-width:0;flex:1;}
  .name{font-size:.88rem;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .sub2{font-size:.74rem;color:var(--muted);margin-top:3px;display:flex;align-items:center;gap:7px;flex-wrap:wrap;}
  .dot{width:3px;height:3px;border-radius:50%;background:var(--muted-2);}
  .actions{display:flex;gap:7px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end;}
  .act{border:1px solid var(--border);background:var(--surface-2);color:var(--text);padding:8px 13px;border-radius:9px;
       font-size:.78rem;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:.15s;}
  .act svg{width:14px;height:14px;fill:currentColor;}
  .act:hover{border-color:var(--border-2);background:#233450;}
  .act.play{background:rgba(34,197,94,.16);border-color:rgba(34,197,94,.45);color:#86efac;}
  .act.play:hover{background:rgba(34,197,94,.26);}
  .act.dl{background:linear-gradient(135deg,var(--primary),var(--primary-2));border-color:transparent;color:#fff;}
  .act.dl:hover{filter:brightness(1.08);}
  .act.icon{padding:8px 10px;}
  .empty{color:var(--muted-2);font-size:.83rem;padding:28px 12px;text-align:center;border:1px dashed var(--border);
         border-radius:var(--radius-sm);background:rgba(255,255,255,.012);}

  /* social downloader */
  .maintabs{margin-bottom:16px;}
  .hero{margin:2px 0 18px;max-width:640px;}
  .hero h2{margin:0 0 7px;font-size:1.32rem;letter-spacing:-.4px;line-height:1.25;}
  .hero p{margin:0;color:var(--muted);font-size:.85rem;line-height:1.62;}
  .platwrap{margin:16px 0;}
  .plathead{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;
        color:var(--muted-2);font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:1.1px;}
  .platcount{background:var(--surface-2);border:1px solid var(--border);border-radius:999px;padding:2px 10px;
        color:var(--muted);font-size:.68rem;letter-spacing:.4px;}
  .platforms{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:9px;}
  /* Group labels sit inside the grid, so they must span every column. */
  .platgroup{grid-column:1 / -1;display:flex;align-items:center;gap:8px;margin:8px 0 0;
        color:var(--muted-2);font-size:.67rem;font-weight:800;text-transform:uppercase;letter-spacing:1.1px;}
  .platgroup:first-child{margin-top:0;}
  .platgroup::after{content:"";flex:1;height:1px;background:var(--border);}
  .plat.feat{border-color:rgba(47,107,255,.34);}
  .plat{display:flex;align-items:center;gap:9px;padding:10px 11px;border-radius:12px;cursor:pointer;
        background:linear-gradient(180deg,var(--surface),var(--bg-2));border:1px solid var(--border);
        color:var(--text);font-size:.78rem;font-weight:600;text-align:left;transition:.15s;min-width:0;}
  .plat:hover{border-color:var(--border-2);transform:translateY(-1px);}
  .plat.active{border-color:var(--primary);background:rgba(47,107,255,.16);}
  .plat .pd{width:22px;height:22px;flex-shrink:0;border-radius:7px;display:flex;align-items:center;justify-content:center;
        font-size:.62rem;font-weight:800;color:#fff;}
  .plat .pn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .platnote{color:var(--muted-2);font-size:.72rem;margin-top:9px;}
  /* The paste box leads the page: it is what every visitor came to use. */
  .pastecard{margin-bottom:16px;}
  /* Popular row: the mainstream sites, one tap away above the input. */
  .quickcard{margin-bottom:16px;}
  .quickrow{display:flex;flex-wrap:wrap;gap:8px;}
  .qplat{display:flex;align-items:center;gap:7px;padding:8px 11px;border-radius:999px;cursor:pointer;
        background:linear-gradient(180deg,var(--surface),var(--bg-2));border:1px solid rgba(47,107,255,.3);
        color:var(--text);font-size:.78rem;font-weight:600;transition:.15s;}
  .qplat:hover{border-color:var(--primary);transform:translateY(-1px);}
  .qplat.active{border-color:var(--primary);background:rgba(47,107,255,.16);}
  .qplat .pd{width:20px;height:20px;flex-shrink:0;border-radius:6px;display:flex;align-items:center;justify-content:center;
        font-size:.58rem;font-weight:800;color:#fff;}
  .platsearch{width:100%;padding:9px 12px;margin-bottom:10px;border:1px solid var(--border);
        border-radius:9px;background:var(--bg-2);color:var(--text);font-size:.82rem;outline:none;transition:.15s;}
  .platsearch::placeholder{color:var(--muted-2);}
  .platsearch:focus{border-color:var(--primary);box-shadow:var(--focus);}
  /* A long list stays usable: cap its height and let it scroll. */
  #platforms{max-height:420px;overflow-y:auto;padding-right:4px;scrollbar-width:thin;
        scrollbar-color:var(--border-2) transparent;}
  #platforms::-webkit-scrollbar{width:8px;}
  #platforms::-webkit-scrollbar-thumb{background:var(--border-2);border-radius:8px;}
  #platforms::-webkit-scrollbar-track{background:transparent;}

  /* per-platform help card */
  .helpcard{margin-top:16px;}
  .helphead{display:flex;align-items:center;gap:10px;margin-bottom:14px;}
  .helpbadge{width:30px;height:30px;flex-shrink:0;border-radius:9px;display:flex;align-items:center;justify-content:center;
        font-size:.68rem;font-weight:800;color:#fff;background:#8b93a7;}
  .helptitle{font-size:.9rem;font-weight:700;letter-spacing:-.2px;}
  .helpblock{margin-bottom:12px;}
  .helplabel{display:block;font-size:.64rem;font-weight:800;text-transform:uppercase;letter-spacing:1.1px;
        color:var(--muted-2);margin-bottom:4px;}
  .helptext{margin:0;font-size:.79rem;line-height:1.55;color:var(--muted);}
  .helpsteps{margin:4px 0 0;padding-left:18px;font-size:.78rem;line-height:1.75;color:var(--muted);}
  .helpsteps strong{color:var(--text);font-weight:700;}

  /* the source link, shown under the player title */
  .plink{display:flex;align-items:center;gap:6px;margin-top:3px;min-width:0;}
  .plink a{color:var(--accent);font-size:.72rem;text-decoration:none;overflow:hidden;
        text-overflow:ellipsis;white-space:nowrap;}
  .plink a:hover{text-decoration:underline;}
  .plink .cp{background:transparent;border:0;color:var(--muted-2);cursor:pointer;font-size:.7rem;
        padding:2px 5px;border-radius:5px;flex-shrink:0;}
  .plink .cp:hover{color:var(--text);background:rgba(255,255,255,.08);}
  .result{display:flex;gap:16px;align-items:center;}
  .rthumb{width:150px;height:86px;flex-shrink:0;border-radius:12px;overflow:hidden;background:var(--surface-2);
        border:1px solid var(--border);display:flex;align-items:center;justify-content:center;}
  .rthumb img{width:100%;height:100%;object-fit:cover;display:block;}
  .rthumb svg{width:26px;height:26px;fill:var(--muted-2);}
  .rmeta{min-width:0;flex:1;}
  .rtitle{font-size:.98rem;font-weight:700;line-height:1.35;margin-bottom:5px;
        display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
  .rsub{color:var(--muted);font-size:.76rem;}
  .rtags{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px;}
  .rtag{font-size:.68rem;font-weight:600;color:var(--muted);border:1px solid var(--border);
        background:var(--surface-2);border-radius:999px;padding:3px 9px;}
  .fhead{display:flex;flex-wrap:wrap;gap:6px;align-items:baseline;justify-content:space-between;margin-bottom:11px;}
  .fhead span:first-child{font-size:.8rem;font-weight:700;color:var(--muted);}
  .fhint{color:var(--muted-2);font-size:.7rem;}
  .flist{display:flex;flex-direction:column;gap:8px;}
  .frow{display:flex;align-items:center;gap:11px;padding:11px 12px;border-radius:11px;
        background:linear-gradient(180deg,var(--surface),var(--bg-2));border:1px solid var(--border);transition:.15s;}
  .frow:hover{border-color:var(--border-2);}
  .frow .fq{font-size:.83rem;font-weight:700;min-width:96px;}
  .frow .fm{color:var(--muted-2);font-size:.72rem;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .frow .fa{display:flex;gap:7px;flex-shrink:0;}

  /* playlist */
  .plbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px;}
  .plcount{color:var(--muted-2);font-size:.76rem;margin-left:auto;}
  .plitems{max-height:420px;overflow-y:auto;}
  .plrow{display:flex;align-items:center;gap:10px;padding:9px 11px;border-radius:10px;
        background:linear-gradient(180deg,var(--surface),var(--bg-2));border:1px solid var(--border);}
  .plrow input{width:16px;height:16px;flex-shrink:0;accent-color:var(--primary);cursor:pointer;}
  .plidx{color:var(--muted-2);font-size:.72rem;font-weight:700;min-width:26px;flex-shrink:0;}
  .plname{font-size:.8rem;font-weight:600;flex:1;min-width:0;overflow:hidden;
          text-overflow:ellipsis;white-space:nowrap;}
  .pldur{color:var(--muted-2);font-size:.72rem;flex-shrink:0;}

  /* Two columns on wide screens: the platform picker sits beside the input. */
  .social-grid{display:block;}
  @media (min-width:1000px){
    .app,.sitefoot,.topbar-inner{max-width:1080px;}
    .social-grid{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(0,1fr);gap:18px;align-items:start;}
    .social-grid .col-main{min-width:0;}
    .social-grid .col-side{min-width:0;position:sticky;top:96px;}
    .social-grid .platwrap{margin-top:0;}
    .platforms{grid-template-columns:repeat(auto-fill,minmax(104px,1fr));}
  }

  /* player: a full page, so the video gets the whole screen */
  #player{position:fixed;inset:0;background:#04070d;display:flex;align-items:stretch;justify-content:center;
          padding:0;z-index:60;}
  #player.full{background:#000;}
  .pbox{background:#080d18;width:100%;height:100%;overflow-x:hidden;overflow-y:auto;display:flex;flex-direction:column;}
  .pbox.wide .phead,.pbox.wide .nextbar{display:none;}
  .phead{display:flex;align-items:center;gap:12px;padding:12px 15px;border-bottom:1px solid var(--border);
         background:linear-gradient(180deg,#0d1526,#0a1120);flex-shrink:0;}
  .ptitle{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px;}
  .ptitletop{display:flex;align-items:center;gap:10px;min-width:0;}
  .ptitle .t{font-size:.9rem;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .tag{font-size:.68rem;font-weight:700;letter-spacing:.3px;text-transform:uppercase;color:#c9cbff;
       border:1px solid rgba(99,102,241,.5);background:rgba(99,102,241,.15);border-radius:999px;padding:3px 10px;flex-shrink:0;}
  .tag.preview{color:#fde68a;border-color:rgba(245,158,11,.5);background:rgba(245,158,11,.14);}
  .phead-actions{display:flex;gap:8px;flex-shrink:0;}
  .stage{position:relative;background:#000;display:flex;align-items:center;justify-content:center;
         overflow:hidden;flex:1;min-height:0;}
  .stage video{width:100%;height:100%;display:block;background:#000;object-fit:contain;
         transition:transform .2s ease;}
  /* The idle video stays in the layout but invisible, so a prefetched stream
     buffers at the real display size without covering the one being watched. */
  .stage video.ghost{position:absolute;inset:0;width:100%;height:100%;opacity:0;pointer-events:none;}
  .spinner{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;z-index:3;}
  .spinner::after{content:"";width:46px;height:46px;border-radius:50%;border:3px solid rgba(255,255,255,.22);
        border-top-color:var(--accent);animation:spin .8s linear infinite;}
  @keyframes spin{to{transform:rotate(360deg);}}
  .centerplay{position:absolute;width:74px;height:74px;border-radius:50%;border:0;cursor:pointer;z-index:4;
        background:rgba(12,18,32,.72);color:#fff;display:flex;align-items:center;justify-content:center;
        backdrop-filter:blur(4px);box-shadow:0 12px 30px -10px rgba(0,0,0,.9);transition:.18s;}
  .centerplay svg{width:30px;height:30px;fill:currentColor;margin-left:3px;}
  .centerplay:hover{background:rgba(99,102,241,.9);transform:scale(1.06);}
  .centerplay.hidden{display:none;}
  .toast{position:absolute;top:16px;left:50%;transform:translate(-50%,-14px);background:rgba(10,16,28,.92);
         border:1px solid var(--border-2);color:#e9eefb;padding:9px 16px;border-radius:999px;font-size:.8rem;
         font-weight:600;opacity:0;pointer-events:none;transition:.2s;z-index:5;}
  .toast.show{opacity:1;transform:translate(-50%,0);}
  .controls{position:absolute;left:0;right:0;bottom:0;padding:10px 14px 13px;display:flex;flex-direction:column;gap:8px;
        background:linear-gradient(180deg,rgba(6,10,20,0),rgba(6,10,20,.94) 42%);transition:opacity .2s,transform .2s;z-index:2;}
  .controls.hide{opacity:0;transform:translateY(8px);pointer-events:none;}
  .progress{position:relative;height:16px;display:flex;align-items:center;cursor:pointer;touch-action:none;}
  .progress .track{position:absolute;left:0;right:0;height:5px;border-radius:3px;background:rgba(255,255,255,.18);overflow:hidden;}
  .progress .buffered{position:absolute;left:0;top:0;bottom:0;background:rgba(255,255,255,.3);width:0;}
  .progress .played{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,var(--primary),var(--accent));width:0;}
  .progress .knob{position:absolute;width:13px;height:13px;border-radius:50%;background:#fff;left:0;transform:translateX(-50%);
        box-shadow:0 0 0 4px rgba(99,102,241,.35);pointer-events:none;}
  .progress .tip{position:absolute;bottom:20px;transform:translateX(-50%);background:rgba(10,16,28,.95);
        border:1px solid var(--border-2);color:#fff;font-size:.72rem;padding:3px 8px;border-radius:6px;
        opacity:0;transition:.12s;pointer-events:none;white-space:nowrap;}
  .progress:hover .tip{opacity:1;}
  .ctrl-row{display:flex;align-items:center;gap:6px;color:#dbe7f2;flex-wrap:wrap;}
  .cbtn{background:transparent;border:0;color:#dbe7f2;width:38px;height:38px;border-radius:9px;cursor:pointer;
        display:flex;align-items:center;justify-content:center;transition:.14s;position:relative;}
  .cbtn svg{width:19px;height:19px;fill:currentColor;}
  .cbtn:hover{background:rgba(255,255,255,.1);color:#fff;}
  .cbtn.on{color:var(--accent);}
  .cbtn.on::after{content:"";position:absolute;bottom:4px;width:4px;height:4px;border-radius:50%;background:var(--accent);}
  .cbtn.big{width:44px;height:44px;background:rgba(255,255,255,.08);}
  .cbtn.big svg{width:22px;height:22px;}
  .time{font-size:.78rem;font-variant-numeric:tabular-nums;color:#b9cbe2;min-width:98px;margin-left:4px;}
  .spacer{flex:1;}
  .vol-wrap{display:flex;align-items:center;gap:4px;}
  input[type=range].vol{width:78px;-webkit-appearance:none;appearance:none;height:4px;border-radius:3px;
        background:rgba(255,255,255,.25);cursor:pointer;outline:none;}
  input[type=range].vol::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;border-radius:50%;background:#fff;cursor:pointer;}
  input[type=range].vol::-moz-range-thumb{width:12px;height:12px;border:0;border-radius:50%;background:#fff;cursor:pointer;}
  .select{background:#16233a;color:#dbe7f2;border:1px solid var(--border-2);border-radius:9px;padding:8px 9px;
          font-size:.76rem;cursor:pointer;outline:none;}
  .pmsg{color:var(--muted);font-size:.78rem;padding:10px 15px;min-height:1.2em;border-top:1px solid var(--border);
        background:#0a1120;}
  .pmsg.error{color:#fca5a5;}

  /* play next bar */
  .nextbar{padding:11px 15px 13px;border-top:1px solid var(--border);background:#0a1120;
           display:flex;flex-direction:column;gap:9px;flex-shrink:0;}
  .nextrow{display:flex;gap:8px;}
  .nextrow input{flex:1;min-width:0;padding:10px 12px;border:1px solid var(--border);border-radius:9px;
        background:var(--bg-2);color:var(--text);font-size:.82rem;outline:none;}
  .nextrow input:focus{border-color:var(--primary);box-shadow:0 0 0 3px rgba(99,102,241,.2);}
  .nextrow input::placeholder{color:var(--muted-2);}
  .nextstatus{font-size:.76rem;color:var(--muted);min-height:1em;}
  .nextstatus.err{color:#fca5a5;}
  .nextstatus.ok{color:#86efac;}
  .nextlist{display:flex;flex-wrap:wrap;gap:6px;max-height:120px;overflow:auto;}
  .nextlist .act{font-size:.74rem;padding:6px 11px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}

  /* fullscreen / theater: hide the chrome so the video fills the screen */
  #player:fullscreen,#player:-webkit-full-screen{background:#000;}
  #player.full .phead,#player:fullscreen .phead,#player:-webkit-full-screen .phead{display:none;}
  #player.full .pmsg,#player:fullscreen .pmsg,#player:-webkit-full-screen .pmsg{display:none;}
  #player.full .nextbar,#player:fullscreen .nextbar,#player:-webkit-full-screen .nextbar{display:none;}

  /* shortcuts modal */
  .modal{position:fixed;inset:0;background:rgba(4,8,16,.82);backdrop-filter:blur(5px);display:flex;
         align-items:center;justify-content:center;padding:20px;z-index:70;}
  .modal .panel{background:linear-gradient(180deg,var(--surface),var(--bg-2));border:1px solid var(--border);
        border-radius:16px;width:min(560px,100%);padding:22px;box-shadow:0 30px 80px -20px rgba(0,0,0,.85);}
  .modal h3{margin:0 0 16px;font-size:1.05rem;}
  .keys{display:grid;grid-template-columns:1fr 1fr;gap:9px 20px;}
  .krow{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:.82rem;color:var(--muted);}
  .kbd{background:var(--surface-2);border:1px solid var(--border-2);border-bottom-width:2px;border-radius:6px;
       padding:2px 8px;font-size:.72rem;font-weight:700;color:var(--text);font-family:ui-monospace,Menlo,monospace;}
  .hidden{display:none !important;}
  body.playing{overflow:hidden;}

  @media (max-width:620px){
    .keys{grid-template-columns:1fr;}
    .item{flex-wrap:wrap;}
    .actions{width:100%;justify-content:flex-start;}
    .time{min-width:auto;}
    .brandname{font-size:1.02rem;}
    .brandtag{font-size:.64rem;letter-spacing:.9px;}
    .prod-pill{display:none;}
    .foot-top{gap:16px;padding:16px 16px 14px;}
    .foot-badges{max-width:100%;}
    .foot-bar{flex-direction:column;align-items:flex-start;gap:6px;padding:11px 16px;}
    .platforms{grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:8px;}
    .plat{padding:9px;font-size:.74rem;gap:7px;}
    .result{flex-direction:column;align-items:stretch;gap:11px;}
    .rthumb{width:100%;height:150px;}
    .frow{flex-wrap:wrap;}
    .frow .fq{min-width:auto;flex:1;}
    .frow .fm{flex-basis:100%;order:3;}
    .frow .fa{width:100%;justify-content:flex-start;}
    .frow .fa .btn{flex:1;}
    .phead{padding:10px 12px;}
    .ptitle .t{font-size:.82rem;}
    .cbtn{width:34px;height:34px;}
    .cbtn.big{width:40px;height:40px;}
    .controls{padding:8px 10px 10px;gap:5px;}
    .nextbar{padding:9px 11px 11px;}
  }
</style>
</head>
<body>
<div class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <div class="logo">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9A1.5 1.5 0 0 1 18.5 16H13l-3.4 3.1A.7.7 0 0 1 8.4 18.6V16H5.5A1.5 1.5 0 0 1 4 14.5v-9Zm6 2.6v4.8l4.2-2.4L10 8.1Z"/></svg>
      </div>
      <div>
        <div class="brandname">All-in-One Video Downloader</div>
        <div class="brandtag">Tech Arslan Software Solutions</div>
      </div>
    </div>
    <div class="brandside">
      <span class="prod-pill"><span class="dot"></span>Fast &amp; Free</span>
      <button class="icon-btn" id="helpBtn" title="Keyboard shortcuts">?</button>
    </div>
  </div>
</div>

<div class="app">
  <nav class="tabs maintabs">
    <button class="tab active" data-page="social">Social Downloader</button>
    <button class="tab" data-page="terabox">TeraBox</button>
  </nav>

  <section id="page-social" class="page">
    <div class="hero">
      <h2>Download videos from any social platform</h2>
      <p>Paste a link from YouTube, Instagram, Facebook, TikTok, X and more. Play it right here when a
        single-file stream is available, or download it as an MP4.</p>
    </div>

    <div class="card pastecard">
      <label for="sLink">Paste a video link</label>
      <div class="input-wrap">
        <input id="sLink" type="text" placeholder="https://www.youtube.com/watch?v=...  or any supported link" autocomplete="off">
        <button id="sPaste" class="btn ghost sm">Paste</button>
      </div>
      <div class="row">
        <button id="sFetch" class="btn primary">Get video</button>
        <button id="sClear" class="btn ghost">Clear</button>
      </div>
      <div id="sStatus" class="status"></div>
    </div>

    <div class="card quickcard">
      <div class="plathead">
        <span>Popular social media</span>
        <span class="platcount" id="quickCount">—</span>
      </div>
      <div id="quickPlatforms" class="quickrow"></div>
    </div>

    <div class="social-grid">
      <div class="col-main">
        <div id="sResult" class="hidden">
          <div class="card result">
            <div class="rthumb" id="sThumb"></div>
            <div class="rmeta">
              <div class="rtitle" id="sTitle">Video</div>
              <div class="rsub" id="sSub"></div>
              <div class="rtags" id="sTags"></div>
            </div>
          </div>
          <div class="card">
            <div class="fhead">
              <span>Choose quality</span>
              <span class="fhint">Play works on single-file streams; other qualities download as MP4.</span>
            </div>
            <div id="sFormats" class="flist"></div>
          </div>
        </div>
      </div>

          <div id="sPlaylist" class="hidden">
            <div class="card">
              <div class="fhead">
                <span id="plTitle">Playlist</span>
                <span class="fhint" id="plHint"></span>
              </div>
              <div class="plbar">
                <button class="btn ghost sm" id="plAll">Select all</button>
                <button class="btn ghost sm" id="plNone">Clear</button>
                <span class="plcount" id="plCount"></span>
                <button class="btn primary sm" id="plDownload">Download selected as ZIP</button>
              </div>
              <div id="plItems" class="flist plitems"></div>
            </div>
          </div>

      <aside class="col-side">
        <div class="card platwrap">
          <div class="plathead">
            <span>Supported platforms</span>
            <span class="platcount" id="platCount">160+</span>
          </div>
          <input id="platSearch" class="platsearch" type="text" placeholder="Search platforms..." autocomplete="off">
          <div id="platforms" class="platforms"></div>
          <div class="platnote">Tap a platform to see where to copy its link from.</div>
        </div>

        <div class="card helpcard" id="helpCard">
          <div class="helphead">
            <span class="helpbadge" id="helpBadge">?</span>
            <span class="helptitle" id="helpTitle">How to use this tool</span>
          </div>
          <div class="helpblock">
            <span class="helplabel">Where to get the link</span>
            <p class="helptext" id="helpWhere">Pick a platform above to see exactly where its
              Share / Copy link option is.</p>
          </div>
          <div class="helpblock">
            <span class="helplabel">How to download</span>
            <p class="helptext" id="helpHow">Paste the link in the box, press Get video, then play
              it here or pick a quality to save it as an MP4.</p>
          </div>
          <ol class="helpsteps">
            <li>Copy the video link from the app or site.</li>
            <li>Paste it into the box and press <strong>Get video</strong>.</li>
            <li>Press <strong>Play</strong> to watch, or <strong>Download</strong> to save it.</li>
          </ol>
        </div>
      </aside>
    </div>
  </section>

  <section id="page-terabox" class="page hidden">
    <div class="note">
      No account needed. TeraBox refuses anonymous direct downloads, so the app streams the full
      video through a helper service. That service sees the links you paste, and its copy is a
      transcoded stream (downloaded as <code>.ts</code>), not TeraBox's original file. Set
      <code>TERABOX_COOKIE</code> (see the README) to download the original MP4 instead, or
      <code>TERABOX_FALLBACK=off</code> to disable the helper.
    </div>

    <div class="card">
      <label for="link">TeraBox share link</label>
      <div class="input-wrap">
        <input id="link" type="text" placeholder="https://www.terabox.com/s/xxxxxxxxxxxxxxxxxxxxxx" autocomplete="off">
        <button id="paste" class="btn ghost sm">Paste</button>
      </div>
      <div class="row">
        <button id="fetch" class="btn primary">Fetch files</button>
        <button id="clear" class="btn ghost">Clear</button>
      </div>
      <div id="status" class="status"></div>
    </div>

    <nav class="tabs">
      <button class="tab active" data-tab="files">Files <span class="count" id="filesCount">0</span></button>
      <button class="tab" data-tab="history">History <span class="count" id="histCount">0</span></button>
    </nav>

    <section id="tab-files">
      <div id="files" class="list"></div>
      <div id="filesEmpty" class="empty">No files yet. Paste a share link above and press Fetch files.</div>
    </section>

    <section id="tab-history" class="hidden">
      <div class="pane-head">
        <div class="filters">
          <button class="chip active" data-filter="all">All</button>
          <button class="chip" data-filter="played">Played</button>
          <button class="chip" data-filter="downloaded">Downloaded</button>
        </div>
        <button id="histClear" class="btn ghost sm hidden">Clear history</button>
      </div>
      <div id="history" class="list"></div>
      <div id="histEmpty" class="empty">Videos you play or download will show up here.</div>
    </section>
  </section>
</div>

<div id="player" class="hidden">
  <div class="pbox" id="pbox">
    <div class="phead">
      <div class="ptitle">
        <div class="ptitletop">
          <span class="t" id="playerTitle">Video</span>
          <span class="tag hidden" id="playerTag"></span>
        </div>
        <div class="plink hidden" id="playerLink">
          <a id="playerLinkA" href="#" target="_blank" rel="noopener noreferrer"></a>
          <button class="cp" id="playerLinkCopy" title="Copy the source link">Copy</button>
        </div>
      </div>
      <div class="phead-actions">
        <button class="btn ghost sm" id="btnDownloadTop">
          <svg viewBox="0 0 24 24"><path d="M12 3v10.6l3.3-3.3 1.4 1.4L12 17.4l-4.7-4.7 1.4-1.4L12 13.6V3zM5 19h14v2H5z"/></svg>
          Download
        </button>
        <button class="icon-btn" id="playerClose" title="Close (Esc)">&#10005;</button>
      </div>
    </div>
    <div class="stage" id="stage">
      <video id="playerVideo" playsinline webkit-playsinline preload="auto"></video>
      <video id="playerVideoIdle" class="ghost" playsinline webkit-playsinline preload="auto" muted></video>
      <div class="spinner" id="playerSpinner"></div>
      <button class="centerplay hidden" id="centerPlay" aria-label="Play">
        <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
      </button>
      <div class="toast" id="toast"></div>
      <a class="sponsor-badge" id="sponsorBadge" href="__SPONSOR_URL__"
         target="_blank" rel="noopener" title="__SPONSOR_NAME__">
        <span class="sb-dot"></span>
        <span class="sb-text">__SPONSOR_TAGLINE__</span>
      </a>
      <div class="controls" id="controls">
        <div class="progress" id="progress">
          <div class="track">
            <div class="buffered" id="buffered"></div>
            <div class="played" id="played"></div>
          </div>
          <div class="knob" id="knob"></div>
          <div class="tip" id="tip">0:00</div>
        </div>
        <div class="ctrl-row">
          <button class="cbtn big" id="btnPlay" title="Play / Pause (Space)">
            <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
          </button>
          <button class="cbtn" id="btnBack" title="Back 10s (&#8592;)">
            <svg viewBox="0 0 24 24"><path d="M12 5V1L7 6l5 5V7a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8z"/></svg>
          </button>
          <button class="cbtn" id="btnFwd" title="Forward 10s (&#8594;)">
            <svg viewBox="0 0 24 24"><path d="M12 5V1l5 5-5 5V7a6 6 0 1 0 6 6h2a8 8 0 1 1-8-8z"/></svg>
          </button>
          <span class="time" id="time">0:00 / 0:00</span>
          <span class="spacer"></span>
          <div class="vol-wrap">
            <button class="cbtn" id="btnMute" title="Mute (M)">
              <svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4zm12.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4z"/></svg>
            </button>
            <input class="vol" id="vol" type="range" min="0" max="1" step="0.05" value="1" aria-label="Volume">
          </div>
          <select class="select" id="rate" title="Playback speed">
            <option value="0.5">0.5&times;</option>
            <option value="0.75">0.75&times;</option>
            <option value="1" selected>1&times;</option>
            <option value="1.25">1.25&times;</option>
            <option value="1.5">1.5&times;</option>
            <option value="2">2&times;</option>
          </select>
          <button class="cbtn" id="btnLoop" title="Loop (L)">
            <svg viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg>
          </button>
          <button class="cbtn" id="btnPip" title="Picture in picture (P)">
            <svg viewBox="0 0 24 24"><path d="M3 5h18v14H3V5zm2 2v10h14V7H5zm7 4h5v4h-5v-4z"/></svg>
          </button>
          <button class="cbtn" id="btnRotate" title="Rotate 90&deg; (R)">
            <svg viewBox="0 0 24 24"><path d="M12 5V2L8 6l4 4V7a5 5 0 1 1-5 5H5a7 7 0 1 0 7-7z"/></svg>
          </button>
          <button class="cbtn" id="btnShot" title="Snapshot (S)">
            <svg viewBox="0 0 24 24"><path d="M9 3h6l1.5 2H21v14H3V5h4.5L9 3zm3 5a5 5 0 1 0 0 10 5 5 0 0 0 0-10zm0 2a3 3 0 1 1 0 6 3 3 0 0 1 0-6z"/></svg>
          </button>
          <button class="cbtn" id="btnWide" title="Theater mode (W)">
            <svg viewBox="0 0 24 24"><path d="M3 5h18v14H3V5zm2 2v10h14V7H5z"/></svg>
          </button>
          <button class="cbtn" id="btnFs" title="Fullscreen (F)">
            <svg viewBox="0 0 24 24"><path d="M4 4h6v2H6v4H4V4zm10 0h6v6h-2V6h-4V4zM4 14h2v4h4v2H4v-6zm14 0h2v6h-6v-2h4v-4z"/></svg>
          </button>
        </div>
      </div>
    </div>
    <div class="pmsg" id="playerMsg"></div>
    <div class="nextbar">
      <div class="nextrow">
        <input id="nextLink" type="text" placeholder="Paste the next TeraBox link here - it loads in the background while this one keeps playing" autocomplete="off">
        <button class="btn primary sm" id="nextLoad">Load</button>
      </div>
      <div class="nextstatus" id="nextStatus">Play a video, then paste the next link. Playback continues until the new one is ready.</div>
      <div class="nextrow">
        <button class="btn primary sm hidden" id="nextPlay" style="flex:1;">Play now</button>
      </div>
      <div class="nextlist" id="nextList"></div>
    </div>
  </div>
</div>

<div id="shortcuts" class="modal hidden">
  <div class="panel">
    <h3>Keyboard shortcuts</h3>
    <div class="keys">
      <div class="krow"><span>Play / Pause</span><span class="kbd">Space</span></div>
      <div class="krow"><span>Seek back 10s</span><span class="kbd">&#8592;</span></div>
      <div class="krow"><span>Seek forward 10s</span><span class="kbd">&#8594;</span></div>
      <div class="krow"><span>Volume up / down</span><span class="kbd">&#8593; &#8595;</span></div>
      <div class="krow"><span>Mute</span><span class="kbd">M</span></div>
      <div class="krow"><span>Fullscreen</span><span class="kbd">F</span></div>
      <div class="krow"><span>Picture in picture</span><span class="kbd">P</span></div>
      <div class="krow"><span>Rotate video</span><span class="kbd">R</span></div>
      <div class="krow"><span>Snapshot</span><span class="kbd">S</span></div>
      <div class="krow"><span>Loop</span><span class="kbd">L</span></div>
      <div class="krow"><span>Theater mode</span><span class="kbd">W</span></div>
      <div class="krow"><span>Jump to 0-90%</span><span class="kbd">0 - 9</span></div>
      <div class="krow"><span>Close player</span><span class="kbd">Esc</span></div>
      <div class="krow"><span>Show this panel</span><span class="kbd">?</span></div>
    </div>
  </div>
</div>

<footer class="sitefoot">
  <div class="foot-top">
    <div class="foot-brand">
      <div class="logo">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9A1.5 1.5 0 0 1 18.5 16H13l-3.4 3.1A.7.7 0 0 1 8.4 18.6V16H5.5A1.5 1.5 0 0 1 4 14.5v-9Zm6 2.6v4.8l4.2-2.4L10 8.1Z"/></svg>
      </div>
      <div>
        <div class="foot-name">Tech Arslan Software Solutions</div>
        <div class="foot-tag">All-in-One Video Downloader</div>
      </div>
    </div>
    <div class="foot-dev">
      <span class="devlabel">Developed by</span>
      <span class="devname">Tech Arslan</span>
      <span class="devorg">Tech Arslan Software Solutions</span>
      <span class="devsub">Design &amp; development of this tool</span>
    </div>
    <div class="foot-badges">
      <span class="fchip">Fast streaming</span>
      <span class="fchip">HD quality</span>
      <span class="fchip">No ads</span>
      <span class="fchip">Play next</span>
    </div>
  </div>
  <div class="foot-bar">
    <span class="copy">&copy; <span id="year"></span> Tech Arslan Software Solutions. All rights reserved.</span>
    <span class="made">Built &amp; maintained by <strong>Tech Arslan</strong></span>
  </div>
</footer>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<script>
const ICON_PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
const BRAND_SUFFIX = 'Powered by Tech Arslan Software Solutions';
const ICON_PAUSE = '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';
const ICON_VOL = '<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4zm12.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4z"/></svg>';
const ICON_MUTE = '<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4zm15.5-1.5-1.4-1.4L15.7 8.5l-2.4-2.4-1.4 1.4 2.4 2.4-2.4 2.4 1.4 1.4 2.4-2.4 2.4 2.4 1.4-1.4-2.4-2.4z"/></svg>';

const $ = id => document.getElementById(id);
const linkEl = $('link'), fetchBtn = $('fetch'), clearBtn = $('clear'), pasteBtn = $('paste');
const statusEl = $('status'), filesEl = $('files'), filesEmpty = $('filesEmpty');
const filesCount = $('filesCount'), histCount = $('histCount');
const player = $('player'), pbox = $('pbox'), stage = $('stage');
const playerVideo = $('playerVideo'), playerVideoIdle = $('playerVideoIdle');
const playerTitle = $('playerTitle'), playerMsg = $('playerMsg');
const playerTag = $('playerTag'), playerSpinner = $('playerSpinner'), controls = $('controls');
const centerPlay = $('centerPlay'), toastEl = $('toast');
const progressEl = $('progress'), bufferedEl = $('buffered'), playedEl = $('played'), knobEl = $('knob'), tipEl = $('tip');
const timeEl = $('time'), volEl = $('vol'), rateEl = $('rate');
const historyEl = $('history'), histEmpty = $('histEmpty'), histClearBtn = $('histClear');

function setStatus(msg, isError){ statusEl.textContent = msg || ''; statusEl.className = 'status' + (isError ? ' error' : ''); }
function toast(msg){ toastEl.textContent = msg; toastEl.classList.add('show'); clearTimeout(toastEl._t); toastEl._t = setTimeout(() => toastEl.classList.remove('show'), 1300); }
function fmtTime(sec){
  if (!isFinite(sec) || sec < 0) sec = 0;
  const s = Math.floor(sec % 60), m = Math.floor(sec / 60) % 60, h = Math.floor(sec / 3600);
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return (h ? h + ':' : '') + mm + ':' + String(s).padStart(2, '0');
}
function esc(s){ const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

// --- Pages (Social Downloader / TeraBox) -----------------------------------
function switchPage(name){
  document.querySelectorAll('.maintabs .tab').forEach(t => t.classList.toggle('active', t.dataset.page === name));
  $('page-social').classList.toggle('hidden', name !== 'social');
  $('page-terabox').classList.toggle('hidden', name !== 'terabox');
}
document.querySelectorAll('.maintabs .tab').forEach(t =>
  t.addEventListener('click', () => switchPage(t.dataset.page)));

// --- Tabs ------------------------------------------------------------------
document.querySelectorAll('#page-terabox .tabs .tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
function switchTab(name){
  document.querySelectorAll('#page-terabox .tabs .tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $('tab-files').classList.toggle('hidden', name !== 'files');
  $('tab-history').classList.toggle('hidden', name !== 'history');
}

// --- History (localStorage) ------------------------------------------------
const HIST_KEY = 'terabox.history.v2';
const HIST_MAX = 40;
let histFilter = 'all';

function loadHistory(){ try { return JSON.parse(localStorage.getItem(HIST_KEY)) || []; } catch (e) { return []; } }
function saveHistory(items){ try { localStorage.setItem(HIST_KEY, JSON.stringify(items.slice(0, HIST_MAX))); } catch (e) {} }
function addHistory(entry){
  const items = loadHistory();
  const key = entry.kind + '|' + entry.name + '|' + (entry.link || '');
  const rest = items.filter(it => (it.kind + '|' + it.name + '|' + (it.link || '')) !== key);
  rest.unshift(Object.assign({ at: Date.now() }, entry));
  saveHistory(rest);
  renderHistory();
}
function removeHistory(at){
  saveHistory(loadHistory().filter(it => it.at !== at));
  renderHistory();
}
document.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => {
  histFilter = c.dataset.filter;
  document.querySelectorAll('.chip').forEach(x => x.classList.toggle('active', x === c));
  renderHistory();
}));

function renderHistory(){
  const all = loadHistory();
  const items = histFilter === 'all' ? all : all.filter(it => it.kind === histFilter);
  historyEl.innerHTML = '';
  histCount.textContent = all.length;
  histClearBtn.classList.toggle('hidden', all.length === 0);
  histEmpty.classList.toggle('hidden', items.length > 0);
  for (const it of items) {
    const row = document.createElement('div');
    row.className = 'item';
    const f = { id: it.id, name: it.name, is_video: true };

    const badge = document.createElement('div');
    badge.className = 'thumb ' + it.kind;
    badge.innerHTML = it.kind === 'played'
      ? '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>'
      : '<svg viewBox="0 0 24 24"><path d="M12 3v10.6l3.3-3.3 1.4 1.4L12 17.4l-4.7-4.7 1.4-1.4L12 13.6V3zM5 19h14v2H5z"/></svg>';

    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.innerHTML = '<div class="name">' + esc(it.name) + '</div>' +
      '<div class="sub2">' + (it.kind === 'played' ? 'Played' : 'Downloaded') +
      '<span class="dot"></span>' + esc(new Date(it.at).toLocaleString()) + '</div>';

    const actions = document.createElement('div');
    actions.className = 'actions';
    const social = it.source === 'social';
    if (it.is_video) {
      const play = document.createElement('button');
      play.className = 'act play'; play.innerHTML = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>Play';
      if (social) {
        if (it.playable) play.addEventListener('click', () => socialPlay(
          { url: it.link, title: it.name }, { format_id: it.id, label: it.tag || 'Stream', audio_only: false }));
        else play.disabled = true;
      } else {
        play.addEventListener('click', () => openPlayer(f, it.link));
      }
      actions.append(play);
    }
    const dl = document.createElement('button');
    dl.className = 'act dl'; dl.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 3v10.6l3.3-3.3 1.4 1.4L12 17.4l-4.7-4.7 1.4-1.4L12 13.6V3zM5 19h14v2H5z"/></svg>Download';
    if (social) {
      dl.addEventListener('click', () => socialDownload(
        { url: it.link, title: it.name }, { format_id: it.id, progressive: it.progressive }, dl));
    } else {
      dl.addEventListener('click', () => startDownload(f, dl, it.link));
    }
    actions.append(dl);
    const copy = document.createElement('button');
    copy.className = 'act icon'; copy.title = 'Copy share link';
    copy.innerHTML = '<svg viewBox="0 0 24 24"><path d="M9 3h9v2H9V3zm-2 4h13v14H7V7zm2 2v10h9V9H9z"/></svg>';
    copy.addEventListener('click', () => copyText(it.link || linkEl.value.trim(), 'Link copied'));
    actions.append(copy);
    const del = document.createElement('button');
    del.className = 'act icon'; del.title = 'Remove from history';
    del.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 7h12v13H6V7zm3-4h6l1 2h4v2H4V5h4l1-2z"/></svg>';
    del.addEventListener('click', () => removeHistory(it.at));
    actions.append(del);

    row.append(badge, meta, actions);
    historyEl.append(row);
  }
}

// --- File list -------------------------------------------------------------
function renderFiles(share){
  filesEl.innerHTML = '';
  filesCount.textContent = share.files.length;
  filesEmpty.classList.toggle('hidden', share.files.length > 0);
  for (const f of share.files) {
    const row = document.createElement('div');
    row.className = 'item';
    const badge = document.createElement('div');
    badge.className = 'thumb';
    badge.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 4h16v16H4V4zm6 3v10l7-5-7-5z"/></svg>';

    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.innerHTML = '<div class="name">' + esc(f.name) + '</div>' +
      '<div class="sub2">' + esc(f.size_text || '') + (f.is_video ? '<span class="dot"></span>Video' : '') + '</div>';

    const actions = document.createElement('div');
    actions.className = 'actions';
    if (f.is_video) {
      const play = document.createElement('button');
      play.className = 'act play'; play.innerHTML = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>Play';
      play.addEventListener('click', () => openPlayer(f));
      actions.append(play);
    }
    const dl = document.createElement('button');
    dl.className = 'act dl'; dl.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 3v10.6l3.3-3.3 1.4 1.4L12 17.4l-4.7-4.7 1.4-1.4L12 13.6V3zM5 19h14v2H5z"/></svg>Download';
    dl.addEventListener('click', () => startDownload(f, dl));
    actions.append(dl);
    const copy = document.createElement('button');
    copy.className = 'act icon'; copy.title = 'Copy file link';
    copy.innerHTML = '<svg viewBox="0 0 24 24"><path d="M9 3h9v2H9V3zm-2 4h13v14H7V7zm2 2v10h9V9H9z"/></svg>';
    copy.addEventListener('click', () => copyText(mediaUrl(f, '/api/download'), 'Download link copied'));
    actions.append(copy);

    row.append(badge, meta, actions);
    filesEl.append(row);
  }
}

function mediaUrl(f, endpoint, link){
  return endpoint + '?link=' + encodeURIComponent(link || linkEl.value.trim()) +
         '&fs_id=' + encodeURIComponent(f.id) +
         '&name=' + encodeURIComponent(f.name || '') + '&t=' + Date.now();
}
function dispositionName(header){
  if (!header) return '';
  const star = /filename[*]=UTF-8''([^;]+)/i.exec(header);
  if (star) { try { return decodeURIComponent(star[1]); } catch (e) { return star[1]; } }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1] : '';
}
function brandName(name){
  if (!name || name.indexOf(BRAND_SUFFIX) >= 0) return name;
  const dot = name.lastIndexOf('.');
  if (dot > 0 && name.length - dot <= 6) return name.slice(0, dot) + ' - ' + BRAND_SUFFIX + name.slice(dot);
  return name + ' - ' + BRAND_SUFFIX;
}
function copyText(text, msg){
  if (!text) { toast('Nothing to copy'); return; }
  const done = () => toast(msg || 'Copied');
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, done);
  else { const t = document.createElement('textarea'); t.value = text; document.body.append(t); t.select();
         try { document.execCommand('copy'); } catch (e) {} t.remove(); done(); }
}

async function startDownload(f, button, link){
  // Preflight a 2-byte ranged GET: a real download answers with bytes, whereas
  // a refusal answers JSON - so the user sees a message instead of a JSON page.
  button.disabled = true;
  const url = mediaUrl(f, '/api/download', link);
  setStatus('Preparing download\u2026');
  try {
    const probe = await fetch(url, { headers: { Range: 'bytes=0-1' } });
    const type = probe.headers.get('Content-Type') || '';
    if (probe.ok && !type.includes('application/json')) {
      const name = dispositionName(probe.headers.get('Content-Disposition')) || f.name;
      addHistory({ kind: 'downloaded', name: name, id: f.id, link: link || linkEl.value.trim(), is_video: !!f.is_video });
      window.location.href = url;
      setStatus('Download started: ' + name);
    } else {
      let msg = 'Download failed.';
      try { msg = (await probe.json()).error || msg; } catch (e) {}
      setStatus(msg, true);
    }
  } catch (err) {
    setStatus('Download failed: ' + err.message, true);
  } finally {
    button.disabled = false;
  }
}

// --- Player ----------------------------------------------------------------
// Two <video> elements: the one being watched, and a hidden one used to buffer
// the next video in the background so switching is instant.
let activeVideo = playerVideo;
let idleVideo = playerVideoIdle;
let hls = null;        // Hls instance for the active video
let hlsIdle = null;    // Hls instance prefetching the next video
let playAttempt = 0;
let rotation = 0;
let hideTimer = null;
let currentFile = null;
let currentLink = '';
let nextFile = null;
let nextLink = '';
let autoSwitched = false;
let prefVolume = 1;
let prefMuted = false;

function showSpinner(on){ playerSpinner.style.display = on ? 'flex' : 'none'; }
function setPlayingIcon(){
  const paused = activeVideo.paused;
  $('btnPlay').innerHTML = paused ? ICON_PLAY : ICON_PAUSE;
  centerPlay.classList.toggle('hidden', !paused || activeVideo.readyState === 0);
}
function showControls(){
  controls.classList.remove('hide');
  clearTimeout(hideTimer);
  if (!activeVideo.paused) hideTimer = setTimeout(() => controls.classList.add('hide'), 3000);
}
function applyRotation(){
  const deg = ((rotation % 360) + 360) % 360;
  const sw = stage.clientWidth, sh = stage.clientHeight;
  if ((deg === 90 || deg === 270) && sw && sh) {
    // Swap the box to the stage's opposite sides so the rotated video fills
    // the screen instead of shrinking to fit inside the upright box.
    activeVideo.style.width = sh + 'px';
    activeVideo.style.height = sw + 'px';
    activeVideo.style.transform = 'rotate(' + deg + 'deg)';
  } else {
    activeVideo.style.width = '';
    activeVideo.style.height = '';
    activeVideo.style.transform = deg ? 'rotate(' + deg + 'deg)' : 'none';
  }
}
function resetVideo(v){
  try { v.pause(); } catch (e) {}
  v.removeAttribute('src');
  try { v.load(); } catch (e) {}
  v.style.transform = 'none';
  v.style.width = '';
  v.style.height = '';
  v.classList.toggle('ghost', v !== activeVideo);
}
function setNextStatus(msg, kind){
  const el = $('nextStatus');
  el.textContent = msg || '';
  el.className = 'nextstatus' + (kind ? ' ' + kind : '');
}
// Show (or hide) the source link under the player title, so the user can see
// where the video came from and copy it again.
function setPlayerSource(url){
  const wrap = $('playerLink'), a = $('playerLinkA');
  if (!url) { wrap.classList.add('hidden'); a.removeAttribute('href'); a.textContent = ''; return; }
  a.href = url;
  const scheme = url.indexOf('://');
  a.textContent = scheme >= 0 ? url.slice(scheme + 3) : url;
  a.title = url;
  wrap.classList.remove('hidden');
}
function tearDownPlayer(){
  playAttempt++;
  if (hls) { hls.destroy(); hls = null; }
  if (hlsIdle) { hlsIdle.destroy(); hlsIdle = null; }
  activeVideo = playerVideo; idleVideo = playerVideoIdle;
  resetVideo(playerVideo);
  resetVideo(playerVideoIdle);
  rotation = 0;
  playedEl.style.width = '0%'; bufferedEl.style.width = '0%'; knobEl.style.left = '0%';
  timeEl.textContent = '0:00 / 0:00';
  playerTag.classList.add('hidden'); playerTag.classList.remove('preview');
  setPlayerSource('');
  playerSpinner.style.display = 'none';
  centerPlay.classList.add('hidden');
  controls.classList.remove('hide');
  nextFile = null; nextLink = ''; autoSwitched = false;
  $('nextList').innerHTML = '';
  $('nextLink').value = '';
  $('nextPlay').classList.add('hidden');
  setNextStatus('Play a video, then paste the next link. Playback continues until the new one is ready.');
  setFullscreen(false);
}

// Attach an HLS (or plain) source to a video element. Returns the Hls instance
// when hls.js is used, so it can be destroyed later.
function loadVideo(video, f, link, onReady, onFail){
  const url = mediaUrl(f, '/api/hls', link);
  if (window.Hls && window.Hls.isSupported()) {
    const inst = new window.Hls({ lowLatencyMode: false });
    inst.loadSource(url);
    inst.attachMedia(video);
    inst.on(window.Hls.Events.MANIFEST_PARSED, () => onReady && onReady());
    inst.on(window.Hls.Events.ERROR, (e, data) => { if (data && data.fatal) onFail && onFail(); });
    return inst;
  }
  video.src = url;
  video.addEventListener('loadedmetadata', () => onReady && onReady(), { once: true });
  video.addEventListener('error', () => onFail && onFail(), { once: true });
  return null;
}
function playFail(){
  showSpinner(false);
  playerMsg.classList.add('error');
  playerMsg.textContent = 'This video could not be played. Try Download instead.';
}

async function openPlayer(f, link){
  const useLink = link || linkEl.value.trim();
  tearDownPlayer();
  currentFile = f; currentLink = useLink;
  const attempt = ++playAttempt;
  playerTitle.textContent = f.name;
  playerTag.textContent = 'Full length';
  playerTag.classList.remove('hidden'); playerTag.classList.remove('preview');
  player.classList.remove('hidden');
  document.body.classList.add('playing');
  pushPlayerHistory();
  playerMsg.textContent = 'Preparing stream\u2026';
  playerMsg.classList.remove('error');
  showSpinner(true);
  setPlayingIcon();
  showControls();
  addHistory({ kind: 'played', name: f.name, id: f.id, link: useLink, is_video: true });

  // Resolve the playlist first (behind the spinner); the helpers take several
  // seconds to answer, and starting the video only afterwards keeps it from
  // giving up while the stream is still being arranged.
  let prep = null;
  try { const resp = await fetch(mediaUrl(f, '/api/prepare', useLink)); prep = await resp.json(); }
  catch (err) {}
  if (attempt !== playAttempt) return;

  if (prep && !prep.ok) {
    playerTag.textContent = 'Preview';
    playerTag.classList.add('preview');
    playerMsg.textContent = (prep.error ? prep.error + ' ' : '') +
      'Showing TeraBox\u2019s 30-second preview.';
  } else {
    playerMsg.textContent = '';
  }
  activeVideo.volume = prefVolume;
  activeVideo.muted = prefMuted;
  hls = loadVideo(activeVideo, f, useLink,
    () => activeVideo.play().catch(() => {}),
    playFail);
}

// --- "Play next": buffer a new link while the current video keeps playing ---
function firstToken(s){
  // Escape-free whitespace split (space, tab, LF, CR) so the value survives
  // being embedded in a Python string literal.
  const out = [];
  for (const ch of s) {
    const c = ch.charCodeAt(0);
    if (c === 32 || c === 9 || c === 10 || c === 13) break;
    out.push(ch);
  }
  return out.join('');
}
function extractShareUrl(raw){
  if (!raw) return '';
  const s = raw.trim();
  // A bare share id (no slashes or dots) is accepted too.
  if (/^[A-Za-z0-9_-]{6,}$/.test(s)) return 'https://www.terabox.com/s/' + s;
  const idx = s.search(/https?:/i);
  if (idx < 0) return '';
  const url = firstToken(s.slice(idx));
  return /terabox|dubox|1024terabox/i.test(url) ? url : '';
}

async function loadNext(){
  const raw = $('nextLink').value.trim();
  if (!raw) { setNextStatus('Paste a TeraBox link first.', 'err'); return; }
  const share = extractShareUrl(raw);
  if (!share) { setNextStatus('That does not look like a TeraBox share link.', 'err'); return; }
  $('nextList').innerHTML = '';
  setNextStatus('Loading the share\u2026');
  let data;
  try {
    const resp = await fetch('/api/resolve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ link: share })
    });
    data = await resp.json();
  } catch (err) { setNextStatus('Request failed: ' + err.message, 'err'); return; }
  if (!data.ok) { setNextStatus(data.error || 'Could not load that share.', 'err'); return; }
  const videos = data.share.files.filter(f => f.is_video);
  const list = videos.length ? videos : data.share.files;
  if (!list.length) { setNextStatus('That share has no files.', 'err'); return; }
  if (list.length === 1) { prefetchNext(list[0], share); return; }
  setNextStatus('Pick the video to play next (' + list.length + ' found):');
  for (const f of list) {
    const b = document.createElement('button');
    b.className = 'act';
    b.textContent = f.name;
    b.addEventListener('click', () => prefetchNext(f, share));
    $('nextList').append(b);
  }
}

function prefetchNext(f, shareLink){
  if (hlsIdle) { hlsIdle.destroy(); hlsIdle = null; }
  idleVideo.removeEventListener('canplay', onIdleReady);
  idleVideo.removeEventListener('progress', onIdleReady);
  resetVideo(idleVideo);
  nextFile = f; nextLink = shareLink; autoSwitched = false;
  $('nextList').innerHTML = '';
  $('nextPlay').classList.remove('hidden');
  setNextStatus('Buffering \u201c' + f.name + '\u201d \u2014 the current video keeps playing.', 'ok');
  idleVideo.muted = true;
  idleVideo.volume = 0;
  hlsIdle = loadVideo(idleVideo, f, shareLink, () => {
    setNextStatus('Buffering \u201c' + f.name + '\u201d \u2014 the current video keeps playing.', 'ok');
  }, () => setNextStatus('Could not prepare that video.', 'err'));
  idleVideo.addEventListener('canplay', onIdleReady);
  idleVideo.addEventListener('progress', onIdleReady);
}

function onIdleReady(){
  if (!nextFile || autoSwitched || activeVideo === idleVideo) return;
  const b = idleVideo.buffered;
  const ahead = b.length ? b.end(b.length - 1) - idleVideo.currentTime : 0;
  if (idleVideo.readyState >= 4 || ahead >= 6) {
    autoSwitched = true;
    idleVideo.removeEventListener('canplay', onIdleReady);
    idleVideo.removeEventListener('progress', onIdleReady);
    activateNext();
  }
}

// Swap the buffered idle video in as the one being watched, without a reload.
function activateNext(){
  if (!nextFile) return;
  const oldActive = activeVideo;
  const oldHls = hls;
  activeVideo = idleVideo;
  idleVideo = oldActive;
  hls = hlsIdle;
  hlsIdle = null;
  if (oldHls) oldHls.destroy();

  activeVideo.classList.remove('ghost');
  idleVideo.classList.add('ghost');
  resetVideo(idleVideo);

  currentFile = nextFile; currentLink = nextLink;
  playerTitle.textContent = currentFile.name;
  playerTag.textContent = 'Full length';
  playerTag.classList.remove('preview');
  nextFile = null; nextLink = ''; autoSwitched = false;
  $('nextLink').value = '';
  $('nextList').innerHTML = '';
  $('nextPlay').classList.add('hidden');
  setNextStatus('Loaded. Paste another link to queue the next video.');

  rotation = 0;
  activeVideo.volume = prefVolume;
  activeVideo.muted = prefMuted;
  activeVideo.playbackRate = Number(rateEl.value);
  activeVideo.loop = $('btnLoop').classList.contains('on');
  playerMsg.textContent = '';
  playerMsg.classList.remove('error');
  applyRotation();
  activeVideo.play().catch(() => {});
  addHistory({ kind: 'played', name: currentFile.name, id: currentFile.id, link: currentLink, is_video: true });
  toast('Now playing: ' + currentFile.name);
  showControls();
}

function closePlayer(){
  tearDownPlayer();
  player.classList.add('hidden');
  document.body.classList.remove('playing');
  playerMsg.textContent = '';
  if (playerOnHistory) {
    // Drop the entry we pushed when the player opened, so Back and Close stay
    // in sync and a second Back does not skip past the app.
    playerOnHistory = false;
    try { history.back(); } catch (e) {}
  }
}

// A plain (non-TeraBox) media URL played in the same full-page player. Used by
// the social downloader, where the server already hands back a ready stream.
async function openMediaPlayer(opts){
  tearDownPlayer();
  currentFile = { name: opts.title || 'Video' };
  currentLink = '';
  playAttempt++;
  playerTitle.textContent = opts.title || 'Video';
  playerTag.textContent = opts.tag || 'Stream';
  playerTag.classList.remove('hidden'); playerTag.classList.remove('preview');
  setPlayerSource(opts.source || '');
  player.classList.remove('hidden');
  document.body.classList.add('playing');
  pushPlayerHistory();
  playerMsg.textContent = '';
  playerMsg.classList.remove('error');
  showSpinner(true);
  setPlayingIcon();
  showControls();

  const video = activeVideo;
  video.volume = prefVolume;
  video.muted = prefMuted;
  video.src = opts.src;
  video.addEventListener('loadedmetadata', () => { video.play().catch(() => {}); }, { once: true });
  video.addEventListener('error', () => {
    showSpinner(false);
    playerMsg.classList.add('error');
    playerMsg.textContent = 'This stream could not be played here. Try Download instead.';
  }, { once: true });
  try { video.load(); } catch (e) {}
}

// video events -> UI (bound to both videos; only the active one drives the UI)
function bindVideo(v){
  v.addEventListener('loadeddata', () => { if (v !== activeVideo) return; showSpinner(false); setPlayingIcon(); });
  v.addEventListener('waiting', () => { if (v !== activeVideo) return; showSpinner(true); });
  v.addEventListener('playing', () => { if (v !== activeVideo) return; showSpinner(false); setPlayingIcon(); showControls(); });
  v.addEventListener('pause', () => { if (v !== activeVideo) return; setPlayingIcon(); controls.classList.remove('hide'); });
  v.addEventListener('play', () => { if (v !== activeVideo) return; setPlayingIcon(); });
  v.addEventListener('ended', () => { if (v !== activeVideo) return; setPlayingIcon(); });
  v.addEventListener('timeupdate', () => { if (v !== activeVideo) return; updateProgress(); });
  v.addEventListener('loadedmetadata', () => { if (v !== activeVideo) return; updateProgress(); applyRotation(); });
  v.addEventListener('progress', () => { if (v !== activeVideo) return; updateProgress(); });
}
bindVideo(playerVideo);
bindVideo(playerVideoIdle);

function updateProgress(){
  const d = activeVideo.duration || 0;
  const pct = d ? (activeVideo.currentTime / d) * 100 : 0;
  playedEl.style.width = pct + '%';
  knobEl.style.left = pct + '%';
  timeEl.textContent = fmtTime(activeVideo.currentTime) + ' / ' + fmtTime(d);
  if (activeVideo.buffered.length && d) {
    bufferedEl.style.width = (activeVideo.buffered.end(activeVideo.buffered.length - 1) / d * 100) + '%';
  }
}

// progress bar: pointer events cover mouse and touch
let dragging = false;
function ratioAt(clientX){
  const rect = progressEl.getBoundingClientRect();
  return Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
}
function seekTo(clientX){
  const d = activeVideo.duration || 0;
  if (!d) return;
  activeVideo.currentTime = ratioAt(clientX) * d;
  updateProgress();
}
progressEl.addEventListener('pointerdown', e => {
  dragging = true;
  try { progressEl.setPointerCapture(e.pointerId); } catch (x) {}
  seekTo(e.clientX);
});
progressEl.addEventListener('pointermove', e => {
  const d = activeVideo.duration || 0;
  const ratio = ratioAt(e.clientX);
  tipEl.style.left = (ratio * 100) + '%';
  tipEl.textContent = fmtTime(ratio * d);
  if (dragging) seekTo(e.clientX);
});
progressEl.addEventListener('pointerup', e => {
  dragging = false;
  try { progressEl.releasePointerCapture(e.pointerId); } catch (x) {}
});
progressEl.addEventListener('pointercancel', () => { dragging = false; });

// controls
$('btnPlay').addEventListener('click', () => { if (activeVideo.paused) activeVideo.play().catch(() => {}); else activeVideo.pause(); });
centerPlay.addEventListener('click', () => activeVideo.play().catch(() => {}));
$('btnBack').addEventListener('click', () => { activeVideo.currentTime = Math.max(0, activeVideo.currentTime - 10); toast('-10s'); });
$('btnFwd').addEventListener('click', () => { activeVideo.currentTime = Math.min(activeVideo.duration || 0, activeVideo.currentTime + 10); toast('+10s'); });
volEl.addEventListener('input', () => {
  prefVolume = Number(volEl.value);
  prefMuted = prefVolume === 0;
  activeVideo.volume = prefVolume;
  activeVideo.muted = prefMuted;
  syncMute();
});
$('btnMute').addEventListener('click', () => { prefMuted = !prefMuted; activeVideo.muted = prefMuted; syncMute(); });
function syncMute(){
  $('btnMute').innerHTML = prefMuted || prefVolume === 0 ? ICON_MUTE : ICON_VOL;
  $('btnMute').classList.toggle('on', prefMuted || prefVolume === 0);
}
rateEl.addEventListener('change', () => { activeVideo.playbackRate = Number(rateEl.value); toast('Speed ' + rateEl.value + '\u00d7'); });
$('btnLoop').addEventListener('click', () => {
  activeVideo.loop = !activeVideo.loop;
  $('btnLoop').classList.toggle('on', activeVideo.loop);
  toast(activeVideo.loop ? 'Loop on' : 'Loop off');
});
$('btnPip').addEventListener('click', async () => {
  try {
    if (document.pictureInPictureElement) await document.exitPictureInPicture();
    else await activeVideo.requestPictureInPicture();
  } catch (e) { toast('Picture in picture not available'); }
});
$('btnRotate').addEventListener('click', () => {
  rotation = (rotation + 90) % 360;
  applyRotation();
  toast('Rotated ' + rotation + '\u00b0');
});
$('btnShot').addEventListener('click', () => {
  try {
    const c = document.createElement('canvas');
    c.width = activeVideo.videoWidth; c.height = activeVideo.videoHeight;
    c.getContext('2d').drawImage(activeVideo, 0, 0);
    const a = document.createElement('a');
    a.href = c.toDataURL('image/png');
    a.download = (currentFile ? currentFile.name.replace(/[.][^.]+$/, '') : 'frame') + '-' + Math.floor(activeVideo.currentTime) + 's.png';
    a.click();
    toast('Snapshot saved');
  } catch (e) { toast('Snapshot blocked by the stream'); }
});
$('btnWide').addEventListener('click', () => {
  pbox.classList.toggle('wide');
  $('btnWide').classList.toggle('on', pbox.classList.contains('wide'));
  setTimeout(applyRotation, 220);
});

// Fullscreen is done in the page itself: the player is already fixed over the
// whole viewport, so toggling the .full class hides the chrome and lets the
// video fill the screen. The browser's native Fullscreen API is deliberately
// not used - the runtime host shows its own overlay on top of it.
function isFs(){ return player.classList.contains('full'); }
function toggleFullscreen(){ setFullscreen(!isFs()); }
function setFullscreen(on){
  player.classList.toggle('full', on);
  $('btnFs').classList.toggle('on', on);
  setTimeout(applyRotation, 120);
}
document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement) setFullscreen(false); });
document.addEventListener('webkitfullscreenchange', () => { if (!document.webkitFullscreenElement) setFullscreen(false); });
$('btnFs').addEventListener('click', toggleFullscreen);
window.addEventListener('resize', applyRotation);
window.addEventListener('orientationchange', () => setTimeout(applyRotation, 200));

$('btnDownloadTop').addEventListener('click', () => { if (currentFile) startDownload(currentFile, $('btnDownloadTop'), currentLink); });
$('playerClose').addEventListener('click', closePlayer);
$('playerLinkCopy').addEventListener('click', () => {
  const a = $('playerLinkA');
  if (a.href) copyText(a.href, 'Source link copied');
});
// No click-to-close on the backdrop: a stray tap outside the video used to
// dismiss the player and stop playback.
stage.addEventListener('mousemove', showControls);
stage.addEventListener('touchstart', showControls, { passive: true });
stage.addEventListener('click', e => {
  if (e.target === activeVideo || e.target === stage) {
    if (activeVideo.paused) activeVideo.play().catch(() => {}); else activeVideo.pause();
  }
});
stage.addEventListener('dblclick', e => { if (e.target === activeVideo || e.target === stage) toggleFullscreen(); });

// The player opens as its own page: give it a history entry so the browser
// Back button closes it instead of leaving the app.
let playerOnHistory = false;
function pushPlayerHistory(){
  if (playerOnHistory) return;
  try { history.pushState({ player: true }, '', '#play'); playerOnHistory = true; } catch (e) {}
}
window.addEventListener('popstate', () => {
  if (playerOnHistory && !player.classList.contains('hidden')) { playerOnHistory = false; closePlayer(); }
});

// play-next wiring
$('nextLoad').addEventListener('click', loadNext);
$('nextLink').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); loadNext(); } });
$('nextPlay').addEventListener('click', () => { if (nextFile) activateNext(); });

// shortcuts modal
function toggleHelp(force){
  const el = $('shortcuts');
  const show = force !== undefined ? force : el.classList.contains('hidden');
  el.classList.toggle('hidden', !show);
}
$('helpBtn').addEventListener('click', () => toggleHelp());
$('shortcuts').addEventListener('click', e => { if (e.target === $('shortcuts')) toggleHelp(false); });

document.addEventListener('keydown', e => {
  const typing = e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA';
  if (e.key === 'Escape') { if (!$('shortcuts').classList.contains('hidden')) { toggleHelp(false); return; } if (!player.classList.contains('hidden')) closePlayer(); return; }
  if (player.classList.contains('hidden')) return;
  if (typing) return;
  const k = e.key.toLowerCase();
  if (e.key === ' ' || k === 'k') { e.preventDefault(); if (activeVideo.paused) activeVideo.play().catch(() => {}); else activeVideo.pause(); }
  else if (e.key === 'ArrowLeft' || k === 'j') { activeVideo.currentTime = Math.max(0, activeVideo.currentTime - 10); showControls(); }
  else if (e.key === 'ArrowRight') { activeVideo.currentTime = Math.min(activeVideo.duration || 0, activeVideo.currentTime + 10); showControls(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); volEl.value = Math.min(1, prefVolume + 0.1); volEl.dispatchEvent(new Event('input')); showControls(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); volEl.value = Math.max(0, prefVolume - 0.1); volEl.dispatchEvent(new Event('input')); showControls(); }
  else if (k === 'm') $('btnMute').click();
  else if (k === 'f') toggleFullscreen();
  else if (k === 'p') $('btnPip').click();
  else if (k === 'r') $('btnRotate').click();
  else if (k === 's') $('btnShot').click();
  else if (k === 'l') $('btnLoop').click();
  else if (k === 'w') $('btnWide').click();
  else if (k === '?') toggleHelp();
  else if (e.key >= '0' && e.key <= '9') { const d = activeVideo.duration || 0; if (d) activeVideo.currentTime = d * (Number(e.key) / 10); }
});
// --- Fetch -----------------------------------------------------------------
async function fetchFiles(){
  const link = linkEl.value.trim();
  if (!link) { setStatus('Please paste a link first.', true); return; }
  fetchBtn.disabled = true;
  setStatus('Fetching file list\u2026');
  try {
    const resp = await fetch('/api/resolve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ link })
    });
    const data = await resp.json();
    if (!data.ok) { setStatus(data.error, true); return; }
    setStatus(data.note
      ? 'Files ready. (TeraBox listing was busy, so these came from the helper service.)'
      : 'Share resolved.');
    renderFiles(data.share);
    switchTab('files');
  } catch (err) {
    setStatus('Request failed: ' + err.message, true);
  } finally {
    fetchBtn.disabled = false;
  }
}

fetchBtn.addEventListener('click', fetchFiles);
linkEl.addEventListener('keydown', e => { if (e.key === 'Enter') fetchFiles(); });
clearBtn.addEventListener('click', () => {
  linkEl.value = ''; filesEl.innerHTML = ''; filesCount.textContent = '0';
  filesEmpty.classList.remove('hidden'); setStatus(''); linkEl.focus();
});
pasteBtn.addEventListener('click', async () => {
  try { const t = await navigator.clipboard.readText(); if (t) { linkEl.value = t.trim(); setStatus('Pasted. Press Fetch files.'); } }
  catch (e) { setStatus('Clipboard blocked - paste manually.', true); }
});
histClearBtn.addEventListener('click', () => { saveHistory([]); renderHistory(); });

// --- Social downloader -----------------------------------------------------
// Badge map is filled from /api/social/platforms so the client never drifts
// from the server's list; 'other' is the only entry needed up front.
const S_PLAT = { other:{c:'#8b93a7',t:'?'} };
const sLinkEl = $('sLink'), sFetchBtn = $('sFetch'), sClearBtn = $('sClear');
const sPasteBtn = $('sPaste'), sStatusEl = $('sStatus');
const sResult = $('sResult'), sThumb = $('sThumb'), sTitle = $('sTitle'), sSub = $('sSub'), sTags = $('sTags');
const sPlaylist = $('sPlaylist'), plTitle = $('plTitle'), plHint = $('plHint');
const plItems = $('plItems'), plCount = $('plCount'), plDownload = $('plDownload');
const plAll = $('plAll'), plNone = $('plNone');
const sFormats = $('sFormats'), platformsEl = $('platforms'), platCountEl = $('platCount');
const platSearchEl = $('platSearch');
const helpBadgeEl = $('helpBadge'), helpTitleEl = $('helpTitle');
const helpWhereEl = $('helpWhere'), helpHowEl = $('helpHow');
const HELP_DEFAULT = {
  where: 'Open the video on the platform, tap the Share button, then choose Copy link. '
       + 'You can also copy the address from your browser\u2019s address bar.',
  how: 'Paste the link in the box above and press Get video. Play it in the browser when a '
     + 'single-file stream is offered, otherwise pick a quality to download it as an MP4.'
};
function showPlatformHelp(p){
  const h = (p && p.help) || HELP_DEFAULT;
  helpBadgeEl.textContent = (p && p.badge) || '?';
  helpBadgeEl.style.background = (p && p.color) || '#8b93a7';
  helpTitleEl.textContent = p ? ('How to download from ' + p.name) : 'How to use this tool';
  helpWhereEl.textContent = h.where;
  helpHowEl.textContent = h.how;
}
platSearchEl.addEventListener('input', () => renderPlatforms(allPlatforms));
let socialMedia = null;

function sSetStatus(msg, isError){ sStatusEl.textContent = msg || ''; sStatusEl.className = 'status' + (isError ? ' error' : ''); }

function humanSize(n){
  if (!n || n < 0) return '';
  const u = ['B','KB','MB','GB'];
  let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(n >= 100 ? 0 : 1)) + ' ' + u[i];
}
function platBadge(key){
  const p = S_PLAT[key] || S_PLAT.other;
  return '<span class="pd" style="background:' + p.c + '">' + p.t + '</span>';
}
let allPlatforms = [];
function makePlatButton(p){
  S_PLAT[p.key] = {c: p.color, t: p.badge || (p.name || '?').slice(0, 2).toUpperCase()};
  const b = document.createElement('button');
  b.className = 'plat' + (p.featured ? ' feat' : '');
  b.dataset.key = p.key;
  b.innerHTML = platBadge(p.key) + '<span class="pn">' + esc(p.name) + '</span>';
  b.title = 'Paste a ' + p.name + ' link';
  b.addEventListener('click', () => {
    document.querySelectorAll('.plat').forEach(x => x.classList.toggle('active', x === b));
    showPlatformHelp(p);
    sLinkEl.focus();
    sSetStatus('Paste the ' + p.name + ' link in the box, then press Get video.');
  });
  return b;
}
function platGroup(label){
  const h = document.createElement('div');
  h.className = 'platgroup';
  h.textContent = label;
  return h;
}
const quickEl = $('quickPlatforms'), quickCountEl = $('quickCount');
function renderQuickPlatforms(list){
  const featured = list.filter(p => p.featured);
  quickEl.innerHTML = '';
  quickCountEl.textContent = featured.length + ' sites';
  for (const p of featured) {
    S_PLAT[p.key] = {c: p.color, t: p.badge || (p.name || '?').slice(0, 2).toUpperCase()};
    const b = document.createElement('button');
    b.className = 'qplat';
    b.dataset.key = p.key;
    b.innerHTML = platBadge(p.key) + '<span class="pn">' + esc(p.name) + '</span>';
    b.title = 'Paste a ' + p.name + ' link';
    b.addEventListener('click', () => {
      // Selecting a popular site scrolls to the input and focuses it, and
      // mirrors the same "selected" state the sidebar picker uses.
      document.querySelectorAll('.plat').forEach(x =>
        x.classList.toggle('active', x.dataset.key === p.key));
      showPlatformHelp(p);
      sLinkEl.focus();
      sLinkEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
      sSetStatus('Paste the ' + p.name + ' link in the box, then press Get video.');
    });
    quickEl.appendChild(b);
  }
}
function renderPlatforms(list){
  allPlatforms = list;
  renderQuickPlatforms(list);
  const q = (platSearchEl.value || '').trim().toLowerCase();
  const shown = q ? list.filter(p => p.name.toLowerCase().includes(q)) : list;
  platformsEl.innerHTML = '';
  platCountEl.textContent = (q ? shown.length + ' / ' : '') + list.length + ' sites';
  if (!shown.length) {
    platformsEl.innerHTML = '<div class="empty">No platform matches "' + esc(q) + '".</div>';
    return;
  }
  // Search results are a flat list; otherwise the mainstream sites lead in
  // their own group and everything else follows, so the common ones are
  // reachable without scrolling.
  if (q) {
    for (const p of shown) platformsEl.appendChild(makePlatButton(p));
    return;
  }
  const featured = shown.filter(p => p.featured);
  const rest = shown.filter(p => !p.featured);
  if (featured.length) {
    platformsEl.appendChild(platGroup('Popular social media'));
    for (const p of featured) platformsEl.appendChild(makePlatButton(p));
  }
  if (rest.length) {
    platformsEl.appendChild(platGroup('All other sites'));
    for (const p of rest) platformsEl.appendChild(makePlatButton(p));
  }
}

function sourceTag(media){
  const attempts = media.attempts || [];
  if (!attempts.length) return '';
  const label = SOURCE_LABELS[media.source] || media.source;
  // Nothing failed: the primary source answered, so no badge is needed.
  return '<span class="rtag" title="' + esc(attempts.map(a => a.label + ': ' + a.reason).join(' | ')) +
         '">Backup source: ' + esc(label) + '</span>';
}

function renderResult(media){
  socialMedia = media;
  sResult.classList.remove('hidden');
  if (media.thumbnail) {
    sThumb.innerHTML = '<img alt="" src="' + esc(media.thumbnail) + '" referrerpolicy="no-referrer" onerror="this.remove()">';
  } else {
    sThumb.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 5h16v14H4V5zm2 2v10h12V7H6zm4 1.5 5 3.5-5 3.5v-7z"/></svg>';
  }
  sTitle.textContent = media.title || 'Video';
  const bits = [];
  if (media.uploader) bits.push(media.uploader);
  if (media.duration_text) bits.push(media.duration_text);
  if (media.is_live) bits.push('Live');
  sSub.textContent = bits.join(' \u00b7 ');
  const pname = (platformsEl.querySelector('.plat[data-key="' + media.platform + '"] .pn') || {}).textContent;
  showPlatformHelp(allPlatforms.find(p => p.key === media.platform));
  sTags.innerHTML =
    (pname ? '<span class="rtag">' + esc(pname) + '</span>' : '') +
    '<span class="rtag">' + media.formats.length + ' formats</span>' +
    (media.can_play ? '<span class="rtag">Playable here</span>' : '<span class="rtag">Download only</span>') +
    sourceTag(media);

  sFormats.innerHTML = '';
  for (const f of media.formats) {
    const row = document.createElement('div');
    row.className = 'frow';
    const size = f.size ? humanSize(f.size) : '';
    const meta = [f.resolution, f.audio_only ? 'audio' : '', size].filter(Boolean).join(' \u00b7 ');
    row.innerHTML =
      '<span class="fq">' + esc(f.label) + '</span>' +
      '<span class="fm">' + esc(meta) + '</span>' +
      '<span class="fa"></span>';
    const actions = row.querySelector('.fa');
    if (f.playable) {
      const pb = document.createElement('button');
      pb.className = 'btn primary sm';
      pb.textContent = 'Play';
      pb.addEventListener('click', () => socialPlay(media, f));
      actions.appendChild(pb);
    }
    const db = document.createElement('button');
    db.className = 'btn ' + (f.playable ? 'ghost' : 'primary') + ' sm';
    db.textContent = 'Download';
    db.addEventListener('click', () => socialDownload(media, f, db));
    actions.appendChild(db);
    sFormats.appendChild(row);
  }
}

function socialPlay(media, f){
  const url = '/api/social/stream?link=' + encodeURIComponent(media.url) + '&format=' + encodeURIComponent(f.format_id);
  openMediaPlayer({
    title: media.title,
    tag: f.label,
    src: url,
    isAudio: !!f.audio_only,
    source: media.url,
  });
  addHistory({ kind: 'played', name: media.title, id: f.format_id, link: media.url,
    is_video: true, source: 'social', playable: !!f.playable,
    progressive: !!f.progressive, tag: f.label });
}

async function socialDownload(media, f, button){
  const url = '/api/social/download?link=' + encodeURIComponent(media.url) + '&format=' + encodeURIComponent(f.format_id);
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Preparing\u2026';
  sSetStatus(f.progressive ? 'Preparing download\u2026'
                           : 'Merging video and audio on the server\u2026 this can take a moment.');
  // The server sends the file as an attachment, so a hidden iframe starts the
  // browser's own download and keeps the page responsive.
  try {
    const frame = document.createElement('iframe');
    frame.style.display = 'none';
    frame.src = url;
    document.body.appendChild(frame);
    setTimeout(() => frame.remove(), 120000);
    addHistory({ kind: 'downloaded', name: brandName(media.title), id: f.format_id, link: media.url,
      is_video: true, source: 'social', playable: !!f.playable,
      progressive: !!f.progressive, tag: f.label });
    setTimeout(() => sSetStatus('Download started. If nothing happened, the source refused it.'), 2500);
  } catch (e) {
    sSetStatus('Download failed to start: ' + e.message, true);
  } finally {
    setTimeout(() => { button.disabled = false; button.textContent = label; }, 1500);
  }
}


// Mirrors the server's check, so a playlist link never goes down the
// single-video path and produces a confusing "no media" error.
function looksLikePlaylist(link){
  let u;
  try { u = new URL(link); } catch (e) { return false; }
  const host = u.hostname.replace(/^www\\./, '').toLowerCase();
  const path = u.pathname.toLowerCase();
  if (host.endsWith('soundcloud.com') && path.includes('/sets/')) return true;
  if (host.includes('vimeo.com') && (path.includes('/album/') || path.includes('/channels/') || path.includes('/showcase/'))) return true;
  if (host.includes('dailymotion.com') && path.startsWith('/playlist/')) return true;
  const known = ['youtube.com','youtu.be','youtube-nocookie.com','soundcloud.com','vimeo.com','dailymotion.com'];
  if (!known.some(h => host === h || host.endsWith('.' + h))) return false;
  return u.searchParams.has('list');
}
let playlistState = null;

const SOURCE_LABELS = {
  'default': 'primary extractor',
  'no-impersonate': 'plain HTTP client',
  'alt-clients': 'alternate player clients',
  'generic': 'generic extractor',
  'html-scrape': 'page HTML',
};

// Mirrors the server cap, which stops a batch from outliving the request timeout.
const PLAYLIST_BATCH_LIMIT = Number('__PLAYLIST_BATCH_LIMIT__') || 40;

async function socialFetchPlaylist(link){
  sResult.classList.add('hidden');
  sPlaylist.classList.remove('hidden');
  plItems.innerHTML = '<div class="empty">Reading the playlist\u2026</div>';
  plTitle.textContent = 'Playlist';
  plHint.textContent = '';
  plCount.textContent = '';
  sSetStatus('Reading the playlist\u2026');
  try {
    const resp = await fetch('/api/social/playlist', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ link })
    });
    const data = await resp.json();
    if (!data.ok) {
      plItems.innerHTML = '';
      sPlaylist.classList.add('hidden');
      sSetStatus(data.error || 'That playlist could not be read.', true);
      return;
    }
    playlistState = data.playlist;
    renderPlaylist(data.playlist);
    sSetStatus('Found ' + data.playlist.count + ' items in "' + data.playlist.title + '".');
  } catch (e) {
    sPlaylist.classList.add('hidden');
    sSetStatus('Request failed: ' + e.message, true);
  }
}

function renderPlaylist(pl){
  plTitle.textContent = pl.title || 'Playlist';
  plHint.textContent = pl.count + ' items' + (pl.uploader ? ' \u00b7 ' + pl.uploader : '');
  plItems.innerHTML = '';
  for (const e of pl.entries) {
    const row = document.createElement('div');
    row.className = 'plrow';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.index = e.index;
    cb.addEventListener('change', updatePlCount);
    const idx = document.createElement('span');
    idx.className = 'plidx';
    idx.textContent = e.index;
    const name = document.createElement('span');
    name.className = 'plname';
    name.textContent = e.title;
    name.title = e.title;
    const dur = document.createElement('span');
    dur.className = 'pldur';
    dur.textContent = e.duration_text || '';
    row.append(cb, idx, name, dur);
    plItems.appendChild(row);
  }
  updatePlCount();
}

function plSelected(){
  return [...plItems.querySelectorAll('input:checked')].map(cb => cb.dataset.index);
}

function updatePlCount(){
  const n = plSelected().length;
  plCount.textContent = n ? n + ' selected' : 'none selected';
  plDownload.disabled = n === 0;
}

async function socialFetch(){
  const link = sLinkEl.value.trim();
  if (!link) { sSetStatus('Please paste a link first.', true); return; }
  sFetchBtn.disabled = true;
  sSetStatus('Reading the link\u2026');
  sResult.classList.add('hidden');
  // A playlist link is a different shape of result, so it is routed before
  // the single-video path rather than failing there.
  if (looksLikePlaylist(link)) {
    await socialFetchPlaylist(link);
    sFetchBtn.disabled = false;
    return;
  }
  try {
    const resp = await fetch('/api/social/resolve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ link })
    });
    const data = await resp.json();
    if (!data.ok) { sSetStatus(data.error || 'Could not read that link.', true); return; }
    renderResult(data.media);
    sSetStatus('Found: ' + data.media.title);
  } catch (e) {
    sSetStatus('Request failed: ' + e.message, true);
  } finally {
    sFetchBtn.disabled = false;
  }
}

plAll.addEventListener('click', () => {
  plItems.querySelectorAll('input').forEach(cb => { cb.checked = true; });
  updatePlCount();
});
plNone.addEventListener('click', () => {
  plItems.querySelectorAll('input').forEach(cb => { cb.checked = false; });
  updatePlCount();
});
plDownload.addEventListener('click', async () => {
  const picked = plSelected();
  if (!picked.length) return;
  // One request holds the connection open until every file in its zip exists, so
  // the server caps a batch. Splitting here means "select all" on a long playlist
  // still works instead of coming back as a limit error the user cannot act on.
  const batches = [];
  for (let i = 0; i < picked.length; i += PLAYLIST_BATCH_LIMIT) {
    batches.push(picked.slice(i, i + PLAYLIST_BATCH_LIMIT));
  }
  plDownload.disabled = true;
  // Two kinds of failure, and they need different words: an item that failed
  // inside a delivered ZIP is written into _failed_items.txt, while a batch that
  // never produced a ZIP has no file to point the user at.
  let ok = 0, inZip = 0, lost = 0, firstError = '';
  try {
    for (let b = 0; b < batches.length; b++) {
      const chunk = batches[b];
      const part = batches.length > 1 ? ' (part ' + (b + 1) + ' of ' + batches.length + ')' : '';
      sSetStatus(part
        ? 'Building ZIP' + part + ' for ' + chunk.length + ' items\u2026'
        : 'Building the ZIP for ' + chunk.length + ' items\u2026 this takes a moment.');
      const url = '/api/social/playlist/download?link=' + encodeURIComponent(playlistState.url)
                + '&items=' + encodeURIComponent(chunk.join(','));
      // A hidden iframe starts the browser's own download, but a failed request
      // answers with JSON that the iframe renders invisibly, so the user ends up
      // staring at an unchanged page. Fetching keeps the error text readable.
      const resp = await fetch(url);
      if (!resp.ok) {
        let why = 'That ZIP could not be built.';
        try { const d = await resp.json(); why = d.error || why; } catch (e) { /* body was not JSON */ }
        // Kept rather than shown now: the next batch would overwrite the status.
        if (!firstError) firstError = why;
        lost += chunk.length;
        continue;
      }
      const blob = await resp.blob();
      const savedN = +(resp.headers.get('X-Playlist-Items') || 0);
      const failedN = +(resp.headers.get('X-Playlist-Failures') || 0);
      const disp = resp.headers.get('Content-Disposition') || '';
      const named = disp.match(/filename\\*=UTF-8''([^;]+)/);
      const href = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = href;
      a.download = named ? decodeURIComponent(named[1]) : 'playlist.zip';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 120000);
      ok += savedN;
      inZip += failedN;
      // Browsers only honour one automatic download per gesture unless they are
      // asked; a short gap between parts keeps the rest from being dropped.
      if (b < batches.length - 1) await new Promise(r => setTimeout(r, 1200));
    }
    if (!ok) {
      sSetStatus('No files could be downloaded: ' + (inZip + lost) + ' item'
        + (inZip + lost === 1 ? '' : 's') + ' failed. ' + firstError, true);
    } else {
      const reasons = [];
      if (inZip) reasons.push(inZip + ' failed (each listed in _failed_items.txt inside its ZIP)');
      if (lost) reasons.push(lost + ' item' + (lost === 1 ? '' : 's') + ' could not be fetched: '
        + (firstError || 'no ZIP was produced'));
      // Server messages end in a full stop of their own; joining them as-is would
      // print "40.." at the end of the line.
      const tail = reasons.length ? '. ' + reasons.join('; ') : '';
      sSetStatus('Done: ' + ok + ' file' + (ok === 1 ? '' : 's') + ' downloaded'
        + (tail ? tail.replace(/[.\\s]+$/, '') : '') + '.');
      addHistory({ kind: 'downloaded', name: playlistState.title + ' (' + ok + ' items)',
        id: 'zip', link: playlistState.url, is_video: false, source: 'social', tag: 'ZIP' });
    }
  } catch (e) {
    sSetStatus('Request failed: ' + e.message, true);
  } finally {
    plDownload.disabled = false;
    updatePlCount();
  }
});

sFetchBtn.addEventListener('click', socialFetch);
sLinkEl.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); socialFetch(); } });
sClearBtn.addEventListener('click', () => {
  sLinkEl.value = ''; sResult.classList.add('hidden'); sSetStatus(''); socialMedia = null; sLinkEl.focus();
});
sPasteBtn.addEventListener('click', async () => {
  try { const t = await navigator.clipboard.readText(); if (t) { sLinkEl.value = t.trim(); sSetStatus('Pasted. Press Get video.'); } }
  catch (e) { sSetStatus('Clipboard blocked - paste manually.', true); }
});

(async () => {
  try {
    const resp = await fetch('/api/social/platforms');
    const data = await resp.json();
    if (data.ok) renderPlatforms(data.platforms);
    if (data.ok && !data.engine) sSetStatus('The downloader engine is not installed on the server.', true);
  } catch (e) {}
})();

renderHistory();
$('year').textContent = new Date().getFullYear();
</script>
</body>
</html>

"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "12000"))
    app.run(host="0.0.0.0", port=port)
