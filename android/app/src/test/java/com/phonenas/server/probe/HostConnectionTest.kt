package com.phonenas.server.probe

import org.junit.Assert.*
import org.junit.Test

class HostConnectionTest {
    @Test fun missingVpnWaitsLocallyWithoutChangingTheSavedPin() {
        val config = HostConfiguration(remoteIp = "100.127.10.41")
        val target = HostConnection.target(config, TailnetState.Snapshot())
        assertNull(target.remoteAddress)
        assertTrue(target.waitingForVpn)
        assertEquals("100.127.10.41", config.remoteIp)
    }

    @Test fun aDifferentVpnAddressCannotBecomeTheRemoteListener() {
        val target = HostConnection.target(HostConfiguration(remoteIp = "100.127.10.41"),
            TailnetState.Snapshot(true, "100.90.2.3", "tun0"))
        assertNull(target.remoteAddress)
        assertTrue(target.waitingForVpn)
    }

    @Test fun onlyTheExactConfirmedVpnAddressEnablesHttps() {
        val target = HostConnection.target(HostConfiguration(remoteIp = "100.127.10.41"),
            TailnetState.Snapshot(true, "100.127.10.41", "tun0"))
        assertEquals("100.127.10.41:18889", target.remoteAddress)
        assertFalse(target.waitingForVpn)
    }

    @Test fun observingAVpnNeverEnablesRemoteAccessByItself() {
        val target = HostConnection.target(HostConfiguration(),
            TailnetState.Snapshot(true, "100.127.10.41", "tun0"))
        assertNull(target.remoteAddress)
        assertFalse(target.waitingForVpn)
    }
}
