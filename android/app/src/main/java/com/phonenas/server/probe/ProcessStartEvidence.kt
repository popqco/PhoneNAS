package com.phonenas.server.probe

/** Matches published onCreate evidence, never an arbitrary newest historical start. */
internal object ProcessStartEvidence {
    data class Identity(val pid: Int, val uid: Int, val processName: String,
                        val bindUptimeMillis: Long, val applicationEnterUptimeNanos: Long)
    data class Record(val pid: Int, val realUid: Int, val packageUid: Int, val processName: String,
                      val cold: Boolean, val applicationOnCreateNanos: Long, val wasForceStopped: Boolean)

    fun select(identity: Identity, records: List<Record>): RecoveryPolicy.Observation {
        if (identity.bindUptimeMillis <= 0 || identity.applicationEnterUptimeNanos <= 0 ||
            identity.bindUptimeMillis > Long.MAX_VALUE / 1_000_000) return RecoveryPolicy.Observation.UNKNOWN
        val lower = identity.bindUptimeMillis * 1_000_000
        val record = records.filter {
            (it.pid == identity.pid || it.pid == 0) && it.realUid == identity.uid &&
                it.packageUid == identity.uid && it.processName == identity.processName && it.cold &&
                it.applicationOnCreateNanos in lower..identity.applicationEnterUptimeNanos
        }.singleOrNull() ?: return RecoveryPolicy.Observation.UNKNOWN
        return if (record.wasForceStopped) RecoveryPolicy.Observation.FORCE_STOPPED else RecoveryPolicy.Observation.NORMAL
    }
}
