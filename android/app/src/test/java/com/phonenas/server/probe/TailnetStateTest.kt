package com.phonenas.server.probe

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TailnetStateTest {
    @Test fun absentAmbiguousOrMismatchedVpnObservationsNeverBecomeReady() {
        val vpn = TailnetState.VpnLink("tun0", listOf("100.100.10.20"))
        val real = TailnetState.InterfaceLink("tun0", listOf("100.100.10.20"), true)
        val observations = listOf(
            emptyList<TailnetState.VpnLink>() to listOf(real),
            listOf(vpn, vpn.copy(interfaceName = "tun1")) to listOf(real),
            listOf(vpn.copy(interfaceName = null)) to listOf(real),
            listOf(vpn.copy(ipv4 = listOf("192.168.1.2"))) to listOf(real),
            listOf(vpn.copy(ipv4 = listOf("100.100.10.20", "100.100.10.21"))) to listOf(real),
            listOf(vpn) to emptyList<TailnetState.InterfaceLink>(),
            listOf(vpn) to listOf(real.copy(name = "wlan0")),
            listOf(vpn) to listOf(real.copy(up = false)),
            listOf(vpn) to listOf(real.copy(ipv4 = listOf("100.100.10.21"))),
            listOf(vpn) to listOf(real, real.copy(name = "tun1")),
        )
        observations.forEach { (vpns, interfaces) ->
            val state = TailnetState.evaluate(vpns, interfaces)
            assertFalse(state.ready)
            assertFalse(state.matchesPinnedIp("100.100.10.20"))
        }
    }

    @Test fun onlyCanonicalCarrierGradeNatIpv4LiteralsCanBePinned() {
        listOf("100.64.0.0", "100.127.255.255", "100.100.10.20").forEach {
            assertTrue(it, TailnetState.isTailnetIpv4(it))
        }
        listOf("100.63.255.255", "100.128.0.0", "100.64.0.256", "0.0.0.0", "127.0.0.1",
            "192.168.1.1", "::1", "100.100.010.20", "100.100.10.20:18889", "100.100.10.20 ",
            "100.100.10.20\n", "tailscale.invalid", "100.100.10", "100.100.10.20.1").forEach {
            assertFalse(it, TailnetState.isTailnetIpv4(it))
        }
    }

    @Test fun pinRequiresAnUnambiguousAssignedVpnAddressAndTheSameLiveInterface() {
        val state = TailnetState.evaluate(
            listOf(TailnetState.VpnLink("tun0", listOf("100.100.10.20"))),
            listOf(TailnetState.InterfaceLink("tun0", listOf("100.100.10.20"), true)))
        assertTrue(state.ready)
        assertTrue(state.matchesPinnedIp("100.100.10.20"))
        assertFalse(state.matchesPinnedIp("100.100.10.21"))
        assertFalse(state.matchesPinnedIp("100.100.010.20"))
    }
}
