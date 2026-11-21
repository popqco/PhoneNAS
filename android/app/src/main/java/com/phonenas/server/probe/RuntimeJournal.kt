package com.phonenas.server.probe

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import java.util.UUID

/** Records observations inside the actual host; never schedules work or probes a listener. */
internal object RuntimeJournal {
    private val process = UUID.randomUUID()
    private var journal: ReliabilityJournal? = null
    private var lastCheckpoint = 0L
    private var attached = false
    @Volatile var error: String = ""
        private set

    @Synchronized private fun get(context: Context): ReliabilityJournal = journal ?: ReliabilityJournal(
        JournalStore(context.applicationContext.filesDir),
        ReliabilityJournal.Clock {
            ReliabilityJournal.Time(System.currentTimeMillis(), SystemClock.elapsedRealtime(),
                runCatching { Settings.Global.getInt(context.contentResolver, Settings.Global.BOOT_COUNT).toLong() }.getOrNull())
        }, object : ReliabilityJournal.Metadata {
            override val buildVersion = BuildConfig.VERSION_CODE.toLong()
            override val processGeneration = process
        }).also { journal = it }

    private fun snapshot(context: Context, desired: Boolean, recoveries: Int, generation: Long?,
                         stats: EngineStats): ReliabilityJournal.Snapshot {
        val battery = runCatching { context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED)) }.getOrNull()
        val level = battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = battery?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
        val temperature = battery?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE)
        val plugged = battery?.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1)
        return ReliabilityJournal.Snapshot(stats.running, desired, generation, recoveries,
            stats.requests, stats.received, stats.sent,
            runCatching { context.getSystemService(PowerManager::class.java).isInteractive }.getOrNull(),
            plugged?.takeIf { it >= 0 }?.let { it != 0 },
            if (level >= 0 && scale > 0) (level.toLong() * 100 / scale).toInt().coerceIn(0, 100) else null,
            temperature?.takeIf { it in -500..1_000 })
    }

    @Synchronized fun begin(context: Context, stoppedConfirmed: Boolean): Boolean {
        val result = get(context).begin(userInitiated = true, serviceStopped = stoppedConfirmed,
            snapshot(context, false, 0, null, EngineStats()))
        error = result.error?.name.orEmpty()
        attached = false
        lastCheckpoint = 0
        return result.recorded
    }

    @Synchronized fun observe(context: Context, event: ReliabilityJournal.Event, desired: Boolean,
                              recoveries: Int, generation: Long?, stats: EngineStats,
                              failure: ReliabilityJournal.FailureCode? = null) {
        try {
            val current = get(context)
            val summary = current.summary()
            if (summary.error != null && summary.error != ReliabilityJournal.Error.RECORDED_FAILURE) {
                error = summary.error.name
                return
            }
            if (!summary.active) return
            val now = SystemClock.elapsedRealtime()
            if (event == ReliabilityJournal.Event.CHECKPOINT && lastCheckpoint != 0L &&
                now - lastCheckpoint < 15 * 60_000L) return
            val sample = snapshot(context, desired, recoveries, generation, stats)
            if (!attached) {
                val resumed = current.resume(sample)
                if (resumed.error != null) { error = resumed.error.name; return }
                attached = true
            }
            val recorded = current.record(event, sample, failure)
            error = (recorded.error ?: summary.error)?.name.orEmpty()
            if (event == ReliabilityJournal.Event.CHECKPOINT && recorded.recorded) lastCheckpoint = now
        } catch (_: Exception) { error = "IO" }
    }

    @Synchronized fun summary(context: Context): ReliabilityJournal.Summary = get(context).summary()
    @Synchronized fun export(context: Context): ReliabilityJournal.Export = get(context).exportText(JournalStore.MAX_BYTES + 1_024)
}
