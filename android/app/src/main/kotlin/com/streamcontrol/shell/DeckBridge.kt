package com.streamcontrol.shell

import android.webkit.JavascriptInterface

/**
 * Exposed to the web control surface as `window.AndroidBridge`. It lets the page
 * store its pairing token and device id in the app's own storage instead of the
 * WebView's per-origin localStorage, so the token survives across origins (USB vs
 * Wi-Fi), restarts, and network changes — the phone re-authenticates silently and
 * never has to re-scan a QR once paired. Only ever exposed to our own agent page.
 */
class DeckBridge(private val store: Store) {

    @JavascriptInterface
    fun getToken(): String = store.token

    @JavascriptInterface
    fun saveToken(token: String) { if (token.isNotBlank()) store.token = token }

    @JavascriptInterface
    fun getDeviceId(): String = store.deviceId()

    @JavascriptInterface
    fun setDeviceId(id: String) { store.seedDeviceId(id) }
}
