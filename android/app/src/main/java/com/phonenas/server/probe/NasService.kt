package com.phonenas.server.probe

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.UserManager
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

/** A real file host: no wake lock, alarm, watchdog, audio, idle job or external restoration. */
class NasService : Service() {
    private val epoch = epochs.incrementAndGet()
    private val main = Handler(Looper.getMainLooper())
    private lateinit var store: NasStore
    @Volatile private var engine: NativeEngine? = null
    private var engineGeneration: Long? = null
    private var poll: ScheduledFuture<*>? = null
    private var launched = false
    private var notificationPhase = ""
    @Volatile private var closing = false
    private var shutdownStarted = false
    private var shutdownConfirmed = false
    private var foregroundOwned = false
    @Volatile private var observingNetwork = false
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    override fun onCreate() {
        super.onCreate()
        if (!getSystemService(UserManager::class.java).isUserUnlocked) return
        if (!ProbeApplication.cleanDebugExportsWhenUnlocked(this)) {
            RuntimeState.view = RuntimeView("启动被拒绝", error = "无法撤销旧调试临时导出；未启动文件服务。")
            return
        }
        store = NasStore(this)
        activeService = this
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "PhoneNAS 文件服务", NotificationManager.IMPORTANCE_LOW))
    }
    override fun onBind(intent: Intent?): IBinder? = null
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!::store.isInitialized) { stopSelf(); return START_NOT_STICKY }
        if (intent?.action == STOP) {
            stop(this)
            return START_NOT_STICKY
        }
        if (closing || launched) return if (closing) START_NOT_STICKY else START_STICKY
        val request = when (intent?.action) {
            START -> RecoveryPolicy.Request.USER
            RESTORE -> RecoveryPolicy.Request.RESTORE
            null -> RecoveryPolicy.Request.STICKY
            else -> { stopSelf(); return START_NOT_STICKY }
        }
        val guard = StartupGuard.get(this)
        try {
            if (!guard.admitService(request, store.desired, store.error.isNotEmpty())) {
                publish(RuntimeView("需要手动启动", error = guard.status().reason))
                stopSelf()
                return START_NOT_STICKY
            }
            val notification = notification("正在启动文件服务 · 未接管旧服务")
            if (android.os.Build.VERSION.SDK_INT >= 34) startForeground(ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            else startForeground(ID, notification)
            foregroundOwned = true
        } catch (_: Exception) {
            store.error = "系统拒绝启动前台文件服务；请检查通知、运行状态与后台限制。"
            publish(RuntimeView("启动被拒绝", error = store.error))
            stopSelf()
            return START_NOT_STICKY
        }
        launched = true
        observeNetwork()
        execute {
            check(!lifetime.quarantined) { "Unconfirmed previous stop" }
            check(store.prepareRecoveryBudget(guard.status().generation))
            if (request == RecoveryPolicy.Request.USER) check(store.recordRecovery(0))
            else if (store.dirty) {
                val next = RecoveryPolicy.nextRecovery(store.recoveries)
                if (next == null) { fail("本次开机异常恢复已达 3 次；请检查后明确重试。"); return@execute }
                check(store.recordRecovery(next))
                scheduleRecovery()
                return@execute
            }
            launchEngine()
        }
        return START_STICKY
    }

    private fun execute(action: () -> Unit) {
        worker.execute { guarded(action) }
    }
    private fun guarded(action: () -> Unit) {
        try { action() }
        catch (_: LinkageError) { fail("本机内核不可加载；请核对安装包与手机 ABI，未清除文件或锁。") }
        catch (_: Exception) { fail("文件服务无法安全继续：请检查目录授权、持久状态、证书或端口；未清除文件或锁。") }
    }
    private fun target(): HostConnection.Target {
        val settings = HostSettings(this)
        settings.preflightStorage()
        return HostConnection.target(settings.read(), if (observingNetwork) TailnetState.sample(this)
            else TailnetState.Snapshot())
    }
    private fun launchEngine() {
        if (closing || !store.desired) { main.post { beginShutdown() }; return }
        val desiredTarget = target()
        publish(RuntimeView(if (desiredTarget.waitingForVpn) "正在启动本机服务 · 等待固定 VPN" else "正在启动"))
        val config = if (desiredTarget.remoteAddress == null) store.localConfig() else try {
            store.config()
        } catch (failure: Exception) {
            // VPN can disappear between observation and config validation. Identity/storage errors do not downgrade.
            if (HostConnection.target(HostSettings(this).read(), TailnetState.sample(this)).remoteAddress != null) throw failure
            store.localConfig()
        }
        check(store.markDirty(true))
        engine = try {
            checkNotNull(lifetime.acquire(this) { NativeEngine(config) })
        } catch (failure: Exception) {
            if (desiredTarget.remoteAddress == null ||
                HostConnection.target(HostSettings(this).read(), TailnetState.sample(this)).remoteAddress != null) throw failure
            // Start is atomic in native code; a failed remote bind leaves no listener or engine behind.
            checkNotNull(lifetime.acquire(this) { NativeEngine(store.localConfig()) })
        }
        engineGeneration = engineGenerations.incrementAndGet()
        notificationPhase = ""
        inspect()
    }
    private fun inspect() {
        poll?.cancel(false)
        if (closing || !store.desired) { main.post { beginShutdown() }; return }
        val desiredTarget = target()
        val current = checkNotNull(engine)
        val stats = current.sample()
        if (stats.running && stats.remoteAddress != desiredTarget.remoteAddress.orEmpty()) {
            current.revokeRemote()
            publish(RuntimeView("正在安全调整入口；不回退 LAN"))
            if (!closeEngine()) { fail("入口调整时停止未确认；禁止第二个内核。"); return }
            launchEngine()
            return
        }
        if (stats.running) {
            val phase = when {
                desiredTarget.waitingForVpn -> "本机运行 · 等待固定 VPN"
                stats.remoteAddress.isNotBlank() -> "正在监听 · VPN HTTPS"
                else -> "正在监听 · 仅本机"
            }
            publish(RuntimeView(phase, stats, sampledAt = System.currentTimeMillis()))
            record(if (notificationPhase.isEmpty()) ReliabilityJournal.Event.READY else ReliabilityJournal.Event.CHECKPOINT, stats)
            if (notificationPhase != phase) {
                notificationPhase = phase
                main.post { notifySafely(if (stats.remoteAddress.isBlank()) phase else "VPN HTTPS 监听已确认 · 对端可达性需实际验证") }
            }
            poll = worker.schedule({ guarded { inspect() } }, 3, TimeUnit.SECONDS)
            return
        }
        if (stats.error.isNotBlank()) { fail("内核监听异常结束；请检查诊断后明确重试。"); return }
        val next = RecoveryPolicy.nextRecovery(store.recoveries)
        if (next == null) { fail("内核恢复已达 3 次；已停止，请检查后明确重试。"); return }
        check(store.recordRecovery(next))
        if (closeEngine()) scheduleRecovery() else fail("内核停止未确认；为避免重复内核已禁止恢复。")
    }
    private fun scheduleRecovery() {
        if (closing || !store.desired) { main.post { beginShutdown() }; return }
        val delay = RecoveryPolicy.recoveryDelaySeconds(store.recoveries)
        notificationPhase = ""
        record(ReliabilityJournal.Event.RECOVERY)
        publish(RuntimeView("恢复等待 ${delay} 秒 · ${store.recoveries}/3"))
        main.post { notifySafely("有界恢复等待 ${delay} 秒 · ${store.recoveries}/3") }
        poll = worker.schedule({ guarded { launchEngine() } }, delay, TimeUnit.SECONDS)
    }
    private fun closeEngine(): Boolean {
        val clean = lifetime.close(this)
        if (!clean) return false
        engine = null
        // Failure here cannot undo a confirmed native close, but must remain in the run evidence.
        if (!store.markDirty(false)) record(ReliabilityJournal.Event.FAILURE, EngineStats(),
            ReliabilityJournal.FailureCode.STORAGE_UNAVAILABLE)
        return true
    }
    private fun fail(reason: String) {
        closing = true
        try { store.error = reason }
        finally {
            record(ReliabilityJournal.Event.FAILURE, failure = ReliabilityJournal.FailureCode.ENGINE_FAILURE)
            main.post { beginShutdown() }
        }
    }
    private fun beginShutdown() {
        if (shutdownStarted) return
        shutdownStarted = true
        closing = true
        poll?.cancel(false)
        stopObservingNetwork()
        runCatching { engine?.revokeRemote() }
        publish(RuntimeView("正在停止", RuntimeState.view.stats, error = store.error))
        notifySafely("正在停止；尚未确认内核关闭")
        worker.execute {
            record(ReliabilityJournal.Event.STOP_REQUESTED)
            val clean = runCatching { closeEngine() }.getOrDefault(false)
            if (clean) record(ReliabilityJournal.Event.STOP_CONFIRMED, EngineStats())
            else record(ReliabilityJournal.Event.FAILURE, failure = ReliabilityJournal.FailureCode.STOP_UNCONFIRMED)
            main.post {
                if (epoch != epochs.get()) return@post
                if (clean) {
                    shutdownConfirmed = true
                    RuntimeState.view = RuntimeView(if (store.error.isEmpty()) "已停止" else "错误 · 已停止", error = store.error)
                    stopForeground(STOP_FOREGROUND_REMOVE)
                    foregroundOwned = false
                    getSystemService(NotificationManager::class.java).cancel(ID)
                    stopSelf()
                } else {
                    store.error = "内核停止未确认；本进程禁止再次启动，请检查后强行停止本应用。"
                    RuntimeState.view = RuntimeView("停止未确认", error = store.error)
                    notifySafely("停止未确认；不能视为服务已停")
                }
            }
        }
    }
    private fun record(event: ReliabilityJournal.Event, stats: EngineStats = RuntimeState.view.stats,
                       failure: ReliabilityJournal.FailureCode? = null) {
        runCatching { RuntimeJournal.observe(this, event, store.desired, store.recoveries, engineGeneration, stats, failure) }
    }
    private fun observeNetwork() {
        val manager = getSystemService(ConnectivityManager::class.java)
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = changed(false)
            override fun onLost(network: Network) = changed(true)
            override fun onLinkPropertiesChanged(network: Network, properties: LinkProperties) = changed(false)
            private fun changed(lost: Boolean) {
                if (closing) return
                if (lost || runCatching {
                    val pin = HostSettings(this@NasService).read().remoteIp
                    pin != null && !TailnetState.sample(this@NasService).matchesPinnedIp(pin)
                }.getOrDefault(true)) runCatching { engine?.revokeRemote() }
                execute { if (!closing && engine != null) inspect() }
            }
        }
        try {
            manager.registerNetworkCallback(NetworkRequest.Builder()
                .removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
                .addTransportType(NetworkCapabilities.TRANSPORT_VPN).build(), callback)
            networkCallback = callback
            observingNetwork = true
        } catch (_: Exception) { observingNetwork = false }
    }
    private fun stopObservingNetwork() {
        networkCallback?.let { runCatching { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) } }
        networkCallback = null
        observingNetwork = false
    }
    private fun publish(value: RuntimeView) {
        main.post { if (epoch == epochs.get()) RuntimeState.view = value }
    }
    override fun onTaskRemoved(rootIntent: Intent?) { super.onTaskRemoved(rootIntent) }
    override fun onDestroy() {
        if (activeService === this) {
            activeService = null
            RuntimeState.configurationRevision++
        }
        closing = true
        poll?.cancel(false)
        stopObservingNetwork()
        if (::store.isInitialized && launched && !shutdownConfirmed && !shutdownStarted) {
            worker.execute {
                val clean = runCatching { closeEngine() }.getOrDefault(false)
                if (!clean) store.error = "系统结束时内核停止未确认；未清除文件或锁。"
                publish(RuntimeView(if (clean) "系统已结束" else "停止未确认", error = store.error))
            }
        }
        super.onDestroy()
    }
    private fun notifySafely(text: String) {
        if (foregroundOwned) runCatching { getSystemService(NotificationManager::class.java).notify(ID, notification(text)) }
    }
    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this, 1, Intent(this, NasService::class.java).setAction(STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_nas)
            .setContentTitle("PhoneNAS · 独立文件服务").setContentText(text)
            .setContentIntent(open).setOngoing(true).setOnlyAlertOnce(true)
            .addAction(Notification.Action.Builder(null, "停止", stop).build()).build()
    }
    companion object {
        private const val CHANNEL = "nas_file_host"
        private const val ID = 18888
        private const val START = "com.phonenas.server.probe.START"
        private const val STOP = "com.phonenas.server.probe.STOP"
        private const val RESTORE = "com.phonenas.server.probe.RESTORE"
        private val epochs = AtomicInteger()
        private val engineGenerations = AtomicLong()
        private var activeService: NasService? = null
        private val stopper = Executors.newSingleThreadExecutor()
        private val lifetime = EngineLifetime<NativeEngine>(stopper, { it.stop() })
        private val worker = Executors.newSingleThreadScheduledExecutor()
        fun configurationMutable(): Boolean = activeService == null && lifetime.stoppedConfirmed && !RuntimeState.view.stats.running
        fun start(context: Context, user: Boolean): Boolean {
            if (!context.getSystemService(UserManager::class.java).isUserUnlocked) return false
            if (!ProbeApplication.cleanDebugExportsWhenUnlocked(context)) {
                RuntimeState.view = RuntimeView("启动被拒绝", error = "无法撤销旧调试临时导出；请检查，不提供远程访问。")
                return false
            }
            val guard = StartupGuard.get(context)
            if (!user && !guard.canReceiveBoot()) return false
            val store = NasStore(context)
            return try {
                if (user) {
                    if (lifetime.quarantined || activeService?.closing == true) {
                        RuntimeState.view = RuntimeView("停止未确认", error = "先前内核尚未确认关闭；不能再次启动。")
                        return false
                    }
                    check(store.desired(true))
                    check(guard.userStart())
                    store.error = ""
                    check(store.recordRecovery(0))
                } else if (!guard.admitBoot(store.desired, store.error.isNotEmpty())) return false
                if (!store.desired || store.error.isNotEmpty() || lifetime.quarantined) return false
                context.startForegroundService(Intent(context, NasService::class.java).setAction(if (user) START else RESTORE))
                true
            } catch (_: Exception) {
                store.error = "无法可靠启动文件服务；请检查系统许可或持久状态后明确重试。"
                RuntimeState.view = RuntimeView("启动被拒绝", error = store.error)
                false
            }
        }
        fun stop(context: Context) {
            val app = context.applicationContext
            val unlocked = { app.getSystemService(UserManager::class.java).isUserUnlocked }
            val persistence = NasPersistence(AndroidStatePreferences(app.createDeviceProtectedStorageContext()
                .getSharedPreferences("nas_boot", Context.MODE_PRIVATE)),
                { if (unlocked()) AndroidStatePreferences(app.getSharedPreferences("nas", Context.MODE_PRIVATE)) else null },
                unlocked = unlocked)
            persistence.stop {
                try { StartupGuard.get(app).userStop() }
                finally {
                    activeService?.beginShutdown()
                    if (activeService == null) {
                        RuntimeState.view = RuntimeView("已停止", error = persistence.error)
                        if (unlocked() && lifetime.stoppedConfirmed) worker.execute {
                            runCatching {
                                RuntimeJournal.observe(app, ReliabilityJournal.Event.STOP_REQUESTED, false, 0, null, EngineStats())
                                if (persistence.error.isNotEmpty()) RuntimeJournal.observe(app, ReliabilityJournal.Event.FAILURE,
                                    false, 0, null, EngineStats(), ReliabilityJournal.FailureCode.STORAGE_UNAVAILABLE)
                                RuntimeJournal.observe(app, ReliabilityJournal.Event.STOP_CONFIRMED, false, 0, null, EngineStats())
                            }
                        }
                    }
                }
            }
        }
    }
}
