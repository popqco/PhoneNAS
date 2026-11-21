package com.phonenas.client

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.RouteInfo
import android.os.Build
import java.io.Closeable
import java.net.Inet4Address
import javax.net.SocketFactory

internal interface CloudRoute : Closeable {
    val socketFactory: SocketFactory
    fun checkAvailable()
}

internal fun interface CloudRoutes {
    fun open(endpoint: CloudEndpoint, onLost: () -> Unit): CloudRoute
}

internal data class VpnRouteMatch(val prefixLength: Int, val reachable: Boolean)

internal object VpnRoutePolicy {
    fun permits(isVpn: Boolean, matchingRoutes: List<VpnRouteMatch>): Boolean {
        if (!isVpn) return false
        val longest = matchingRoutes.maxOfOrNull { it.prefixLength } ?: return false
        return longest > 0 && matchingRoutes.filter { it.prefixLength == longest }.singleOrNull()?.reachable == true
    }
}

internal class AndroidCloudRoutes(context: Context) : CloudRoutes {
    private val connections = context.getSystemService(ConnectivityManager::class.java)

    override fun open(endpoint: CloudEndpoint, onLost: () -> Unit): CloudRoute {
        val network = connections.activeNetwork ?: throw CloudFailure(CloudIssue.VPN)
        checkNetwork(network, endpoint)
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(newNetwork: Network) {
                if (network != newNetwork) onLost()
            }

            override fun onLost(lostNetwork: Network) {
                if (network == lostNetwork) onLost()
            }

            override fun onCapabilitiesChanged(current: Network, capabilities: NetworkCapabilities) {
                if (current == network && !capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) onLost()
            }

            override fun onLinkPropertiesChanged(current: Network, properties: LinkProperties) {
                if (current == network && !hasRoute(properties, endpoint)) onLost()
            }

            override fun onBlockedStatusChanged(current: Network, blocked: Boolean) {
                if (current == network && blocked) onLost()
            }
        }
        var registered = false
        try {
            connections.registerDefaultNetworkCallback(callback)
            registered = true
            checkNetwork(network, endpoint)
            return object : CloudRoute {
                override val socketFactory: SocketFactory = network.socketFactory
                override fun checkAvailable() = checkNetwork(network, endpoint)
                override fun close() { connections.unregisterNetworkCallback(callback) }
            }
        } catch (_: Exception) {
            if (registered) runCatching { connections.unregisterNetworkCallback(callback) }
            throw CloudFailure(CloudIssue.VPN)
        }
    }

    private fun checkNetwork(network: Network, endpoint: CloudEndpoint) {
        val allowed = runCatching {
            connections.activeNetwork == network &&
                connections.getNetworkCapabilities(network)?.hasTransport(NetworkCapabilities.TRANSPORT_VPN) == true &&
                connections.getLinkProperties(network)?.let { hasRoute(it, endpoint) } == true
        }.getOrDefault(false)
        if (!allowed) throw CloudFailure(CloudIssue.VPN)
    }

    private fun hasRoute(properties: LinkProperties, endpoint: CloudEndpoint): Boolean {
        val matching = properties.routes.distinct().filter {
            it.destination.address is Inet4Address && it.matches(endpoint.address)
        }.map {
            // VpnService exclusion (throw) routes were introduced with API 33.
            val reachable = Build.VERSION.SDK_INT < 33 || it.type == RouteInfo.RTN_UNICAST
            VpnRouteMatch(it.destination.prefixLength, reachable)
        }
        return VpnRoutePolicy.permits(true, matching)
    }
}
