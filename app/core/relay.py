from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from app import constants as C
from app.core.filenames import LEGACY, LITERAL, portable_name_key, require_legacy_paths, require_legacy_rules
from app.core.history import validate_history_inventory
from app.core.tasks import MirrorTask, dataset_id_for_remote, normalize_remote_path
from app.core.winpath import filesystem_path


@dataclass(frozen=True)
class FileContent:
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if type(self.size) is not int or self.size < 0:
            raise ValueError("文件大小必须是非负整数")
        if type(self.sha256) is not str or not re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256):
            raise ValueError("文件必须有完整的 SHA256 内容校验值")
        object.__setattr__(self, "sha256", self.sha256.lower())


@dataclass(frozen=True)
class ContentSnapshot:
    files: Mapping[str, FileContent]
    directories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.files, Mapping) or any(not isinstance(item, FileContent)
                                                      for item in self.files.values()):
            raise ValueError("快照必须包含路径到 FileContent 的映射")
        if type(self.directories) is not tuple or len(set(self.directories)) != len(self.directories):
            raise ValueError("快照目录必须是不重复的元组")
        validate_history_inventory(self.files, self.directories)
        object.__setattr__(self, "files", MappingProxyType(dict(sorted(self.files.items()))))
        object.__setattr__(self, "directories", tuple(sorted(self.directories)))

    def to_dict(self) -> dict:
        return {"files": {path: {"size": value.size, "sha256": value.sha256}
                          for path, value in self.files.items()},
                "directories": list(self.directories)}

    @classmethod
    def from_dict(cls, data: dict) -> ContentSnapshot:
        if (type(data) is not dict or set(data) != {"files", "directories"}
                or type(data["files"]) is not dict or type(data["directories"]) is not list):
            raise ValueError("无效的内容快照")
        files = {}
        for path, value in data["files"].items():
            if type(value) is not dict or set(value) != {"size", "sha256"}:
                raise ValueError("无效的快照文件")
            files[path] = FileContent(value["size"], value["sha256"])
        if any(type(path) is not str for path in data["directories"]):
            raise ValueError("无效的快照目录")
        return cls(files, tuple(data["directories"]))

    @property
    def digest(self) -> str:
        payload = {"files": {path: [content.size, content.sha256]
                             for path, content in self.files.items()},
                   "directories": list(self.directories)}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    def same_content(self, other: ContentSnapshot) -> bool:
        """Compare file contents only; empty directory differences are not dirty data.

        Directories remain in the snapshot/digest for empty-directory transfers.
        """
        return isinstance(other, ContentSnapshot) and self.files == other.files


_MAX_METADATA = 32 * 1024 * 1024
_MAX_ENTRIES = 100_000


class _CancelProbe:
    def __init__(self, callback):
        self.callback = callback

    def is_set(self) -> bool:
        return bool(self.callback())


def _check_cancel(cancelled) -> None:
    if cancelled is not None and (cancelled() if callable(cancelled) else cancelled.is_set()):
        raise RuntimeError("操作已取消")


def _json(text: str, limit: int = _MAX_METADATA):
    if type(text) is not str or len(text) > limit or len(text.encode("utf-8")) > limit:
        raise RuntimeError("元数据超过安全大小限制")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON 包含重复字段")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, TypeError, RecursionError) as exc:
        raise RuntimeError("元数据不是有效的 JSON") from exc


def _no_links(path: Path) -> None:
    path = filesystem_path(path)
    for ancestor in reversed((path, *path.parents)):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("路径不能包含符号链接、junction 或重解析点")


def _inspect_local_tree(path: Path, cancelled) -> None:
    path = filesystem_path(path)
    _no_links(path)
    try:
        if not path.exists():
            return  # Only rclone exit code 3 may authorize a missing snapshot.
        if not path.is_dir():
            raise ValueError("快照根路径必须是目录")
        pending = [path]
        count = 0
        while pending:
            _check_cancel(cancelled)
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    _check_cancel(cancelled)
                    count += 1
                    if count > _MAX_ENTRIES:
                        raise RuntimeError("目录项超过安全数量限制")
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise ValueError("目录树包含符号链接、junction 或重解析点")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(filesystem_path(Path(entry.path)))
                    elif not stat.S_ISREG(info.st_mode):
                        raise ValueError("目录树包含非普通文件")
    except OSError as exc:
        raise RuntimeError("无法安全读取本地目录树") from exc


