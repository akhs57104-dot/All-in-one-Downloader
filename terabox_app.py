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

import hashlib
import json
import os
import re
import secrets
import time
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
    return Response(INDEX_HTML, mimetype="text/html")

@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    link = ((request.json or {}).get("link") or "").strip()
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

def _proxy_stream(upstream, *, as_attachment: bool, filename: str,
                  content_type: str | None = None) -> Response:
    """Relay an upstream response, preserving Range/length semantics."""
    headers = {
        "Content-Type": content_type or upstream.headers.get(
            "Content-Type", "application/octet-stream"),
        "Accept-Ranges": "bytes",
    }
    if as_attachment:
        quoted = urllib.parse.quote(filename)
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quoted}"
    for name in ("Content-Length", "Content-Range"):
        if upstream.headers.get(name):
            headers[name] = upstream.headers[name]

    def body():
        try:
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
                # The helper's stream is a transcoded MPEG-TS, not the original
                # container, so it is offered with a .ts extension.
                headers = {"User-Agent": BROWSER_UA}
                if request.headers.get("Range"):
                    headers["Range"] = request.headers["Range"]
                upstream = _request_session().get(info["dl"], headers=headers,
                                                  stream=True, timeout=60)
                if upstream.status_code in (200, 206):
                    return _proxy_stream(upstream, as_attachment=True,
                                         filename=_ts_name(name),
                                         content_type="video/mp2t")
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

def _ts_name(name: str) -> str:
    """Give a transcoded stream a .ts name so players expect the right format."""
    base = name.rsplit(".", 1)[0] if "." in name else name
    return base + ".ts"

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
  .file .actions { display:flex; gap:8px; flex-shrink:0; }
  .file a, .file button { text-decoration:none; background:var(--primary); color:#fff; padding:8px 14px;
            border-radius:8px; font-size:.82rem; font-weight:600; border:0; cursor:pointer; }
  .file .play { background:#16a34a; }
  .hidden { display:none !important; }
  .note { background:#eefaf0; border:1px solid #b7e4c1; color:#1f5f36; border-radius:10px;
          padding:12px 14px; font-size:.82rem; line-height:1.45; margin-bottom:16px; }
  .note code { background:#d6f0dd; padding:1px 5px; border-radius:4px; font-size:.8rem; }
  .card-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
  .card-head label { margin:0; }
  .link-btn { background:none; border:0; color:var(--primary); font-size:.8rem; font-weight:600;
              cursor:pointer; padding:4px 6px; }
  .empty { color:var(--muted); font-size:.83rem; padding:4px 0; }

  /* History */
  .hist-item { display:flex; align-items:center; gap:12px; padding:9px 10px; border:1px solid #e2eaf2;
               border-radius:8px; margin-bottom:8px; }
  .hist-badge { flex-shrink:0; width:30px; height:30px; border-radius:8px; display:flex;
                align-items:center; justify-content:center; font-size:14px; background:#e7eef7; }
  .hist-badge.played { background:#dcfce7; }
  .hist-badge.downloaded { background:#dbeafe; }
  .hist-meta { min-width:0; flex:1; }
  .hist-name { font-size:.86rem; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .hist-sub { font-size:.74rem; color:var(--muted); }
  .hist-actions { display:flex; gap:6px; flex-shrink:0; }
  .hist-actions button { background:#e7eef7; color:var(--text); padding:6px 11px; font-size:.76rem; }
  .hist-actions .play { background:#16a34a; color:#fff; }

  /* Player */
  #player { position:fixed; inset:0; background:rgba(6,14,24,.9); display:flex; align-items:center;
            justify-content:center; padding:16px; z-index:50; }
  #player .box { background:#0b1520; border-radius:14px; width:min(1040px,100%); overflow:hidden;
                 box-shadow:0 24px 60px rgba(0,0,0,.55); display:flex; flex-direction:column; }
  #player .head { display:flex; align-items:center; gap:12px; padding:12px 16px; color:#eaf1f8; }
  #player .head .t { flex:1; font-size:.92rem; font-weight:600; overflow:hidden;
                     text-overflow:ellipsis; white-space:nowrap; }
  #player .head .tag { font-size:.7rem; color:#8fb0cc; border:1px solid #274054; border-radius:999px;
                       padding:2px 9px; flex-shrink:0; }
  #player .head .icon-btn { background:transparent; color:#cfe0ef; padding:6px 9px; font-size:1rem; }
  #player .stage { position:relative; background:#000; }
  #player video { width:100%; display:block; background:#000; max-height:74vh; }
  #player .spinner { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
                     pointer-events:none; }
  #player .spinner::after { content:""; width:42px; height:42px; border-radius:50%;
        border:3px solid rgba(255,255,255,.25); border-top-color:#fff; animation:spin .8s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  #player .controls { display:flex; flex-direction:column; gap:6px; padding:10px 14px 14px;
                      background:#0b1520; }
  #player .seek { width:100%; -webkit-appearance:none; appearance:none; height:5px; border-radius:3px;
                  background:#22384f; cursor:pointer; outline:none; }
  #player .seek::-webkit-slider-thumb { -webkit-appearance:none; width:13px; height:13px; border-radius:50%;
                  background:var(--primary); cursor:pointer; }
  #player .seek::-moz-range-thumb { width:13px; height:13px; border:0; border-radius:50%;
                  background:var(--primary); cursor:pointer; }
  #player .ctrl-row { display:flex; align-items:center; gap:12px; color:#dbe7f2; }
  #player .ctrl-row button { background:transparent; color:#dbe7f2; padding:6px 8px; font-size:1rem; }
  #player .ctrl-row button:hover { color:#fff; }
  #player .time { font-size:.78rem; font-variant-numeric:tabular-nums; color:#a9c1d6; min-width:96px; }
  #player .spacer { flex:1; }
  #player select { background:#16283a; color:#dbe7f2; border:1px solid #274054; border-radius:6px;
                   padding:5px 8px; font-size:.78rem; }
  #player .vol { width:80px; -webkit-appearance:none; appearance:none; height:4px; border-radius:3px;
                 background:#22384f; cursor:pointer; }
  #player .vol::-webkit-slider-thumb { -webkit-appearance:none; width:12px; height:12px; border-radius:50%;
                 background:#dbe7f2; cursor:pointer; }
  #player .pmsg { color:#a9c1d6; font-size:.8rem; padding:0 16px 14px; min-height:1em; }
