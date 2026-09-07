package com.streamcontrol.shell

import android.content.Context
import java.util.UUID

/**
 * App-private persistence for the trusted connection: the pairing token + a stable
 * device id (so the phone re-authenticates silently across restarts, networks, and
 * origins), plus the last PC we connected to (so we can seek it and reconnect with
 * no QR scan). Stored in the app's own SharedPreferences — unlike the WebView's
 * per-origin localStorage, this survives switching between USB (localhost) and
 * Wi-Fi (LAN IP). (Future hardening: EncryptedSharedPreferences.)
 */
class Store(context: Context) {
    private val p = context.applicationContext.getSharedPreferences("deckster", Context.MODE_PRIVATE)

    var token: String
        get() = p.getString("token", "") ?: ""
        set(v) { p.edit().putString("token", v).apply() }

    /** Stable per-install device id; generated once and reused. */
    fun deviceId(): String {
        var id = p.getString("device_id", "") ?: ""
        if (id.isBlank()) {
            id = "dev-" + UUID.randomUUID().toString().replace("-", "").take(18)
            p.edit().putString("device_id", id).apply()
        }
        return id
    }

    fun seedDeviceId(id: String) {
        if ((p.getString("device_id", "") ?: "").isBlank() && id.isNotBlank()) {
            p.edit().putString("device_id", id).apply()
        }
    }

    /** Remember the PC we're connected to, for a no-scan reconnect next time. */
    fun rememberPc(host: String, port: Int, secure: Boolean, fingerprint: String, name: String) {
        p.edit()
            .putString("pc_host", host).putInt("pc_port", port)
            .putBoolean("pc_secure", secure).putString("pc_fp", fingerprint)
            .putString("pc_name", name).putLong("pc_seen", System.currentTimeMillis())
            .apply()
    }

    fun rememberPcFromUrl(url: String, fingerprint: String, name: String) {
        try {
            val u = java.net.URI(url)
            val secure = u.scheme == "https"
            val port = if (u.port > 0) u.port else if (secure) 443 else 8765
            rememberPc(u.host ?: return, port, secure, fingerprint, name)
        } catch (e: Exception) { /* best-effort */ }
    }

    fun lastPc(): Pc? {
        val host = p.getString("pc_host", null) ?: return null
        val port = p.getInt("pc_port", 8765)
        val secure = p.getBoolean("pc_secure", false)
        val fp = p.getString("pc_fp", "") ?: ""
        val name = p.getString("pc_name", "PC") ?: "PC"
        val scheme = if (secure) "https" else "http"
        return Pc(name, "$scheme://$host:$port/", fp)
    }

    fun lastPcFingerprint(): String = p.getString("pc_fp", "") ?: ""
}
