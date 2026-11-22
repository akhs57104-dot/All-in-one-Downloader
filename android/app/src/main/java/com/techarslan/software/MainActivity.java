package com.techarslan.software;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.URLUtil;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * A thin native shell around the hosted web app.
 *
 * The point of the shell is not to look native - it is to add the three things a
 * browser tab cannot do, and to survive the backend moving:
 *
 *   1. Native downloads, so a 2 GB file lands in Downloads with a progress
 *      notification instead of dying when the WebView is backgrounded.
 *   2. Share-intent intake, so a TeraBox link can be sent straight here.
 *   3. Backend discovery. The server URL is read from a config file at launch, so
 *      moving the backend to a new Railway account takes effect in every
 *      installed app without shipping a new APK.
 */
public class MainActivity extends Activity {

    /** Used only when both remote sources are unreachable. */
    private static final String BAKED_BACKEND =
            "https://web-production-ac65b984.up.railway.app";

    /** The single switch file. Editing this on GitHub moves every installed app. */
    private static final String CONFIG_RAW_URL =
            "https://raw.githubusercontent.com/akhs57104-dot/All-in-one-Downloader/main/api_config.json";

    private static final String PREFS = "techarslan";
    private static final String KEY_BACKEND = "backend_url";
    private static final String KEY_OVERRIDE = "backend_override";

    private static final int CONNECT_TIMEOUT_MS = 6000;
    private static final int READ_TIMEOUT_MS = 6000;

