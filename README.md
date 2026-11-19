# All-in-One Video Downloader

A single-file web app by **Tech Arslan Software Solutions** with two tools in one
page:

- **Social Downloader** — paste a link from any of **156 named sites** (YouTube,
  Instagram, Facebook, TikTok, X, Dailymotion, Vimeo, SoundCloud, Twitch, Bilibili
  and many more), with a searchable platform picker; play it in the browser or
  save it as an MP4. yt-dlp's generic extractor still covers sites that are not
  named, so the "Other link" option works for them too.
- **TeraBox** — resolve a public TeraBox share link, list its files, play videos
  in the browser, and download them.

## Run

```bash
pip install -r requirements.txt
python3 terabox_app.py          # serves on http://0.0.0.0:12000
```

Set `PORT` to change the port. `ffmpeg` on `PATH` is recommended: it lets yt-dlp
merge separate video and audio tracks for the highest qualities. Without it, only
single-file (progressive) streams are offered.

## Deploy

The app keeps short-lived state in process memory - resolved HLS playlists and
the TeraBox helper cache - so run **one worker with threads**, not multiple
processes. `wsgi.py` is the WSGI entry point.

Keep it running on any Linux box (auto-restarts if gunicorn ever exits):

```bash
PORT=12000 nohup ./run.sh > /tmp/techarslan.out 2>&1 &
```

Managed hosts: `Dockerfile`, `Procfile`, `railway.json`, `railpack.json`,
`nixpacks.toml`, `render.yaml` and `fly.toml` are ready for Railway, Render,
Fly.io, Heroku and similar.

## Free hosting that stays up

Free hosting splits into two groups: hosts that want a card on file even on the
free plan (Northflank, Koyeb), and hosts that do not (Render, alwaysdata). The
Docker hosts build the bundled `Dockerfile`, so `ffmpeg` comes along.

**If you have no card, you have two options.** Render is the easier one to start
and keeps `ffmpeg` for free, but it spins down after 15 idle minutes and its
dashboard sometimes refuses to create the service. alwaysdata is 0 €/month for
life, never asks for a card, and does not spin down, but has no Docker (install
`ffmpeg` by hand) and only 256 MB of RAM.

### Northflank - free plan, but a card is required

Northflank's Developer Sandbox is free in usage (2 services, "Always on, no
sleeping"), but its billing docs state plainly that *"all users must add a
payment method to start creating resources on Northflank, regardless of plan
selection. This is to verify user identity."* The card is not charged while you
stay inside the sandbox, but you cannot deploy without entering one.

1. Sign up at <https://app.northflank.com/signup>.
2. **Create** → **Service** → **Combined**.
3. Name it, then under **Repository** connect GitHub and pick
   `All-in-one-Downloader`, branch `main`.
4. **Build options** → **Dockerfile** (leave the paths at the defaults).
5. **Networking** → keep the detected port `12000`, set it **public**.
6. **Create service**. The first build takes a few minutes; the service header
   then shows a public `*.code.run` URL that does not change.

### Render - free, no card, kept awake

Render's Free plan gives **750 instance hours per month**, and the longest month
is 744 hours, so a single always-on service fits inside the free allowance. A
Free web service still *spins down after 15 minutes without traffic*, and a
spun-down service serves a slow loading page on the next visit. A free uptime
pinger removes that: it requests `/healthz` every 5 minutes, which both keeps
the service awake and counts as the inbound traffic Render requires.

1. Deploy: <https://render.com/deploy?repo=https://github.com/akhs57104-dot/All-in-one-Downloader>
   (Render reads `render.yaml`, picks Docker, health-checks `/healthz`).
2. Create a free monitor at <https://uptimerobot.com> pointing at your
   `https://<name>.onrender.com/healthz`, interval 5 minutes.

Without the pinger the URL still works, but the first request after an idle
spell waits about a minute while the service spins back up.

#### Deploying with the API instead of the dashboard

The Render dashboard cannot be automated: it is behind a login and an hCaptcha.
The API can. `deploy_render.py` creates the service from the repository's
`render.yaml` equivalent in one call:

