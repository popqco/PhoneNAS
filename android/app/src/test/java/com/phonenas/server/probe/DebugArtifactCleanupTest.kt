package com.phonenas.server.probe

import java.io.File
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class DebugArtifactCleanupTest {
    @get:Rule val temporary = TemporaryFolder()

    @Test fun candidateRemovesOnlyKnownTemporaryDebugExports() {
        val root = temporary.newFolder()
        File(root, "test-access.json").writeText("synthetic temporary credential")
        File(root, "start-evidence.txt").writeText("synthetic numeric diagnostic")
        val unrelated = File(root, "keep.bin").apply { writeText("keep") }
        assertTrue(DebugArtifactCleanup.remove(root))
        assertFalse(File(root, "test-access.json").exists())
        assertFalse(File(root, "start-evidence.txt").exists())
        assertEquals("keep", unrelated.readText())
        assertTrue(DebugArtifactCleanup.remove(root))
    }
    @Test fun anUnexpectedDirectoryIsNotRecursivelyDeletedOrReportedClean() {
        val root = temporary.newFolder()
        val directory = File(root, "test-access.json").apply { mkdir() }
        val witness = File(directory, "keep.bin").apply { writeText("keep") }
        assertFalse(DebugArtifactCleanup.remove(root))
        assertEquals("keep", witness.readText())
    }
}
