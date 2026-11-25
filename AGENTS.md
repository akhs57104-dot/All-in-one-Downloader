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
  - The container can be recycled between sessions, which empties site-packages
    and drops anything installed outside `bin/`. If imports fail, re-run
    `pip install -r requirements.txt`. **There is no root here**, so
    `apt-get install ffmpeg` fails with a dpkg lock error: run
    `tools/fetch-ffmpeg.sh` instead, which puts a static ffmpeg and ffprobe in
    `bin/`. `_ffmpeg_path()` checks `bin/ffmpeg` before `PATH`, and `bin/` is
    gitignored (153 MB for both binaries) because that script rebuilds it.
    `run.sh` does both steps itself on start, so a recycled container recovers
    without anyone noticing.

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
- **Known-good link for smoke tests** (public, resolves to one 118.86 MB
  `Ttsunade.zip`): `https://terabox.com/s/1DcGWQPuMVDgkXrFhP7AlcQ`. It carries no
  video, so it exercises resolve, `/api/download` and the "no stream" answer from
  `/api/hls` but not playback. Share links expire: when this one stops resolving,
  find a fresh one with GitHub code search
  (`https://api.github.com/search/code?q=%22terabox.com%2Fs%2F%22+in:file`) and
  grep the raw file for `terabox.*\.(com|app)/(s|sharing)/[A-Za-z0-9_-]{8,}`. Most
  hits are placeholders like `1abcdEFGH123` and must be skipped.

## Social downloader facts (measured)

- `SOCIAL_PLATFORMS` in `terabox_app.py` lists ~156 sites as
  `(key, name, colour, badge, host_roots)`. It drives host detection and the UI
  picker; yt-dlp's generic extractor covers anything not listed. The client's
  `S_PLAT` badge map is built from `/api/social/platforms` at load time, so add a
  site by editing only the Python tuple.

- YouTube: **do not pin `player_client`**. Pinning a set (the old
  `_YT_CLIENTS = ("android", "ios", "web", "tv_embedded")`) dropped the format
  list to 1; letting yt-dlp choose returned 24+ for the same URL.
- **YouTube does not download from this host at all** (measured 2026-11-25).
  Metadata extraction works - 44 formats come back - but every byte-serving path
  is refused, for every video, not just one. Progressive media gives
  `HTTP Error 403: Forbidden` with an empty body; HLS manifests return 200 while
  every segment inside them is 403; `b[height<=360]` reports "Requested format is
  not available".
  Everything that was tried, all still refused:
  13 player clients (`web`, `web_safari`, `web_creator`, `mweb`, `tv`,
  `tv_embedded`, `tv_simply`, `ios`, `ipad`, `android`, `android_vr`,
  `android_music`, `ios_music`); installing `yt-dlp-ejs` and enabling the node
  runtime so the JS signature challenges actually get solved (the URL then
  carries a valid `sig`/`lsig` and still 403s); curl_cffi TLS impersonation on
  the media URL; and rewriting or dropping the URL's `ip` parameter.
  The decisive test: the same request through an open proxy changes the error
  from `403` to "Sign in to confirm you're not a bot". So the refusal is bound to
  the egress address, not to the extractor, the cookies or the account.
  Control videos `jNQXAC9IVRw`, `dQw4w9WgXcQ` and `aqz-KE-bpKQ` fail identically.
  SoundCloud (a 4.2 MB m4a) and the TeraBox resolve/download paths work from this
  same host, so the app itself is fine.
- The container has **no IPv6 at all** (`/proc/net/if_inet6` empty, AF_INET6
  connect raises OSError) and a private IPv4 `10.2.49.157`, yet YouTube's media
  URLs carry an IPv6 `ip=fda3:...` parameter. Do not expect rewrites of that
  parameter to help - they were measured and they do not.
- yt-dlp **warns "No supported JavaScript runtime could be found"** unless told
  about one: deno is its only default and this host has node instead. That
  warning silently drops formats. `_js_runtime_opts()` now passes
  `js_runtimes: {"node": {}}` when node exists, and falls back to
  `remote_components: {"ejs:github"}` when the `yt-dlp-ejs` package is missing
  (`requirements.txt` now pins it). This is a real config fix even though it
  cannot beat the address block on its own.
- Public fallbacks were checked and are all unavailable from here: six Invidious
  instances (401/403/530/connection refused), five Piped instances (one returns
  metadata but no `proxyUrl`, rest 502/404), five cobalt instances (JWT required,
  Cloudflare 1033, timeouts).
