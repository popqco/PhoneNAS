package com.phonenas.client

import okhttp3.tls.HeldCertificate
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.util.HexFormat
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Exercises production serialization, GCM and file I/O; Android CE storage and KeyStore are not emulated. */
class ClientProfilesTest {
    @get:Rule val temp = TemporaryFolder()

    private val keys = SoftwareKeys()
    private val leaf = HeldCertificate.Builder().addSubjectAlternativeName("100.101.2.3").build()
    private val identity = CloudIdentity.verify(
        CloudEndpoint.parse("100.101.2.3"),
        leaf.certificatePem().encodeToByteArray(),
        HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(leaf.certificate.encoded)),
    )

    @Test fun `only the latest profile survives reopening without plaintext on disk`() {
        val store = store()
        assertNull(store.load())
        assertNull(keys.key)
        assertArrayEquals(emptyArray<String>(), temp.root.list())

        store.save(profile("previous-test-password"))
        store.save(profile("current-test-password-é"))

        val reopened = checkNotNull(store().load())
        assertEquals("100.101.2.3", reopened.identity.endpoint.host)
        assertEquals(identity.fingerprint, reopened.identity.fingerprint)
        assertArrayEquals(identity.pem, reopened.identity.pem)
        assertEquals("test-user-é", reopened.credentials.username)
        assertEquals("current-test-password-é", reopened.credentials.password)
        val onDisk = temp.root.listFiles()!!.single().readBytes().decodeToString()
        listOf("test-user", "test-password", "100.101.2.3", identity.fingerprint, "BEGIN CERTIFICATE")
            .forEach { assertFalse(onDisk.contains(it)) }
    }

    @Test fun `an expired saved identity remains replaceable but cannot authenticate`() {
        val expired = HeldCertificate.Builder().addSubjectAlternativeName("100.101.2.3")
            .validityInterval(1_700_000_000_000, 1_700_000_001_000).build()
        val endpoint = CloudEndpoint.parse("100.101.2.3")
        val pem = expired.certificatePem().encodeToByteArray()
        val fingerprint = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(expired.certificate.encoded))
        assertEquals(CloudIssue.CERTIFICATE, assertThrows(CloudFailure::class.java) {
            CloudIdentity.verify(endpoint, pem, fingerprint)
        }.issue)
        val store = store()
        store.save(CloudProfile(CloudIdentity.restore(endpoint, pem, fingerprint), CloudCredentials.create("synthetic", "fixture")))
        val restored = checkNotNull(store.load())
        assertEquals(CloudIssue.CERTIFICATE, assertThrows(CloudFailure::class.java) {
            CloudDav(restored, CloudRoutes { _, _ -> error("expired identity must not open the network") })
                .list(DavPath.Root, CloudOperation())
        }.issue)
        store.save(profile("replacement-test-password"))
        assertEquals(identity.fingerprint, store.load()?.identity?.fingerprint)
    }

    @Test fun `a missing key does not regenerate or overwrite the stored profile`() {
        val store = store()
        store.save(profile("original-test-password"))
        val committed = temp.root.listFiles()!!.single()
        val ciphertext = committed.readBytes()
        keys.key = null

        failsClosed { store.load() }
        failsClosed { store.save(profile("replacement-test-password")) }
        assertNull(keys.key)
        assertArrayEquals(ciphertext, committed.readBytes())

        store.forget()
        assertNull(store().load())
        store.save(profile("replacement-test-password"))
        assertEquals("replacement-test-password", store().load()?.credentials?.password)
    }

    @Test fun `tampering is rejected and forget removes only profile state`() {
        val store = store()
        store.save(profile("test-password"))
        val committed = temp.root.listFiles()!!.single()
        val damaged = committed.readBytes().apply { this[lastIndex] = (last().toInt() xor 1).toByte() }
        committed.writeBytes(damaged)
        val unrelated = temp.newFile("unrelated.bin").apply { writeText("keep") }

        failsClosed { store.load() }
        failsClosed { store.save(profile("replacement-test-password")) }
        assertArrayEquals(damaged, committed.readBytes())

        store.forget()
        store.forget()
        assertNull(keys.key)
        assertNull(store().load())
        assertEquals(listOf(unrelated), temp.root.listFiles()!!.toList())
        assertEquals("keep", unrelated.readText())
    }

    @Test fun `authenticated but invalid V1 fields are not accepted`() {
        val key = keys.create()
        val committed = temp.newFile("cloud-profile.bin")
        val valid = listOf(
            "100.101.2.3".encodeToByteArray(), identity.fingerprint.encodeToByteArray(), identity.pem,
            "test-user".encodeToByteArray(), "test-password".encodeToByteArray(),
        )
        val wrongFingerprint = (if (identity.fingerprint.startsWith('0')) "1" else "0") + identity.fingerprint.drop(1)
        val invalidFields = listOf(
            0 to "127.0.0.1".encodeToByteArray(),
            1 to wrongFingerprint.encodeToByteArray(),
            3 to "user:name".encodeToByteArray(),
            4 to byteArrayOf(0xc3.toByte(), 0x28), // Invalid UTF-8.
            4 to ByteArray(1025) { 'x'.code.toByte() },
        )
        val encoded = packV1(valid)
        val invalidPayloads = invalidFields.map { (index, field) ->
            packV1(valid.toMutableList().apply { set(index, field) })
        } + listOf(
            encoded + byteArrayOf(0),
            encoded.copyOf(encoded.size - 1),
            encoded.copyOf().apply { ByteBuffer.wrap(this).putInt(Int.MAX_VALUE) },
        )

        val store = store()
        val v1 = sealV1(encoded, key)
        committed.writeBytes(v1)
        val previous = checkNotNull(store.load())
        assertEquals("test-password", previous.credentials.password)
        assertNull(previous.exchange)
        assertArrayEquals(v1, committed.readBytes())

        val exchange = ExchangeShare.of(DavPath.Root.visibleChild("互传", true)!!)
        store.save(CloudProfile(previous.identity, previous.credentials, exchange))
        val reopened = checkNotNull(store.load())
        assertEquals(exchange, reopened.exchange)
        assertEquals("test-password", reopened.credentials.password)
        assertFalse(committed.readBytes().decodeToString().contains("互传"))
        for (payload in invalidPayloads) {
            val ciphertext = sealV1(payload, key)
            committed.writeBytes(ciphertext)
            failsClosed { store.load() }
            assertArrayEquals(ciphertext, committed.readBytes())
        }
    }

    @Test fun `uncommitted ciphertext is not loaded and explicit forget clears it`() {
        val store = store()
        store.save(profile("test-password"))
        val committed = temp.root.listFiles()!!.single()
        val ciphertext = committed.readBytes()
        val pending = temp.newFile("${committed.name}.new").apply { writeBytes(ciphertext) }
        check(committed.delete())

        assertNull(store().load())
        assertArrayEquals(ciphertext, pending.readBytes())
        store.forget()
        assertFalse(pending.exists())
        assertNull(keys.key)
    }

    // An independent V1 wire fixture lets us create authenticated records the model forbids.
    private fun packV1(fields: List<ByteArray>): ByteArray = ByteBuffer
        .allocate(fields.sumOf { 4 + it.size })
        .apply { fields.forEach { putInt(it.size).put(it) } }
        .array()

    private fun sealV1(payload: ByteArray, key: SecretKey): ByteArray {
        val header = byteArrayOf(0x50, 0x4e, 0x43, 1)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key) }
        cipher.updateAAD(header)
        return header + cipher.iv + cipher.doFinal(payload)
    }

    private fun store() = ClientProfiles(temp.root.toPath(), keys)
    private fun profile(password: String) = CloudProfile(identity, CloudCredentials.create("test-user-é", password))

    private fun failsClosed(operation: () -> Unit) {
        val failure = assertThrows(CloudFailure::class.java) { operation() }
        assertEquals(CloudIssue.PROFILE, failure.issue)
        assertNull(failure.cause)
    }

    private class SoftwareKeys : ClientProfiles.Keys {
        var key: SecretKey? = null
        override fun existing() = key
        override fun create(): SecretKey = KeyGenerator.getInstance("AES").generateKey().also { key = it }
        override fun delete() { key = null }
    }
}