def _exclude_args(excludes: tuple) -> list[str]:
    if type(excludes) is not tuple or any(type(value) is not str or not value
            or any(ord(char) < 32 for char in value) for value in excludes):
        raise ValueError("排除规则必须是不含控制字符的字符串元组")
    return [arg for pattern in excludes for arg in ("--exclude", pattern)]


def _inventory(text: str, cancelled) -> tuple[dict, list, dict]:
    entries = _json(text)
    if type(entries) is not list or len(entries) > _MAX_ENTRIES:
        raise RuntimeError("无效或过大的内容清单")
    files, directories, hashes = {}, [], {}
    for entry in entries:
        _check_cancel(cancelled)
        if type(entry) is not dict or type(entry.get("IsDir")) is not bool:
            raise ValueError("内容清单包含无效项")
        relative = entry.get("Path")
        if type(relative) is not str or len(relative) > 4096:
            raise ValueError("无效的内容路径")
        if entry["IsDir"]:
            directories.append(relative)
            continue
        size = entry.get("Size")
        if type(size) is not int or not 0 <= size <= 2**63 - 1 or relative in files:
            raise ValueError("文件大小无效或文件重复")
        files[relative] = size
        record = entry.get("Hashes", {})
        if type(record) is not dict:
            raise ValueError("文件哈希字段无效")
        for kind, value in record.items():
            if kind.casefold().replace("-", "") == "sha256" and value not in (None, ""):
                normalized = FileContent(size, value).sha256
                if relative in hashes and hashes[relative] != normalized:
                    raise ValueError("SHA256 哈希别名不一致")
                hashes[relative] = normalized
    validate_history_inventory(files, directories)
    if len(set(directories)) != len(directories):
        raise ValueError("目录清单有重复项")
    return files, directories, hashes


def snapshot_tree(rclone, path: str, excludes: tuple = (), *, allow_missing=False,
                  cancelled=None) -> ContentSnapshot | None:
    """Read actual SHA256 contents, never infer equality from size or timestamps.

    Missing roots return None only when explicitly allowed and lsjson reports code
    3. Directories are preserved but same_content deliberately ignores them.
    """
    if type(path) is not str or not path or any(ord(char) < 32 for char in path):
        raise ValueError("无效的快照根路径")
    flags = _exclude_args(excludes)
    _check_cancel(cancelled)
    is_remote = re.match(r"^[^/\\:]+:", path) and not re.match(r"^[a-zA-Z]:[/\\]", path)
    if not is_remote:
        _inspect_local_tree(Path(os.path.abspath(path)), cancelled)
    probe = _CancelProbe(cancelled) if callable(cancelled) else cancelled
    options = {"cancel_event": probe} if probe is not None else {}
    result = rclone.run(["lsjson", path, "--recursive", "--hash-type", "SHA-256",
                         "--no-modtime", "--no-mimetype", *flags], timeout=120, **options)
    _check_cancel(cancelled)
    if not result.ok:
        if allow_missing and result.code == 3:
            return None
        raise RuntimeError(f"读取内容清单失败：{result.err or result.out or result.code}")
    files, directories, hashes = _inventory(result.out, cancelled)
    if len(hashes) != len(files):
        result = rclone.run(["hashsum", "SHA256", path, "--download", *flags], timeout=1800, **options)
        _check_cancel(cancelled)
        if not result.ok:
            raise RuntimeError(f"下载内容校验失败：{result.err or result.out or result.code}")
        if len(result.out) > _MAX_METADATA or len(result.out.encode("utf-8")) > _MAX_METADATA:
            raise RuntimeError("校验清单超过安全大小限制")
        downloaded = {}
        lines = result.out.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        for line in lines:
            _check_cancel(cancelled)
            line = line.removesuffix("\r")
            match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
            if not match or match[2] not in files or match[2] in downloaded:
                raise ValueError("下载校验清单与文件清单不一致")
            downloaded[match[2]] = match[1].lower()
        if set(downloaded) != set(files) or any(downloaded[key] != value for key, value in hashes.items()):
            raise ValueError("下载校验清单不完整或内容在读取时改变")
        result = rclone.run(["lsjson", path, "--recursive", "--no-modtime", "--no-mimetype",
                             *flags], timeout=120, **options)
        _check_cancel(cancelled)
        if not result.ok:
            raise RuntimeError(f"无法复核下载后的文件清单：{result.err or result.out or result.code}")
        after_files, after_directories, _ = _inventory(result.out, cancelled)
        if after_files != files or set(after_directories) != set(directories):
            raise RuntimeError("下载校验期间文件清单改变，请重新预览")
        hashes = downloaded
    _check_cancel(cancelled)
    return ContentSnapshot({relative: FileContent(size, hashes[relative])
                            for relative, size in files.items()}, tuple(directories))


