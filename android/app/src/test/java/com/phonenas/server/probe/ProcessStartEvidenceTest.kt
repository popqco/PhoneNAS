package com.phonenas.server.probe

import org.junit.Assert.*
import org.junit.Test

class ProcessStartEvidenceTest {
    private val self = ProcessStartEvidence.Identity(42, 10321, "com.phonenas.server.probe", 1_000, 1_020_500_000)
    private fun record(pid: Int = 0, onCreate: Long = 1_020_000_000, forced: Boolean = false,
                       uid: Int = 10321, process: String = self.processName, cold: Boolean = true) =
        ProcessStartEvidence.Record(pid, uid, uid, process, cold, onCreate, forced)

    @Test fun coldStartWithRedactedPidMustMatchThisApplicationsOnCreateInterval() {
        assertEquals(RecoveryPolicy.Observation.NORMAL, ProcessStartEvidence.select(self, listOf(record())))
        assertEquals(RecoveryPolicy.Observation.FORCE_STOPPED,
            ProcessStartEvidence.select(self, listOf(record(forced = true))))
    }
    @Test fun unrelatedRecentOrPreviousRecordsCannotGrantAutomaticRecovery() {
        val candidates = listOf(record(onCreate = 999_999_999), record(onCreate = 1_020_500_001),
            record(uid = 10322), record(process = "other"), record(cold = false))
        candidates.forEach { assertEquals(RecoveryPolicy.Observation.UNKNOWN, ProcessStartEvidence.select(self, listOf(it))) }
    }
    @Test fun missingOrAmbiguousOnCreateEvidenceStaysUnknown() {
        assertEquals(RecoveryPolicy.Observation.UNKNOWN, ProcessStartEvidence.select(self, emptyList()))
        assertEquals(RecoveryPolicy.Observation.UNKNOWN, ProcessStartEvidence.select(self, listOf(record(onCreate = 0))))
        assertEquals(RecoveryPolicy.Observation.UNKNOWN, ProcessStartEvidence.select(self,
            listOf(record(), record(onCreate = 1_010_000_000))))
    }
    @Test fun exactCurrentPidAlsoRequiresIdentityAndRejectsConflictingEvidence() {
        assertEquals(RecoveryPolicy.Observation.NORMAL, ProcessStartEvidence.select(self, listOf(record(pid = 42))))
        assertEquals(RecoveryPolicy.Observation.UNKNOWN, ProcessStartEvidence.select(self, listOf(record(pid = 41))))
        assertEquals(RecoveryPolicy.Observation.UNKNOWN, ProcessStartEvidence.select(self,
            listOf(record(pid = 42), record(forced = true))))
    }
}
