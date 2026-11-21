package com.phonenas.server.probe

import java.util.concurrent.ExecutorService
import java.util.concurrent.TimeUnit

/** One process-wide engine lease; the service owns handles only through this executor. */
internal class EngineLifetime<T>(
    private val stopper: ExecutorService,
    private val nativeStop: (T) -> Unit,
    private val stopTimeoutMillis: Long = 8_000L,
) {
    private var owner: Any? = null
    private var engine: T? = null
    private var stopping = false
    @Volatile var quarantined = false
        private set
    @get:Synchronized val stoppedConfirmed: Boolean get() = owner == null && engine == null && !quarantined

    @Synchronized fun acquire(owner: Any, construct: () -> T): T? {
        if (!stoppedConfirmed) return null
        this.owner = owner
        return try { construct().also { engine = it } }
        catch (failure: Throwable) { this.owner = null; throw failure }
    }
    fun close(owner: Any): Boolean {
        val current = synchronized(this) {
            if (quarantined || stopping || (this.owner != null && this.owner !== owner)) return false
            engine?.also { stopping = true }
        } ?: return true
        return try {
            stopper.submit { nativeStop(current) }.get(stopTimeoutMillis, TimeUnit.MILLISECONDS)
            synchronized(this) { engine = null; this.owner = null; stopping = false }
            true
        } catch (_: Exception) { quarantined = true; false }
    }
}

/** Pure admission state used by both boot delivery and the real service, not a runtime simulator. */
internal class RecoveryPolicy(
    var generation: Int?,
    blocked: Reason?,
    observation: Observation,
    restoreGeneration: Int? = null,
) {
    enum class Observation { NORMAL, FORCE_STOPPED, UNKNOWN, LEGACY }
    enum class Request { USER, RESTORE, STICKY }
    enum class Reason(val message: String) {
        FORCE_STOPPED("系统曾强行停止本应用；请明确点击启动，不会因打开页面自动恢复。"),
        UNKNOWN("无法确认本进程启动来源；请明确点击启动。"),
        NO_GENERATION("缺少可信开机代次；请明确点击启动建立基线。"),
        SAME_BOOT("同次开机的广播不触发恢复；需要时请手动启动。"),
        BOOT_NOT_ADMITTED("本次开机尚未获准恢复；请手动启动。"),
        NO_RESTORE("没有待接收的开机恢复许可；请手动启动。"),
        STOPPED("用户期望停止；不会自动启动。"),
        ERROR("存在持久错误；请检查后手动启动。"),
        USER("用户已明确启动。"),
        BOOT("新开机首次解锁恢复已接收。"),
        STICKY("系统重建；仍受持久错误和 3 次恢复上限约束。"),
    }

    var blocked: Reason? = when (observation) {
        Observation.FORCE_STOPPED -> Reason.FORCE_STOPPED
        Observation.UNKNOWN -> blocked ?: Reason.UNKNOWN
        else -> blocked ?: if (generation == null) Reason.NO_GENERATION else null
    }
        private set
    var reason: Reason = this.blocked ?: Reason.BOOT_NOT_ADMITTED
        private set
    var restoreGeneration: Int? = restoreGeneration.takeIf { it == generation && this.blocked == null }
        private set
    private var userStarted = false

    fun userStart(currentGeneration: Int?) {
        generation = currentGeneration
        blocked = null
        restoreGeneration = null
        userStarted = true
        reason = Reason.USER
    }

    fun userStop() {
        restoreGeneration = null
        userStarted = false
        reason = Reason.STOPPED
    }

    fun admitBoot(currentGeneration: Int?, desired: Boolean, hasError: Boolean,
        consume: Boolean = true): Boolean {
        if (!automaticGate(desired, hasError)) return false
        val previous = generation
        if (currentGeneration == null || previous == null || currentGeneration < previous) {
            reason = Reason.NO_GENERATION
            return false
        }
        if (currentGeneration == previous) {
            reason = Reason.SAME_BOOT
            return false
        }
        if (consume) {
            generation = currentGeneration
            restoreGeneration = currentGeneration
            userStarted = false
        }
        reason = Reason.BOOT
        return true
    }

    fun admitService(request: Request, currentGeneration: Int?, desired: Boolean,
        hasError: Boolean): Boolean {
        if (!automaticGate(desired, hasError)) return false
        if (request == Request.USER) {
            if (!userStarted) { reason = Reason.UNKNOWN; return false }
            reason = Reason.USER
            return true
        }
        if (currentGeneration == null || generation == null) {
            reason = Reason.NO_GENERATION
            return false
        }
        if (generation != currentGeneration) {
            reason = Reason.BOOT_NOT_ADMITTED
            return false
        }
        if (request == Request.RESTORE) {
            if (restoreGeneration != currentGeneration) { reason = Reason.NO_RESTORE; return false }
            restoreGeneration = null
            reason = Reason.BOOT
        } else reason = Reason.STICKY
        return true
    }

    private fun automaticGate(desired: Boolean, hasError: Boolean): Boolean {
        if (!desired) { reason = Reason.STOPPED; restoreGeneration = null; return false }
        if (hasError) { reason = Reason.ERROR; return false }
        blocked?.let { reason = it; return false }
        return true
    }

    companion object {
        fun nextRecovery(recoveries: Int): Int? = if (recoveries in 0..2) recoveries + 1 else null
        fun recoveryDelaySeconds(recoveries: Int): Long = when (recoveries) {
            1 -> 2L
            2 -> 5L
            else -> 15L
        }
    }
}