def _uuid(value: str) -> str:
    if type(value) is not str or not re.fullmatch(
            r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", value):
        raise ValueError("无效的 UUID")
    return uuid.UUID(value).hex


def read_file_namespace(rclone, path: str, *, allow_missing=False, cancelled=None):
    """Read actual, unfiltered file and directory names without hashing content."""
    _check_cancel(cancelled)
    if not re.match(r"^[^/\\:]+:", path) or re.match(r"^[a-zA-Z]:[/\\]", path):
        _inspect_local_tree(Path(os.path.abspath(path)), cancelled)
    options = {"cancel_event": cancelled} if cancelled is not None else {}
    result = rclone.run(["lsjson", path, "--recursive", "--no-modtime", "--no-mimetype"],
                        timeout=120, **options)
    _check_cancel(cancelled)
    if not result.ok:
        if allow_missing and result.code == 3:
            return None
        raise RuntimeError(f"无法核对完整文件名清单：{result.err or result.out or result.code}")
    files, directories, _ = _inventory(result.out, cancelled)
    return files, directories


def require_legacy_scope(task: MirrorTask) -> None:
    require_legacy_paths((task.local_path, task.local_history_path, task.remote_path, task.versions_path),
                         location="任务或历史根目录")
    require_legacy_rules(task.excludes)


def _task_scope(task: MirrorTask) -> dict:
    if not isinstance(task, MirrorTask) or not re.fullmatch(r"[0-9a-f]{32}", task.dataset_id):
        raise ValueError("数据集必须关联有效任务")
    validate_history_inventory((), (task.remote_path, task.versions_path))
    _exclude_args(task.excludes)
    roots = [tuple(map(portable_name_key, path.split("/"))) for path in (task.remote_path, task.versions_path)]
    if any(root[0] == ".phonenas" for root in roots):
        raise ValueError("数据与历史目录不能包含控制目录")
    if roots[0][:min(map(len, roots))] == roots[1][:min(map(len, roots))]:
        raise ValueError("数据与历史目录不能重叠")
    return {"dataset_id": task.dataset_id, "remote_path": task.remote_path,
            "versions_path": task.versions_path, "excludes": list(task.excludes)}


def _read_file(path: Path, limit: int = _MAX_METADATA) -> str:
    _no_links(path)
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("状态路径不是普通文件")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise RuntimeError("状态文件超过安全大小限制")
    return raw.decode("utf-8")


def _atomic_json(path: Path, payload: dict) -> None:
    _no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _no_links(path)
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        _no_links(path)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class BaselineStore:
    """Private local baseline. All storage roots are explicitly supplied by callers."""

    def __init__(self, root: Path, task: MirrorTask, config):
        self.root = Path(os.path.abspath(root))
        scope = _task_scope(task)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", task.id):
            raise ValueError("任务 ID 不能作为安全的状态文件名")
        self.path = self.root / f"{task.id}.json"
        self.binding = {**scope, "task_id": task.id, "direction": task.direction,
                        "url": config.url, "username": config.username,
                        "local_path": task.local_path, "local_history_path": task.local_history_path,
                        "sync_identity": _uuid(task.sync_identity)}

    def load(self, *, protocol: int | None = None) -> tuple[ContentSnapshot, str] | None:
        _no_links(self.path)
        try:
            payload = _json(_read_file(self.path))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError("本地基线已损坏或不可读") from exc
        try:
            if (type(payload) is not dict or set(payload) != {
                    "version", "binding", "snapshot", "generation", "content_digest"}
                    or type(payload["version"]) is not int or payload["version"] not in (LEGACY, LITERAL)
                    or type(payload["binding"]) is not dict
                    or set(payload["binding"]) != set(self.binding)):
                raise ValueError("无效的基线格式")
            binding = payload["binding"]
            if any(type(binding[key]) is not type(value) for key, value in self.binding.items()):
                raise ValueError("无效的基线绑定")
            _uuid(binding["sync_identity"])
            if (not re.fullmatch(r"[0-9a-f]{32}", binding["dataset_id"])
                    or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", binding["task_id"])
                    or binding["direction"] not in ("upload", "download")):
                raise ValueError("无效的基线任务身份")
            validate_history_inventory((), (binding["remote_path"], binding["versions_path"]))
            _exclude_args(tuple(binding["excludes"]))
            snapshot = ContentSnapshot.from_dict(payload["snapshot"])
            _uuid(payload["generation"])
            if payload["content_digest"] != snapshot.digest:
                raise ValueError("基线校验失败")
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("本地基线已损坏") from exc
        # Local archive routing affects recovery, not the identity of the
        # shared working copy. Keep the field on disk for older readers and
        # keep comparing it exactly in PendingOperation.
        if any(binding[key] != expected for key, expected in self.binding.items() if key != "local_history_path"):
            return None
        if protocol is not None and payload["version"] != protocol:
            raise RuntimeError("本地基线与手机数据集的文件名协议不同；请保留两端文件和原有状态，不会自动转换。")
        return snapshot, payload["generation"]

    def save(self, snapshot: ContentSnapshot, generation: str, *, protocol: int = LEGACY) -> None:
        if not isinstance(snapshot, ContentSnapshot):
            raise ValueError("基线必须是内容快照")
        if type(protocol) is not int or protocol not in (LEGACY, LITERAL):
            raise ValueError("无效的文件名协议")
        _uuid(generation)
        _atomic_json(self.path, {"version": protocol, "binding": self.binding,
                                "snapshot": snapshot.to_dict(), "generation": generation,
                                "content_digest": snapshot.digest})


_CONTROL_LIMIT = 64 * 1024
_PHASES = {"ready", "publishing", "interrupted"}


def _validate_dataset_state(value: dict, scope: dict) -> dict:
    required = {"version", *scope, "generation", "phase"}
    if (type(value) is not dict or not required.issubset(value)
            or set(value) - required - {"content_digest", "writer_identity"}
            or type(value["version"]) is not int or value["version"] not in (LEGACY, LITERAL)
            or any(type(value[key]) is not type(expected) or value[key] != expected for key, expected in scope.items())
            or type(value["phase"]) is not str or value["phase"] not in _PHASES):
        raise ValueError("数据集状态与任务目录、身份或排除规则不一致")
    _uuid(value["generation"])
    if "writer_identity" in value:
        _uuid(value["writer_identity"])
    digest = value.get("content_digest", "")
    if type(digest) is not str or (digest and not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("数据集内容校验值无效")
    return value


class DatasetCoordinator:
    """Dataset-scoped metadata and a shared exclusive lease for readers/writers."""

    def __init__(self, rclone, task: MirrorTask, lease_factory=None):
        self.rclone = rclone
        self.task = task
        self.scope = _task_scope(task)
        self.control = f"{C.DEFAULT_REMOTE}:{task.dataset_control_path}"
        self.lock_path = f".phonenas/locks/{task.dataset_id}"
        self._lease_factory = lease_factory

    def _state(self, value: dict) -> dict:
        return _validate_dataset_state(value, self.scope)

    def read(self) -> dict | None:
        result = self.rclone.run(["cat", f"{self.control}/state.json", "--head",
                                  str(_CONTROL_LIMIT + 1)], timeout=60)
        if not result.ok:
            if result.code in (3, 4):
                return None
            raise RuntimeError(f"读取数据集状态失败：{result.err or result.out or result.code}")
        try:
            return self._state(_json(result.out, _CONTROL_LIMIT))
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("数据集状态损坏或与任务不匹配") from exc

    def write(self, phase: str, generation: str, content_digest: str = "", *, protocol: int = LEGACY) -> None:
        state = self._state({"version": protocol, **self.scope, "phase": phase,
                             "generation": generation, "content_digest": content_digest,
                             "writer_identity": self.task.sync_identity})
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-dataset-") as root:
            source = Path(root) / "state.json"
            _atomic_json(source, state)
            for args in (["mkdir", self.control],
                         ["copyto", str(source), f"{self.control}/state.json", "--ignore-times"]):
                result = self.rclone.run(args, timeout=60)
                if not result.ok:
                    raise RuntimeError(f"写入数据集状态失败：{result.err or result.out or result.code}")

    def locked(self) -> bool:
        """Read-only preview guard. This must never create the lock or its parents."""
        result = self.rclone.run(["lsjson", f"{C.DEFAULT_REMOTE}:{self.lock_path}",
                                  "--stat", "--no-modtime", "--no-mimetype"], timeout=60)
        if result.ok:
            return True
        if result.code in (3, 4):
            return False
        raise RuntimeError(f"无法检查数据集锁：{result.err or result.out or result.code}")

    def _new_lease(self):
        return (self._lease_factory() if self._lease_factory
                else _WebDAVDatasetLease(self.rclone, self.task))

    @contextmanager
    def lease(self):
        with self._new_lease() as lease:
            yield lease

    @contextmanager
    def registration_lease(self, dataset_lease):
        """Serialize first registrations and purges across dataset IDs."""
        with dataset_lease.for_registration():
            yield

    @contextmanager
    def recovery_lease(self, expected_token: str):
        """Explicit recovery only, under the caller's job gate and approved journal.

        Token ownership cannot prove the original process has stopped. The caller
        must establish that before invoking recovery; this never runs implicitly.
        """
        with self._new_lease().recover(expected_token) as lease:
            yield lease


_DATASET_SCAN_LIMIT = 64
_DATASET_LIST_LIMIT = 64 * 1024
_DATASET_SCAN_TIMEOUT = 35
_DATASET_PATH_LIMIT = 4096  # Same bound as the file inventory.


def _registered_root(path: str) -> tuple[str, ...]:
    if type(path) is not str or len(path) > _DATASET_PATH_LIMIT:
        raise ValueError("远端目录无效或过长")
    if normalize_remote_path(path) != path:
        raise ValueError("必须使用原样的规范化远端目录")
    key = tuple(map(portable_name_key, path.split("/")))
    if ".phonenas" in key:
        raise ValueError("远端目录不能占用保留的 .phonenas 控制目录")
    return key


class _DatasetRegistry:
    """Bounded, read-only access to exact remote registrations."""

    control = f"{C.DEFAULT_REMOTE}:.phonenas/datasets"
    listing_flags = ("--no-modtime", "--no-mimetype")

    def __init__(self, rclone, cancelled=None):
        self.rclone = rclone
        self.cancelled = cancelled
        probe = _CancelProbe(cancelled) if callable(cancelled) else cancelled
        self.options = {"cancel_event": probe} if probe is not None else {}
        self.deadline = time.monotonic() + _DATASET_SCAN_TIMEOUT

    def _read(self, args: list[str], *, missing_ok=True) -> str | None:
        _check_cancel(self.cancelled)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("核对手机数据集登记超时")
        result = self.rclone.run([*args, "--retries", "1", "--low-level-retries", "1"],
                                 timeout=max(1, min(20, int(remaining))), **self.options)
        _check_cancel(self.cancelled)
        if not result.ok:
            if missing_ok and result.code in (3, 4):
                return None
            raise RuntimeError(f"读取手机数据集失败：{result.err or result.out or result.code}")
        return result.out

    def state(self, dataset_id: str) -> dict | None:
        path = f"{self.control}/{dataset_id}/state.json"
        raw = self._read(["lsjson", path, "--stat", *self.listing_flags])
        if raw is None:
            return None
        info = _json(raw, _CONTROL_LIMIT)
        # cat also accepts directories. Check the exact file first, then enforce
        # the byte limit again during transfer (in case the file has grown).
        if (type(info) is not dict or info.get("IsDir") is not False
                or info.get("Path") != "state.json" or info.get("Name", "state.json") != "state.json"
                or type(info.get("Size")) is not int or not 0 <= info["Size"] <= _CONTROL_LIMIT):
            raise RuntimeError("数据集状态文件无效或超过安全大小限制")
        raw = self._read(["cat", path, "--head", str(_CONTROL_LIMIT + 1), "--max-depth", "1",
                          "--max-transfer", f"{_CONTROL_LIMIT}B", "--cutoff-mode", "HARD"], missing_ok=False)
        value = _json(raw, _CONTROL_LIMIT)
        try:
            if type(value) is not dict or type(value.get("excludes")) is not list:
                raise ValueError("无效的状态或排除规则")
            _registered_root(value.get("remote_path"))
            _registered_root(value.get("versions_path"))
            task = MirrorTask(id="dataset-lookup", name="", enabled=False, local_path="",
                              remote_path=value["remote_path"], versions_path=value["versions_path"],
                              interval_minutes=C.DEFAULT_INTERVAL_MINUTES,
                              retention_days=C.DEFAULT_RETENTION_DAYS, exclude_preset="custom",
                              excludes=tuple(value["excludes"]), dataset_id=dataset_id)
            return _validate_dataset_state(value, _task_scope(task))
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("数据集状态损坏或与控制目录不匹配") from exc

    def ids(self) -> set[str]:
        raw = self._read(["lsjson", self.control, "--dirs-only", "--max-depth", "1", *self.listing_flags])
        if raw is None:
            return set()
        listing = _json(raw, _DATASET_LIST_LIMIT)
        if type(listing) is not list or len(listing) > _DATASET_SCAN_LIMIT:
            raise RuntimeError("数据集目录列表无效或超过安全数量限制")
        ids = set()
        for entry in listing:
            _check_cancel(self.cancelled)
            if type(entry) is not dict or type(entry.get("IsDir")) is not bool:
                raise RuntimeError("数据集目录列表包含无效项")
            name = entry.get("Path")
            if not entry["IsDir"]:
                continue
            if type(name) is not str or not re.fullmatch(r"[0-9a-f]{32}", name) or entry.get("Name", name) != name:
                raise RuntimeError("数据集控制目录包含未知或不安全的登记项")
            if name in ids:
                raise RuntimeError("数据集控制目录存在重复目录项")
            ids.add(name)
        return ids


def find_published_dataset(rclone, mirror: str, *, cancelled=None) -> dict | None:
    """Return a unique, exact ready record. Never guess an ambiguous binding."""
    _check_cancel(cancelled)
    mirror_key = _registered_root(mirror)
    registry = _DatasetRegistry(rclone, cancelled)
    canonical_id = dataset_id_for_remote(mirror)
    match = registry.state(canonical_id)
    if match is not None and match["remote_path"] != mirror:
        raise RuntimeError("数据集镜像目录的精确名称不匹配")
    for dataset_id in sorted(registry.ids() - {canonical_id}):
        record = registry.state(dataset_id)
        if record is None or _registered_root(record["remote_path"]) != mirror_key:
            continue
        if match is not None:
            raise RuntimeError("该镜像关联多个数据集，无法安全选择")
        match = record
    if match is not None:
        if match["remote_path"] != mirror:
            raise RuntimeError("数据集镜像目录的精确名称不匹配")
        if match["phase"] != "ready" or not match.get("content_digest"):
            raise RuntimeError("数据集尚未准备就绪或缺少内容校验值")
    _check_cancel(cancelled)
    return match


def verify_remote_scope(rclone, task: MirrorTask, *, require_ready=False, cancelled=None) -> None:
    """Reject overlaps with any other visible registered mirror or archive."""
    registry = _DatasetRegistry(rclone, cancelled)
    expected = _task_scope(task)
    paths = tuple(_registered_root(expected[key]) for key in ("remote_path", "versions_path"))
    ids = registry.ids()
    if require_ready:
        state = registry.state(task.dataset_id)
        if state is not None and state["phase"] != "ready":
            raise RuntimeError("数据集有未完成发布；不能永久删除手机历史。")
        if state is None or not state.get("content_digest"):
            raise RuntimeError("手机历史目录没有已发布的可核实归属，拒绝永久删除。")
        if any(state[key] != value for key, value in expected.items()):
            raise RuntimeError("手机历史目录的发布记录与任务不匹配，拒绝永久删除。")
    for dataset_id in sorted(ids - {task.dataset_id}):
        record = registry.state(dataset_id)
        if record is None:
            continue
        for key in ("remote_path", "versions_path"):
            other = _registered_root(record[key])
            if any(path[:min(len(path), len(other))] == other[:min(len(path), len(other))] for path in paths):
                raise RuntimeError("手机镜像或历史目录已与另一个数据集重叠；请使用独立目录。")
    _check_cancel(cancelled)


def _owner(task: MirrorTask, token: str) -> dict:
    return {"version": 1, "dataset_id": task.dataset_id,
            "writer_identity": task.sync_identity, "token": token}


class LocalDatasetLease:
    """Offline alias equivalent of an exclusive MKCOL; never steals stale locks."""

    def __init__(self, root: Path, task: MirrorTask):
        _task_scope(task)
        self.task = task
        self.path = Path(os.path.abspath(root)) / ".phonenas" / "locks" / task.dataset_id
        self.token = uuid.uuid4().hex
        self._held = False
        self._retained = False

    def for_registration(self):
        lease = LocalDatasetLease(self.path.parents[2], self.task)
        lease.path = self.path.parent / "directory-registration"
        return lease

    def __enter__(self):
        if self._held:
            raise RuntimeError("数据集锁不可重入")
        _no_links(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _no_links(self.path)
        try:
            os.mkdir(self.path, mode=0o700)
        except FileExistsError as exc:
            raise RuntimeError("数据集已锁定；不会自动接管旧锁") from exc
        self._held = True
        try:
            _atomic_json(self.path / "owner.json", _owner(self.task, self.token))
        except Exception:
            # Leave an unprovable acquisition in place rather than delete others' data.
            self._retained = True
            raise
        return self

    def retain(self) -> None:
        if not self._held:
            raise RuntimeError("没有持有数据集锁")
        self._retained = True

    def _verify_owner(self) -> None:
        owner = _json(_read_file(self.path / "owner.json", _CONTROL_LIMIT), _CONTROL_LIMIT)
        if owner != _owner(self.task, self.token):
            raise RuntimeError("锁所有者已改变")

    def _release(self) -> None:
        self._verify_owner()
        _no_links(self.path)
        (self.path / "owner.json").unlink()
        self.path.rmdir()

    @contextmanager
    def recover(self, expected_token: str):
        self.token = _uuid(expected_token)
        try:
            self._verify_owner()
        except (OSError, ValueError) as exc:
            raise RuntimeError("原锁所有权无法确认，拒绝恢复") from exc
        recovery = LocalDatasetLease.__new__(LocalDatasetLease)
        recovery.task, recovery.path = self.task, self.path / "recovery"
        recovery.token, recovery._held, recovery._retained = uuid.uuid4().hex, False, False
        with recovery:
            self._verify_owner()  # Check again after acquiring the exclusive child.
            self._held = True
            try:
                yield self
                if not self._retained:
                    self._verify_owner()
                    (self.path / "owner.json").unlink()  # Invalidate proof before releasing child.
            except BaseException:
                self._retained = True
                raise
            finally:
                self._held = False
        if not self._retained:
            _no_links(self.path)
            self.path.rmdir()

    def __exit__(self, exc_type, exc, traceback):
        if self._held and not self._retained:
            try:
                self._release()
            except (OSError, ValueError, RuntimeError) as cleanup:
                if exc is not None:
                    exc.add_note(f"数据集锁未释放：{cleanup}")
                else:
                    raise RuntimeError("数据集锁所有权无法确认；锁已保留") from cleanup
        self._held = False
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _WebDAVDatasetLease:
    """Atomic MKCOL, not rclone mkdir. HTTP cleanup ignores rclone cancellation.

    An unknown acquisition/write outcome is deliberately left locked. There is
    no stale-lock timeout or automatic recovery/steal mechanism.
    """

    def __init__(self, rclone, task: MirrorTask):
        self.rclone, self.task = rclone, task
        self.token = uuid.uuid4().hex
        self._held = self._retained = False
        config = rclone.config
        base = urllib.parse.urlsplit(config.url)
        if (base.scheme not in ("http", "https") or not base.netloc or base.username is not None
                or base.password is not None or base.query or base.fragment
                or any(ord(char) < 32 for char in config.url)):
            raise ValueError("数据集锁需要不含凭据、查询串或片段的 HTTP(S) WebDAV 地址")
        control = ".phonenas/locks/" + task.dataset_id
        self.url = config.url.rstrip("/") + "/" + urllib.parse.quote(control, safe="/") + "/"
        credentials = f"{config.username}:{config.password}".encode("utf-8")
        self.authorization = "Basic " + base64.b64encode(credentials).decode("ascii")
        # Ignore proxy environment and refuse every redirect, including same-host.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def for_registration(self):
        lease = _WebDAVDatasetLease(self.rclone, self.task)
        lease.url = lease.url.rstrip("/").rsplit("/", 1)[0] + "/directory-registration/"
        return lease

    def _request(self, method: str, url: str, data: bytes | None = None) -> tuple[int, bytes]:
        headers = {"Authorization": self.authorization}
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(_CONTROL_LIMIT + 1)
                if len(raw) > _CONTROL_LIMIT:
                    raise RuntimeError("WebDAV 锁响应超过安全大小限制")
                return response.status, raw
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            return code, b""
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError("WebDAV 数据集锁请求未确认完成") from exc

    def __enter__(self):
        if self._held:
            raise RuntimeError("数据集锁不可重入")
        result = self.rclone.run(["mkdir", f"{C.DEFAULT_REMOTE}:.phonenas/locks"], timeout=60)
        if not result.ok:
            raise RuntimeError(f"无法建立锁父目录：{result.err or result.out or result.code}")
        status, _ = self._request("MKCOL", self.url)
        if status == 405:
            raise RuntimeError("数据集已锁定；不会自动接管旧锁")
        if status != 201:
            raise RuntimeError(f"WebDAV 不支持独占数据集锁或请求失败（HTTP {status}）")
        self._held = True
        owner = json.dumps(_owner(self.task, self.token), separators=(",", ":")).encode("utf-8")
        try:
            status, _ = self._request("PUT", self.url + "owner.json", owner)
            if status not in (200, 201, 204):
                raise RuntimeError(f"锁所有者写入未确认（HTTP {status}）")
        except Exception:
            self._retained = True
            raise  # No token proof: fail closed and leave the acquired directory.
        return self

    def retain(self) -> None:
        if not self._held:
            raise RuntimeError("没有持有数据集锁")
        self._retained = True

    def _verify_owner(self) -> None:
        status, raw = self._request("GET", self.url + "owner.json")
        if status != 200 or _json(raw.decode("utf-8"), _CONTROL_LIMIT) != _owner(self.task, self.token):
            raise RuntimeError("数据集锁所有者无法确认")

    def _release(self) -> None:
        self._verify_owner()
        status, _ = self._request("DELETE", self.url)
        if status not in (200, 204):
            raise RuntimeError(f"锁删除未确认（HTTP {status}）")

    @contextmanager
    def recover(self, expected_token: str):
        self.token = _uuid(expected_token)
        self._verify_owner()
        recovery = _WebDAVDatasetLease(self.rclone, self.task)
        recovery.url = self.url + "recovery/"
        status, _ = recovery._request("MKCOL", recovery.url)
        if status != 201:
            raise RuntimeError(f"恢复已在进行或无法独占恢复锁（HTTP {status}）")
        owner = json.dumps(_owner(self.task, recovery.token), separators=(",", ":")).encode("utf-8")
        status, _ = recovery._request("PUT", recovery.url + "owner.json", owner)
        if status not in (200, 201, 204):
            raise RuntimeError("恢复锁所有者写入未确认；拒绝接管")
        recovery._held = True
        try:
            self._verify_owner()
            self._held = True
            try:
                yield self
            except BaseException:
                self._retained = True
                raise
            finally:
                self._held = False
            if not self._retained:
                recovery._verify_owner()
                self._release()  # DELETE primary atomically removes its owned recovery child.
                recovery._held = False
        finally:
            if recovery._held:
                recovery.__exit__(None, None, None)

    def __exit__(self, exc_type, exc, traceback):
        if self._held and not self._retained:
            try:
                self._release()
            except (OSError, ValueError, RuntimeError) as cleanup:
                if exc is not None:
                    exc.add_note(f"数据集锁未释放：{cleanup}")
                else:
                    raise RuntimeError("数据集锁未能释放，已保留供人工核查") from cleanup
        self._held = False
        return False
