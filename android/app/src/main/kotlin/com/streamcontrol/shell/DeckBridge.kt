package com.streamcontrol.shell

import android.webkit.JavascriptInterface
import android.os.Build

/**
 * Exposed to the web control surface as `window.AndroidBridge`. It lets the page
 * store its pairing token and device id in the app's own storage instead of the
 * WebView's per-origin localStorage, so the token survives across origins (USB vs
 * Wi-Fi), restarts, and network changes — the phone re-authenticates silently and
 * never has to re-scan a QR once paired. Only ever exposed to our own agent page.
 *
 * @param onLost invoked (with the page's origin) when the page reports it can no
 *   longer reach the PC — used to offer a Wi-Fi handoff when a USB cable is pulled.
 *   Runs on the WebView's JS-bridge thread, so the handler must hop to the main
 *   thread before touching UI/state.
 */
class DeckBridge(
    private val store: Store,
    private val onLost: (String) -> Unit = {},
) {

    @JavascriptInterface
    fun getToken(): String = store.token

    @JavascriptInterface
    fun saveToken(token: String) { if (token.isNotBlank()) store.token = token }

    @JavascriptInterface
    fun getDeviceId(): String = store.deviceId()

    @JavascriptInterface
    fun getDeviceName(): String {
        val maker = Build.MANUFACTURER.orEmpty().trim()
        val model = Build.MODEL.orEmpty().trim()
        return when {
            model.isBlank() -> "Android phone"
            maker.isBlank() || model.startsWith(maker, ignoreCase = true) -> model
            else -> "$maker $model"
        }.take(48)
    }

    @JavascriptInterface
    fun setDeviceId(id: String) { store.seedDeviceId(id) }

    /** The page lost contact with the PC (WebSocket down past its retry budget). */
    @JavascriptInterface
    fun connectionLost(origin: String) { onLost(origin) }
}
