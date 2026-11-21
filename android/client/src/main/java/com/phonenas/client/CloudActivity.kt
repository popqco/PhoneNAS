package com.phonenas.client

import android.content.pm.ActivityInfo
import android.net.Uri
import android.os.Bundle
import android.os.SystemClock
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ViewModel
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.concurrent.Executors

internal enum class CloudWork(val label: String, val foregroundOnly: Boolean = false) {
    LOAD("读取本机连接"), IMPORT("导入公开证书", true), VERIFY_IDENTITY("验证公开证书", true), CONNECT("正在连接云盘", true),
    LIST("正在刷新文件", true), CHECK_EXCHANGE("检查互传位置", true),
    DOWNLOAD("正在下载文件", true), SAVE_FILE("正在保存文件", true),
    UPLOAD("检查上传目标", true), SAVE_PROFILE("保存本机连接"), FORGET("移除本机连接"),
}

internal class PendingCloudSave(val file: StagedDownload)
internal class CloudUploadTarget(val profile: CloudProfile, val directory: DavPath)
internal class CloudUploadPrompt(val target: CloudUploadTarget, val files: List<Uri>)
internal class CloudUploadReport(val directory: DavPath, val files: List<UploadResult>, val refreshed: CloudDirectory?)

private val cloudWorker = Executors.newSingleThreadExecutor()

// A picker may outlive Activity rotation. Its destination and temporary URI grants stay in memory only.
// Never serialize a stage path or credentials, or automatically resume a transfer after process death.
internal class CloudPickerState : ViewModel() {
    var session by mutableStateOf<CloudProfile?>(null)
    var directory by mutableStateOf<CloudDirectory?>(null)
    var rememberCurrent by mutableStateOf(false)
    var uploadPrompt by mutableStateOf<CloudUploadPrompt?>(null)
    var uploadReport by mutableStateOf<CloudUploadReport?>(null)
    var feedback by mutableStateOf<CloudFeedback?>(null)
    var uploadTarget: CloudUploadTarget? = null
    var pending: PendingCloudSave? = null
    var saveLocation: Uri? = null
    var importing = false
    var importedLocation: Uri? = null
    var repairExchange: ExchangeShare? = null
    var repairEndpoint: String? = null
    var tab by mutableStateOf(CloudTab.FILES)
    var setupStep by mutableStateOf<CloudSetupStep?>(null)
    var address by mutableStateOf("")
    var fingerprint by mutableStateOf("")
    var username by mutableStateOf("phonenas")
    var certificate by mutableStateOf<ByteArray?>(null)
    var verifiedIdentity: CloudIdentity? = null

    override fun onCleared() {
        session = null
        uploadPrompt = null
        uploadTarget = null
        pending?.file?.let { file -> cloudWorker.execute { runCatching { file.close() } } }
        pending = null
    }
}

class CloudActivity : ComponentActivity() {
    private lateinit var profiles: ClientProfiles
    private lateinit var staging: DownloadStaging
    private lateinit var routes: CloudRoutes
    private lateinit var uploadSource: CloudUploadSource
    private val pickers by viewModels<CloudPickerState>()
    private class Task(val kind: CloudWork, val operation: CloudOperation = CloudOperation())

    private var active: Task? = null
    private var session: CloudProfile?
        get() = pickers.session
        set(value) { pickers.session = value }
    private var directory: CloudDirectory?
        get() = pickers.directory
        set(value) { pickers.directory = value }
    private var rememberCurrent: Boolean
        get() = pickers.rememberCurrent
        set(value) { pickers.rememberCurrent = value }
    private var remembered: CloudProfile? = null
    private var work by mutableStateOf<CloudWork?>(null)
    private var feedback: CloudFeedback?
        get() = pickers.feedback
        set(value) { pickers.feedback = value }
    private var progress by mutableStateOf<Pair<Long, Long>?>(null)
    private var uploadProgress by mutableStateOf<UploadProgress?>(null)
    private var savedHost by mutableStateOf<String?>(null)
    private var profileUnreadable by mutableStateOf(false)
    private var choosingFile by mutableStateOf(false)
    private var choosingExchange by mutableStateOf(false)
    private var askingForget by mutableStateOf(false)
    private var password by mutableStateOf("")

