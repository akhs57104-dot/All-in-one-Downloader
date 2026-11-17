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

  * a third-party resolver (iteraplay.com) that holds its own TeraBox account
    and returns the original file with Range support — full quality, but rate
    limited and it sees every link (TERABOX_FALLBACK=off disables it);
  * TeraBox's own /share/streaming transcode, which needs no cookie but is only
    a fixed ~30-second preview, so it is used for playback, never a download.

Run:  python3 terabox_app.py   (serves on PORT, default 12000)
"""

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
# Helper service fallback
# ---------------------------------------------------------------------------
#
# TeraBox now hands the bytes of a shared file only to a signed-in session, so an
# anonymous visitor can list a share but never download it. iteraplay.com is a
# third-party resolver holding its own TeraBox account; it returns the original
# file (not a transcode) with Range support, which makes both download and
# in-browser playback work. It is used only when TeraBox's own route is
# unavailable, it discloses each link to that service, and it is rate-limited
# (a few videos per six hours), so its answers are cached briefly. Set
# TERABOX_FALLBACK=off to disable it entirely.

ITERAPLAY_HOME = "https://iteraplay.com/"
ITERAPLAY_ENDPOINT = "https://iteraplay.com/api/download"

# surl -> (resolved_at, {filename: direct_url}); the helper's links expire in
# minutes, so this only spares a repeat click, it never outlives the link.
_FALLBACK_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_FALLBACK_TTL = 240

def _fallback_enabled() -> bool:
    return os.environ.get("TERABOX_FALLBACK", "on").strip().lower() not in ("off", "0", "no", "false")

def _fallback_files(surl: str) -> dict[str, str]:
    """Map `filename -> direct url` for one share via the helper service."""
    cached = _FALLBACK_CACHE.get(surl)
    if cached and (time.time() - cached[0]) < _FALLBACK_TTL:
        return cached[1]

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
        raise TeraBoxError(f"The helper service is not responding ({exc}).") from exc

    try:
        data = resp.json()
    except ValueError:
        raise TeraBoxError("The helper service sent an unreadable reply.")

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    if data.get("error") == "usage_limit" or resp.status_code == 429:
        limit = usage.get("limit")
        allowance = f" ({limit} resolutions per 6 hours)" if limit else ""
        raise TeraBoxError(
            f"The free helper route has used up its quota{allowance}. Try again "
            f"later, or set TERABOX_COOKIE to download directly, or "
            f"TERABOX_FALLBACK=off to disable the helper.")
    if resp.status_code >= 400 or data.get("status") != "success":
        note = data.get("message") or data.get("status") or resp.status_code
        raise TeraBoxError(
            f"The helper service could not open that link ({note}). It may be "
            f"private, removed, or password-protected.")

    links = {}
    for entry in data.get("list") or []:
        if not isinstance(entry, dict) or entry.get("is_dir"):
            continue
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("normal_dlink") or "").strip()
        if name and url:
            links[name] = url
    if not links:
        raise TeraBoxError("The helper service found no downloadable file behind that link.")

    _FALLBACK_CACHE[surl] = (time.time(), links)
    return links

def resolve_share_fallback(link: str) -> dict:
    """Resolve a share using the helper service alone (used when TeraBox
    listing is rate-limited)."""
    surl = extract_share_id(link)
    links = _fallback_files(surl)
    files = []
    for name, url in links.items():
        files.append({"id": name, "fs_id": None, "name": name, "size": 0,
                      "size_text": "", "is_video": _is_video(name),
                      "direct_url": url})
    return {"surl": surl, "source": "helper", "share_username": "", "files": files}


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

def _proxy_stream(upstream, *, as_attachment: bool, filename: str) -> Response:
    """Relay an upstream response, preserving Range/length semantics."""
    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
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

def _serve(link: str, fs_id: str, *, mode: str):
    """Download or stream one file.

    `mode` is one of:
      "download" — TeraBox dlink, then the helper service. Errors if neither
                   can serve the whole file; never a partial/preview stream.
      "stream"   — the same direct routes, sent inline for native playback.
      "hls"      — TeraBox's 30-second preview transcode, as a same-origin
                   M3U8 playlist, for playback when nothing else works.
    """
    if not link:
        return jsonify({"ok": False, "error": "Missing link."}), 400

    share = None
    chosen = None
    terabox_error = None
    helper_error = None
    if is_allowed_share_url(link) and not fs_id.startswith("name:"):
        try:
            share = resolve_share(link)
        except TeraBoxError as exc:
            terabox_error = exc
        else:
            chosen = next((f for f in share["files"]
                           if str(f["fs_id"]) == str(fs_id)), None) or share["files"][0]

    direct = mode in ("download", "stream")

    # 1) TeraBox's own dlink (only works with a signed-in cookie).
    if direct and share is not None and chosen is not None:
        try:
            session, cookie, dlink = _terabox_dlink(share, chosen)
        except TeraBoxError as exc:
            terabox_error = exc
        else:
            headers = {"User-Agent": BROWSER_UA, "Referer": share["referer"]}
            if request.headers.get("Range"):
                headers["Range"] = request.headers["Range"]
            if cookie:
                headers["Cookie"] = cookie
            upstream = session.get(dlink, headers=headers, stream=True, timeout=60)
            if upstream.status_code in (200, 206):
                return _proxy_stream(upstream, as_attachment=(mode == "download"),
                                     filename=chosen["name"])
            upstream.close()
            terabox_error = TeraBoxError(
                f"TeraBox returned status {upstream.status_code} for the file.")

    # 2) The helper service (no cookie, but a small quota).
    if direct and _fallback_enabled():
        try:
            surl = extract_share_id(link)
            links = _fallback_files(surl)
            wanted = fs_id[5:] if fs_id.startswith("name:") else None
            if wanted is None and chosen is not None:
                wanted = chosen["name"]
            url = links.get(wanted) if wanted else next(iter(links.values()))
            if url is None:
                raise TeraBoxError("That file was not offered by the helper service.")
            helper_headers = {"User-Agent": BROWSER_UA, "Referer": ITERAPLAY_HOME}
            if request.headers.get("Range"):
                helper_headers["Range"] = request.headers["Range"]
            upstream = _request_session().get(url, headers=helper_headers,
                                              stream=True, timeout=60)
            if upstream.status_code in (200, 206):
                return _proxy_stream(upstream, as_attachment=(mode == "download"),
                                     filename=wanted or "download")
            upstream.close()
            helper_error = TeraBoxError(
                f"The helper service returned status {upstream.status_code}.")
        except TeraBoxError as exc:
            helper_error = exc

    # 3) TeraBox's anonymous HLS transcode, for previewing only.
    #
    # Measured against several shares: TeraBox returns a fixed 30-second window
    # (three segments, then ENDLIST) regardless of file length, so it is never a
    # real download and is offered only when playback would otherwise fail.
    if mode == "hls" and share is not None and chosen is not None \
            and chosen.get("is_video"):
        try:
            segments, referer = _hls_playlist(share, chosen)
        except TeraBoxError as exc:
            terabox_error = exc
        else:
            token = secrets.token_urlsafe(12)
            _HLS_STORE[token] = (time.time(), segments, referer)
            playlist = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:15",
                        "#EXT-X-MEDIA-SEQUENCE:0"]
            for i in range(len(segments)):
                # Root-relative so the player uses whatever host it reached us
                # on (works behind the proxy) and never a hardcoded origin.
                playlist.append(f"#EXTINF:10,\n/api/hls/{token}/{i}.ts")
            playlist.append("#EXT-X-ENDLIST")
            return Response("\n".join(playlist) + "\n",
                            headers={"Content-Type": "application/vnd.apple.mpegurl",
                                     "Cache-Control": "no-store"})

    error = helper_error or terabox_error
    return jsonify({"ok": False, "error": str(error) if error else "Could not resolve that file."}), 400


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
    """Direct-route stream for native browser playback (an MP4)."""
    return _serve(request.args.get("link", ""), request.args.get("fs_id", ""),
                  mode="stream")


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
  .hidden { display:none; }
  .note { background:#eefaf0; border:1px solid #b7e4c1; color:#1f5f36; border-radius:10px;
          padding:12px 14px; font-size:.82rem; line-height:1.45; margin-bottom:16px; }
  .note code { background:#d6f0dd; padding:1px 5px; border-radius:4px; font-size:.8rem; }
  #player { position:fixed; inset:0; background:rgba(12,24,38,.82); display:flex; align-items:center;
            justify-content:center; padding:20px; z-index:50; }
  #player.hidden { display:none; }
  #player .box { background:#0b1520; border-radius:14px; width:min(960px,100%); overflow:hidden;
                 box-shadow:0 12px 40px rgba(0,0,0,.45); }
  #player .head { display:flex; align-items:center; justify-content:space-between; gap:12px;
                  padding:12px 16px; color:#eaf1f8; }
  #player .head .t { font-size:.9rem; font-weight:600; overflow:hidden; text-overflow:ellipsis;
                     white-space:nowrap; }
  #player .head button { background:#22384f; color:#fff; }
  #player video { width:100%; display:block; background:#000; max-height:78vh; }
  #player .pmsg { color:#c9d6e3; font-size:.85rem; padding:0 16px 16px; }
</style>
</head>
<body>
<div class="app">
  <h1>TeraBox Downloader</h1>
  <div class="sub">Paste a public TeraBox share link to list, play, and download its files.</div>

  <div id="note" class="note">
    No account needed: when TeraBox refuses the direct download, the app resolves
    the file through a helper service instead. That service is rate-limited
    (a few files per six hours) and sees the links you paste, so set
    <code>TERABOX_COOKIE</code> (see the README) for unlimited direct downloads,
    or <code>TERABOX_FALLBACK=off</code> to disable the helper. Playback falls
    back to TeraBox's own 30-second preview when nothing else is available.
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

<div id="player" class="hidden">
  <div class="box">
    <div class="head">
      <span class="t" id="playerTitle">Video</span>
      <button id="playerClose" class="ghost">Close</button>
    </div>
    <video id="playerVideo" controls autoplay playsinline></video>
    <div class="pmsg" id="playerMsg"></div>
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

async function startDownload(f, button) {
  // Preflight a 2-byte ranged GET: a real download answers with bytes, whereas
  // a refusal answers JSON — so the user sees a message instead of a JSON page.
  button.disabled = true;
  const url = streamUrl(f, '/api/download');
  try {
    const probe = await fetch(url, { headers: { Range: 'bytes=0-1' } });
    const type = probe.headers.get('Content-Type') || '';
    if (probe.ok && !type.includes('application/json')) {
      window.location.href = url;  // Content-Disposition: attachment keeps the page
      setStatus('Download started.');
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

let hls = null;
let playAttempt = 0;

function tearDownPlayer() {
  playAttempt++;
  if (hls) { hls.destroy(); hls = null; }
  playerVideo.pause();
  playerVideo.removeAttribute('src');
  playerVideo.load();
}

function streamUrl(f, endpoint) {
  return endpoint + '?link=' + encodeURIComponent(linkEl.value.trim()) +
         '&fs_id=' + encodeURIComponent(f.id) + '&t=' + Date.now();
}

function playHls(f, attempt) {
  const url = streamUrl(f, '/api/hls');
  playerMsg.textContent = 'Direct playback unavailable — showing a 30-second preview.';
  if (window.Hls && window.Hls.isSupported()) {
    if (hls) { hls.destroy(); }
    hls = new window.Hls({ lowLatencyMode: false });
    hls.loadSource(url);
    hls.attachMedia(playerVideo);
    hls.on(window.Hls.Events.MANIFEST_PARSED, () => playerVideo.play().catch(() => {}));
    hls.on(window.Hls.Events.ERROR, (e, data) => {
      if (attempt !== playAttempt) return;
      if (data && data.fatal) {
        playerMsg.textContent = 'This video could not be played. Try Download instead.';
      }
    });
  } else {
    playerVideo.src = url;
    playerVideo.play().catch(() => {});
  }
}

function openPlayer(f) {
  tearDownPlayer();
  const attempt = ++playAttempt;
  playerTitle.textContent = f.name;
  playerMsg.textContent = 'Loading...';
  player.classList.remove('hidden');

  // Try the direct MP4 first (fast, full quality); if the server cannot provide
  // it, fall back to the HLS transcode, which always works anonymously.
  playerVideo.addEventListener('error', function onErr() {
    playerVideo.removeEventListener('error', onErr);
    if (attempt !== playAttempt) return;
    playHls(f, attempt);
  });
  playerVideo.addEventListener('loadeddata', function onData() {
    if (attempt === playAttempt) playerMsg.textContent = '';
  });
  playerVideo.src = streamUrl(f, '/api/stream');
  playerVideo.play().catch(() => {});
  // Safety net: if nothing started shortly, switch to HLS.
  setTimeout(() => {
    if (attempt !== playAttempt) return;
    if (playerVideo.readyState < 2) playHls(f, attempt);
  }, 6000);
}

function closePlayer() {
  tearDownPlayer();
  player.classList.add('hidden');
  playerMsg.textContent = '';
}

document.getElementById('playerClose').addEventListener('click', closePlayer);
player.addEventListener('click', e => { if (e.target === player) closePlayer(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closePlayer(); });

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
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "12000"))
    app.run(host="0.0.0.0", port=port)
