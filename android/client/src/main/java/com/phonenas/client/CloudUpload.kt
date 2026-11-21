package com.phonenas.client

import java.nio.charset.CharacterCodingException

internal const val MAX_BATCH_FILES = 50

/** Client-side guard against writing into an unrelated share. This is not a per-user server ACL. */
internal class ExchangeShare private constructor(val root: DavPath) {
    fun contains(directory: DavPath): Boolean {
        if (!directory.isCollection) return false
        var path = directory
        while (path.parent != null) {
            if (!safeFilename(path.name) || protectedFolder(path.name)) return false
            if (path == root) return true
            path = checkNotNull(path.parent)
        }
        return false
    }

    override fun equals(other: Any?): Boolean = other is ExchangeShare && root == other.root
    override fun hashCode(): Int = root.hashCode()

    companion object {
        fun selectable(path: DavPath): Boolean = path.isCollection && path.parent == DavPath.Root &&
            safeFilename(path.name) && !protectedFolder(path.name)

        fun of(path: DavPath): ExchangeShare {
            if (!selectable(path)) throw CloudFailure(CloudIssue.UPLOAD_TARGET)
            return ExchangeShare(path)
        }

        fun restore(name: String): ExchangeShare = of(checkedChild(DavPath.Root, name, true))

        private fun protectedFolder(name: String) = name.lowercase() in
            setOf("backup", "mirror", "versions", "history", "baselines", "镜像", "历史")
    }
}

internal class UploadItem(
    val name: String,
    val prepare: (CloudOperation, (Long) -> Unit) -> UploadContent,
)

internal enum class UploadState { CREATED, SKIPPED, FAILED, UNKNOWN, CANCELLED, NOT_STARTED }
internal data class UploadResult(
    val name: String, val state: UploadState, val savedAs: String? = null, val issue: CloudIssue? = null,
)

internal enum class UploadPhase { PREPARING, SENDING, CONFIRMING }
internal data class UploadProgress(
    val index: Int, val totalFiles: Int, val phase: UploadPhase, val bytes: Long = 0L, val totalBytes: Long? = null,
)

/** Serial writes to one explicitly chosen destination, never retries an ambiguous write. */
internal class CloudUploadBatch(private val dav: CloudDav) {
    fun <T> send(
        exchange: ExchangeShare, directory: DavPath, files: List<T>, keepBoth: Boolean,
        operation: CloudOperation, resolve: (T, CloudOperation) -> UploadItem, progress: (UploadProgress) -> Unit,
    ): List<UploadResult> {
        check(exchange.contains(directory) && files.size in 1..MAX_BATCH_FILES)
        val unavailable = try {
            dav.checkWritable(directory, operation)
            null
        } catch (failure: CloudFailure) {
            failure.issue
        }
        var stopped = unavailable
        return files.mapIndexed { index, file ->
            var name = "文件 ${index + 1}"
            stopped?.let { return@mapIndexed UploadResult(name, UploadState.NOT_STARTED, issue = it) }
            fun update(phase: UploadPhase, bytes: Long = 0L, total: Long? = null) =
                progress(UploadProgress(index + 1, files.size, phase, bytes, total))
            try {
                operation.check()
                update(UploadPhase.PREPARING)
                val item = resolve(file, operation)
                if (safeFilename(item.name)) name = item.name
                val original = checkedChild(directory, item.name, false)
                val content = item.prepare(operation) { update(UploadPhase.PREPARING, it) }
                try {
                    sendOne(original, content, keepBoth, operation) { bytes, total ->
                        update(if (bytes == total) UploadPhase.CONFIRMING else UploadPhase.SENDING, bytes, total)
                    }.also { if (it.state == UploadState.UNKNOWN) stopped = CloudIssue.UPLOAD_UNCONFIRMED }
                } finally {
                    // A cache cleanup failure must not turn a confirmed 201 into a failed upload.
                    runCatching { content.close() }
                }
            } catch (failure: CloudFailure) {
                val issue = failure.issue
                val state = when (issue) {
                    CloudIssue.UPLOAD_EXISTS -> UploadState.SKIPPED
                    CloudIssue.UPLOAD_UNCONFIRMED -> UploadState.UNKNOWN
                    CloudIssue.CANCELLED -> UploadState.CANCELLED
                    else -> UploadState.FAILED
                }
                if (issue in stopBatch) stopped = issue
                UploadResult(name, state, issue = issue)
            }
        }
    }

