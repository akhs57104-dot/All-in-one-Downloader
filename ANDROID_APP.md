# Publishing the Android app

The Android app is a small native shell around the hosted web app. It lives in
`android/` and is built by `.github/workflows/build-apk.yml`.

## What the shell adds over the website

The website already works in any browser. The APK exists for four reasons:

- **Native downloads.** A 2 GB file goes to Android's `DownloadManager`, so it
  survives the app being backgrounded and shows a progress notification. A
  WebView download would die with the activity.
- **Share intent.** Sending a link from TeraBox or a social app offers "Tech
  Arslan" in the share sheet, so no copy-paste.
- **Backend that moves.** The server address is read from `api_config.json` at
  launch, so a new Railway account reaches installed apps without a new APK.
- **In-app updates.** The same config carries `latest_app_version_code` and an
  `update_url`. When it advertises a build newer than the installed one, the app
  offers to download it and hands the APK to Android's installer. There is no
  Play listing, so this is the only way a fix reaches a customer's phone.

## Releasing a new version

The whole release is three steps, and only the last one needs a phone:

1. Bump the code and build, signing with the release keystore:
   `./gradlew assembleRelease -PappVersionCode=3 -PappVersionName=1.2.0`
   with `KEYSTORE_PATH`, `KEYSTORE_PASSWORD`, `KEY_ALIAS` and `KEY_PASSWORD` set.
2. Copy the APK to `dist/`, delete the previous one, and set
   `latest_app_version_code` / `latest_app_version_name` in `api_config.json` to
   match. Leave `update_url` empty: the app then downloads from
   `<backend>/download-apk`. Commit and push.
3. Installed apps see the new version on their next launch and offer the update.

Set `min_app_version_code` to force the update instead of allowing "Later" —
use that only when the old build genuinely cannot talk to the current backend.

## Moving the backend to a new Railway account

This is the part that matters operationally, and it needs no rebuild:

1. Deploy the repo to the new Railway account.
2. Confirm `https://<new-host>/healthz` returns `{"ok": true}`.
3. Edit `api_config.json` in this repo: set `backend_url` to the new host.
4. Commit and push to `main`.

Every installed app picks this up on its next launch. Resolution order is:

| Order | Source | Why |
|---|---|---|
| 1 | `raw.githubusercontent.com/.../api_config.json` | Works even if the old backend is dead |
| 2 | `GET <last-known-backend>/api/app-config` | Covers networks that block GitHub raw |
| 3 | `BAKED_BACKEND` in `MainActivity.java` | First launch, offline, or total config failure |

A candidate is accepted only if its `/healthz` answers 200, so a half-deployed
Railway service is skipped rather than shown as a blank screen. The user can also
set a server by hand from the error screen ("Change server"), which overrides all
three.

**Caveat worth knowing:** if you ever rename the repository or move it to another
GitHub account, the raw URL inside already-installed APKs breaks. Step 2 then
carries the migration — but only while the *old* backend is still answering. If
both the repo and the old backend move at once, existing installs have nothing
left to ask, and users must reinstall. Move them one at a time.

## Signing

Play requires every upload signed by the same key forever. The keystore for this
project is **not** in the repository and must never be.

- Local keystore: `/workspace/keystore/new-release.jks`
  (alias `techarslan`, RSA 2048, PKCS12, valid 10000 days). The passphrase is in
  `/workspace/keystore/PASSWORD.txt`, mode 0600, outside the repository.
- `/workspace/keystore/techarslan-old-UNKNOWN-PASSWORD.jks` is the first keystore.
  Its passphrase was never recorded and could not be recovered, so it is unusable;
  keep the file only in case that password turns up somewhere.
- Back it up somewhere you will still have in five years. Losing it means you
  cannot update the app — you would have to publish a new listing under a new
  package name.
- For CI, add these repository secrets: `KEYSTORE_BASE64`
  (`base64 -w0 new-release.jks`), `KEYSTORE_PASSWORD` (contents of `PASSWORD.txt`),
  `KEY_ALIAS` (`techarslan`), `KEY_PASSWORD` (same as `KEYSTORE_PASSWORD`).
  The workflow signs only when `KEYSTORE_BASE64` is present.

**Signature break at 1.1.0.** The 1.0.0 APK was signed with the lost keystore, so
the 1.1.0 build carries a different certificate
(`d644d6af7840b84bac693068a036e73ed5c54721d237198e9ba3dce790315e72`). Android
refuses to replace an installed app with one signed by a different key: anyone on
1.0.0 must uninstall it once, then install 1.1.0. From 1.1.0 onward the in-app
update works, so this happens once and never again — as long as this keystore is
kept.

Back the keystore and `PASSWORD.txt` up somewhere you will still have in five
years. Losing them again means another forced uninstall for every customer.

## Building

Local:

```bash
cd android
./gradlew assembleRelease -PappVersionCode=2 -PappVersionName=1.0.1
```

`versionCode` must increase on every Play upload; the CI workflow derives it from
the run number, which always increases.

## Play Console checklist

- **App bundle, not APK.** Play wants `.aab` for new apps:
  `./gradlew bundleRelease`. Add that task to the workflow when you are ready to
  upload.
- **Privacy policy URL.** `https://<host>/privacy` — served by this backend.
- **Data safety form.** Declare: no data collected, no data shared. That matches
  what the app actually does. Do not declare "device ID" — the shell does not
  read it.
- **Content rating.** Fill the questionnaire honestly; a downloader will typically
  land at Everyone/PEGI 3 unless the resolved content changes that.
- **Target API level.** Currently 35, which meets the current requirement. Play
  raises this yearly; bump `compileSdk`/`targetSdk` when it does.
- **Screenshots.** Phone screenshots of the home page, a resolved file list, and
  the download notification.

## Honest risk: Play may reject a downloader

This is the part I would not want you to find out after paying the $25 fee. Google
Play's policy has repeatedly removed apps whose main function is downloading
content from third-party platforms, on the grounds of intellectual property. The
relevant policies are *Intellectual Property* and *Deceptive Behavior*. TeraBox and
social-platform downloading is exactly the category that attracts takedowns.

What that means practically:

- The listing may be rejected at review, or removed later after a complaint.
- Your developer account can receive strikes; repeated strikes close the account,
  which also affects any other apps on it.
- Nothing in this codebase changes that risk — it is a policy question, not a
  technical one.

If Play distribution is essential, the safer framing is to position the app as a
**link resolver and player** for content the user owns, and to add a visible notice
that users must only download material they have the rights to. That reduces but
does not eliminate the risk.

The reliable alternative is direct distribution: publish the APK on GitHub
Releases (the workflow already attaches it to `v*` tags) and let users sideload.
No review, no policy risk, no 30% cut — the trade-off is that users must enable
"install unknown apps" and updates are manual.

My recommendation: ship the APK via GitHub Releases first, see whether the tool is
useful, and only then decide whether the Play listing is worth the account risk.

## Verified in this session

- `assembleDebug` and `assembleRelease` both succeed with JDK 21, AGP 8.7.3,
  Gradle 8.9, compileSdk 35.
- `apksigner verify` passes on v1 and v2 schemes; `zipalign -c 4` passes.
- Manifest declares only `INTERNET` and `ACCESS_NETWORK_STATE`; `minSdk` 23,
  `targetSdk` 35.
- The config URL and both endpoints are present in the built dex.
- `/privacy`, `/api/app-config`, `/healthz` all return 200 from the Flask app.
