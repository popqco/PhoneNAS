package com.phonenas.server.probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class StartupHandoffTest {
    @Test fun admittedBootSurvivesReceiverProcessDeathAndIsDeliveredOnlyOnce() {
        val disk = PreferenceDisk(mapOf("guard_generation" to 12))
        val receiver = guard(disk, 13)
        assertTrue(receiver.canReceiveBoot())
        assertTrue(receiver.admitBoot(desired = true, hasError = false))

        val serviceProcess = guard(disk, 13)

        assertFalse("Same boot broadcast never issues a second grant", serviceProcess.canReceiveBoot())
        assertTrue("A committed handoff must reach Service after receiver process death",
            serviceProcess.admitService(RecoveryPolicy.Request.RESTORE, true, false))
        assertFalse(serviceProcess.admitService(RecoveryPolicy.Request.RESTORE, true, false))
        assertFalse(guard(disk, 13).admitService(RecoveryPolicy.Request.RESTORE, true, false))
    }

    @Test fun actualNewBootResetsExhaustedCrashBudgetOnceButSameBootDoesNot() {
        val boot = PreferenceDisk(mapOf("guard_generation" to 12))
        val ce = PreferenceDisk(mapOf("desired_running" to true, "unclean_engine" to true,
            "recoveries" to 3, "recovery_generation" to 12))
        assertTrue(guard(boot, 13).admitBoot(true, false))
        val serviceProcess = guard(boot, 13)
        assertTrue(serviceProcess.admitService(RecoveryPolicy.Request.RESTORE, true, false))
        val credential = ce.open()
        val state = NasPersistence(boot.open(), { credential }, ProcessStartBlock())

        assertTrue(state.prepareRecoveryBudget(serviceProcess.status().generation))
        assertEquals("A running dirty marker from the previous boot is not a same-boot crash", 0, state.recoveries)
        assertFalse("A confirmed new boot consumes the prior boot's dirty marker", state.dirty)
        assertTrue(state.markDirty(true))
        assertTrue(state.recordRecovery(1))
        val nextProcess = NasPersistence(boot.open(), { ce.open() }, ProcessStartBlock())
        assertTrue(nextProcess.prepareRecoveryBudget(13))
        assertEquals("Same-boot sticky recreation keeps the finite budget", 1, nextProcess.recoveries)
        assertFalse(guard(boot, 13).admitBoot(true, false))
    }

    @Test fun failedGuardCommitIsVisibleAndNeverGrantsServiceAdmission() {
        val boot = PreferenceDisk(mapOf("guard_generation" to 12), throwsOnCommit = true)
        val guarded = guard(boot, 13)
        assertTrue(guarded.status().blocked)
        assertFalse(guarded.canReceiveBoot())
        assertFalse(guarded.admitService(RecoveryPolicy.Request.STICKY, true, false))
    }

    @Test fun forceStopInvalidatesDurableHandoffAndStickyUntilExplicitStart() {
        val boot = PreferenceDisk(mapOf("guard_generation" to 12))
        assertTrue(guard(boot, 13).admitBoot(true, false))
        val stopped = guard(boot, 13, RecoveryPolicy.Observation.FORCE_STOPPED)
        assertFalse(stopped.admitService(RecoveryPolicy.Request.RESTORE, true, false))
        assertFalse(stopped.admitService(RecoveryPolicy.Request.STICKY, true, false))
        assertFalse(guard(boot, 13).admitService(RecoveryPolicy.Request.STICKY, true, false))
        assertTrue(stopped.userStart())
        assertTrue(stopped.admitService(RecoveryPolicy.Request.USER, true, false))
        assertTrue(guard(boot, 13).admitService(RecoveryPolicy.Request.STICKY, true, false))
        assertFalse(guard(boot, 13).canReceiveBoot())
    }

    @Test fun lockedBootDoesNotReadGenerationOrAdmitAnyService() {
        val guarded = StartupGuard(PreferenceDisk().open(), RecoveryPolicy.Observation.NORMAL,
            { false }, { throw AssertionError("Locked start read boot generation") })
        guarded.lockedBoot()
        assertFalse(guarded.canReceiveBoot())
        assertFalse(guarded.admitService(RecoveryPolicy.Request.STICKY, true, false))
    }

    private fun guard(disk: PreferenceDisk, generation: Int,
        observation: RecoveryPolicy.Observation = RecoveryPolicy.Observation.NORMAL,
        unlocked: Boolean = true) = StartupGuard(disk.open(), observation, { unlocked }, { generation })
}
