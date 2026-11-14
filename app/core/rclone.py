from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    def __init__(self, config: Config):
        self.config = config
        self.log = get_logger()
        self.conf_path = data_dir() / "rclone.conf"

    # -- process ----------------------------------------------------------
    def run(self, args: list[str], timeout: int = 120, conf_path: Path | None = None) -> Result:
        exe = rclone_exe()
        if not exe.exists():
            return Result(False, "", f"找不到 rclone: {exe}", -1)
        conf = conf_path or self.conf_path
        cmd = [str(exe), *args, "--config", str(conf)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
            return Result(proc.returncode == 0, proc.stdout, proc.stderr, proc.returncode)
        except subprocess.TimeoutExpired:
            return Result(False, "", f"rclone 超时（{timeout}s）", -1)
        except Exception as exc:  # noqa: BLE001
            return Result(False, "", str(exc), -1)

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
        proc = subprocess.Popen(
            cmd,
            stdout=stream or subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if stream is not None:
            proc._phonenas_log = stream  # type: ignore[attr-defined]
        return proc

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
    def test_connection(self, url: str, username: str, password: str) -> Result:
        exe = rclone_exe()
        if not exe.exists():
            return Result(False, "", f"找不到 rclone: {exe}", -1)
        tmp_conf = data_dir() / "rclone.test.conf"
        try:
            self._write_conf(tmp_conf, url, username, password)
            return self.run(
                ["lsd", f"{C.DEFAULT_REMOTE}:", "--max-depth", "1", "--timeout", "20s"],
                timeout=35,
                conf_path=tmp_conf,
            )
        finally:
            try:
                tmp_conf.unlink()
            except OSError:
                pass

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