    private WebView webView;
    private ProgressBar spinner;
    private LinearLayout errorPanel;
    private TextView errorText;
    private FrameLayout root;

    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private String backend;
    private boolean pageFailed;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildUi();
        resolveBackendThenLoad();
    }

    // ---------------------------------------------------------------------
    // UI
    // ---------------------------------------------------------------------

    private void buildUi() {
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.WHITE);

        webView = new WebView(this);
        webView.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        webView.setBackgroundColor(Color.WHITE);
        root.addView(webView);

        spinner = new ProgressBar(this);
        FrameLayout.LayoutParams spinnerParams = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        spinnerParams.gravity = Gravity.CENTER;
        spinner.setLayoutParams(spinnerParams);
        root.addView(spinner);

        errorPanel = buildErrorPanel();
        errorPanel.setVisibility(View.GONE);
        root.addView(errorPanel);

        setContentView(root);
        configureWebView();
    }

    private LinearLayout buildErrorPanel() {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setGravity(Gravity.CENTER);
        panel.setPadding(dp(32), dp(32), dp(32), dp(32));
        panel.setBackgroundColor(Color.WHITE);
        panel.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        TextView title = new TextView(this);
        title.setText(R.string.offline_title);
        title.setTextSize(19);
        title.setGravity(Gravity.CENTER);
        panel.addView(title);

        errorText = new TextView(this);
        errorText.setTextSize(14);
        errorText.setGravity(Gravity.CENTER);
        errorText.setPadding(0, dp(12), 0, dp(24));
        panel.addView(errorText);

        Button retry = new Button(this);
        retry.setText(R.string.retry);
        retry.setOnClickListener(v -> {
            errorPanel.setVisibility(View.GONE);
            spinner.setVisibility(View.VISIBLE);
            resolveBackendThenLoad();
        });
        panel.addView(retry);

        Button settings = new Button(this);
        settings.setText(R.string.change_server);
        settings.setOnClickListener(v -> showServerDialog());
        panel.addView(settings);

        return panel;
    }

    private void configureWebView() {
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(false);
        s.setSupportMultipleWindows(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        // The hosted page is responsive; a fixed width would break its layout.
        s.setCacheMode(WebSettings.LOAD_DEFAULT);

        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true);

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                if (newProgress >= 90) spinner.setVisibility(View.GONE);
            }
        });

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return handleUrl(request.getUrl().toString());
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                spinner.setVisibility(View.GONE);
                if (pageFailed) {
                    pageFailed = false;
                    showError(getString(R.string.offline_body));
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                // Only a failed main document means the site is down; a single
                // blocked image must not replace a working page with an error.
                if (request.isForMainFrame()) {
                    pageFailed = true;
                }
            }
        });

        webView.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition,
                                        String mimeType, long contentLength) {
                startNativeDownload(url, userAgent, contentDisposition, mimeType);
            }
        });
    }

    /** Keep the web app inside the shell; send anything foreign to a real browser. */
    private boolean handleUrl(String url) {
        if (backend != null && url.startsWith(backend)) {
            return false;
        }
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (ActivityNotFoundException ignored) {
            Toast.makeText(this, R.string.no_browser, Toast.LENGTH_SHORT).show();
        }
        return true;
    }

    // ---------------------------------------------------------------------
    // Downloads
    // ---------------------------------------------------------------------

    private void startNativeDownload(String url, String userAgent, String contentDisposition,
                                     String mimeType) {
        try {
            String fileName = URLUtil.guessFileName(url, contentDisposition, mimeType);
            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
            request.setMimeType(mimeType);
            request.addRequestHeader("User-Agent", userAgent);
            // The backend may have set a session cookie; without it a social
            // stream can answer 403 to the DownloadManager.
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null && !cookie.isEmpty()) {
                request.addRequestHeader("Cookie", cookie);
            }
            request.setTitle(fileName);
            request.setDescription(getString(R.string.downloading));
            request.setNotificationVisibility(
                    DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            request.setAllowedOverMetered(true);
            request.setAllowedOverRoaming(false);
            request.allowScanningByMediaScanner();
            request.setDestinationInExternalPublicDir(
                    Environment.DIRECTORY_DOWNLOADS, fileName);

            DownloadManager manager = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            if (manager == null) {
                throw new IllegalStateException("no download manager");
            }
            manager.enqueue(request);
            Toast.makeText(this, R.string.download_started, Toast.LENGTH_SHORT).show();
        } catch (Exception exc) {
            // A download the system cannot queue is usually a non-HTTP URL
            // (blob:), so hand it back to the WebView rather than failing silently.
            Toast.makeText(this, getString(R.string.download_failed, String.valueOf(exc.getMessage())),
                    Toast.LENGTH_LONG).show();
        }
    }

    // ---------------------------------------------------------------------
    // Backend discovery
    // ---------------------------------------------------------------------

    private void resolveBackendThenLoad() {
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        String override = prefs.getString(KEY_OVERRIDE, null);
        if (override != null && !override.isEmpty()) {
            backend = override;
            loadSite();
            return;
        }

        io.execute(() -> {
            String resolved = null;
            String note = null;

            // 1. The switch file on GitHub. This is the whole point: a new
            //    Railway URL reaches existing installs from here.
            String remote = fetchConfigBackend(CONFIG_RAW_URL);
            if (remote != null && healthy(remote)) {
                resolved = remote;
            }

            // 2. The last backend that worked, asking it for its own config. This
            //    covers a network that blocks raw.githubusercontent.com.
            if (resolved == null) {
                String cached = prefs.getString(KEY_BACKEND, null);
                if (cached != null && !cached.isEmpty()) {
                    String served = fetchConfigBackend(cached + "/api/app-config");
                    if (served != null && healthy(served)) {
                        resolved = served;
                    } else if (healthy(cached)) {
                        resolved = cached;
                    }
                }
            }

            // 3. The value compiled in, so a first launch offline still tries
            //    something instead of showing a dead screen.
            if (resolved == null) {
                if (healthy(BAKED_BACKEND)) {
                    resolved = BAKED_BACKEND;
                } else {
                    note = getString(R.string.offline_body);
                }
            }

            final String chosen = resolved;
            final String failure = note;
            runOnUiThread(() -> {
                if (chosen == null) {
                    spinner.setVisibility(View.GONE);
                    showError(failure);
                    return;
                }
                backend = chosen;
                prefs.edit().putString(KEY_BACKEND, chosen).apply();
                loadSite();
            });
        });
    }

    /** Read `backend_url` from a config document, tolerating both shapes it comes in. */
    private String fetchConfigBackend(String url) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
            conn.setReadTimeout(READ_TIMEOUT_MS);
            conn.setRequestProperty("Accept", "application/json");
            if (conn.getResponseCode() != 200) {
                return null;
            }
            String body = readAll(conn.getInputStream());
            JSONObject json = new JSONObject(body);
            // GitHub raw serves the file itself; our own route wraps it in
            // {"ok":true,"config":{...}}. Accept either.
            JSONObject config = json.optJSONObject("config");
            if (config == null) {
                config = json;
            }
            String value = config.optString("backend_url", "").trim();
            if (value.isEmpty()) {
                return null;
            }
            while (value.endsWith("/")) {
                value = value.substring(0, value.length() - 1);
            }
            return isHttpUrl(value) ? value : null;
        } catch (Exception ignored) {
            return null;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    /** Accept only real web URLs; `startsWith("http")` alone would allow `httpfoo:`. */
    private static boolean isHttpUrl(String value) {
        if (!value.startsWith("http://") && !value.startsWith("https://")) {
            return false;
        }
        try {
            String scheme = Uri.parse(value).getScheme();
            return "http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme);
        } catch (Exception ignored) {
            return false;
        }
    }

    /** A backend is usable only if its health probe answers. */
    private boolean healthy(String base) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(base + "/healthz").openConnection();
            conn.setConnectTimeout(CONNECT_TIMEOUT_MS);
            conn.setReadTimeout(READ_TIMEOUT_MS);
            if (conn.getResponseCode() != 200) {
                return false;
            }
            return readAll(conn.getInputStream()).contains("ok");
        } catch (Exception ignored) {
            return false;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    private static String readAll(InputStream in) throws Exception {
        StringBuilder out = new StringBuilder();
        try (BufferedReader reader =
                     new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                out.append(line);
            }
        }
        return out.toString();
    }

    private void loadSite() {
        if (backend == null) {
            return;
        }
        webView.loadUrl(backend);
        String shared = extractSharedLink(getIntent());
        if (shared != null) {
            injectSharedLink(shared);
        }
    }

    /**
     * Fill the page's link box with a shared URL once the page is ready, so the
     * user lands on a prefilled form instead of an empty one. The field name is
     * part of the hosted page's DOM, so the lookup is guarded.
     */
    private void injectSharedLink(String link) {
        String js = "(function(){var l=" + JSONObject.quote(link) + ";"
                + "var i=document.querySelector('input[type=url],input[type=text],#link,.link-input');"
                + "if(i){i.value=l;i.dispatchEvent(new Event('input',{bubbles:true}));"
                + "i.dispatchEvent(new Event('change',{bubbles:true}));"
                + "if(i.form&&i.form.requestSubmit){i.form.requestSubmit();}}})();";
        webView.evaluateJavascript(js, null);
    }

    private String extractSharedLink(Intent intent) {
        if (intent == null) {
            return null;
        }
        String action = intent.getAction();
        if (Intent.ACTION_SEND.equals(action)) {
            String text = intent.getStringExtra(Intent.EXTRA_TEXT);
            return firstUrl(text);
        }
        if (Intent.ACTION_VIEW.equals(action) && intent.getData() != null) {
            return intent.getData().toString();
        }
        return null;
    }

    private static String firstUrl(String text) {
        if (text == null) {
            return null;
        }
        String[] parts = text.split("\\s+");
        for (String part : parts) {
            if (part.startsWith("http://") || part.startsWith("https://")) {
                return part;
            }
        }
        return null;
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String shared = extractSharedLink(intent);
        if (shared != null && backend != null) {
            webView.loadUrl(backend);
            injectSharedLink(shared);
        }
    }

    // ---------------------------------------------------------------------
    // Error state and settings
    // ---------------------------------------------------------------------

    private void showError(String message) {
        errorText.setText(message == null ? getString(R.string.offline_body) : message);
        errorPanel.setVisibility(View.VISIBLE);
        webView.setVisibility(View.INVISIBLE);
    }

    private void showServerDialog() {
        EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint(R.string.server_hint);
        String current = getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_OVERRIDE, "");
        input.setText(current == null ? "" : current);

        new AlertDialog.Builder(this)
                .setTitle(R.string.change_server)
                .setView(input)
                .setPositiveButton(R.string.save, (d, w) -> {
                    String value = input.getText().toString().trim();
                    while (value.endsWith("/")) {
                        value = value.substring(0, value.length() - 1);
                    }
                    SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
                    prefs.edit().putString(KEY_OVERRIDE, value).apply();
                    if (value.isEmpty()) {
                        prefs.edit().remove(KEY_BACKEND).apply();
                    }
                    spinner.setVisibility(View.VISIBLE);
                    errorPanel.setVisibility(View.GONE);
                    webView.setVisibility(View.VISIBLE);
                    resolveBackendThenLoad();
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }

    // ---------------------------------------------------------------------
    // Lifecycle
    // ---------------------------------------------------------------------

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView.canGoBack()) {
            webView.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        io.shutdownNow();
        if (webView != null) {
            webView.destroy();
        }
        super.onDestroy();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
