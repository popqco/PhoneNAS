package com.phonenas.client

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit

class CloudUploadSourceTest {
    @get:Rule val temp = TemporaryFolder()

    @Test fun `unknown length is measured and can be read until its stage is closed`() {
        val directory = temp.root.resolve("uploads")
        val bytes = ByteArray(150_000) { (it % 251).toByte() }
        val progress = mutableListOf<Long>()
        val operation = CloudOperation()
        val spool = UploadSpool(directory, limit = 150_000)

        spool.receive(bytes.inputStream(), operation) { progress += it }.use { content ->
            assertEquals(150_000L, content.length)
            content.withInput(operation) { input -> assertArrayEquals(bytes, input.readBytes()) }
        }

        assertEquals(150_000L, progress.last())
        assertTrue(progress.zipWithNext().all { (previous, next) -> previous < next })
        assertTrue(directory.list().orEmpty().isEmpty())
    }

    @Test fun `a source one byte beyond the cap leaves no partial stage`() {
        val directory = temp.root.resolve("uploads")
        val spool = UploadSpool(directory, limit = 100_000)
        val progress = mutableListOf<Long>()
        val oversized = ByteArray(100_001).inputStream()

        val failure = assertThrows(CloudFailure::class.java) {
            spool.receive(oversized, CloudOperation()) { progress += it }.close()
        }

        assertEquals(CloudIssue.UPLOAD_CAPACITY, failure.issue)
        assertTrue(progress.isNotEmpty() && progress.all { it in 1..100_000L })
        assertTrue(directory.list().orEmpty().isEmpty())
        spool.receive(byteArrayOf().inputStream(), CloudOperation()) {}.use { content ->
            assertEquals(0L, content.length)
            content.withInput(CloudOperation()) { assertEquals(-1, it.read()) }
        }
    }

    @Test fun `abandoned cleanup only removes matching inactive regular files`() {
        val directory = temp.newFolder("uploads")
        val cleaner = UploadSpool(directory.resolve("."))
        val abandoned = directory.resolve("upload-aaaabbbb-cccc-4ddd-8eee-ffff00001111.part").apply { writeText("old") }
        val unrelated = directory.resolve("upload-not-owned.part").apply { writeText("keep") }
        val nested = directory.resolve("upload-ddddbbbb-cccc-4ddd-8eee-ffff00001111.part").apply { mkdir() }
            .resolve(abandoned.name).apply { writeText("nested") }
        val bytes = ByteArray(120_000) { (it % 7).toByte() }
        val operation = CloudOperation()
        val content = UploadSpool(directory).receive(bytes.inputStream(), operation) { cleaner.cleanAbandoned() }

        content.use { stage ->
            cleaner.cleanAbandoned()
            assertFalse(abandoned.exists())
            val sinkFailure = IOException("synthetic sink failure")
            assertSame(sinkFailure, assertThrows(IOException::class.java) {
                stage.withInput(operation) { throw sinkFailure }
            })
            stage.withInput(operation) { input -> assertArrayEquals(bytes, input.readBytes()) }
        }
        assertThrows(CloudFailure::class.java) { content.withInput(operation) {} }
        assertEquals("keep", unrelated.readText())
        assertEquals("nested", nested.readText())
        assertEquals(setOf(unrelated.name, nested.parentFile!!.name), directory.list().orEmpty().toSet())
    }

    @Test fun `cancellation closes a blocked input off thread alongside an independent hook`() {
        val operation = CloudOperation()
        val enteredRead = CountDownLatch(1)
        val startedClose = CountDownLatch(1)
        val releaseClose = CountDownLatch(1)
        val outerCancelled = CountDownLatch(1)
        val input = object : InputStream() {
            override fun read(): Int {
                enteredRead.countDown()
                check(startedClose.await(10, TimeUnit.SECONDS))
                throw IOException("synthetic interrupted read")
            }

            override fun close() {
                startedClose.countDown()
                check(releaseClose.await(10, TimeUnit.SECONDS))
            }
        }
        val upload = FutureTask {
            assertThrows(CloudFailure::class.java) {
                operation.duringCancellation(outerCancelled::countDown) {
                    withUploadInput(input, operation, CloudIssue.UPLOAD_SOURCE) { it.read() }
                }
            }.issue
        }
        Thread(upload).apply { isDaemon = true }.start()
        try {
            assertTrue(enteredRead.await(3, TimeUnit.SECONDS))
            val cancel = FutureTask { operation.cancel() }
            Thread(cancel).apply { isDaemon = true }.start()
            cancel.get(3, TimeUnit.SECONDS)
            assertTrue(outerCancelled.await(3, TimeUnit.SECONDS))
            assertTrue(startedClose.await(3, TimeUnit.SECONDS))
            assertEquals(CloudIssue.CANCELLED, upload.get(3, TimeUnit.SECONDS))
        } finally {
            releaseClose.countDown()
        }
    }

    @Test fun `cancellation during preparation preserves the issue and removes the stage`() {
        val directory = temp.root.resolve("uploads")
        val operation = CloudOperation()
        val failure = assertThrows(CloudFailure::class.java) {
            UploadSpool(directory).receive(ByteArray(100_000).inputStream(), operation) {
                operation.cancel(CloudIssue.VPN)
            }.close()
        }

        assertEquals(CloudIssue.VPN, failure.issue)
        assertTrue(directory.list().orEmpty().isEmpty())
    }
}
