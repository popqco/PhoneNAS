package com.phonenas.client

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class CloudFilesTest {
    private val exchangePath = DavPath.Root.visibleChild("exchange", true)!!
    private val exchange = ExchangeShare.of(exchangePath)

    @Test fun `failed refresh keeps visible files but does not allow choosing an upload`() {
        val file = DavEntry(exchangePath.visibleChild("sample.txt", false)!!, 12)
        val listing = CloudDirectory(exchangePath, listOf(file))
        val stale = listing.refreshFailed(exchangePath)
        assertTrue(listing.canUpload(exchange))
        assertTrue(stale.needsRefresh)
        assertEquals(listOf(file), stale.entries)
        assertFalse(stale.canUpload(exchange))
        assertFalse(CloudDirectory(DavPath.Root, emptyList()).canUpload(exchange))
    }

    @Test fun `a successful or unknown upload never leaves an obsolete empty view actionable`() {
        val before = CloudDirectory(exchangePath, emptyList())
        for (outcome in listOf(UploadState.CREATED, UploadState.UNKNOWN)) {
            val updated = before.afterUpload(CloudUploadReport(exchangePath, listOf(UploadResult("new.txt", outcome)), null))
            assertTrue(updated.needsRefresh)
            assertFalse(updated.canUpload(exchange))
        }
        val newEntry = DavEntry(exchangePath.visibleChild("new.txt", false)!!, 3)
        val fresh = CloudDirectory(exchangePath, listOf(newEntry))
        val updated = before.afterUpload(CloudUploadReport(exchangePath, listOf(UploadResult("new.txt", UploadState.CREATED)), fresh))
        assertEquals(listOf(newEntry), updated.entries)
        assertTrue(updated.canUpload(exchange))
    }

    @Test fun `failed navigation leaves the visible directory distinct from the target`() {
        val child = exchangePath.visibleChild("child", true)!!
        val visible = CloudDirectory(exchangePath, listOf(DavEntry(child, null)))
        val remaining = visible.refreshFailed(child)
        assertEquals(exchangePath, remaining.path)
        assertFalse(remaining.needsRefresh)
    }

    @Test fun `the file list shows folders first with names ordered within each group`() {
        val root = DavPath.Root
        fun item(name: String, folder: Boolean) = DavEntry(root.visibleChild(name, folder)!!, null)
        val listing = CloudDirectory(root, listOf(item("b.txt", false), item("z", true),
            item("a.txt", false), item("y", true)))
        assertEquals(listOf("y", "z", "a.txt", "b.txt"), listing.orderedEntries().map { it.path.name })
    }

    @Test fun `an old HTTP address tells the user why the pairing cannot start`() {
        for (input in listOf("http://100.101.102.103:8888/", "HTTP://100.101.102.103:18889/", "100.101.102.103:8888")) {
            val failure = assertThrows(CloudFailure::class.java) { CloudEndpoint.parse(input) }
            assertEquals(CloudIssue.LEGACY_HTTP, failure.issue)
        }
        assertEquals("100.101.102.103", CloudEndpoint.parse("https://100.101.102.103:18889/").host)
    }
}
