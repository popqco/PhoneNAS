from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app import constants as C
from app.core.config import Config, data_dir
from app.core.log import get_logger
from app.core.rclone import Rclone
from app.core.retention import old_versions, version_timestamp


def build_sync_args(config: Config, timestamp: str) -> list[str]:
    remote = config.remote_path
    versions = config.versions_path
    args = [
        "sync",
        str(config.local_path),
        f"{C.DEFAULT_REMOTE}:{remote}",
        "--backup-dir",
        f"{C.DEFAULT_REMOTE}:{versions}/{timestamp}",
        "--transfers",
        "4",
        "--checkers",
        "8",
        "--create-empty-src-dirs",
        "--stats",
        "5s",
        "--stats-one-line",
        "--log-level",
        "INFO",
    ]
    for pattern in config.get("mirror.excludes", []) or []:
        args.extend(["--exclude", pattern])
    return args


class SyncWorker(QThread):
    progress = Signal(str)
    finished_ok = Signal(str, str)  # summary, timestamp
    failed = Signal(str)

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self.rclone = Rclone(config)
        self.log = get_logger()
        self._proc = None
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    def run(self) -> None:  # noqa: C901
        if not self.config.local_path.exists():
            self.failed.emit(f"本地文件夹不存在: {self.config.local_path}")
            return

        self.rclone.ensure_conf()
        for remote_path in (self.config.remote_path, self.config.versions_path):
            res = self.rclone.mkdir(remote_path)
            if not res.ok:
                self.failed.emit(f"创建远端目录失败: {res.err or res.out}")
                return

        stamp = version_timestamp()
        args = build_sync_args(self.config, stamp)
        log_path = data_dir() / "logs" / f"sync-{datetime.now():%Y%m%d}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.progress.emit("正在同步…")

        try:
            self._proc = self.rclone.popen(args)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return

        assert self._proc.stdout is not None
        with log_path.open("a", encoding="utf-8", errors="replace") as log_handle:
            for line in self._proc.stdout:
                if line.strip():
                    self.progress.emit(line.rstrip())
                    log_handle.write(line)
        code = self._proc.wait()
        if self._stopped:
            self.failed.emit("同步已取消")
            return
        if code != 0:
            self.failed.emit(f"rclone 退出码 {code}，详见日志 {log_path.name}")
            return

        # retention: delete version folders older than configured days
        removed = 0
        listing = self.rclone.lsf_dirs(self.config.versions_path)
        if listing.ok:
            names = [line.strip() for line in listing.out.splitlines() if line.strip()]
            for name in old_versions(names, self.config.retention_days):
                res = self.rclone.purge(f"{self.config.versions_path}/{name}")
                if res.ok:
                    removed += 1
                else:
                    self.log.warning("清理版本失败 %s: %s", name, res.err)
        summary = "同步完成"
        if removed:
            summary += f"，清理 {removed} 个过期版本"
        self.finished_ok.emit(summary, stamp)
