package com.phonenas.client

import android.content.Context
import android.os.UserManager
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.system.Os
import android.system.OsConstants
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.nio.file.NoSuchFileException
import java.nio.file.Path
import java.nio.file.StandardCopyOption.ATOMIC_MOVE
import java.nio.file.StandardOpenOption.CREATE_NEW
import java.nio.file.StandardOpenOption.READ
import java.nio.file.StandardOpenOption.WRITE
import java.nio.file.attribute.BasicFileAttributes
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** One connection, in the client's app-private, credential-encrypted no-backup directory. IO thread only. */
internal class ClientProfiles private constructor(
    private val directory: () -> Path,
    private val keys: Keys,
    private val syncDirectory: (Path) -> Unit,
) {
    constructor(context: Context) : this(credentialDirectory(context), AndroidKeys, ::syncAndroidDirectory)

    /** Host JVM tests supply only a storage directory and software keys, not a fake Android context. */
    internal constructor(directory: Path, keys: Keys) : this({ directory }, keys, {})

    internal interface Keys {
        fun existing(): SecretKey?
        fun create(): SecretKey
        fun delete()
    }

    /** Null means no committed profile. Unreadable data stays untouched until explicit forget. */
    fun load(): CloudProfile? = access {
        val envelope = read(directory().resolve(FILE_NAME)) ?: return@access null
        open(envelope, checkNotNull(keys.existing()))
    }

    fun save(profile: CloudProfile): Unit = access {
        val file = directory().resolve(FILE_NAME)
        val previous = read(file)
        val key = keys.existing()
        // Preserve the saved envelope when the key is missing or authentication fails.
        // Do not recheck the old certificate here: explicit pairing may replace an expired one.
        if (previous != null) decrypt(previous, checkNotNull(key)).fill(0)

        val plaintext = encode(profile)
        try {
            replace(file, seal(plaintext, key ?: keys.create()))
        } finally {
            plaintext.fill(0)
        }
    }

    fun forget(): Unit = access {
        val file = directory().resolve(FILE_NAME)
        // Crypto-erase first. Do not decrypt, scan aliases, or recursively remove any directory.
        try {
            keys.delete()
        } finally {
            try {
                Files.deleteIfExists(file)
            } finally {
                Files.deleteIfExists(file.resolveSibling(PENDING_NAME))
            }
        }
        syncDirectory(file.parent)
    }

    private fun <T> access(operation: () -> T): T = synchronized(lock) {
        try {
            operation()
        } catch (_: Exception) {
            // Do not expose paths, provider details, ciphertext, or model inputs to UI or logs.
            throw CloudFailure(CloudIssue.PROFILE)
        }
    }

    private fun read(file: Path): ByteArray? {
        val attributes = try {
            Files.readAttributes(file, BasicFileAttributes::class.java, NOFOLLOW_LINKS)
        } catch (_: NoSuchFileException) {
            return null
        }
        check(attributes.isRegularFile && attributes.size() in 1L..MAX_FILE_BYTES.toLong())
        val envelope = ByteBuffer.allocate(attributes.size().toInt())
        FileChannel.open(file, READ, NOFOLLOW_LINKS).use { channel ->
            while (envelope.hasRemaining()) check(channel.read(envelope) > 0)
            check(channel.read(ByteBuffer.allocate(1)) == -1)
        }
        return envelope.array()
    }

    private fun replace(file: Path, envelope: ByteArray) {
        val pending = file.resolveSibling(PENDING_NAME)
        // A process interruption can leave this uncommitted ciphertext. It is never read as a profile.
        Files.deleteIfExists(pending)
        try {
            FileChannel.open(pending, CREATE_NEW, WRITE).use { channel ->
                val bytes = ByteBuffer.wrap(envelope)
                while (bytes.hasRemaining()) check(channel.write(bytes) > 0)
                channel.force(true)
            }
            // Same directory on Android's private filesystem; errors propagate and there is no copy fallback.
            Files.move(pending, file, ATOMIC_MOVE)
            syncDirectory(file.parent)
        } catch (failure: Exception) {
            runCatching { Files.deleteIfExists(pending) } // Never touch the committed file on failure.
            throw failure
        }
    }

    private fun seal(plaintext: ByteArray, key: SecretKey): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key) // AndroidKeyStore supplies a fresh, randomized IV.
        val iv = cipher.iv
        check(iv.size == IV_BYTES)
        cipher.updateAAD(v2Header)
        val ciphertext = cipher.doFinal(plaintext)
        check(ciphertext.size == plaintext.size + TAG_BYTES)
        return v2Header + iv + ciphertext
    }

    private fun open(envelope: ByteArray, key: SecretKey): CloudProfile {
        val plaintext = decrypt(envelope, key)
        return try {
            decode(plaintext, envelope[v2Header.lastIndex])
        } finally {
            plaintext.fill(0)
        }
    }

    private fun decrypt(envelope: ByteArray, key: SecretKey): ByteArray {
        val offset = v2Header.size + IV_BYTES
        check(envelope.size > offset + TAG_BYTES && (0..2).all { v2Header[it] == envelope[it] })
        check(envelope[v2Header.lastIndex] in 1..2)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(TAG_BYTES * 8, envelope, v2Header.size, IV_BYTES))
        cipher.updateAAD(envelope, 0, v2Header.size)
        return cipher.doFinal(envelope, offset, envelope.size - offset)
    }

    // V2 adds an optional sixth field to V1's five big-endian length-prefixed fields.
    // Loading a V1 profile never rewrites it; only the explicit save action records the exchange.
    private fun encode(profile: CloudProfile): ByteArray {
        val identity = profile.identity
        val host = identity.endpoint.host.encodeToByteArray()
        val fingerprint = identity.fingerprint.encodeToByteArray()
        val pem = identity.pem
        val username = profile.credentials.username.encodeToByteArray(throwOnInvalidSequence = true)
        try {
            val password = profile.credentials.password.encodeToByteArray(throwOnInvalidSequence = true)
            try {
                val fields = arrayOf(host, fingerprint, pem, username, password)
                check(fields.indices.all { fields[it].size in 1..fieldLimits[it] })
                val exchange = profile.exchange?.root?.name?.encodeToByteArray(throwOnInvalidSequence = true) ?: byteArrayOf()
                check(exchange.size <= MAX_EXCHANGE)
                val result = ByteBuffer.allocate(LENGTH_BYTES + fields.sumOf { it.size } + exchange.size)
                for (field in fields) result.putInt(field.size).put(field)
                return result.putInt(exchange.size).put(exchange).array()
            } finally {
                password.fill(0)
            }
        } finally {
            username.fill(0)
        }
    }

    private fun decode(plaintext: ByteArray, version: Byte): CloudProfile {
        val input = ByteBuffer.wrap(plaintext)
        val host = input.field(MAX_HOST).utf8()
        val fingerprint = input.field(MAX_FINGERPRINT).utf8()
        val pemField = input.field(CloudIdentity.MAX_PEM_BYTES)
        val pem = ByteArray(pemField.remaining()).also { pemField.get(it) }
        val username = input.field(MAX_USERNAME).utf8()
        val password = input.field(MAX_PASSWORD).utf8()
        val exchange = if (version.toInt() == 2) input.field(MAX_EXCHANGE, min = 0).utf8()
            .takeIf { it.isNotEmpty() }?.let(ExchangeShare::restore) else null
        check(!input.hasRemaining())
        val endpoint = CloudEndpoint.parse(host)
        val identity = CloudIdentity.restore(endpoint, pem, fingerprint)
        check(endpoint.host == host && identity.fingerprint == fingerprint)
        return CloudProfile(identity, CloudCredentials.create(username, password), exchange)
    }

    private fun ByteBuffer.field(max: Int, min: Int = 1): ByteBuffer {
        val size = int
        check(size in min..max && size <= remaining())
        val value = slice()
        value.limit(size)
        position(position() + size)
        return value
    }

    private fun ByteBuffer.utf8(): String = Charsets.UTF_8.newDecoder().decode(this).toString()

    private object AndroidKeys : Keys {
        private const val PROVIDER = "AndroidKeyStore"
        private const val ALIAS = "phonenas_cloud_profile_v1"
        private fun store() = KeyStore.getInstance(PROVIDER).apply { load(null) }

        override fun existing(): SecretKey? {
            val store = store()
            val key = store.getKey(ALIAS, null)
            if (key == null) {
                check(!store.containsAlias(ALIAS))
                return null
            }
            check(key is SecretKey && key.algorithm == KeyProperties.KEY_ALGORITHM_AES)
            return key
        }

        override fun create(): SecretKey = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, PROVIDER)
            .apply {
                init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                    .setKeySize(256)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setRandomizedEncryptionRequired(true)
                    .build())
            }.generateKey()

        override fun delete() = store().deleteEntry(ALIAS)
    }

    private companion object {
        private const val FILE_NAME = "cloud-profile.bin"
        private const val PENDING_NAME = "$FILE_NAME.new"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val IV_BYTES = 12
        private const val TAG_BYTES = 16
        private const val MAX_HOST = 15
        private const val MAX_FINGERPRINT = 64
        private const val MAX_USERNAME = 128
        private const val MAX_PASSWORD = 1024
        private const val LENGTH_BYTES = 6 * Int.SIZE_BYTES
        private const val MAX_EXCHANGE = 255
        private val fieldLimits = intArrayOf(MAX_HOST, MAX_FINGERPRINT, CloudIdentity.MAX_PEM_BYTES, MAX_USERNAME, MAX_PASSWORD)
        private val v2Header = byteArrayOf(0x50, 0x4e, 0x43, 2) // PNC + format version, authenticated as GCM AAD.
        private val MAX_FILE_BYTES = v2Header.size + IV_BYTES + TAG_BYTES + LENGTH_BYTES + fieldLimits.sum() + MAX_EXCHANGE

        // The client runs in one process. Serialize even across different ClientProfiles instances.
        private val lock = Any()

        private fun syncAndroidDirectory(directory: Path) {
            val descriptor = Os.open(directory.toString(), OsConstants.O_RDONLY, 0)
            try {
                check(OsConstants.S_ISDIR(Os.fstat(descriptor).st_mode))
                Os.fsync(descriptor)
            } finally {
                Os.close(descriptor)
            }
        }

        private fun credentialDirectory(context: Context): () -> Path {
            val app = context.applicationContext
            if (context.isDeviceProtectedStorage || app.isDeviceProtectedStorage) throw CloudFailure(CloudIssue.PROFILE)
            return {
                check(app.getSystemService(UserManager::class.java)?.isUserUnlocked == true)
                app.noBackupFilesDir.toPath()
            }
        }
    }
}
