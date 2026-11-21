package com.phonenas.client

import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.OutputStream
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Test

class CloudDownloadsTest {
    @Test
    fun `save copies and syncs a new pending download before publishing the store name`() {
        val store = TestDownloads()
        val bytes = byteArrayOf(1, 2, 3)

        val result = savePendingDownload(
            "notes.txt", 3L,
            verifyActive = { if ("close" in store.events) store.events.add("verify") },
            create = store::create,
        ) { output ->
            assertFalse(store.published.containsKey("notes (1).txt"))
            store.events.add("copy")
            output.write(bytes)
        }

        assertEquals("notes (1).txt", result.name)
        assertArrayEquals(bytes, store.published.getValue(result.name))
        assertArrayEquals(byteArrayOf(7), store.published.getValue("notes.txt"))
        assertEquals(listOf("create", "open", "copy", "flush", "sync", "close", "verify", "publish"), store.events)
    }

    @Test
    fun `copy failure closes the file and discards only the new pending row`() {
        val store = TestDownloads()
        val failure = CloudFailure(CloudIssue.UNREACHABLE)
        val reported = assertThrows(CloudFailure::class.java) {
            savePendingDownload("notes.txt", 2L, {}, store::create) { output ->
                output.write(1)
                throw failure
            }
        }

        assertSame(failure, reported)
        assertEquals(setOf("notes.txt"), store.published.keys)
        assertArrayEquals(byteArrayOf(7), store.published.getValue("notes.txt"))
        assertEquals(listOf("create", "open", "close", "discarded", "discard"), store.events)
    }

    @Test
    fun `failed writes syncs or closes are local failures and do not publish`() {
        for (step in listOf("write", "sync", "close")) {
            val store = TestDownloads(failAt = step)
            val reported = assertThrows(CloudFailure::class.java) {
                savePendingDownload("notes.txt", 1L, {}, store::create) { it.write(1) }
            }

            assertEquals(CloudIssue.DOWNLOAD_SAVE, reported.issue)
            assertNull(reported.cause)
            assertFalse("publish" in store.events)
            assertEquals(listOf("close", "discarded", "discard"), store.events.takeLast(3))
            assertEquals(setOf("notes.txt"), store.published.keys)
        }
    }

    @Test
    fun `upstream failure survives close failure without attaching provider details`() {
        val store = TestDownloads(failAt = "close")
        val failure = CloudFailure(CloudIssue.DOWNLOAD_LENGTH)
        val reported = assertThrows(CloudFailure::class.java) {
            savePendingDownload("notes.txt", 2L, {}, store::create) { throw failure }
        }
        assertSame(failure, reported)
        assertEquals(0, reported.suppressed.size)
        assertEquals(setOf("notes.txt"), store.published.keys)
    }

    @Test
    fun `unknown length cannot create a pending row`() {
        val store = TestDownloads()
        val reported = assertThrows(CloudFailure::class.java) {
            savePendingDownload("notes.txt", -1L, {}, store::create) { error("copy must not run") }
        }

        assertEquals(CloudIssue.DOWNLOAD_LENGTH, reported.issue)
        assertEquals(emptyList<String>(), store.events)
    }

    @Test
    fun `short and oversized output never publish and an empty download can publish`() {
        for (length in listOf(0L, 2L)) {
            val store = TestDownloads()
            val reported = assertThrows(CloudFailure::class.java) {
                savePendingDownload("notes.txt", length, {}, store::create) { it.write(1) }
            }
            assertEquals(CloudIssue.DOWNLOAD_SAVE, reported.issue)
            assertEquals(setOf("notes.txt"), store.published.keys)
            assertEquals("discard", store.events.last())
        }
        val store = TestDownloads()
        val saved = savePendingDownload("empty", 0L, {}, store::create) { }
        assertArrayEquals(byteArrayOf(), store.published.getValue(saved.name))
    }

