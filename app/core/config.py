from __future__ import annotations

import base64
import copy
import ctypes
import json
import os
import stat
import sys
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterable
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

from app import constants as C
from app.core.tasks import MirrorTask, validate_tasks
from app.core.tls import trusted_leaf, webdav_url


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

# Keep 1.2.3 defaults for display while the provenance of omitted NAS fields is reviewed.
_CONNECTION_FIELDS = ("url", "username")
_BINDING_REVIEW = "binding_review"

_SAVED_DEFAULTS: dict[str, Any] = {
    "version": 3,
    "nas": {
        "url": C.LEGACY_DEFAULT_URL,
        "username": C.LEGACY_DEFAULT_USERNAME,
        "password_enc": "",
        "tls_url": "",
        "tls_cert": "",
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


# First-run defaults must not change the behavior of older saved profiles.
DEFAULTS: dict[str, Any] = _deep_merge(_SAVED_DEFAULTS, {
    "nas": {"url": "", "username": ""},
    "mount": {"auto_mount": False},
    "app": {"start_with_windows": False},
})


def _decode_config(incoming: Any) -> tuple[dict[str, Any], bool, tuple[str, ...]]:
    if not isinstance(incoming, dict):
        raise ValueError("配置必须是 JSON 对象")
    version = incoming.get("version", 1)
    if type(version) is not int or version not in (1, 2, 3):
        raise ValueError(f"不支持的配置版本: {version!r}")
    for section in ("nas", "mount", "mirror", "app"):
        if section in incoming and not isinstance(incoming[section], dict):
            raise ValueError(f"{section} 配置必须是对象")
    data = _deep_merge(_SAVED_DEFAULTS, incoming)
    if version in (1, 2) and "read_only" not in incoming.get("mount", {}):
        # Existing installations may intentionally use a writable mount; new
        # configurations default read-only without silently changing old ones.
        data["mount"]["read_only"] = False
    for section in ("nas", "mount", "app"):
        for key, default in _SAVED_DEFAULTS[section].items():
            if type(data[section][key]) is not type(default):
                raise ValueError(f"{section}.{key} 配置类型不正确")
    nas = data["nas"]
    saved_nas = incoming.get("nas", {})
    recorded_review = nas.pop(_BINDING_REVIEW, [])
    if type(recorded_review) is not list or any(field not in _CONNECTION_FIELDS for field in recorded_review):
        raise ValueError("旧连接复核字段无效")
    review = tuple(field for field in _CONNECTION_FIELDS if field not in saved_nas or field in recorded_review)
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
    return data, migrated, review


def _normalize_tls(nas: dict[str, Any], review: tuple[str, ...] = ()) -> None:
    endpoint = nas["tls_url"].rstrip("/")
    # A placeholder URL is not an endpoint change; keep the local enrollment dormant.
    if nas["tls_cert"] and ("url" in review or endpoint == nas["url"].rstrip("/")):
        if webdav_url(endpoint).scheme != "https":
            raise ValueError("服务器证书必须绑定 HTTPS 地址")
        nas["tls_url"] = endpoint
        nas["tls_cert"] = trusted_leaf(nas["tls_cert"])
    else:
        # A confirmed endpoint change cannot revive the old enrollment later.
        nas["tls_url"] = nas["tls_cert"] = ""


def _record_binding_review(payload: dict[str, Any], review: tuple[str, ...]) -> dict[str, Any]:
    if review:
        return {**payload, "nas": {**payload["nas"], _BINDING_REVIEW: list(review)}}
    return payload


_TASK_BINDING_FIELDS = (
    "direction", "local_path", "remote_path", "versions_path", "local_history_path",
    "dataset_id", "excludes", "sync_identity",
)
_PENDING_SUFFIX = ".pending.json"
_PENDING_UNVERIFIABLE = "存在未完成操作记录，无法确认原配置的任务绑定"


def _connection_identity(nas: dict[str, Any]) -> tuple[str, str]:
    return nas["url"].rstrip("/"), nas["username"]


def _validate_connection_identity(connection: tuple[str, str]) -> None:
    url, username = connection
    webdav_url(url)
    if any(ord(char) < 32 or ord(char) == 127 for char in username):
        raise ValueError("用户名不能包含换行或控制字符")


def _known_pending_connection(
    nas: dict[str, Any], review: tuple[str, ...], pending: list[tuple[Path, dict[str, Any]]]
) -> tuple[str, str] | None:
    if not pending:
        return None
    for default_url, default_username in ((C.LEGACY_DEFAULT_URL, C.LEGACY_DEFAULT_USERNAME),
                                          (C.PREVIEW_DEFAULT_URL, C.PREVIEW_DEFAULT_USERNAME)):
        candidate = (default_url if "url" in review else nas["url"].rstrip("/"),
                     default_username if "username" in review else nas["username"])
        if _matches_pending_connection(candidate, pending):
            return candidate
    return None


def _matches_pending_connection(
    connection: tuple[str, str], pending: list[tuple[Path, dict[str, Any]]]
) -> bool:
    # Use the recovery reader's complete validation, including its known state
    # variants. A journal can corroborate a candidate, never supply a URL or password.
    from app.core.pending import PendingOperation
    from app.core.relay import BaselineStore

    try:
        _validate_connection_identity(connection)
        endpoint = SimpleNamespace(url=connection[0], username=connection[1])
        for path, task in pending:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                return False
            journal = PendingOperation(BaselineStore(path.parent, MirrorTask.from_dict(task), endpoint))
            if journal.load() is None:
                return False
    except (OSError, RuntimeError, ValueError):
        return False
    return True


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
        self._connection_review: tuple[str, ...] = ()
        self._recovered_connection: tuple[bytes, tuple[str, str]] | None = None
        if not self.path.exists():
            return
        try:
            original = self.path.read_bytes()
            incoming = json.loads(original.decode("utf-8-sig"))
            data, migrated, review = _decode_config(incoming)
            connection = None
            if review:
                try:
                    pending = self._pending_tasks(data, self._pending_entries())
                except (OSError, ValueError):
                    pending = []  # An unavailable journal cannot authorize a connection.
                connection = _known_pending_connection(data["nas"], review, pending)
                if connection is not None:
                    for key, value in zip(_CONNECTION_FIELDS, connection):
                        if key in review:
                            data["nas"][key] = value
                    review = ()
            _normalize_tls(data["nas"], review)
            self.data, self._migrated, self._connection_review = data, migrated, review
            if connection is not None:
                self._recovered_connection = original, connection
            if self._migrated:
                self._migration_original = original
                self._migration_version = incoming.get("version", 1)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            self._load_error = str(exc) or type(exc).__name__
            self.data = copy.deepcopy(DEFAULTS)
            self.data["mirror"]["enabled"] = False

    def save(self) -> None:
        self._save()

    def save_recovered_connection(self) -> bool:
        """Persist a journal-proven connection before recovery can clear its evidence.

        Return False when there is no inferred connection to persist. A failed
        recheck or save must abort recovery before any journal is removed.
        """
        if self._recovered_connection is None:
            return False
        self.save()
        return True

    def _save(self, *, backup_existing: bool = False) -> None:
        if self.load_error:
            raise ValueError(f"配置加载失败，拒绝覆盖原文件: {self.load_error}")
        payload, _, review = _decode_config(_record_binding_review(self.data, self._connection_review))
        _normalize_tls(payload["nas"], review)
        # A draft may predate the on-disk config, and journals can outlive it.
        original = self._saved_bytes()
        has_pending = self._check_pending_bindings(original, payload, review)
        if self._recovered_connection is not None:
            recovered_from, connection = self._recovered_connection
            if (not has_pending or original != recovered_from or review
                    or _connection_identity(payload["nas"]) != connection):
                raise ValueError(_PENDING_UNVERIFIABLE)
        if backup_existing and original is not None:
            self._backup_replaced_config(original)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._migration_original is not None:
            try:
                with self.path.with_suffix(f".v{self._migration_version}-backup").open("xb") as backup:
                    backup.write(self._migration_original)
            except FileExistsError:
                pass
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(_record_binding_review(payload, review), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)
        self.data = payload
        self._connection_review = review
        self._recovered_connection = None
        self._migration_original = None
        self._migration_version = None

    def _saved_bytes(self) -> bytes | None:
        try:
            self.path.lstat()
        except FileNotFoundError:
            return None
        return self.path.read_bytes()

    def _pending_entries(self) -> list[Path]:
        baselines = self.path.parent / "baselines"
        try:
            info = baselines.lstat()
        except FileNotFoundError:
            return []
        if (not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("基线状态路径不是普通目录，拒绝覆盖原配置")
        # Every matching entry is protected, including unknown or broken records.
        return [path for path in baselines.iterdir() if os.path.normcase(path.name).endswith(_PENDING_SUFFIX)]

    @staticmethod
    def _pending_tasks(saved: dict[str, Any], entries: list[Path]) -> list[tuple[Path, dict[str, Any]]]:
        by_name = {os.path.normcase(entry.name): entry for entry in entries}
        matches = []
        for task in saved["mirror"]["tasks"]:
            if (name := os.path.normcase(f"{task['id']}{_PENDING_SUFFIX}")) in by_name:
                matches.append((by_name[name], task))
        if len(matches) != len(entries) or len({path for path, _ in matches}) != len(entries):
            raise ValueError(_PENDING_UNVERIFIABLE)
        return matches

    def _check_pending_bindings(
        self, original: bytes | None, payload: dict[str, Any], review: tuple[str, ...],
    ) -> bool:
        entries = self._pending_entries()
        if not entries:
            return False
        if original is None:
            raise ValueError(_PENDING_UNVERIFIABLE)
        try:
            saved, _, saved_review = _decode_config(json.loads(original.decode("utf-8-sig")))
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise ValueError(_PENDING_UNVERIFIABLE) from exc
        pending = self._pending_tasks(saved, entries)
        saved_nas = saved["nas"]
        old_connection = _connection_identity(saved_nas)
        new_connection = _connection_identity(payload["nas"])
        if saved_review and not review:
            # Replacing a display guess with the proven old identity is not a rebind.
            connection_changed = (
                any(old != new and field not in saved_review for field, old, new in
                    zip(_CONNECTION_FIELDS, old_connection, new_connection))
                or not _matches_pending_connection(new_connection, pending)
            )
        else:
            connection_changed = old_connection != new_connection or saved_review != review
        try:
            _normalize_tls(saved_nas, saved_review)
        except ValueError as exc:
            raise ValueError(_PENDING_UNVERIFIABLE) from exc
        candidates = {task["id"]: task for task in payload["mirror"]["tasks"]}
        for _, old in pending:
            task_id = old["id"]
            new = candidates.get(task_id)
            if (connection_changed or new is None
                    or any(old[field] != new[field] for field in _TASK_BINDING_FIELDS)):
                raise ValueError(f"任务 {task_id} 有未完成操作记录，请先恢复操作再更改连接、任务绑定或移除任务")
        return True

    @property
    def load_error(self) -> str:
        return self._load_error

    @property
    def migrated(self) -> bool:
        return self._migrated

    @property
    def connection_needs_confirmation(self) -> bool:
        """A display value from an old sparse profile still lacks provenance."""
        return bool(self._connection_review)

    def confirm_connection(self) -> bool:
        """Approve the draft's current connection; return whether it restores a pending binding.

        Call only after explicit user confirmation. Save the draft before publishing it.
        """
        if self.load_error:
            raise ValueError(f"配置加载失败: {self.load_error}")
        if not self.connection_needs_confirmation:
            return False
        payload, _, _ = _decode_config(self.data)
        _validate_connection_identity(_connection_identity(payload["nas"]))
        _normalize_tls(payload["nas"])
        restores_binding = self._check_pending_bindings(self._saved_bytes(), payload, ())
        self.data["nas"] = payload["nas"]
        self._connection_review = ()
        return restores_binding

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
        snapshot._connection_review = self._connection_review
        snapshot._recovered_connection = self._recovered_connection
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
        self._connection_review = snapshot._connection_review
        self._recovered_connection = snapshot._recovered_connection

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
    def certificate_enrollment(self) -> tuple[str, str]:
        """Local enrollment for display/editing; it may still be pending review."""
        endpoint = self.get("nas.tls_url", "")
        if endpoint == self.url or "url" in self._connection_review:
            return endpoint, self.get("nas.tls_cert", "")
        return "", ""

    def trusted_certificate_for(self, url: str) -> str:
        if not self.connection_needs_confirmation:
            endpoint, pem = self.certificate_enrollment
            if url.rstrip("/") == endpoint:
                return pem
        return ""

    def set_trusted_certificate(self, url: str, pem: str) -> None:
        """Enroll only certificate bytes independently confirmed by the user."""
        endpoint = url.rstrip("/")
        if pem:
            if endpoint != self.url or webdav_url(endpoint).scheme != "https":
                raise ValueError("服务器证书必须绑定当前 HTTPS 地址")
            pem = trusted_leaf(pem)
        self.set("nas.tls_url", endpoint if pem else "")
        self.set("nas.tls_cert", pem)

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
        nas = payload.setdefault("nas", {})
        nas["password_enc"] = ""
        nas.pop("tls_url", None)
        nas.pop("tls_cert", None)
        payload.setdefault("app", {}).pop("schedule_approval", None)
        Path(path).write_text(
            json.dumps(_record_binding_review(payload, self._connection_review), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def import_from(self, path: Path) -> None:
        incoming = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        # Importing a profile never enrolls a new trust anchor. Like a stored
        # credential, local certificate trust can survive for the same address.
        if isinstance(incoming, dict) and isinstance(incoming.get("nas"), dict):
            incoming["nas"].pop("tls_cert", None)
            incoming["nas"].pop("tls_url", None)
        payload, migrated, review = _decode_config(incoming)
        nas = payload["nas"]
        keep_password = self.get("nas.password_enc", "")
        if not nas["password_enc"]:
            nas["password_enc"] = keep_password
        endpoint, pem = self.certificate_enrollment
        if pem and (endpoint == nas["url"].rstrip("/") or "url" in review):
            nas["tls_url"], nas["tls_cert"] = endpoint, pem
            # Importing a matching address must not activate a dormant enrollment.
            review = tuple(field for field in _CONNECTION_FIELDS
                           if field in review or field in self._connection_review)
        draft = self.snapshot()
        draft.data = payload
        draft._connection_review = review
        # Imported success times belong to another runtime/device. Re-inspect any
        # existing target before allowing it to be treated as our known mirror.
        draft.data["mirror"]["tasks"] = [
            replace(MirrorTask.from_dict(item), last_sync_at="", last_file_count=0,
                    sync_identity=uuid.uuid4().hex).to_dict()
            for item in payload["mirror"]["tasks"]
        ]
        draft.data["app"]["last_sync_at"] = ""
        # Opt-in applies to this runtime's reviewed settings, not a replacement.
        draft.data["app"].pop("schedule_approval", None)
        draft._load_error = ""
        draft._migrated = migrated
        draft._save(backup_existing=True)
        self.apply_snapshot(draft)

    def _backup_replaced_config(self, original: bytes) -> None:
        # Back up the file being replaced, not the normalized in-memory config.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        suffix = 0
        while True:
            unique = f"-{suffix}" if suffix else ""
            backup_path = self.path.with_name(f"{self.path.stem}.replaced-{stamp}{unique}.backup")
            try:
                backup = backup_path.open("xb")
            except FileExistsError:
                suffix += 1
                continue
            try:
                with backup:
                    backup.write(original)
            except OSError:
                try:
                    backup_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return
