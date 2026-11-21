package com.phonenas.client

import android.content.ContentProviderClient
import android.content.ContentResolver
import android.content.Context
import android.database.Cursor
import android.net.Uri
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import java.io.Closeable
import java.io.File
import java.io.InputStream
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.nio.file.Path
import java.nio.file.StandardOpenOption.CREATE_NEW
import java.nio.file.StandardOpenOption.WRITE
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

internal interface UploadContent : Closeable {
    val length: Long

    /** The input is owned by this call and is valid only during [consume]. */
    fun withInput(operation: CloudOperation, consume: (InputStream) -> Unit)
}

internal class UploadDocument(val uri: Uri, val name: String, val reportedLength: Long?)

/** Blocking SAF adapter; retains no grant beyond the picker-provided access. */
internal class CloudUploadSource(context: Context) {
    private val context = context.applicationContext
    private val spool by lazy {
        uploadIo(CloudIssue.UPLOAD_STAGING) {
            val cache = this.context.cacheDir ?: throw CloudFailure(CloudIssue.UPLOAD_STAGING)
            UploadSpool(cache.resolve("cloud-uploads-v1"))
        }
    }

    fun inspect(uri: Uri, operation: CloudOperation): UploadDocument {
        requireDocument(uri, operation)
        val signal = CancellationSignal()
        return operation.duringCancellation({ cancelOffThread(signal::cancel) }) {
            withProvider(uri, operation) { provider ->
                uploadIo(CloudIssue.UPLOAD_SOURCE, operation) {
                    operation.check()
                    val projection = arrayOf(Document.COLUMN_DISPLAY_NAME, Document.COLUMN_SIZE,
                        Document.COLUMN_MIME_TYPE, Document.COLUMN_FLAGS)
                    val cursor = provider.query(uri, projection, null, signal)
                        ?: throw CloudFailure(CloudIssue.UPLOAD_SOURCE)
                    cursor.use {
                        operation.check()
                        if (!it.moveToFirst()) throw CloudFailure(CloudIssue.UPLOAD_SOURCE)
                        val type = it.getString(it.getColumnIndexOrThrow(Document.COLUMN_MIME_TYPE))
                        val flagsIndex = it.getColumnIndex(Document.COLUMN_FLAGS)
                        val flags = if (flagsIndex >= 0) it.getInt(flagsIndex) else 0
                        if (type.isNullOrBlank() || type == Document.MIME_TYPE_DIR ||
                            flags and (Document.FLAG_VIRTUAL_DOCUMENT or Document.FLAG_PARTIAL) != 0) {
                            throw CloudFailure(CloudIssue.UPLOAD_SOURCE)
                        }
                        val name = it.getString(it.getColumnIndexOrThrow(Document.COLUMN_DISPLAY_NAME))
                            ?.takeIf(String::isNotBlank) ?: throw CloudFailure(CloudIssue.UPLOAD_SOURCE)
                        val sizeIndex = it.getColumnIndex(Document.COLUMN_SIZE)
                        // Zero may be a placeholder; read such sources to obtain a trustworthy size.
                        val size = if (sizeIndex >= 0 && it.getType(sizeIndex) == Cursor.FIELD_TYPE_INTEGER) {
                            it.getLong(sizeIndex).takeIf { bytes -> bytes > 0L }
                        } else null
                        UploadDocument(uri, name, size)
                    }
                }
            }
        }.also { operation.check() }
    }

    fun prepare(document: UploadDocument, operation: CloudOperation, progress: (Long) -> Unit): UploadContent {
        val uri = document.uri
        requireDocument(uri, operation)
        val size = document.reportedLength
        if (size != null && size > 0L) {
            if (size > MAX_UPLOAD_BYTES) throw CloudFailure(CloudIssue.UPLOAD_CAPACITY)
            return object : UploadContent {
                override val length: Long = size
                private val closed = AtomicBoolean()

                override fun withInput(operation: CloudOperation, consume: (InputStream) -> Unit) {
                    operation.check()
                    if (closed.get()) throw CloudFailure(CloudIssue.UPLOAD_SOURCE)
                    withDocumentInput(uri, operation, consume)
                }

                override fun close() { closed.set(true) }
            }
        }

        var staged: UploadContent? = null
        try {
            withDocumentInput(uri, operation) { staged = spool.receive(it, operation, progress) }
            return checkNotNull(staged)
        } catch (failure: Throwable) {
            // The stage may already exist if the provider failed to close or cancellation won the race.
            staged?.let { failure.suppressCleanup(it::close) }
            throw failure
        }
    }

