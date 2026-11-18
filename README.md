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
Fly.io, Heroku and similar. All three Railway builders work:

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
