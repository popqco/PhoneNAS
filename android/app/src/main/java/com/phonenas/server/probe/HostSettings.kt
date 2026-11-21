package com.phonenas.server.probe

import android.content.Context
import android.net.Uri
import android.os.Environment
import android.os.UserManager
import android.provider.DocumentsContract
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.util.Locale

/** DEMO is a deliberate mode, never the fallback for a failed CUSTOM configuration. */
enum class HostMode { DEMO, CUSTOM }

data class HostConfiguration(
    val mode: HostMode = HostMode.DEMO,
    val shares: List<ShareConfiguration> = emptyList(),
    /** Null disables remote access. A non-null value is an explicit, literal VPN IP pin. */
    val remoteIp: String? = null,
)

/** CE-only settings. Neither passwords nor service/recovery intent belong in this store. */
class HostSettings(private val preferences: Preferences, private val platform: Platform) {
    /** System boundaries; production and tests use the same validation/build implementation. */
    interface Preferences {
        fun read(): Map<String, *>
        /** Durable, separate evidence that a configuration has previously been initialized. */
        fun hasInitializationMarker(): Boolean = false
        /** Must make the initialization marker durable BEFORE replacing the configuration. */
        fun commit(values: Map<String, Any>): Boolean
    }

    interface Platform {
        val primaryRoot: File
        val privateRoots: List<File>
        val controlRoot: File
        val tlsDirectory: File
        fun hasManageExternalStorage(): Boolean
        fun tailnetState(): TailnetState.Snapshot
    }

    constructor(context: Context) : this(androidPreferences(context), AndroidPlatform(context))

    private var persistenceFailed = false
    private var hasStoredConfiguration = false

    @Synchronized fun read(): HostConfiguration {
        check(!persistenceFailed) { "配置提交失败；本进程内禁止继续使用未确认持久化的配置。" }
        val values = preferences.read()
        if (values.isEmpty()) {
            check(!hasStoredConfiguration && !preferences.hasInitializationMarker()) {
                "已保存的共享配置丢失或损坏；不会回退演示模式。"
            }
            return HostConfiguration()
        }
        hasStoredConfiguration = true
        check(values["schema_version"] == 1 && values["schema_version"] is Int) { "共享配置版本损坏或不支持。" }
        val mode = values["mode"] as? String ?: error("共享模式损坏。")
        val count = values["share_count"] as? Int ?: error("共享数量损坏。")
        check(count in 0..MAX_SHARES) { "共享数量损坏。" }
        val remote = values["remote_ip"] as? String ?: error("远端配置损坏。")
        val keys = mutableSetOf("schema_version", "mode", "share_count", "remote_ip")
        val shares = (0 until count).map { index ->
            fun key(field: String) = "share_${index}_$field".also { keys.add(it) }
            ShareConfiguration(
                values[key("alias")] as? String ?: error("共享别名损坏。"),
                values[key("root")] as? String ?: error("共享路径损坏。"),
                values[key("read_only")] as? Boolean ?: error("共享只读配置损坏。"),
                values[key("enabled")] as? Boolean ?: error("共享启用配置损坏。"),
            )
        }
        check(values.keys == keys) { "共享配置字段不完整或含未知字段。" }
        val configuration = HostConfiguration(
            HostMode.entries.singleOrNull { it.name == mode } ?: error("共享模式不支持。"),
            shares, remote.takeIf { it.isNotEmpty() })
        validateStructure(configuration)
        return configuration
    }

