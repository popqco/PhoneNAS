package com.phonenas.server.probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class NasPersistenceTest {
    @Test fun stopStillClosesWhenCredentialCommitFails() {
        val boot = PreferenceDisk(mapOf("desired_running" to true))
        val ce = PreferenceDisk(mapOf("desired_running" to true), commits = false)
        val state = NasPersistence(boot.open(), { ce.open() }, ProcessStartBlock())
        var closed = false

        val saved = state.stop { closed = true }

        assertTrue("Stop must actually close even when saving user intent fails", closed)
        assertFalse(saved)
    }

    @Test fun deviceStopTombstoneBlocksStickyFromANewProcessDespiteStaleCredentialIntent() {
        val boot = PreferenceDisk(mapOf("desired_running" to true))
        val ce = PreferenceDisk(mapOf("desired_running" to true), commits = false)
        val stopped = NasPersistence(boot.open(), { ce.open() }, ProcessStartBlock())
        stopped.stop {}

        val freshProcess = NasPersistence(boot.open(), { ce.open() }, ProcessStartBlock())

        assertFalse("A successful DE Stop must override old CE desired=true after process death", freshProcess.desired)
    }

    @Test fun totalStorageFailureIsVisibleAndBlocksOtherStoresInTheSameProcess() {
        val boot = PreferenceDisk(mapOf("desired_running" to true), commits = false, throwsOnCommit = true)
        val ce = PreferenceDisk(mapOf("desired_running" to true), commits = false)
        val process = ProcessStartBlock()
        val state = NasPersistence(boot.open(), { ce.open() }, process)
        var closed = false

        assertFalse(state.stop { closed = true })
        val anotherStore = NasPersistence(boot.open(), { ce.open() }, process)

        assertTrue(closed)
        assertFalse("No other store in this process may re-admit stale disk intent", anotherStore.desired)
        assertTrue("Persistence failure must remain observable without another disk write", anotherStore.error.isNotBlank())
        assertFalse("A retry cannot clear a process persistence failure", anotherStore.desired(true))
    }

    @Test fun recoveryCommitFailureReturnsFailureAndBlocksAutomaticRestart() {
        val boot = PreferenceDisk(mapOf("desired_running" to true))
        val ce = PreferenceDisk(mapOf("desired_running" to true), commits = false)
        val state = NasPersistence(boot.open(), { ce.open() }, ProcessStartBlock())

        assertFalse("An executor must be able to close rather than lose an unobserved Future", state.recordRecovery(1))
        assertFalse(state.desired)
        assertTrue(state.error.isNotBlank())
        assertFalse(NasPersistence(boot.open(), { ce.open() }, ProcessStartBlock()).desired)
    }

    @Test fun dirtyCommitExceptionNeverAuthorizesConstructingAnUntrackedEngine() {
        val boot = PreferenceDisk(mapOf("desired_running" to true))
        val ce = PreferenceDisk(mapOf("desired_running" to true), throwsOnCommit = true)
        val state = NasPersistence(boot.open(), { ce.open() }, ProcessStartBlock())

        assertFalse(state.markDirty(true))
        assertFalse(state.desired)
        assertTrue(state.error.isNotBlank())
    }

    @Test fun errorCommitFailureRemainsVisibleWithoutThrowingFromTheFailurePath() {
        val boot = PreferenceDisk(mapOf("desired_running" to true))
        val ce = PreferenceDisk(mapOf("desired_running" to true), commits = false)
        val state = NasPersistence(boot.open(), { ce.open() }, ProcessStartBlock())

        assertFalse(state.writeError("引擎失败"))
        assertTrue(state.error.contains("无法可靠保存"))
        assertFalse(state.desired)
    }

    @Test fun lockedStopTouchesOnlyDeviceStorageAndDoesNotConstructCredentialStore() {
        val boot = PreferenceDisk(mapOf("desired_running" to true))
        val state = NasPersistence(boot.open(), {
            throw AssertionError("Locked execution constructed CE storage")
        }, ProcessStartBlock(), unlocked = { false })
        var closed = false

        assertTrue(state.stop { closed = true })
        assertTrue(closed)
        assertFalse(state.desired)
        val ce = PreferenceDisk(mapOf("desired_running" to true))
        assertFalse(NasPersistence(boot.open(), { ce.open() }, ProcessStartBlock()).desired)
    }
}

/** Only the external preference disk is replaced; all admission and failure policy is production. */
internal class PreferenceDisk(
    initial: Map<String, Any?> = emptyMap(),
    var commits: Boolean = true,
    var throwsOnCommit: Boolean = false,
) {
    private val durable = initial.toMutableMap()
    fun open(): StatePreferences = object : StatePreferences {
        private val memory = durable.toMutableMap()
        override fun boolean(key: String, fallback: Boolean) = memory[key] as? Boolean ?: fallback
        override fun int(key: String, fallback: Int) = memory[key] as? Int ?: fallback
        override fun string(key: String, fallback: String?) = memory[key] as? String ?: fallback
        override fun commit(values: Map<String, Any?>): Boolean {
            values.forEach { (key, value) -> if (value == null) memory.remove(key) else memory[key] = value }
            if (throwsOnCommit) throw IllegalStateException("preference disk unavailable")
            if (commits) {
                durable.clear()
                durable.putAll(memory)
            }
            return commits
        }
    }
}
