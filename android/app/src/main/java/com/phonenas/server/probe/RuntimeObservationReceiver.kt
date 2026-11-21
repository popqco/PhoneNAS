package com.phonenas.server.probe

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** System boot only; never opens CE data before first unlock or accepts a custom start action. */
class RuntimeObservationReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_LOCKED_BOOT_COMPLETED -> StartupGuard.get(context).lockedBoot()
            Intent.ACTION_BOOT_COMPLETED -> {
                val guard = StartupGuard.get(context)
                // Check DE policy first. A launcher-triggered same-boot redelivery never creates a service.
                if (guard.canReceiveBoot()) NasService.start(context, user = false)
            }
        }
    }
}
