package com.phonenas.server.probe

import java.io.IOException
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.TreeMap
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Uses disposable host loopback listeners only. No emulator, phone, or production port. */
class LoopbackVerifierTest {
    private val deadlines: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor()

    @After fun closeDeadlines() {
        deadlines.shutdownNow()
    }

    @Test fun authenticatedGetVerifiesExactFixtureAndReportsOnlyScopedResult() {
        val request = AtomicReference<List<String>>()
        LocalServer { exchange ->
            request.set(listOf(exchange.requestMethod, exchange.requestPath,
                exchange.requestHeaders["Authorization"].orEmpty()))
            exchange.reply(200,
                "PhoneNAS isolated readonly demo. No personal folders are exported.\n")
        }.use { server ->
            val result = LoopbackVerifier.forLocalTest(deadlines, server.port)
                .verify("phonenas", "pass")
            assertEquals(LoopbackOutcome.VERIFIED, result.outcome)
            assertEquals(200, result.httpStatus)
            assertTrue(result.checkedAt > 0L)
            assertEquals(listOf("GET", "/readonly/readme.txt", "Basic cGhvbmVuYXM6cGFzcw=="),
                request.get())
            assertEquals(1, server.requests.get())
        }
    }

    @Test fun unauthorizedResponseIsNotSuccessAndDoesNotRetry() {
        LocalServer { it.reply(401, "Unauthorized") }.use { server ->
            val result = LoopbackVerifier.forLocalTest(deadlines, server.port).verify("phonenas", "pass")
            assertEquals(LoopbackOutcome.UNAUTHORIZED, result.outcome)
            assertEquals(401, result.httpStatus)
            assertEquals(1, server.requests.get())
        }
    }

    @Test fun wrongOrOversizedDemoBodyIsRejected() {
        for (body in listOf("wrong fixture", "x".repeat(4096))) {
            LocalServer { it.reply(200, body) }.use { server ->
                assertEquals(LoopbackOutcome.UNEXPECTED_BODY,
                    LoopbackVerifier.forLocalTest(deadlines, server.port).verify("phonenas", "pass").outcome)
                assertEquals(1, server.requests.get())
            }
        }
    }

    @Test fun redirectsAreRejectedWithoutContactingTheirTarget() {
        LocalServer { it.reply(200, "must not be requested") }.use { target ->
            LocalServer { exchange ->
                exchange.responseHeaders["Location"] = "http://127.0.0.1:${target.port}/private"
                exchange.reply(302, "Moved")
            }.use { server ->
                val result = LoopbackVerifier.forLocalTest(deadlines, server.port).verify("phonenas", "pass")
                assertEquals(LoopbackOutcome.REDIRECT_REJECTED, result.outcome)
                assertEquals(302, result.httpStatus)
                assertEquals(1, server.requests.get())
                assertEquals(0, target.requests.get())
            }
        }
    }

    @Test fun nonrespondingPeerHitsBoundWithoutSleepBasedRaces() {
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val client = Executors.newSingleThreadExecutor()
        try {
            LocalServer {
                entered.countDown()
                release.await(5, TimeUnit.SECONDS)
            }.use { server ->
                val result = client.submit<LoopbackResult> {
                    LoopbackVerifier.forLocalTest(deadlines, server.port,
                        connectTimeoutMs = 1_000, readTimeoutMs = 150, totalTimeoutMs = 2_000)
                        .verify("phonenas", "pass")
                }
                assertTrue("Request must reach the disposable server", entered.await(3, TimeUnit.SECONDS))
                try {
                    assertEquals(LoopbackOutcome.TIMEOUT, result.get(4, TimeUnit.SECONDS).outcome)
                    assertEquals(1, server.requests.get())
                } finally { release.countDown() }
            }
        } finally {
            release.countDown()
            client.shutdownNow()
        }
    }

