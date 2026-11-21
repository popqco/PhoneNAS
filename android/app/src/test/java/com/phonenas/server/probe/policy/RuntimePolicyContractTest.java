package com.phonenas.server.probe.policy;

/** Standalone public-interface contract; needs only javac/java, not Android/JUnit. */
public final class RuntimePolicyContractTest {
    public static void main(String[] args) {
        RuntimePolicy policy = RuntimePolicy.conservative();
        expect(
                policy,
                new RuntimePolicy.Input(false, false, false, false, false, false),
                RuntimePolicy.Action.STOP_REQUESTED,
                "A stopped user's intent must win over every missing prerequisite");
        expect(
                policy,
                new RuntimePolicy.Input(true, false, false, false, false, false),
                RuntimePolicy.Action.WAIT_FOR_UNLOCK,
                "A boot before first unlock must wait without touching credential storage");
        expect(
                policy,
                new RuntimePolicy.Input(true, true, true, true, true, false),
                RuntimePolicy.Action.WAIT_FOR_ENGINE,
                "An unlocked host with all grants must not claim a missing engine is running");
        expect(
                policy,
                new RuntimePolicy.Input(true, true, false, true, true, true),
                RuntimePolicy.Action.WAIT_FOR_STORAGE_GRANT,
                "Engine availability never bypasses the user-authorized storage gate");
        expect(
                policy,
                new RuntimePolicy.Input(true, true, true, false, true, true),
                RuntimePolicy.Action.WAIT_FOR_NOTIFICATION_PERMISSION,
                "This conservative policy requires user-visible notifications before readiness");
        expect(
                policy,
                new RuntimePolicy.Input(true, true, true, true, false, true),
                RuntimePolicy.Action.WAIT_FOR_LOCAL_NETWORK,
                "An available engine with no observed Wi-Fi/Ethernet must wait");
        expect(
                policy,
                new RuntimePolicy.Input(true, true, true, true, true, true),
                RuntimePolicy.Action.READY_FOR_USER_START,
                "Satisfied gates mean manual-start readiness only, never running or boot autostart");
        // Exhaust the public gate seam for the two most important safety invariants.
        for (int mask = 0; mask < 32; mask++) {
            expect(policy,
                    new RuntimePolicy.Input(false, bit(mask, 0), bit(mask, 1),
                            bit(mask, 2), bit(mask, 3), bit(mask, 4)),
                    RuntimePolicy.Action.STOP_REQUESTED,
                    "User stop must dominate prerequisite combination " + mask);
        }
        for (int mask = 0; mask < 8; mask++) {
            expect(policy,
                    new RuntimePolicy.Input(true, true, bit(mask, 0), bit(mask, 1),
                            bit(mask, 2), false),
                    RuntimePolicy.Action.WAIT_FOR_ENGINE,
                    "A missing engine must block readiness for grant/network combination " + mask);
        }
        // Same intent across boot, first unlock, and a later explicit user stop.
        expect(policy, new RuntimePolicy.Input(true, false, true, true, true, false),
                RuntimePolicy.Action.WAIT_FOR_UNLOCK, "Boot waits without altering desiredRunning");
        expect(policy, new RuntimePolicy.Input(true, true, true, true, true, false),
                RuntimePolicy.Action.WAIT_FOR_ENGINE, "Unlock cannot start a nonexistent engine");
        expect(policy, new RuntimePolicy.Input(false, true, true, true, true, true),
                RuntimePolicy.Action.STOP_REQUESTED, "Later engine availability cannot undo user stop");
        System.out.println("RuntimePolicy contract: 50 scenarios passed (pure Java only).");
    }

    private static boolean bit(int value, int index) {
        return (value & (1 << index)) != 0;
    }

    private static void expect(
            RuntimePolicy policy,
            RuntimePolicy.Input input,
            RuntimePolicy.Action expected,
            String scenario) {
        RuntimePolicy.Decision decision = policy.evaluate(input);
        if (decision.action != expected) {
            throw new AssertionError(scenario + ": expected " + expected + ", got " + decision.action);
        }
        if (decision.explanation == null || decision.explanation.trim().isEmpty()) {
            throw new AssertionError(scenario + ": action must explain why");
        }
    }
}