- **Downloads walk a chain of routes, not one.** `_STRATEGIES` is shared by
  extraction and download: `default`, `no-impersonate`, `alt-clients`,
  `android-client`, `hls-only`, `generic`. `_download_to_file()` tries each route
  and, inside a route, loosens the format selector (`bv*+ba/b` -> `b` -> none);
  a route that refuses the bytes hands over to the next one. This matters because
  a source can return full metadata and still 403 the media - measured on YouTube,
  where all six routes were walked before failing.
  Each route was verified to produce a file on its own by running the chain with
  only that route (`_download_playlist_item(url, dir, strategies=(route,))`):
  `no-impersonate`, `alt-clients`, `android-client`, `hls-only` and `generic` all
  returned the same 4214 KB SoundCloud track, so the fallbacks are real and not
  decorative.
- The response says which route served the file: `X-Route` (exposed via
  `Access-Control-Expose-Headers`) is set **only when a fallback produced it**, so
  the normal first-route download stays quiet. A failure returns the walked routes
  as `{"routes": [{"source", "label", "reason"}]}`, deduped to one entry per route
  by `_attempts_public()`, and the page prints them as "(tried: ...)".
  Per-request state lives in `threading.local()` (`_ROUTE_LOG`, `_ROUTE_WIN`) because
  gunicorn runs 8 threads; a plain global interleaved one download's attempts into
  another's message. Note `threading.local()` takes attribute assignment, not
  `.set()` - that mistake raised AttributeError on every download until caught.
- The single-download button uses `fetch`, not a hidden iframe. An iframe renders a
  JSON error body invisibly, so a refusal looked like nothing happening; the fetch
  path can read the JSON and print the reason plus the routes tried.
- `_friendly_download_error()` names that hosting block *before* the cookie
  case, because yt-dlp's own text ("403", "Requested format is not available")
  otherwise reaches the user verbatim - the playlist path used to show
  `HTTP Error 403: Forbidden` and nothing else.
- `SOCIAL_PROXY` (env) sets yt-dlp's `proxy` for every strategy. A loopback
  value is ignored on purpose (`_is_loopback_proxy`): it cannot change the
  egress address, so it would only make yt-dlp talk to itself and time out.
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

- **Source failover (`_EXTRACT_STRATEGIES`).** `_extract_info()` walks an ordered
  chain - `default` -> `no-impersonate` -> `alt-clients` -> `generic` ->
  `html-scrape` - and returns the first strategy that yields media. Each
  strategy is a different *kind* of source, not a retry of the same one: a bot
  check wants a browser fingerprint, a stale player client wants a different
  one, a broken extractor wants the generic one. The winning name lands in
  `info["_source"]` and the refusals in `info["_attempts"]`, which
  `social_extract()` exposes as `source` / `attempts`; the UI shows a "Backup
  source" badge when the chain had to shift. `_download_to_file()` does the same
  for selectors, and `/api/social/download` retries a stale CDN link through
  `_failover_download()` before reporting an error.
- **YouTube is IP-blocked on this host.** Every player client (`android_vr`,
  `ios`, `tv`, `web_safari`, `mweb`, `android`, `tv_embedded`) returns either
  `HTTP 403` on the media URL or "Sign in to confirm you're not a bot". Metadata
  extraction still works, so playlists list fine, but downloads fail. That is
  the datacenter IP, not the code; it needs a cookies file or a residential
  proxy to change.
- **Playlists** are detected by `_looks_like_playlist()` (mirrored in JS as
  `looksLikePlaylist()`) for YouTube (`?list=`), SoundCloud `/sets/`, Vimeo
  `/album/`, `/channels/`, `/showcase/`, and Dailymotion `/playlist/`.
  `_playlist_entries()` reads them flat (`extract_flat: "in_playlist"`, capped
  at 500) so a 182-item list comes back in seconds. Flat entries often carry no
  title: `_title_from_url()` recovers one from the URL slug and returns `""` for
  bare numeric API ids, so the caller falls back to `Track <id>` instead of
  showing "295".
- **Playlist downloads are one zip per request.** `PLAYLIST_BATCH_LIMIT` (40)
  exists because the zip is only sent after every item exists, so the request
  must outlive the whole batch inside gunicorn's 300s timeout; 11 SoundCloud
  tracks measured ~42s. The page splits a larger selection into parts, so "Select
  all" on a 182-item list still works. Items that fail are listed in
  `_failed_items.txt` inside the zip instead of failing the whole request, and the
  response carries `X-Playlist-Items` / `X-Playlist-Failures`.
