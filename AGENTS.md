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

- `SOCIAL_PLATFORMS` in `terabox_app.py` lists ~156 sites as
  `(key, name, colour, badge, host_roots)`. It drives host detection and the UI
  picker; yt-dlp's generic extractor covers anything not listed. The client's
  `S_PLAT` badge map is built from `/api/social/platforms` at load time, so add a
  site by editing only the Python tuple.

- YouTube: **do not pin `player_client`**. Pinning a set (the old
  `_YT_CLIENTS = ("android", "ios", "web", "tv_embedded")`) dropped the format
  list to 1; letting yt-dlp choose returned 24+ for the same URL. The `web`
  client is bot-blocked from a datacenter IP, but the default client set already
  includes ones that work.
- Every extractor runs through curl_cffi Chrome impersonation
  (`_impersonate_target()`, cached). yt-dlp needs an `ImpersonateTarget`
  instance, not a string - a string trips an assertion in yt-dlp. This is what
  gets TikTok/Facebook/Dailymotion working without cookies.
- `_extract_info(url) -> (info, cookies)` runs yt-dlp and returns the info dict
  plus the extractor's cookie jar. The cookies matter: **TikTok signs its media
  URLs against yt-dlp's session cookies**, so they are threaded through
  resolve -> stream -> download or the CDN answers 403.
- Extraction occasionally fails transiently (empty format list from a CDN
  hiccup); `_is_transient()` + one 1.5s retry turns that into a success. On an
  "unsupported URL" the generic extractor is tried once (`force_generic_extractor`).
- When every extractor fails, `_html_media_fallback(url)` scrapes the page's
  HTML for media (`videoUrls`, `video_url`, `og:video`, `<video src>`,
  `contentUrl`). Pinterest keeps plain MP4 + m3u8 links in a `videoUrls` list
  even when its extractor's unauth API call 404s, so this is what makes those
  pins work. Only URLs with a **media extension** are trusted - a player *page*
  URL (Vimeo's `player.vimeo.com/video/<id>`) is HTML and must be dropped, or
  the download saves a web page.
- `_format_for_url(url, format_id)` returns a spec: `{"direct": url, ...}` for a
  single-file/audio-only format, `{"hls": True, "url": ..., ...}` for an m3u8
  playlist, or a video+audio pair to merge with ffmpeg.
- HLS playback: `_social_hls_playlist()` rewrites the manifest to same-origin
  `/api/social/hls/<token>/<n>.ts` segment URLs; `_SOCIAL_HLS` is a TTL cache
  (`_SOCIAL_HLS_TTL`) pruned by `_prune_hls()`.
- Instagram, Facebook, Reddit, Vimeo and X/Twitter need a signed-in session;
  supply a Netscape `cookies.txt` via `SOCIAL_COOKIES` (path or contents).
  yt-dlp reports several of these as "no video could be found", which is why
  that message names cookies too.
- **Threads cannot be downloaded without cookies.** `threads.com` and
  `threads.net` answer every request - browser or crawler UA alike - with the
  login page (`og:title` = "Threads • Log in"), and yt-dlp has no Threads
  extractor at all. `_PLATFORM_HINTS` gives it a specific message instead of the
  generic "unsupported site".
- **Test with real, live URLs.** Dead links (a guessed Dailymotion id, an old
  tweet, Pinterest ids scraped from a search page) fail for reasons that look
  like code bugs but are not. Public posts on the platforms that allow
  anonymous access are the ones to measure against.

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

## Deploying to Railway (why it crashed twice)

Railway reports a failed **build** and a failed **runtime** the same way: the
deployment says "crashed". Both real causes so far were build failures.

1. **A hardcoded port.** Railway injects `$PORT` and health-checks that exact
   port. `ENV PORT=12000` made the app listen where nothing was checked.
   Fix: bind to `$PORT` (`start.sh`).
2. **`nixpacks.toml` with `nixPkgs`.** Declaring `nixPkgs` replaces the whole
   setup phase Nixpacks generates for Python, so `python3` was never installed
   and the build died with `python: command not found`. Use `aptPkgs` for
   system packages instead; it leaves Python setup intact.

Other traps:

- **Non-root + mode `0600` files.** If a checkout leaves files owner-only, the
  platform's unprivileged uid cannot read `wsgi.py` and gunicorn dies with
  `PermissionError`. The Dockerfile does `chmod -R a+rX /app`; keep it.
- **`--no-control-socket` on old gunicorn.** gunicorn below 23 exits with
  `unrecognized arguments: --no-control-socket`. `start.sh` passes the flag
  only when `--help` lists it.
- **Three builders, three config files.** `railway.json` pins the Dockerfile.
  If the service is switched to Railpack (Railway's current default) or
  Nixpacks (older), `railpack.json` / `nixpacks.toml` must supply `ffmpeg`
  themselves; the Dockerfile is ignored. All three are kept working.

## Conventions

- Keep it a single file; no build step.
- Never print secret values (e.g. `TERABOX_COOKIE`) to logs or the UI.