</style>
</head>
<body>
<div class="app">
  <h1>TeraBox Downloader</h1>
  <div class="sub">Paste a public TeraBox share link to list, play, and download its files.</div>

  <div id="note" class="note">
    No account needed. TeraBox refuses anonymous direct downloads, so the app
    streams the full video through a helper service. That service sees the links
    you paste, and its copy is a transcoded stream (downloaded as <code>.ts</code>),
    not TeraBox's original file. Set <code>TERABOX_COOKIE</code> (see the README)
    to download the original MP4 instead, or <code>TERABOX_FALLBACK=off</code>
    to disable the helper.
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

  <div class="card">
    <div class="card-head">
      <label>History</label>
      <button id="histClear" class="link-btn hidden">Clear history</button>
    </div>
    <div id="history"></div>
  </div>
</div>

<div id="player" class="hidden">
  <div class="box">
    <div class="head">
      <span class="t" id="playerTitle">Video</span>
      <span class="tag hidden" id="playerTag"></span>
      <button class="icon-btn" id="playerClose" title="Close (Esc)">&#10005;</button>
    </div>
    <div class="stage">
      <video id="playerVideo" playsinline></video>
      <div class="spinner" id="playerSpinner"></div>
    </div>
    <div class="controls">
      <input class="seek" id="seek" type="range" min="0" max="0" step="0.1" value="0" aria-label="Seek">
      <div class="ctrl-row">
        <button id="btnPlay" title="Play/Pause (Space)">&#9654;</button>
        <button id="btnBack" title="Back 10s (&#8592;)">&#9198;</button>
        <button id="btnFwd" title="Forward 10s (&#8594;)">&#9197;</button>
        <span class="time" id="time">0:00 / 0:00</span>
        <span class="spacer"></span>
        <input class="vol" id="vol" type="range" min="0" max="1" step="0.05" value="1" aria-label="Volume">
        <button id="btnMute" title="Mute (M)">&#128266;</button>
        <select id="rate" title="Playback speed">
          <option value="0.5">0.5&times;</option>
          <option value="0.75">0.75&times;</option>
          <option value="1" selected>1&times;</option>
          <option value="1.25">1.25&times;</option>
          <option value="1.5">1.5&times;</option>
          <option value="2">2&times;</option>
        </select>
        <button id="btnFs" title="Fullscreen (F)">&#9974;</button>
      </div>
      <div class="pmsg" id="playerMsg"></div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<script>
