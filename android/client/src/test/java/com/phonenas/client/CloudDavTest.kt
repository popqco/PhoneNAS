package com.phonenas.client

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayOutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketAddress
import java.security.MessageDigest
import java.util.HexFormat
import java.util.concurrent.TimeUnit
import javax.net.SocketFactory

class CloudDavTest {
    @get:Rule val temp = TemporaryFolder()
    private val host = CloudEndpoint.parse("100.95.187.47")
    private val leaf = HeldCertificate.Builder().addSubjectAlternativeName(host.host).build()
    private val credentials = CloudCredentials.create("synthetic", "fixture-pass")

    @Test fun listsAndDownloadsTheEscapedFileOverPinnedTls() = withServer { server ->
        val root = davResponse("/", true) + davResponse("/public/", true) + davResponse("/.phonenas/", true)
        val child = "/public/%E4%B8%AD%20%25%23+.txt"
        server.enqueue(multistatus(root))
        server.enqueue(multistatus(davResponse("/public/", true) + davResponse(child, false, 4)))
        server.enqueue(MockResponse().setBody("data"))
        val client = client(server)
        val share = client.list(DavPath.Root, CloudOperation()).single()
        val file = client.list(share.path, CloudOperation()).single()
        assertEquals("中 %#+.txt", file.path.name)
        val downloaded = ByteArrayOutputStream()
        client.download(file, DownloadStaging(temp.newFolder()), CloudOperation()) { _, _ -> }.use {
            it.copyTo(downloaded)
        }
        assertArrayEquals(byteArrayOf(100, 97, 116, 97), downloaded.toByteArray())
        val request = server.takeRequest(3, TimeUnit.SECONDS)!!
        assertEquals("PROPFIND", request.method)
        assertEquals("1", request.getHeader("Depth"))
        assertEquals("Basic c3ludGhldGljOmZpeHR1cmUtcGFzcw==", request.getHeader("Authorization"))
        assertEquals("/public/", server.takeRequest(3, TimeUnit.SECONDS)!!.path)
        val get = server.takeRequest(3, TimeUnit.SECONDS)!!
        assertEquals("GET", get.method)
        // Either origin spelling for a literal plus represents the same segment, not a space.
        assertEquals(child, get.path!!.replace("%2B", "+"))
    }

    @Test fun addsANewFileWithItsExactBytesAndNeverAnUnconditionalPut() = withServer { server ->
        val directory = DavPath.Root.visibleChild("互传", true)!!
        val name = "中 %#+.bin"
        val payload = ByteArray(143_789) { (it * 19).toByte() }
        server.enqueue(MockResponse().setHeader("Allow", "OPTIONS, GET, HEAD, PROPFIND, PUT"))
        server.enqueue(MockResponse().setSocketPolicy(okhttp3.mockwebserver.SocketPolicy.EXPECT_CONTINUE)
            .setResponseCode(201))
        val client = client(server)
        client.checkWritable(directory, CloudOperation())
        client.upload(directory.visibleChild(name, false)!!, bytes(payload), CloudOperation()) { _, _ -> }

        assertEquals("OPTIONS", server.takeRequest(3, TimeUnit.SECONDS)!!.method)
        val put = server.takeRequest(3, TimeUnit.SECONDS)!!
        assertEquals("PUT", put.method)
        assertEquals("/%E4%BA%92%E4%BC%A0/%E4%B8%AD%20%25%23%2B.bin", put.path)
        assertEquals("*", put.getHeader("If-None-Match"))
        assertEquals(payload.size.toString(), put.getHeader("Content-Length"))
        assertArrayEquals(payload, put.body.readByteArray())
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
    }

    @Test fun wrongCertificateEvenForSameIpNeverReceivesAnHttpRequest() = withServer { server ->
        val wrong = HeldCertificate.Builder().addSubjectAlternativeName(host.host).build()
        val client = client(server, imported = wrong)
        assertEquals(CloudIssue.CERTIFICATE, fail { client.list(DavPath.Root, CloudOperation()) })
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
    }

    @Test fun redirectIsNotFollowedAndWrongCredentialsAreNotAnEmptyFolder() = withServer { server ->
        server.enqueue(MockResponse().setResponseCode(307).setHeader("Location", "/wrong-place/"))
        val client = client(server)
        assertEquals(CloudIssue.RESPONSE, fail { client.list(DavPath.Root, CloudOperation()) })
        assertEquals("/", server.takeRequest(3, TimeUnit.SECONDS)!!.path)
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
        server.enqueue(MockResponse().setResponseCode(401))
        assertEquals(CloudIssue.AUTH, fail { client.list(DavPath.Root, CloudOperation()) })
    }

