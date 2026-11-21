package com.phonenas.server.probe

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.Collections

/**
 * A conservative address observation, NOT proof of VPN owner, Tailscale identity or peer trust.
 * The service must re-sample and close its remote listener when the pinned address stops matching.
 */
object TailnetState {
    data class Snapshot(
        val ready: Boolean = false,
        val ipv4: String? = null,
        val interfaceName: String? = null,
        val reason: String = "尚未观察到唯一可用的 VPN 地址。",
    ) {
        fun matchesPinnedIp(ip: String): Boolean = ready && isTailnetIpv4(ip) && ip == ipv4
    }

    data class VpnLink(val interfaceName: String?, val ipv4: List<String>)
    data class InterfaceLink(val name: String, val ipv4: List<String>, val up: Boolean)

    /** Numeric literal only: no DNS lookup, whitespace, alternate bases or IPv6. */
    fun isTailnetIpv4(ip: String): Boolean {
        val parts = ip.split('.')
        if (parts.size != 4 || parts.any { part ->
            part.isEmpty() || part.length > 3 || part.any { it !in '0'..'9' } ||
                (part.length > 1 && part[0] == '0')
        }) return false
        val bytes = parts.map { it.toIntOrNull() ?: return false }
        return bytes.all { it in 0..255 } && bytes[0] == 100 && bytes[1] in 64..127
    }

    /** The same public seam is used by real Android samples and unit tests of ambiguity. */
    fun evaluate(vpns: List<VpnLink>, interfaces: List<InterfaceLink>): Snapshot {
        if (vpns.size != 1) return Snapshot(reason = "未发现唯一 VPN；缺失或存在多个 VPN。")
        val vpn = vpns.single()
        val name = vpn.interfaceName?.takeIf { it.isNotBlank() }
            ?: return Snapshot(reason = "VPN 接口不可确定。")
        val assigned = vpn.ipv4.filter(::isTailnetIpv4)
        if (assigned.size != 1) return Snapshot(reason = "VPN 没有唯一已分配的 100.64/10 IPv4。")
        val ip = assigned.single()
        val owners = interfaces.filter { ip in it.ipv4 }
        if (owners.size != 1 || owners.single().name != name || !owners.single().up ||
            owners.single().ipv4.filter(::isTailnetIpv4) != listOf(ip)) {
            return Snapshot(reason = "VPN 与实时网络接口的地址不一致或有歧义。")
        }
        return Snapshot(true, ip, name, "仅确认 VPN 接口及固定 IP 一致；未验证 VPN 所有者。")
    }

    fun sample(context: Context): Snapshot = try {
        val manager = context.getSystemService(ConnectivityManager::class.java)
            ?: return Snapshot(reason = "无法读取系统网络状态。")
        val vpns = ArrayList<VpnLink>()
        for (network in manager.allNetworks) {
            val capabilities = manager.getNetworkCapabilities(network)
                ?: return Snapshot(reason = "网络状态变化中，无法确认 VPN。")
            if (capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
                val properties = manager.getLinkProperties(network)
                    ?: return Snapshot(reason = "VPN 地址尚不可读。")
                vpns.add(VpnLink(properties.interfaceName,
                    properties.linkAddresses.mapNotNull { link ->
                        (link.address as? Inet4Address)?.hostAddress
                    }))
            }
        }
        val enumeration = NetworkInterface.getNetworkInterfaces()
            ?: return Snapshot(reason = "无法枚举实时网络接口。")
        val interfaces = Collections.list(enumeration).map { device ->
            InterfaceLink(device.name, Collections.list(device.inetAddresses)
                .mapNotNull { (it as? Inet4Address)?.hostAddress }, device.isUp)
        }
        evaluate(vpns, interfaces)
    } catch (_: Exception) {
        Snapshot(reason = "网络状态权限不足、接口变化或采样失败；远端不可用。")
    }
}
