package com.streamcontrol.shell

import android.net.http.SslCertificate
import android.os.Build
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/** Small networking + certificate helpers shared by the shell. */
object Net {

    /** Cancels an in-flight health request when a newer choice or pause owns the flow. */
    class Probe {
        @Volatile private var cancelled = false
        private var connection: HttpURLConnection? = null
        @Synchronized fun attach(value: HttpURLConnection): Boolean {
            if (cancelled) { value.disconnect(); return false }
            connection = value
            return true
        }
        @Synchronized fun detach(value: HttpURLConnection) { if (connection === value) connection = null }
        @Synchronized fun cancel() { cancelled = true; connection?.disconnect(); connection = null }
    }

    enum class PinnedProbe { VERIFIED, UNAVAILABLE, PIN_MISMATCH, INVALID_RESPONSE }

    private fun decksterHealth(connection: HttpURLConnection): Boolean {
        if (connection.responseCode !in 200..299) return false
        val body = connection.inputStream.bufferedReader().use { it.readText() }
        val json = JSONObject(body)
        return json.optBoolean("ok") && json.optString("app") == "deckster"
    }

    /** True only when the health response identifies the Deckster agent. */
    fun reachable(url: String, timeoutMs: Int = 800, probe: Probe? = null): Boolean = try {
        (URL(url).openConnection() as HttpURLConnection).run {
            if (probe != null && !probe.attach(this)) return false
            connectTimeout = timeoutMs
            readTimeout = timeoutMs
            requestMethod = "GET"
            try { decksterHealth(this) } finally { probe?.detach(this); disconnect() }
        }
    } catch (e: Exception) {
        false
    }

    /** Probe a self-signed Deckster over USB with the previously saved certificate pin. */
    fun reachablePinned(url: String, fingerprint: String, timeoutMs: Int = 800, probe: Probe? = null): PinnedProbe {
        val expected = fingerprint.replace(":", "").uppercase()
        if (fingerprint.isBlank()) return PinnedProbe.UNAVAILABLE
        if (!expected.matches(Regex("[0-9A-F]{64}"))) return PinnedProbe.PIN_MISMATCH
        var pinMismatch = false
        var pinValidated = false
        val trust = object : X509TrustManager {
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
            override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {
                throw CertificateException("client certificates are not accepted")
            }
            override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
                val actual = chain.firstOrNull()?.encoded?.let { bytes ->
                    MessageDigest.getInstance("SHA-256").digest(bytes)
                        .joinToString("") { "%02X".format(it) }
                }
                if (actual != expected) {
                    pinMismatch = true
                    throw CertificateException("Deckster certificate pin mismatch")
                }
                pinValidated = true
            }
        }
        return try {
            val context = SSLContext.getInstance("TLS")
            context.init(null, arrayOf(trust), null)
            val connection = URL(url).openConnection() as HttpsURLConnection
            if (probe != null && !probe.attach(connection)) return PinnedProbe.UNAVAILABLE
            connection.sslSocketFactory = context.socketFactory
            // The trusted PC certificate is pinned above; its LAN hostname can
            // differ from localhost when the same server is reached over USB.
            connection.hostnameVerifier = javax.net.ssl.HostnameVerifier { _, _ -> true }
            connection.connectTimeout = timeoutMs
            connection.readTimeout = timeoutMs
            connection.requestMethod = "GET"
            try {
                if (decksterHealth(connection)) PinnedProbe.VERIFIED else PinnedProbe.INVALID_RESPONSE
            } finally {
                probe?.detach(connection)
                connection.disconnect()
            }
        } catch (e: Exception) {
            when {
                pinMismatch -> PinnedProbe.PIN_MISMATCH
                pinValidated -> PinnedProbe.INVALID_RESPONSE
                else -> PinnedProbe.UNAVAILABLE
            }
        }
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
