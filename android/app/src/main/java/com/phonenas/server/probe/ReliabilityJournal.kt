package com.phonenas.server.probe

import java.util.UUID
import java.util.zip.CRC32

/**
 * Caller-driven local observations, NOT a keepalive or an availability/72-hour acceptance test.
 * No Android dependency, scheduler, network request or free-form diagnostic text is accepted.
 */
internal class ReliabilityJournal(
    private val store: JournalStore,
    private val clock: Clock,
    private val metadata: Metadata
) {
    fun interface Clock { fun now(): Time }
    interface Metadata {
        val buildVersion: Long
        /** One UUID per app process, not per Activity, service, or journal instance. */
        val processGeneration: UUID
    }
    data class Time(val utcMillis: Long, val elapsedRealtimeMillis: Long, val bootCount: Long? = null)
    data class Snapshot(
        val running: Boolean,
        val desired: Boolean,
        val engineGeneration: Long? = null,
        val recoveries: Int = 0,
        val requests: Long = 0,
        val receivedBytes: Long = 0,
        val sentBytes: Long = 0,
        val screenInteractive: Boolean? = null,
        val charging: Boolean? = null,
        val batteryPercent: Int? = null,
        val batteryTemperatureTenthsC: Int? = null
    )
    enum class Event { START, READY, CHECKPOINT, STOP_REQUESTED, STOP_CONFIRMED, FAILURE, PROCESS_START, RECOVERY }
    enum class FailureCode { UNKNOWN, ENGINE_FAILURE, START_REJECTED, STOP_UNCONFIRMED, RECOVERY_EXHAUSTED, STORAGE_UNAVAILABLE }
    enum class Error {
        BEGIN_REQUIRES_USER_AND_STOPPED, ALREADY_ACTIVE, NO_RUN, FINISHED, RESUME_REQUIRED,
        INVALID_INPUT, ARCHIVE_LIMIT, OVERFLOW, CORRUPT, IO, WRITE_INTERRUPTED, EXPORT_LIMIT, RECORDED_FAILURE
    }
    data class Result(val recorded: Boolean, val runId: UUID? = null, val error: Error? = null)
    data class Summary(
        val runId: UUID? = null,
        val active: Boolean = false,
        val eventCount: Int = 0,
        /** Sum of adjacent running observations in one process/boot; never inferred across a gap. */
        val observedElapsedMillis: Long = 0,
        val unexplainedGap: Boolean = false,
        val overflow: Boolean = false,
        val error: Error? = null,
        val failureCount: Int = 0
    ) { val continuousClaim: Boolean get() = false }
    data class Export(val text: String, val truncated: Boolean, val error: Error? = null)

    private data class Entry(
        val run: UUID, val build: Long, val sequence: Int, val time: Time, val process: UUID,
        val event: Event, val snapshot: Snapshot, val failure: FailureCode?, val gap: Boolean
    )
    private data class Run(val segment: JournalStore.Segment, val entries: List<Entry>)
    private var lastError: Error? = null

    /** The only operation that creates a run. Existing/unknown files are never removed. */
    fun begin(userInitiated: Boolean, serviceStopped: Boolean, snapshot: Snapshot): Result = store.access {
        if (!userInitiated || !serviceStopped || snapshot.running) {
            return@access Result(false, error = Error.BEGIN_REQUIRES_USER_AND_STOPPED)
        }
        writeResult {
            val runs = load()
            val previous = runs.lastOrNull()
            if (previous != null && previous.entries.last().event != Event.STOP_CONFIRMED) {
                return@writeResult Result(false, previous.entries.first().run, Error.ALREADY_ACTIVE)
            }
            if (runs.size >= JournalStore.MAX_SEGMENTS) return@writeResult Result(false, error = Error.ARCHIVE_LIMIT)
            val id = UUID.randomUUID()
            val entry = entry(id, 1, Event.START, snapshot, null, null)
            store.append(runs.size + 1, encode(entry).toByteArray(Charsets.US_ASCII), runs.map { it.segment })
            lastError = null
            Result(true, id)
        }
    }

    /** Call on a real process/service attachment, never merely on opening the UI. */
    fun resume(snapshot: Snapshot): Result = store.access {
        writeResult {
            val runs = load()
            val run = runs.lastOrNull() ?: return@writeResult Result(false, error = Error.NO_RUN)
            val last = run.entries.last()
            if (last.event == Event.STOP_CONFIRMED) return@writeResult Result(false, last.run, Error.FINISHED)
            val now = clock.now()
            if (last.process == metadata.processGeneration && last.time.bootCount == now.bootCount &&
                now.elapsedRealtimeMillis >= last.time.elapsedRealtimeMillis) {
                return@writeResult Result(false, last.run) // Idempotent attachment in the same process.
            }
            append(runs, entry(last.run, last.sequence + 1, Event.PROCESS_START, snapshot, null, last, now))
        }
    }

    fun record(event: Event, snapshot: Snapshot, failure: FailureCode? = null): Result = store.access {
        writeResult {
            if (event == Event.START || event == Event.PROCESS_START ||
                (event == Event.FAILURE) != (failure != null) ||
                (event == Event.READY && !snapshot.running) ||
                (event == Event.STOP_CONFIRMED && snapshot.running)) invalid()
            val runs = load()
            val last = runs.lastOrNull()?.entries?.lastOrNull()
                ?: return@writeResult Result(false, error = Error.NO_RUN)
            if (last.event == Event.STOP_CONFIRMED) return@writeResult Result(false, last.run, Error.FINISHED)
            if (last.process != metadata.processGeneration) {
                return@writeResult Result(false, last.run, Error.RESUME_REQUIRED)
            }
            append(runs, entry(last.run, last.sequence + 1, event, snapshot, failure, last))
        }
    }

    /** Read-only: no checkpoint is synthesized, and no elapsed threshold can produce "passed". */
    fun summary(): Summary = store.access {
        try {
            val entries = load().lastOrNull()?.entries.orEmpty()
            if (entries.isEmpty()) return@access Summary(error = lastError)
            val last = entries.last()
            val active = last.event != Event.STOP_CONFIRMED
            var observed = 0L
            entries.zipWithNext().forEach { (before, after) ->
                if (!after.gap && before.process == after.process && before.time.bootCount == after.time.bootCount &&
                    before.snapshot.running && after.snapshot.running &&
                    before.snapshot.engineGeneration == after.snapshot.engineGeneration) {
                    observed += after.time.elapsedRealtimeMillis - before.time.elapsedRealtimeMillis
                }
            }
            val stale = if (active) gap(last, clock.now(), metadata.processGeneration) else false
            val failures = entries.count { it.event == Event.FAILURE }
            Summary(last.run, active, entries.size, observed,
                entries.any { it.gap } || stale, lastError == Error.OVERFLOW,
                lastError ?: if (failures > 0) Error.RECORDED_FAILURE else null, failures)
        } catch (fault: JournalStore.Fault) {
            lastError = fault.code
            Summary(unexplainedGap = true, overflow = fault.code == Error.OVERFLOW, error = fault.code)
        } catch (_: Exception) {
            lastError = Error.IO
            Summary(unexplainedGap = true, error = Error.IO)
        }
    }

    /** Public-safe, checksummed ASCII records only. Never return unvalidated/corrupt file contents. */
    fun exportText(maxBytes: Int): Export = store.access {
        if (maxBytes <= 0) return@access Export("", true, Error.EXPORT_LIMIT)
        try {
            val runs = load()
            val header = "PhoneNAS local runtime record; continuousClaim=false; not an acceptance test.\n" +
                "schema|runUUID|buildVersion|sequence|utcMillis|elapsedRealtimeMillis|bootCount|processGeneration|" +
                "engineGeneration|event|running|desired|recoveries|requests|receivedBytes|sentBytes|" +
                "screenInteractive|charging|batteryPercent|batteryTemperatureTenthsC|failureCode|gap|crc32\n"
            if (header.length > maxBytes) return@access Export("", true, Error.EXPORT_LIMIT)
            val output = StringBuilder(header)
            var truncated = false
            for (entry in runs.flatMap { it.entries }) {
                val line = encode(entry)
                if (output.length + line.length > maxBytes) { truncated = true; break }
                output.append(line)
            }
            Export(output.toString(), truncated, lastError)
        } catch (fault: JournalStore.Fault) {
            lastError = fault.code
            Export("", false, fault.code)
        } catch (_: Exception) {
            lastError = Error.IO
            Export("", false, Error.IO)
        }
    }

    private fun writeResult(block: () -> Result): Result = try { block() }
    catch (fault: JournalStore.Fault) {
        lastError = fault.code
        Result(false, error = fault.code)
    } catch (_: Exception) {
        lastError = Error.IO
        Result(false, error = Error.IO)
    }

    private fun append(runs: List<Run>, entry: Entry): Result {
        store.append(runs.last().segment.number, encode(entry).toByteArray(Charsets.US_ASCII), runs.map { it.segment })
        return Result(true, entry.run)
    }

    private fun entry(id: UUID, sequence: Int, event: Event, snapshot: Snapshot, failure: FailureCode?,
                      previous: Entry?, now: Time = clock.now()): Entry {
        validate(snapshot, now, metadata.buildVersion)
        return Entry(id, metadata.buildVersion, sequence, now, metadata.processGeneration, event, snapshot,
            failure, event == Event.PROCESS_START || event == Event.RECOVERY ||
                (previous != null && (gap(previous, now, metadata.processGeneration) ||
                    (previous.snapshot.running && !snapshot.running && event != Event.STOP_REQUESTED &&
                        event != Event.STOP_CONFIRMED) ||
                    (previous.snapshot.engineGeneration != null && snapshot.engineGeneration != null &&
                        previous.snapshot.engineGeneration != snapshot.engineGeneration))))
    }

    private fun gap(previous: Entry, now: Time, process: UUID): Boolean =
        previous.process != process || previous.time.bootCount != now.bootCount ||
            now.elapsedRealtimeMillis < previous.time.elapsedRealtimeMillis ||
            now.utcMillis < previous.time.utcMillis ||
            now.elapsedRealtimeMillis - previous.time.elapsedRealtimeMillis > MAX_OBSERVATION_GAP_MILLIS ||
            now.utcMillis - previous.time.utcMillis > MAX_OBSERVATION_GAP_MILLIS

    private fun load(): List<Run> = store.read().mapIndexed { index, segment ->
        try {
            if (segment.number != index + 1 || segment.bytes.isEmpty() || segment.bytes.last() != 10.toByte() ||
                segment.bytes.any { it != 10.toByte() && it.toInt() !in 32..126 }) corrupt()
            val lines = segment.bytes.toString(Charsets.US_ASCII).dropLast(1).split('\n')
            val entries = lines.map { decode(it) }
            if (entries.first().event != Event.START || entries.first().snapshot.running) corrupt()
            entries.forEachIndexed { number, current ->
                if (current.sequence != number + 1 || current.run != entries.first().run ||
                    (number > 0 && current.event == Event.START) ||
                    (current.event == Event.STOP_CONFIRMED && number != entries.lastIndex)) corrupt()
                if (number > 0) {
                    val before = entries[number - 1]
                    if (before.process != current.process && current.event != Event.PROCESS_START) corrupt()
                    if (gap(before, current.time, current.process) && !current.gap) corrupt()
                }
            }
            Run(segment, entries)
        } catch (fault: JournalStore.Fault) { throw fault }
        catch (_: Exception) { corrupt() }
    }.also { runs ->
        if (runs.dropLast(1).any { it.entries.last().event != Event.STOP_CONFIRMED } ||
            runs.map { it.entries.first().run }.distinct().size != runs.size) corrupt()
    }

    private fun encode(entry: Entry): String {
        val s = entry.snapshot
        val payload = listOf(SCHEMA_VERSION, entry.run, entry.build, entry.sequence, entry.time.utcMillis,
            entry.time.elapsedRealtimeMillis, entry.time.bootCount, entry.process, s.engineGeneration,
            entry.event, bit(s.running), bit(s.desired), s.recoveries, s.requests, s.receivedBytes,
            s.sentBytes, bit(s.screenInteractive), bit(s.charging), s.batteryPercent,
            s.batteryTemperatureTenthsC, entry.failure, bit(entry.gap)).joinToString("|") { it?.toString() ?: "-" }
        return "$payload|${checksum(payload)}\n"
    }

    private fun decode(line: String): Entry {
        if (line.length > 1_024) corrupt()
        val f = line.split('|')
        if (f.size != 23 || f[0] != SCHEMA_VERSION.toString() || checksum(f.take(22).joinToString("|")) != f[22]) corrupt()
        fun long(i: Int) = f[i].toLong().also { if (it.toString() != f[i]) corrupt() }
        fun optional(i: Int): Long? = if (f[i] == "-") null else long(i)
        fun int(i: Int) = long(i).also { if (it !in Int.MIN_VALUE..Int.MAX_VALUE) corrupt() }.toInt()
        fun optionalInt(i: Int): Int? = if (f[i] == "-") null else int(i)
        fun bool(i: Int): Boolean? = when (f[i]) { "1" -> true; "0" -> false; "-" -> null; else -> corrupt() }
        fun uuid(i: Int) = UUID.fromString(f[i]).also { if (it.toString() != f[i]) corrupt() }
        val time = Time(long(4), long(5), optional(6))
        val snapshot = Snapshot(bool(10) ?: corrupt(), bool(11) ?: corrupt(), optional(8), int(12),
            long(13), long(14), long(15), bool(16), bool(17), optionalInt(18), optionalInt(19))
        val event = Event.valueOf(f[9])
        val failure = if (f[20] == "-") null else FailureCode.valueOf(f[20])
        if ((event == Event.FAILURE) != (failure != null) || (event == Event.READY && !snapshot.running) ||
            (event == Event.STOP_CONFIRMED && snapshot.running)) corrupt()
        validate(snapshot, time, long(2))
        return Entry(uuid(1), long(2), int(3), time, uuid(7), event, snapshot, failure, bool(21) ?: corrupt())
    }

    private fun validate(s: Snapshot, t: Time, build: Long) {
        if (build < 0 || t.utcMillis !in 0..253_402_300_799_999L || t.elapsedRealtimeMillis < 0 ||
            (t.bootCount != null && t.bootCount < 0) || (s.engineGeneration != null && s.engineGeneration < 0) ||
            s.recoveries < 0 || s.requests < 0 || s.receivedBytes < 0 || s.sentBytes < 0 ||
            (s.batteryPercent != null && s.batteryPercent !in 0..100) ||
            (s.batteryTemperatureTenthsC != null && s.batteryTemperatureTenthsC !in -1_000..1_500)) invalid()
    }
    private fun bit(value: Boolean?): String? = value?.let { if (it) "1" else "0" }
    private fun checksum(value: String) = CRC32().apply { update(value.toByteArray(Charsets.US_ASCII)) }
        .value.toString(16).padStart(8, '0')
    private fun invalid(): Nothing = throw JournalStore.Fault(Error.INVALID_INPUT)
    private fun corrupt(): Nothing = throw JournalStore.Fault(Error.CORRUPT)

    companion object {
        const val SCHEMA_VERSION = 1
        const val MAX_OBSERVATION_GAP_MILLIS = 20 * 60 * 1_000L
    }
}
