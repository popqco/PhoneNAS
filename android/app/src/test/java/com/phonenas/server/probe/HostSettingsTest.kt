package com.phonenas.server.probe

import java.io.File
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class HostSettingsTest {
    @get:Rule val temp = TemporaryFolder()

    private class MemoryPreferences : HostSettings.Preferences {
        var values: Map<String, Any> = emptyMap()
        var writable = true
        var initialized = false
        override fun hasInitializationMarker() = initialized
        override fun read(): Map<String, *> = values.toMap()
        override fun commit(values: Map<String, Any>): Boolean {
            if (!writable) return false
            initialized = true
            this.values = values.toMap()
            return true
        }
    }

    private class TestPlatform(base: File) : HostSettings.Platform {
        override val primaryRoot = File(base, "primary").apply { mkdir() }
        private val privateRoot = File(base, "app-private").apply { mkdir() }
        override val privateRoots = listOf(privateRoot)
        override val controlRoot = File(privateRoot, "control")
        override val tlsDirectory = File(privateRoot, "tls")
        var permitted = true
        var network = TailnetState.Snapshot()
        override fun hasManageExternalStorage() = permitted
        override fun tailnetState() = network
    }

    @Test fun storagePreflightAndLocalBuildWaitForPinnedVpnWithoutErasingThePinOrInitializingDuringRead() {
        val platform = TestPlatform(temp.root.canonicalFile)
        val preferences = MemoryPreferences()
        val settings = HostSettings(preferences, platform)
        val root = File(platform.primaryRoot, "Documents").apply { mkdir() }
        platform.network = TailnetState.Snapshot(true, "100.100.10.20", "tun0", "observed")
        settings.save(HostConfiguration(HostMode.CUSTOM,
            listOf(ShareConfiguration("docs", root.path)), "100.100.10.20"), true)
        platform.network = TailnetState.Snapshot()
        settings.preflightStorage()
        assertFalse("Read-only preflight must not initialize control", platform.controlRoot.exists())
        assertThrows(IllegalStateException::class.java) { settings.preflight() }
        val local = JSONObject(settings.buildLocalConfig("strong-password-not-stored", platform.controlRoot,
            File(temp.root, "unused-demo")))
        assertFalse(local.has("remote_listen"))
        assertEquals("127.0.0.1:18888", local.getString("listen"))
        assertEquals("100.100.10.20", settings.read().remoteIp)
        assertFalse(File(temp.root, "unused-demo").exists())
    }

    @Test fun aPreviouslySavedConfigurationDisappearingCannotSilentlyReturnToDemo() {
        val platform = TestPlatform(temp.root.canonicalFile)
        val preferences = MemoryPreferences()
        val settings = HostSettings(preferences, platform)
        settings.save(HostConfiguration(HostMode.CUSTOM), true)
        preferences.values = emptyMap()
        assertThrows(IllegalStateException::class.java) { settings.read() }
    }

    @Test fun serviceGateAndFailedCommitNeverReportASuccessfulSave() {
        val platform = TestPlatform(temp.root.canonicalFile)
        val preferences = MemoryPreferences()
        val settings = HostSettings(preferences, platform)
        assertThrows(IllegalStateException::class.java) {
            settings.save(HostConfiguration(HostMode.CUSTOM), stoppedConfirmed = false)
        }
        assertEquals(HostMode.DEMO, settings.read().mode)
        preferences.writable = false
        assertThrows(IllegalStateException::class.java) {
            settings.save(HostConfiguration(HostMode.CUSTOM), stoppedConfirmed = true)
        }
        assertThrows(IllegalStateException::class.java) { settings.read() }
        assertThrows(IllegalStateException::class.java) { settings.preflightStorage() }
    }

    @Test fun corruptedTypedSchemasCannotBecomeDefaultDemoOrBeSilentlyOverwritten() {
        val platform = TestPlatform(temp.root.canonicalFile)
        val preferences = MemoryPreferences()
        HostSettings(preferences, platform).save(HostConfiguration(HostMode.CUSTOM), true)
        val valid = preferences.values
        val damaged = listOf(valid - "mode", valid + ("mode" to "custom"),
            valid + ("share_count" to "0"), valid + ("schema_version" to 2),
            valid + ("unknown" to "ignored"), valid + ("remote_ip" to "100.100.10.20:18889"))
        damaged.forEach { map ->
            preferences.values = map
            val settings = HostSettings(preferences, platform)
            assertThrows(RuntimeException::class.java) { settings.read() }
            assertThrows(RuntimeException::class.java) { settings.save(HostConfiguration(), true) }
            assertEquals(map, preferences.values)
        }
    }

    @Test fun disabledAndRemovedSharesNeverRemoveFilesAndPermissionOrMissingRootsCannotBecomeDemo() {
        val platform = TestPlatform(temp.root.canonicalFile)
        val preferences = MemoryPreferences()
        val settings = HostSettings(preferences, platform)
        val root = File(platform.primaryRoot, "Documents").apply { mkdir() }
        val content = File(root, "keep.txt").apply { writeText("personal fixture survives") }
        val share = ShareConfiguration("docs", root.path)
        settings.save(HostConfiguration(HostMode.CUSTOM, listOf(share.copy(enabled = false))), true)
        assertThrows(IllegalStateException::class.java) { settings.preflightStorage() }
        settings.save(HostConfiguration(HostMode.CUSTOM), true)
        assertEquals("personal fixture survives", content.readText())
        assertThrows(IllegalStateException::class.java) { settings.preflightStorage() }
        settings.save(HostConfiguration(HostMode.CUSTOM, listOf(share)), true)
        platform.permitted = false
        assertThrows(IllegalStateException::class.java) {
            settings.buildLocalConfig("strong-password-not-stored", platform.controlRoot, File(temp.root, "demo"))
        }
        platform.permitted = true
        content.delete()
        root.delete()
        assertThrows(IllegalArgumentException::class.java) { settings.preflightStorage() }
        assertFalse("Missing custom roots must not be recreated", root.exists())
        assertFalse(platform.controlRoot.exists())
        assertEquals(HostMode.CUSTOM, settings.read().mode)
    }

    @Test fun demoKeepsExactIsolatedFixturesAndAllowsOnlyExplicitReadyRemotePinsWithoutPublicPermission() {
        val platform = TestPlatform(temp.root.canonicalFile)
        platform.permitted = false
        val preferences = MemoryPreferences()
        val settings = HostSettings(preferences, platform)
        val demo = File(platform.privateRoots.single(), "contract-test")
        settings.preflightStorage()
        assertFalse(demo.exists())
        assertFalse(platform.controlRoot.exists())
        val local = JSONObject(settings.buildConfig("strong-password-not-stored", platform.controlRoot, demo))
        assertFalse(local.has("remote_listen"))
        assertEquals(2, local.getJSONArray("shares").length())
        assertEquals("test", local.getJSONArray("shares").getJSONObject(0).getString("name"))
        assertFalse(local.getJSONArray("shares").getJSONObject(0).getBoolean("read_only"))
        assertEquals("PhoneNAS isolated test demo. No personal folders are exported.\n",
            File(demo, "test/readme.txt").readText())
        assertEquals("PhoneNAS isolated readonly demo. No personal folders are exported.\n",
            File(demo, "readonly/readme.txt").readText())
        assertEquals(true, local.getJSONArray("shares").getJSONObject(1).getBoolean("read_only"))
        platform.network = TailnetState.Snapshot(true, "100.100.10.20", "tun0", "observed")
        settings.save(HostConfiguration(remoteIp = "100.100.10.20"), true)
        val remote = JSONObject(settings.buildConfig("strong-password-not-stored", platform.controlRoot, demo))
        assertEquals("100.100.10.20:18889", remote.getString("remote_listen"))
        assertEquals(platform.tlsDirectory.path, remote.getString("tls_directory"))
        platform.network = TailnetState.Snapshot()
        assertThrows(IllegalStateException::class.java) { settings.buildConfig("strong-password-not-stored", platform.controlRoot, demo) }
        val waiting = JSONObject(settings.buildLocalConfig("strong-password-not-stored", platform.controlRoot, demo))
        assertFalse(waiting.has("remote_listen"))
        assertEquals("100.100.10.20", settings.read().remoteIp)
    }

    @Test fun trustedAppControlArgumentIsCanonicalizedWithoutRelaxingCustomSharePaths() {
        val platform = TestPlatform(temp.root.canonicalFile)
        val settings = HostSettings(MemoryPreferences(), platform)
        val data = File(platform.primaryRoot, "Documents").apply { mkdir() }
        settings.save(HostConfiguration(HostMode.CUSTOM, listOf(ShareConfiguration("docs", data.path))), true)
        val equivalentControl = File(platform.controlRoot.parentFile, "./control")
        val config = JSONObject(settings.buildLocalConfig("strong-password-not-stored", equivalentControl,
            File(temp.root, "unused-demo")))
        assertEquals(platform.controlRoot.canonicalPath, config.getString("control_root"))
        assertThrows(IllegalArgumentException::class.java) {
            settings.save(HostConfiguration(HostMode.CUSTOM, listOf(
                ShareConfiguration("docs", File(data.parentFile, "./Documents").path))), true)
        }
    }

    @Test fun anExistingOfflinePinDoesNotBlockDisablingRemovingOrMakingSharesReadOnly() {
        val platform = TestPlatform(temp.root.canonicalFile)
        val preferences = MemoryPreferences()
        val settings = HostSettings(preferences, platform)
        val root = File(platform.primaryRoot, "Documents").apply { mkdir() }
        val content = File(root, "keep.txt").apply { writeText("personal fixture survives") }
        val original = HostConfiguration(HostMode.CUSTOM, listOf(ShareConfiguration("docs", root.path)),
            "100.100.10.20")
        platform.network = TailnetState.Snapshot(true, "100.100.10.20", "tun0", "observed")
        settings.save(original, true)
        platform.network = TailnetState.Snapshot()
        val readOnly = original.copy(shares = original.shares.map { it.copy(readOnly = true) })
        settings.save(readOnly, true)
        assertEquals(readOnly, settings.read())
        val disabled = readOnly.copy(shares = readOnly.shares.map { it.copy(enabled = false) })
        settings.save(disabled, true)
        assertEquals(disabled, HostSettings(preferences, platform).read())
        val removed = disabled.copy(shares = emptyList())
        settings.save(removed, true)
        assertEquals(removed, settings.read())
        val demo = removed.copy(mode = HostMode.DEMO)
        settings.save(demo, true)
        assertEquals(demo, settings.read())
        assertEquals("personal fixture survives", content.readText())
        assertThrows(IllegalStateException::class.java) {
            settings.save(demo.copy(remoteIp = "100.100.10.21"), true)
        }
        assertEquals(demo, settings.read())
    }

    @Test fun persistedInitializationMarkerMakesEmptyCorruptPreferencesFailClosedAfterRecreation() {
        val platform = TestPlatform(temp.root.canonicalFile)
        val preferences = MemoryPreferences()
        val fresh = HostSettings(preferences, platform)
        assertEquals(HostMode.DEMO, fresh.read().mode)
        assertFalse("Diagnostic reads must not initialize storage", preferences.initialized)
        fresh.save(HostConfiguration(HostMode.CUSTOM), true)
        preferences.values = emptyMap() // Android may present malformed preferences XML as an empty map.
        val recreated = HostSettings(preferences, platform)
        assertThrows(IllegalStateException::class.java) { recreated.read() }
        assertThrows(IllegalStateException::class.java) { recreated.preflightStorage() }
        assertThrows(IllegalStateException::class.java) { recreated.save(HostConfiguration(), true) }
        assertFalse(platform.controlRoot.exists())
    }

    @Test fun explicitCustomSettingsRoundTripAndBuildOnlyEnabledPublicRoots() {
        val platform = TestPlatform(temp.root.canonicalFile)
        val preferences = MemoryPreferences()
        val settings = HostSettings(preferences, platform)
        assertEquals(HostMode.DEMO, settings.read().mode)
        val photos = File(platform.primaryRoot, "Pictures").apply { mkdir() }
        val docs = File(platform.primaryRoot, "Documents").apply { mkdir() }
        val configuration = HostConfiguration(HostMode.CUSTOM, listOf(
            ShareConfiguration("photos", photos.path),
            ShareConfiguration("archive", docs.path, readOnly = true, enabled = false)))
        settings.save(configuration, stoppedConfirmed = true)
        assertEquals(configuration, HostSettings(preferences, platform).read())
        platform.controlRoot.mkdir()
        settings.preflight()
        val config = JSONObject(settings.buildConfig("strong-password-not-stored", platform.controlRoot,
            File(temp.root, "unused-demo")))
        assertEquals("127.0.0.1:18888", config.getString("listen"))
        assertFalse(config.has("remote_listen"))
        assertEquals("phonenas", config.getString("username"))
        assertEquals("photos", config.getJSONArray("shares").getJSONObject(0).getString("name"))
        assertEquals(1, config.getJSONArray("shares").length())
        assertFalse(preferences.values.values.contains("strong-password-not-stored"))
    }
}
