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
- **`deploy.aptPackages` needs the `"..."` entry.** Railpack replaces its
  generated runtime package list with whatever you write. Writing
  `["ffmpeg"]` drops everything Railpack installed at runtime, and `railpack
  prepare` says so: `Add '...' to 'deploy.aptPackages' to retain Railpack
  packages`. Keep `["...", "ffmpeg"]` in both `buildAptPackages` and
  `deploy.aptPackages`.
- **IPv6 private network.** Railway health-checks the container over its
  private network, which is IPv6. gunicorn bound to `0.0.0.0` answers IPv4
  only, so the check gets "connection refused" and the deploy is reported as
  crashed while the process is healthy. `start.sh` binds `[::]` (dual-stack)
  when the kernel supports it, falling back to `0.0.0.0`. Verified: with the
  IPv4-only bind, `curl http://[::1]:$PORT/` inside the container is refused;
  with the dual-stack bind it returns 200.
- **Health check on `/` is expensive.** `/` renders the ~88 KB home page.
  `/healthz` answers `{"ok": true}` and is what `railway.json` /
  `render.yaml` point at.
- **`requests` is a real dependency.** `terabox_app.py` imports it as the
  fallback when `curl_cffi` is missing, so it is in `requirements.txt`; a
  Railpack/Nixpacks build that installs only the listed packages needs it
  there.
- **The failing deploy is not this app.** The Railway project is
  `remarkable-tenderness` and the deploys fail after a ~16 s build and ~7 s
  runtime. The same tree builds and serves 200 on the Dockerfile, Railpack and
  Nixpacks paths locally, under a read-only rootfs, and under a 512 MB cap
  (peak ~75 MB). The public host
  `remarkable-tenderness-production.up.railway.app` answers 200 but serves a
  **different app** (title `ZIANTGYM`), so the service being health-checked is
  not this repository. Reading Railway's runtime logs needs a dashboard login
  or a project token, neither of which is available here; that is the one
  piece of evidence still missing.
- **Alternative platforms are wired up.** `render.yaml` (Docker runtime) and
  `fly.toml` (Dockerfile, `/healthz` check, 1 GB) deploy the same repo, and
  `Procfile` covers Heroku-style hosts. All three point at `bash ./start.sh`.
- **Free hosting, verified against the providers' own docs.** Northflank's
  Developer Sandbox is free *in usage* and its pricing page says "Always on, no
  sleeping", but its billing docs require a payment method for every user
  before any resource can be created, so a card is unavoidable there. Render's
  Free plan is the one that needs no payment details at all. Render's Free plan grants 750 instance hours a month, and
  the longest month is 744 hours, so one always-on service fits - but it spins
  down after 15 minutes idle, so pair it with a free UptimeRobot check on
  `/healthz` every 5 minutes. Fly.io and Railway are *not* free any more:
  Fly gives $1 of credit for 7 days, Railway needs a card. Hugging Face moved
  Docker Spaces behind a paid plan (only Static Spaces stayed free), and Koyeb's
  free tier is only 5 hours a month - both are traps, do not recommend them.
- **Vercel and Netlify cannot host this app.** Vercel Functions cap at 300 s on
  Hobby with a 4.5 MB body limit and no long-lived process, so the in-memory HLS
  playlists and TeraBox helper cache break; its Container runtime takes a
  Dockerfile but keeps the function limits, and the Terms of Service limit Hobby
  to "personal or non-commercial use" and forbid serving third parties. Netlify
  has no Dockerfile/container support at all and caps functions at 60 s
  synchronous. Do not offer either as a host.
- **The `'$PORT' is not a valid port number` crash is fixed.** Railway's start
  command field shows a `$PORT` placeholder, and a command that keeps the
  unquoted form reaches gunicorn with the literal text `$PORT`; `${PORT:-12000}`
  does not rescue that, because a non-empty literal is not an empty variable.
  `start.sh` and `run.sh` now reject any non-numeric `PORT` and fall back to
  12000. Verified in the built image with `-e 'PORT=$PORT'` (boots, `/healthz`
  200) and with `-e PORT=8080` (200).
- **No managed host can be deployed from here.** Railway fails at runtime with
  no logs, and Fly.io, Render, Koyeb and Heroku all need an interactive login
  or an API token that this environment does not have (the browser has no
  session either). `tunnel.sh` is the fallback that needs no account: it starts
  the app if needed and opens a Cloudflare quick tunnel, printing a
  `https://*.trycloudflare.com` URL. That URL works end to end - home 200,
  `/healthz` 200, 156 platforms, a 1 MB MP4 download verifies as valid MP4.
  YouTube *downloads* answer 403 from a datacenter IP even locally, which is a
  YouTube-side block, not an app bug; direct-file and non-YouTube links work.

## Hosting facts (measured, not from marketing pages)

The user's original complaint was "link not working", and the app was never at
fault. Diagnose a dead host URL before touching code.

**A Render 404 means the service was never created.** `https://<name>.onrender.com`
answering 404 returns the exact same body as a hostname that never existed, so it
is not a crash and not a failed health check - there is nothing deployed. A
deploy that failed shows a Render error page instead. Two things stop creation:
the blueprint review screen was never submitted, or the account needs payment
details (the API answers 402).

**GitHub push is read-only from this environment.** `GITHUB_TOKEN` reports
`permissions: {push: true}` on the repo, but `git push` returns 403 and the
Contents API returns "Resource not accessible by integration".
`OH_GIT_CREDENTIAL_ACCESS_TOKEN_GITHUB` is rejected too ("Invalid username or
token"). So commits stay local and the remote `main` drifts behind - check
`git branch -r --contains HEAD` before claiming a change is on GitHub. Never
report a commit as deployed when it only exists locally.

**Free hosts, checked against each host's own docs:**

| Host | Verdict |
| --- | --- |
| alwaysdata | Free 0 EUR/month for life, no card field at signup, `wsgi` site type, 256 MB RAM, no Docker |
| Render | Free, no card, but stops a service after 15 idle minutes; 512 MB |
| Hugging Face Spaces | Compute Spaces need a paid plan; only static is free |
| Koyeb | Free allowance is gated behind a card |
| PythonAnywhere | Free account has restricted outbound access - fatal here |
| Vercel / Netlify | No long-lived process; the in-memory caches this app needs die |

**alwaysdata API specifics** (all from `https://api.alwaysdata.com/v1/site/doc/`):

- Auth is HTTP Basic with the token as the username and an empty password. The
  trailing colon is required: `curl --basic --user APIKEY:`.
- A site's `addresses` field is a **list of plain hostname strings**, not objects.
- A site has **no `name` field**; the label is `annotation`.
- `max_idle_time` defaults to **1800** seconds, after which the application is
  stopped. Raise it or the first visit after a quiet spell is a cold start.

`deploy_alwaysdata.py` and `deploy_alwaysdata.sh` implement the above. Signup is
the only step that cannot be scripted, because the verification mail goes to the
account owner.

## Conventions

- Keep it a single file; no build step.
- Never print secret values (e.g. `TERABOX_COOKIE`) to logs or the UI.
