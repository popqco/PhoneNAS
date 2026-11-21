package com.phonenas.server.probe

import android.Manifest
import android.app.ActivityManager
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.os.Environment
import android.provider.Settings
import android.view.WindowManager
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import java.text.DateFormat
import java.util.Date
import java.util.concurrent.Executors
import java.util.concurrent.Future

class MainActivity : ComponentActivity() {
    private lateinit var store: NasStore
    private lateinit var hostSettings: HostSettings
    private lateinit var folderPicker: ActivityResultLauncher<Uri?>
    private lateinit var certificateExporter: ActivityResultLauncher<String>
    private lateinit var journalExporter: ActivityResultLauncher<String>
    private var pendingCertificate: ByteArray? = null
    private var pendingCertificateFingerprint: String? = null
    private var pendingJournal: ByteArray? = null
    private var pendingJournalRun: String? = null
    private var host by mutableStateOf(HostConfiguration())
    private var hostReadable by mutableStateOf(true)
    private var fileAccessAllowed by mutableStateOf(false)
    private var chosenRoot by mutableStateOf<String?>(null)
    private var newAlias by mutableStateOf("")
    private var newReadOnly by mutableStateOf(true)
    private var tailnetStatus by mutableStateOf("尚未观察 VPN 地址")
    private var remoteCandidate by mutableStateOf<String?>(null)
    private var confirmRemote by mutableStateOf(false)
    private var removeShare by mutableStateOf<ShareConfiguration?>(null)
    private var revision by mutableIntStateOf(0)
    private var password by mutableStateOf<String?>(null)
    private var message by mutableStateOf("")
    private var observations by mutableStateOf(ReliabilityObservations.unknown())
    private var manualReviews by mutableStateOf<Map<ManualSetting, ManualReview>>(emptyMap())
    private var localResult by mutableStateOf<LoopbackResult?>(null)
    private var localStatus by mutableStateOf("尚未检查；仅在本页明确点击后读取固定只读演示文件。")
    private var localBusy by mutableStateOf(false)
    private val verificationWorker = Executors.newSingleThreadExecutor()
    private val verificationDeadlines = Executors.newSingleThreadScheduledExecutor()
    private var verification: LoopbackVerifier? = null
    private var verificationFuture: Future<*>? = null
    private var verificationGeneration = 0L
    private var resumed = false
    private val reliabilityPrefs by lazy { getSharedPreferences("reliability_setup", MODE_PRIVATE) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = NasStore(this)
        hostSettings = HostSettings(this)
        pendingCertificateFingerprint = savedInstanceState?.getString("public_certificate_export")
        pendingJournalRun = savedInstanceState?.getString("journal_export_run")
        folderPicker = registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
            if (uri != null) runCatching { hostSettings.resolveTree(uri).absolutePath }
                .onSuccess { chosenRoot = it }
                .onFailure { message = "未采用此目录：只支持内置存储中的独立现有目录；不导出整个存储、Android、私有目录或其他提供者。" }
        }
        certificateExporter = registerForActivityResult(ActivityResultContracts.CreateDocument("application/x-pem-file")) { uri ->
            if (uri != null) runCatching {
                val data = pendingCertificate ?: readPublicCertificate()
                check(certificateFingerprint(data) == checkNotNull(pendingCertificateFingerprint))
                checkNotNull(contentResolver.openOutputStream(uri, "w")).use { it.write(data) }
            }.onSuccess { message = "已导出公开服务器证书（不含私钥或密码）。仅在授权客户端核对 SHA-256 后信任。" }
                .onFailure { message = "证书导出未完成或页面重建后身份不一致，请重新导出；没有更换证书、私钥或账号。" }
            pendingCertificate = null
            pendingCertificateFingerprint = null
        }
        journalExporter = registerForActivityResult(ActivityResultContracts.CreateDocument("text/plain")) { uri ->
            if (uri != null) runCatching {
                check(RuntimeJournal.summary(this).runId?.toString() == checkNotNull(pendingJournalRun))
                val data = pendingJournal ?: RuntimeJournal.export(this).let {
                    check(it.error == null && !it.truncated)
                    it.text.toByteArray(Charsets.US_ASCII)
                }
                checkNotNull(contentResolver.openOutputStream(uri, "w")).use { it.write(data) }
            }.onSuccess { message = "已导出脱敏本机运行记录；它不是持续远端可达或 72 小时验收通过证明。" }
                .onFailure { message = "运行记录导出未完成或记录组已改变，请重新导出；没有修改共享数据。" }
            pendingJournal = null
            pendingJournalRun = null
        }
        reloadHost()
        manualReviews = ManualSetting.entries.associateWith { setting ->
            decodeManualReview(reliabilityPrefs.getString("${setting.preferenceKey}_state", null),
                reliabilityPrefs.getLong("${setting.preferenceKey}_time", 0L))
        }
        setContent { MaterialTheme { Workbench() } }
    }
    override fun onSaveInstanceState(outState: Bundle) {
        pendingCertificateFingerprint?.let { outState.putString("public_certificate_export", it) }
        pendingJournalRun?.let { outState.putString("journal_export_run", it) }
        super.onSaveInstanceState(outState)
    }
    override fun onResume() {
        super.onResume()
        resumed = true
        refreshObservations()
        reloadHost()
        fileAccessAllowed = Environment.isExternalStorageManager()
        observeTailnet()
        revision++
    }
    override fun onPause() {
        resumed = false
        password = null
        cancelVerification("页面离开：未完成结果已取消并丢弃；不会在后台继续验收。")
        super.onPause()
    }
    override fun onDestroy() {
        verificationWorker.shutdownNow()
        verificationDeadlines.shutdownNow()
        super.onDestroy()
    }
    private fun reloadHost() {
        if (!::hostSettings.isInitialized) return
        runCatching { hostSettings.read() }.onSuccess { host = it; hostReadable = true }
            .onFailure {
                hostReadable = false
                message = "共享配置无法可靠读取；已阻止启动和修改，不会重置配置或退回演示目录。"
            }
    }
    private fun observeTailnet() {
        val observed = runCatching { TailnetState.sample(this) }.getOrNull()
        remoteCandidate = observed?.takeIf { it.ready }?.ipv4
        tailnetStatus = remoteCandidate?.let { "系统观察到 VPN IPv4：$it；不能据此自动证明 VPN 归属。" }
            ?: "未观察到唯一且可核对的 VPN IPv4；远程入口不可启用，不会回退 LAN。"
    }
    private fun canEditHost(): Boolean = hostReadable && !store.desired && NasService.configurationMutable()
    private fun saveHost(configuration: HostConfiguration): Boolean {
        if (!canEditHost()) {
            message = "请先停止服务并等待内核关闭确认，再修改共享或入口。未写入配置。"
            return false
        }
        return runCatching { hostSettings.save(configuration, stoppedConfirmed = true) }
            .onSuccess {
                cancelVerification("共享或入口已改变；旧检查结果已丢弃。")
                reloadHost()
                revision++
                message = "配置已保存；没有自动启动服务，也没有删除共享目录、文件或保护锁。"
            }.onFailure {
                message = "配置未保存：请核对目录授权、独立路径、唯一合法名称与当前 VPN 地址；持久化失败不会被当成成功。"
            }.isSuccess
    }
    private fun certificateFingerprint(data: ByteArray): String {
        val certificate = java.security.cert.CertificateFactory.getInstance("X.509")
            .generateCertificate(data.inputStream())
        return java.security.MessageDigest.getInstance("SHA-256")
            .digest(certificate.encoded).joinToString("") { "%02x".format(it) }
    }
    private fun readPublicCertificate(): ByteArray {
        val file = java.io.File(filesDir, "tls/server.crt")
        check(file.isFile && file.length() in 1..32768)
        val data = file.readBytes()
        val active = RuntimeState.view.stats.certificateSha256.replace(":", "").lowercase()
        check(active.isEmpty() || active == certificateFingerprint(data))
        return data
    }
    private fun exportCertificate() {
        runCatching { readPublicCertificate() }.onSuccess {
            pendingCertificate = it
            pendingCertificateFingerprint = certificateFingerprint(it)
            certificateExporter.launch("PhoneNAS-server.crt")
        }.onFailure {
            message = "没有可核对的公开证书；请先成功启动固定 VPN HTTPS 入口。未生成或更换密钥。"
        }
    }
    private fun appDetails() = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
        Uri.parse("package:$packageName")).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)

    private fun settings(intent: Intent) {
        val opened = runCatching { startActivity(intent) }.isSuccess ||
            runCatching { startActivity(appDetails()) }.isSuccess
        if (!opened) message = "系统未提供此入口；请手动在系统设置搜索应用名。没有更改任何设置。"
    }
    private fun refreshObservations() {
        val now = System.currentTimeMillis()
        fun observe(read: () -> Boolean): ObservedValue = try {
            ObservedValue(if (read()) EvidenceValue.YES else EvidenceValue.NO, now)
        } catch (_: Exception) { ObservedValue(EvidenceValue.UNKNOWN, now, "系统未提供可读结果") }
        val manager = getSystemService(NotificationManager::class.java)
        val channel = try {
            val channel = manager.getNotificationChannel("nas_file_host")
            if (channel == null) ObservedValue(EvidenceValue.UNKNOWN, now, "通道尚未创建；不是已允许")
            else ObservedValue(if (channel.importance == NotificationManager.IMPORTANCE_NONE)
                EvidenceValue.NO else EvidenceValue.YES, now)
        } catch (_: Exception) { ObservedValue(EvidenceValue.UNKNOWN, now, "通道不可读取") }
        observations = ReliabilityObservations(
            notificationRuntime = if (Build.VERSION.SDK_INT >= 33) observe {
                checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            } else ObservedValue(EvidenceValue.UNKNOWN, now, "本系统不适用运行时通知许可"),
            notificationApp = observe { manager.areNotificationsEnabled() },
            notificationChannel = channel,
            batteryExempt = observe { getSystemService(PowerManager::class.java)
                .isIgnoringBatteryOptimizations(packageName) },
            backgroundRestricted = observe { getSystemService(ActivityManager::class.java)
                .isBackgroundRestricted },
            runtimePermissionRequired = Build.VERSION.SDK_INT >= 33
        )
    }
    private fun saveReview(setting: ManualSetting, state: ManualReviewState) {
        val review = ManualReview(state, System.currentTimeMillis())
        reliabilityPrefs.edit().putString("${setting.preferenceKey}_state", state.name)
            .putLong("${setting.preferenceKey}_time", checkNotNull(review.recordedAt)).apply()
        manualReviews = manualReviews + (setting to review)
    }
    private fun canVerify(): Boolean {
        val state = RuntimeState.view
        return hostReadable && host.mode == HostMode.DEMO &&
            localVerificationAllowed(store.desired, state.stats.running,
                state.phase.contains("停止"), state.error.isNotEmpty() || store.error.isNotEmpty())
    }
    private fun cancelVerification(reason: String) {
        verificationGeneration++
        verification?.cancel()
        verificationFuture?.cancel(true)
        verification = null
        verificationFuture = null
        val hadResult = localResult != null
        localResult = null
        if (localBusy || hadResult) localStatus = reason
        localBusy = false
    }
    private fun verifyLocalRead() {
        if (localBusy || !resumed) return
        localResult = null
        if (!canVerify()) {
            localStatus = "${timestamp(System.currentTimeMillis())} · 当前未确认服务运行（已停止、停止中或有错误）；未发送请求，也不会启动服务。"
            return
        }
        localBusy = true
        localStatus = "正在前台读取固定 readonly/readme.txt；最多 4 秒，不写文件。"
        val generation = ++verificationGeneration
        val verifier = LoopbackVerifier(verificationDeadlines)
        verification = verifier
        verificationFuture = verificationWorker.submit {
            var unavailable: String? = null
            val result = try {
                val credential = if (store.desired) store.existingPassword() else null
                if (!store.desired) {
                    unavailable = "服务已请求停止；未发送检查请求，不会自动启动。"
                    null
                } else if (credential == null) {
                    unavailable = "没有已存凭据；未发送请求，也未生成密钥、目录或密码。"
                    null
                } else verifier.verify(USERNAME, credential)
            } catch (_: Exception) {
                unavailable = "无法读取现有凭据或完成检查；未重置密钥、生成配置或启动服务。"
                null
            }
            runOnUiThread {
                if (!resumed || generation != verificationGeneration) return@runOnUiThread
                verification = null
                verificationFuture = null
                localBusy = false
                if (!canVerify()) {
                    localResult = null
                    localStatus = "服务已停止或状态改变；本次结果已丢弃，未启动或重启服务。"
                } else {
                    localResult = result
                    localStatus = unavailable?.let { "${timestamp(System.currentTimeMillis())} · $it" }
                        ?: "本次前台检查已结束；不构成后台或远端验收。"
                }
            }
        }
    }
    @Composable private fun Workbench() {
        var tab by rememberSaveable { mutableIntStateOf(0) }
        var hints by remember { mutableStateOf(!store.prefs.getBoolean("hints_seen", false)) }
        val state = RuntimeState.view
        val startup = StartupGuard.get(this).status()
        LaunchedEffect(password != null) {
            if (password != null) window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
            else window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
        }
        val notification = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            refreshObservations()
            message = "${timestamp(System.currentTimeMillis())} · 通知运行时请求返回：${if (granted) "已允许" else "未获允许"}。应用总开关和服务通道分别见下方观测；未更改运行意愿。"
            revision++
        }
        // Returning from settings refreshes permission/system observations without starting the engine.
        @Suppress("UNUSED_VARIABLE") val refresh = revision
        val verificationAllowed = canVerify()
        LaunchedEffect(verificationAllowed) {
            if (!verificationAllowed) cancelVerification("服务已停止或状态改变；本次检查已取消并丢弃。")
        }
        Scaffold(bottomBar = {
            NavigationBar {
                listOf("首页", "共享", "可靠性", "诊断").forEachIndexed { index, title ->
                    NavigationBarItem(selected = tab == index, onClick = {
                        if (tab != index) cancelVerification("已离开可靠性页；未完成的检查结果已丢弃。")
                        tab = index; password = null
                    },
                        icon = { Text(listOf("A", "S", "R", "D")[index]) }, label = { Text(title) })
                }
            }
        }) { inset ->
            Column(Modifier.fillMaxSize().padding(inset).verticalScroll(rememberScrollState())
                .padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("PhoneNAS", style = MaterialTheme.typography.headlineLarge)
                Text("${BuildConfig.VERSION_NAME} · 独立手机文件服务 · 未接管旧服务",
                    style = MaterialTheme.typography.titleMedium)
                if (!hostReadable) Text("配置读取失败：启动与共享修改已阻止。", color = MaterialTheme.colorScheme.error)
                when (tab) {
                    0 -> {
                        Panel("文件服务 · ${state.phase}") {
                            Text("用户期望：${if (store.desired) "运行" else "停止"}")
                            if (startup.blocked) Text(startup.reason, color = MaterialTheme.colorScheme.error)
                            Text(if (state.stats.running) "监听已确认：http://${state.stats.address}/" else "当前未确认监听；不代表远端可达。")
                            Text(if (state.stats.remoteAddress.isBlank()) "远程入口：未确认监听；不回退 LAN。"
                                else "HTTPS 监听：https://${state.stats.remoteAddress}/；监听存在不等于对端已验证。")
                            Text("共享模式：${if (host.mode == HostMode.DEMO) "隔离演示" else "自定义内置目录"}；远程访问不依赖 USB 或电脑保活。")
                            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                                Button(enabled = hostReadable && !state.stats.running
                                    && state.phase !in listOf("正在停止", "停止未确认"), onClick = {
                                    NasService.start(this@MainActivity, user = true); revision++
                                }) { Text(if (store.error.isEmpty()) "启动文件服务" else "明确重试") }
                                OutlinedButton(onClick = {
                                    cancelVerification("已点击停止；本次检查已取消并丢弃。")
                                    NasService.stop(this@MainActivity); password = null; revision++
                                }) { Text("停止") }
                            }
                        }
                        Panel("部署与访问") {
                            Text("先在共享页选择内置目录并按需启用固定 VPN HTTPS，再明确启动。官方 Tailscale 独立运行；本应用不会代登录、修改旧服务或自动开放局域网。")
                            TextButton(onClick = { tab = 1; password = null }) { Text("配置共享与 HTTPS") }
                            TextButton(onClick = { tab = 2; password = null }) { Text("检查通知与后台限制") }
                        }
                        Panel("连接凭据与证书") {
                            Text("用户名：$USERNAME。密码仅在此明确点击后显示；离开页面即隐藏。所有文件和控制方法都需要认证。")
                            Text("本机 HTTP 只绑定 127.0.0.1:18888；远端只使用固定 VPN IPv4 的 HTTPS，不允许客户端忽略证书校验。")
                            if (state.stats.certificateSha256.isNotEmpty()) {
                                SelectionContainer { Text("证书 SHA-256：\n${state.stats.certificateSha256}") }
                                TextButton(onClick = { exportCertificate() }) { Text("导出公开证书（不含私钥）") }
                            }
                            OutlinedButton(onClick = {
                                if (password != null) password = null else {
                                    window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
                                    runCatching { store.password() }.onSuccess { password = it }.onFailure {
                                        window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
                                        message = "无法解密本机凭据；不会重置密钥或文件锁。"
                                    }
                                }
                            }) { Text(if (password == null) "明确显示连接密码" else "隐藏密码") }
                            password?.let { SelectionContainer { Text(it) } }
                        }
                        TextButton(onClick = { hints = true }) { Text("首次使用说明") }
                    }
                    1 -> SharedFoldersPanel()
                    2 -> {
                        Panel("运行与恢复") {
                            Text("期望运行保存在解锁后存储。重启后仅在首次解锁、系统允许且没有硬错误时尝试恢复实际文件服务；锁定启动只记录等待，不读凭据或文件。")
                            Text("划掉页面不等于停止。系统回收可通过 START_STICKY 重建；原生进程崩溃恢复最多 3 次，硬错误不后台重试。系统强行停止和厂商限制必须尊重，无法保证常驻。")
                            Text("点击停止先保存“不运行”，再关闭服务。没有音频、定时唤醒、WorkManager、电脑看门狗或自动清锁；本版不持有 CPU 唤醒锁。")
                            Text("异常恢复计数：${store.recoveries}/3；持久错误：${store.error.ifBlank { "无" }}")
                            Text("启动保护：${startup.reason}\n开机会话：${startup.generation ?: "未知"}。强停后仅打开页面不会自动启用；明确点击启动后才解除保护。")
                        }
                        ReliabilityPanel(verificationAllowed, onRequestNotification = {
                            if (Build.VERSION.SDK_INT >= 33) notification.launch(Manifest.permission.POST_NOTIFICATIONS)
                        })
                    }
                    3 -> {
                        Panel("本进程引擎快照 · 不含凭据") {
                            Text("${BuildConfig.APPLICATION_ID} · ${BuildConfig.VERSION_NAME}\n期望监听 $LISTEN\n采样：${if (state.sampledAt == 0L) "尚未确认" else DateFormat.getTimeInstance().format(Date(state.sampledAt))}")
                            Text("请求：${state.stats.requests}\n收到：${state.stats.received} bytes\n发出：${state.stats.sent} bytes")
                            Text("状态：${state.phase}\n错误：${state.error.ifBlank { store.error.ifBlank { "无" } }}")
                            Text("统计仅反映本机监听和已处理请求，不证明电脑/远端可达。运行时每 3 秒采样；停止时没有轮询或唤醒。")
                        }
                        Panel("本机有界运行记录 · 非验收结论") {
                            val journal = runCatching { RuntimeJournal.summary(this@MainActivity) }.getOrNull()
                            Text("记录状态：${when { journal == null -> "不可读取"; journal.error != null -> "故障 ${journal.error}"; journal.active -> "记录中"; else -> "未开始或已结束" }}")
                            Text("事件 ${journal?.eventCount ?: 0}；同进程/代次相邻运行观察累计 ${(journal?.observedElapsedMillis ?: 0) / 60_000} 分钟；存在空白/中断：${journal?.unexplainedGap ?: true}；故障 ${journal?.failureCount ?: 0}。")
                            Text("仅在真实服务内追加事件与稀疏非唤醒观察，最多 2 MiB。没有 HTTP 自检、定时拉起或电脑任务；时长不能自动变成全天稳定通过。")
                            TextButton(enabled = canEditHost() && journal?.active != true, onClick = {
                                message = if (RuntimeJournal.begin(this@MainActivity, stoppedConfirmed = canEditHost()))
                                    "已明确开始本机运行记录；请另行启动服务。未自动开始远端或 72 小时验收。"
                                else "未开始记录：${RuntimeJournal.error.ifBlank { "请检查已停止状态或旧记录" }}。"
                                revision++
                            }) { Text("明确开始本机运行记录") }
                            TextButton(onClick = {
                                val exported = RuntimeJournal.export(this@MainActivity)
                                if (exported.error != null || exported.truncated) {
                                    message = "记录未完整导出：${exported.error ?: "超出边界"}；不能用不完整记录报告通过。"
                                } else {
                                    pendingJournal = exported.text.toByteArray(Charsets.US_ASCII)
                                    pendingJournalRun = RuntimeJournal.summary(this@MainActivity).runId?.toString()
                                    journalExporter.launch("PhoneNAS-runtime.txt")
                                }
                            }) { Text("导出脱敏运行记录") }
                            if (RuntimeJournal.error.isNotEmpty()) Text("记录故障：${RuntimeJournal.error}", color = MaterialTheme.colorScheme.error)
                        }
                        Panel("安全与验收") {
                            Text("凭据由 SecureRandom 生成，使用 Android Keystore AES-GCM 加密保存；仅明确点击后显示。无日志、Toast 或自动剪贴板输出密码。")
                            Text("仅 DEBUG + 受系统 DUMP 权限保护的测试入口可为固定演示根生成私有 test-access.json，供授权 run-as 验收读取。普通应用、桌面启动和开机不能触发。")
                            Text("specialUse 用于真实文件托管。系统启动资格、分发审核及 OEM 恢复仍有平台限制；候选版本的可靠性必须以该版本真实验收证据为准。")
                        }
                    }
                }
                if (store.error.isNotEmpty()) Text(store.error, color = MaterialTheme.colorScheme.error)
                if (message.isNotEmpty()) Text(message)
            }
        }
        if (hints) AlertDialog(onDismissRequest = { hints = false }, title = { Text("独立部署，不接管旧服务") },
            text = { Text("默认仍是隔离演示。你可在共享页选择多个独立内置目录，逐项设置只读或可读写；需要时经正常系统页面允许文件访问。远程仅使用固定 VPN 地址的 HTTPS，并要求客户端核对证书。停止、移除共享都不删除文件或清保护锁；系统强停后需明确启动，重启恢复和后台能力以实际证据为准。") },
            confirmButton = { TextButton(onClick = {
                store.prefs.edit().putBoolean("hints_seen", true).apply(); hints = false
            }) { Text("了解，进入 PhoneNAS") } })
    }

    @Composable private fun SharedFoldersPanel() {
        @Suppress("UNUSED_VARIABLE") val configurationChanged = RuntimeState.configurationRevision
        val editable = canEditHost()
        Panel("共享配置 · ${if (host.mode == HostMode.DEMO) "隔离演示" else "自定义目录"}") {
            Text("修改只在服务停止且内核关闭确认后允许。保存不会自动启动；禁用或移除仅撤销导出，不删除目录、历史或保护锁。")
            if (!editable) Text("当前不可修改：请先停止并等候关闭确认。", color = MaterialTheme.colorScheme.error)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(enabled = editable && host.mode != HostMode.DEMO,
                    onClick = { saveHost(host.copy(mode = HostMode.DEMO)) }) { Text("使用隔离演示") }
                OutlinedButton(enabled = editable && host.mode != HostMode.CUSTOM,
                    onClick = { saveHost(host.copy(mode = HostMode.CUSTOM)) }) { Text("使用自定义目录") }
            }
            Text("内置存储文件不会因移除共享而删除。卸载/清数据仍会丢失本应用私有凭据和控制状态，不能当作安全重置或迁移。")
        }
        if (host.mode == HostMode.DEMO) Panel("隔离演示目录") {
            val root = runCatching { store.demoRoot.absolutePath }.getOrDefault("专属存储暂不可用")
            Text("/test/ · 可读写\n$root/test\n/readonly/ · 只读\n$root/readonly")
            Text("首次启动仅创建这些 App 专属演示文件；卸载会删除演示数据。不会导出个人文件或接管旧服务。")
        }
        Panel("内置目录授权与共享列表") {
            Text("所有文件访问：${if (fileAccessAllowed) "系统已授权" else "未授权"}。公共目录读写需要经正常系统页面明确允许；选目录不是绕过此授权。")
            TextButton(onClick = { settings(Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                Uri.parse("package:$packageName"))) }) { Text("打开本应用文件访问授权") }
            host.shares.forEach { share ->
                HorizontalDivider()
                Text("/${share.alias}/ · ${if (share.readOnly) "只读" else "可读写"} · ${if (share.enabled) "已启用" else "已禁用"}\n${share.root}")
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    TextButton(enabled = editable, onClick = {
                        saveHost(host.copy(shares = host.shares.map {
                            if (it == share) it.copy(enabled = !it.enabled) else it
                        }))
                    }) { Text(if (share.enabled) "禁用" else "启用") }
                    TextButton(enabled = editable, onClick = {
                        saveHost(host.copy(shares = host.shares.map {
                            if (it == share) it.copy(readOnly = !it.readOnly) else it
                        }))
                    }) { Text(if (share.readOnly) "改为可读写" else "改为只读") }
                    TextButton(enabled = editable, onClick = { removeShare = share }) { Text("移除导出") }
                }
            }
            if (host.shares.isEmpty()) Text("尚未添加自定义共享。每个共享必须是独立目录，不能同根或父子重叠。")
            OutlinedTextField(value = newAlias, onValueChange = { newAlias = it },
                enabled = editable, singleLine = true, label = { Text("固定 URL 名称，如 documents") },
                supportingText = { Text("1–40 位英文字母、数字、_ 或 -；不随目录名变化") }, modifier = Modifier.fillMaxWidth())
            Text(chosenRoot?.let { "待添加目录：$it" } ?: "尚未选择目录（仅内置存储，禁止整个存储/Android/私有目录）。")
            Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                Checkbox(checked = newReadOnly, enabled = editable, onCheckedChange = { newReadOnly = it })
                Text("只读（普通浏览；完整接力还需要控制写锁）")
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(enabled = editable, onClick = {
                    runCatching { folderPicker.launch(null) }.onFailure { message = "系统目录选择器不可用；未改变共享。" }
                }) { Text("选择内置目录") }
                Button(enabled = editable && chosenRoot != null && newAlias.isNotBlank(), onClick = {
                    val root = chosenRoot ?: return@Button
                    if (saveHost(host.copy(shares = host.shares + ShareConfiguration(newAlias.trim(), root, newReadOnly)))) {
                        chosenRoot = null; newAlias = ""; newReadOnly = true
                    }
                }) { Text("添加共享") }
            }
            Text("外部文件管理器修改直接可见；请先结束编辑再接力。本应用不声称跨文件原子快照，不以文件时间戳代替内容核对。")
        }
        Panel("固定 VPN HTTPS · 不开放 LAN") {
            Text(tailnetStatus)
            Text("已保存地址：${host.remoteIp ?: "未启用"}；HTTPS 端口 18889。只绑定已确认的 VPN IPv4，并限制来源；地址不匹配或丢失时关闭服务，不回退其他入口。")
            Text("首次证书由本机私钥生成并稳定保存；客户端必须核对 SHA-256 并信任公开证书，不能使用忽略 TLS 校验。系统观察不证明 VPN 归属，请核对独立官方 Tailscale 中的本机地址。")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(onClick = { observeTailnet(); revision++ }) { Text("重新观察地址") }
                OutlinedButton(enabled = editable && remoteCandidate != null, onClick = { confirmRemote = true }) { Text("确认并启用 HTTPS") }
            }
            TextButton(enabled = editable && host.remoteIp != null, onClick = {
                saveHost(host.copy(remoteIp = null))
            }) { Text("停用远程入口（保留证书和文件）") }
        }
        removeShare?.let { target -> AlertDialog(onDismissRequest = { removeShare = null },
            title = { Text("只移除导出？") }, text = { Text("将撤销 /${target.alias}/ 的共享配置。目录与其中的文件、历史和保护锁都不会被删除。") },
            confirmButton = { TextButton(onClick = {
                saveHost(host.copy(shares = host.shares.filterNot { it == target })); removeShare = null
            }) { Text("仅移除导出") } }, dismissButton = { TextButton(onClick = { removeShare = null }) { Text("取消") } }) }
        if (confirmRemote) AlertDialog(onDismissRequest = { confirmRemote = false },
            title = { Text("固定本机 VPN 地址") }, text = {
                Text("请确认 ${remoteCandidate ?: "未知"} 与独立官方 Tailscale 显示的本机 IPv4 一致。启用后只在此地址提供经过认证的 HTTPS，不更改 Tailscale 或旧 WebDAV。证书绑定此地址；地址变化不会自动换证书。")
            }, confirmButton = { TextButton(onClick = {
                remoteCandidate?.let { saveHost(host.copy(remoteIp = it)) }; confirmRemote = false
            }) { Text("地址一致，保存") } }, dismissButton = { TextButton(onClick = { confirmRemote = false }) { Text("取消") } })
    }

    @Composable private fun ReliabilityPanel(verificationAllowed: Boolean, onRequestNotification: () -> Unit) {
        val evidence = ReliabilityEvidence(observations, manualReviews, localResult)
        Panel("后台可靠性 · 按证据区分") {
            Text("历史${if (evidence.backgroundReadiness == BackgroundReadiness.KNOWN_BLOCKED) "默认策略阻断" else "待核对"}：vivo / Android 16 智能后台耗电曾导致冻结；仅本测试包改为允许后台耗电后，120 秒 Home 空闲读取通过。短窗口不是长期熄屏或 72 小时证明。",
                color = MaterialTheme.colorScheme.error)
            Text("系统观察、人工核对、本机前台读取、远端请求和长期运行分别记录；不会因点击设置或一次成功而自动宣告可靠。")
        }
        Panel("1 / 4 · 通知的三层观测") {
            Observation("运行时通知许可", observations.notificationRuntime, "已允许", "未允许")
            Observation("应用通知总开关", observations.notificationApp, "开启", "关闭")
            Observation("文件服务通知通道", observations.notificationChannel, "未被禁用", "已禁用")
            Text("三项互不替代；服务通道尚未创建时为未知。通知可提供停止入口，但不保证系统或厂商允许后台运行。")
            if (Build.VERSION.SDK_INT >= 33) OutlinedButton(onClick = onRequestNotification) {
                Text("明确请求通知许可")
            }
            TextButton(onClick = { settings(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, packageName)) }) { Text("打开应用通知设置") }
            TextButton(onClick = { settings(Intent(Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
                .putExtra(Settings.EXTRA_CHANNEL_ID, "nas_file_host")) }) { Text("打开服务通道设置") }
        }
        Panel("2 / 4 · 电池与后台限制（只读）") {
            Observation("系统电池优化豁免", observations.batteryExempt, "是", "否")
            Observation("系统报告后台受限", observations.backgroundRestricted, "是", "否")
            Text("这是公开系统 API 的当时观测，不包含所有厂商冻结策略；“否 / 已豁免”也不证明后台可用。不会代你修改全局省电设置或申请直接豁免。")
            TextButton(onClick = { settings(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }) {
                Text("打开电池优化列表")
            }
            TextButton(onClick = { settings(appDetails()) }) { Text("打开本应用电量 / 后台设置") }
        }
        Panel("3 / 4 · 厂商手动核对") {
            Text(vendorSettingsHint(Build.MANUFACTURER, Build.BRAND))
            Text("本 vivo 已实测：应用详情 → 电量 → 后台耗电管理 → 允许后台耗电；应用详情 → 查看所有权限 → 自启动、关联启动。仅允许后台耗电能解决短时冻结，但系统进程重建还曾被关联启动阻断。其他系统名称可能不同；仅调整本应用，自行权衡耗电，勿关闭全局安全或冻结策略。")
            Text("以下仅保存你的自述，不是系统检测。打开设置不会勾选、启动服务或更改运行意愿；可随时重置。")
            ManualSetting.entries.forEach { setting ->
                val review = manualReviews[setting] ?: ManualReview()
                val label = when (review.state) {
                    ManualReviewState.UNREVIEWED -> "未核对"
                    ManualReviewState.USER_REPORTED -> "用户自述已核对（非系统验证）"
                    ManualReviewState.UNAVAILABLE -> "用户报告无此选项 / 不可用"
                }
                Text("${setting.title} · $label\n记录：${timestamp(review.recordedAt)}")
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    TextButton(onClick = { saveReview(setting, ManualReviewState.USER_REPORTED) }) { Text("我已核对") }
                    TextButton(onClick = { saveReview(setting, ManualReviewState.UNAVAILABLE) }) { Text("无此选项") }
                    TextButton(onClick = { saveReview(setting, ManualReviewState.UNREVIEWED) }) { Text("重置") }
                }
            }
            TextButton(onClick = { settings(appDetails()) }) { Text("打开应用详情后手动查找") }
        }
        Panel("4 / 4 · 一次性前台本机只读检查") {
            Text("仅演示模式可用：发送一次认证 GET http://127.0.0.1:18888/readonly/readme.txt，核对固定内容；不显示密码、不写文件、不跟随重定向、不启动 / 重启服务。自定义共享不自动生成或读取用户文件作自检。")
            if (!verificationAllowed) Text("服务未确认运行（已停止、停止中或有错误）；检查不可用，不会自动启动。")
            OutlinedButton(enabled = verificationAllowed && !localBusy, onClick = { verifyLocalRead() }) {
                Text(if (localBusy) "正在检查…" else "明确检查一次本机读取")
            }
            Text(localStatus)
            localResult?.let { result ->
                val detail = when (result.outcome) {
                    LoopbackOutcome.VERIFIED -> "本次前台本机读取成功：认证通过，固定只读演示内容精确匹配。"
                    LoopbackOutcome.UNAUTHORIZED -> "失败：HTTP 401，现有凭据未获服务接受。"
                    LoopbackOutcome.UNEXPECTED_BODY -> "失败：响应不匹配固定只读演示内容（或超出边界）。"
                    LoopbackOutcome.REDIRECT_REJECTED -> "失败：服务返回重定向；已拒绝跟随，没有向其他地址发送凭据。"
                    LoopbackOutcome.HTTP_ERROR -> "失败：HTTP ${result.httpStatus}，未取得预期文件。"
                    LoopbackOutcome.TIMEOUT -> "失败：本机请求超时；不推断厂商原因。"
                    LoopbackOutcome.CONNECTION_REFUSED -> "失败：本机连接被拒绝；不会启动或恢复服务。"
                    LoopbackOutcome.NETWORK_ERROR -> "失败：本机连接未完成；不代表远端状态。"
                    LoopbackOutcome.CANCELLED -> "已取消；不构成一次通过记录。"
                }
                Text("${timestamp(result.checkedAt)}\n$detail")
                Text("仅为该时刻前台回环证据，不证明后台、熄屏、USB、LAN、Tailscale 或全天稳定性。后台仍未通过。")
            }
            TextButton(onClick = { refreshObservations(); revision++ }) { Text("刷新只读系统观测（不发网络请求）") }
        }
    }

    @Composable private fun Observation(title: String, observed: ObservedValue, yes: String, no: String) {
        val value = when (observed.value) {
            EvidenceValue.YES -> yes
            EvidenceValue.NO -> no
            EvidenceValue.UNKNOWN -> "未知"
        }
        Text("$title：$value\n观测：${timestamp(observed.observedAt)}${if (observed.note.isBlank()) "" else " · ${observed.note}"}")
    }
    private fun timestamp(time: Long?): String = if (time == null) "尚无记录"
        else DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.MEDIUM).format(Date(time))
}

@Composable private fun Panel(title: String, content: @Composable ColumnScope.() -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            content()
        }
    }
}
