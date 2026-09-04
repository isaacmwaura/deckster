package com.streamcontrol.shell

import android.net.http.SslCertificate
import android.os.Build
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/** Small networking + certificate helpers shared by the shell. */
object Net {

    /** True if a quick GET of [url] returns 2xx (used to probe the agent). */
    fun reachable(url: String, timeoutMs: Int = 800): Boolean = try {
        (URL(url).openConnection() as HttpURLConnection).run {
            connectTimeout = timeoutMs
            readTimeout = timeoutMs
            requestMethod = "GET"
            val ok = responseCode in 200..299
            disconnect()
            ok
        }
    } catch (e: Exception) {
        false
    }

    /**
     * SHA-256 fingerprint (uppercase colon-hex) of a certificate the WebView presented,
     * for pinning the agent's self-signed cert against the value from mDNS/QR.
     */
    fun certSha256(cert: SslCertificate): String? = try {
        val der: ByteArray? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            cert.x509Certificate?.encoded
        } else {
            SslCertificate.saveState(cert).getByteArray("x509-certificate")
        }
        der?.let { bytes ->
            MessageDigest.getInstance("SHA-256").digest(bytes)
                .joinToString(":") { "%02X".format(it) }
        }
    } catch (e: Exception) {
        null
    }
}
