package com.phonenas.server.probe

import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.nio.file.Files

/** Single-process file seam. Pass Context.filesDir, never a share or a user-selected directory. */
internal class JournalStore(privateFilesDir: File, val maxBytes: Int = MAX_BYTES) {
    private val directory = File(privateFilesDir, "reliability-journal-v1")
    private var failed: ReliabilityJournal.Error? = null

    init { require(maxBytes in 1_024..MAX_BYTES) }

    internal data class Segment(val number: Int, val file: File, val bytes: ByteArray)
    internal class Fault(val code: ReliabilityJournal.Error) : Exception()

    // All facade instances in this app process share the transaction lock. No background work.
    internal fun <T> access(block: () -> T): T = synchronized(monitor) { block() }

    internal fun read(): List<Segment> {
        failed?.let { throw Fault(it) }
        try {
            if (!directory.exists()) return emptyList()
            if (!directory.isDirectory || Files.isSymbolicLink(directory.toPath())) bad()
            val files = directory.listFiles() ?: throw Fault(ReliabilityJournal.Error.IO)
            if (files.any { !it.isFile || Files.isSymbolicLink(it.toPath()) }) bad()
            if (files.sumOf { it.length() } > maxBytes) throw Fault(ReliabilityJournal.Error.OVERFLOW)
            val fault = files.firstOrNull { it.name == "fault" }
            if (fault != null) {
                val code = ReliabilityJournal.Error.entries.firstOrNull { it.name == readBounded(fault).toString(Charsets.US_ASCII).trim() }
                throw Fault(code ?: ReliabilityJournal.Error.CORRUPT)
            }
            if (files.any { it.name == "pending" }) throw Fault(ReliabilityJournal.Error.WRITE_INTERRUPTED)
            if (files.size > MAX_SEGMENTS || files.any { !it.name.matches(Regex("run-[1-3]\\.pnj")) }) bad()
            return files.map { Segment(it.name[4].digitToInt(), it, readBounded(it)) }.sortedBy { it.number }
        } catch (fault: Fault) {
            failed = fault.code
            throw fault
        } catch (_: Exception) {
            failed = ReliabilityJournal.Error.IO
            throw Fault(ReliabilityJournal.Error.IO)
        }
    }

    internal fun append(number: Int, line: ByteArray, existing: List<Segment>) {
        try {
            if (existing.sumOf { it.bytes.size.toLong() } + line.size > maxBytes - RESERVED_BYTES) {
                fail(ReliabilityJournal.Error.OVERFLOW)
            }
            if (!directory.exists() && !directory.mkdirs()) throw Fault(ReliabilityJournal.Error.IO)
            if (!directory.isDirectory || Files.isSymbolicLink(directory.toPath())) bad()
            val target = File(directory, "run-$number.pnj")
            if (target.exists() && Files.isSymbolicLink(target.toPath())) bad()
            // A crash or failed write leaves a durable marker. Never truncate/repair a torn tail.
            val pending = File(directory, "pending")
            if (!pending.createNewFile()) throw Fault(ReliabilityJournal.Error.WRITE_INTERRUPTED)
            FileOutputStream(pending).use { it.write(1); it.fd.sync() }
            FileOutputStream(target, true).use { it.write(line); it.fd.sync() }
            if (!pending.delete()) throw Fault(ReliabilityJournal.Error.IO)
        } catch (fault: Fault) {
            fail(fault.code)
        } catch (_: Exception) {
            fail(ReliabilityJournal.Error.IO)
        }
    }

    internal fun fail(code: ReliabilityJournal.Error): Nothing {
        failed = code
        // If storage itself is unavailable, the caller still receives a latched non-green result.
        try {
            if ((directory.exists() || directory.mkdirs()) && directory.isDirectory &&
                !Files.isSymbolicLink(directory.toPath())) {
                val marker = File(directory, "fault")
                val bytes = code.name.toByteArray(Charsets.US_ASCII)
                val total = directory.listFiles()?.sumOf { it.length() } ?: maxBytes.toLong()
                if (!marker.exists() && total + bytes.size <= maxBytes) FileOutputStream(marker).use {
                    it.write(bytes); it.fd.sync()
                }
            }
        } catch (_: Exception) { /* Error remains explicit; never include exception text or paths. */ }
        throw Fault(code)
    }

    private fun bad(): Nothing = throw Fault(ReliabilityJournal.Error.CORRUPT)

    private fun readBounded(file: File): ByteArray = FileInputStream(file).use { input ->
        val output = ByteArrayOutputStream()
        val buffer = ByteArray(8_192)
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            if (output.size() + count > maxBytes) throw Fault(ReliabilityJournal.Error.OVERFLOW)
            output.write(buffer, 0, count)
        }
        output.toByteArray()
    }

    companion object {
        const val MAX_BYTES = 2 * 1_024 * 1_024
        internal const val MAX_SEGMENTS = 3
        private const val RESERVED_BYTES = 128
        private val monitor = Any()
    }
}
