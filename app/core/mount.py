from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from app import constants as C
from app.core.config import Config, data_dir
from app.core.log import get_logger
from app.core.rclone import Rclone

BACKOFF_SECONDS = [5, 15, 30, 60, 120]
MAX_RETRIES = len(BACKOFF_SECONDS)


class MountSupervisor(QObject):
    state_changed = Signal(str)  # stopped / starting / mounted / retrying / error
    log_line = Signal(str)

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self.rclone = Rclone(config)
        self.log = get_logger()
        self._proc = None
        self._state = "stopped"
        self._want_mounted = False
        self._retry_index = 0

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1500)
        self._poll_timer.timeout.connect(self._poll)

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._spawn)

    # -- public -----------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def drive(self) -> str:
        return self.config.get("mount.drive", C.DEFAULT_DRIVE)

    def drive_ready(self) -> bool:
        try:
            return os.path.isdir(self.drive + "\\")
        except OSError:
            return False

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._want_mounted = True
        self._retry_index = 0
        if self.drive_ready():
            self._set_state("error")
            self.log_line.emit(f"盘符 {self.drive} 已被占用，请在设置中更换")
            return
        self._spawn()

    def stop(self) -> None:
        self._want_mounted = False
        self._poll_timer.stop()
        self._retry_timer.stop()
        self._kill_proc()
        self._set_state("stopped")

    def restart(self) -> None:
        self.stop()
        QTimer.singleShot(500, self.start)

    # -- internals --------------------------------------------------------
    def _spawn(self) -> None:
        if not self._want_mounted:
            return
        self.rclone.ensure_conf()
        args = [
            "mount",
            f"{C.DEFAULT_REMOTE}:",
            self.drive,
            "--vfs-cache-mode",
            "writes",
            "--volname",
            "PhoneNAS",
            "--dir-cache-time",
            "1m",
            "--log-level",
            "INFO",
            "--stats",
            "0",
        ]
        if self.config.get("mount.network_mode", False):
            args.append("--network-mode")
        log_path = data_dir() / "logs" / f"mount-{datetime.now():%Y%m%d}.log"
        try:
            self._proc = self.rclone.popen(args, log_path=log_path)
        except Exception as exc:  # noqa: BLE001
            self._set_state("error")
            self.log_line.emit(f"挂载启动失败: {exc}")
            self._schedule_retry()
            return
        self._set_state("starting")
        self.log_line.emit(f"正在挂载到 {self.drive}…")
        self._poll_timer.start()

    def _poll(self) -> None:
        if self._proc is None:
            return
        code = self._proc.poll()
        if code is None:
            if self._state == "starting" and self.drive_ready():
                self._retry_index = 0
                self._set_state("mounted")
                self.log_line.emit(f"已挂载 {self.drive}")
            return
        # process exited
        self._poll_timer.stop()
        self._proc = None
        if not self._want_mounted:
            self._set_state("stopped")
            return
        self.log_line.emit(f"挂载进程退出 (code={code})")
        self._schedule_retry()

    def _schedule_retry(self) -> None:
        if not self._want_mounted:
            return
        if self._retry_index >= MAX_RETRIES:
            self._set_state("error")
            self.log_line.emit("挂载多次失败，请检查网络或 NAS 地址")
            return
        delay = BACKOFF_SECONDS[self._retry_index]
        self._retry_index += 1
        self._set_state("retrying")
        self.log_line.emit(f"{delay} 秒后重试挂载…")
        self._retry_timer.start(delay * 1000)

    def _kill_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)
