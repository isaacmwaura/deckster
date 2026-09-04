package com.streamcontrol.shell

import android.annotation.SuppressLint
import android.content.Intent
import android.net.http.SslError
import android.os.Bundle
import android.view.WindowManager
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import com.streamcontrol.shell.ui.ConnectScreen
import com.streamcontrol.shell.ui.ErrorScreen

private val BG = Color(0xFF0B0E14)

/**
 * Thin native shell around the web control surface (see docs/android-brief.md).
 * Compose hosts a native Connect screen (USB probe -> mDNS discovery / QR / manual)
 * and, once a URL is chosen, a fullscreen WebView. The activity is landscape-locked in
 * the manifest; here it adds immersive fullscreen and keep-awake.
 */
class MainActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enterImmersive()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(background = BG, surface = BG)) {
                val vm: ConnectViewModel = viewModel()
                val state by vm.state.collectAsState()
                val scan = rememberLauncherForActivityResult(
                    ActivityResultContracts.StartActivityForResult()
                ) { res ->
                    val url = res.data?.getStringExtra(QrScannerActivity.EXTRA_URL)
                    if (!url.isNullOrBlank()) vm.connectUrl(url)
                }
                when (val s = state) {
                    is UiState.Searching ->
                        androidx.compose.foundation.layout.Box(Modifier.fillMaxSize().background(BG))
                    is UiState.NeedConnect -> ConnectScreen(
                        devices = s.devices,
                        onPick = { vm.connect(it) },
                        onScan = { scan.launch(Intent(this, QrScannerActivity::class.java)) },
                        onManual = { vm.connectUrl(it) },
                        onRetry = { vm.retryUsb() },
                    )
                    is UiState.Connected ->
                        ShellWebView(s.url, s.fingerprint, onBack = { vm.disconnect() })
                }
            }
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) enterImmersive()
    }

    private fun enterImmersive() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        WindowInsetsControllerCompat(window, window.decorView).apply {
            hide(WindowInsetsCompat.Type.systemBars())
            systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }
    }
}

@SuppressLint("SetJavaScriptEnabled")
@Composable
private fun ShellWebView(url: String, fingerprint: String, onBack: () -> Unit) {
    var web by remember { mutableStateOf<WebView?>(null) }
    var error by remember { mutableStateOf(false) }
    BackHandler {
        val w = web
        when {
            error -> onBack()
            w != null && w.canGoBack() -> w.goBack()
            else -> onBack()
        }
    }
    Box(Modifier.fillMaxSize()) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { ctx ->
                WebView(ctx).apply {
                    web = this
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true             // localStorage holds the token
                    settings.mediaPlaybackRequiresUserGesture = false
                    settings.cacheMode = WebSettings.LOAD_DEFAULT
                    webChromeClient = WebChromeClient()
                    webViewClient = ShellClient(
                        fingerprint,
                        onError = { error = true },
                        onOk = { error = false },
                    )
                    loadUrl(url)
                }
            },
        )
        if (error) {
            ErrorScreen(
                onRetry = { error = false; web?.reload() },
                onBack = onBack,
            )
        }
    }
}

/** WebView policy: pinned-TLS acceptance + a native error screen (no browser page). */
private class ShellClient(
    private val fingerprint: String,
    private val onError: () -> Unit,
    private val onOk: () -> Unit,
) : WebViewClient() {
    private var failed = false

    override fun onPageStarted(view: WebView, url: String?, favicon: android.graphics.Bitmap?) {
        failed = false
    }

    override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
        // Accept the agent's self-signed cert ONLY when it matches the pinned fingerprint
        // (from mDNS). No fingerprint -> fail closed, so a stranger's cert is never trusted.
        val fp = Net.certSha256(error.certificate)
        if (fingerprint.isNotEmpty() && fp != null && fp.equals(fingerprint, ignoreCase = true)) {
            handler.proceed()
        } else {
            handler.cancel()
        }
    }

    override fun onReceivedError(
        view: WebView, request: WebResourceRequest, error: WebResourceError
    ) {
        if (request.isForMainFrame) { failed = true; onError() }   // show the native error screen
    }

    override fun onPageFinished(view: WebView, url: String?) {
        if (!failed) onOk()                                        // a clean load clears the error
    }
}
