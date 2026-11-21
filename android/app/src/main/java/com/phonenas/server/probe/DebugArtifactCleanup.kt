package com.phonenas.server.probe

import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS

/** Remove only the fixed temporary exports created by this package's DEBUG controller. */
internal object DebugArtifactCleanup {
    fun remove(privateFilesDirectory: File): Boolean {
        var clean = true
        for (name in listOf("test-access.json", "start-evidence.txt")) {
            val file = File(privateFilesDirectory, name)
            try {
                if (!Files.exists(file.toPath(), NOFOLLOW_LINKS)) continue
                if (!Files.isRegularFile(file.toPath(), NOFOLLOW_LINKS) || Files.isSymbolicLink(file.toPath())) {
                    clean = false
                    continue
                }
                Files.delete(file.toPath())
            } catch (_: Exception) { clean = false }
        }
        return clean
    }
}
