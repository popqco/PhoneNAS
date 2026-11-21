package com.phonenas.server.probe

import android.content.Context
import android.content.SharedPreferences
import android.os.UserManager
import android.provider.Settings

/** The only persistence boundary: implementations must report a durable commit, never apply(). */
internal interface StatePreferences {
    fun boolean(key: String, fallback: Boolean = false): Boolean
    fun int(key: String, fallback: Int = 0): Int
    fun string(key: String, fallback: String? = null): String?
    fun commit(values: Map<String, Any?>): Boolean
}

internal class AndroidStatePreferences(private val prefs: SharedPreferences) : StatePreferences {
    override fun boolean(key: String, fallback: Boolean) = prefs.getBoolean(key, fallback)
    override fun int(key: String, fallback: Int) = prefs.getInt(key, fallback)
    override fun string(key: String, fallback: String?) = prefs.getString(key, fallback)
    override fun commit(values: Map<String, Any?>): Boolean {
        val edit = prefs.edit()
        values.forEach { (key, value) -> when (value) {
            null -> edit.remove(key)
            is Boolean -> edit.putBoolean(key, value)
            is Int -> edit.putInt(key, value)
            is String -> edit.putString(key, value)
            else -> error("Unsupported persisted state")
        } }
        return edit.commit()
    }
}

internal class ProcessStartBlock {
    var blocked = false
        private set
    var error = ""
        private set
    fun block(reason: String) { blocked = true; error = reason }
    companion object { val shared = ProcessStartBlock() }
}

/** Real state executor, also used for a Stop delivered before CE becomes available. */
internal class NasPersistence(
    private val boot: StatePreferences,
    private val credential: () -> StatePreferences?,
    private val process: ProcessStartBlock = ProcessStartBlock.shared,
    private val unlocked: () -> Boolean = { true },
) {
    val desired get() = !process.blocked && unlocked() && !boot.boolean("stop_tombstone") && credential()?.boolean("desired_running") == true
    val error get() = process.error.ifBlank { credential()?.string("error", "").orEmpty() }
    val dirty get() = credential()?.boolean("unclean_engine") == true
    val recoveries get() = credential()?.int("recoveries") ?: 0

    fun desired(value: Boolean): Boolean {
        if (!value) {
            // DE first: a later process must not trust stale CE intent if the CE commit fails.
            val hinted = commit { boot.commit(mapOf("desired_running" to false, "stop_tombstone" to true)) }
            val saved = !unlocked() || commit { credential()?.commit(mapOf("desired_running" to false)) == true }
            if (!saved || !hinted) process.block(PERSISTENCE_ERROR)
            return saved && hinted
        }
        if (process.blocked || !unlocked()) return false
        val saved = commit { credential()?.commit(mapOf("desired_running" to true)) == true }
        val hinted = saved && commit { boot.commit(mapOf("desired_running" to true, "stop_tombstone" to false)) }
        if (!saved || !hinted) process.block(PERSISTENCE_ERROR)
        return saved && hinted
    }
    fun stop(shutdown: () -> Unit): Boolean = try {
        desired(false)
    } finally {
        shutdown()
    }
    private fun commit(write: () -> Boolean): Boolean = try { write() } catch (_: Exception) { false }
    fun writeError(value: String): Boolean {
        if (commit { credential()?.commit(mapOf("error" to value)) == true }) return true
        persistenceFailure()
        return false
    }
    fun markDirty(value: Boolean): Boolean {
        if (commit { credential()?.commit(mapOf("unclean_engine" to value)) == true }) return true
        persistenceFailure()
        return false
    }
    fun recordRecovery(value: Int): Boolean {
        if (commit { credential()?.commit(mapOf("recoveries" to value)) == true }) return true
        persistenceFailure()
        return false
    }
    fun prepareRecoveryBudget(generation: Int?): Boolean {
        if (generation == null || !unlocked() || process.blocked) return !process.blocked
        val ce = credential() ?: return false
        val saved = ce.int("recovery_generation", -1)
        val admittedBoot = boot.int("guard_boot_recovery_generation", -1)
        if (saved == generation) return true
        val reset = admittedBoot == generation && saved < generation
        val values = if (reset) mapOf("recovery_generation" to generation, "recoveries" to 0,
            "unclean_engine" to false) else mapOf("recovery_generation" to generation)
        if (commit { ce.commit(values) }) return true
        persistenceFailure()
        return false
    }
    private fun persistenceFailure() {
        process.block(PERSISTENCE_ERROR)
        // No error-path write may prevent actual native shutdown. Tombstone is best effort.
        commit { boot.commit(mapOf("stop_tombstone" to true)) }
    }
    companion object {
        const val PERSISTENCE_ERROR = "无法可靠保存服务状态；已禁止本进程再次启动。若所有存储均不可写，跨进程停止状态无法保证，请检查后手动启动。"
    }
}

