package com.phonenas.client

import android.content.ContentResolver
import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.CancellationSignal
import android.os.Environment
import android.os.OperationCanceledException
import android.os.ParcelFileDescriptor
import android.provider.MediaStore
import android.provider.MediaStore.MediaColumns.DISPLAY_NAME
import android.provider.MediaStore.MediaColumns.GENERATION_ADDED
import android.provider.MediaStore.MediaColumns.IS_PENDING
import android.provider.MediaStore.MediaColumns.MIME_TYPE
import android.provider.MediaStore.MediaColumns.OWNER_PACKAGE_NAME
import android.provider.MediaStore.MediaColumns.RELATIVE_PATH
import android.webkit.MimeTypeMap
import java.io.OutputStream
import java.util.UUID
import java.util.concurrent.Executors

/** Blocking save for the transfer worker. The copy callback borrows (and must not close) the stream. */
internal class CloudDownloads(private val context: Context) {
    fun save(
        name: String,
        length: Long,
        operation: CloudOperation,
        verifyActive: () -> Unit,
        copy: (OutputStream) -> Unit,
    ): PublishedDownload {
        val signal = CancellationSignal()
        return operation.duringCancellation({ canceller.execute { signal.cancel() } }) {
            savePendingDownload(
                downloadName(name), length, verifyActive,
                create = { createPending(signal, operation) }, copy = copy,
            )
        }
    }

    private fun createPending(signal: CancellationSignal, operation: CloudOperation): PendingCloudDownload {
        val resolver = context.contentResolver
        val collection = MediaStore.Downloads.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
        // Some provider versions upsert when the same pending path is inserted before it is
        // opened. Use a distinct pending name, then let MediaStore uniquify the final name.
        val temporaryName = "PhoneNAS-${UUID.randomUUID()}"
        val uri = resolver.insert(collection, ContentValues().apply {
            put(DISPLAY_NAME, temporaryName)
            put(RELATIVE_PATH, downloadPath)
            put(MIME_TYPE, BINARY_MIME)
            put(IS_PENDING, 1)
        }) ?: throw CloudFailure(CloudIssue.DOWNLOAD_SAVE)
        val id = ContentUris.parseId(uri)
        if (id <= 0L || uri != ContentUris.withAppendedId(collection, id)) {
            throw CloudFailure(CloudIssue.DOWNLOAD_SAVE)
        }
        return MediaStoreDownload(resolver, uri, context.packageName, temporaryName, signal, operation)
    }

    private companion object {
        val canceller = Executors.newCachedThreadPool { task ->
            Thread(task, "Cloud-download-cancel").apply { isDaemon = true }
        }
    }
}

private val downloadPath = "${Environment.DIRECTORY_DOWNLOADS}/PhoneNAS/"
private const val BINARY_MIME = "application/octet-stream"
private const val OWNED_PENDING = "$IS_PENDING = 1 AND $OWNER_PACKAGE_NAME = ? AND $RELATIVE_PATH = ? AND $DISPLAY_NAME = ?"

private class MediaStoreDownload(
    private val resolver: ContentResolver,
    private val uri: Uri,
    private val owner: String,
    private val temporaryName: String,
    private val signal: CancellationSignal,
    private val operation: CloudOperation,
) : PendingCloudDownload {
    private var generation: Long? = null
    private val pendingArgs get() = arrayOf(owner, downloadPath, temporaryName)
    private val selection get() = if (generation == null) OWNED_PENDING else "$OWNED_PENDING AND $GENERATION_ADDED = ?"
    private val selectionArgs get() = generation?.let { pendingArgs + it.toString() } ?: pendingArgs

    override fun open(): PendingDownloadFile {
        try {
            val row = readRow(signal) ?: throw CloudFailure(CloudIssue.DOWNLOAD_SAVE)
            if (row.pending != 1 || row.name != temporaryName) throw CloudFailure(CloudIssue.DOWNLOAD_SAVE)
            generation = row.generation
            // Do not request truncation, even for this new row. The lifecycle checks it's empty.
            val descriptor = resolver.openFileDescriptor(uri, "rw", signal)
                ?: throw CloudFailure(CloudIssue.DOWNLOAD_SAVE)
            return object : PendingDownloadFile {
                private val stream = ParcelFileDescriptor.AutoCloseOutputStream(descriptor)
                override val output: OutputStream get() = stream
                override val size: Long get() = descriptor.statSize
                override fun sync() {
                    stream.fd.sync()
                    if (descriptor.canDetectErrors()) descriptor.checkError()
                }
                override fun close() = stream.close()
            }
        } catch (_: OperationCanceledException) {
            operation.check()
            throw CloudFailure(CloudIssue.DOWNLOAD_SAVE)
        }
    }

    override fun publish(name: String): Int {
        val mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(name.substringAfterLast('.', "").lowercase())
        val values = ContentValues().apply {
            put(DISPLAY_NAME, name)
            put(MIME_TYPE, mime ?: BINARY_MIME)
            put(IS_PENDING, 0)
        }
        return resolver.update(uri, values, selection, selectionArgs)
    }

    override fun publishedName(): String? = readRow(null)?.takeIf {
        it.pending == 0 && it.generation == generation
    }?.name?.takeIf { it.isNotBlank() }

    override fun discardIfPending() {
        // The predicate is applied by the provider to this exact URI. No collection delete,
        // no path/name lookup, and no unconditional delete if the provider has published it.
        resolver.delete(uri, selection, selectionArgs)
    }

    private data class DownloadRow(val pending: Int, val name: String, val generation: Long)

    private fun readRow(cancellation: CancellationSignal?): DownloadRow? {
        // An item URI includes its pending row on API 30+; no deprecated setIncludePending.
        val columns = arrayOf(IS_PENDING, DISPLAY_NAME, GENERATION_ADDED)
        val cursor = resolver.query(uri, columns, "$OWNER_PACKAGE_NAME = ? AND $RELATIVE_PATH = ?",
            arrayOf(owner, downloadPath), null, cancellation)
            ?: throw CloudFailure(CloudIssue.DOWNLOAD_SAVE)
        return cursor.use {
            if (it.count != 1 || !it.moveToFirst() || (0..2).any(it::isNull)) null
            else DownloadRow(it.getInt(0), it.getString(1), it.getLong(2))
        }
    }
}

// Supply only a visible basename. MediaStore performs any further filesystem sanitization.
private fun downloadName(name: String): String = buildString {
    for (character in name) append(
        if (character.isISOControl() || character in "\\/:*?\"<>|" ||
            character in '\u202a'..'\u202e' || character in '\u2066'..'\u2069') '_' else character,
    )
}.trim().trimEnd('.').ifEmpty { "download" }.let { if (it.startsWith('.')) "_$it" else it }
