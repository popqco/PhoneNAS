package com.phonenas.server.probe

import android.app.ActivityManager
import android.app.Application
import android.app.ApplicationStartInfo
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.os.SystemClock

class ProbeApplication : Application() {
    override fun onCreate() {
        applicationEnterUptimeNanos = if (Build.VERSION.SDK_INT >= 35) SystemClock.uptimeNanos() else 0L
        super.onCreate()
        // ONCREATE is published by finishAttachApplication only after this callback returns.
        // Components call ensureStartup() before touching intent/config; the queued call covers idle UI starts.
        Handler(Looper.getMainLooper()).post { ensureStartup(this) }
    }

    companion object {
        private var applicationEnterUptimeNanos = 0L
        private var observed = false
        private var debugExportsChecked = false
        private var debugExportsClean = true
        private var diagnostic = ""

        @Synchronized internal fun ensureStartup(application: android.content.Context) {
            cleanDebugExportsWhenUnlocked(application)
            if (observed) return
            val observation = if (Build.VERSION.SDK_INT >= 35) {
                try {
                    val identity = ProcessStartEvidence.Identity(Process.myPid(), Process.myUid(), getProcessName(),
                        Process.getStartUptimeMillis(), applicationEnterUptimeNanos)
                    val records = application.getSystemService(ActivityManager::class.java)
                        .getHistoricalProcessStartReasons(0).map { info ->
                            ProcessStartEvidence.Record(info.pid, info.realUid, info.packageUid,
                                info.processName.orEmpty(), info.startType == ApplicationStartInfo.START_TYPE_COLD,
                                info.startupTimestamps[ApplicationStartInfo.START_TIMESTAMP_APPLICATION_ONCREATE] ?: 0L,
                                info.wasForceStopped())
                        }
                    val selected = ProcessStartEvidence.select(identity, records)
                    diagnostic = buildString {
                        append("pid=${identity.pid};uid=${identity.uid};bindMs=${identity.bindUptimeMillis};entryNs=${identity.applicationEnterUptimeNanos};observation=$selected\n")
                        records.take(20).forEach { append("pid=${it.pid};uid=${it.realUid};packageUid=${it.packageUid};cold=${it.cold};onCreateNs=${it.applicationOnCreateNanos};forceStopped=${it.wasForceStopped}\n") }
                    }
                    selected
                } catch (_: Exception) { RecoveryPolicy.Observation.UNKNOWN }
            } else RecoveryPolicy.Observation.LEGACY
            val status = StartupGuard.observe(application, observation).status()
            observed = true
            if (status.blocked) RuntimeState.view = RuntimeView("需要手动启动", error = status.reason)
        }

        @Synchronized internal fun cleanDebugExportsWhenUnlocked(context: android.content.Context): Boolean {
            if (BuildConfig.DEBUG || !context.getSystemService(android.os.UserManager::class.java).isUserUnlocked) return true
            if (!debugExportsChecked) {
                debugExportsClean = DebugArtifactCleanup.remove(context.applicationContext.filesDir)
                debugExportsChecked = true
            }
            return debugExportsClean
        }

        internal fun diagnostic(): String {
            check(BuildConfig.DEBUG)
            return diagnostic
        }
    }
}
