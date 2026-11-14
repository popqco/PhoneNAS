from __future__ import annotations

import base64
import copy
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from app import constants as C


# --------------------------------------------------------------------------- paths

def app_root() -> Path:
    """Project root in development, folder containing the EXE when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def is_portable() -> bool:
    return (app_root() / "portable.flag").exists()


def data_dir() -> Path:
    if is_portable():
        base = app_root() / "data"
    else:
        base = Path(os.environ.get("APPDATA", Path.home())) / C.APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


# --------------------------------------------------------------------------- DPAPI

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _make_blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    return blob, buf


def _take_blob(blob: _DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def encrypt_password(plain: str) -> str:
    if not plain:
        return ""
    blob_in, _buf = _make_blob(plain.encode("utf-8"))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("CryptProtectData failed")
    return base64.b64encode(_take_blob(blob_out)).decode("ascii")


def decrypt_password(encoded: str) -> str:
    if not encoded:
        return ""
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return ""
    blob_in, _buf = _make_blob(raw)
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        return ""
    return _take_blob(blob_out).decode("utf-8", "replace")


# --------------------------------------------------------------------------- config

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "nas": {
        "url": C.DEFAULT_URL,
        "username": "admin",
        "password_enc": "",
    },
    "mount": {
        "enabled": True,
        "drive": C.DEFAULT_DRIVE,
        "auto_mount": True,
        "network_mode": False,
    },
    "mirror": {
        "enabled": True,
        "local_path": C.DEFAULT_LOCAL_PATH,
        "remote_path": "backup/work",
        "versions_path": "backup/work-versions",
        "interval_minutes": C.DEFAULT_INTERVAL_MINUTES,
        "retention_days": C.DEFAULT_RETENTION_DAYS,
        "exclude_preset": "recommended",
        "excludes": list(C.RECOMMENDED_EXCLUDES),
    },
    "app": {
        "start_with_windows": True,
        "minimize_to_tray": True,
        "language": "zh_CN",
        "last_sync_at": "",
    },
}


def _deep_merge(base: dict, incoming: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_dir() / "config.json"
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    # -- io ---------------------------------------------------------------
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            incoming = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        self.data = _deep_merge(DEFAULTS, incoming)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    # -- helpers ----------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        node = self.data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    @property
    def password(self) -> str:
        return decrypt_password(self.get("nas.password_enc", ""))

    @password.setter
    def password(self, plain: str) -> None:
        self.set("nas.password_enc", encrypt_password(plain))

    @property
    def url(self) -> str:
        return (self.get("nas.url") or "").rstrip("/")

    @property
    def username(self) -> str:
        return self.get("nas.username") or ""

    @property
    def local_path(self) -> Path:
        return Path(self.get("mirror.local_path") or "")

    @property
    def remote_path(self) -> str:
        return (self.get("mirror.remote_path") or "backup/work").strip("/")

    @property
    def versions_path(self) -> str:
        return (self.get("mirror.versions_path") or "backup/work-versions").strip("/")

    @property
    def interval_minutes(self) -> int:
        try:
            return max(1, int(self.get("mirror.interval_minutes", 20)))
        except (TypeError, ValueError):
            return 20

    @property
    def retention_days(self) -> int:
        try:
            return max(1, int(self.get("mirror.retention_days", 30)))
        except (TypeError, ValueError):
            return 30

    # -- import / export --------------------------------------------------
    def export_to(self, path: Path) -> None:
        payload = copy.deepcopy(self.data)
        payload.setdefault("nas", {})["password_enc"] = ""
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def import_from(self, path: Path) -> None:
        incoming = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        keep_password = self.get("nas.password_enc", "")
        self.data = _deep_merge(DEFAULTS, incoming)
        if not self.get("nas.password_enc"):
            self.set("nas.password_enc", keep_password)
        self.save()
