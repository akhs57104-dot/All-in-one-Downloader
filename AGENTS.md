# AGENTS.md

## Project

`terabox_app.py` — a single-file Flask web app (branded "Tech Arslan Software
Solutions") with two tools in one page: a **Social Downloader** (yt-dlp) for
YouTube/Instagram/Facebook/TikTok/X/Dailymotion and ~25 more sites, and a
**TeraBox** share downloader. `README.md` documents usage.

## Environment

- Bare Python 3.13, headless container. **No tkinter/display**, so a desktop GUI
  cannot run here; use a web app (the provided work hosts map to ports
  12000/12001).
- Dependencies: `flask`, `curl_cffi`, `yt-dlp` (`requirements.txt`). `ffmpeg`
  on `PATH` enables merged high-quality downloads; without it only single-file
  (progressive) formats are offered.
- The container can be recycled between sessions, which empties
  site-packages. If imports fail, re-run `pip install -r requirements.txt`
  (plus `requests`, which the app falls back to) and
  `sudo apt-get update && sudo apt-get install -y ffmpeg`.

## TeraBox API facts (measured)

- Listing works anonymously: `GET /api/shorturlinfo` (params `app_id=250528`,
  `web=1`, `channel=dubox`, `shorturl=1<surl>`, `root=1`) returns the share
  record (`shareid`, `uk`, `sign`, `timestamp`, `randsk`) and the file list.
- `GET /api/sharedownload` (params include `fid_list`, `primaryid`, `product=share`,
  `nozip=0`, `type=download`, and `extra={"sekey": <url-decoded randsk>}`) returns
  a short-lived direct `dlink`.
- **Anonymous download is blocked.** The `dlink` answers
  `403 {"error_code":31045,"error_msg":"user not exists"}` without a logged-in
  session. The newer `POST /share/download` answers
  `errno 400310 "need verify_v2"` (a CAPTCHA). Set `TERABOX_COOKIE` to a
  logged-in cookie to download.
- **Helper fallback (no cookie).** playterabox.com is tried first (token =
  `md5("T9do@SM1?xGn5" + timestamp + "/api/stream.php")`, POST
  `https://playterabox.com/api/fetch-video?token=..&t=..`, JSON `{"url": <share>}`).
  It returns, per file, a full-length `Range`-capable `download_link` (host
  `dl-worker.teraboxdl.site`) and a complete `stream_url` HLS playlist (hundreds
  of segments, `#EXT-X-ENDLIST`) — no quota. iteraplay.com is secondary and
  rate-limited (5 resolutions per 6 hours, `429 usage_limit`); a
  `TERABOX_FALLBACK_TOKEN` (its `login_token` cookie) raises that quota.
  **Important:** the helper's bytes are a transcoded MPEG-TS stream, not the
  original MP4 — the `.mp4` name lies. HLS playback still carries the full
  duration; a raw download should be named `.ts` and remuxed with
  `ffmpeg -i x.ts -c copy x.mp4`. Deep `Range` seeks on the raw link can also
  stall (it transcodes on demand), but the HLS route is fast and seekable.
- **TeraBox's own `/share/streaming` is a 30-second preview only.** With a
  valid `sign`/`timestamp` and `type=M3U8_AUTO_480` it returns an HLS playlist,
  but always exactly three segments then `#EXT-X-ENDLIST`, whatever the file
  length; `start_ply` slides the window rather than extending it. 720p
  (`M3U8_MP4_264_1080`) answers `errno 130`. Good for a preview, never a
  download.
- The `surl` is spelled differently by different endpoints: `/s/1abc` path form
  for the share page, `?surl=abc` (no leading `1`) for some endpoints, and
  `shorturl=1abc` (with the `1`) for `/api/shorturlinfo`.
- TeraBox's WAF can reject Python's TLS fingerprint; use `curl_cffi` with
  `impersonate="chrome124"`.
- A session cookie is bound to one TeraBox host and answers `errno -6` on others.
- Soft rate limit: `errno 105` / `460030` after a burst; wait a minute.

## Social downloader facts (measured)

- `_extract_info(url) -> (info, cookies)` runs yt-dlp and returns the info dict
  plus the extractor's cookie jar. The cookies matter: **TikTok signs its media
  URLs against yt-dlp's session cookies**, so they are threaded through
  resolve -> stream -> download or the CDN answers 403.
- YouTube from a datacenter IP needs the `android` player client (see
  `_YT_CLIENTS`); the `web` client is bot-blocked. Progressive format `18`
  (360p MP4) streams and downloads cleanly.
- `_format_for_url(url, format_id)` returns a spec: `{"direct": url, ...}` for a
  single-file/audio-only format, `{"hls": True, "url": ..., ...}` for an m3u8
  playlist, or a video+audio pair to merge with ffmpeg.
- HLS playback: `_social_hls_playlist()` rewrites the manifest to same-origin
  `/api/social/hls/<token>/<n>.ts` segment URLs; `_SOCIAL_HLS` is a TTL cache
  (`_SOCIAL_HLS_TTL`) pruned by `_prune_hls()`.
- Extraction occasionally fails transiently (empty format list from a CDN
  hiccup); `_is_transient()` + one 1.5s retry turns that into a success.
- Instagram, Facebook, Vimeo and age-gated content need a signed-in session;
  supply a Netscape `cookies.txt` via `SOCIAL_COOKIES` (path or contents).

## Routes

- `GET /` — the single-page UI.
- `GET /api/social/platforms` — the supported-site list for the UI chips.
- `POST /api/social/resolve` — extract metadata + formats (yt-dlp).
- `GET /api/social/stream` — stream one format for playback (Range-capable).
- `GET /api/social/download` — save one format as a file (ffmpeg-merged if needed).
- `GET /api/social/hls/<token>/<n>.ts` — proxy one HLS segment.
- `POST /api/resolve` — list a share's files (TeraBox, else helper).
- `GET /api/prepare` — warm a file's helper entry so the next `/api/hls` is
  instant (returns JSON).
- `GET /api/hls` — same-origin M3U8 playlist (helper's full-length one first,
  TeraBox's 30-second preview as a last resort). `GET /api/stream` is an alias.
- `GET /api/hls/<token>/<n>.ts` — proxies one playlist segment.
- `GET /api/download` — the whole file as an attachment (original MP4 with a
  cookie; otherwise the helper's `.ts` transcode).

## Conventions

- Keep it a single file; no build step.
- Never print secret values (e.g. `TERABOX_COOKIE`) to logs or the UI.
