package com.phonenas.server.probe

/** A missing VPN permits loopback hosting, never a substitute remote address. */
internal object HostConnection {
    data class Target(val remoteAddress: String?, val waitingForVpn: Boolean)

    fun target(configuration: HostConfiguration, observation: TailnetState.Snapshot): Target {
        val pin = configuration.remoteIp ?: return Target(null, false)
        return if (observation.matchesPinnedIp(pin)) Target("$pin:18889", false)
            else Target(null, true)
    }
}
