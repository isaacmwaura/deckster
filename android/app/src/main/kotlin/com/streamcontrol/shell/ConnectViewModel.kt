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

    init { probeUsbThenSearch() }

    fun retryUsb() = probeUsbThenSearch()

    /** Leave the current connection and return to the connect flow (the "Back" action). */
    fun disconnect() = probeUsbThenSearch()

    fun connect(pc: Pc) {
        stopDiscovery()
        _state.value = UiState.Connected(pc.url, pc.fingerprint)
    }

    /** From a scanned QR or a typed address (no pinned fingerprint on this path yet). */
    fun connectUrl(url: String, fingerprint: String = "") {
        stopDiscovery()
        _state.value = UiState.Connected(normalize(url), fingerprint)
    }

    private fun probeUsbThenSearch() {
        _state.value = UiState.Searching
        viewModelScope.launch {
            val usb = withContext(Dispatchers.IO) {
                Net.reachable("http://localhost:$port/health")
            }
            if (usb) {
                _state.value = UiState.Connected("http://localhost:$port/", "")
            } else {
                _state.value = UiState.NeedConnect(found.values.toList())
                startDiscovery()
            }
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
