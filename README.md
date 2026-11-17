# TeraBox Downloader

A single-file web app that resolves a public TeraBox share link, lists its
files, plays videos in the browser, and downloads them.

## Run

```bash
pip install -r requirements.txt
python3 terabox_app.py          # serves on http://0.0.0.0:12000
```

Set `PORT` to change the port.

## How it works

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
