package com.phonenas.client

import okhttp3.tls.HeldCertificate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.security.MessageDigest
import java.util.HexFormat

class CloudIdentityTest {
    private val endpoint = CloudEndpoint.parse("https://100.101.2.3:18889/")
    private val leaf = HeldCertificate.Builder().addSubjectAlternativeName("100.101.2.3").build()
    private val pem = leaf.certificatePem().toByteArray(Charsets.US_ASCII)
    private val pin = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(leaf.certificate.encoded))

    @Test fun importedPublicCertificateMustMatchTheDisplayPinAndEndpoint() {
        val identity = CloudIdentity.verify(endpoint, pem, pin)
        assertEquals("100.101.2.3", identity.endpoint.host)
        assertEquals(pin, identity.fingerprint)
        val changed = if (pin.startsWith("0")) "1" else "0"
        assertEquals(CloudIssue.FINGERPRINT, assertThrows(CloudFailure::class.java) {
            CloudIdentity.verify(endpoint, pem, changed + pin.drop(1))
        }.issue)
        assertEquals(CloudIssue.CERTIFICATE, assertThrows(CloudFailure::class.java) {
            CloudIdentity.verify(CloudEndpoint.parse("100.101.2.4"), pem, pin)
        }.issue)
        assertThrows(CloudFailure::class.java) { CloudIdentity.verify(endpoint, pem + pem, pin) }
    }

    @Test fun endpointAcceptsOnlyCanonicalPrivateVpnIpv4AndTheNasPort() {
        assertEquals("100.64.0.1", CloudEndpoint.parse("100.64.0.1").host)
        assertEquals("100.127.255.254", CloudEndpoint.parse("100.127.255.254").host)
        listOf("100.63.12.4", "100.128.12.4", "127.0.0.1", "100.101.02.3", "2130706433",
            "https://100.101.2.3:18888/", "https://x@100.101.2.3:18889/",
            "https://100.101.2.3:18889/share", "https://100.101.2.3:18889/?x", "100.101.2.3#x",
            "::ffff:100.101.2.3", "nas.example").forEach { value ->
            assertEquals(CloudIssue.ADDRESS, assertThrows(CloudFailure::class.java) {
                CloudEndpoint.parse(value)
            }.issue)
        }
    }
}