- A playlist zip is fetched with `fetch()` and saved from a blob, not through a
  hidden iframe: an iframe renders an error response invisibly, so a rejected
  request looked like nothing happening at all. When every item fails, the error
  carries the first item's reason so the user can act on it.

## Routes

- `GET /` — the single-page UI.
- `GET /api/social/platforms` — the supported-site list for the UI chips.
- `POST /api/social/resolve` — extract metadata + formats (yt-dlp).
- `GET /api/social/stream` — stream one format for playback (Range-capable).
- `GET /api/social/download` — save one format as a file (ffmpeg-merged if needed).
- `POST /api/social/playlist` — flat-list a playlist's entries (JSON).
- `GET /api/social/playlist/download` — download selected items (`items=1,3,5`
  or `1-5`) and stream them back as one zip.
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
    `/healthz` every 5 minutes. Fly.io is *not* free any more: it gives $1 of
    credit for 7 days. **Railway is free without a card** as of this writing -
    `railway.com/pricing` lists a Free plan at $0/month, "No credit card
    required", $1 of monthly usage credits, services that wake on request, and
    1 vCPU / 0.5 GB RAM - so an earlier note in this file that Railway needs a
    card was wrong. Hugging Face moved
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

**SnapDeploy** (`snapdeploy.dev`, a product of AAR Labs) is the best technical fit
on paper - Docker on AWS, free Small container 512 MB / 0.25 vCPU, no card - and
**it blocks this app by policy.** Their Terms of Service, section "3. Video
Piracy & Copyright Infringement Tools", names `yt-dlp` in its blocked-video-
downloader list, and also blocks "cloud-storage relay frontends". A deploy was
accepted and then failed:

```
status: FAILED
errorMessage: Your deployment was blocked because it violates SnapDeploy's
Acceptable Use Policy.
```

The account stayed healthy (4 of 5 rolling deploys left, no orphan container) but
no container was ever created. This is not fixable in code, so do not spend
another deploy on it. Railway has only a general copyright clause, not a
downloader ban, so it is the strongest candidate - see deploy_railway.py.
**Render and alwaysdata also have only general copyright clauses**

Their API is undocumented in prose; the paths, the `X-API-Key` header and the
payloads live in `deploy_snapdeploy.py` and were read out of the official MCP
client (`@snapdeploy/mcp`, `dist/tools.js`):

- `GET /api/mobile/bootstrap` - plan, containers, quota, deploy cap
- `POST /api/mobile/deploy` - `{repo, name, branch, env, port, size}`
- `GET /api/mobile/containers/<id>` and `/deployments?limit=N` for build logs

Two traps found while probing: Cloudflare fronts the domain and answers **403
error 1010** to `Python-urllib/x.y`, so scripts must set their own `User-Agent`;
and a sleeping container's 503 **only a real browser visit wakes** - uptime
pingers and cron jobs get the 503 and do not wake it. Tokens are `sd_pat_...`
with nested scopes `read` < `deploy` < `manage`.

**Infrlo** (`infrlo.com`) is the one no-card host whose terms clearly allow a
downloader: section 3 bans only malware, spam, illegal content and DDoS. Free
plan is one app, 512 MB, 0.5 vCPU, 3 GB, no card, and it builds Python from
`requirements.txt` with no Dockerfile. **Its console API was unreachable when
tried**: `haa.infrlo.com` (the axios baseURL in the console bundle, not
`dash.infrlo.com`) resolves to `172.190.110.129` and times out on 80 and 443,
and two external proxies return Cloudflare **522**, so the origin is down rather
than the network here. `deploy_infrlo.py` mirrors the console's own calls and is
ready for when it returns. Two details that cost time: ffmpeg is not in the base
image, so the build command installs it; and `gunicorn` may not be on `PATH` even
when installed, so use `python3 -m gunicorn`.

**The app is deployed and live at https://web-production-ac65b984.up.railway.app.**
Railway project `tech-arslan-software-solutions`, service `web`, deployment
SUCCESS, verified on the running instance: `/healthz` 200, `/` 200, 156
platforms with `"ffmpeg": true`, three separate sources resolve, and
`/api/social/download` returns 788,493 bytes of valid `video/mp4`. The UI was
also exercised in a browser: a link resolves to a title with Play and Download
buttons, and the TeraBox tab renders. Redeploy with
`RAILWAY_TOKEN=... python3 deploy_railway.py`.

