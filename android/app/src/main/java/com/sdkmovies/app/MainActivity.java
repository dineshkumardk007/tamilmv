package com.sdkmovies.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.ConsoleMessage;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

public class MainActivity extends Activity {

    private static final String PREFS_NAME = "sdk_movies_prefs";
    private static final String KEY_SERVER_URL = "server_url";
    private static final String DEFAULT_SERVER_URL = "http://192.168.29.197:8777";

    private WebView webView;
    private ProgressBar loadingSpinner;
    private LinearLayout errorContainer;
    private TextView errorMessage;
    private Button btnRetry;
    private Button btnChangeHost;

    private SharedPreferences prefs;
    private String currentServerUrl;
    private boolean isPageLoadedSuccessfully = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Keep screen active on TV during playback
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        // Immersive sticky fullscreen
        setupImmersiveMode();

        setContentView(R.layout.activity_main);

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        currentServerUrl = prefs.getString(KEY_SERVER_URL, DEFAULT_SERVER_URL);

        initViews();
        setupWebView();
        loadServerUrl();
    }

    private void setupImmersiveMode() {
        Window window = getWindow();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT) {
            window.getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            );
        }
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            setupImmersiveMode();
        }
    }

    private void initViews() {
        webView = findViewById(R.id.webview);
        loadingSpinner = findViewById(R.id.loading_spinner);
        errorContainer = findViewById(R.id.error_container);
        errorMessage = findViewById(R.id.error_message);
        btnRetry = findViewById(R.id.btn_retry);
        btnChangeHost = findViewById(R.id.btn_change_host);

        btnRetry.setOnClickListener(v -> loadServerUrl());
        btnChangeHost.setOnClickListener(v -> showServerSettingsDialog());
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void setupWebView() {
        WebSettings settings = webView.getSettings();

        // High-performance JavaScript and HTML5 video settings
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(true);
        settings.setLoadsImagesAutomatically(true);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(true);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        }

        // Custom User-Agent tag indicating Android TV companion app
        String defaultUa = settings.getUserAgentString();
        settings.setUserAgentString(defaultUa + " SDKMovies-AndroidTV/1.0 Leanback LargeScreen SmartTV");

        // Hardware Acceleration & OLED Pitch-Black Background
        webView.setLayerType(View.LAYER_TYPE_HARDWARE, null);
        webView.setBackgroundColor(Color.BLACK);

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                if (newProgress < 100) {
                    if (!isPageLoadedSuccessfully) {
                        loadingSpinner.setVisibility(View.VISIBLE);
                    }
                } else {
                    loadingSpinner.setVisibility(View.GONE);
                }
            }

            @Override
            public boolean onConsoleMessage(ConsoleMessage cm) {
                return super.onConsoleMessage(cm);
            }
        });

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                super.onPageStarted(view, url, favicon);
                errorContainer.setVisibility(View.GONE);
                view.evaluateJavascript("window.__IS_TV_DEVICE__ = true;", null);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                view.evaluateJavascript("window.__IS_TV_DEVICE__ = true;", null);
                loadingSpinner.setVisibility(View.GONE);
                if (!errorContainer.isShown()) {
                    isPageLoadedSuccessfully = true;
                    webView.setVisibility(View.VISIBLE);
                    webView.requestFocus();
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                super.onReceivedError(view, request, error);
                if (request != null && request.isForMainFrame()) {
                    showConnectionError();
                }
            }

            @SuppressWarnings("deprecation")
            @Override
            public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
                super.onReceivedError(view, errorCode, description, failingUrl);
                showConnectionError();
            }
        });
    }

    private void loadServerUrl() {
        errorContainer.setVisibility(View.GONE);
        loadingSpinner.setVisibility(View.VISIBLE);
        String targetUrl = currentServerUrl;
        if (!targetUrl.contains("mode=tv")) {
            targetUrl = targetUrl + (targetUrl.contains("?") ? "&" : "?") + "mode=tv";
        }
        webView.loadUrl(targetUrl);
    }

    private void showConnectionError() {
        isPageLoadedSuccessfully = false;
        loadingSpinner.setVisibility(View.GONE);
        webView.setVisibility(View.GONE);
        errorMessage.setText(getString(R.string.error_connection, currentServerUrl));
        errorContainer.setVisibility(View.VISIBLE);
        btnRetry.requestFocus();
    }

    private void showServerSettingsDialog() {
        AlertDialog.Builder builder = new AlertDialog.Builder(this, AlertDialog.THEME_DEVICE_DEFAULT_DARK);
        builder.setTitle(R.string.server_settings_title);
        builder.setMessage(R.string.server_settings_prompt);

        final EditText input = new EditText(this);
        input.setText(currentServerUrl);
        input.setSingleLine(true);
        input.setTextColor(Color.WHITE);
        input.setSelection(currentServerUrl.length());

        LinearLayout container = new LinearLayout(this);
        container.setOrientation(LinearLayout.VERTICAL);
        int padding = (int) (18 * getResources().getDisplayMetrics().density);
        container.setPadding(padding, padding / 2, padding, padding / 2);
        container.addView(input);
        builder.setView(container);

        builder.setPositiveButton(R.string.btn_save, (dialog, which) -> {
            String url = input.getText().toString().trim();
            if (!url.startsWith("http://") && !url.startsWith("https://")) {
                url = "http://" + url;
            }
            if (url.endsWith("/")) {
                url = url.substring(0, url.length() - 1);
            }
            currentServerUrl = url;
            prefs.edit().putString(KEY_SERVER_URL, currentServerUrl).apply();
            Toast.makeText(this, "Server URL updated to: " + currentServerUrl, Toast.LENGTH_SHORT).show();
            loadServerUrl();
        });

        builder.setNegativeButton(R.string.btn_cancel, (dialog, which) -> dialog.dismiss());
        AlertDialog dialog = builder.create();
        dialog.show();
    }

    // Android TV Remote Key Handling (Back, Media Keys, D-Pad, Menu)
    @Override
    public boolean dispatchKeyEvent(KeyEvent event) {
        if (event.getAction() == KeyEvent.ACTION_DOWN) {
            int keyCode = event.getKeyCode();

            // Menu button opens Server Host settings dialog
            if (keyCode == KeyEvent.KEYCODE_MENU) {
                showServerSettingsDialog();
                return true;
            }

            // TV Remote Back key evaluation via web bridge
            if (keyCode == KeyEvent.KEYCODE_BACK) {
                if (errorContainer.getVisibility() == View.VISIBLE) {
                    finish();
                    return true;
                }

                // Check if web app handles back (closes video player or modal)
                if (webView != null && isPageLoadedSuccessfully) {
                    webView.evaluateJavascript("window.handleTvBack ? window.handleTvBack() : false;", new ValueCallback<String>() {
                        @Override
                        public void onReceiveValue(String value) {
                            if (!"true".equalsIgnoreCase(value)) {
                                if (webView.canGoBack()) {
                                    webView.goBack();
                                } else {
                                    showExitConfirmDialog();
                                }
                            }
                        }
                    });
                    return true;
                }
            }

            // TV Remote Media playback keys
            if (keyCode == KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
                    || keyCode == KeyEvent.KEYCODE_MEDIA_PLAY
                    || keyCode == KeyEvent.KEYCODE_MEDIA_PAUSE) {
                if (webView != null) {
                    webView.evaluateJavascript("window.handleTvMediaKey ? window.handleTvMediaKey('play_pause') : false;", null);
                    return true;
                }
            } else if (keyCode == KeyEvent.KEYCODE_MEDIA_REWIND
                    || keyCode == KeyEvent.KEYCODE_MEDIA_PREVIOUS) {
                if (webView != null) {
                    webView.evaluateJavascript("window.handleTvMediaKey ? window.handleTvMediaKey('rewind') : false;", null);
                    return true;
                }
            } else if (keyCode == KeyEvent.KEYCODE_MEDIA_FAST_FORWARD
                    || keyCode == KeyEvent.KEYCODE_MEDIA_NEXT) {
                if (webView != null) {
                    webView.evaluateJavascript("window.handleTvMediaKey ? window.handleTvMediaKey('fast_forward') : false;", null);
                    return true;
                }
            }
        }

        return super.dispatchKeyEvent(event);
    }

    private void showExitConfirmDialog() {
        new AlertDialog.Builder(this, AlertDialog.THEME_DEVICE_DEFAULT_DARK)
                .setTitle("Exit SDK Movies")
                .setMessage("Are you sure you want to close the app?")
                .setPositiveButton("Exit", (dialog, which) -> finish())
                .setNegativeButton("Cancel", (dialog, which) -> dialog.dismiss())
                .show();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) {
            webView.onResume();
        }
        setupImmersiveMode();
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (webView != null) {
            webView.onPause();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
        }
        super.onDestroy();
    }
}