    private val openCertificate = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        pickers.importing = false
        choosingFile = false
        pickers.importedLocation = uri?.takeIf { pickers.setupStep == CloudSetupStep.IDENTITY }
        dispatchPickerResult()
    }

    private val createFile = registerForActivityResult(ActivityResultContracts.CreateDocument("application/octet-stream")) { uri ->
        // Until saveFile takes the stage, the ViewModel owns it even across another rotation.
        if (uri != null) {
            pickers.saveLocation = uri
            dispatchPickerResult()
        } else {
            pickers.saveLocation = null
            val value = pickers.pending
            pickers.pending = null
            choosingFile = false
            value?.let {
                dispose(it.file)
                feedback = CloudFeedback("已取消另存，未写入本地文件。")
            }
        }
    }

    private val openUploadFiles = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { files ->
        val target = pickers.uploadTarget
        pickers.uploadTarget = null
        choosingFile = false
        when {
            files.isEmpty() -> feedback = CloudFeedback("已取消选择，没有上传文件。")
            target == null || !isCurrent(target) ->
                feedback = CloudFeedback("页面已重建，未上传文件。请重新进入互传目录选择。", true)
            files.size > MAX_BATCH_FILES ->
                feedback = CloudFeedback("一次最多选择 $MAX_BATCH_FILES 个文件，没有上传。", true)
            else -> pickers.uploadPrompt = CloudUploadPrompt(target, files.distinct())
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        enableEdgeToEdge()
        profiles = ClientProfiles(this)
        staging = DownloadStaging(File(cacheDir, "cloud-downloads-v1"))
        routes = AndroidCloudRoutes(this)
        uploadSource = CloudUploadSource(applicationContext)
        choosingFile = pickers.pending != null || pickers.saveLocation != null || pickers.importing ||
            pickers.importedLocation != null || pickers.uploadTarget != null
        setContent {
            CloudWorkbench(
                state = CloudUiState(pickers.tab, pickers.setupStep, directory, session?.identity?.endpoint?.url,
                    savedHost, rememberCurrent, profileUnreadable, session?.exchange,
                    work, choosingFile, choosingExchange, askingForget, feedback, progress, uploadProgress,
                    pickers.uploadPrompt, pickers.uploadReport),
                credentials = CloudSetupFields(pickers.address, pickers.fingerprint, pickers.username, password,
                    pickers.certificate != null),
                actions = CloudUiActions(
                    onTab = ::selectTab, onConnect = ::openCloud, onEdit = ::startEditing, onBack = ::goBack,
                    onList = ::openDirectory, onDownload = { download(it, false) }, onSaveAs = { download(it, true) },
                    onChooseExchange = ::chooseExchange, onDismissExchange = ::dismissExchange,
                    onExchange = ::selectExchange, onRemoveExchange = ::removeExchange,
                    onChooseUploads = ::chooseUploads, onUpload = ::upload,
                    onDismissUpload = { pickers.uploadPrompt = null },
                    onDismissReport = ::dismissUploadReport,
                    onRemember = ::remember, onAskForget = { askingForget = true }, onForget = ::forget,
                    onDismissForget = { askingForget = false }, onCancel = { active?.operation?.cancel() },
                    onDismissFeedback = { feedback = null },
                ),
                setup = CloudSetupActions(
                    onAddress = { pickers.address = it.take(256); invalidateIdentity() },
                    onFingerprint = { pickers.fingerprint = it.take(128); invalidateIdentity() },
                    onUsername = { pickers.username = it.take(128); feedback = null },
                    onPassword = { password = it.take(1024); feedback = null },
                    onImport = ::chooseCertificate, onContinue = ::continueSetup,
                ),
            )
        }
        launch(CloudWork.LOAD, CloudIssue.PROFILE, {
            runCatching { staging.cleanAbandoned() }
            runCatching { uploadSource.cleanAbandoned() }
            runCatching { profiles.load() }
        }) { loaded ->
            loaded.onSuccess {
                remembered = it
                savedHost = it?.identity?.endpoint?.host
            }.onFailure {
                profileUnreadable = true
                feedback = CloudFeedback(CloudIssue.PROFILE)
            }
        }
    }

    override fun onResume() {
        super.onResume()
        dispatchPickerResult()
    }

    override fun onStop() {
        active?.takeIf { it.kind.foregroundOnly }?.operation?.cancel()
        // If the password field had focus, do not let the IME keep an editable copy on resume.
        password = ""
        super.onStop()
    }

    override fun onDestroy() {
        active?.operation?.cancel()
        password = ""
        super.onDestroy()
    }

    private fun dispatchPickerResult() {
        if (active != null || !lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) return
        pickers.saveLocation?.let { uri ->
            pickers.saveLocation = null
            saveFile(uri)
            return
        }
        val uri = pickers.importedLocation ?: return
        pickers.importedLocation = null
        choosingFile = false
        if (pickers.setupStep != CloudSetupStep.IDENTITY) return
        pickers.certificate = null
        invalidateIdentity()
        launch(CloudWork.IMPORT, CloudIssue.IMPORT, { readCertificate(uri) }) { bytes ->
            pickers.certificate = bytes
        }
    }

    private fun chooseCertificate() {
        if (work != null || choosingFile || pickers.setupStep != CloudSetupStep.IDENTITY) return
        pickers.importing = true
        choosingFile = true
        runCatching { openCertificate.launch(arrayOf("*/*")) }.onFailure {
            pickers.importing = false
            choosingFile = false
            feedback = CloudFeedback(CloudIssue.IMPORT)
        }
    }

    private fun readCertificate(uri: Uri): ByteArray {
        if (uri.scheme != "content") throw CloudFailure(CloudIssue.IMPORT)
        val limit = CloudIdentity.MAX_PEM_BYTES
        return contentResolver.openInputStream(uri)?.use { source ->
            val buffer = ByteArray(4096)
            val result = ByteArrayOutputStream()
            while (true) {
                val n = source.read(buffer)
                if (n == -1) break
                if (n <= 0 || n > limit - result.size()) throw CloudFailure(CloudIssue.IMPORT)
                result.write(buffer, 0, n)
            }
            if (result.size() == 0) throw CloudFailure(CloudIssue.IMPORT)
            result.toByteArray()
        } ?: throw CloudFailure(CloudIssue.IMPORT)
    }

    private fun invalidateIdentity() {
        pickers.verifiedIdentity = null
        feedback = null
    }

    private fun continueSetup() {
        if (work != null || choosingFile) return
        when (pickers.setupStep) {
            CloudSetupStep.ADDRESS -> {
                try {
                    val endpoint = CloudEndpoint.parse(pickers.address)
                    pickers.address = endpoint.url
                    invalidateIdentity()
                    pickers.setupStep = CloudSetupStep.IDENTITY
                } catch (failure: CloudFailure) {
                    feedback = CloudFeedback(failure.issue)
                }
            }
            CloudSetupStep.IDENTITY -> {
                val pem = pickers.certificate ?: return
                val inputAddress = pickers.address
                val fingerprint = pickers.fingerprint
                launch(CloudWork.VERIFY_IDENTITY, CloudIssue.CERTIFICATE, {
                    CloudIdentity.verify(CloudEndpoint.parse(inputAddress), pem, fingerprint)
                }) { verified ->
                    pickers.verifiedIdentity = verified
                    pickers.setupStep = CloudSetupStep.SIGN_IN
                }
            }
            CloudSetupStep.SIGN_IN -> {
                val identity = pickers.verifiedIdentity ?: return
                val inputUsername = pickers.username
                val inputPassword = password
                // Re-pairing the same endpoint keeps the chosen exchange; changing the
                // address does not carry a share name over to a different server.
                val exchange = pickers.repairExchange?.takeIf {
                    identity.endpoint.url == pickers.repairEndpoint
                }
                password = ""
                connect {
                    CloudProfile(identity, CloudCredentials.create(inputUsername, inputPassword), exchange)
                }
            }
            null -> Unit
        }
    }

    private fun openCloud() {
        if (work != null || choosingFile) return
        pickers.tab = CloudTab.FILES
        if (session != null) return
        val saved = remembered
        if (saved != null) connect { saved } else if (!profileUnreadable) startEditing()
    }

    private fun connect(profile: () -> CloudProfile) {
        launch(CloudWork.CONNECT, CloudIssue.UNREACHABLE, { operation ->
            val candidate = profile()
            val entries = CloudDav(candidate, routes).list(DavPath.Root, operation)
            candidate to CloudDirectory(DavPath.Root, entries)
        }) { (profile, listing) ->
            password = ""
            session = profile
            rememberCurrent = profile === remembered
            directory = listing
            clearSetup()
            pickers.tab = CloudTab.FILES
        }
    }

    private fun startEditing() {
        if (work != null || choosingFile || profileUnreadable) return
        // Re-pairing usually changes only the password. Carry the public identity and
        // username forward so the user does not have to re-import the certificate or
        // retype its fingerprint; nothing secret is prefilled.
        val current = session
        val identity = current?.identity ?: remembered?.identity
        val username = current?.credentials?.username ?: remembered?.credentials?.username
        val exchange = current?.exchange ?: if (current == null) remembered?.exchange else null
        clearSetup()
        pickers.address = identity?.endpoint?.url.orEmpty()
        pickers.certificate = identity?.pem
        pickers.fingerprint = identity?.fingerprint.orEmpty()
        pickers.username = username ?: "phonenas"
        pickers.repairExchange = exchange
        pickers.repairEndpoint = identity?.endpoint?.url
        pickers.setupStep = CloudSetupStep.ADDRESS
        feedback = null
    }

    private fun clearSetup() {
        pickers.setupStep = null
        pickers.address = ""
        pickers.fingerprint = ""
        pickers.username = "phonenas"
        pickers.certificate = null
        pickers.verifiedIdentity = null
        pickers.importedLocation = null
        pickers.repairExchange = null
        pickers.repairEndpoint = null
        password = ""
    }

    private fun selectTab(tab: CloudTab) {
        if (work != null || choosingFile || pickers.uploadPrompt != null || pickers.uploadReport != null) return
        if (pickers.tab != tab) {
            feedback = null
            pickers.tab = tab
        }
    }

    private fun goBack() {
        if (work?.foregroundOnly == true) {
            active?.operation?.cancel()
            return
        }
        if (work != null || choosingFile) return
        when (pickers.setupStep) {
            CloudSetupStep.SIGN_IN -> {
                password = ""
                invalidateIdentity()
                pickers.setupStep = CloudSetupStep.IDENTITY
            }
            CloudSetupStep.IDENTITY -> {
                invalidateIdentity()
                pickers.setupStep = CloudSetupStep.ADDRESS
            }
            CloudSetupStep.ADDRESS -> {
                clearSetup()
                feedback = null
            }
            null -> if (pickers.tab == CloudTab.CONNECTION) selectTab(CloudTab.FILES)
                else directory?.path?.parent?.let(::openDirectory)
        }
    }

    private fun openDirectory(path: DavPath) {
        val profile = session ?: return
        pickers.tab = CloudTab.FILES
        launch(CloudWork.LIST, CloudIssue.UNREACHABLE, { operation ->
            CloudDirectory(path, CloudDav(profile, routes).list(path, operation))
        }, failed = { issue ->
            if (issue != CloudIssue.CANCELLED && session === profile) directory = directory?.refreshFailed(path)
        }) { directory = it }
    }

    private fun chooseExchange() {
        val profile = session ?: return
        if (work != null || choosingFile) return
        choosingExchange = false
        launch(CloudWork.LIST, CloudIssue.UNREACHABLE, { operation ->
            CloudDirectory(DavPath.Root, CloudDav(profile, routes).list(DavPath.Root, operation))
        }, failed = { issue ->
            if (issue != CloudIssue.CANCELLED && session === profile) directory = directory?.refreshFailed(DavPath.Root)
        }) { root ->
            directory = root
            choosingExchange = true
        }
    }

    private fun selectExchange(path: DavPath) {
        val profile = session ?: return
        if (work != null || choosingFile) return
        if (directory?.path != DavPath.Root || directory?.entries?.none { it.path == path } != false) return
        // Keep the dialog open while checking. A read-only or unreachable share then
        // reports the reason in place instead of closing and forcing the user to reopen it.
        launch(CloudWork.CHECK_EXCHANGE, CloudIssue.UPLOAD_TARGET, { operation ->
            val exchange = ExchangeShare.of(path)
            val dav = CloudDav(profile, routes)
            dav.checkWritable(path, operation)
            val listing = CloudDirectory(path, dav.list(path, operation))
            CloudProfile(profile.identity, profile.credentials, exchange) to listing
        }) { (selected, listing) ->
            choosingExchange = false
            session = selected
            directory = listing
            pickers.tab = CloudTab.FILES
            rememberCurrent = selected.exchange == remembered?.exchange && profile.identity === remembered?.identity
            feedback = CloudFeedback("已设为互传位置。需要下次自动保留时，可在「连接」里保存。")
        }
    }

    private fun dismissExchange() {
        if (work != null || choosingFile) return
        choosingExchange = false
        feedback = null
    }

    private fun removeExchange() {
        val profile = session ?: return
        if (work != null || choosingFile) return
        choosingExchange = false
        session = CloudProfile(profile.identity, profile.credentials)
        rememberCurrent = profile.identity === remembered?.identity && remembered?.exchange == null
        feedback = CloudFeedback("已取消本次连接的互传位置。保存连接后，下次也不再使用原位置。")
    }

    private fun chooseUploads() {
        if (work != null || choosingFile || pickers.uploadPrompt != null) return
        val profile = session ?: return
        val listing = directory ?: return
        if (!listing.canUpload(profile.exchange)) return
        pickers.uploadTarget = CloudUploadTarget(profile, listing.path)
        choosingFile = true
        feedback = null
        runCatching { openUploadFiles.launch(arrayOf("*/*")) }.onFailure {
            pickers.uploadTarget = null
            choosingFile = false
            feedback = CloudFeedback(CloudIssue.UPLOAD_SOURCE)
        }
    }

    private fun isCurrent(target: CloudUploadTarget): Boolean = session === target.profile &&
        directory?.let { it.path == target.directory && it.canUpload(target.profile.exchange) } == true

    private fun dismissUploadReport(verify: Boolean) {
        val report = pickers.uploadReport ?: return
        if (!verify) {
            pickers.uploadReport = null
            return
        }
        val profile = session ?: return
        if (directory?.path != report.directory) return
        launch(CloudWork.LIST, CloudIssue.UNREACHABLE, { operation ->
            CloudDirectory(report.directory, CloudDav(profile, routes).list(report.directory, operation))
        }, failed = {
            // Keep the exact attempted names available when the refresh could not finish.
            directory = directory?.refreshFailed(report.directory)
        }) { refreshed ->
            directory = refreshed
            pickers.uploadReport = CloudUploadReport(report.directory, report.files, refreshed)
        }
    }

    private fun upload(keepBoth: Boolean) {
        val prompt = pickers.uploadPrompt ?: return
        if (!isCurrent(prompt.target) || !lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) return
        val (profile, path) = prompt.target.let { it.profile to it.directory }
        val exchange = checkNotNull(profile.exchange)
        val started = launch(CloudWork.UPLOAD, CloudIssue.UPLOAD_UNCONFIRMED, { operation ->
            val dav = CloudDav(profile, routes)
            var last: UploadProgress? = null
            var lastUpdate = 0L
            val files = CloudUploadBatch(dav).send(exchange, path, prompt.files, keepBoth, operation, { uri, active ->
                val document = uploadSource.inspect(uri, active)
                UploadItem(document.name) { op, progress -> uploadSource.prepare(document, op, progress) }
            }) { update ->
                val now = SystemClock.elapsedRealtime()
                if (last?.phase != update.phase || last?.index != update.index || now - lastUpdate >= 160L) {
                    last = update
                    lastUpdate = now
                    runOnUiThread {
                        if (active?.operation === operation) uploadProgress = update
                    }
                }
            }
            val refreshed = if (files.any { it.state == UploadState.CREATED } &&
                files.none { it.state == UploadState.UNKNOWN }) {
                runCatching { CloudDirectory(path, dav.list(path, operation)) }.getOrNull()
            } else null
            CloudUploadReport(path, files, refreshed)
        }, terminalResult = true, onRecreated = { showUploadReport(prompt.target, it) }) { report ->
            showUploadReport(prompt.target, report)
        }
        if (started) pickers.uploadPrompt = null
    }

    private fun showUploadReport(target: CloudUploadTarget, report: CloudUploadReport) {
        if (session !== target.profile || directory?.path != target.directory) return
        directory = directory?.afterUpload(report)
        pickers.uploadReport = report
    }

    private fun download(entry: DavEntry, saveAs: Boolean) {
        val profile = session ?: return
        if (!saveAs) {
            launch(CloudWork.DOWNLOAD, CloudIssue.UNREACHABLE, { operation ->
                CloudDav(profile, routes).downloadTo(entry, operation, downloadProgress(operation)) { length, verify, copy ->
                    CloudDownloads(applicationContext).save(entry.path.name, length, operation, verify, copy)
                }
            }, terminalResult = true, onRecreated = ::showDownload, onRecreatedError = { error ->
                (error as? CloudFailure)?.issue?.let { feedback = CloudFeedback(it) }
            }, done = ::showDownload)
            return
        }
        launch(CloudWork.DOWNLOAD, CloudIssue.UNREACHABLE, { operation ->
            CloudDav(profile, routes).download(entry, staging, operation, downloadProgress(operation))
        }, discard = ::dispose) { staged ->
            pickers.pending = PendingCloudSave(staged)
            choosingFile = true
            runCatching { createFile.launch(entry.path.name) }.onFailure {
                choosingFile = false
                pickers.pending = null
                dispose(staged)
                feedback = CloudFeedback("无法打开系统保存窗口，未写入本地文件。", true)
            }
        }
    }

    private fun showDownload(saved: PublishedDownload) {
        feedback = CloudFeedback("已下载到 下载/PhoneNAS：${saved.name}")
    }

    private fun downloadProgress(operation: CloudOperation): (Long, Long) -> Unit {
        var lastUpdate = 0L
        return { received, total ->
            val now = SystemClock.elapsedRealtime()
            if (now - lastUpdate >= 160L || received == total) {
                lastUpdate = now
                runOnUiThread {
                    if (active?.operation === operation) progress = received to total
                }
            }
        }
    }

    private fun saveFile(uri: Uri) {
        val value = pickers.pending
        pickers.pending = null
        choosingFile = false
        if (value == null) {
            feedback = CloudFeedback("下载已取消或应用已重建。未写入所选文件，请检查是否留下空文件。", true)
            return
        }
        val started = launch(CloudWork.SAVE_FILE, CloudIssue.SAVE, { operation ->
            CloudDocument(this).save(uri, value.file, operation)
        }, release = { value.file.close() }, terminalResult = true,
            onRecreated = { feedback = CloudFeedback("已保存到你选择的位置。") },
            onRecreatedError = { feedback = CloudFeedback(CloudIssue.SAVE) }) {
            feedback = CloudFeedback("已保存到你选择的位置。")
        }
        if (!started) dispose(value.file)
    }

    private fun remember() {
        val profile = session ?: return
        launch(CloudWork.SAVE_PROFILE, CloudIssue.PROFILE, { profiles.save(profile) }) {
            remembered = profile
            savedHost = profile.identity.endpoint.host
            rememberCurrent = true
            feedback = CloudFeedback("连接已在此手机加密保存。")
        }
    }

    private fun forget() {
        askingForget = false
        val started = launch(CloudWork.FORGET, CloudIssue.PROFILE, { profiles.forget() }) {
            profileUnreadable = false
            clearSetup()
            pickers.tab = CloudTab.FILES
            feedback = CloudFeedback("已移除本机连接，云盘不受影响。")
        }
        if (started) {
            // Never keep a usable cached connection if deletion or its final durability check fails.
            remembered = null
            savedHost = null
            session = null
            directory = null
            rememberCurrent = false
            profileUnreadable = true
            clearSetup()
        }
    }

    private fun dispose(staged: StagedDownload) {
        cloudWorker.execute { runCatching { staged.close() } }
    }

    private fun <T> launch(kind: CloudWork, fallback: CloudIssue, run: (CloudOperation) -> T,
                           discard: (T) -> Unit = {}, release: (() -> Unit)? = null,
                           terminalResult: Boolean = false, onRecreated: ((T) -> Unit)? = null,
                           onRecreatedError: ((Exception) -> Unit)? = null,
                           failed: (CloudIssue) -> Unit = {}, done: (T) -> Unit): Boolean {
        if (active != null || isDestroyed) return false
        // A rotation otherwise reaches onStop and cancels a foreground-only transfer.
        // Lock the current orientation for the duration instead of silently losing
        // a long upload or download; the pickers remain separate activities.
        val locksOrientation = kind == CloudWork.UPLOAD || kind == CloudWork.DOWNLOAD ||
            kind == CloudWork.SAVE_FILE
        if (locksOrientation) requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LOCKED
        val task = Task(kind)
        active = task
        work = kind
        if (kind != CloudWork.LOAD) feedback = null
        progress = null
        uploadProgress = null
        cloudWorker.execute {
            val result = try {
                // The batch itself accounts for all selections if it was cancelled in the queue.
                if (kind != CloudWork.UPLOAD) task.operation.check()
                Result.success(run(task.operation))
            } catch (error: Exception) {
                Result.failure(error)
            } finally {
                release?.let { runCatching(it) }
            }
            runOnUiThread {
                if (active !== task || isDestroyed) {
                    if (locksOrientation) requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
                    result.onSuccess(if (isChangingConfigurations && onRecreated != null) onRecreated else discard)
                    if (isChangingConfigurations && onRecreatedError != null) {
                        (result.exceptionOrNull() as? Exception)?.let(onRecreatedError)
                    }
                    return@runOnUiThread
                }
                active = null
                work = null
                if (locksOrientation) requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
                progress = null
                uploadProgress = null
                val cancelled = if (kind.foregroundOnly && !terminalResult)
                    runCatching { task.operation.check() }.exceptionOrNull() else null
                val error = cancelled ?: result.exceptionOrNull()
                if (error != null) {
                    result.onSuccess(discard)
                    val issue = if (kind == CloudWork.SAVE_FILE) CloudIssue.SAVE else (error as? CloudFailure)?.issue ?: fallback
                    feedback = CloudFeedback(issue)
                    failed(issue)
                } else {
                    done(result.getOrThrow())
                }
                dispatchPickerResult()
            }
        }
        return true
    }
}