const linkEl = document.getElementById('link');
const fetchBtn = document.getElementById('fetch');
const clearBtn = document.getElementById('clear');
const statusEl = document.getElementById('status');
const filesCard = document.getElementById('filesCard');
const filesEl = document.getElementById('files');
const filesTitle = document.getElementById('filesTitle');
const player = document.getElementById('player');
const playerVideo = document.getElementById('playerVideo');
const playerTitle = document.getElementById('playerTitle');
const playerMsg = document.getElementById('playerMsg');
const playerTag = document.getElementById('playerTag');
const playerSpinner = document.getElementById('playerSpinner');
const seekEl = document.getElementById('seek');
const timeEl = document.getElementById('time');
const volEl = document.getElementById('vol');
const rateEl = document.getElementById('rate');
const historyEl = document.getElementById('history');
const histClearBtn = document.getElementById('histClear');

function setStatus(msg, isError) {
  statusEl.textContent = msg || '';
  statusEl.className = isError ? 'error' : '';
}

function fmtTime(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const s = Math.floor(sec % 60), m = Math.floor(sec / 60) % 60, h = Math.floor(sec / 3600);
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return (h ? h + ':' : '') + mm + ':' + String(s).padStart(2, '0');
}

// --- History (localStorage) ------------------------------------------------

const HIST_KEY = 'terabox.history.v1';
const HIST_MAX = 30;

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HIST_KEY)) || []; }
  catch (e) { return []; }
}

function saveHistory(items) {
  try { localStorage.setItem(HIST_KEY, JSON.stringify(items.slice(0, HIST_MAX))); }
  catch (e) { /* storage full or blocked; history is best-effort */ }
}

function addHistory(entry) {
  const items = loadHistory();
  const key = entry.kind + '|' + entry.name + '|' + (entry.link || '');
  const rest = items.filter(it => (it.kind + '|' + it.name + '|' + (it.link || '')) !== key);
  rest.unshift(Object.assign({ at: Date.now() }, entry));
  saveHistory(rest);
  renderHistory();
}

function renderHistory() {
  const items = loadHistory();
  historyEl.innerHTML = '';
  histClearBtn.classList.toggle('hidden', items.length === 0);
  if (!items.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = 'Videos you play or download will show up here.';
    historyEl.append(p);
    return;
  }
  for (const it of items) {
    const row = document.createElement('div');
    row.className = 'hist-item';

    const badge = document.createElement('div');
    badge.className = 'hist-badge ' + it.kind;
    badge.textContent = it.kind === 'played' ? '\u25B6' : '\u2193';

    const meta = document.createElement('div');
    meta.className = 'hist-meta';
    const name = document.createElement('div');
    name.className = 'hist-name';
    name.textContent = it.name;
    const sub = document.createElement('div');
    sub.className = 'hist-sub';
    const when = new Date(it.at).toLocaleString();
    sub.textContent = (it.kind === 'played' ? 'Played' : 'Downloaded') + ' \u00B7 ' + when;
    meta.append(name, sub);

    const actions = document.createElement('div');
    actions.className = 'hist-actions';
    const f = { id: it.id, name: it.name, is_video: true };
    if (it.is_video) {
      const play = document.createElement('button');
      play.className = 'play';
      play.textContent = 'Play';
      play.addEventListener('click', () => openPlayer(f, it.link));
      actions.append(play);
    }
    const dl = document.createElement('button');
    dl.textContent = 'Download';
    dl.addEventListener('click', () => startDownload(f, dl, it.link));
    actions.append(dl);

    row.append(badge, meta, actions);
    historyEl.append(row);
  }
}