```bash
RENDER_API_KEY=rnd_xxx python3 deploy_render.py --dry-run   # print the request
RENDER_API_KEY=rnd_xxx python3 deploy_render.py --watch     # create and wait
```

The key comes from <https://dashboard.render.com/u/settings#api-keys> and is
read from the environment only. A `402` from the API means Render wants payment
details on that account; the free plan itself does not ask for them.

### Render says "not found" - the service was never created

If `https://<name>.onrender.com` answers **404 Not Found**, nothing is deployed
there. That 404 is byte-for-byte the same response a name that never existed
returns, so it is not the app crashing and not the health check failing - the
service simply is not there. A deploy that failed would show a Render error page
or a "service unavailable" screen instead.

The two things that stop the service from being created:

- **The blueprint was never applied.** Opening `render.com/deploy?repo=...` only
  shows the review screen; the service exists once that screen is submitted.
- **The account needs payment details.** Render's API answers `402` and the
  dashboard asks for a card. The Free plan does not normally require one, but
  accounts flagged for verification do, and there is no way around it from the
  API.

Confirm locally that the app itself is fine before blaming the host - this
reproduces Render's own run (`$PORT`, non-root user, health check):

```bash
sudo docker build -t techarslan .
sudo docker run -d --name t1 -e PORT=10000 -p 10000:10000 techarslan
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:10000/healthz   # 200
```

### alwaysdata - free forever, no card, no documented spin-down

alwaysdata's Free plan is **0 €/month for life**: 1 GB SSD, 256 MB RAM, and no
payment details at any point. It runs Python 2.4-3.14 behind uWSGI and offers a
**Python WSGI** site type, which is exactly what `wsgi.py` provides. Flask is one
of its auto-installable applications. The app idles around 75 MB, so 256 MB is
enough.

This is the only option here that is free, card-free **and** not subject to a
documented idle spin-down: Render states plainly that it stops a Free web service
after 15 minutes without traffic, while alwaysdata's Free plan is sold as "0 €
/month ... for life" with no such statement.

1. Sign up at <https://www.alwaysdata.com/en/register/> and pick the **Free**
   offer. The signup form asks only for an email and a password - there is no
   card field.
2. **Web → Sites → Add a site**: type **Python WSGI**, point the application
   path at `/home/<account>/techarslan/wsgi.py`.
3. Upload the code over SFTP/SSH, then in a shell:

   ```bash
   cd ~/techarslan
   python -m venv venv && . venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Web → Sites →** the site → **Environment**: set `TERABOX_FALLBACK=on` (and
   `TERABOX_COOKIE` if you have one).
5. The site answers at `https://<account>.alwaysdata.net`.

Two things to know about this host:

- **No Docker**, so `ffmpeg` is not bundled - but it can be installed, and
  alwaysdata documents how: download the static build into `~/ffmpeg`, and add
  that directory to `PATH` in the site's environment.

  ```bash
  mkdir -p ~/ffmpeg && cd ~/ffmpeg
  wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
  tar -xJf ffmpeg-release-amd64-static.tar.xz --strip-components=1
  rm ffmpeg-release-amd64-static.tar.xz
  ```

  Without it, downloads still work; yt-dlp only loses the ability to merge
  separate video and audio tracks into a single file.
- **256 MB RAM**. A merge of a long video can approach that. If it does, prefer
  single-file formats and keep `TERABOX_FALLBACK=on`.

### Hosts that look free but are not usable here

Checked against each host's own docs, because the marketing pages disagree:

- **Hugging Face Spaces** - the hardware table lists CPU Basic as "Free!", but
  the same page adds that *"creating a new Space that runs on compute (Gradio or
  Docker) requires a paid plan. Static Spaces are free for everyone."* A static
  Space cannot run Flask.
- **Koyeb** - its own pricing FAQ contains the question *"Why does Koyeb require
  a credit card?"*, so the free allowance is gated behind a card.
- **PythonAnywhere** - the free "Beginner" account is described as giving *"two
  consoles with restricted outbound Internet access"*. Every download this app
  performs is outbound, so it cannot work there.
- **Vercel / Netlify** - see below; no long-lived process.

### Why not Vercel or Netlify

Both are built for short, stateless requests, and this app is the opposite, so
neither can host it well:

