package com.phonenas.server.probe

/** A timestamped observation is not a promise that an OEM will keep the process runnable. */
internal enum class EvidenceValue { YES, NO, UNKNOWN }

internal data class ObservedValue(
    val value: EvidenceValue = EvidenceValue.UNKNOWN,
    val observedAt: Long? = null,
    val note: String = ""
)

internal data class ReliabilityObservations(
    val notificationRuntime: ObservedValue = ObservedValue(),
    val notificationApp: ObservedValue = ObservedValue(),
    val notificationChannel: ObservedValue = ObservedValue(),
    val batteryExempt: ObservedValue = ObservedValue(),
    val backgroundRestricted: ObservedValue = ObservedValue(),
    val runtimePermissionRequired: Boolean? = null
) {
    companion object {
        fun unknown() = ReliabilityObservations()
    }
}

/** Only the user may change these records. They are never inferred from opening Settings. */
internal enum class ManualReviewState { UNREVIEWED, USER_REPORTED, UNAVAILABLE }
internal enum class ManualSetting(val preferenceKey: String, val title: String) {
    BACKGROUND_POWER("background_power", "后台运行 / 后台高耗电"),
    AUTOSTART("autostart", "自启动 / 关联启动"),
    OTHER_SETTINGS("other_settings", "其他可选设置（如最近任务锁定）")
}
internal data class ManualReview(
    val state: ManualReviewState = ManualReviewState.UNREVIEWED,
    val recordedAt: Long? = null
)

internal fun decodeManualReview(state: String?, recordedAt: Long): ManualReview {
    val parsed = ManualReviewState.entries.firstOrNull { it.name == state }
    return if (parsed == null || recordedAt <= 0L) ManualReview()
    else ManualReview(parsed, recordedAt)
}

/** This release has no automatic or in-app background acceptance test. */
internal enum class BackgroundTestEvidence { DEFAULT_VIVO_HOME_BLOCKED, NOT_TESTED }
internal enum class BackgroundReadiness { KNOWN_BLOCKED, UNVERIFIED }
internal data class ReliabilityEvidence(
    val observations: ReliabilityObservations = ReliabilityObservations.unknown(),
    val manualReviews: Map<ManualSetting, ManualReview> = emptyMap(),
    val localCheck: LoopbackResult? = null,
    val backgroundTest: BackgroundTestEvidence = BackgroundTestEvidence.DEFAULT_VIVO_HOME_BLOCKED
) {
    // Neither a local GET nor a self-reported setting can provide background evidence.
    val backgroundReadiness: BackgroundReadiness
        get() = when (backgroundTest) {
            BackgroundTestEvidence.DEFAULT_VIVO_HOME_BLOCKED -> BackgroundReadiness.KNOWN_BLOCKED
            BackgroundTestEvidence.NOT_TESTED -> BackgroundReadiness.UNVERIFIED
        }
}

internal fun localVerificationAllowed(
    desiredRunning: Boolean,
    listenerRunning: Boolean,
    stopping: Boolean,
    hasError: Boolean
): Boolean = desiredRunning && listenerRunning && !stopping && !hasError

/** Search terms, not claims about a particular firmware/menu path or private settings entry. */
internal fun vendorSettingsHint(manufacturer: String?, brand: String?): String {
    val vendor = "${manufacturer.orEmpty()} ${brand.orEmpty()}".lowercase(java.util.Locale.ROOT)
    return when {
        "vivo" in vendor || "iqoo" in vendor ->
            "可在系统设置或 i管家搜索：后台高耗电、后台运行、自启动。不同 vivo / iQOO 版本菜单可能不同。"
        "oppo" in vendor || "realme" in vendor || "oneplus" in vendor ->
            "可在应用管理 / 电池中搜索：允许后台活动、自启动、耗电管理；菜单随系统版本变化。"
        "xiaomi" in vendor || "redmi" in vendor || "poco" in vendor ->
            "可在应用设置 / 安全中心搜索：自启动、省电策略、后台运行；菜单随系统版本变化。"
        "huawei" in vendor || "honor" in vendor ->
            "可在电池 / 应用启动管理中搜索：自动管理、后台活动、自启动；菜单随系统版本变化。"
        "samsung" in vendor ->
            "可在电池 / 后台使用限制中搜索：休眠应用、后台运行；菜单随系统版本变化。"
        else -> "在系统设置搜索：后台运行、后台高耗电、自启动、电池优化。厂商菜单名称可能不同，也可能不提供入口。"
    }
}