    @Test fun noNetworkLeaseMeansNoConnect() = withServer { server ->
        val denied = CloudRoutes { _, _ -> throw CloudFailure(CloudIssue.VPN) }
        assertEquals(CloudIssue.VPN, fail { client(server, routes = denied).list(DavPath.Root, CloudOperation()) })
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
    }

    @Test fun lossWhileRequestIsInFlightCancelsTheCall() = withServer { server ->
        val route = LoopbackRoute(server)
        server.enqueue(MockResponse().setSocketPolicy(okhttp3.mockwebserver.SocketPolicy.NO_RESPONSE))
        val result = java.util.concurrent.FutureTask { fail { client(server, routes = route).list(DavPath.Root, CloudOperation()) } }
        Thread(result).apply { isDaemon = true }.start()
        assertEquals("/", server.takeRequest(3, TimeUnit.SECONDS)!!.path)
        route.lose()
        assertEquals(CloudIssue.VPN, result.get(3, TimeUnit.SECONDS))
    }

    @Test fun truncatedContentNeverLeavesADownloadForExport() = withServer { server ->
        val data = ByteArray(32_768) { 3 }
        val path = "/partial.bin"
        server.enqueue(multistatus(davResponse("/", true) + davResponse(path, false, data.size.toLong())))
        server.enqueue(MockResponse().setBody(okio.Buffer().write(data))
            .setSocketPolicy(okhttp3.mockwebserver.SocketPolicy.DISCONNECT_DURING_RESPONSE_BODY))
        val client = client(server)
        val item = client.list(DavPath.Root, CloudOperation()).single()
        val staging = temp.newFolder()
        val outcome = fail { client.download(item, DownloadStaging(staging), CloudOperation()) { _, _ -> } }
        assertEquals(CloudIssue.DOWNLOAD_LENGTH, outcome)
        assertFalse(staging.listFiles().orEmpty().any { it.isFile })
    }