    /**
     * The caller must supply a real stopped-and-stop-confirmed gate, not merely desired=false.
     * Disabling/removing a share changes only this map; no shared file is ever removed.
     */
    @Synchronized fun save(configuration: HostConfiguration, stoppedConfirmed: Boolean) {
        check(stoppedConfirmed) { "服务运行中或停止尚未确认；不能修改共享配置。" }
        val previous = read() // Corrupt state is never silently replaced with DEMO or an unrelated map.
        val snapshot = configuration.copy(shares = configuration.shares.toList())
        validateStructure(snapshot)
        if (snapshot.mode == HostMode.CUSTOM && snapshot.shares.any { it.enabled }) {
            ShareRules.validateShares(snapshot.shares.filter { it.enabled }, platform.primaryRoot,
                platform.privateRoots, platform.controlRoot)
        }
        if (snapshot.remoteIp != null && snapshot.remoteIp != previous.remoteIp) verifyRemote(snapshot)
        val values = linkedMapOf<String, Any>("schema_version" to 1, "mode" to snapshot.mode.name,
            "share_count" to snapshot.shares.size, "remote_ip" to (snapshot.remoteIp ?: ""))
        snapshot.shares.forEachIndexed { index, share ->
            values["share_${index}_alias"] = share.alias
            values["share_${index}_root"] = share.root
            values["share_${index}_read_only"] = share.readOnly
            values["share_${index}_enabled"] = share.enabled
        }
        val committed = try { preferences.commit(values) } catch (e: Exception) {
            persistenceFailed = true
            throw IllegalStateException("无法持久化共享配置；没有报告保存成功。", e)
        }
        if (!committed) persistenceFailed = true
        check(committed) { "无法持久化共享配置；没有报告保存成功。" }
        hasStoredConfiguration = true
    }

    /** Read-only launch/inspection gate: no mkdir, fixture, password or credential initialization. */
    @Synchronized fun preflight() {
        verifyReady(read(), includeRemote = true)
    }

    /** Storage readiness is independent of VPN startup order; the persisted remote pin is untouched. */
    @Synchronized fun preflightStorage() {
        verifyReady(read(), includeRemote = false)
    }

    /** Only primary external-storage document trees are resolved; no SAF provider bridge is used. */
    fun resolveTree(uri: Uri): File {
        require(uri.scheme == "content" && uri.authority == "com.android.externalstorage.documents" &&
            uri.query == null && uri.fragment == null && DocumentsContract.isTreeUri(uri)) {
            "仅支持系统内置存储的目录选择，不支持其他文件提供者。"
        }
        val root = ShareRules.resolvePrimaryTree(DocumentsContract.getTreeDocumentId(uri), platform.primaryRoot)
        ShareRules.validateShares(listOf(ShareConfiguration("selection", root.path, readOnly = true)),
            platform.primaryRoot, platform.privateRoots, platform.controlRoot)
        return root
    }

    /** Password is injected for this start only and is never persisted here. */
    @Synchronized fun buildConfig(password: String, controlRoot: File, demoRoot: File): String =
        build(password, controlRoot, demoRoot, includeRemote = true)

    /** Real loopback-only service while waiting for VPN; never rewrites/discards the user's pin. */
    @Synchronized fun buildLocalConfig(password: String, controlRoot: File, demoRoot: File): String =
        build(password, controlRoot, demoRoot, includeRemote = false)

    private fun build(password: String, controlRoot: File, demoRoot: File, includeRemote: Boolean): String {
        require(password.toByteArray(Charsets.UTF_8).size >= 20) { "服务密码至少需要 20 字节。" }
        val privateControl = platform.controlRoot.absoluteFile
        require(controlRoot.canonicalFile == privateControl) { "控制目录与应用私有目录不一致。" }
        val configuration = read()
        verifyReady(configuration, includeRemote)
        ensureDemoDirectory(privateControl)
        val shares = if (configuration.mode == HostMode.DEMO) {
            val privateDemo = demoRoot.canonicalFile
            ensureDemoDirectory(privateDemo)
            listOf("test", "readonly").map { name ->
                val root = File(privateDemo, name)
                ensureDemoDirectory(root)
                val fixture = File(root, "readme.txt")
                check(!Files.isSymbolicLink(fixture.toPath())) { "演示文件不能是符号链接。" }
                if (!Files.exists(fixture.toPath(), NOFOLLOW_LINKS)) {
                    Files.newOutputStream(fixture.toPath(), java.nio.file.StandardOpenOption.CREATE_NEW,
                        java.nio.file.StandardOpenOption.WRITE).use { output ->
                        output.write("PhoneNAS isolated $name demo. No personal folders are exported.\n"
                            .toByteArray(Charsets.UTF_8))
                    }
                }
                check(fixture.isFile && fixture.canRead()) { "演示文件不可读。" }
                ShareConfiguration(name, root.absolutePath, readOnly = name == "readonly")
            }
        } else configuration.shares.filter { it.enabled }
        val control = ShareRules.requireExistingDirectory(privateControl)
        check(control.canWrite()) { "控制目录不可写。" }
        val exports = JSONArray()
        shares.forEach { share ->
            val root = ShareRules.requireExistingDirectory(File(share.root))
            check(!pathsOverlap(root, control)) { "共享不能覆盖控制目录。" }
            exports.put(JSONObject().put("name", share.alias).put("root", root.path)
                .put("read_only", share.readOnly))
        }
        val config = JSONObject().put("listen", "127.0.0.1:18888").put("username", "phonenas")
            .put("password", password).put("control_root", control.path).put("shares", exports)
            .put("tls_directory", platform.tlsDirectory.absolutePath)
        if (includeRemote) configuration.remoteIp?.let { config.put("remote_listen", "$it:18889") }
        return config.toString()
    }