Three things about this token type, learned by hitting them: a workspace token
(`rlwy_wksp_...`) authenticates but answers "Not Authorized" on `me`, because
`me` describes a user; `projectCreate` infers the workspace from the token so no
workspaceId is needed; and a fresh service has no public domain until
`serviceDomainCreate` runs with `targetPort` matching PORT. Railway also 403s
Python-urllib's default user agent on `__schema`, so send a browser agent.

**YouTube fails on any datacenter IP, and this was tested hard rather than
assumed.** Nine player clients were tried individually (`tv`, `ios`, `android`,
`mweb`, `web_safari`, `tv_embedded`, `web`, `android_vr`, `tv_simply`), then a
full `bgutil` PO-token provider was built from source - node 24, `npm ci` (254
packages), `tsc`, server on `127.0.0.1:4416`, confirmed generating real tokens
via `/get_pot`. What happens:

- `tv`, `web`, `ios`, `mweb`, `web_safari`, `tv_simply` answer "Sign in to
  confirm you're not a bot" outright.
- `tv_embedded` and `android` do return formats (52 and 4), and `visionos` -
  the client yt-dlp picks by default now - returns the full ladder up to 4K.
- But every media URL, and every HLS fragment, then answers **403**. `visionos`
  gets as far as downloading fragments 1-123 before 403ing on the rest.
- With the PO-token server up, extraction improves (no more sign-in error) but
  the fragments still 403. So the PO token is not what is missing.

Conclusion: YouTube is blocking this IP range at the media layer. No client
choice, PO token, or format flag fixes that. The app's cookie hint is the
correct answer, and `SOCIAL_COOKIES` is the real fix - a cookies.txt from a
signed-in browser passes the bot check. Everything non-YouTube works, verified
by an 788,493-byte download.

Do not re-run this investigation: the nine-client probe and the bgutil build
both take ~15 minutes and land on 403 every time.

**The Android shell is in `android/` and builds with JDK 21 + AGP 8.7.3 +
Gradle 8.9 (wrapper committed).** `assembleDebug` and `assembleRelease` both
verified here; `apksigner verify` passes v1+v2 and `zipalign -c 4` passes. The
toolchain is not preinstalled: JDK 21 is `openjdk-21-jdk-headless` (Debian 13 has
no JDK 17 package), the SDK is at `~/android-sdk` with `platforms;android-35` and
`build-tools;35.0.0`, and Gradle 8.9 is unpacked at `~/tools/gradle-8.9`. The
project also needs `pluginManagement { repositories { google() } }` in
`settings.gradle` or AGP cannot resolve. Keystore lives at
`/workspace/keystore/techarslan-release.jks`, outside the repo, with a placeholder
passphrase that must be rotated before a real Play upload.

**Backend switching without an APK rebuild is `api_config.json` + `/api/app-config`.**
The client tries GitHub raw first, then the last-known backend's own
`/api/app-config`, then the `BAKED_BACKEND` constant. A candidate is accepted only
if `/healthz` answers 200. Verified locally that the chain resolves and that a
non-http value is rejected (`isHttpUrl`, because `startsWith("http")` would accept
`httpfoo:`). Note the migration trap: renaming the repo breaks the raw URL inside
already-installed APKs, so repo and backend must not move simultaneously.

**`/privacy` is served by the Flask app** for the Play Console privacy policy URL.
It states the app collects nothing, which matches the actual permission list
(INTERNET + ACCESS_NETWORK_STATE only).

**`GITHUB_TOKEN` here is a GitHub App installation token and cannot push.**
`x-oauth-scopes` comes back empty, the API still reports `push: true`, and git
answers 403 "Resource not accessible by integration". A user-supplied fine-grained
PAT with Contents: Read and write does push. One trap: such a PAT also needs the
**Workflows** permission, or GitHub rejects any commit that touches
`.github/workflows/` with "refusing to allow a Personal Access Token to create or
update workflow". Commits that do not touch a workflow file push fine, which is a
usable workaround — drop the workflow from the commit, push the rest, and add the
workflow once the token has the scope. Pass the token through `GIT_ASKPASS` rather
than embedding it in the remote URL, and never write it into the repo.