- **Vercel** - a Vercel Function may run for at most 300 s on Hobby and answers
  with a 4.5 MB body cap (streaming excepted). There is no long-lived process, so
  the in-memory HLS playlists and TeraBox helper cache this app relies on are
  gone between requests. Its Container runtime accepts a Dockerfile, but the
  same function limits still apply. On top of that, the Vercel Terms of Service
  restrict the Hobby plan to "personal or non-commercial use", and forbid
  making the Services available to a third party - so a paid customer cannot be
  served from a free Hobby project.
- **Netlify** - no Dockerfile or container support at all. Serverless Functions
  stop at 60 s synchronous and 15 minutes background, and there is no bundled
  `ffmpeg` or persistent process.

A long-running download server needs the Northflank or Render setup above.

### A URL that survives a restart, with no account

A Cloudflare quick tunnel is free but its hostname is new on every restart.
localtunnel lets you *ask for* a subdomain, so the address is the same every
time:

```
https://tech-arslan-software-solutions.loca.lt
```

`serve.sh` starts localtunnel first and cloudflared second, then keeps both
alive. The requested subdomain is written to `/tmp/techarslan-url.txt` and the
cloudflared address to `/tmp/techarslan-cf-url.txt`.

The trade-off is real and worth knowing before you share the link: localtunnel
shows every new visitor a one-time "Tunnel website ahead" page that asks them to
click Continue. Any HTTP client can skip it with the header
`bypass-tunnel-reminder: true`, but a person on a phone sees the page once. If
that is unacceptable, hand out the cloudflared address instead - it has no
interstitial but changes whenever the tunnel restarts.

### Cloudflare - good tunnel, wrong product for hosting

Cloudflare is the best *tunnel* here, and the worst *host*.

**As a tunnel it is free and needs no account.** `./tunnel.sh` (or `serve.sh`)
runs `cloudflared tunnel --url http://127.0.0.1:12000`, which prints a
`https://<words>.trycloudflare.com` address. That address is a fresh one every
time the tunnel restarts, because a quick tunnel has no saved name. It is fine
for a demo or a short-lived share, not for a customer's bookmark.

**As a host it costs money.** Cloudflare Workers caps the free plan at 10 ms
CPU per request, 128 MB memory and 100,000 requests a day - the app cannot run
there. Cloudflare Containers, the product that would run this Dockerfile, is
listed as "Available on Workers Paid plan", so it needs the paid Workers plan
($5/month). A named tunnel that keeps one hostname needs a domain on
Cloudflare, which means buying a domain.

The stable address for this deployment is the sandbox hostname, which the
platform gives you and which outlives any tunnel:

```
https://work-1-wcqnygvsjfbxdklm.prod-runtime.all-hands.dev
```

`serve.sh` writes it to `/tmp/techarslan-stable-url.txt` when `PUBLIC_URL` is
set, and keeps the tunnel URL in `/tmp/techarslan-url.txt` as the fallback.

### One-click on a host you already have an account for

Every one of these builds the bundled `Dockerfile`, so `ffmpeg` comes along and
the dual-stack `start.sh` binds whatever `$PORT` the host injects.

- **Render** — <https://render.com/deploy?repo=https://github.com/akhs57104-dot/All-in-one-Downloader>
  reads `render.yaml`, picks the Docker runtime and health-checks `/healthz`.
- **Koyeb** — <https://app.koyeb.com/deploy?type=git&repository=github.com/akhs57104-dot/All-in-one-Downloader&branch=main&builder=dockerfile&ports=8080;http;/&env%5BPORT%5D=8080&env%5BTERABOX_FALLBACK%5D=on>
  builds the same Dockerfile and exposes port 8080 over HTTP.
- **Railway** — <https://railway.com/new/github/akhs57104-dot/All-in-one-Downloader>
  then pick "Dockerfile" as the builder if it does not do so itself.
- **Heroku / Dokku / Coolify** — they read `Procfile` (`web: bash ./start.sh`).

No account is needed for a temporary public URL on any Linux box that can reach
the internet: `cloudflared tunnel --url http://127.0.0.1:12000` prints a
`https://*.trycloudflare.com` link that proxies to the local server.

### Railway

All three Railway builders work:

