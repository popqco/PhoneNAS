from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app import constants as C
from app.core.config import Config, app_root, data_dir
from app.core.log import get_logger

CREATE_NO_WINDOW = 0x08000000


def rclone_exe() -> Path:
    bundle = Path(getattr(sys, "_MEIPASS", app_root()))
    candidates = [
        bundle / "vendor" / "rclone.exe",
        app_root() / "vendor" / "rclone.exe",
        app_root() / "rclone.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    found = shutil.which("rclone")
    if found:
        return Path(found)
    return candidates[0]


@dataclass
class Result:
    ok: bool
    out: str
    err: str
    code: int


class Rclone:
    def __init__(self, config: Config, conf_path: Path | None = None):
        self.config = config
        self.log = get_logger()
        self.conf_path = Path(conf_path) if conf_path is not None else data_dir() / "rclone.conf"
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen] = set()

    @contextmanager
    def isolated_conf(self):
        """Use a private configuration for this operation, never the mount's file."""
        original = self.conf_path
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-connection-") as root:
            self.conf_path = Path(root) / "rclone.conf"
            try:
                yield self
            finally:
                self.conf_path = original

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            processes = tuple(self._processes)
        for proc in processes:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except OSError:
                pass

    # -- process ----------------------------------------------------------
    def run(
        self, args: list[str], timeout: int = 120, conf_path: Path | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Result:
        if self._cancelled.is_set() or (cancel_event and cancel_event.is_set()):
            return Result(False, "", "操作已取消", -1)
        exe = rclone_exe()
        if not exe.exists():
            return Result(False, "", f"找不到 rclone: {exe}", -1)
        conf = conf_path or self.conf_path
        cmd = [str(exe), *args, "--config", str(conf)]
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            with self._lock:
                self._processes.add(proc)
            deadline = time.monotonic() + timeout
            while True:
                cancelled = self._cancelled.is_set() or (cancel_event and cancel_event.is_set())
                if cancelled or time.monotonic() >= deadline:
                    proc.kill()
                    out, err = proc.communicate()
                    message = "操作已取消" if cancelled else f"rclone 超时（{timeout}s）"
                    return Result(False, out, message + (f"\n{err}" if err else ""), -1)
                try:
                    out, err = proc.communicate(timeout=min(0.2, max(0.01, deadline-time.monotonic())))
                    return Result(proc.returncode == 0, out, err, proc.returncode)
                except subprocess.TimeoutExpired:
                    continue
        except Exception as exc:  # noqa: BLE001
            return Result(False, "", str(exc), -1)
        finally:
            if proc is not None:
                with self._lock:
                    self._processes.discard(proc)
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()

    def popen(self, args: list[str], log_path: Path | None = None, conf_path: Path | None = None) -> subprocess.Popen:
        exe = rclone_exe()
        if not exe.exists():
            raise FileNotFoundError(f"找不到 rclone: {exe}")
        conf = conf_path or self.conf_path
        cmd = [str(exe), *args, "--config", str(conf)]
        stream = None
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            stream = open(log_path, "a", encoding="utf-8", errors="replace")
        try:
            if self._cancelled.is_set():
                raise RuntimeError("操作已取消")
            proc = subprocess.Popen(
                cmd, stdout=stream or subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            if stream is not None:
                stream.close()
            raise
        with self._lock:
            self._processes.add(proc)
        if stream is not None:
            proc._phonenas_log = stream  # type: ignore[attr-defined]
        return proc

    def release(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._processes.discard(proc)
        stream = getattr(proc, "_phonenas_log", None)
        if stream is not None:
            stream.close()

    def obscure(self, password: str) -> str:
        if not password:
            return ""
        res = self.run(["obscure", password], timeout=20)
        if not res.ok:
            raise RuntimeError(f"rclone obscure 失败: {res.err or res.out}")
        return res.out.strip()

    def write_conf(self, url: str, username: str, password: str) -> None:
        self._write_conf(self.conf_path, url, username, password)

    def _write_conf(self, path: Path, url: str, username: str, password: str) -> None:
        if any(c in str(value) for value in (url, username, password) for c in ("\r", "\n", "\0")):
            raise ValueError("连接信息不能包含换行或控制字符")
        obscured = self.obscure(password)
        lines = [
            f"[{C.DEFAULT_REMOTE}]",
            "type = webdav",
            f"url = {url.rstrip('/')}/",
            "vendor = other",
        ]
        if username:
            lines.append(f"user = {username}")
        if obscured:
            lines.append(f"pass = {obscured}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def ensure_conf(self) -> None:
        if not self.conf_path.exists():
            self.write_conf(self.config.url, self.config.username, self.config.password)

    def version(self) -> str:
        res = self.run(["version"], timeout=20)
        first = (res.out or res.err).splitlines()
        return first[0] if first else "unknown"

    # -- operations -------------------------------------------------------
    def test_connection(
        self, url: str, username: str, password: str,
        cancel_event: threading.Event | None = None,
    ) -> Result:
        if cancel_event and cancel_event.is_set():
            return Result(False, "", "操作已取消", -1)
        try:
            with self.isolated_conf():
                self._write_conf(self.conf_path, url, username, password)
                return self.run(
                    ["lsd", f"{C.DEFAULT_REMOTE}:", "--max-depth", "1", "--timeout", "20s",
                     "--contimeout", "10s", "--retries", "1", "--low-level-retries", "1"],
                    timeout=35, cancel_event=cancel_event,
                )
        except Exception as exc:  # noqa: BLE001
            return Result(False, "", str(exc), -1)

    def mkdir(self, remote_path: str) -> Result:
        return self.run(["mkdir", f"{C.DEFAULT_REMOTE}:{remote_path}"], timeout=60)

    def lsf_dirs(self, remote_path: str) -> Result:
        return self.run(
            ["lsf", f"{C.DEFAULT_REMOTE}:{remote_path}", "--dirs-only"],
            timeout=60,
        )

    def purge(self, remote_path: str) -> Result:
        return self.run(["purge", f"{C.DEFAULT_REMOTE}:{remote_path}"], timeout=300)

    def copy(self, src: str, dst: str) -> Result:
        return self.run(["copy", src, dst, "--stats", "5s"], timeout=1800)

    def copyto(self, src: str, dst: str) -> Result:
        return self.run(["copyto", src, dst], timeout=600)

    def lsf_files(self, remote_path: str) -> Result:
        return self.run(
            ["lsf", f"{C.DEFAULT_REMOTE}:{remote_path}", "--recursive", "--files-only"],
            timeout=120,
        )