**TeraBox is verified end to end against real public share links**, and doing
that found a real bug. Two working MP4 shares and six ZIP shares were resolved
from public GitHub READMEs. Findings:

- The primary TeraBox resolver works without a cookie: a real share resolved to
  1 file with `"source": "terabox"`, so the playterabox fallback was not used.
- `_serve` used to stamp every helper download as `.ts` / `video/mp2t` on the
  assumption the helper transcodes to MPEG-TS. It does not - an MP4 share came
  back as `ftypisom...avc1mp41`, and a ZIP share came back as `PK..`. The app was
  handing users a correct MP4 named `.ts` with `video/mp2t`, which players then
  refused. `_sniff_media()` now reads the first 64 KB and picks the real
  extension and content type, replaying that chunk through `_proxy_stream`'s
  `prefix` argument. Unknown bytes fall back to the upstream's own headers.
- Re-verified after the fix: MP4 share -> `video/mp4`, 2,321,282 bytes, valid
  `ftyp`; ZIP share -> `application/zip`, valid `PK`; `Range: bytes=0-1023` ->
  206 with exact `Content-Range: bytes 0-1023/2321282`; `/api/hls` playlist ->
  200 and its first segment is a real MPEG-TS (0x47 every 188 bytes).
- An invalid share id fails cleanly, and a non-TeraBox host is rejected with
  "That does not look like a TeraBox share link."

**Railway's build is verified, not assumed.** The Dockerfile builds, and the
container answers 200 on `/healthz` and `/` with `PORT=8080`, binding dual-stack
(`[start.sh] gunicorn on [::]:8080`). Inside it: ffmpeg 7.1.5,
`/api/social/platforms` reports 156 platforms with `"ffmpeg": true`, a real
YouTube link resolves to a title plus 15 formats, and an end-to-end download
returns 788,493 bytes of `video/mp4` with HTTP 200. `deploy_railway.py` drives
the GraphQL API at `backboard.railway.com/graphql/v2`; every call in it was
introspected from the live schema, and an invalid token returns Railway's own
"Not Authorized", which proves the request shape is right.

**YouTube will fail on any datacenter IP, and that is not a bug here.** From
this network `yt-dlp` returns the metadata and lists 15 formats, but the HLS
fragments come back empty and no progressive format is offered. Railway, Fly and
Render all run on datacenter IPs, so expect the same there. TeraBox and plain
HTTP sources work. YouTube needs cookies or a proxy, which is a separate job.

**Check a host's AUP before writing the deploy script.** Three of the five hosts
examined here refuse a downloader outright or sit in a category they ban, and a
blocked deploy still consumes a quota slot. Read the terms, not the pricing page.

**Signup cannot be automated on any of these hosts.** All of them verify an email
address, so the account has to be created by its owner. Build the deploy script,
verify its request shape against the API's own schema, and hand over the one
command - do not claim a deploy happened when only the script exists.

## Android updates

There is no Play listing, so an installed APK cannot be updated by the store. The
shell reads `api_config.json` at launch and compares `latest_app_version_code`
against its own `versionCode`; when the config is newer it offers a download from
`update_url`, or `<backend>/download-apk` when that is empty. Shipping a release is
therefore: build with a bumped `-PappVersionCode`, drop the APK in `dist/`, bump the
two `latest_app_*` fields, commit, push. The signing keystore lives outside the
repository in `/workspace/keystore/`; losing it forces every customer to uninstall
before they can install the next build, because Android will not replace an app
signed by a different key.

## Sponsor branding

Every download carries the sponsor in three layers: the filename (`_branded`),
container metadata (`_apply_sponsor_metadata`, ffmpeg `-c copy` so there is no
re-encode and no quality loss), and an on-screen badge in the player. Tagging is
skipped for ranged requests, non-video content, and anything above
`SPONSOR_TAG_LIMIT_MB` (default 300), because a free host cannot spool a
multi-gigabyte file twice. Pixel burn-in is off by default; `SPONSOR_BURN_IN=1`
opts in, but it re-encodes and will time out on large files.

`_tagged_download` takes a `prefix` argument. The helper path sniffs the first
64 KB to learn the container, so those bytes must be written back before the
rest or the file silently loses its header. This was a real bug; keep the
argument wired at the call site.

## Conventions

- Keep it a single file; no build step.
- Never print secret values (e.g. `TERABOX_COOKIE`) to logs or the UI.