    fun cleanAbandoned() = spool.cleanAbandoned()

    private fun requireDocument(uri: Uri, operation: CloudOperation) {
        operation.check()
        uploadIo(CloudIssue.UPLOAD_SOURCE, operation) {
            if (uri.scheme != ContentResolver.SCHEME_CONTENT || !DocumentsContract.isDocumentUri(context, uri)) {
                throw CloudFailure(CloudIssue.UPLOAD_SOURCE)
            }
        }
        operation.check()
    }

    private fun withDocumentInput(uri: Uri, operation: CloudOperation, consume: (InputStream) -> Unit) {
        val signal = CancellationSignal()
        operation.duringCancellation({ cancelOffThread(signal::cancel) }) {
            withProvider(uri, operation) { provider ->
                // ContentResolver's read-only open uses a typed wildcard, and DocumentsProvider can
                // drop the signal on that path. Open the whole document directly instead.
                val input = uploadIo(CloudIssue.UPLOAD_SOURCE, operation) {
                    operation.check()
                    val descriptor = provider.openFile(uri, "r", signal) ?: throw CloudFailure(CloudIssue.UPLOAD_SOURCE)
                    try {
                        ParcelFileDescriptor.AutoCloseInputStream(descriptor)
                    } catch (failure: Throwable) {
                        failure.suppressCleanup(descriptor::close)
                        throw failure
                    }
                }
                withUploadInput(input, operation, CloudIssue.UPLOAD_SOURCE, consume)
            }
        }
        operation.check()
    }

    private fun <T> withProvider(uri: Uri, operation: CloudOperation, block: (ContentProviderClient) -> T): T {
        operation.check()
        val provider = uploadIo(CloudIssue.UPLOAD_SOURCE, operation) {
            context.contentResolver.acquireUnstableContentProviderClient(uri)
                ?: throw CloudFailure(CloudIssue.UPLOAD_SOURCE)
        }
        return Closeable { uploadIo(CloudIssue.UPLOAD_SOURCE, operation) { provider.close() } }.use { block(provider) }
    }
}

internal const val MAX_UPLOAD_SPOOL_BYTES = 512L * 1024 * 1024

/** App-private cache for an unknown-size upload. The caller owns the incoming stream. */
internal class UploadSpool(private val directory: File, private val limit: Long = MAX_UPLOAD_SPOOL_BYTES) {
    init { require(limit in 0..MAX_UPLOAD_SPOOL_BYTES) }

    fun receive(input: InputStream, operation: CloudOperation, progress: (Long) -> Unit): UploadContent {
        operation.check()
        val (path, output) = uploadIo(CloudIssue.UPLOAD_STAGING, operation) {
            synchronized(ownership) {
                val root = checkNotNull(openRoot(create = true))
                if (root in activeStages) throw CloudFailure(CloudIssue.UPLOAD_STAGING)
                val path = root.resolve("upload-${UUID.randomUUID()}.part")
                val output = Files.newOutputStream(path, CREATE_NEW, WRITE, NOFOLLOW_LINKS)
                activeStages[root] = path
                path to output
            }
        }
        try {
            var size = 0L
            Closeable { uploadIo(CloudIssue.UPLOAD_STAGING, operation) { output.close() } }.use {
                val buffer = ByteArray(64 * 1024)
                while (true) {
                    operation.check()
                    // Read at most one byte past the limit to distinguish exact capacity from overflow.
                    val max = minOf(buffer.size.toLong(), limit - size + 1).toInt()
                    val read = uploadIo(CloudIssue.UPLOAD_SOURCE, operation) { input.read(buffer, 0, max) }
                    operation.check()
                    if (read == -1) break
                    if (read !in 1..max) throw CloudFailure(CloudIssue.UPLOAD_LENGTH)
                    if (read > limit - size) throw CloudFailure(CloudIssue.UPLOAD_CAPACITY)
                    uploadIo(CloudIssue.UPLOAD_STAGING, operation) { output.write(buffer, 0, read) }
                    size += read
                    operation.check()
                    progress(size)
                }
            }
            operation.check()
            if (size == 0L) progress(0L)
            operation.check()
            return StagedUpload(path, size)
        } catch (failure: Throwable) {
            failure.suppressCleanup { releaseStage(path) }
            throw failure
        }
    }

