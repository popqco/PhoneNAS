package com.phonenas.server.probe

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure evidence rules: no Android context, preferences, service, or device is used. */
class ReliabilityEvidenceTest {
    @Test fun unknownObservationsAreNotPermissionDenialsOrApprovals() {
        val observed = ReliabilityObservations.unknown()
        assertEquals(EvidenceValue.UNKNOWN, observed.notificationRuntime.value)
        assertEquals(EvidenceValue.UNKNOWN, observed.notificationApp.value)
        assertEquals(EvidenceValue.UNKNOWN, observed.notificationChannel.value)
        assertEquals(EvidenceValue.UNKNOWN, observed.batteryExempt.value)
        assertEquals(EvidenceValue.UNKNOWN, observed.backgroundRestricted.value)
        assertNull(observed.notificationChannel.observedAt)
    }

    @Test fun permissionLayersStaySeparate() {
        val observed = ReliabilityObservations.unknown().copy(
            notificationRuntime = ObservedValue(EvidenceValue.YES, 100L),
            notificationApp = ObservedValue(EvidenceValue.NO, 101L),
            notificationChannel = ObservedValue(EvidenceValue.UNKNOWN, 102L, "通道尚未创建")
        )
        assertEquals(EvidenceValue.YES, observed.notificationRuntime.value)
        assertEquals(EvidenceValue.NO, observed.notificationApp.value)
        assertEquals(EvidenceValue.UNKNOWN, observed.notificationChannel.value)
        assertEquals(102L, observed.notificationChannel.observedAt)
    }

    @Test fun manualReportAndMissingMenuAreNotSystemObservations() {
        val reported = decodeManualReview("USER_REPORTED", 123L)
        val unavailable = decodeManualReview("UNAVAILABLE", 456L)
        assertEquals(ManualReviewState.USER_REPORTED, reported.state)
        assertEquals(123L, reported.recordedAt)
        assertEquals(ManualReviewState.UNAVAILABLE, unavailable.state)
        assertEquals(456L, unavailable.recordedAt)
        assertEquals(EvidenceValue.UNKNOWN,
            ReliabilityObservations.unknown().backgroundRestricted.value)
    }

    @Test fun invalidOrUntimedManualRecordReturnsUnreviewed() {
        for (record in listOf(
            decodeManualReview(null, 100L),
            decodeManualReview("true", 100L),
            decodeManualReview("USER_REPORTED", 0L),
            decodeManualReview("UNAVAILABLE", -1L)
        )) {
            assertEquals(ManualReviewState.UNREVIEWED, record.state)
            assertNull(record.recordedAt)
        }
        assertEquals(ManualReview(ManualReviewState.UNREVIEWED, 999L),
            decodeManualReview("UNREVIEWED", 999L))
    }

    @Test fun successfulForegroundReadAndAllReviewsCannotClearKnownHomeBlocker() {
        val yes = ObservedValue(EvidenceValue.YES, 123L)
        val evidence = ReliabilityEvidence(
            observations = ReliabilityObservations(yes, yes, yes, yes,
                ObservedValue(EvidenceValue.NO, 123L), true),
            manualReviews = ManualSetting.entries.associateWith {
                ManualReview(ManualReviewState.USER_REPORTED, 123L)
            },
            localCheck = LoopbackResult(LoopbackOutcome.VERIFIED, 123L, 200)
        )
        assertEquals(BackgroundReadiness.KNOWN_BLOCKED, evidence.backgroundReadiness)
        assertEquals(BackgroundTestEvidence.DEFAULT_VIVO_HOME_BLOCKED, evidence.backgroundTest)
    }

    @Test fun lackOfBackgroundTestRemainsUnknownEvenWithLocalSuccess() {
        val evidence = ReliabilityEvidence(
            localCheck = LoopbackResult(LoopbackOutcome.VERIFIED, 123L, 200),
            backgroundTest = BackgroundTestEvidence.NOT_TESTED
        )
        assertEquals(BackgroundReadiness.UNVERIFIED, evidence.backgroundReadiness)
    }

    @Test fun localCheckRequiresCurrentRunningIntentAndConfirmedListener() {
        assertTrue(localVerificationAllowed(desiredRunning = true, listenerRunning = true,
            stopping = false, hasError = false))
        assertFalse(localVerificationAllowed(false, true, false, false))
        assertFalse(localVerificationAllowed(true, false, false, false))
        assertFalse(localVerificationAllowed(true, true, true, false))
        assertFalse(localVerificationAllowed(true, true, false, true))
    }

    @Test fun vendorHintsTolerateBrandSpellingAndUnknownVendors() {
        assertTrue(vendorSettingsHint("vIvO", "iQOO").contains("i管家"))
        assertTrue(vendorSettingsHint("unknown", " IQOO ").contains("i管家"))
        assertTrue(vendorSettingsHint("XIAOMI", "Redmi").contains("自启动"))
        assertTrue(vendorSettingsHint("new-vendor", "").contains("搜索"))
    }
}
