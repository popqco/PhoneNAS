from __future__ import annotations

import base64
import copy
import ctypes
import json
import os
import sys
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterable
from dataclasses import replace
from datetime import datetime

from app import constants as C
from app.core.tasks import MirrorTask, validate_tasks


# --------------------------------------------------------------------------- paths

def app_root() -> Path:
    """Project root in development, folder containing the EXE when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def is_portable() -> bool:
    return (app_root() / "portable.flag").exists()


def data_dir() -> Path:
    override = os.environ.get("PHONENAS_DATA_DIR", "")
    if override:
        base = Path(override).resolve()
    elif is_portable():
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
    "version": 3,
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
        "read_only": True,
    },
    "mirror": {
        "enabled": True,
        "tasks": [],
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


def _decode_config(incoming: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(incoming, dict):
        raise ValueError("配置必须是 JSON 对象")
    version = incoming.get("version", 1)
    if type(version) is not int or version not in (1, 2, 3):
        raise ValueError(f"不支持的配置版本: {version!r}")
    for section in ("nas", "mount", "mirror", "app"):
        if section in incoming and not isinstance(incoming[section], dict):
            raise ValueError(f"{section} 配置必须是对象")
    data = _deep_merge(DEFAULTS, incoming)
    if version in (1, 2) and "read_only" not in incoming.get("mount", {}):
        # Existing installations may intentionally use a writable mount; new
        # configurations default read-only without silently changing old ones.
        data["mount"]["read_only"] = False
    for section in ("nas", "mount", "app"):
        for key, default in DEFAULTS[section].items():
            if type(data[section][key]) is not type(default):
                raise ValueError(f"{section}.{key} 配置类型不正确")
    if type(data["mirror"]["enabled"]) is not bool:
        raise ValueError("mirror.enabled 必须是布尔值")
    migrated = version in (1, 2)
    if version == 1:
        legacy = incoming.get("mirror", {})
        if not isinstance(legacy, dict):
            raise ValueError("mirror 配置必须是对象")
        local_path = legacy.get("local_path", C.DEFAULT_LOCAL_PATH)
        remote_path = legacy.get("remote_path", "backup/work")
        versions_path = legacy.get("versions_path", "backup/work-versions")
        identity = json.dumps([local_path, remote_path, versions_path], ensure_ascii=False)
        task = MirrorTask.from_dict({
            "id": uuid.uuid5(uuid.NAMESPACE_URL, "phonenas-v1:" + identity).hex,
            "name": Path(local_path).name if isinstance(local_path, str) and local_path else "工作文件夹",
            "enabled": legacy.get("enabled", True),
            "local_path": local_path,
            "remote_path": remote_path,
            "versions_path": versions_path,
            "interval_minutes": legacy.get("interval_minutes", C.DEFAULT_INTERVAL_MINUTES),
            "retention_days": legacy.get("retention_days", C.DEFAULT_RETENTION_DAYS),
            "exclude_preset": legacy.get("exclude_preset", "recommended"),
            "excludes": legacy.get("excludes", list(C.RECOMMENDED_EXCLUDES)),
            "last_sync_at": data["app"].get("last_sync_at", ""),
        })
        extra = copy.deepcopy(legacy)
        for key in ("local_path", "remote_path", "versions_path", "interval_minutes", "retention_days",
                    "exclude_preset", "excludes", "last_sync_at", "last_file_count"):
            extra.pop(key, None)
        data["mirror"] = {**extra, "enabled": legacy.get("enabled", True), "tasks": [task.to_dict()]}
    data["version"] = 3
    if not isinstance(data["mirror"]["tasks"], list):
        raise ValueError("mirror.tasks 必须是列表")
    tasks = tuple(MirrorTask.from_dict(item) for item in data["mirror"]["tasks"])
    validate_tasks(tasks, data["mount"]["drive"])
    data["mirror"]["tasks"] = [task.to_dict() for task in tasks]
    return data, migrated


class Config:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_dir() / "config.json"
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._load_error = ""
        self._migrated = False
        self._migration_original: bytes | None = None
        self._migration_version: int | None = None
        self.load()

    # -- io ---------------------------------------------------------------
    def load(self) -> None:
        self.data = copy.deepcopy(DEFAULTS)
        self._load_error = ""
        self._migrated = False
        self._migration_original = None
        self._migration_version = None
        if not self.path.exists():
            return
        try:
            original = self.path.read_bytes()
            incoming = json.loads(original.decode("utf-8-sig"))
            self.data, self._migrated = _decode_config(incoming)
            if self._migrated:
                self._migration_original = original
                self._migration_version = incoming.get("version", 1)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            self._load_error = str(exc) or type(exc).__name__
            self.data = copy.deepcopy(DEFAULTS)
            self.data["mirror"]["enabled"] = False

    def save(self) -> None:
        if self.load_error:
            raise ValueError(f"配置加载失败，拒绝覆盖原文件: {self.load_error}")
        payload, _ = _decode_config(self.data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._migration_original is not None:
            try:
                with self.path.with_suffix(f".v{self._migration_version}-backup").open("xb") as backup:
                    backup.write(self._migration_original)
            except FileExistsError:
                pass
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)
        self.data = payload
        self._migration_original = None
        self._migration_version = None

    @property
    def load_error(self) -> str:
        return self._load_error

    @property
    def migrated(self) -> bool:
        return self._migrated

    @property
    def tasks(self) -> tuple[MirrorTask, ...]:
        if self.load_error:
            return ()
        return tuple(MirrorTask.from_dict(item) for item in self.get("mirror.tasks", []))

    def get_task(self, task_id: str) -> MirrorTask | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def replace_tasks(self, tasks: Iterable[MirrorTask]) -> None:
        candidates = tuple(tasks)
        validate_tasks(candidates, self.get("mount.drive", C.DEFAULT_DRIVE))
        self.set("mirror.tasks", [task.to_dict() for task in candidates])

    def upsert_task(self, task: MirrorTask) -> None:
        existing = list(self.tasks)
        for index, candidate in enumerate(existing):
            if candidate.id == task.id:
                existing[index] = task
                break
        else:
            existing.append(task)
        self.replace_tasks(existing)

    def remove_task(self, task_id: str) -> None:
        self.replace_tasks(task for task in self.tasks if task.id != task_id)

    def mark_task_success(self, task_id: str, when: str | datetime, file_count: int | None = None) -> None:
        task = self.get_task(task_id)
        if task is None:
            return
        stamp = when.isoformat(timespec="seconds") if isinstance(when, datetime) else when
        changes: dict[str, Any] = {"last_sync_at": stamp}
        if file_count is not None:
            changes["last_file_count"] = file_count
        self.upsert_task(replace(task, **changes))

    def snapshot(self) -> Config:
        snapshot = object.__new__(Config)
        snapshot.path = self.path
        snapshot.data = copy.deepcopy(self.data)
        snapshot._load_error = self._load_error
        snapshot._migrated = self._migrated
        snapshot._migration_original = self._migration_original
        snapshot._migration_version = self._migration_version
        return snapshot

    def apply_snapshot(self, snapshot: Config) -> None:
        """Publish a validated draft without loading or saving any files."""
        if not isinstance(snapshot, Config):
            raise ValueError("配置草稿必须是 Config")
        self.data = copy.deepcopy(snapshot.data)
        self._load_error = snapshot._load_error
        self._migrated = snapshot._migrated
        self._migration_original = snapshot._migration_original
        self._migration_version = snapshot._migration_version

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

    def _first_task(self) -> MirrorTask:
        if not self.tasks:
            raise ValueError("没有可用同步任务，请先选择或新建任务")
        return self.tasks[0]

    @property
    def local_path(self) -> Path:
        return Path(self._first_task().local_path)

    @property
    def remote_path(self) -> str:
        return self._first_task().remote_path

    @property
    def versions_path(self) -> str:
        return self._first_task().versions_path

    @property
    def interval_minutes(self) -> int:
        return self._first_task().interval_minutes

    @property
    def retention_days(self) -> int:
        return self._first_task().retention_days

    # -- import / export --------------------------------------------------
    def export_to(self, path: Path) -> None:
        payload = copy.deepcopy(self.data)
        payload.setdefault("nas", {})["password_enc"] = ""
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def import_from(self, path: Path) -> None:
        incoming = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        payload, migrated = _decode_config(incoming)
        keep_password = self.get("nas.password_enc", "")
        if not payload["nas"]["password_enc"]:
            payload["nas"]["password_enc"] = keep_password
        draft = self.snapshot()
        draft.data = payload
        # Imported success times belong to another runtime/device. Re-inspect any
        # existing target before allowing it to be treated as our known mirror.
        draft.data["mirror"]["tasks"] = [
            replace(MirrorTask.from_dict(item), last_sync_at="", last_file_count=0,
                    sync_identity=uuid.uuid4().hex).to_dict()
            for item in payload["mirror"]["tasks"]
        ]
        draft.data["app"]["last_sync_at"] = ""
        draft._load_error = ""
        draft._migrated = migrated
        if self.load_error and self._migration_original is None and self.path.exists():
            original = self.path.read_bytes()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            suffix = 0
            while True:
                unique = f"-{suffix}" if suffix else ""
                backup_path = self.path.with_name(f"{self.path.stem}.replaced-{stamp}{unique}.backup")
                try:
                    with backup_path.open("xb") as backup:
                        backup.write(original)
                    break
                except FileExistsError:
                    suffix += 1
        draft.save()
        self.apply_snapshot(draft)