* **Dockerfile** - pinned in `railway.json`, bundles `ffmpeg`.
* **Railpack** (Railway's current default) - `railpack.json` adds `ffmpeg`
  to both the build and runtime images. Keep the `"..."` entry in
  `deploy.aptPackages`: without it the list replaces the runtime packages
  Railpack generates.
* **Nixpacks** (older) - `nixpacks.toml` adds `ffmpeg` via `aptPkgs`. Do not
  declare `nixPkgs` there: that replaces the setup phase Nixpacks generates
  for Python, and the build dies with `python: command not found`.

The container runs one gunicorn worker with threads via `start.sh`, which
binds to the host's `$PORT`:

```
gunicorn --workers 1 --threads 8 --timeout 300 --no-control-socket --bind [::]:$PORT wsgi:app
```

The start command must stay `bash ./start.sh` (what `railway.json` sets). If a
custom start command is typed into Railway's dashboard instead, it is passed
through without a shell in some cases, which is how the literal `$PORT` in
`--bind 0.0.0.0:$PORT` reaches gunicorn and crashes the deploy.

Three settings matter when a host reports the app as "crashed":

* **Bind to `$PORT`, dual-stack.** Never hardcode a port. Railway, Render and
  Fly inject `$PORT` and health-check that exact port; an app listening on
  12000 looks dead to them. A host whose private network is IPv6 (Railway)
  also needs the IPv6 listen address, so `start.sh` binds `[::]` when the
  kernel allows it and falls back to `0.0.0.0`.
* **Health check on `/healthz`, not `/`.** `/` renders the ~88 KB home page;
  `/healthz` answers `{"ok": true}`. `railway.json`, `render.yaml` and
  `fly.toml` point their checks at it.
* **Readable files, non-root user.** The image does `chmod -R a+rX /app` and
  runs as `appuser`. A checkout whose files are mode `0600` (owner-only) makes
  gunicorn die with `PermissionError: /app/wsgi.py` when the platform runs it
  as an unprivileged uid. Every builder starts through `bash ./start.sh`, so a
  missing exec bit cannot fail the boot either.

`start.sh` also only passes `--no-control-socket` when the installed gunicorn
supports it; gunicorn below 23 exits with `unrecognized arguments` otherwise.

#### `Error: '$PORT' is not a valid port number`

Railway's start command field pre-fills a `$PORT` placeholder. If the value
stays in the form `--bind 0.0.0.0:$PORT` without a shell to expand it, gunicorn
receives the literal text `$PORT` and exits with that message. `${PORT:-12000}`
does not rescue it, because a non-empty literal is not an empty variable.

`start.sh` and `run.sh` now reject any non-numeric `PORT` and fall back to
12000, so the message is harmless even if a host still injects it. If you set a
start command yourself, leave it as `bash ./start.sh` (no `--bind` argument) and
let `start.sh` read `$PORT` in the shell, where expansion works.

Docker (bundles `ffmpeg`):

```bash
docker build -t techarslan .
docker run -d --restart unless-stopped -p 12000:12000 techarslan
```

The `--timeout 300` matters: merging a long video with `ffmpeg` can take longer
than a default request timeout.

## Social Downloader

Backed by [yt-dlp](https://github.com/yt-dlp/yt-dlp). The home page lists every
supported platform in a searchable picker; selecting one shows a short card with
where to copy the link from on that site and how the download works, and the
platform is also picked automatically once a link resolves.

The paste box leads the page, then the mainstream sites (YouTube, Instagram,
Facebook, TikTok, X, WhatsApp and so on) sit in a "Popular social media" row
just below it. They also lead the sidebar picker in their own group, with the
long tail under "All other sites" so it is out of the way. The featured set is `FEATURED_KEYS` in
`terabox_app.py`; the server tags each entry with `featured: true/false` and the
client groups on that. The flow is:

1. `POST /api/social/resolve` extracts the media metadata and the list of
   formats.
2. `GET /api/social/stream` streams one format for in-page playback. It supports
   HTTP `Range`, so seeking works. A single-file or audio-only format is proxied
   straight through; an HLS format has its playlist rewritten to same-origin
   segment URLs (served by `/api/social/hls/<token>/<n>.ts`).
3. `GET /api/social/download` saves the chosen quality as a file, merging video
   and audio with `ffmpeg` when the site only offers them separately.

Every saved file carries the developer branding in its name -
`<title> - Powered by Tech Arslan Software Solutions.<ext>` - so a download is
recognisable after it leaves the page. The same name is shown in the history
list, and the TeraBox tab uses it too.

Session-bound sites (TikTok signs its media URLs against the extractor's own
cookies) work because the cookies yt-dlp collected are threaded from extraction
through to the stream and download requests.

Some sites — Instagram, Facebook, and private or age-gated videos — refuse
anonymous requests. Export a Netscape-format `cookies.txt` from a signed-in
browser and pass it in via `SOCIAL_COOKIES` (see below).

## TeraBox

TeraBox no longer hands the bytes of a shared file to an anonymous visitor, so
the app tries several routes in order and uses the first that works.

Listing (always available):

1. Load the share page to pick up the anonymous session cookies and the page's
   `jsToken`.
2. `GET /api/shorturlinfo` — reads the share record (`shareid`, `uk`, `sign`,
   `timestamp`) and the file list. This works anonymously.

Getting the bytes:

1. **TeraBox direct** — `GET /api/sharedownload` mints a short-lived `dlink`,
   which is streamed through this server. This needs a signed-in cookie (see
   below) and is the only route that returns TeraBox's original MP4; without a
   cookie TeraBox answers `403 "user not exists"`, so it is skipped.
2. **Helper services** — with no cookie, the app asks third-party resolvers that
   hold their own TeraBox accounts. `playterabox.com` is tried first: it returns
   a full-length, `Range`-capable link plus a complete HLS playlist (hundreds of
   segments), so both download and full-length playback work and there is no
   quota. `iteraplay.com` is a secondary source, but it is rate-limited (a few
   files per six hours for an anonymous caller). Both see every link you paste,
   so they are a fallback, not the default.

   TeraBox never hands an anonymous visitor the original file, so a helper's copy
   is a transcoded MPEG-TS stream. Playback is unaffected (HLS carries the
   original duration), but a download arrives as `.ts`, not `.mp4`. Remux it to
   MP4 with `ffmpeg -i file.ts -c copy file.mp4`, or set `TERABOX_COOKIE` to
   download the original MP4 directly.
3. **TeraBox preview** — TeraBox's own `/share/streaming` returns an anonymous
   HLS transcode, but only a fixed 30-second window, so it is used only to give
   a short preview when neither of the above can serve the file.

Requests go through `curl_cffi` with browser TLS impersonation, because
TeraBox's WAF can reject Python's TLS fingerprint before it reads the headers.
If `curl_cffi` is not installed the app falls back to plain `requests`.

## Configuration

| Variable | Effect |
|---|---|
| `PORT` | Port to listen on (default `12000`). |
| `SOCIAL_COOKIES` | A Netscape `cookies.txt` (a path, or the file's contents) for sites that need a signed-in session, such as Instagram and Facebook. |
| `TERABOX_COOKIE` | A signed-in TeraBox cookie. Enables unlimited direct downloads at full quality. |
| `TERABOX_FALLBACK` | Set to `off` to disable the third-party helper entirely. |
| `TERABOX_FALLBACK_TOKEN` | Your own iteraplay `login_token`; raises the helper's quota. |

## The cookie requirement

Listing files works with no account, and the helper service covers downloading
without one. To download directly from TeraBox — unlimited, no third party, full
quality — sign in to TeraBox in a browser, copy the session cookie, and give it
to the app:

```bash
export TERABOX_COOKIE='ndus=...'      # full cookie string works too
python3 terabox_app.py
```

A bare `ndus` value is accepted and the `ndus=` name is added for you.

Note that a TeraBox session is bound to the host that issued it, so a cookie may
be accepted by one TeraBox domain and refused (`errno -6`) by another. If a
download is refused with a cookie set, try the other mirrors the app already
knows about.

## Notes

- Some shares are password-protected; those are reported, not downloaded.
- TeraBox applies a soft rate limit to anonymous listing; if you see
  `errno 105` or `460030`, wait a minute and retry.
- Only download content you have the right to download.
