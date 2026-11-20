from __future__ import annotations

import ntpath
import os
import re
import uuid
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Iterable, Mapping

from app import constants as C
from app.core.filenames import portable_name_key, relative_parts


MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 1440
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650


@dataclass(frozen=True)
class MirrorTask:
    id: str
    name: str
    enabled: bool
    local_path: str
    remote_path: str
    versions_path: str
    interval_minutes: int
    retention_days: int
    exclude_preset: str
    excludes: tuple[str, ...]
    last_sync_at: str = ""
    last_file_count: int = 0
    direction: str = "upload"
    dataset_id: str = ""
    local_history_path: str = ""
    sync_identity: str = ""
    auto_sync: bool = True

    def __post_init__(self) -> None:
        if type(self.direction) is not str or self.direction not in ("upload", "download"):
            raise ValueError("任务 direction 必须是 upload 或 download")
        if self.dataset_id == "":
            dataset_id = dataset_id_for_remote(self.remote_path)
        else:
            dataset_id = _normalize_uuid(self.dataset_id, "dataset_id")
        object.__setattr__(self, "dataset_id", dataset_id)
        if self.sync_identity == "":
            sync_identity = uuid.uuid5(uuid.NAMESPACE_URL, "phonenas-binding:" + str(self.id)).hex
        else:
            sync_identity = _normalize_uuid(self.sync_identity, "sync_identity")
        object.__setattr__(self, "sync_identity", sync_identity)
        if type(self.local_history_path) is not str:
            raise ValueError("任务 local_history_path 必须是字符串")
        if not self.local_history_path and type(self.local_path) is str and self.local_path.strip():
            local = Path(_plain_local_name(self.local_path))
            if local.is_absolute() and local.name:
                history = local.with_name(f"{local.name}.PhoneNAS-history-{self.id[:8]}")
                object.__setattr__(self, "local_history_path", str(history))

    @property
    def action_text(self) -> str:
        return "立即上传" if self.direction == "upload" else "立即拉取"

    @property
    def direction_label(self) -> str:
        return "上传到手机" if self.direction == "upload" else "从手机拉取"

    @property
    def history_location(self) -> str:
        return self.versions_path if self.direction == "upload" else self.local_history_path

    @property
    def dataset_control_path(self) -> str:
        return f".phonenas/datasets/{self.dataset_id}"

    def to_dict(self) -> dict[str, Any]:
        result = {field.name: getattr(self, field.name) for field in fields(self)}
        result["excludes"] = list(self.excludes)
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MirrorTask:
        if not isinstance(data, Mapping):
            raise ValueError("任务必须是对象")
        required = {field.name for field in fields(cls)} - {
            "last_sync_at", "last_file_count", "direction", "dataset_id", "local_history_path", "sync_identity", "auto_sync",
        }
        if not required.issubset(data) or set(data) - {field.name for field in fields(cls)}:
            raise ValueError("任务字段缺失或未知")
        values = dict(data)
        for key in ("id", "name", "local_path", "remote_path", "versions_path", "exclude_preset", "last_sync_at"):
            if type(values.get(key, "")) is not str:
                raise ValueError(f"任务 {key} 必须是字符串")
        if type(values["enabled"]) is not bool:
            raise ValueError("任务 enabled 必须是布尔值")
        if type(values.get("auto_sync", True)) is not bool:
            raise ValueError("任务 auto_sync 必须是布尔值")
        for key, lower, upper in (
            ("interval_minutes", MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES),
            ("retention_days", MIN_RETENTION_DAYS, MAX_RETENTION_DAYS),
        ):
            if type(values[key]) is not int or not lower <= values[key] <= upper:
                raise ValueError(f"任务 {key} 必须是 {lower}..{upper} 的整数")
        count = values.get("last_file_count", 0)
        if type(count) is not int or count < 0:
            raise ValueError("任务 last_file_count 必须是非负整数")
        patterns = values.get("excludes")
        if not isinstance(patterns, (list, tuple)) or any(type(item) is not str for item in patterns):
            raise ValueError("任务 excludes 必须是字符串列表")
        values["excludes"] = tuple(patterns)
        if not values["id"].strip() or not values["name"].strip():
            raise ValueError("任务 ID 和名称不能为空")
        for key in ("remote_path", "versions_path"):
            values[key] = normalize_remote_path(values[key])
        return cls(**values)


