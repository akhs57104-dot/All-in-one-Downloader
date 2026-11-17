# TeraBox Downloader

A single-file web app that resolves a public TeraBox share link and downloads
its files.

## Run

```bash
pip install -r requirements.txt
python3 terabox_app.py          # serves on http://0.0.0.0:12000
```

Set `PORT` to change the port.

## How it works

The app talks to TeraBox's own share API:

1. Load the share page to pick up the anonymous session cookies and the page's
   `jsToken`.
2. `GET /api/shorturlinfo` — reads the share record (`shareid`, `uk`, `sign`,
   `timestamp`) and the file list. This works anonymously.
3. `GET /api/sharedownload` — mints a short-lived direct `dlink` for a file.
4. Stream that `dlink` through this server, forwarding cookies and `Range`
   headers so seeking and resuming work.

Requests go through `curl_cffi` with browser TLS impersonation, because
TeraBox's WAF can reject Python's TLS fingerprint before it reads the headers.
If `curl_cffi` is not installed the app falls back to plain `requests`.

## The cookie requirement

Listing files works with no account. Downloading does not: TeraBox now answers
`403 {"error_code":31045,"error_msg":"user not exists"}` to the `dlink` for an
anonymous session, and its newer `POST /share/download` endpoint returns
`errno 400310 "need verify_v2"` (a CAPTCHA).

To download, sign in to TeraBox in a browser, copy the session cookie, and give
it to the app:

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
