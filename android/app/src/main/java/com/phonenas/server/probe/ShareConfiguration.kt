package com.phonenas.server.probe

import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.util.Locale

/** A configured export, not ownership of the directory or permission to delete it. */
data class ShareConfiguration(
    val alias: String,
    val root: String,
    val readOnly: Boolean = false,
    val enabled: Boolean = true,
)

/** Public validation boundary shared by the picker, settings persistence and launch preflight. */
object ShareRules {
    /** Parse a DocumentsContract tree ID, never a provider bridge or an alternate volume. */
    fun resolvePrimaryTree(documentId: String, primaryRoot: File): File {
        require(documentId.startsWith("primary:")) { "仅支持内置主存储的公共目录。" }
        val relative = documentId.removePrefix("primary:")
        require(relative.isNotEmpty() && '\\' !in relative && ':' !in relative &&
            relative.none { it.code < 32 || it.code == 127 }) { "目录标识无效。" }
        require(relative.split('/').all { it.isNotEmpty() && it != "." && it != ".." }) {
            "不能选择存储根目录或含路径跳转的目录。"
        }
        return File(primaryRoot, relative)
    }

    fun validateShares(
        shares: List<ShareConfiguration>,
        primaryRoot: File,
        privateRoots: List<File> = emptyList(),
        controlRoot: File? = null,
    ) {
        val primary = requireExistingDirectory(primaryRoot)
        val forbidden = listOf(File(primary, "Android")) + privateRoots + listOfNotNull(controlRoot)
        val aliases = HashSet<String>()
        val roots = ArrayList<File>()
        shares.forEach {
            requireValidAlias(it.alias)
            require(aliases.add(it.alias.lowercase(Locale.ROOT))) { "共享别名不能重复（忽略大小写）。" }
            val root = requireExistingDirectory(File(it.root))
            require(root.toPath().startsWith(primary.toPath()) && root != primary) {
                "共享必须是内置主存储内的公共子目录，不能导出整个存储。"
            }
            require(forbidden.none { blocked -> overlaps(root, blocked.canonicalFile) ||
                (blocked.exists() && Files.isSameFile(root.toPath(), blocked.toPath())) }) {
                "不能共享 Android、应用私有目录或控制目录。"
            }
            require(roots.none { other -> overlaps(root, other) ||
                Files.isSameFile(root.toPath(), other.toPath()) }) {
                "共享目录不能相同、互为父子或指向同一物理目录。"
            }
            roots.add(root)
            require(it.readOnly || root.canWrite()) { "可写共享的目录已失去写入权限。" }
        }
    }

    /** Read-only inspection: never creates a missing directory or tests access by writing a file. */
    fun requireExistingDirectory(directory: File): File {
        require(directory.isAbsolute && directory.toPath().normalize() == directory.toPath()) {
            "目录必须是绝对且无路径跳转的路径。"
        }
        val absolute = directory.absoluteFile
        require(absolute.canonicalFile == absolute) { "不支持符号链接或非规范目录路径。" }
        var part: File? = absolute
        while (part != null) {
            require(Files.isDirectory(part.toPath(), NOFOLLOW_LINKS) &&
                !Files.isSymbolicLink(part.toPath())) { "目录已丢失、不是目录或含符号链接。" }
            part = part.parentFile
        }
        require(absolute.canRead() && absolute.canExecute()) { "无法读取或进入所选目录。" }
        try {
            Files.newDirectoryStream(absolute.toPath()).use { /* Opening verifies current access. */ }
        } catch (e: Exception) {
            throw IllegalArgumentException("无法访问所选目录。", e)
        }
        return absolute
    }

    private fun overlaps(a: File, b: File): Boolean =
        a.toPath().startsWith(b.toPath()) || b.toPath().startsWith(a.toPath())

    fun requireValidAlias(alias: String) {
        require(Regex("[a-zA-Z0-9_-]{1,40}").matches(alias)) {
            "共享别名必须是 1–40 个 ASCII 字母、数字、下划线或连字符；.phonenas 为系统保留。"
        }
    }
}
