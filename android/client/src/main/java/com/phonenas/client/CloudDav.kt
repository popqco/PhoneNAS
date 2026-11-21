package com.phonenas.client

import okhttp3.Authenticator
import okhttp3.ConnectionPool
import okhttp3.ConnectionSpec
import okhttp3.Dns
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.Response
import okhttp3.TlsVersion
import okio.BufferedSink
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.ProtocolException
import java.net.Proxy
import java.net.UnknownHostException
import java.util.Base64
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLException

internal const val MAX_UPLOAD_BYTES = 64L * 1024 * 1024 * 1024

internal class CloudOperation {
    private var failure: CloudIssue? = null
    private val cancelActions = LinkedHashMap<Any, () -> Unit>()

    @Synchronized fun check() {
        failure?.let { throw CloudFailure(it) }
    }

    fun <T> duringCancellation(action: () -> Unit, block: () -> T): T {
        val registration = Any()
        synchronized(this) {
            check()
            cancelActions[registration] = action
        }
        return try {
            block()
        } finally {
            synchronized(this) { cancelActions.remove(registration) }
        }
    }

    fun cancel(issue: CloudIssue = CloudIssue.CANCELLED) {
        val actions = synchronized(this) {
            if (failure != null) return
            failure = issue
            cancelActions.values.toList().also { cancelActions.clear() }
        }
        actions.forEach { runCatching(it) }
    }
}

internal class CloudDav(private val profile: CloudProfile, private val routes: CloudRoutes) {
    fun list(directory: DavPath, operation: CloudOperation): List<DavEntry> {
        check(directory.isCollection)
        return request("PROPFIND", directory, 20, operation) { response, route ->
            accepted(response, 207)
            val body = response.body ?: throw CloudFailure(CloudIssue.RESPONSE)
            val media = body.contentType()
            if (media?.subtype != "xml" || media.type !in listOf("application", "text")) {
                throw CloudFailure(CloudIssue.RESPONSE)
            }
            val entries = try {
                DavListing.parse(directory, body.byteStream())
            } catch (_: IOException) {
                operation.check()
                throw CloudFailure(CloudIssue.RESPONSE)
            }
            route.checkAvailable()
            operation.check()
            entries
        }
    }

    fun download(entry: DavEntry, staging: DownloadStaging, operation: CloudOperation,
                 progress: (Long, Long) -> Unit): StagedDownload {
        return readFile(entry, operation) { input, length, verifyActive ->
            val staged = staging.receive(input, length, entry.size, operation::check, progress)
            try {
                verifyActive()
                staged
            } catch (error: IOException) {
                runCatching { staged.close() }
                throw error
            }
        }
    }

    private fun <T> readFile(entry: DavEntry, operation: CloudOperation,
                             receive: (InputStream, Long, () -> Unit) -> T): T {
        check(!entry.path.isCollection)
        return request("GET", entry.path, 600, operation) { response, route ->
            accepted(response, 200)
            val length = response.headers.values("Content-Length").singleOrNull()
                ?.takeIf { it.matches(Regex("[0-9]{1,19}")) }?.toLongOrNull()
                ?: throw CloudFailure(CloudIssue.DOWNLOAD_LENGTH)
            val body = response.body ?: throw CloudFailure(CloudIssue.DOWNLOAD_LENGTH)
            if (body.contentLength() != length || response.header("Content-Range") != null ||
                response.header("Transfer-Encoding") != null || entry.size?.let { it != length } == true) {
                throw CloudFailure(CloudIssue.DOWNLOAD_LENGTH)
            }
            try {
                // Provider output errors and network input errors have distinct outcomes.
                val stream = body.byteStream()
                val input = object : InputStream() {
                    override fun read() = networkRead { stream.read() }
                    override fun read(buffer: ByteArray, offset: Int, count: Int) =
                        networkRead { stream.read(buffer, offset, count) }

                    private inline fun networkRead(read: () -> Int): Int = try {
                        read()
                    } catch (failure: IOException) {
                        operation.check()
                        throw CloudFailure(if (failure is EOFException || failure is ProtocolException)
                            CloudIssue.DOWNLOAD_LENGTH else CloudIssue.UNREACHABLE)
                    }
                }
                receive(input, length) {
                    route.checkAvailable()
                    operation.check()
                }
            } catch (_: DownloadCapacityException) {
                throw CloudFailure(CloudIssue.DOWNLOAD_CAPACITY)
            } catch (_: DownloadLengthException) {
                operation.check()
                throw CloudFailure(CloudIssue.DOWNLOAD_LENGTH)
            } catch (_: DownloadStagingException) {
                throw CloudFailure(CloudIssue.STAGING)
            }
        }
    }