    fun cleanAbandoned() = uploadIo(CloudIssue.UPLOAD_STAGING) {
        synchronized(ownership) {
            val root = openRoot(create = false) ?: return@synchronized
            Files.newDirectoryStream(root).use { files ->
                for (file in files) {
                    if (file != activeStages[root] && stageName.matches(file.fileName.toString()) &&
                        Files.isRegularFile(file, NOFOLLOW_LINKS)) {
                        Files.deleteIfExists(file)
                    }
                }
            }
        }
    }

    private fun openRoot(create: Boolean): Path? {
        val root = directory.toPath().toAbsolutePath().normalize()
        if (create) Files.createDirectories(root)
        else if (Files.notExists(root, NOFOLLOW_LINKS)) return null
        if (!Files.isDirectory(root, NOFOLLOW_LINKS)) throw CloudFailure(CloudIssue.UPLOAD_STAGING)
        return root.toRealPath()
    }

    private class StagedUpload(private val path: Path, override val length: Long) : UploadContent {
        private var closed = false

        override fun withInput(operation: CloudOperation, consume: (InputStream) -> Unit) {
            operation.check()
            val input = uploadIo(CloudIssue.UPLOAD_STAGING, operation) { open() }
            withUploadInput(input, operation, CloudIssue.UPLOAD_STAGING, consume)
        }

        @Synchronized private fun open(): InputStream {
            if (closed || !Files.isRegularFile(path, NOFOLLOW_LINKS) || Files.size(path) != length) {
                throw CloudFailure(CloudIssue.UPLOAD_STAGING)
            }
            return Files.newInputStream(path, NOFOLLOW_LINKS)
        }

        @Synchronized override fun close() {
            if (closed) return
            closed = true
            releaseStage(path)
        }
    }

    private companion object {
        // Activity recreation can create another cleaner while this process still owns a stage.
        val ownership = Any()
        val activeStages = mutableMapOf<Path, Path>()
        val stageName = Regex("upload-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\\.part")

        fun releaseStage(path: Path) = uploadIo(CloudIssue.UPLOAD_STAGING) {
            synchronized(ownership) {
                if (activeStages[path.parent] == path) {
                    try {
                        if (Files.isRegularFile(path, NOFOLLOW_LINKS)) Files.deleteIfExists(path)
                        else if (!Files.notExists(path, NOFOLLOW_LINKS)) throw CloudFailure(CloudIssue.UPLOAD_STAGING)
                    } finally {
                        activeStages.remove(path.parent)
                    }
                }
            }
        }
    }
}

/** Owns a single open input; cancellation can break a read without waiting for the consuming thread. */
internal fun withUploadInput(input: InputStream, operation: CloudOperation, issue: CloudIssue,
                             consume: (InputStream) -> Unit) {
    val closed = AtomicBoolean()
    val scoped = object : InputStream() {
        override fun read(): Int = readInput { input.read() }
        override fun read(buffer: ByteArray, offset: Int, count: Int): Int =
            readInput { input.read(buffer, offset, count) }

        private inline fun readInput(read: () -> Int): Int {
            operation.check()
            if (closed.get()) throw CloudFailure(issue)
            return uploadIo(issue, operation, read).also { operation.check() }
        }

        override fun close() {
            if (closed.compareAndSet(false, true)) uploadIo(issue, operation) { input.close() }
        }
    }
    scoped.use {
        operation.duringCancellation({ cancelOffThread(scoped::close) }) {
            operation.check()
            // The callback may write to a remote sink; do not translate its errors as input errors.
            consume(scoped)
            operation.check()
        }
    }
    operation.check()
}

// Provider cancellation can itself block in Binder; never wait for it or for a close on the UI thread.
private val uploadCanceller = Executors.newCachedThreadPool { action ->
    Thread(action, "cloud-upload-cancel").apply { isDaemon = true }
}

private fun cancelOffThread(action: () -> Unit) {
    uploadCanceller.execute { runCatching(action) }
}

private inline fun <T> uploadIo(issue: CloudIssue, operation: CloudOperation? = null, block: () -> T): T = try {
    block()
} catch (failure: Exception) {
    operation?.check()
    if (failure is CloudFailure) throw failure
    throw CloudFailure(issue)
}

private inline fun Throwable.suppressCleanup(cleanup: () -> Unit) {
    try {
        cleanup()
    } catch (failure: Exception) {
        addSuppressed(failure)
    }
}