/** Only nonsecret DE metadata is touched here, including from Application before first unlock. */
internal class StartupGuard(
    private val prefs: StatePreferences,
    observation: RecoveryPolicy.Observation,
    private val unlocked: () -> Boolean,
    private val bootCount: () -> Int?,
) {
    private val policy = RecoveryPolicy(
        prefs.int("guard_generation", -1).takeIf { it >= 0 },
        prefs.string("guard_block")?.let { saved ->
            RecoveryPolicy.Reason.entries.firstOrNull { it.name == saved } ?: RecoveryPolicy.Reason.UNKNOWN
        }, observation, prefs.int("guard_restore_generation", -1).takeIf { it >= 0 })
    private var storageFailed = false

    init { persist() }

    data class Status(val blocked: Boolean, val reason: String, val generation: Int?)

    @Synchronized fun status(): Status = Status(storageFailed || policy.blocked != null,
        if (storageFailed) "无法保存启动保护状态；请检查后手动启动。" else policy.reason.message,
        policy.generation)

    @Synchronized fun userStart(): Boolean {
        if (!unlocked()) return false
        policy.userStart(bootCount())
        return persist()
    }

    @Synchronized fun userStop() {
        policy.userStop()
        persist()
    }

    /** Called before NasStore construction, so locked boot cannot reach CE. */
    @Synchronized fun canReceiveBoot(): Boolean {
        if (!unlocked() || storageFailed) return false
        return policy.admitBoot(bootCount(), desired = true, hasError = false, consume = false)
    }

    /** Consume before requesting a service; failed system admission does not create a retry loop. */
    @Synchronized fun admitBoot(desired: Boolean, hasError: Boolean): Boolean {
        if (!unlocked() || storageFailed) return false
        if (!policy.admitBoot(bootCount(), desired, hasError)) return false
        return persist(mapOf("guard_boot_recovery_generation" to policy.generation))
    }

    @Synchronized fun admitService(request: RecoveryPolicy.Request, desired: Boolean,
        hasError: Boolean): Boolean {
        if (!unlocked() || storageFailed) return false
        val admitted = policy.admitService(request, bootCount(), desired, hasError)
        return admitted && persist()
    }

    fun lockedBoot() {
        // This hint is diagnostic only; BOOT_COUNT, never the presence of this broadcast, authorizes boot.
        try {
            if (!prefs.commit(mapOf("waited_for_unlock" to true))) storageFailed = true
        } catch (_: Exception) { storageFailed = true }
    }

    private fun persist(extra: Map<String, Any?> = emptyMap()): Boolean {
        val saved = try {
            prefs.commit(mapOf("guard_block" to policy.blocked?.name,
                "guard_reason" to policy.reason.name, "guard_generation" to policy.generation,
                "guard_restore_generation" to policy.restoreGeneration) + extra)
        } catch (_: Exception) { false }
        storageFailed = storageFailed || !saved
        return !storageFailed
    }

    companion object {
        @Volatile private var instance: StartupGuard? = null
        @Synchronized fun observe(context: Context, observation: RecoveryPolicy.Observation): StartupGuard {
            // Never re-apply cached wasForceStopped after this process receives an explicit Start.
            return instance ?: context.applicationContext.let { app ->
                StartupGuard(AndroidStatePreferences(app.createDeviceProtectedStorageContext()
                    .getSharedPreferences("nas_boot", Context.MODE_PRIVATE)), observation,
                    { app.getSystemService(UserManager::class.java).isUserUnlocked },
                    { try {
                        Settings.Global.getInt(app.contentResolver, Settings.Global.BOOT_COUNT).takeIf { it >= 0 }
                    } catch (_: Exception) { null } }).also { instance = it }
            }
        }
        fun get(context: Context): StartupGuard {
            instance?.let { return it }
            ProbeApplication.ensureStartup(context.applicationContext)
            return instance ?: observe(context, RecoveryPolicy.Observation.UNKNOWN)
        }
    }
}