    fun <T> downloadTo(entry: DavEntry, operation: CloudOperation, progress: (Long, Long) -> Unit,
                       save: (Long, () -> Unit, (OutputStream) -> Unit) -> T): T =
        readFile(entry, operation) { input, length, verifyActive ->
            save(length, verifyActive) { output ->
                transferExactly(length, operation::check,
                    read = { buffer, count -> input.read(buffer, 0, count) },
                    write = { buffer, count -> output.write(buffer, 0, count) },
                    lengthError = {
                        operation.check()
                        CloudFailure(CloudIssue.DOWNLOAD_LENGTH)
                    },
                    progress = { progress(it, length) })
                if (length == 0L) progress(0L, 0L)
            }
        }

    fun checkWritable(directory: DavPath, operation: CloudOperation) {
        check(directory.isCollection && directory.parent != null)
        request("OPTIONS", directory, 20, operation) { response, route ->
            if (response.code in listOf(403, 405)) throw CloudFailure(CloudIssue.UPLOAD_READ_ONLY)
            accepted(response, 200)
            val allow = response.headers.values("Allow").flatMap { it.split(',') }.map { it.trim() }
            if ("PROPFIND" !in allow) throw CloudFailure(CloudIssue.RESPONSE)
            if ("PUT" !in allow) throw CloudFailure(CloudIssue.UPLOAD_READ_ONLY)
            route.checkAvailable()
            operation.check()
        }
    }

    fun upload(path: DavPath, content: UploadContent, operation: CloudOperation, progress: (Long, Long) -> Unit) {
        check(!path.isCollection && path.parent?.parent != null)
        val length = content.length
        if (length !in 0..MAX_UPLOAD_BYTES) throw CloudFailure(CloudIssue.UPLOAD_CAPACITY)
        var mayBeCommitted = false
        var sentEntireBody = length == 0L
        if (length == 0L) {
            // The server can publish an empty file as soon as the headers arrive.
            content.withInput(operation) { if (it.read() != -1) throw CloudFailure(CloudIssue.UPLOAD_LENGTH) }
        }
        val body = object : RequestBody() {
            override fun contentType() = null
            override fun contentLength() = length
            override fun isOneShot() = true

            override fun writeTo(sink: BufferedSink) {
                if (length == 0L) return
                content.withInput(operation) { input ->
                    val buffer = ByteArray(64 * 1024)
                    var sent = 0L
                    while (sent < length) {
                        operation.check()
                        val max = minOf(length - sent, buffer.size.toLong()).toInt()
                        val read = input.read(buffer, 0, max)
                        if (read !in 1..max) throw CloudFailure(CloudIssue.UPLOAD_LENGTH)
                        if (sent + read == length) {
                            // Do not let the server commit a valid-length prefix of a growing source.
                            if (input.read() != -1) throw CloudFailure(CloudIssue.UPLOAD_LENGTH)
                            operation.check()
                            mayBeCommitted = true
                        }
                        operation.check()
                        sink.write(buffer, 0, read)
                        sent += read
                        if (sent == length) {
                            sink.flush()
                            sentEntireBody = true
                        }
                        progress(sent, length)
                    }
                }
            }
        }
        try {
            request("PUT", path, 125, operation, body, beforeSend = {
                if (length == 0L) {
                    mayBeCommitted = true
                    progress(0L, 0L)
                }
            }) { response, _ ->
                val issue = when (response.code) {
                    201 -> if (sentEntireBody) null else CloudIssue.UPLOAD_UNCONFIRMED
                    400 -> CloudIssue.UPLOAD_TARGET
                    409 -> CloudIssue.UPLOAD_CONFLICT
                    401 -> CloudIssue.AUTH
                    403, 405 -> CloudIssue.UPLOAD_READ_ONLY
                    404 -> CloudIssue.NOT_FOUND
                    412 -> CloudIssue.UPLOAD_EXISTS
                    413 -> CloudIssue.UPLOAD_CAPACITY
                    503 -> CloudIssue.UPLOAD_BUSY
                    507 -> CloudIssue.UPLOAD_STORAGE
                    else -> if (mayBeCommitted || response.isSuccessful) CloudIssue.UPLOAD_UNCONFIRMED else CloudIssue.RESPONSE
                }
                if (issue != null) throw CloudFailure(issue)
            }
        } catch (failure: CloudFailure) {
            if (mayBeCommitted && failure.issue in ambiguousUploadFailures) {
                throw CloudFailure(CloudIssue.UPLOAD_UNCONFIRMED)
            }
            throw failure
        }
    }

