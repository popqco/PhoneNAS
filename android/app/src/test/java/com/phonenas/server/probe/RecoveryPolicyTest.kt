package com.phonenas.server.probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RecoveryPolicyTest {
    @Test fun forceStopThenLauncherBootRedeliveryRequiresManualStart() {
        val policy = RecoveryPolicy(12, null, RecoveryPolicy.Observation.FORCE_STOPPED)
        assertFalse(policy.admitBoot(12, desired = true, hasError = false))
        assertFalse(policy.admitService(RecoveryPolicy.Request.STICKY, 12, true, false))
        assertEquals(RecoveryPolicy.Reason.FORCE_STOPPED, policy.blocked)

        policy.userStart(12)
        assertNull(policy.blocked)
        assertTrue(policy.admitService(RecoveryPolicy.Request.USER, 12, true, false))
        // The cached process observation must not re-block this explicitly started process.
        assertTrue(policy.admitService(RecoveryPolicy.Request.STICKY, 12, true, false))
        assertFalse(policy.admitBoot(12, true, false))
        assertEquals(RecoveryPolicy.Reason.SAME_BOOT, policy.reason)
    }

    @Test fun actualNewBootHasOnePermissionButAdmittedRestoreStillRuns() {
        val policy = RecoveryPolicy(12, null, RecoveryPolicy.Observation.NORMAL)
        assertTrue(policy.admitBoot(13, true, false, consume = false))
        assertEquals(12, policy.generation)
        assertTrue(policy.admitBoot(13, true, false))
        assertEquals(13, policy.generation)
        assertFalse(policy.admitBoot(13, true, false))
        assertTrue(policy.admitService(RecoveryPolicy.Request.RESTORE, 13, true, false))
        assertFalse(policy.admitService(RecoveryPolicy.Request.RESTORE, 13, true, false))
    }

    @Test fun directRestoreCannotForgeOrReuseTheReceiverPermission() {
        val policy = RecoveryPolicy(12, null, RecoveryPolicy.Observation.NORMAL)
        assertFalse(policy.admitService(RecoveryPolicy.Request.RESTORE, 13, true, false))
        assertFalse(policy.admitService(RecoveryPolicy.Request.STICKY, 13, true, false))
        assertTrue(policy.admitBoot(13, true, false))
        // The generation is durable, but an in-memory handoff is deliberately not replayable.
        val rebuilt = RecoveryPolicy(policy.generation, null, RecoveryPolicy.Observation.NORMAL)
        assertFalse(rebuilt.admitService(RecoveryPolicy.Request.RESTORE, 13, true, false))
    }

    @Test fun unknownObservationAndItsPersistentBlockNeedExplicitStart() {
        val unknown = RecoveryPolicy(12, null, RecoveryPolicy.Observation.UNKNOWN)
        assertFalse(unknown.admitBoot(13, true, false))
        assertFalse(unknown.admitService(RecoveryPolicy.Request.STICKY, 12, true, false))
        val rebuilt = RecoveryPolicy(12, unknown.blocked, RecoveryPolicy.Observation.NORMAL)
        assertFalse(rebuilt.admitBoot(13, true, false))
        rebuilt.userStart(13)
        assertTrue(rebuilt.admitService(RecoveryPolicy.Request.USER, 13, true, false))
    }

    @Test fun missingOrRegressedGenerationNeverEstablishesAnAutomaticBaseline() {
        val legacy = RecoveryPolicy(null, null, RecoveryPolicy.Observation.LEGACY)
        assertFalse(legacy.admitBoot(13, true, false))
        assertNull(legacy.generation)
        assertTrue(legacy.blocked != null)
        legacy.userStart(13)
        assertTrue(legacy.admitService(RecoveryPolicy.Request.USER, 13, true, false))
        assertFalse(legacy.admitBoot(13, true, false))
        assertFalse(legacy.admitBoot(12, true, false))
        assertFalse(legacy.admitBoot(null, true, false))
        assertTrue(legacy.admitBoot(14, true, false))
    }

    @Test fun legacyUsesBootGenerationWithoutClaimingForceStopClassification() {
        val legacy = RecoveryPolicy(12, null, RecoveryPolicy.Observation.LEGACY)
        assertNull(legacy.blocked)
        assertFalse(legacy.admitBoot(12, true, false))
        assertTrue(legacy.admitService(RecoveryPolicy.Request.STICKY, 12, true, false))
        assertTrue(legacy.admitBoot(13, true, false))
        assertTrue(legacy.admitService(RecoveryPolicy.Request.RESTORE, 13, true, false))
    }

    @Test fun stopAndPersistentErrorWinWithoutConsumingNewBootPermission() {
        val policy = RecoveryPolicy(12, null, RecoveryPolicy.Observation.NORMAL)
        assertFalse(policy.admitBoot(13, false, false))
        assertFalse(policy.admitBoot(13, true, true))
        assertEquals(12, policy.generation)
        assertTrue(policy.admitBoot(13, true, false))
        policy.userStop()
        assertFalse(policy.admitService(RecoveryPolicy.Request.RESTORE, 13, true, false))
        assertFalse(policy.admitService(RecoveryPolicy.Request.STICKY, 13, false, false))
        assertFalse(policy.admitService(RecoveryPolicy.Request.STICKY, 13, true, true))
        assertFalse(policy.admitService(RecoveryPolicy.Request.USER, 13, true, false))
        policy.userStart(13)
        assertTrue(policy.admitService(RecoveryPolicy.Request.USER, 13, true, false))
    }

    @Test fun missingCurrentGenerationRequiresManualButManualCanStillStart() {
        val policy = RecoveryPolicy(12, null, RecoveryPolicy.Observation.NORMAL)
        assertFalse(policy.admitService(RecoveryPolicy.Request.STICKY, null, true, false))
        policy.userStart(null)
        assertTrue(policy.admitService(RecoveryPolicy.Request.USER, null, true, false))
        assertFalse(policy.admitService(RecoveryPolicy.Request.STICKY, null, true, false))
    }

    @Test fun realServiceRecoveryBudgetIsFiniteAndUsesExistingBackoff() {
        assertEquals(1, RecoveryPolicy.nextRecovery(0))
        assertEquals(2, RecoveryPolicy.nextRecovery(1))
        assertEquals(3, RecoveryPolicy.nextRecovery(2))
        assertNull(RecoveryPolicy.nextRecovery(3))
        assertNull(RecoveryPolicy.nextRecovery(4))
        assertNull(RecoveryPolicy.nextRecovery(-1))
        assertEquals(2L, RecoveryPolicy.recoveryDelaySeconds(1))
        assertEquals(5L, RecoveryPolicy.recoveryDelaySeconds(2))
        assertEquals(15L, RecoveryPolicy.recoveryDelaySeconds(3))
    }
}
