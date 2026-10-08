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
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.activity.viewModels
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.key
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import com.streamcontrol.shell.ui.ConnectScreen
import com.streamcontrol.shell.ui.ErrorScreen

private val BG = Color(0xFF0B0E14)

/**
 * Thin native shell around the web control surface.
 * Compose hosts a native Connect screen (USB probe -> mDNS discovery / QR / manual)
 * and, once a URL is chosen, a fullscreen WebView. The activity is landscape-locked in
 * the manifest; here it adds immersive fullscreen and keep-awake.
 */
class MainActivity : AppCompatActivity() {
    private val vm: ConnectViewModel by viewModels()
    private var foreground by mutableStateOf(false)
    private var powerMode by mutableStateOf("mounted")

    private fun choosePowerMode(mode: String) {
        Store(this).powerMode = mode
        powerMode = Store(this).powerMode
        updateScreenPolicy()
    }

    private fun updateScreenPolicy() {
        if (foreground && powerMode == "mounted") window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    override fun onResume() {
        super.onResume()
        foreground = true
        vm.setForeground(true)
        updateScreenPolicy()
    }

    override fun onPause() {
        foreground = false
        vm.setForeground(false)
        updateScreenPolicy()
        super.onPause()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enterImmersive()
        powerMode = Store(this).powerMode
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(background = BG, surface = BG)) {
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
                        powerMode = powerMode,
                        onPowerMode = { choosePowerMode(it) },
                    )
                    is UiState.Connected ->
                        ShellWebView(
                            s.url, s.fingerprint,
                            onBack = { vm.disconnect() },
                            onLost = { origin -> vm.onConnectionLost(origin) },
                            foreground = foreground,
                            powerMode = powerMode,
                            onPowerMode = { mode -> runOnUiThread { choosePowerMode(mode) } },
                        )
                    is UiState.OfferWifi -> WifiHandoffDialog(
                        pcName = s.pc.name,
                        onAccept = { vm.acceptWifiHandoff() },
                        onDecline = { vm.declineWifiHandoff() },
                    )
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

/**
 * Shown when a wired (USB) session drops but the PC is still reachable on Wi-Fi.
 * Pulling the cable often just means "let me move the phone", so we offer to carry
 * the session over rather than dumping the user back at the connect screen.
 */
@Composable
private fun WifiHandoffDialog(pcName: String, onAccept: () -> Unit, onDecline: () -> Unit) {
    Box(Modifier.fillMaxSize().background(BG)) {
        AlertDialog(
            onDismissRequest = onDecline,
            title = { Text("USB disconnected") },
            text = { Text("“$pcName” is still on your Wi-Fi. Keep the connection going over Wi-Fi?") },
            confirmButton = { TextButton(onClick = onAccept) { Text("Reconnect over Wi-Fi") } },
            dismissButton = { TextButton(onClick = onDecline) { Text("Not now") } },
        )
    }
}

@SuppressLint("SetJavaScriptEnabled")
@Composable
private fun ShellWebView(url: String, fingerprint: String, onBack: () -> Unit, onLost: (String) -> Unit,
                         foreground: Boolean, powerMode: String, onPowerMode: (String) -> Unit) {
    var web by remember { mutableStateOf<WebView?>(null) }
    var error by remember { mutableStateOf(false) }
    var rendererGone by remember { mutableStateOf(false) }
    var rendererGeneration by remember { mutableStateOf(0) }
    val latestForeground by rememberUpdatedState(foreground)
    val latestPowerMode by rememberUpdatedState(powerMode)
    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(web, foreground, powerMode) {
        web?.let { applyPageLifecycle(it, foreground, powerMode) }
    }
    DisposableEffect(Unit) { onDispose { web?.let(::releaseWebView); web = null } }
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME || event == Lifecycle.Event.ON_PAUSE) {
                web?.let { applyPageLifecycle(it, event == Lifecycle.Event.ON_RESUME, latestPowerMode) }
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }
    BackHandler {
        val w = web
        when {
            error -> onBack()
            w != null && w.canGoBack() -> w.goBack()
            else -> onBack()
        }
    }
    Box(Modifier.fillMaxSize()) {
        if (!rendererGone) key(url, fingerprint, rendererGeneration) { AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { ctx ->
                WebView(ctx).apply {
                    web = this
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true             // localStorage holds the token
                    settings.mediaPlaybackRequiresUserGesture = false
                    settings.cacheMode = WebSettings.LOAD_DEFAULT
                    webChromeClient = WebChromeClient()
                    // Durable, origin-independent credentials so the page never has to
                    // re-scan a QR once paired (see DeckBridge / Store).
                    addJavascriptInterface(DeckBridge(Store(ctx), onLost = onLost, onPowerMode = onPowerMode), "AndroidBridge")
                    webViewClient = ShellClient(
                        fingerprint,
                        onError = { if (web === this) error = true },
                        onOk = { if (web === this) { error = false; applyPageLifecycle(this, latestForeground, latestPowerMode) } },
                        onRendererGone = { if (web === this) { rendererGone = true; error = true; web = null } },
                    )
                    loadUrl(url)
                }
            },
            onRelease = { released -> releaseWebView(released); if (web === released) web = null },
        ) }
        if (error) {
            ErrorScreen(
                onRetry = {
                    error = false
                    if (rendererGone) { rendererGeneration++; rendererGone = false }
                    else web?.reload()
                },
                onBack = onBack,
            )
        }
    }
}

private fun applyPageLifecycle(view: WebView, foreground: Boolean, powerMode: String) {
    if (foreground) view.onResume()
    view.evaluateJavascript("window.DecksterLifecycle && window.DecksterLifecycle({foreground:$foreground,powerMode:'$powerMode'});", null)
    if (!foreground) view.onPause()
}

private fun releaseWebView(view: WebView) {
    if (view.tag == "deckster-released") return
    view.tag = "deckster-released"
    (view.parent as? android.view.ViewGroup)?.removeView(view)
    view.removeJavascriptInterface("AndroidBridge")
    view.stopLoading()
    view.webChromeClient = null
    view.webViewClient = WebViewClient()
    view.destroy()
}

/** WebView policy: pinned-TLS acceptance + a native error screen (no browser page). */
private class ShellClient(
    private val fingerprint: String,
    private val onError: () -> Unit,
    private val onOk: () -> Unit,
    private val onRendererGone: () -> Unit,
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

    override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
        releaseWebView(view)
        onRendererGone()
        return true
    }
}
