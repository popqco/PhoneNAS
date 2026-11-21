package com.phonenas.server.probe

import android.app.Activity
import android.os.Bundle

/** Explicit shell/system fixture control; the manifest enforces DUMP permission. */
class DebugControlActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        try {
            when (intent.action) {
                "com.phonenas.server.probe.DEBUG_START_DEMO" -> {
                    NasStore(this).exportDebugAccess()
                    NasService.start(this, user = true)
                }
                "com.phonenas.server.probe.DEBUG_STOP_DEMO" -> NasService.stop(this)
                "com.phonenas.server.probe.DEBUG_START_EVIDENCE" -> {
                    ProbeApplication.ensureStartup(this)
                    openFileOutput("start-evidence.txt", MODE_PRIVATE).use {
                        it.write(ProbeApplication.diagnostic().toByteArray(Charsets.US_ASCII))
                    }
                }
            }
        } finally {
            finish()
        }
    }
}
