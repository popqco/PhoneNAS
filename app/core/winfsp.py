from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

from app.core.config import app_root
from app.core.log import get_logger

DLL_CANDIDATES = [
    r"C:\Program Files (x86)\WinFsp\bin\winfsp-x64.dll",
    r"C:\Program Files\WinFsp\bin\winfsp-x64.dll",
]


def is_installed() -> bool:
    if any(os.path.exists(path) for path in DLL_CANDIDATES):
        return True
    try:
        import winreg

        for path in (r"SOFTWARE\WOW6432Node\WinFsp", r"SOFTWARE\WinFsp"):
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
                key.Close()
                return True
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return False


def msi_path():
    bundle = Path(getattr(sys, "_MEIPASS", app_root()))
    for candidate in (bundle / "vendor" / "winfsp.msi", app_root() / "vendor" / "winfsp.msi"):
        if candidate.exists():
            return candidate
    return app_root() / "vendor" / "winfsp.msi"


def install() -> tuple[bool, str]:
    log = get_logger()
    if is_installed():
        return True, "WinFsp 已安装"
    msi = msi_path()
    if not msi.exists():
        return False, f"缺少安装包: {msi}"

    log.info("installing WinFsp from %s", msi)
    rc = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        "msiexec.exe",
        f'/i "{msi}" /qn /norestart',
        None,
        1,
    )
    if rc <= 32:
        return False, f"无法启动安装程序 (ShellExecute={rc})"

    for _ in range(120):
        time.sleep(1)
        if is_installed():
            log.info("WinFsp installed")
            return True, "WinFsp 安装完成"
    return False, "安装超时，请手动运行 vendor/winfsp.msi"
