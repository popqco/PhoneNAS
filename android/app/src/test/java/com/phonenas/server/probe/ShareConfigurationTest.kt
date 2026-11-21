package com.phonenas.server.probe

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ShareConfigurationTest {
    @get:Rule val temp = TemporaryFolder()

    @Test fun pickerResolvesOnlyPrimaryRelativeDirectoryIdsWithoutPathRewriting() {
        val primary = temp.newFolder("primary").canonicalFile
        val chosen = File(primary, "Documents/Family photos").apply { mkdirs() }
        assertEquals(chosen, ShareRules.resolvePrimaryTree("primary:Documents/Family photos", primary))
        listOf("primary:", "primary:/Documents", "primary:../Documents", "primary:Documents/../Pictures",
            "primary:Documents/./Pictures", "primary:Documents//Pictures", "primary:Documents/",
            "primary:Documents\\Pictures", "primary:C:/Documents", "primary:Documents\u0000",
            "ABCD-1234:Documents", "home:Documents", "Primary:Documents", "Documents").forEach {
            assertThrows("Rejected tree ID: $it", IllegalArgumentException::class.java) {
                ShareRules.resolvePrimaryTree(it, primary)
            }
        }
    }

    @Test fun multipleIndependentExistingRootsAreAcceptedButAliasIdentityIsCaseInsensitive() {
        val primary = temp.newFolder("primary").canonicalFile
        val photos = File(primary, "Pictures").apply { mkdir() }
        val docs = File(primary, "Documents").apply { mkdir() }
        val shares = listOf(ShareConfiguration("photos", photos.path),
            ShareConfiguration("docs", docs.path, readOnly = true))
        ShareRules.validateShares(shares, primary)
        assertThrows(IllegalArgumentException::class.java) {
            ShareRules.validateShares(shares + ShareConfiguration("PHOTOS", docs.path, enabled = false), primary)
        }
    }

    @Test fun rootsMustBeExistingAccessibleDirectoriesStrictlyInsidePublicPrimaryStorage() {
        val primary = temp.newFolder("primary").canonicalFile
        val privateRoot = File(primary, "private-fixture").apply { mkdir() }
        val privateChild = File(privateRoot, "nested").apply { mkdir() }
        val android = File(primary, "Android").apply { mkdir() }
        val androidChild = File(android, "media").apply { mkdir() }
        val outside = temp.newFolder("secondary").canonicalFile
        val missing = File(primary, "not-created")
        val notDirectory = File(primary, "plain-file").apply { writeText("not a directory") }
        for (root in listOf(primary.path, android.path, androidChild.path, privateRoot.path,
            privateChild.path, outside.path, missing.path, notDirectory.path, "Pictures")) {
            assertThrows("Rejected root: $root", IllegalArgumentException::class.java) {
                ShareRules.validateShares(listOf(ShareConfiguration("data", root)), primary, listOf(privateRoot))
            }
        }
        assertFalse("Validation must not create a replacement for a missing root", missing.exists())
    }

    @Test fun duplicateNestedAndNonCanonicalRootsCannotBecomeIndependentExports() {
        val primary = temp.newFolder("primary").canonicalFile
        val data = File(primary, "Data").apply { mkdir() }
        val child = File(data, "Child").apply { mkdir() }
        for (second in listOf(data.path, child.path, File(data, "../Data").path)) {
            assertThrows(IllegalArgumentException::class.java) {
                ShareRules.validateShares(listOf(ShareConfiguration("one", data.path),
                    ShareConfiguration("two", second)), primary)
            }
        }
        assertThrows(IllegalArgumentException::class.java) {
            ShareRules.validateShares(listOf(ShareConfiguration("child", child.path),
                ShareConfiguration("parent", data.path)), primary)
        }
    }

    @Test fun aliasesArePortableAsciiNamesRatherThanWebdavPaths() {
        listOf("photos", "Family_2026-11", "A", "a".repeat(40)).forEach {
            ShareRules.requireValidAlias(it)
        }
        listOf("", "a".repeat(41), ".phonenas", ".PHONENAS", ".phonenas-temp-x",
            ".", "..", "a/b", "a\\b", "space name", "中文", "trailing\n", "\u0000").forEach {
            assertThrows("Rejected alias: $it", IllegalArgumentException::class.java) {
                ShareRules.requireValidAlias(it)
            }
        }
    }
}