// --- File list -------------------------------------------------------------

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
    size.textContent = f.size_text || '';
    meta.append(name, size);

    const actions = document.createElement('div');
    actions.className = 'actions';
    if (f.is_video) {
      const play = document.createElement('button');
      play.className = 'play';
      play.textContent = 'Play';
      play.addEventListener('click', () => openPlayer(f));
      actions.append(play);
    }
    const dl = document.createElement('button');
    dl.textContent = 'Download';
    dl.addEventListener('click', () => startDownload(f, dl));
    actions.append(dl);

    row.append(meta, actions);
    filesEl.append(row);
  }
  filesCard.classList.remove('hidden');
}

function mediaUrl(f, endpoint, link) {
  return endpoint + '?link=' + encodeURIComponent(link || linkEl.value.trim()) +
         '&fs_id=' + encodeURIComponent(f.id) +
         '&name=' + encodeURIComponent(f.name || '') + '&t=' + Date.now();
}

function dispositionName(header) {
  if (!header) return '';
  const star = /filename[*]=UTF-8''([^;]+)/i.exec(header);
  if (star) { try { return decodeURIComponent(star[1]); } catch (e) { return star[1]; } }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1] : '';
}

async function startDownload(f, button, link) {
  // Preflight a 2-byte ranged GET: a real download answers with bytes, whereas
  // a refusal answers JSON — so the user sees a message instead of a JSON page.
  button.disabled = true;
  const url = mediaUrl(f, '/api/download', link);
  setStatus('Preparing download\u2026');
  try {
    const probe = await fetch(url, { headers: { Range: 'bytes=0-1' } });
    const type = probe.headers.get('Content-Type') || '';
    if (probe.ok && !type.includes('application/json')) {
      const name = dispositionName(probe.headers.get('Content-Disposition')) || f.name;
      addHistory({ kind: 'downloaded', name: name, id: f.id, link: link || linkEl.value.trim(),
                   is_video: !!f.is_video });
      window.location.href = url;  // Content-Disposition: attachment keeps the page
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

let hls = null;
let playAttempt = 0;

function tearDownPlayer() {
  playAttempt++;
  if (hls) { hls.destroy(); hls = null; }
  playerVideo.pause();
  playerVideo.removeAttribute('src');
  playerVideo.load();
  seekEl.value = 0; seekEl.max = 0;
  timeEl.textContent = '0:00 / 0:00';
  playerTag.classList.add('hidden');
  playerSpinner.style.display = 'none';
}

function showSpinner(on) { playerSpinner.style.display = on ? 'flex' : 'none'; }

function setPlayingIcon() {
  document.getElementById('btnPlay').innerHTML = playerVideo.paused ? '&#9654;' : '&#10074;&#10074;';
}

async function openPlayer(f, link) {
  const useLink = link || linkEl.value.trim();
  tearDownPlayer();
  const attempt = ++playAttempt;
  playerTitle.textContent = f.name;
  playerMsg.textContent = 'Preparing stream\u2026';
  playerTag.textContent = 'Full length';
  playerTag.classList.remove('hidden');
  showSpinner(true);
  player.classList.remove('hidden');
  setPlayingIcon();
  addHistory({ kind: 'played', name: f.name, id: f.id, link: useLink, is_video: true });

  // Resolve the playlist first (behind the spinner); the helpers take several
  // seconds to answer, and starting the video only afterwards keeps it from
  // giving up while the stream is still being arranged.
  let prep = null;
  try {
    const resp = await fetch(mediaUrl(f, '/api/prepare', useLink));
    prep = await resp.json();
  } catch (err) { /* fall through to playback attempt */ }
  if (attempt !== playAttempt) return;
  if (prep && !prep.ok) { playPreview(f, attempt, useLink, prep.error); return; }

  playerMsg.textContent = '';
  playStream(f, attempt, useLink);
}

function playStream(f, attempt, link) {
  const url = mediaUrl(f, '/api/hls', link);
  if (window.Hls && window.Hls.isSupported()) {
    if (hls) hls.destroy();
    hls = new window.Hls({ lowLatencyMode: false });
    hls.loadSource(url);
    hls.attachMedia(playerVideo);
    hls.on(window.Hls.Events.MANIFEST_PARSED, () => { playerVideo.play().catch(() => {}); });
    hls.on(window.Hls.Events.ERROR, (e, data) => {
      if (attempt !== playAttempt) return;
      if (data && data.fatal) {
        showSpinner(false);
        playerMsg.textContent = 'This video could not be played. Try Download instead.';
      }
    });
  } else {
    playerVideo.src = url;
    playerVideo.play().catch(() => {});
  }
}

function playPreview(f, attempt, link, reason) {
  playerMsg.textContent = (reason ? reason + ' ' : '') +
    'Showing TeraBox\u2019s 30-second preview.';
  playerTag.textContent = 'Preview';
  playerTag.classList.remove('hidden');
  playStream(f, attempt, link);
}

function closePlayer() {
  tearDownPlayer();
  player.classList.add('hidden');
  playerMsg.textContent = '';
}

// video events -> UI
playerVideo.addEventListener('loadeddata', () => { showSpinner(false); setPlayingIcon(); });
playerVideo.addEventListener('waiting', () => showSpinner(true));
playerVideo.addEventListener('playing', () => { showSpinner(false); setPlayingIcon(); });
playerVideo.addEventListener('pause', setPlayingIcon);
playerVideo.addEventListener('play', setPlayingIcon);
playerVideo.addEventListener('ended', setPlayingIcon);
playerVideo.addEventListener('timeupdate', () => {
  if (!seekEl.dragging) seekEl.value = playerVideo.currentTime;
  timeEl.textContent = fmtTime(playerVideo.currentTime) + ' / ' + fmtTime(playerVideo.duration);
});
playerVideo.addEventListener('loadedmetadata', () => {
  seekEl.max = playerVideo.duration || 0;
  timeEl.textContent = fmtTime(playerVideo.currentTime) + ' / ' + fmtTime(playerVideo.duration);
});

// controls
seekEl.addEventListener('input', () => { seekEl.dragging = true; });
seekEl.addEventListener('change', () => {
  playerVideo.currentTime = Number(seekEl.value);
  seekEl.dragging = false;
});
document.getElementById('btnPlay').addEventListener('click', () => {
  if (playerVideo.paused) playerVideo.play().catch(() => {}); else playerVideo.pause();
});
document.getElementById('btnBack').addEventListener('click', () => {
  playerVideo.currentTime = Math.max(0, playerVideo.currentTime - 10);
});
document.getElementById('btnFwd').addEventListener('click', () => {
  playerVideo.currentTime = Math.min(playerVideo.duration || 0, playerVideo.currentTime + 10);
});
volEl.addEventListener('input', () => { playerVideo.volume = Number(volEl.value); playerVideo.muted = false; syncMute(); });
document.getElementById('btnMute').addEventListener('click', () => {
  playerVideo.muted = !playerVideo.muted;
  syncMute();
});
function syncMute() {
  document.getElementById('btnMute').innerHTML = playerVideo.muted ? '&#128263;' : '&#128266;';
}
rateEl.addEventListener('change', () => { playerVideo.playbackRate = Number(rateEl.value); });
document.getElementById('btnFs').addEventListener('click', () => {
  const box = player.querySelector('.box');
  if (document.fullscreenElement) document.exitFullscreen();
  else (box.requestFullscreen ? box.requestFullscreen() : playerVideo.webkitEnterFullscreen?.());
});
document.getElementById('playerClose').addEventListener('click', closePlayer);
player.addEventListener('click', e => { if (e.target === player) closePlayer(); });

document.addEventListener('keydown', e => {
  if (player.classList.contains('hidden')) return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.key === 'Escape') closePlayer();
  else if (e.key === ' ' || e.key === 'k') { e.preventDefault();
    if (playerVideo.paused) playerVideo.play().catch(() => {}); else playerVideo.pause(); }
  else if (e.key === 'ArrowLeft') playerVideo.currentTime = Math.max(0, playerVideo.currentTime - 10);
  else if (e.key === 'ArrowRight') playerVideo.currentTime = Math.min(playerVideo.duration || 0, playerVideo.currentTime + 10);
  else if (e.key === 'm') playerVideo.muted = !playerVideo.muted;
  else if (e.key === 'f') document.getElementById('btnFs').click();
});

// --- Fetch ----------------------------------------------------------------

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
    setStatus(data.note
      ? 'Files ready. (TeraBox listing was busy, so these came from the helper service.)'
      : 'Share resolved.');
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
histClearBtn.addEventListener('click', () => {
  saveHistory([]);
  renderHistory();
});

renderHistory();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "12000"))
    app.run(host="0.0.0.0", port=port)
