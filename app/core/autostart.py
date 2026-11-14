from __future__ import annotations

import sys
import winreg

from app.core.config import app_root
from app.core.log import get_logger

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "PhoneNAS"


def is_autostart_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY)
        try:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
        finally:
            key.Close()
    except OSError:
        return False


def set_autostart(enabled: bool) -> tuple[bool, str]:
    if not getattr(sys, "frozen", False):
        return False, "开发模式下不写入开机自启"
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        )
        try:
            if enabled:
                command = f'"{sys.executable}" --tray'
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
                return True, "已设置开机自启"
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
            return True, "已取消开机自启"
        finally:
            key.Close()
    except OSError as exc:
        get_logger().warning("autostart update failed: %s", exc)
        return False, str(exc)
