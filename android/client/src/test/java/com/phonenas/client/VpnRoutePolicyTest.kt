package com.phonenas.client

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VpnRoutePolicyTest {
    @Test fun requiresAnExplicitUnambiguousReachableVpnRoute() {
        assertFalse(VpnRoutePolicy.permits(false, listOf(VpnRouteMatch(10, true))))
        assertFalse(VpnRoutePolicy.permits(true, emptyList()))
        assertFalse(VpnRoutePolicy.permits(true, listOf(VpnRouteMatch(0, true))))
        assertFalse(VpnRoutePolicy.permits(true, listOf(VpnRouteMatch(10, true), VpnRouteMatch(32, false))))
        assertFalse(VpnRoutePolicy.permits(true, listOf(VpnRouteMatch(10, true), VpnRouteMatch(10, false))))
        assertTrue(VpnRoutePolicy.permits(true, listOf(VpnRouteMatch(0, false), VpnRouteMatch(10, true))))
    }
}