    private fun sendOne(path: DavPath, content: UploadContent, keepBoth: Boolean, operation: CloudOperation,
                        progress: (Long, Long) -> Unit): UploadResult {
        val name = path.name
        for (attempt in 1..(if (keepBoth) MAX_NAME_ATTEMPTS else 1)) {
            val target = if (attempt == 1) path else checkedChild(checkNotNull(path.parent), copyName(name, attempt), false)
            try {
                operation.check()
                progress(0L, content.length.coerceAtLeast(1L))
                dav.upload(target, content, operation, progress)
                return UploadResult(name, UploadState.CREATED, savedAs = target.name)
            } catch (failure: CloudFailure) {
                if (failure.issue == CloudIssue.UPLOAD_UNCONFIRMED) {
                    return UploadResult(name, UploadState.UNKNOWN, savedAs = target.name, issue = failure.issue)
                }
                // A directory occupying the same name is 409 on the host. Other 409s
                // also exist (e.g. a removed parent), so verify before trying a new name.
                if (failure.issue == CloudIssue.UPLOAD_CONFLICT &&
                    dav.list(checkNotNull(path.parent), operation).none { it.path.name == target.name }) throw failure
                if (failure.issue != CloudIssue.UPLOAD_EXISTS && failure.issue != CloudIssue.UPLOAD_CONFLICT) throw failure
            }
        }
        return UploadResult(name, UploadState.SKIPPED, issue = CloudIssue.UPLOAD_EXISTS)
    }

    private companion object {
        const val MAX_NAME_ATTEMPTS = 20
        val stopBatch = setOf(CloudIssue.CANCELLED, CloudIssue.VPN, CloudIssue.UNREACHABLE,
            CloudIssue.CERTIFICATE, CloudIssue.AUTH, CloudIssue.UPLOAD_UNCONFIRMED, CloudIssue.UPLOAD_READ_ONLY,
            CloudIssue.NOT_FOUND, CloudIssue.UPLOAD_STORAGE, CloudIssue.UPLOAD_BUSY, CloudIssue.RESPONSE)
    }
}

private const val MAX_BASENAME_BYTES = 255

private fun safeFilename(name: String): Boolean = name.isNotBlank() && name.length <= MAX_BASENAME_BYTES &&
    name != "." && name != ".." && name.none {
        it == '/' || it == '\\' || it.isISOControl() || it in '\u202a'..'\u202e' || it in '\u2066'..'\u2069'
    } && try {
        name.encodeToByteArray(throwOnInvalidSequence = true).size <= MAX_BASENAME_BYTES
    } catch (_: CharacterCodingException) {
        false
    }

private fun checkedChild(parent: DavPath, name: String, collection: Boolean): DavPath {
    if (!safeFilename(name)) throw CloudFailure(CloudIssue.UPLOAD_TARGET)
    return parent.visibleChild(name, collection) ?: throw CloudFailure(CloudIssue.UPLOAD_TARGET)
}

private fun copyName(name: String, attempt: Int): String {
    val dot = name.lastIndexOf('.')
    val suffix = " ($attempt)"
    val extension = if (dot > 0 && dot < name.lastIndex && name.length - dot <= 32) name.substring(dot) else ""
    val limit = MAX_BASENAME_BYTES - (suffix + extension).encodeToByteArray().size
    var stem = name.dropLast(extension.length)
    while (stem.encodeToByteArray().size > limit) stem = stem.dropLast(Character.charCount(stem.codePointBefore(stem.length)))
    return stem + suffix + extension
}
