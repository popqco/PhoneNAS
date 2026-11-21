package com.phonenas.client

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.CancellationException
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class DownloadStagingTest {
    @get:Rule
    val temp = TemporaryFolder()

    @Test
    fun `an exact download can be copied and leaves both caller streams open`() {
        val staging = DownloadStaging(temp.root.resolve("downloads"))
        val bytes = ByteArray(150_000) { (it % 251).toByte() }
        val input = object : ByteArrayInputStream(bytes) {
            var closed = false
            override fun close() { closed = true }
        }
        val output = object : ByteArrayOutputStream() {
            var closed = false
            override fun close() { closed = true }
        }

        staging.receive(input, 150_000, expectedFromListing = 150_000).use { stage ->
            assertEquals(150_000L, stage.size)
            stage.copyTo(output)
        }

        assertArrayEquals(bytes, output.toByteArray())
        assertFalse(input.closed)
        assertFalse(output.closed)
    }

    @Test
    fun `unknown or inconsistent lengths are rejected before receiving bytes`() {
        val directory = temp.root.resolve("downloads")
        val staging = DownloadStaging(directory)
        val unreadable = object : InputStream() {
            override fun read(): Int = error("length must be validated before reading")
        }
        for ((header, listing) in listOf(-1L to null, 8L to 9L, 0L to -1L)) {
            assertThrows(DownloadLengthException::class.java) {
                staging.receive(unreadable, header, listing).close()
            }
        }
        assertFalse(directory.exists())
    }

    @Test
    fun `a length above 512 MiB is refused before reading or creating a stage`() {
        val directory = temp.root.resolve("downloads")
        val unreadable = object : InputStream() {
            override fun read(): Int = error("oversize rejected before reading")
        }
        assertThrows(DownloadCapacityException::class.java) {
            DownloadStaging(directory).receive(unreadable, 536_870_913).close()
        }
        assertFalse(directory.exists())
    }

    @Test
    fun `short and excess bodies never produce a stage or leave partial files`() {
        val directory = temp.root.resolve("downloads")
        val staging = DownloadStaging(directory)
        for ((bodySize, declaredSize) in listOf(90_000 to 100_000, 100_001 to 100_000, 1 to 0)) {
            assertThrows(DownloadLengthException::class.java) {
                staging.receive(ByteArrayInputStream(ByteArray(bodySize)), declaredSize.toLong()).close()
            }
            assertEquals(emptyList<String>(), directory.list().orEmpty().toList())
        }
    }

    @Test
    fun `an empty body can be staged and copied`() {
        val staging = DownloadStaging(temp.root.resolve("downloads"))
        val output = ByteArrayOutputStream()
        staging.receive(ByteArrayInputStream(byteArrayOf()), 0).use { stage ->
            assertEquals(0L, stage.size)
            stage.copyTo(output)
        }
        assertArrayEquals(byteArrayOf(), output.toByteArray())
    }

    @Test
    fun `cancellation after progress deletes the partial file and permits retry`() {
        val directory = temp.root.resolve("downloads")
        val staging = DownloadStaging(directory)
        val cancellation = CancellationException("cancelled")
        val bytes = ByteArray(160_000)
        var lastProgress = 0L
        val error = assertThrows(CancellationException::class.java) {
            staging.receive(
                ByteArrayInputStream(bytes), 160_000,
                checkActive = { if (lastProgress > 0) throw cancellation },
                progress = { received, total ->
                    assertEquals(160_000L, total)
                    assertTrue(received in (lastProgress + 1) until total)
                    lastProgress = received
                },
            ).close()
        }
        assertSame(cancellation, error)
        assertEquals(emptyList<String>(), directory.list().orEmpty().toList())
        staging.receive(ByteArrayInputStream(bytes), 160_000).use { assertEquals(160_000L, it.size) }
    }

    @Test
    fun `cleanup removes abandoned stages but protects current and unrelated files`() {
        val directory = temp.newFolder("downloads")
        val abandoned = directory.resolve("download-06fbfc5d-5115-431d-a64f-f1c22c6000cb.part").apply { writeText("old") }
        val unrelated = directory.resolve("download-not-owned.part").apply { writeText("keep") }
        val nested = directory.resolve("download-9df2055f-99f0-466d-976b-3966c90bf1f0.part").apply { mkdir() }
            .resolve("download-06fbfc5d-5115-431d-a64f-f1c22c6000cb.part").apply { writeText("nested") }
        val staging = DownloadStaging(directory)
        val cleaner = DownloadStaging(directory.resolve("."))
        val bytes = ByteArray(120_000) { (it % 7).toByte() }
        val output = ByteArrayOutputStream()

        staging.receive(ByteArrayInputStream(bytes), 120_000, progress = { _, _ -> cleaner.cleanAbandoned() })
            .use { stage ->
                cleaner.cleanAbandoned()
                assertThrows(IOException::class.java) {
                    cleaner.receive(ByteArrayInputStream(byteArrayOf()), 0).close()
                }
                stage.copyTo(output)
            }

        assertArrayEquals(bytes, output.toByteArray())
        assertFalse(abandoned.exists())
        assertEquals("keep", unrelated.readText())
        assertEquals("nested", nested.readText())
        assertEquals(setOf(unrelated.name, nested.parentFile!!.name), directory.list().orEmpty().toSet())
        cleaner.receive(ByteArrayInputStream(byteArrayOf()), 0).close()
    }

    @Test
    fun `provider flush failure propagates without closing the caller output`() {
        val directory = temp.root.resolve("downloads")
        val staging = DownloadStaging(directory)
        val failure = IOException("provider flush failed")
        val output = object : ByteArrayOutputStream() {
            override fun flush() { throw failure }
            override fun close() { error("Only the caller closes the output") }
        }
        val error = assertThrows(IOException::class.java) {
            staging.receive(ByteArrayInputStream(byteArrayOf(1)), 1).use { it.copyTo(output) }
        }
        assertSame(failure, error)
        assertEquals(emptyList<String>(), directory.list().orEmpty().toList())
    }

    @Test
    fun `a discarded stage cannot reopen a file subsequently reusing its path`() {
        val directory = temp.root.resolve("downloads")
        val staging = DownloadStaging(directory)
        val stage = staging.receive(ByteArrayInputStream(byteArrayOf(1)), 1)
        val formerFile = directory.listFiles()!!.single()
        stage.close()
        formerFile.writeBytes(byteArrayOf(2))
        val output = ByteArrayOutputStream()

        assertThrows(DownloadStagingException::class.java) { stage.copyTo(output) }
        assertArrayEquals(byteArrayOf(), output.toByteArray())
        stage.close()
        assertArrayEquals(byteArrayOf(2), formerFile.readBytes())
    }

    @Test
    fun `discard never deletes a directory that replaced the staged file`() {
        val directory = temp.root.resolve("downloads")
        val stage = DownloadStaging(directory).receive(ByteArrayInputStream(byteArrayOf()), 0)
        val replacement = directory.listFiles()!!.single()
        assertTrue(replacement.delete())
        assertTrue(replacement.mkdir())

        assertThrows(DownloadStagingException::class.java) { stage.close() }
        assertTrue(replacement.isDirectory)
    }
}