    private fun verifyReady(configuration: HostConfiguration, includeRemote: Boolean) {
        if (configuration.mode == HostMode.CUSTOM) {
            check(platform.hasManageExternalStorage()) {
                "请先在系统设置中明确授予所有文件访问权限；目录选择授权不能替代它。"
            }
            check(configuration.shares.any { it.enabled }) { "自定义模式至少需要一个启用的共享；不会回退演示目录。" }
            ShareRules.validateShares(configuration.shares.filter { it.enabled }, platform.primaryRoot,
                platform.privateRoots, platform.controlRoot)
            inspectPrivateDirectory(platform.controlRoot)
        }
        if (includeRemote) verifyRemote(configuration)
    }

    private fun verifyRemote(configuration: HostConfiguration) {
        configuration.remoteIp?.let { pin ->
            check(platform.tailnetState().matchesPinnedIp(pin)) {
                "固定的远端 VPN IPv4 当前不可确认；不会改用其他地址或通配监听。"
            }
            check(platform.tlsDirectory.canonicalFile == platform.tlsDirectory.absoluteFile &&
                !Files.isSymbolicLink(platform.tlsDirectory.toPath())) { "TLS 私有目录无效。" }
        }
    }

    private fun validateStructure(configuration: HostConfiguration) {
        require(configuration.shares.size <= MAX_SHARES) { "共享数量过多。" }
        require(configuration.remoteIp == null || TailnetState.isTailnetIpv4(configuration.remoteIp)) {
            "远端必须是明确固定的 100.64/10 IPv4 字面地址。"
        }
        val aliases = HashSet<String>()
        val roots = ArrayList<File>()
        val primary = platform.primaryRoot.absoluteFile
        val forbidden = listOf(File(primary, "Android")) + platform.privateRoots + platform.controlRoot
        configuration.shares.forEach { share ->
            ShareRules.requireValidAlias(share.alias)
            require(aliases.add(share.alias.lowercase(Locale.ROOT))) { "共享别名不能重复（忽略大小写）。" }
            val root = File(share.root)
            require(root.isAbsolute && root.toPath().normalize() == root.toPath() &&
                share.root.none { it.code < 32 || it.code == 127 } &&
                (File.separatorChar == '\\' || '\\' !in share.root)) { "共享路径不是规范绝对路径。" }
            require(root != primary && root.toPath().startsWith(primary.toPath()) &&
                forbidden.none { pathsOverlap(root, it.absoluteFile) }) { "共享必须是内置存储的公共子目录。" }
            require(roots.none { pathsOverlap(root, it) }) { "共享目录不能相同或互为父子。" }
            roots.add(root)
        }
    }

    private fun inspectPrivateDirectory(directory: File) {
        val absolute = directory.absoluteFile
        check(absolute.canonicalFile == absolute && platform.privateRoots.any {
            absolute != it.absoluteFile && absolute.toPath().startsWith(it.absoluteFile.toPath())
        }) { "只能使用应用自己的隔离演示、控制或 TLS 目录。" }
        var existing: File? = absolute
        while (existing != null && !Files.exists(existing.toPath(), NOFOLLOW_LINKS)) existing = existing.parentFile
        checkNotNull(existing)
        val ancestor = ShareRules.requireExistingDirectory(existing)
        check(ancestor.canWrite()) { "应用私有目录不可写。" }
    }

