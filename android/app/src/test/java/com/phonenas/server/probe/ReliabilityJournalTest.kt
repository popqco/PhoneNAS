package com.phonenas.server.probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.UUID

/** The public journal seam uses real private-like files and injected time/metadata, never Android. */
class ReliabilityJournalTest {
    @get:Rule val temporary = TemporaryFolder()

    private class TestClock : ReliabilityJournal.Clock {
        var utc = 1_700_000_000_000L
        var elapsed = 1_000L
        var boot: Long? = 7L
        override fun now() = ReliabilityJournal.Time(utc, elapsed, boot)
        fun advance(millis: Long) { utc += millis; elapsed += millis }
    }

    private val metadata = object : ReliabilityJournal.Metadata {
        override val buildVersion = 3L
        override val processGeneration = UUID.fromString("00000000-0000-0000-0000-000000000001")
    }

    @Test fun anEngineRecoveryIsAGapEvenWhenTheNextObservationArrivesPromptly() {
        val clock = TestClock()
        val journal = ReliabilityJournal(JournalStore(temporary.newFolder()), clock, metadata)
        journal.begin(true, true, ReliabilityJournal.Snapshot(false, true))
        journal.record(ReliabilityJournal.Event.READY, ReliabilityJournal.Snapshot(true, true, 1))
        clock.advance(900_000)
        journal.record(ReliabilityJournal.Event.CHECKPOINT, ReliabilityJournal.Snapshot(true, true, 1))
        clock.advance(1_000)
        journal.record(ReliabilityJournal.Event.RECOVERY,
            ReliabilityJournal.Snapshot(false, true, 1, recoveries = 1))
        clock.advance(1_000)
        journal.record(ReliabilityJournal.Event.READY,
            ReliabilityJournal.Snapshot(true, true, 2, recoveries = 1))
        val summary = journal.summary()
        assertTrue(summary.unexplainedGap)
        assertEquals(900_000L, summary.observedElapsedMillis)
        assertFalse(summary.continuousClaim)
    }

    @Test fun aDurableFailureRemainsVisibleAndStoppingCannotResumeOrClearIt() {
        val files = temporary.newFolder()
        val journal = ReliabilityJournal(JournalStore(files), TestClock(), metadata)
        journal.begin(true, true, ReliabilityJournal.Snapshot(false, true))
        assertTrue(journal.record(ReliabilityJournal.Event.FAILURE,
            ReliabilityJournal.Snapshot(false, true), ReliabilityJournal.FailureCode.ENGINE_FAILURE).recorded)
        val reopened = ReliabilityJournal(JournalStore(files), TestClock(), metadata)
        assertEquals(1, reopened.summary().failureCount)
        assertEquals(ReliabilityJournal.Error.RECORDED_FAILURE, reopened.summary().error)
        assertTrue(reopened.record(ReliabilityJournal.Event.STOP_CONFIRMED,
            ReliabilityJournal.Snapshot(false, false)).recorded)
        assertFalse(reopened.summary().active)
        assertEquals(1, reopened.summary().failureCount)
        assertEquals(ReliabilityJournal.Error.RECORDED_FAILURE, reopened.summary().error)
        assertEquals(ReliabilityJournal.Error.FINISHED,
            reopened.resume(ReliabilityJournal.Snapshot(false, false)).error)
    }

    @Test fun unknownBootAndAChangedProcessProduceARecordedRestartGapWithoutInventingObservedHours() {
        val files = temporary.newFolder()
        val clock = TestClock()
        val first = ReliabilityJournal(JournalStore(files), clock, metadata)
        val run = first.begin(true, true, ReliabilityJournal.Snapshot(false, true)).runId
        first.record(ReliabilityJournal.Event.READY, ReliabilityJournal.Snapshot(true, true, 1))
        clock.advance(900_000)
        first.record(ReliabilityJournal.Event.CHECKPOINT, ReliabilityJournal.Snapshot(true, true, 1))
        clock.advance(72 * 60 * 60 * 1_000L)
        clock.boot = null
        clock.elapsed = 25L
        val nextProcess = object : ReliabilityJournal.Metadata {
            override val buildVersion = 4L
            override val processGeneration = UUID.fromString("00000000-0000-0000-0000-000000000002")
        }
        val restarted = ReliabilityJournal(JournalStore(files), clock, nextProcess)
        assertTrue(restarted.summary().unexplainedGap)
        assertEquals(ReliabilityJournal.Error.RESUME_REQUIRED,
            restarted.record(ReliabilityJournal.Event.CHECKPOINT, ReliabilityJournal.Snapshot(true, true)).error)
        assertTrue(restarted.resume(ReliabilityJournal.Snapshot(false, true)).recorded)
        assertEquals(run, restarted.summary().runId)
        assertTrue(restarted.summary().unexplainedGap)
        assertEquals(900_000L, restarted.summary().observedElapsedMillis)
        assertFalse(restarted.summary().continuousClaim)
        assertTrue(restarted.exportText(16_384).text.contains("PROCESS_START"))
    }