    private fun accepted(response: Response, expected: Int) {
        val issue = when (response.code) {
            expected -> null
            401 -> CloudIssue.AUTH
            403 -> CloudIssue.FORBIDDEN
            404 -> CloudIssue.NOT_FOUND
            else -> CloudIssue.RESPONSE
        }
        if (issue != null) throw CloudFailure(issue)
        val coding = response.headers.values("Content-Encoding")
        if (coding.isNotEmpty() && !(coding.size == 1 && coding[0].equals("identity", true))) {
            throw CloudFailure(CloudIssue.RESPONSE)
        }
    }

    private fun <T> request(method: String, path: DavPath, timeoutSeconds: Long, operation: CloudOperation,
                            body: RequestBody? = null, beforeSend: () -> Unit = {},
                            consume: (Response, CloudRoute) -> T): T {
        operation.check()
        val identity = profile.identity
        identity.checkValidity()
        val endpoint = identity.endpoint
        val url = endpoint.url.toHttpUrl().newBuilder().encodedPath(path.encoded).build()
        check(url.encodedPath == path.encoded)
        val credentials = profile.credentials
        val authorization = Base64.getEncoder().encodeToString(
            "${credentials.username}:${credentials.password}".toByteArray(Charsets.UTF_8))
        val request = Request.Builder().url(url).method(method, body)
            .header("Authorization", "Basic $authorization")
            .header("Accept-Encoding", "identity")
            .apply {
                if (method == "PROPFIND") header("Depth", "1")
                if (method == "PUT") {
                    header("If-None-Match", "*")
                    // A rejected larger body can otherwise race the server closing the connection.
                    if (checkNotNull(body).contentLength() > 0) header("Expect", "100-continue")
                }
            }.build()
        try {
            return routes.open(endpoint) { operation.cancel(CloudIssue.VPN) }.use { route ->
                route.checkAvailable()
                val trust = identity.trustManager()
                val tls = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), null) }
                val client = OkHttpClient.Builder()
                    .socketFactory(route.socketFactory)
                    .sslSocketFactory(tls.socketFactory, trust)
                    .connectionSpecs(listOf(ConnectionSpec.Builder(ConnectionSpec.MODERN_TLS)
                        .tlsVersions(TlsVersion.TLS_1_3, TlsVersion.TLS_1_2).build()))
                    .protocols(listOf(Protocol.HTTP_1_1))
                    .dns(object : Dns {
                        override fun lookup(hostname: String) = if (hostname == endpoint.host) {
                            listOf(endpoint.address)
                        } else throw UnknownHostException()
                    })
                    .proxy(Proxy.NO_PROXY)
                    .authenticator(Authenticator.NONE)
                    .followRedirects(false).followSslRedirects(false).retryOnConnectionFailure(false)
                    .connectionPool(ConnectionPool(0, 1, TimeUnit.SECONDS))
                    .connectTimeout(8, TimeUnit.SECONDS)
                    // The body can be fully written while the server is still draining it or
                    // syncing the staged file. A short idle read timeout would turn a slow but
                    // successful upload into a false "unconfirmed" result. The call timeout
                    // still bounds the whole request.
                    .readTimeout(if (method == "PUT") 60 else 12, TimeUnit.SECONDS)
                    .callTimeout(timeoutSeconds, TimeUnit.SECONDS)
                    .addNetworkInterceptor { chain ->
                        operation.check()
                        route.checkAvailable()
                        beforeSend()
                        val response = chain.proceed(chain.request())
                        // OkHttp otherwise handles 503 with Retry-After: 0 as a follow-up.
                        if (response.code == 503) {
                            response.close()
                            throw CloudFailure(if (method == "PUT") CloudIssue.UPLOAD_BUSY else CloudIssue.RESPONSE)
                        }
                        response
                    }.build()
                val call = client.newCall(request)
                try {
                    operation.duringCancellation(call::cancel) {
                        call.execute().use { consume(it, route) }
                    }
                } finally {
                    client.connectionPool.evictAll()
                }
            }
        } catch (failure: IOException) {
            if (failure is CloudFailure) throw failure
            operation.check()
            if (failure is SSLException) throw CloudFailure(CloudIssue.CERTIFICATE)
            throw CloudFailure(CloudIssue.UNREACHABLE)
        }
    }

    private companion object {
        val ambiguousUploadFailures = setOf(CloudIssue.CANCELLED, CloudIssue.VPN, CloudIssue.CERTIFICATE,
            CloudIssue.UNREACHABLE, CloudIssue.UPLOAD_SOURCE, CloudIssue.UPLOAD_STAGING)
    }
}
