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
<title>TeraBox Downloader - Tech Arslan Software Solutions</title>
<style>
  :root{
    --bg:#0a0f1c; --bg-2:#0e1526; --surface:#131c31; --surface-2:#1a2540;
    --border:#25324f; --border-2:#31415f;
    --text:#e9eefb; --muted:#93a4c4; --muted-2:#6b7da0;
    --primary:#6366f1; --primary-2:#8b5cf6; --accent:#22d3ee;
    --success:#22c55e; --danger:#ef4444; --warn:#f59e0b;
    --radius:14px; --radius-sm:10px;
    --shadow:0 18px 40px -18px rgba(0,0,0,.75);
  }
  *{box-sizing:border-box;}
  html,body{height:100%;}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Helvetica,Arial,sans-serif;
       color:var(--text);background:
         radial-gradient(1200px 600px at 10% -10%,rgba(99,102,241,.18),transparent 60%),
         radial-gradient(900px 500px at 100% 0%,rgba(139,92,246,.14),transparent 55%),
         var(--bg);
       display:flex;flex-direction:column;align-items:center;padding:26px 16px 60px;-webkit-font-smoothing:antialiased;}
  .app{width:100%;max-width:760px;}

  /* topbar */
  .topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px;}
  .brand{display:flex;align-items:center;gap:13px;min-width:0;}
  .logo{width:46px;height:46px;flex-shrink:0;border-radius:14px;display:flex;align-items:center;justify-content:center;
        color:#fff;position:relative;overflow:hidden;
        background:linear-gradient(140deg,#6366f1 0%,#8b5cf6 55%,#22d3ee 130%);
        box-shadow:0 12px 26px -12px rgba(99,102,241,.95),inset 0 1px 0 rgba(255,255,255,.28);}
  .logo svg{width:26px;height:26px;display:block;}
  .logo::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(255,255,255,.18),transparent 60%);}
  .brandname{font-size:1.16rem;font-weight:800;letter-spacing:-.3px;line-height:1.15;
        background:linear-gradient(92deg,#ffffff,#c7d2fe 60%,#a5f3fc);
        -webkit-background-clip:text;background-clip:text;color:transparent;}
  .brandtag{display:flex;align-items:center;gap:6px;color:var(--muted-2);font-size:.72rem;font-weight:600;
        letter-spacing:1.1px;text-transform:uppercase;margin-top:3px;}
  .brandtag::before{content:"";width:14px;height:2px;border-radius:2px;background:linear-gradient(90deg,var(--primary),var(--accent));}
  .brandside{display:flex;align-items:center;gap:10px;}
  .prod-pill{display:inline-flex;align-items:center;gap:7px;font-size:.72rem;font-weight:700;color:#dbe3ff;
        border:1px solid rgba(99,102,241,.42);background:rgba(99,102,241,.13);border-radius:999px;padding:6px 13px;
        white-space:nowrap;}
  .prod-pill .dot{width:6px;height:6px;border-radius:50%;background:var(--success);box-shadow:0 0 8px var(--success);}
  .icon-btn{background:var(--surface);border:1px solid var(--border);color:var(--muted);width:38px;height:38px;
            border-radius:11px;cursor:pointer;font-size:1rem;font-weight:700;display:flex;align-items:center;
            justify-content:center;transition:.15s;}
  .icon-btn:hover{color:var(--text);border-color:var(--border-2);background:var(--surface-2);}

  /* footer */
  .sitefoot{margin-top:30px;width:100%;max-width:760px;border:1px solid var(--border);border-radius:var(--radius);
        background:linear-gradient(180deg,var(--surface),var(--bg-2));box-shadow:var(--shadow);overflow:hidden;}
  .foot-top{display:flex;flex-wrap:wrap;gap:22px;justify-content:space-between;padding:20px 20px 18px;}
  .foot-brand{display:flex;align-items:center;gap:12px;min-width:210px;}
  .foot-brand .logo{width:40px;height:40px;border-radius:12px;}
  .foot-brand .logo svg{width:22px;height:22px;}
  .foot-name{font-size:.96rem;font-weight:800;letter-spacing:-.2px;color:var(--text);}
  .foot-tag{color:var(--muted-2);font-size:.74rem;margin-top:2px;letter-spacing:.4px;}
  .foot-meta{display:flex;flex-direction:column;gap:7px;font-size:.76rem;color:var(--muted);min-width:180px;}
  .foot-meta .k{color:var(--muted-2);font-size:.66rem;text-transform:uppercase;letter-spacing:1px;font-weight:700;}
  .foot-meta .v{color:var(--text);font-weight:600;}
  .foot-badges{display:flex;flex-wrap:wrap;gap:7px;align-items:flex-start;max-width:260px;}
  .fchip{font-size:.68rem;font-weight:600;color:var(--muted);border:1px solid var(--border);background:var(--surface-2);
        border-radius:999px;padding:5px 11px;white-space:nowrap;}
  .foot-bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;
        padding:12px 20px;border-top:1px solid var(--border);background:rgba(10,15,28,.55);}
  .foot-bar .copy{color:var(--muted-2);font-size:.72rem;}
  .foot-bar .made{color:var(--muted-2);font-size:.72rem;display:flex;align-items:center;gap:6px;}
  .foot-bar .made strong{color:var(--muted);font-weight:700;}

  .card{background:linear-gradient(180deg,var(--surface),var(--bg-2));border:1px solid var(--border);
        border-radius:var(--radius);padding:18px;box-shadow:var(--shadow);margin-bottom:16px;}
  label{display:block;font-size:.78rem;font-weight:600;color:var(--muted);margin-bottom:8px;letter-spacing:.2px;}
  .input-wrap{display:flex;gap:8px;}
  input[type=text]{flex:1;min-width:0;padding:12px 13px;border:1px solid var(--border);border-radius:var(--radius-sm);
       font-size:.92rem;background:var(--bg-2);color:var(--text);transition:.15s;}
  input[type=text]::placeholder{color:var(--muted-2);}
  input[type=text]:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgba(99,102,241,.22);}
  .row{display:flex;gap:10px;margin-top:12px;}
  button{font-family:inherit;}
  .btn{border:1px solid transparent;border-radius:var(--radius-sm);padding:12px 18px;font-size:.88rem;font-weight:600;
       cursor:pointer;display:inline-flex;align-items:center;gap:8px;justify-content:center;transition:.15s;white-space:nowrap;}
  .btn svg{width:16px;height:16px;fill:currentColor;flex-shrink:0;}
  .primary{background:linear-gradient(135deg,var(--primary),var(--primary-2));color:#fff;flex:1;
           box-shadow:0 12px 26px -14px rgba(99,102,241,.95);}
  .primary:hover{filter:brightness(1.08);}
  .primary:disabled{opacity:.55;cursor:default;filter:none;box-shadow:none;}
  .ghost{background:var(--surface-2);color:var(--text);border-color:var(--border);}
  .ghost:hover{border-color:var(--border-2);background:#1f2c4a;}
  .sm{padding:9px 13px;font-size:.8rem;border-radius:9px;}
  .status{font-size:.83rem;color:var(--muted);min-height:18px;margin-top:11px;}
  .status.error{color:var(--danger);}

  .note{background:linear-gradient(180deg,rgba(34,211,238,.09),rgba(34,211,238,.03));
        border:1px solid rgba(34,211,238,.28);color:#b7e9f5;border-radius:var(--radius);
        padding:13px 15px;font-size:.79rem;line-height:1.5;margin-bottom:16px;}
  .note code{background:rgba(34,211,238,.14);color:#d9f6ff;padding:1px 6px;border-radius:5px;font-size:.76rem;}

  /* tabs */
  .tabs{display:flex;gap:6px;background:var(--surface);border:1px solid var(--border);padding:6px;
        border-radius:var(--radius);margin-bottom:16px;box-shadow:var(--shadow);}
  .tab{flex:1;border:0;background:transparent;color:var(--muted);padding:11px 12px;border-radius:9px;
       font-size:.86rem;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;transition:.15s;}
  .tab:hover{color:var(--text);}
  .tab.active{background:linear-gradient(135deg,var(--primary),var(--primary-2));color:#fff;
              box-shadow:0 10px 22px -14px rgba(99,102,241,.95);}
  .count{background:rgba(255,255,255,.16);color:inherit;border-radius:999px;padding:1px 8px;font-size:.72rem;font-weight:700;}
  .tab:not(.active) .count{background:var(--surface-2);color:var(--muted);}

  .pane-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap;}
  .filters{display:flex;gap:6px;}
  .chip{background:var(--surface);border:1px solid var(--border);color:var(--muted);padding:7px 13px;
        border-radius:999px;font-size:.78rem;font-weight:600;cursor:pointer;transition:.15s;}
  .chip:hover{color:var(--text);}
  .chip.active{background:rgba(99,102,241,.18);border-color:var(--primary);color:#c7c9ff;}

  /* lists */
  .list{display:flex;flex-direction:column;gap:9px;}
  .item{display:flex;align-items:center;gap:13px;padding:12px 13px;background:linear-gradient(180deg,var(--surface),var(--bg-2));
        border:1px solid var(--border);border-radius:var(--radius-sm);transition:.15s;}
  .item:hover{border-color:var(--border-2);}
  .thumb{width:44px;height:44px;flex-shrink:0;border-radius:11px;display:flex;align-items:center;justify-content:center;
         background:linear-gradient(135deg,rgba(99,102,241,.25),rgba(139,92,246,.2));color:#c9cbff;}
  .thumb svg{width:20px;height:20px;fill:currentColor;}
  .thumb.played{background:linear-gradient(135deg,rgba(34,197,94,.28),rgba(34,197,94,.12));color:#86efac;}
  .thumb.downloaded{background:linear-gradient(135deg,rgba(59,130,246,.28),rgba(34,211,238,.16));color:#93c5fd;}
  .meta{min-width:0;flex:1;}
  .name{font-size:.88rem;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .sub2{font-size:.74rem;color:var(--muted);margin-top:3px;display:flex;align-items:center;gap:7px;flex-wrap:wrap;}
  .dot{width:3px;height:3px;border-radius:50%;background:var(--muted-2);}
  .actions{display:flex;gap:7px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end;}
  .act{border:1px solid var(--border);background:var(--surface-2);color:var(--text);padding:8px 13px;border-radius:9px;
       font-size:.78rem;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:.15s;}
  .act svg{width:14px;height:14px;fill:currentColor;}
  .act:hover{border-color:var(--border-2);background:#22304f;}
  .act.play{background:rgba(34,197,94,.16);border-color:rgba(34,197,94,.45);color:#86efac;}
  .act.play:hover{background:rgba(34,197,94,.26);}
  .act.dl{background:linear-gradient(135deg,var(--primary),var(--primary-2));border-color:transparent;color:#fff;}
  .act.dl:hover{filter:brightness(1.08);}
  .act.icon{padding:8px 10px;}
  .empty{color:var(--muted-2);font-size:.83rem;padding:26px 12px;text-align:center;border:1px dashed var(--border);
         border-radius:var(--radius-sm);}

  /* player: a full page, so the video gets the whole screen */
  #player{position:fixed;inset:0;background:#04070d;display:flex;align-items:stretch;justify-content:center;
          padding:0;z-index:60;}
  #player.full{background:#000;}
  .pbox{background:#080d18;width:100%;height:100%;overflow-x:hidden;overflow-y:auto;display:flex;flex-direction:column;}
  .pbox.wide .phead,.pbox.wide .nextbar{display:none;}
  .phead{display:flex;align-items:center;gap:12px;padding:12px 15px;border-bottom:1px solid var(--border);
         background:linear-gradient(180deg,#0d1526,#0a1120);flex-shrink:0;}
  .ptitle{flex:1;min-width:0;display:flex;align-items:center;gap:10px;}
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
<div class="app">
  <div class="topbar">
    <div class="brand">
      <div class="logo">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9A1.5 1.5 0 0 1 18.5 16H13l-3.4 3.1A.7.7 0 0 1 8.4 18.6V16H5.5A1.5 1.5 0 0 1 4 14.5v-9Zm6 2.6v4.8l4.2-2.4L10 8.1Z"/></svg>
      </div>
      <div>
        <div class="brandname">TeraBox Downloader</div>
        <div class="brandtag">Tech Arslan Software Solutions</div>
      </div>
    </div>
    <div class="brandside">
      <span class="prod-pill"><span class="dot"></span>Fast &amp; Free</span>
      <button class="icon-btn" id="helpBtn" title="Keyboard shortcuts">?</button>
    </div>
  </div>

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
</div>

<div id="player" class="hidden">
  <div class="pbox" id="pbox">
    <div class="phead">
      <div class="ptitle">
        <span class="t" id="playerTitle">Video</span>
        <span class="tag hidden" id="playerTag"></span>
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
        <div class="foot-tag">TeraBox Downloader</div>
      </div>
    </div>
    <div class="foot-meta">
      <span class="k">Developed by</span>
      <span class="v">Tech Arslan Software Solutions</span>
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
    <span class="made">Made with care by <strong>Tech Arslan</strong></span>
  </div>
</footer>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<script>
const ICON_PLAY = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';
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

// --- Tabs ------------------------------------------------------------------
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
function switchTab(name){
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
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
    if (it.is_video) {
      const play = document.createElement('button');
      play.className = 'act play'; play.innerHTML = '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>Play';
      play.addEventListener('click', () => openPlayer(f, it.link));
      actions.append(play);
    }
    const dl = document.createElement('button');
    dl.className = 'act dl'; dl.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 3v10.6l3.3-3.3 1.4 1.4L12 17.4l-4.7-4.7 1.4-1.4L12 13.6V3zM5 19h14v2H5z"/></svg>Download';
    dl.addEventListener('click', () => startDownload(f, dl, it.link));
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

renderHistory();
$('year').textContent = new Date().getFullYear();
</script>
</body>
</html>

"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "12000"))
    app.run(host="0.0.0.0", port=port)