    @Test fun exhaustedBudgetRemainsANonGreenBoundedErrorAfterReopening() {
        val files = temporary.newFolder()
        val clock = TestClock()
        val journal = ReliabilityJournal(JournalStore(files, maxBytes = 1_024), clock, metadata)
        assertTrue(journal.begin(true, true, ReliabilityJournal.Snapshot(false, true)).recorded)
        var result = ReliabilityJournal.Result(true)
        var attempts = 0
        while (result.recorded && attempts++ < 20) {
            clock.advance(900_000)
            result = journal.record(ReliabilityJournal.Event.CHECKPOINT,
                ReliabilityJournal.Snapshot(true, true, 1))
        }
        assertEquals(ReliabilityJournal.Error.OVERFLOW, result.error)
        assertTrue(journal.summary().overflow)
        val reopened = ReliabilityJournal(JournalStore(files, maxBytes = 1_024), clock, metadata)
        assertEquals(ReliabilityJournal.Error.OVERFLOW, reopened.summary().error)
        assertTrue(reopened.summary().overflow)
        assertFalse(reopened.summary().continuousClaim)
        assertEquals(ReliabilityJournal.Error.OVERFLOW, reopened.exportText(2_048).error)
        assertTrue(files.walkTopDown().filter { it.isFile }.sumOf { it.length() } <= 1_024)
    }

    @Test fun aTornTailFailsClosedAndNeverExportsUntrustedTextOrReplacesTheRun() {
        val files = temporary.newFolder()
        val clock = TestClock()
        val journal = ReliabilityJournal(JournalStore(files), clock, metadata)
        assertTrue(journal.begin(true, true, ReliabilityJournal.Snapshot(false, true)).recorded)
        // Filesystem is the external fault-injection seam; every observation stays through the API.
        val persisted = files.walkTopDown().single { it.isFile && it.extension == "pnj" }
        persisted.appendText("Authorization: secret /user/private password=secret")
        val before = persisted.readBytes()
        val reopened = ReliabilityJournal(JournalStore(files), clock, metadata)
        assertEquals(ReliabilityJournal.Error.CORRUPT, reopened.summary().error)
        assertTrue(reopened.summary().unexplainedGap)
        val export = reopened.exportText(16_384)
        assertEquals(ReliabilityJournal.Error.CORRUPT, export.error)
        assertEquals("", export.text)
        assertEquals(ReliabilityJournal.Error.CORRUPT,
            reopened.begin(true, true, ReliabilityJournal.Snapshot(false, true)).error)
        assertTrue(before.contentEquals(persisted.readBytes()))
    }

    @Test fun seventyTwoHoursOfQuarterHourlyCheckpointsFitButNeverConstituteAcceptance() {
        val files = temporary.newFolder()
        val clock = TestClock()
        val journal = ReliabilityJournal(JournalStore(files), clock, metadata)
        journal.begin(true, true, ReliabilityJournal.Snapshot(false, true))
        journal.record(ReliabilityJournal.Event.READY, ReliabilityJournal.Snapshot(true, true, 1))
        repeat(288) {
            clock.advance(900_000)
            assertTrue(journal.record(ReliabilityJournal.Event.CHECKPOINT,
                ReliabilityJournal.Snapshot(true, true, 1, requests = it.toLong(),
                    screenInteractive = false, charging = true, batteryPercent = 90,
                    batteryTemperatureTenthsC = 320)).recorded)
        }
        val summary = journal.summary()
        assertEquals(259_200_000L, summary.observedElapsedMillis)
        assertEquals(290, summary.eventCount)
        assertFalse(summary.continuousClaim)
        assertFalse(summary.unexplainedGap)
        assertEquals(null, summary.error)
        assertTrue(files.walkTopDown().filter { it.isFile }.sumOf { it.length() } < 2 * 1_024 * 1_024)
        val export = journal.exportText(1_024)
        assertTrue(export.truncated)
        assertTrue(export.text.toByteArray(Charsets.UTF_8).size <= 1_024)
        assertTrue(export.text.endsWith("\n"))
        val full = journal.exportText(2 * 1_024 * 1_024)
        assertFalse(full.truncated)
        assertTrue(full.text.contains("not an acceptance test"))
        clock.advance(21 * 60 * 1_000L)
        assertTrue(journal.summary().unexplainedGap)
        assertEquals(259_200_000L, journal.summary().observedElapsedMillis)
    }

