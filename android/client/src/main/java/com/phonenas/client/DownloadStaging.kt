package com.phonenas.client

import java.io.Closeable
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import java.util.UUID

private const val MAX_DOWNLOAD_BYTES = 512L * 1024 * 1024

internal class DownloadLengthException(message: String) : IOException(message)
internal class DownloadCapacityException : IOException("Download exceeds the 512 MiB limit")
internal class DownloadStagingException(message: String, cause: IOException? = null) : IOException(message, cause)

/** Private app-cache staging. All operations are blocking; the caller owns the input stream. */
internal class DownloadStaging(private val directory: File) {
    fun receive(
        input: InputStream,
        expectedLength: Long,
        expectedFromListing: Long? = null,
        checkActive: () -> Unit = {},
        progress: (Long, Long) -> Unit = { _, _ -> },
    ): StagedDownload {
        if (expectedLength < 0) throw DownloadLengthException("Download length is unknown")
        if (expectedFromListing != null && expectedFromListing != expectedLength) {
            throw DownloadLengthException("Download length does not match the listing")
        }
        if (expectedLength > MAX_DOWNLOAD_BYTES) throw DownloadCapacityException()
        checkActive()
        val (path, output) = synchronized(ownership) {
            val root = privateIo("Could not open the private download cache") {
                directory.canonicalFile.toPath().also { Files.createDirectories(it) }
            }
            if (root in activeDirectories) throw DownloadStagingException("A download is already staged")
            val file = root.resolve("download-${UUID.randomUUID()}.part")
            val stream = privateIo("Could not create the private download stage") {
                Files.newOutputStream(file, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)
            }
            activeDirectories[root] = file
            file to stream
        }
        try {
            Closeable { privateIo("Could not close the private download stage") { output.close() } }.use {
                transferExactly(
                    expectedLength, checkActive,
                    read = { buffer, count -> input.read(buffer, 0, count) },
                    write = { buffer, count ->
                        privateIo("Could not write the private download stage") { output.write(buffer, 0, count) }
                    },
                    lengthError = ::DownloadLengthException,
                    progress = { if (it < expectedLength) progress(it, expectedLength) },
                )
            }
            checkActive()
            progress(expectedLength, expectedLength)
            checkActive()
            return StagedDownload(path, expectedLength)
        } catch (failure: Throwable) {
            try {
                releaseStage(path)
            } catch (cleanupFailure: IOException) {
                failure.addSuppressed(cleanupFailure)
            }
            throw failure
        }
    }

    fun cleanAbandoned() = synchronized(ownership) {
        privateIo("Could not clean the private download cache") {
            val root = directory.canonicalFile.toPath()
            if (Files.notExists(root, NOFOLLOW_LINKS)) return@privateIo
            Files.newDirectoryStream(root).use { entries ->
                for (file in entries) {
                    if (file != activeDirectories[root] && stageName.matches(file.fileName.toString()) &&
                        Files.isRegularFile(file, NOFOLLOW_LINKS)
                    ) {
                        Files.deleteIfExists(file)
                    }
                }
            }
        }
    }
}

// App recreation can produce a second DownloadStaging instance while the first still owns a file.
// Protect registration, release, and the sweep together without locking during a transfer.
private val ownership = Any()
private val activeDirectories = mutableMapOf<Path, Path>()
private val stageName = Regex("download-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\\.part")

private fun releaseStage(path: Path) = synchronized(ownership) {
    if (activeDirectories[path.parent] == path) {
        try {
            if (!Files.isRegularFile(path, NOFOLLOW_LINKS) && !Files.notExists(path, NOFOLLOW_LINKS)) {
                throw DownloadStagingException("Could not safely delete the private download stage")
            }
            privateIo("Could not delete the private download stage") { Files.deleteIfExists(path) }
        } finally {
            // If deletion failed, the file is abandoned and a later cache sweep can retry it.
            activeDirectories.remove(path.parent)
        }
    }
}

private inline fun <T> privateIo(message: String, operation: () -> T): T = try {
    operation()
} catch (error: IOException) {
    throw DownloadStagingException(message, error)
}

internal fun transferExactly(
    length: Long,
    checkActive: () -> Unit,
    read: (ByteArray, Int) -> Int,
    write: (ByteArray, Int) -> Unit,
    lengthError: (String) -> IOException,
    progress: (Long) -> Unit = {},
) {
    val buffer = ByteArray(64 * 1024)
    var transferred = 0L
    while (transferred < length) {
        checkActive()
        val max = minOf(length - transferred, buffer.size.toLong()).toInt()
        val count = read(buffer, max)
        checkActive()
        if (count == -1) throw lengthError("Download ended before the expected length")
        if (count !in 1..max) throw lengthError("Download stream returned an invalid byte count")
        write(buffer, count)
        transferred += count
        progress(transferred)
    }
    checkActive()
    val extra = read(buffer, 1)
    checkActive()
    if (extra != -1) throw lengthError("Download exceeded the expected length")
}

internal class StagedDownload(
    private val path: Path,
    val size: Long,
) : Closeable {
    private var closed = false

    /**
     * Flushes, but never closes, the destination. A caller must also require its close to succeed.
     * A write, flush, or cancellation failure may already have left bytes in the destination.
     */
    @Synchronized
    fun copyTo(output: OutputStream, checkActive: () -> Unit = {}) {
        checkActive()
        if (closed) throw DownloadStagingException("The private download stage has been closed")
        val canCopy = privateIo("Could not inspect the private download stage") {
            Files.isRegularFile(path, NOFOLLOW_LINKS) && Files.size(path) == size
        }
        if (!canCopy) throw DownloadStagingException("Private download stage is missing or has changed size")
        val input = privateIo("Could not open the private download stage") {
            Files.newInputStream(path, NOFOLLOW_LINKS)
        }
        Closeable { privateIo("Could not close the private download stage") { input.close() } }.use {
            transferExactly(
                size, checkActive,
                read = { buffer, count ->
                    privateIo("Could not read the private download stage") { input.read(buffer, 0, count) }
                },
                write = { buffer, count -> output.write(buffer, 0, count) },
                lengthError = { DownloadStagingException(it) },
            )
        }
        checkActive()
        output.flush()
        checkActive()
    }

    @Synchronized
    override fun close() {
        if (closed) return
        closed = true
        releaseStage(path)
    }
}
