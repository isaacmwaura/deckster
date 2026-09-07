package com.streamcontrol.shell

import android.app.Application
import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** A PC the app can connect to (discovered on Wi-Fi, scanned, or typed). */
data class Pc(val name: String, val url: String, val fingerprint: String)

sealed interface UiState {
    data object Searching : UiState                                  // probing USB
    data class NeedConnect(val devices: List<Pc>) : UiState          // no USB — offer Wi-Fi
    data class Connected(val url: String, val fingerprint: String) : UiState
}

/**
 * Drives the connect flow: try USB first (the primary, secure path), and if that's not
 * present, discover the PC on Wi-Fi via mDNS (`_streamctl._tcp`) while offering QR scan
 * and manual entry. Once a URL is chosen the shell loads it in the WebView.
 */
class ConnectViewModel(app: Application) : AndroidViewModel(app) {

    private val port = 8765
    private val _state = MutableStateFlow<UiState>(UiState.Searching)
    val state: StateFlow<UiState> = _state.asStateFlow()

    private val nsd = app.getSystemService(Context.NSD_SERVICE) as NsdManager
    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private val found = LinkedHashMap<String, Pc>()
    private val store = Store(app)

    init { probeUsbThenSearch() }

    fun retryUsb() = probeUsbThenSearch()

    /** Leave the current connection and return to the connect flow (the "Back" action). */
    fun disconnect() = probeUsbThenSearch()

    fun connect(pc: Pc) {
        stopDiscovery()
        store.rememberPcFromUrl(pc.url, pc.fingerprint, pc.name)
        _state.value = UiState.Connected(pc.url, pc.fingerprint)
    }

    /** From a scanned QR or a typed address (no pinned fingerprint on this path yet). */
    fun connectUrl(url: String, fingerprint: String = "") {
        stopDiscovery()
        val norm = normalize(url)
        store.rememberPcFromUrl(norm, fingerprint, "PC")   // query (?pair=) is ignored
        _state.value = UiState.Connected(norm, fingerprint)
    }

    /**
     * Seek a connection with no user action, in order of reliability:
     *   1. wired USB (adb reverse -> localhost),
     *   2. the PC we used last time, probed directly (plain-HTTP LAN),
     *   3. mDNS discovery — auto-connecting a trusted PC (fingerprint match), else
     *      listing what's found on the connect screen.
     * Only when none of these land does the user see Scan-QR / manual entry. Once a
     * PC has been set up, opening the app just reconnects — no re-scan.
     */
    private fun probeUsbThenSearch() {
        _state.value = UiState.Searching
        viewModelScope.launch {
            if (withContext(Dispatchers.IO) { Net.reachable("http://localhost:$port/health") }) {
                _state.value = UiState.Connected("http://localhost:$port/", ""); return@launch
            }
            val last = store.lastPc()
            if (last != null && last.url.startsWith("http://")) {   // https can't be probed w/o the pin
                val health = last.url.trimEnd('/') + "/health"
                if (withContext(Dispatchers.IO) { Net.reachable(health) }) {
                    connect(last); return@launch
                }
            }
            found.clear()
            _state.value = UiState.NeedConnect(found.values.toList())
            startDiscovery()
        }
    }

    private fun normalize(u: String): String {
        val s = u.trim()
        return if (s.startsWith("http://") || s.startsWith("https://")) s else "http://$s"
    }

    // ---- mDNS discovery ------------------------------------------------------
    private fun startDiscovery() {
        if (discoveryListener != null) return
        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {}
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
            override fun onServiceLost(service: NsdServiceInfo) {}
            override fun onServiceFound(service: NsdServiceInfo) = resolve(service)
        }
        discoveryListener = listener
        try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            discoveryListener = null            // discovery is optional; QR/manual still work
        }
    }

    private fun resolve(service: NsdServiceInfo) {
        val rl = object : NsdManager.ResolveListener {
            override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {}
            override fun onServiceResolved(info: NsdServiceInfo) {
                val host = info.host?.hostAddress ?: return
                val attrs = info.attributes
                val secure = attrs["secure"]?.toString(Charsets.UTF_8) == "1"
                val fp = attrs["fp"]?.toString(Charsets.UTF_8) ?: ""
                val scheme = if (secure) "https" else "http"
                val pc = Pc(info.serviceName ?: "PC", "$scheme://$host:${info.port}/", fp)
                // A trusted PC (its pinned fingerprint matches the one we remember)
                // reappearing on the network -> reconnect automatically, no tap.
                val knownFp = store.lastPcFingerprint()
                if (knownFp.isNotEmpty() && fp == knownFp) { connect(pc); return }
                found[pc.url] = pc
                if (_state.value is UiState.NeedConnect || _state.value is UiState.Searching) {
                    _state.value = UiState.NeedConnect(found.values.toList())
                }
            }
        }
        try { nsd.resolveService(service, rl) } catch (e: Exception) {}
    }

    private fun stopDiscovery() {
        discoveryListener?.let { l -> try { nsd.stopServiceDiscovery(l) } catch (e: Exception) {} }
        discoveryListener = null
    }

    override fun onCleared() = stopDiscovery()

    companion object { private const val SERVICE_TYPE = "_streamctl._tcp." }
}