def _normalize_uuid(value: str, field: str) -> str:
    if type(value) is not str or not re.fullmatch(
        r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        value,
    ):
        raise ValueError(f"任务 {field} 必须是 32 位十六进制 ID 或 UUID")
    return uuid.UUID(value).hex


def new_task(
    local_path: str, name: str = "", *, direction: str = "upload",
    remote_path: str | None = None, versions_path: str | None = None,
    dataset_id: str | None = None,
) -> MirrorTask:
    if type(local_path) is not str or type(name) is not str:
        raise ValueError("本地路径和任务名称必须是字符串")
    task_id = uuid.uuid4().hex
    label = name.strip() or ntpath.basename(local_path.rstrip("/\\")) or "文件夹"
    suggested_mirror, suggested_history = suggest_phone_paths(label, task_id)
    remote = normalize_remote_path(suggested_mirror if remote_path is None else remote_path)
    if versions_path is None:
        versions_path = suggested_history if remote_path is None else suggest_phone_history(remote)
    versions = normalize_remote_path(versions_path)
    return MirrorTask(
        id=task_id,
        name=label,
        enabled=True,
        local_path=local_path,
        remote_path=remote,
        versions_path=versions,
        interval_minutes=C.DEFAULT_INTERVAL_MINUTES,
        retention_days=C.DEFAULT_RETENTION_DAYS,
        exclude_preset="recommended",
        excludes=tuple(C.RECOMMENDED_EXCLUDES),
        direction=direction,
        dataset_id="" if dataset_id is None else dataset_id,
        local_history_path=suggest_local_history(local_path, task_id),
        sync_identity=uuid.uuid4().hex,
    )


def task_folder_name(name: str, task_id: str) -> str:
    # Keep readable letters (including Chinese), but never let a display name
    # become a path component without checking it.
    stem = re.sub(r'[\\/<>:"|?*]+', "-", name)
    stem = " ".join(stem.split())[:48].rstrip(" .-") or "文件夹"
    candidate = f"{stem}-{task_id[:8]}"
    try:
        relative_parts(candidate)
    except ValueError:
        candidate = f"文件夹-{task_id[:8]}"
    return candidate


def suggest_phone_paths(name: str, task_id: str) -> tuple[str, str]:
    folder = task_folder_name(name, task_id)
    return f"PhoneNasBackup/{folder}", f"PhoneNasHistory/{folder}"


def suggest_phone_history(remote: str) -> str:
    parent, _, leaf = remote.rpartition("/")
    if portable_name_key(parent) == "phonenasbackup":
        return f"PhoneNasHistory/{leaf}"
    # Keep the previous convention for explicitly chosen old-style mirrors.
    if parent and leaf.casefold() == "mirror":
        return f"{parent}/versions"
    return f"{remote}-versions"


def suggest_local_history(local_path: str, task_id: str) -> str:
    if not local_path.strip():
        return ""
    local = Path(_plain_local_name(local_path))
    if not local.is_absolute() or not local.name:
        return ""
    group = "PhoneNAS-本地历史"
    if portable_name_key(local.name) == portable_name_key(group):
        group += "-归档"
    return str(local.parent / group / task_folder_name(local.name, task_id))


def normalize_remote_path(path: str) -> str:
    """Return a relative remote root, refusing traversal and rclone remotes."""
    if type(path) is not str:
        raise ValueError("远端目录必须是字符串")
    value = path.strip().replace("\\", "/")
    if not value or value.startswith("/") or ":" in value or any(ord(char) < 32 for char in value):
        raise ValueError("远端目录不能为空、根目录、绝对路径或包含冒号/控制字符")
    parts = [part for part in value.split("/") if part]
    if not parts or any(part in (".", "..") or part != part.strip() or part.endswith(".") for part in parts):
        raise ValueError("远端目录不能包含空白或 . / .. 路径段")
    return "/".join(relative_parts("/".join(parts)))


def dataset_id_for_remote(path: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, "phonenas-dataset:" + normalize_remote_path(path).casefold()).hex


def _plain_local_name(value: str) -> str:
    # Win32 extended-length spellings must not create a second identity for a drive.
    windows = value.replace("/", "\\")
    if windows.casefold().startswith("\\\\?\\unc\\"):
        return "\\\\" + windows[8:]
    if windows.startswith("\\\\?\\") and re.match(r"[a-zA-Z]:\\", windows[4:]):
        return windows[4:]
    if windows.startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError("不允许 Windows 设备或特殊命名空间作为来源")
    return value