    private fun ensureDemoDirectory(directory: File) {
        inspectPrivateDirectory(directory)
        check(directory.mkdirs() || directory.isDirectory) { "不能初始化应用私有目录。" }
        ShareRules.requireExistingDirectory(directory)
    }

    private fun pathsOverlap(a: File, b: File) =
        a.toPath().startsWith(b.toPath()) || b.toPath().startsWith(a.toPath())

    private class AndroidPlatform(private val context: Context) : Platform {
        override val primaryRoot: File get() = Environment.getExternalStorageDirectory().canonicalFile
        override val privateRoots: List<File> get() = listOf(context.dataDir.canonicalFile,
            File(primaryRoot, "Android/data/${context.packageName}"))
        override val controlRoot: File get() = File(context.filesDir.canonicalFile, "control")
        override val tlsDirectory: File get() = File(context.filesDir.canonicalFile, "tls")
        override fun hasManageExternalStorage() = Environment.isExternalStorageManager()
        override fun tailnetState() = TailnetState.sample(context)
    }

    companion object {
        private const val MAX_SHARES = 128
        private const val PREFERENCES_NAME = "phonenas_host_configuration_v1"
        @Volatile private var androidCommitFailed = false

        private fun androidPreferences(context: Context): Preferences {
            check(!context.isDeviceProtectedStorage &&
                context.getSystemService(UserManager::class.java)?.isUserUnlocked == true) {
                "共享配置仅能在用户解锁后的凭据保护存储中读取。"
            }
            val prefs = context.getSharedPreferences(PREFERENCES_NAME, Context.MODE_PRIVATE)
            val marker = File(context.filesDir.canonicalFile, "$PREFERENCES_NAME.initialized")
            return object : Preferences {
                override fun hasInitializationMarker(): Boolean =
                    Files.exists(marker.toPath(), NOFOLLOW_LINKS)

                override fun read(): Map<String, *> {
                    check(!androidCommitFailed) { "配置提交失败，须重新打开应用进程后检查。" }
                    return prefs.all.toMap()
                }
                override fun commit(values: Map<String, Any>): Boolean {
                    check(!androidCommitFailed)
                    // A failed first save must never be misread as a pristine DEMO install later.
                    // The marker is deliberately outside SharedPreferences: bad XML can read as {}.
                    try {
                        if (!hasInitializationMarker()) check(marker.createNewFile()) {
                            "无法创建共享配置初始化标记。"
                        }
                        check(Files.isRegularFile(marker.toPath(), NOFOLLOW_LINKS) &&
                            !Files.isSymbolicLink(marker.toPath())) { "共享配置初始化标记无效。" }
                        FileOutputStream(marker, false).use { output ->
                            output.write("PhoneNAS host configuration initialized v1\n".toByteArray(Charsets.UTF_8))
                            output.fd.sync()
                        }
                        // Persist the new directory entry before committing the configuration.
                        val parent = android.system.Os.open(marker.parentFile!!.path,
                            android.system.OsConstants.O_RDONLY, 0)
                        try { android.system.Os.fsync(parent) } finally { android.system.Os.close(parent) }
                    } catch (e: Exception) {
                        androidCommitFailed = true
                        throw IllegalStateException("无法持久化共享配置初始化标记；配置未提交。", e)
                    }
                    val editor = prefs.edit().clear()
                    values.forEach { (key, value) ->
                        when (value) {
                            is String -> editor.putString(key, value)
                            is Int -> editor.putInt(key, value)
                            is Boolean -> editor.putBoolean(key, value)
                            else -> error("不支持的配置字段类型。")
                        }
                    }
                    return try { editor.commit().also { if (!it) androidCommitFailed = true } }
                    catch (e: Exception) { androidCommitFailed = true; throw e }
                }
            }
        }
    }
}