    @Test fun blockedPrivateStorageIsExplicitAndDoesNotDiscloseItsPath() {
        val files = temporary.newFolder()
        val blocker = File(files, "reliability-journal-v1").apply { writeText("user-owned-unknown-data") }
        val journal = ReliabilityJournal(JournalStore(files), TestClock(), metadata)
        val result = journal.begin(true, true, ReliabilityJournal.Snapshot(false, true))
        assertFalse(result.recorded)
        assertEquals(ReliabilityJournal.Error.CORRUPT, result.error)
        assertEquals(ReliabilityJournal.Error.CORRUPT, journal.summary().error)
        assertEquals("", journal.exportText(16_384).text)
        assertEquals("user-owned-unknown-data", blocker.readText())
    }

    @Test fun retainedRunsAreBoundedAndNeitherUnknownNorUserFilesAreCleanedUp() {
        val files = temporary.newFolder()
        val unrelated = File(files, "user-file").apply { writeText("untouched") }
        val clock = TestClock()
        val journal = ReliabilityJournal(JournalStore(files), clock, metadata)
        val runs = mutableSetOf<UUID?>()
        repeat(3) {
            val result = journal.begin(true, true, ReliabilityJournal.Snapshot(false, true))
            assertTrue(result.recorded)
            runs.add(result.runId)
            assertTrue(journal.record(ReliabilityJournal.Event.STOP_CONFIRMED,
                ReliabilityJournal.Snapshot(false, false)).recorded)
            clock.advance(1_000)
        }
        assertEquals(3, runs.size)
        assertEquals(ReliabilityJournal.Error.ARCHIVE_LIMIT,
            journal.begin(true, true, ReliabilityJournal.Snapshot(false, true)).error)
        val text = journal.exportText(16_384).text
        for (run in runs) assertTrue(text.contains(run.toString()))
        assertEquals("untouched", unrelated.readText())
        assertEquals(3, files.walkTopDown().count { it.isFile && it.extension == "pnj" })
    }

    @Test fun anInterruptedAppendMarkerSurvivesReopeningAsAnExplicitFailure() {
        val files = temporary.newFolder()
        val journal = ReliabilityJournal(JournalStore(files), TestClock(), metadata)
        journal.begin(true, true, ReliabilityJournal.Snapshot(false, true))
        File(files, "reliability-journal-v1/pending").writeBytes(byteArrayOf(1))
        val reopened = ReliabilityJournal(JournalStore(files), TestClock(), metadata)
        assertEquals(ReliabilityJournal.Error.WRITE_INTERRUPTED, reopened.summary().error)
        assertFalse(reopened.record(ReliabilityJournal.Event.READY,
            ReliabilityJournal.Snapshot(true, true)).recorded)
        assertEquals(ReliabilityJournal.Error.WRITE_INTERRUPTED, reopened.exportText(16_384).error)
        assertTrue(File(files, "reliability-journal-v1/pending").exists())
    }

    @Test fun explicitSessionPersistsStructuredObservationsWithoutClaimingContinuousUptime() {
        val files = temporary.newFolder()
        val clock = TestClock()
        val journal = ReliabilityJournal(JournalStore(files), clock, metadata)
        assertFalse(journal.begin(false, true, ReliabilityJournal.Snapshot(false, false)).recorded)
        assertFalse(journal.begin(true, false, ReliabilityJournal.Snapshot(false, false)).recorded)
        val started = journal.begin(true, true, ReliabilityJournal.Snapshot(false, true))
        assertTrue(started.recorded)
        assertTrue(journal.record(ReliabilityJournal.Event.READY,
            ReliabilityJournal.Snapshot(true, true, engineGeneration = 1)).recorded)
        clock.advance(900_000)
        assertTrue(journal.record(ReliabilityJournal.Event.CHECKPOINT,
            ReliabilityJournal.Snapshot(true, true, engineGeneration = 1, requests = 4,
                receivedBytes = 128, sentBytes = 256)).recorded)

        // Reopening for inspection does not create a new run or an extra observation.
        val reopened = ReliabilityJournal(JournalStore(files), clock, metadata)
        val summary = reopened.summary()
        assertEquals(started.runId, summary.runId)
        assertEquals(900_000L, summary.observedElapsedMillis)
        assertEquals(3, summary.eventCount)
        assertFalse(summary.continuousClaim)
        assertFalse(summary.unexplainedGap)
        assertEquals(null, summary.error)
        val export = reopened.exportText(16_384)
        assertEquals(null, export.error)
        assertFalse(export.truncated)
        assertTrue(export.text.contains("START"))
        assertTrue(export.text.contains("READY"))
        assertTrue(export.text.contains("CHECKPOINT"))
        assertTrue(export.text.contains("continuousClaim=false"))
    }
}
