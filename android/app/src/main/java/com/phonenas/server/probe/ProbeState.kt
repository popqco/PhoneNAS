package com.phonenas.server.probe

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.json.JSONObject
import java.io.File
import java.security.KeyStore
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

internal const val LISTEN = "127.0.0.1:18888"
internal const val USERNAME = "phonenas"
internal data class EngineStats(val running: Boolean = false, val address: String = "",
    val requests: Long = 0, val received: Long = 0, val sent: Long = 0, val error: String = "",
    val remoteAddress: String = "", val certificateSha256: String = "")
internal data class RuntimeView(val phase: String = "已停止", val stats: EngineStats = EngineStats(),
    val error: String = "", val sampledAt: Long = 0)
internal object RuntimeState {
    var view by mutableStateOf(RuntimeView())
    var configurationRevision by mutableIntStateOf(0)
}

/** CE owns intent/config/secrets. DE contains only nonsecret automatic-start protection. */
internal class NasStore(val context: Context) {
    val prefs = context.getSharedPreferences("nas", Context.MODE_PRIVATE)
    private val boot = context.createDeviceProtectedStorageContext()
        .getSharedPreferences("nas_boot", Context.MODE_PRIVATE)
    private val state = NasPersistence(AndroidStatePreferences(boot), { AndroidStatePreferences(prefs) })
    val desired get() = state.desired
    fun desired(value: Boolean) = state.desired(value)
    var error: String
        get() = state.error
        set(value) { state.writeError(value) }
    var dirty: Boolean
        get() = state.dirty
        set(value) { state.markDirty(value) }
    var recoveries: Int
        get() = state.recoveries
        set(value) { state.recordRecovery(value) }
    fun markDirty(value: Boolean) = state.markDirty(value)
    fun recordRecovery(value: Int) = state.recordRecovery(value)
    fun prepareRecoveryBudget(generation: Int?) = state.prepareRecoveryBudget(generation)
    val waitedForUnlock get() = boot.getBoolean("waited_for_unlock", false)
    val demoRoot: File get() = File(checkNotNull(context.getExternalFilesDir(null)) {
        "App external storage unavailable"
    }, "contract-test")
    val controlRoot: File get() = File(context.filesDir, "control")

    /** A diagnostic read must never initialize credentials, storage or an engine. */
    @Synchronized fun existingPassword(): String? {
        if (!prefs.contains("credential")) return null
        return password()
    }

    @Synchronized fun password(): String {
        val keystore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        val alias = "phonenas_probe_local_v1"
        val saved = prefs.getString("credential", null)
        // Never silently replace an unreadable credential/key or clear provider state.
        val key = if (keystore.containsAlias(alias)) keystore.getKey(alias, null) as SecretKey else {
            check(saved == null) { "Credential key unavailable" }
            KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
                init(KeyGenParameterSpec.Builder(alias,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
            }.generateKey()
        }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        if (saved != null) {
            val data = JSONObject(saved)
            cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128,
                Base64.decode(data.getString("iv"), Base64.NO_WRAP)))
            return String(cipher.doFinal(Base64.decode(data.getString("data"), Base64.NO_WRAP)), Charsets.UTF_8)
        }
        val password = Base64.encodeToString(ByteArray(32).also { SecureRandom().nextBytes(it) },
            Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
        cipher.init(Cipher.ENCRYPT_MODE, key)
        val encrypted = cipher.doFinal(password.toByteArray(Charsets.UTF_8))
        check(prefs.edit().putString("credential", JSONObject()
            .put("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .put("data", Base64.encodeToString(encrypted, Base64.NO_WRAP)).toString()).commit())
        return password
    }

    fun config(): String = HostSettings(context).buildConfig(password(), controlRoot, demoRoot)
    fun localConfig(): String = HostSettings(context).buildLocalConfig(password(), controlRoot, demoRoot)

    /** Only invoked by an explicit DEBUG start_demo intent; never by ordinary UI/boot. */
    fun exportDebugAccess() {
        check(BuildConfig.DEBUG)
        check(HostSettings(context).read().mode == HostMode.DEMO) { "Demo control requires demo mode" }
        config()
        context.openFileOutput("test-access.json", Context.MODE_PRIVATE).use {
            it.write(JSONObject().put("username", USERNAME).put("password", password())
                .put("root", demoRoot.absolutePath).toString().toByteArray(Charsets.UTF_8))
        }
    }
}
