package com.phonenas.server.probe

import java.io.IOException
import java.net.ConnectException
import java.net.HttpURLConnection
import java.net.Proxy
import java.net.SocketTimeoutException
import java.net.URL
import java.util.Base64
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

internal enum class LoopbackOutcome {
    VERIFIED, UNAUTHORIZED, UNEXPECTED_BODY, REDIRECT_REJECTED, HTTP_ERROR,
    TIMEOUT, CONNECTION_REFUSED, NETWORK_ERROR, CANCELLED
}

/** Contains no password, response body, redirect target, exception text, or credential header. */
internal data class LoopbackResult(
    val outcome: LoopbackOutcome,
    val checkedAt: Long,
    val httpStatus: Int? = null
)

/**
 * One explicit, authenticated read of the fixed demo fixture, never a health poll or recovery.
 * Production construction fixes the host, port and path. The test seam changes only the LOCAL port.
 */
internal class LoopbackVerifier private constructor(
    private val deadlines: ScheduledExecutorService,
    private val localPort: Int,
    private val connectTimeoutMs: Int,
    private val readTimeoutMs: Int,
    private val totalTimeoutMs: Int
) {
    private val cancelled = AtomicBoolean(false)
    private val started = AtomicBoolean(false)
    private val active = AtomicReference<HttpURLConnection?>()

    constructor(deadlines: ScheduledExecutorService) : this(deadlines, 18888, 1_500, 1_500, 4_000)

    fun cancel() {
        cancelled.set(true)
        val connection = active.getAndSet(null) ?: return
        // Android connection teardown can block: never perform it on the Activity/UI caller.
        try { deadlines.execute { connection.disconnect() } }
        catch (_: java.util.concurrent.RejectedExecutionException) {
            // The owner is closing; the interrupted request still has finite timeouts and finally cleanup.
        }
    }

    fun verify(username: String, password: String): LoopbackResult {
        check(started.compareAndSet(false, true)) { "One-shot verifier already used" }
        val expired = AtomicBoolean(false)
        var connection: HttpURLConnection? = null
        var deadline: java.util.concurrent.ScheduledFuture<*>? = null
        fun result(outcome: LoopbackOutcome, status: Int? = null) =
            LoopbackResult(outcome, System.currentTimeMillis(), status)
        fun interrupted() = cancelled.get() || Thread.currentThread().isInterrupted
        try {
            if (interrupted()) return result(LoopbackOutcome.CANCELLED)
            connection = URL("http://127.0.0.1:$localPort/readonly/readme.txt")
                .openConnection(Proxy.NO_PROXY) as HttpURLConnection
            val request = connection
            active.set(request)
            if (interrupted()) return result(LoopbackOutcome.CANCELLED)
            request.requestMethod = "GET"
            request.instanceFollowRedirects = false
            request.connectTimeout = connectTimeoutMs
            request.readTimeout = readTimeoutMs
            request.useCaches = false
            request.doOutput = false
            request.setRequestProperty("Accept-Encoding", "identity")
            request.setRequestProperty("Cache-Control", "no-cache, no-store")
            request.setRequestProperty("Authorization", "Basic " + Base64.getEncoder()
                .encodeToString("$username:$password".toByteArray(Charsets.UTF_8)))
            // A wall deadline bounds even a peer that drips bytes inside every read timeout.
            deadline = deadlines.schedule({
                expired.set(true)
                request.disconnect()
            }, totalTimeoutMs.toLong(), TimeUnit.MILLISECONDS)
            val status = request.responseCode
            if (interrupted()) return result(LoopbackOutcome.CANCELLED)
            if (expired.get()) return result(LoopbackOutcome.TIMEOUT)
            when {
                status == 401 -> return result(LoopbackOutcome.UNAUTHORIZED, status)
                status in 300..399 -> return result(LoopbackOutcome.REDIRECT_REJECTED, status)
                status != 200 -> return result(LoopbackOutcome.HTTP_ERROR, status)
            }
            val expected = FIXTURE.toByteArray(Charsets.UTF_8)
            if (request.contentLengthLong > expected.size ||
                (!request.contentEncoding.isNullOrBlank() && request.contentEncoding != "identity")) {
                return result(LoopbackOutcome.UNEXPECTED_BODY, status)
            }
            val body = ByteArray(expected.size + 1)
            var size = 0
            request.inputStream.use { input ->
                while (size < body.size) {
                    if (interrupted()) return result(LoopbackOutcome.CANCELLED)
                    if (expired.get()) return result(LoopbackOutcome.TIMEOUT)
                    val read = input.read(body, size, body.size - size)
                    if (read < 0) break
                    size += read
                }
            }
            if (interrupted()) return result(LoopbackOutcome.CANCELLED)
            if (expired.get()) return result(LoopbackOutcome.TIMEOUT)
            return result(if (size == expected.size && expected.indices.all { body[it] == expected[it] })
                LoopbackOutcome.VERIFIED else LoopbackOutcome.UNEXPECTED_BODY, status)
        } catch (_: SocketTimeoutException) {
            return result(if (interrupted()) LoopbackOutcome.CANCELLED else LoopbackOutcome.TIMEOUT)
        } catch (_: ConnectException) {
            return result(when {
                interrupted() -> LoopbackOutcome.CANCELLED
                expired.get() -> LoopbackOutcome.TIMEOUT
                else -> LoopbackOutcome.CONNECTION_REFUSED
            })
        } catch (_: IOException) {
            return result(when {
                interrupted() -> LoopbackOutcome.CANCELLED
                expired.get() -> LoopbackOutcome.TIMEOUT
                else -> LoopbackOutcome.NETWORK_ERROR
            })
        } catch (_: SecurityException) {
            return result(if (interrupted()) LoopbackOutcome.CANCELLED else LoopbackOutcome.NETWORK_ERROR)
        } finally {
            deadline?.cancel(false)
            active.compareAndSet(connection, null)
            connection?.disconnect()
        }
    }

    companion object {
        private const val FIXTURE = "PhoneNAS isolated readonly demo. No personal folders are exported.\n"

        /** Host/path cannot be supplied, so even tests cannot redirect authenticated traffic elsewhere. */
        internal fun forLocalTest(
            deadlines: ScheduledExecutorService,
            localPort: Int,
            connectTimeoutMs: Int = 1_000,
            readTimeoutMs: Int = 1_000,
            totalTimeoutMs: Int = 3_000
        ): LoopbackVerifier {
            require(localPort in 1..65535)
            require(connectTimeoutMs > 0 && readTimeoutMs > 0 && totalTimeoutMs > 0)
            return LoopbackVerifier(deadlines, localPort, connectTimeoutMs, readTimeoutMs, totalTimeoutMs)
        }
    }
}
