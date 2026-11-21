package com.phonenas.server.probe.policy;

final class GuardedRuntimePolicy implements RuntimePolicy {
    @Override
    public Decision evaluate(Input input) {
        if (!input.desiredRunning) {
            return new Decision(Action.STOP_REQUESTED, "用户期望已停止；不得恢复或启动文件服务。");
        }
        if (!input.userUnlocked) {
            return new Decision(Action.WAIT_FOR_UNLOCK, "等待本次开机后首次解锁；不访问凭据加密存储。");
        }
        if (!input.engineAvailable) {
            return new Decision(Action.WAIT_FOR_ENGINE, "未接入文件服务内核；保留用户期望，但不启动任何服务。");
        }
        if (!input.storageGrantPresent) {
            return new Decision(Action.WAIT_FOR_STORAGE_GRANT, "缺少用户选择目录的持久读写授权；不得读写或清除任何锁。");
        }
        if (!input.notificationsAllowed) {
            return new Decision(Action.WAIT_FOR_NOTIFICATION_PERMISSION, "本探针策略要求通知可见；不等同于 Android 的强制 FGS 启动条件。");
        }
        if (!input.localNetworkPresent) {
            return new Decision(Action.WAIT_FOR_LOCAL_NETWORK, "未观察到 Wi-Fi/以太网连接；不进行后台重试或自动恢复。");
        }
        return new Decision(Action.READY_FOR_USER_START, "门槛已满足，仅建议回到可见页面由用户启动；这不是运行成功。");
    }
}
