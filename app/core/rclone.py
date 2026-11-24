from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from configparser import ConfigParser
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from app import constants as C
from app.core.config import Config, app_root, data_dir
from app.core.filenames import LITERAL_FLAGS
from app.core.log import get_logger
from app.core.tls import https_endpoint_proxy, trusted_leaf, webdav_url

CREATE_NO_WINDOW = 0x08000000
_FILENAME_COMMANDS = frozenset({"lsjson", "lsf", "hashsum", "cat", "mkdir", "copy", "copyto", "moveto", "purge", "sync"})
_NETWORK_COMMANDS = _FILENAME_COMMANDS | {"lsd", "mount"}


def _environment() -> dict[str, str]:
    # Neither mount nor shorter private operations use external TLS, proxy or
    # rclone settings. Use the application's connection and OS trust only.
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(("RCLONE_", "SSL_CERT_"))
            and not key.upper().endswith("_PROXY") and key.upper() != "SSLKEYLOGFILE"}


def rclone_exe() -> Path:
    # A different binary on PATH has not been verified against the filename
    # protocol. Source checkouts use build/fetch-vendor.ps1, like the bundle.
    return Path(getattr(sys, "_MEIPASS", app_root())) / "vendor" / "rclone.exe"


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
    def _command(
        self, exe: Path, args: list[str], conf: Path, resources: ExitStack, *, expected_url: str,
    ) -> list[str]:
        # A WinFsp mount keeps the Windows VFS names that Explorer expects.
        flags = list(LITERAL_FLAGS) if args and args[0] in _FILENAME_COMMANDS else []
        if args and args[0] in _NETWORK_COMMANDS:
            if self.config.connection_needs_confirmation:
                raise ValueError("请先在设置中核对并保存 WebDAV 地址和账号")
            flags.extend(("--allow-https-downgrade=false", "--webdav-auth-redirect=false"))
            # Use the actual configuration selected for this process, including
            # unsaved connection tests. Alias configurations are local fixtures.
            ini = ConfigParser(interpolation=None)
            try:
                with conf.open(encoding="utf-8-sig") as stream:
                    ini.read_file(stream)
            except FileNotFoundError:
                pass
            if ini.get(C.DEFAULT_REMOTE, "type", fallback="") == "webdav":
                url = ini.get(C.DEFAULT_REMOTE, "url")
                if expected_url and url.rstrip("/") != expected_url.rstrip("/"):
                    raise ValueError("rclone 的 WebDAV 地址与当前连接配置不一致")
                endpoint = webdav_url(url)
                if endpoint.scheme == "https" and (pem := self.config.trusted_certificate_for(url)):
                    root = resources.enter_context(tempfile.TemporaryDirectory(prefix="PhoneNAS-tls-"))
                    certificate = Path(root) / "trusted-server.pem"
                    certificate.write_text(trusted_leaf(pem), encoding="ascii")
                    proxy = resources.enter_context(https_endpoint_proxy(endpoint))
                    flags.extend(("--ca-cert", str(certificate), "--http-proxy", proxy))
        return [str(exe), *args, *flags, "--config", str(conf)]

    def run(
        self, args: list[str], timeout: int = 120, conf_path: Path | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Result:
        return self._run(args, timeout, conf_path or self.conf_path, cancel_event, expected_url=self.config.url)

    def _run(
        self, args: list[str], timeout: int, conf: Path, cancel_event: threading.Event | None, *, expected_url: str,
    ) -> Result:
        if self._cancelled.is_set() or (cancel_event and cancel_event.is_set()):
            return Result(False, "", "操作已取消", -1)
        exe = rclone_exe()
        if not exe.exists():
            return Result(False, "", f"找不到 rclone: {exe}", -1)
        resources = ExitStack()
        proc = None
        try:
            cmd = self._command(exe, args, conf, resources, expected_url=expected_url)
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=_environment(),
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
            try:
                if proc is not None:
                    with self._lock:
                        self._processes.discard(proc)
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
            finally:
                resources.close()

    def popen(self, args: list[str], log_path: Path | None = None, conf_path: Path | None = None) -> subprocess.Popen:
        exe = rclone_exe()
        if not exe.exists():
            raise FileNotFoundError(f"找不到 rclone: {exe}")
        conf = conf_path or self.conf_path
        with ExitStack() as resources:
            if self._cancelled.is_set():
                raise RuntimeError("操作已取消")
            cmd = self._command(exe, args, conf, resources, expected_url=self.config.url)
            stream = None
            if log_path:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                stream = resources.enter_context(log_path.open("a", encoding="utf-8", errors="replace"))
            with self._lock:
                if self._cancelled.is_set():
                    raise RuntimeError("操作已取消")
                proc = subprocess.Popen(
                    cmd, stdout=stream or subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", env=_environment(),
                    creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self._processes.add(proc)
                proc._phonenas_resources = resources.pop_all()  # type: ignore[attr-defined]
        return proc

    def release(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._processes.discard(proc)
        resources = getattr(proc, "_phonenas_resources", None)
        if resources is not None:
            resources.close()

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
        if any(ord(c) < 32 or ord(c) == 127 for value in (username, password) for c in value):
            raise ValueError("连接信息不能包含换行或控制字符")
        webdav_url(url)
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
                return self._run(
                    ["lsd", f"{C.DEFAULT_REMOTE}:", "--max-depth", "1", "--timeout", "20s",
                     "--contimeout", "10s", "--retries", "1", "--low-level-retries", "1"],
                    35, self.conf_path, cancel_event, expected_url=url,
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