    @Test fun aPrematureCreatedStatusCannotConfirmAnUnreadSource() = withServer { server ->
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DO_NOT_READ_REQUEST_BODY).setResponseCode(201))
        assertEquals(CloudIssue.UPLOAD_UNCONFIRMED, fail {
            client(server).upload(uploadPath(), bytes(byteArrayOf(1, 2, 3)), CloudOperation()) { _, _ -> }
        })
        assertEquals(0L, server.takeRequest(3, TimeUnit.SECONDS)!!.bodySize)
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
    }

    @Test fun knownRejectedLargeBodiesNeedNotReadTheSourceAndAreNotRetried() = withServer { server ->
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DO_NOT_READ_REQUEST_BODY).setResponseCode(412))
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DO_NOT_READ_REQUEST_BODY)
            .setResponseCode(503).setHeader("Retry-After", "0"))
        val source = object : UploadContent {
            override val length = 128L * 1024
            override fun withInput(operation: CloudOperation, consume: (java.io.InputStream) -> Unit) {
                error("the server rejected the headers before requesting the source")
            }
            override fun close() = Unit
        }
        for (expected in listOf(CloudIssue.UPLOAD_EXISTS, CloudIssue.UPLOAD_BUSY)) {
            assertEquals(expected, fail { client(server).upload(uploadPath(), source, CloudOperation()) { _, _ -> } })
            val request = server.takeRequest(3, TimeUnit.SECONDS)!!
            assertEquals(0L, request.bodySize)
            assertEquals("100-continue", request.getHeader("Expect"))
            assertEquals("*", request.getHeader("If-None-Match"))
            assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
        }
    }

    @Test fun lossAfterTheFullPutIsNotAConfirmedFailureOrAutomaticRetry() = withServer { server ->
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        val route = LoopbackRoute(server)
        val result = java.util.concurrent.FutureTask {
            fail { client(server, routes = route).upload(uploadPath(), bytes(byteArrayOf()), CloudOperation()) { _, _ -> } }
        }
        Thread(result).apply { isDaemon = true }.start()
        assertEquals("PUT", server.takeRequest(3, TimeUnit.SECONDS)!!.method)
        route.lose()
        assertEquals(CloudIssue.UPLOAD_UNCONFIRMED, result.get(3, TimeUnit.SECONDS))
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
    }

    @Test fun sourceLengthMismatchCannotCompleteARequestBody() = withServer { server ->
        val client = client(server)
        for ((length, payload) in listOf(0L to byteArrayOf(1), 1L to byteArrayOf(1, 2), 2L to byteArrayOf(1))) {
            if (length > 0) server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.EXPECT_CONTINUE).setResponseCode(201))
            assertEquals(CloudIssue.UPLOAD_LENGTH, fail { client.upload(uploadPath(), bytes(payload, length),
                CloudOperation()) { _, _ -> } })
            if (length == 0L) assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
        }
    }

    @Test fun batchKeepsBothOnlyOnExplicitChoiceAndSkipsProtectedPaths() = withServer { server ->
        val exchange = ExchangeShare.of(DavPath.Root.visibleChild("exchange", true)!!)
        val directory = exchange.root
        val payload = "sample".encodeToByteArray()
        fun send(keep: Boolean): List<UploadResult> {
            server.enqueue(MockResponse().setHeader("Allow", "OPTIONS, PROPFIND, PUT"))
            server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DO_NOT_READ_REQUEST_BODY).setResponseCode(412))
            if (keep) server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.EXPECT_CONTINUE).setResponseCode(201))
            return CloudUploadBatch(client(server)).send(exchange, directory, listOf("bad/name", "same.txt"), keep,
                CloudOperation(), { name, _ -> UploadItem(name) { _, _ -> bytes(payload) } }, {})
        }
        assertFalse(exchange.contains(DavPath.Root))
        assertFalse(exchange.contains(directory.visibleChild("Versions", true)!!))
        assertFalse(exchange.contains(DavPath.Root.visibleChild("other", true)!!))
        assertEquals(listOf(UploadState.FAILED, UploadState.SKIPPED), send(false).map { it.state })
        assertEquals("OPTIONS", server.takeRequest(3, TimeUnit.SECONDS)!!.method)
        assertEquals("/exchange/same.txt", server.takeRequest(3, TimeUnit.SECONDS)!!.path)
        assertEquals("same (2).txt", send(true).last().savedAs)
        assertEquals("OPTIONS", server.takeRequest(3, TimeUnit.SECONDS)!!.method)
        assertEquals("/exchange/same.txt", server.takeRequest(3, TimeUnit.SECONDS)!!.path)
        val added = server.takeRequest(3, TimeUnit.SECONDS)!!
        assertEquals("/exchange/same%20%282%29.txt", added.path)
        assertEquals("*", added.getHeader("If-None-Match"))
        assertArrayEquals(payload, added.body.readByteArray())
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
    }

    @Test fun anExistingDirectoryWithTheSameNameAlsoKeepsItsContents() = withServer { server ->
        val exchange = ExchangeShare.of(DavPath.Root.visibleChild("exchange", true)!!)
        server.enqueue(MockResponse().setHeader("Allow", "OPTIONS, PROPFIND, PUT"))
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DO_NOT_READ_REQUEST_BODY).setResponseCode(409))
        server.enqueue(multistatus(davResponse("/exchange/", true) + davResponse("/exchange/same/", true)))
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.EXPECT_CONTINUE).setResponseCode(201))
        val result = CloudUploadBatch(client(server)).send(exchange, exchange.root, listOf("same"), true,
            CloudOperation(), { name, _ -> UploadItem(name) { _, _ -> bytes(byteArrayOf(1)) } }, {}).single()
        assertEquals(UploadState.CREATED, result.state)
        assertEquals("same (2)", result.savedAs)
        assertEquals("OPTIONS", server.takeRequest(3, TimeUnit.SECONDS)!!.method)
        assertEquals("/exchange/same", server.takeRequest(3, TimeUnit.SECONDS)!!.path)
        assertEquals("PROPFIND", server.takeRequest(3, TimeUnit.SECONDS)!!.method)
        assertEquals("/exchange/same%20%282%29", server.takeRequest(3, TimeUnit.SECONDS)!!.path)
    }

    @Test fun readOnlyBatchDoesNotOpenAnySelectedFiles() = withServer { server ->
        server.enqueue(MockResponse().setHeader("Allow", "OPTIONS, PROPFIND, GET, HEAD"))
        val exchange = ExchangeShare.of(DavPath.Root.visibleChild("readonly", true)!!)
        val results = CloudUploadBatch(client(server)).send(exchange, exchange.root, listOf("selected"), false,
            CloudOperation(), { _, _ -> error("should never read a source when the destination is read-only") }, {})
        assertEquals(UploadState.NOT_STARTED, results.single().state)
        assertEquals(CloudIssue.UPLOAD_READ_ONLY, results.single().issue)
        assertEquals("OPTIONS", server.takeRequest(3, TimeUnit.SECONDS)!!.method)
        assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
    }

    @Test fun directDownloadChecksTheContentBeforePublication() = withServer { server ->
        val data = ByteArray(48_190) { (it * 17).toByte() }
        val path = DavPath.Root.visibleChild("copy.bin", false)!!
        val entry = DavEntry(path, data.size.toLong())
        val client = client(server)
        fun fetch() = client.downloadTo(entry, CloudOperation(), { _, _ -> }) { length, verify, copy ->
            assertEquals(data.size.toLong(), length)
            val received = ByteArrayOutputStream()
            copy(received)
            verify()
            received.toByteArray()
        }
        server.enqueue(MockResponse().setBody(okio.Buffer().write(data))
            .setSocketPolicy(SocketPolicy.DISCONNECT_DURING_RESPONSE_BODY))
        assertEquals(CloudIssue.DOWNLOAD_LENGTH, fail { fetch() })
        server.enqueue(MockResponse().setBody(okio.Buffer().write(data)))
        assertArrayEquals(data, fetch())
    }

    private fun uploadPath() = DavPath.Root.visibleChild("exchange", true)!!.visibleChild("new.bin", false)!!

    private fun bytes(payload: ByteArray, length: Long = payload.size.toLong()) = object : UploadContent {
        override val length = length
        override fun withInput(operation: CloudOperation, consume: (java.io.InputStream) -> Unit) {
            payload.inputStream().use(consume)
        }
        override fun close() = Unit
    }

    private fun client(server: MockWebServer, imported: HeldCertificate = leaf,
                       routes: CloudRoutes = LoopbackRoute(server)): CloudDav {
        val pin = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(imported.certificate.encoded))
        val identity = CloudIdentity.verify(host, imported.certificatePem().toByteArray(Charsets.US_ASCII), pin)
        return CloudDav(CloudProfile(identity, credentials), routes)
    }

    private fun withServer(block: (MockWebServer) -> Unit) = MockWebServer().use { server ->
        server.useHttps(HandshakeCertificates.Builder().heldCertificate(leaf).build().sslSocketFactory(), false)
        server.start(InetAddress.getByName("127.0.0.1"), 0)
        block(server)
    }

    private fun multistatus(responses: String) = MockResponse().setResponseCode(207)
        .setHeader("Content-Type", "application/xml; charset=utf-8")
        .setBody("<D:multistatus xmlns:D=\"DAV:\">$responses</D:multistatus>")

    private fun davResponse(path: String, collection: Boolean, length: Long? = null): String {
        val resource = if (collection) "<D:collection/>" else ""
        val size = length?.let { "<D:getcontentlength>$it</D:getcontentlength>" }.orEmpty()
        return "<D:response><D:href>$path</D:href><D:propstat><D:prop>" +
            "<D:resourcetype>$resource</D:resourcetype>$size</D:prop>" +
            "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
    }

    private fun fail(block: () -> Unit) = assertThrows(CloudFailure::class.java, block).issue

    private class LoopbackRoute(private val server: MockWebServer) : CloudRoutes {
        @Volatile private var invalidation: (() -> Unit)? = null
        @Volatile private var lost = false

        override fun open(endpoint: CloudEndpoint, onLost: () -> Unit): CloudRoute {
            invalidation = onLost
            return object : CloudRoute {
                override val socketFactory = object : SocketFactory() {
                    override fun createSocket(): Socket = object : Socket() {
                        override fun connect(endpoint: SocketAddress?, timeout: Int) {
                            super.connect(InetSocketAddress("127.0.0.1", server.port), timeout)
                        }
                    }
                    override fun createSocket(a: String?, b: Int): Socket = error("expected unconnected socket")
                    override fun createSocket(a: String?, b: Int, c: InetAddress?, d: Int): Socket = error("unexpected socket API")
                    override fun createSocket(a: InetAddress?, b: Int): Socket = error("unexpected socket API")
                    override fun createSocket(a: InetAddress?, b: Int, c: InetAddress?, d: Int): Socket = error("unexpected socket API")
                }
                override fun checkAvailable() { if (lost) throw CloudFailure(CloudIssue.VPN) }
                override fun close() { invalidation = null }
            }
        }

        fun lose() {
            lost = true
            requireNotNull(invalidation).invoke()
        }
    }
}