def _path_parts(path: Path) -> tuple[str, ...]:
    return tuple(map(portable_name_key, Path(_plain_local_name(str(path))).parts))


def _overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    length = min(len(left), len(right))
    return left[:length] == right[:length]


def _source_path(task: MirrorTask, mount_drive: str, *, history: bool = False) -> Path:
    value = task.local_history_path if history else task.local_path
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError("本地来源不能为空")
    value = _plain_local_name(value)
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("本地来源必须是绝对路径")
    drive = ntpath.splitdrive(value)[0].casefold()
    mount_value = str(mount_drive).rstrip("/\\")
    mounted_drive = mount_value.casefold() if re.fullmatch(r"[a-zA-Z]:", mount_value) else ""
    if mounted_drive and drive == mounted_drive:
        raise ValueError("不能把 PhoneNAS 挂载盘作为来源")
    try:
        canonical = path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"无法解析本地来源: {value}") from exc
    if canonical == Path(canonical.anchor):
        raise ValueError("不能把磁盘根目录作为来源")
    canonical_drive = ntpath.splitdrive(_plain_local_name(str(canonical)))[0].casefold()
    if mounted_drive and canonical_drive == mounted_drive:
        raise ValueError("本地来源指向 PhoneNAS 挂载盘")
    mount = Path(mount_value + "/")
    try:
        mount = mount.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        if not mounted_drive or not isinstance(exc, OSError):
            raise ValueError(f"无法解析 PhoneNAS 挂载目录: {mount}") from exc
        # An unavailable drive (e.g. WinError 1005) still reserves its letter.
        # A directory mount needs a canonical path to check for aliases.
    if _overlap(_path_parts(canonical), _path_parts(mount)):
        raise ValueError("本地来源指向 PhoneNAS 挂载目录")
    return canonical


def validate_source(task: MirrorTask, mount_drive: str = "X:") -> Path:
    """Recheck the live source before sync; a missing disk is never empty data."""
    if not isinstance(task, MirrorTask):
        raise ValueError("任务必须是 MirrorTask")
    canonical = _source_path(task, mount_drive)
    try:
        if not canonical.is_dir() or not os.access(canonical, os.R_OK | os.X_OK):
            raise ValueError(f"本地来源不是可读目录: {task.local_path}")
        with os.scandir(canonical) as entries:
            next(entries, None)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"本地来源不可读: {task.local_path}") from exc
    return canonical


def validate_tasks(tasks: Iterable[MirrorTask], mount_drive: str = "X:") -> None:
    """Validate persisted structure/isolation, including disabled tasks.

    Offline sources may remain configured; validate_source is the execution gate.
    Existing symlinks and junctions are resolved even while checking configuration.
    """
    seen: set[str] = set()
    datasets: set[str] = set()
    local_roots: list[tuple[str, ...]] = []
    remote_roots: list[tuple[str, ...]] = []
    for task in tasks:
        if not isinstance(task, MirrorTask):
            raise ValueError("任务必须是 MirrorTask")
        if type(task.excludes) is not tuple:
            raise ValueError("冻结任务的 excludes 必须是不可变元组")
        MirrorTask.from_dict(task.to_dict())
        if task.id in seen:
            raise ValueError("任务 ID 不能重复")
        seen.add(task.id)
        if task.dataset_id in datasets:
            raise ValueError("同一台电脑不能重复绑定同一个数据集")
        datasets.add(task.dataset_id)
        for history in (False, True):
            local = _path_parts(_source_path(task, mount_drive, history=history))
            if any(_overlap(local, root) for root in local_roots):
                raise ValueError("所有本地数据和历史根目录必须互相隔离，不能重复或互为父子目录")
            local_roots.append(local)
        for value in (task.remote_path, task.versions_path):
            normalized = normalize_remote_path(value)
            if value != normalized:
                raise ValueError("远端目录必须使用规范化相对路径")
            remote = tuple(map(portable_name_key, normalized.split("/")))
            if ".phonenas" in remote:
                raise ValueError("远端数据和历史目录不能占用保留的 .phonenas 控制目录")
            if any(_overlap(remote, root) for root in remote_roots):
                raise ValueError("所有镜像和历史根目录必须互相隔离，不能相同或互为父子目录")
            remote_roots.append(remote)
