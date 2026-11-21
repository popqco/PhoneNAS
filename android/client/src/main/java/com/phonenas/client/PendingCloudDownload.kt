package com.phonenas.client

import java.io.Closeable
import java.io.OutputStream

internal class PublishedDownload(val name: String)

/** A handle to this attempt's row, never a lookup by the requested final name. */
internal interface PendingCloudDownload {
    fun open(): PendingDownloadFile
    fun publish(name: String): Int
    fun publishedName(): String?
    fun discardIfPending()
}

internal interface PendingDownloadFile : Closeable {
    val output: OutputStream
    val size: Long
    fun sync()
}

internal fun savePendingDownload(
    name: String,
    length: Long,
    verifyActive: () -> Unit,
    create: () -> PendingCloudDownload,
    copy: (OutputStream) -> Unit,
): PublishedDownload {
    if (length < 0L) throw CloudFailure(CloudIssue.DOWNLOAD_LENGTH)
    val pending = saveIo {
        verifyActive()
        create()
    }
    var confirmed = false
    try {
        saveIo {
            verifyActive()
            val file = pending.open()
            var written = false
            try {
                if (file.size != 0L) throw CloudFailure(CloudIssue.DOWNLOAD_SAVE)
                copy(file.output)
                file.output.flush()
                if (file.size != length) throw CloudFailure(CloudIssue.DOWNLOAD_SAVE)
                file.sync()
                written = true
            } finally {
                try {
                    file.close()
                } catch (_: Exception) {
                    // A raw provider error must not be attached to an upstream CloudFailure.
                    if (written) throw CloudFailure(CloudIssue.DOWNLOAD_SAVE)
                }
            }
        }
        // MediaStore chooses a free destination when it publishes. Do not race another save
        // from this process for that destination, and check the route after acquiring the lock.
        return synchronized(publicationLock) {
            saveIo(verifyActive)
            val updated = try { pending.publish(name) } catch (_: Exception) { -1 }
            confirmed = updated == 1
            val actualName = try { pending.publishedName() } catch (_: Exception) {
                throw CloudFailure(CloudIssue.DOWNLOAD_SAVE_UNKNOWN)
            }
            if (actualName == null) throw CloudFailure(
                if (updated == 0) CloudIssue.DOWNLOAD_SAVE else CloudIssue.DOWNLOAD_SAVE_UNKNOWN,
            )
            confirmed = true
            PublishedDownload(actualName)
        }
    } finally {
        if (!confirmed) {
            // This must be conditional: the provider may have published before losing its reply.
            try { pending.discardIfPending() } catch (_: Exception) { /* provider can expire the row */ }
        }
    }
}

private val publicationLock = Any()

private inline fun <T> saveIo(block: () -> T): T = try {
    block()
} catch (failure: CloudFailure) {
    throw failure
} catch (_: Exception) {
    throw CloudFailure(CloudIssue.DOWNLOAD_SAVE)
}
