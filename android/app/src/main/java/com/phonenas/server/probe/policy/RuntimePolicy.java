package com.phonenas.server.probe.policy;

/**
 * Approved phase-1 lifecycle/storage gate seam. Pure decisions only: no Android,
 * persistence, permission requests, engine start, file access, or lock mutation.
 * This conservative probe is NOT the final product restoration policy. Eligible
 * automatic recovery after boot/unlock still needs a real engine and validation.
 */
@FunctionalInterface
public interface RuntimePolicy {
    Decision evaluate(Input input);

    static RuntimePolicy conservative() {
        return new GuardedRuntimePolicy();
    }

    enum Action {
        STOP_REQUESTED,
        WAIT_FOR_UNLOCK,
        WAIT_FOR_ENGINE,
        WAIT_FOR_STORAGE_GRANT,
        WAIT_FOR_NOTIFICATION_PERMISSION,
        WAIT_FOR_LOCAL_NETWORK,
        READY_FOR_USER_START
    }

    /**
     * Storage/network flags describe observed gates, not proof of file I/O or
     * peer reachability. Battery exemption is deliberately not a prerequisite.
     */
    final class Input {
        public final boolean desiredRunning;
        public final boolean userUnlocked;
        public final boolean storageGrantPresent;
        public final boolean notificationsAllowed;
        public final boolean localNetworkPresent;
        public final boolean engineAvailable;

        public Input(
                boolean desiredRunning,
                boolean userUnlocked,
                boolean storageGrantPresent,
                boolean notificationsAllowed,
                boolean localNetworkPresent,
                boolean engineAvailable) {
            this.desiredRunning = desiredRunning;
            this.userUnlocked = userUnlocked;
            this.storageGrantPresent = storageGrantPresent;
            this.notificationsAllowed = notificationsAllowed;
            this.localNetworkPresent = localNetworkPresent;
            this.engineAvailable = engineAvailable;
        }
    }

    /** An action recommendation is never evidence that an engine has started. */
    final class Decision {
        public final Action action;
        public final String explanation;

        Decision(Action action, String explanation) {
            this.action = action;
            this.explanation = explanation;
        }
    }
}
