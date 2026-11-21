package com.phonenas.server.probe

import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class EngineLifetimeTest {
    @Test fun aSecondServiceInstanceCannotConstructAnotherEngineBeforeConfirmedStop() {
        val stopper = Executors.newSingleThreadExecutor()
        try {
            val lifetime = EngineLifetime<Any>(stopper, {})
            val first = Any()
            assertNotNull(lifetime.acquire(first) { Any() })
            var secondConstructed = false

            assertNull(lifetime.acquire(Any()) { secondConstructed = true; Any() })
            assertFalse(secondConstructed)
            assertFalse(lifetime.stoppedConfirmed)
            assertTrue(lifetime.close(first))
            assertTrue(lifetime.stoppedConfirmed)
            assertNotNull(lifetime.acquire(Any()) { Any() })
        } finally { stopper.shutdownNow() }
    }

    @Test fun anUnrelatedServiceCannotCloseTheCurrentOwnersEngine() {
        val stopper = Executors.newSingleThreadExecutor()
        try {
            var stopped = false
            val lifetime = EngineLifetime<Any>(stopper, { stopped = true })
            val first = Any()
            assertNotNull(lifetime.acquire(first) { Any() })
            assertFalse(lifetime.close(Any()))
            assertFalse(stopped)
            assertTrue(lifetime.close(first))
        } finally { stopper.shutdownNow() }
    }

    @Test fun concurrentCloseDoesNotSubmitNativeStopTwice() {
        val stopper = Executors.newSingleThreadExecutor()
        val callers = Executors.newCachedThreadPool()
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        try {
            val lifetime = EngineLifetime<Any>(stopper, { entered.countDown(); release.await() })
            val owner = Any()
            lifetime.acquire(owner) { Any() }
            val first = callers.submit<Boolean> { lifetime.close(owner) }
            assertTrue(entered.await(1, TimeUnit.SECONDS))
            val second = callers.submit<Boolean> { lifetime.close(owner) }
            assertFalse("An already pending native stop is not a new stop operation", second.get(250, TimeUnit.MILLISECONDS))
            release.countDown()
            assertTrue(first.get(1, TimeUnit.SECONDS))
        } finally { release.countDown(); callers.shutdownNow(); stopper.shutdownNow() }
    }

    @Test fun aNativeStopTimeoutAndLateReturnNeverReleaseTheProcessLease() {
        val stopper = Executors.newSingleThreadExecutor()
        val release = CountDownLatch(1)
        val returned = CountDownLatch(1)
        try {
            val lifetime = EngineLifetime<Any>(stopper, {
                release.await()
                returned.countDown()
            }, stopTimeoutMillis = 40)
            val owner = Any()
            assertNotNull(lifetime.acquire(owner) { Any() })
            assertFalse(lifetime.close(owner))
            assertTrue(lifetime.quarantined)
            assertFalse(lifetime.stoppedConfirmed)
            assertNull(lifetime.acquire(Any()) { Any() })
            release.countDown()
            assertTrue(returned.await(1, TimeUnit.SECONDS))
            assertFalse(lifetime.stoppedConfirmed)
            assertNull(lifetime.acquire(Any()) { Any() })
            assertFalse(lifetime.close(owner))
        } finally { release.countDown(); stopper.shutdownNow() }
    }

    @Test fun nativeStopExceptionQuarantinesEveryLaterServiceInstance() {
        val stopper = Executors.newSingleThreadExecutor()
        try {
            val lifetime = EngineLifetime<Any>(stopper, { throw IllegalStateException("native stop") })
            val first = Any()
            assertNotNull(lifetime.acquire(first) { Any() })

            assertFalse(lifetime.close(first))
            assertTrue(lifetime.quarantined)
            assertFalse(lifetime.stoppedConfirmed)
            assertNull(lifetime.acquire(Any()) { Any() })
            assertFalse(lifetime.close(Any()))
        } finally { stopper.shutdownNow() }
    }
}
