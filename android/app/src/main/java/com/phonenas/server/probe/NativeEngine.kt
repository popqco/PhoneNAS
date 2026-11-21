package com.phonenas.server.probe

import mobile.Mobile
import mobile.Server
import org.json.JSONObject

/** Calls the real gomobile engine. Exceptions propagate; no simulated listener state. */
internal class NativeEngine(config: String) {
    private val server: Server = Mobile.start(config)
    fun sample(): EngineStats {
        val data = JSONObject(server.status())
        val address = server.address()
        check(address == LISTEN) { "Unexpected listener address" }
        return EngineStats(data.getBoolean("running"), address, data.optLong("requests"),
            data.optLong("bytes_received"), data.optLong("bytes_sent"), data.optString("last_error"),
            data.optString("remote_address"), data.optString("certificate_sha256"))
    }
    fun revokeRemote() = server.revokeRemote()
    fun stop() = server.stop()
}
