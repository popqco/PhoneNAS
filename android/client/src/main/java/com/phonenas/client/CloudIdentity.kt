package com.phonenas.client

import java.io.IOException
import java.net.Inet4Address
import java.net.InetAddress
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.Base64
import javax.net.ssl.X509TrustManager

internal enum class CloudIssue {
    ADDRESS, LEGACY_HTTP, IMPORT, FINGERPRINT, CERTIFICATE, CREDENTIALS, VPN, UNREACHABLE,
    AUTH, FORBIDDEN, NOT_FOUND, RESPONSE, DOWNLOAD_LENGTH, DOWNLOAD_CAPACITY,
    STAGING, SAVE, DOWNLOAD_SAVE, DOWNLOAD_SAVE_UNKNOWN, PROFILE, CANCELLED,
    UPLOAD_SOURCE, UPLOAD_LENGTH, UPLOAD_CAPACITY, UPLOAD_STAGING, UPLOAD_READ_ONLY,
    UPLOAD_EXISTS, UPLOAD_CONFLICT, UPLOAD_TARGET, UPLOAD_STORAGE, UPLOAD_BUSY, UPLOAD_UNCONFIRMED,
}

internal class CloudFailure(val issue: CloudIssue) : IOException(issue.name)

internal class CloudEndpoint private constructor(val host: String, val address: Inet4Address) {
    val url = "https://$host:18889"

    companion object {
        private val base = Regex("(https://)?(100\\.[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3})(:18889)?(/)?")

        fun parse(input: String): CloudEndpoint {
            val addressInput = input.trim()
            if (addressInput.startsWith("http://", ignoreCase = true) ||
                Regex("100\\.[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}:8888/?").matches(addressInput)) {
                throw CloudFailure(CloudIssue.LEGACY_HTTP)
            }
            val match = base.matchEntire(addressInput) ?: throw CloudFailure(CloudIssue.ADDRESS)
            val (scheme, host, port, slash) = match.destructured
            if ((scheme.isNotEmpty() && port.isEmpty()) || (slash.isNotEmpty() && scheme.isEmpty())) {
                throw CloudFailure(CloudIssue.ADDRESS)
            }
            val octets = host.split('.').map { it.toInt() }
            if (octets.any { it !in 0..255 } || octets[1] !in 64..127 || octets.joinToString(".") != host) {
                throw CloudFailure(CloudIssue.ADDRESS)
            }
            val address = InetAddress.getByAddress(octets.map(Int::toByte).toByteArray()) as Inet4Address
            return CloudEndpoint(host, address)
        }
    }
}

internal class CloudCredentials private constructor(val username: String, val password: String) {
    companion object {
        fun create(username: String, password: String): CloudCredentials {
            if (username.isBlank() || ':' in username || password.isEmpty() ||
                username.toByteArray(Charsets.UTF_8).size > 128 || password.toByteArray(Charsets.UTF_8).size > 1024 ||
                username.any(::disallowed) || password.any(::disallowed)) throw CloudFailure(CloudIssue.CREDENTIALS)
            return CloudCredentials(username, password)
        }

        private fun disallowed(character: Char): Boolean = character.isISOControl()
    }
}

internal class CloudProfile(
    val identity: CloudIdentity, val credentials: CloudCredentials, val exchange: ExchangeShare? = null,
)

internal class CloudIdentity private constructor(
    val endpoint: CloudEndpoint,
    private val leaf: X509Certificate,
    private val publicPem: ByteArray,
    val fingerprint: String,
) {
    val pem: ByteArray get() = publicPem.copyOf()

    fun checkValidity() {
        try {
            leaf.checkValidity()
        } catch (_: CertificateException) {
            throw CloudFailure(CloudIssue.CERTIFICATE)
        }
    }

    fun trustManager(): X509TrustManager = object : X509TrustManager {
        override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf(leaf)
        override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
            throw CertificateException("not a client trust store")
        }
        override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
            val peer = chain?.singleOrNull() ?: throw CertificateException("unexpected certificate chain")
            if (!MessageDigest.isEqual(leaf.encoded, peer.encoded)) throw CertificateException("unexpected leaf")
            peer.checkValidity()
        }
    }

    companion object {
        const val MAX_PEM_BYTES = 32 * 1024
        private val fingerprintSyntax = Regex("[0-9a-f]{64}")
        private val pemSyntax = Regex("-----BEGIN CERTIFICATE-----\\r?\\n([A-Za-z0-9+/=\\r\\n]+)\\r?\\n-----END CERTIFICATE-----")

        fun verify(endpoint: CloudEndpoint, pem: ByteArray, expectedFingerprint: String): CloudIdentity =
            restore(endpoint, pem, expectedFingerprint).also(CloudIdentity::checkValidity)

        // An expired saved leaf must not strand a user's connection or permit a live request.
        fun restore(endpoint: CloudEndpoint, pem: ByteArray, expectedFingerprint: String): CloudIdentity {
            val expected = expectedFingerprint.trim().replace(":", "").lowercase()
            if (!fingerprintSyntax.matches(expected)) throw CloudFailure(CloudIssue.FINGERPRINT)
            val leaf = parseLeaf(pem)
            val actual = MessageDigest.getInstance("SHA-256").digest(leaf.encoded)
                .joinToString("") { "%02x".format(it) }
            if (actual != expected) throw CloudFailure(CloudIssue.FINGERPRINT)
            try {
                leaf.verify(leaf.publicKey)
                val san = leaf.subjectAlternativeNames?.singleOrNull()
                val serverUsage = leaf.extendedKeyUsage
                if (leaf.basicConstraints >= 0 || leaf.subjectX500Principal != leaf.issuerX500Principal ||
                    san?.getOrNull(0) != 7 || san.getOrNull(1) != endpoint.host ||
                    serverUsage != null && "1.3.6.1.5.5.7.3.1" !in serverUsage ||
                    leaf.keyUsage?.firstOrNull() == false) {
                    throw CertificateException("certificate is not valid for this server")
                }
            } catch (_: Exception) {
                throw CloudFailure(CloudIssue.CERTIFICATE)
            }
            return CloudIdentity(endpoint, leaf, pem.copyOf(), actual)
        }

        private fun parseLeaf(pem: ByteArray): X509Certificate = try {
            require(pem.size in 1..MAX_PEM_BYTES && pem.all { it in 0..127 })
            val text = pem.toString(Charsets.US_ASCII).trim()
            val body = requireNotNull(pemSyntax.matchEntire(text)).groupValues[1]
            val der = Base64.getDecoder().decode(body.replace("\r", "").replace("\n", ""))
            val input = der.inputStream()
            val leaf = CertificateFactory.getInstance("X.509").generateCertificate(input) as X509Certificate
            require(input.available() == 0 && leaf.encoded.contentEquals(der))
            leaf
        } catch (_: Exception) {
            throw CloudFailure(CloudIssue.CERTIFICATE)
        }
    }
}