    @Test
    fun `route lost after the descriptor closes prevents publication`() {
        val store = TestDownloads()
        val failure = CloudFailure(CloudIssue.VPN)
        val reported = assertThrows(CloudFailure::class.java) {
            savePendingDownload("notes.txt", 1L,
                verifyActive = { if ("close" in store.events) throw failure },
                create = store::create,
            ) { it.write(1) }
        }
        assertSame(failure, reported)
        assertEquals(listOf("sync", "close", "discarded", "discard"), store.events.takeLast(4))
        assertEquals(setOf("notes.txt"), store.published.keys)
    }

    @Test
    fun `readback confirms a committed save even if the reply is lost and cancellation follows`() {
        val store = TestDownloads(failAt = "publish-reply")
        val saved = savePendingDownload("notes.txt", 1L,
            verifyActive = { if ("publish" in store.events) throw CloudFailure(CloudIssue.CANCELLED) },
            create = store::create,
        ) { it.write(1) }

        assertEquals("notes (1).txt", saved.name)
        assertFalse("discard" in store.events)
        assertArrayEquals(byteArrayOf(1), store.published.getValue(saved.name))
    }

    @Test
    fun `a refused publication cleans only the unchanged pending row`() {
        val store = TestDownloads(failAt = "publish-refused")
        val reported = assertThrows(CloudFailure::class.java) {
            savePendingDownload("notes.txt", 1L, {}, store::create) { it.write(1) }
        }

        assertEquals(CloudIssue.DOWNLOAD_SAVE, reported.issue)
        assertEquals(listOf("publish", "discarded", "discard"), store.events.takeLast(3))
        assertArrayEquals(byteArrayOf(7), store.published.getValue("notes.txt"))
    }

    @Test
    fun `failed confirmation must not delete a published row or an unrecognized replacement`() {
        for (step in listOf("readback", "publish-and-readback", "replaced")) {
            val store = TestDownloads(failAt = step)
            val reported = assertThrows(CloudFailure::class.java) {
                savePendingDownload("notes.txt", 1L, {}, store::create) { it.write(1) }
            }

            assertEquals(CloudIssue.DOWNLOAD_SAVE_UNKNOWN, reported.issue)
            assertFalse("discarded" in store.events)
            assertArrayEquals(byteArrayOf(7), store.published.getValue("notes.txt"))
            if (step != "replaced") assertArrayEquals(byteArrayOf(1), store.published.getValue("notes (1).txt"))
        }
    }

    private class TestDownloads(private val failAt: String? = null) : PendingCloudDownload {
        val events = mutableListOf<String>()
        val published = mutableMapOf("notes.txt" to byteArrayOf(7))
        private val data = ByteArrayOutputStream()
        private var name: String? = null
        private var ownedPending = false

        fun create(): PendingCloudDownload {
            events.add("create")
            ownedPending = true
            return this
        }

        override fun open(): PendingDownloadFile {
            events.add("open")
            return object : PendingDownloadFile {
                override val output: OutputStream = object : OutputStream() {
                    override fun write(value: Int) {
                        if (failAt == "write") throw IOException("private provider path")
                        data.write(value)
                    }
                    override fun flush() { events.add("flush") }
                }
                override val size: Long get() = data.size().toLong()
                override fun sync() {
                    events.add("sync")
                    if (failAt == "sync") throw IOException("private provider path")
                }
                override fun close() {
                    events.add("close")
                    if (failAt == "close") throw IOException("private provider path")
                }
            }
        }

        override fun publish(name: String): Int {
            events.add("publish")
            if (failAt == "publish-refused") return 0
            ownedPending = false
            if (failAt == "replaced") return -1
            val finalName = if (name == "notes.txt") "notes (1).txt" else name
            published[finalName] = data.toByteArray()
            this.name = finalName
            if (failAt == "publish-reply" || failAt == "publish-and-readback") throw IOException()
            return 1
        }

        override fun publishedName(): String? {
            if (failAt == "readback" || failAt == "publish-and-readback") throw IOException()
            return name
        }

        override fun discardIfPending() {
            if (ownedPending) {
                ownedPending = false
                events.add("discarded")
            }
            events.add("discard")
        }
    }
}