    @Test fun cancellationDiscardsARequestWithoutRetrying() {
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val client = Executors.newSingleThreadExecutor()
        try {
            LocalServer {
                entered.countDown()
                release.await(5, TimeUnit.SECONDS)
            }.use { server ->
                val verifier = LoopbackVerifier.forLocalTest(deadlines, server.port)
                val result = client.submit<LoopbackResult> { verifier.verify("phonenas", "pass") }
                assertTrue(entered.await(3, TimeUnit.SECONDS))
                verifier.cancel()
                try {
                    assertEquals(LoopbackOutcome.CANCELLED, result.get(4, TimeUnit.SECONDS).outcome)
                    assertEquals(1, server.requests.get())
                } finally { release.countDown() }
            }
        } finally {
            release.countDown()
            client.shutdownNow()
        }
    }

    @Test fun closedDisposablePortIsReportedAsRefused() {
        val loopback = InetAddress.getByAddress(byteArrayOf(127, 0, 0, 1))
        val port = ServerSocket(0, 1, loopback).use { it.localPort }
        assertEquals(LoopbackOutcome.CONNECTION_REFUSED,
            LoopbackVerifier.forLocalTest(deadlines, port).verify("phonenas", "pass").outcome)
    }

    @Test(expected = IllegalArgumentException::class)
    fun testSeamCannotUseInvalidPort() {
        LoopbackVerifier.forLocalTest(deadlines, 0)
    }

    /** Minimal HTTP/1.1 fixture using only java.net APIs present on Android's test classpath. */
    private class LocalServer(handler: (LocalExchange) -> Unit) : AutoCloseable {
        val requests = AtomicInteger()
        private val executor = Executors.newCachedThreadPool()
        private val closed = AtomicBoolean(false)
        private val connections = ConcurrentHashMap.newKeySet<Socket>()
        private val server = ServerSocket(0, 8,
            InetAddress.getByAddress(byteArrayOf(127, 0, 0, 1)))
        val port: Int get() = server.localPort

        init {
            executor.execute {
                while (!closed.get()) {
                    val socket = try { server.accept() } catch (_: IOException) { break }
                    connections.add(socket)
                    if (closed.get()) {
                        socket.close()
                        connections.remove(socket)
                        break
                    }
                    executor.execute {
                        try {
                            socket.soTimeout = 2_000
                            val exchange = LocalExchange(socket)
                            requests.incrementAndGet()
                            handler(exchange)
                        } catch (_: IOException) {
                            // Refusal/cancellation/body-bound tests may deliberately close the peer.
                        } catch (_: InterruptedException) {
                            Thread.currentThread().interrupt()
                        } finally {
                            connections.remove(socket)
                            socket.close()
                        }
                    }
                }
            }
        }

        override fun close() {
            closed.set(true)
            server.close()
            connections.forEach { runCatching { it.close() } }
            executor.shutdownNow()
            assertTrue("Disposable responder must terminate", executor.awaitTermination(3, TimeUnit.SECONDS))
        }
    }

    private class LocalExchange(private val socket: Socket) {
        val requestMethod: String
        val requestPath: String
        val requestHeaders = TreeMap<String, String>(String.CASE_INSENSITIVE_ORDER)
        val responseHeaders = linkedMapOf<String, String>()

        init {
            val reader = socket.getInputStream().bufferedReader(Charsets.US_ASCII)
            val requestLine = reader.readLine()?.split(' ', limit = 3)
                ?: throw IOException("Missing request line")
            if (requestLine.size != 3) throw IOException("Invalid request line")
            requestMethod = requestLine[0]
            requestPath = requestLine[1]
            var complete = false
            repeat(64) {
                if (!complete) {
                    val line = reader.readLine() ?: throw IOException("Incomplete headers")
                    if (line.isEmpty()) complete = true
                    else {
                        val separator = line.indexOf(':')
                        if (separator <= 0) throw IOException("Invalid header")
                        requestHeaders[line.substring(0, separator)] = line.substring(separator + 1).trim()
                    }
                }
            }
            if (!complete) throw IOException("Too many headers")
        }

        fun reply(status: Int, body: String) {
            val bytes = body.toByteArray(Charsets.UTF_8)
            val headers = buildString {
                append("HTTP/1.1 $status Fixture\r\n")
                append("Content-Length: ${bytes.size}\r\nConnection: close\r\n")
                responseHeaders.forEach { (name, value) -> append("$name: $value\r\n") }
                append("\r\n")
            }.toByteArray(Charsets.US_ASCII)
            socket.getOutputStream().apply {
                write(headers)
                write(bytes)
                flush()
            }
        }
    }
}
