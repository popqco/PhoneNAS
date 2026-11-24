from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from app import constants as C
from app.core.filenames import (
    LEGACY, LITERAL, display_path as display_path, is_legacy_ambiguous, portable_name_key,
    relative_parts, require_legacy_paths,
)
from app.core.retention import parse_version_name
from app.core.tasks import MirrorTask
from app.core.winpath import filesystem_path


def _version_name(name: str) -> str:
    # retention's parser deliberately tolerates slashes/whitespace for cleanup;
    # history actions must never canonicalize an untrusted path into a version.
    if not isinstance(name, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}", name):
        raise ValueError("无效的历史时间戳")
    if parse_version_name(name) is None:
        raise ValueError("无效的历史时间戳")
    return name


def validate_history_inventory(
    paths: Iterable[str], directories: Iterable[str] = (),
) -> list[str]:
    """Validate a complete portable tree, including aliases of intermediate dirs.

    Inputs use slash-separated relative paths. Explicit directories let local
    inventories validate empty directories that a remote files-only listing omits.
    Return sorted file paths; reject unsafe trees rather than filtering them.
    """
    aliases: dict[tuple[str, ...], tuple[tuple[str, ...], bool]] = {}

    def add(path: str, file_entry: bool) -> None:
        parts = relative_parts(path)
        for length in range(1, len(parts) + 1):
            spelling = parts[:length]
            key = tuple(map(portable_name_key, spelling))
            is_file = file_entry and length == len(parts)
            previous = aliases.get(key)
            if previous is not None:
                old_spelling, old_is_file = previous
                if old_spelling != spelling or old_is_file or is_file:
                    raise ValueError("历史文件存在大小写、Unicode 或文件/目录别名冲突，已停止操作")
            aliases[key] = (spelling, is_file)

    for directory in directories:
        add(directory, False)
    result = []
    for path in paths:
        add(path, True)
        result.append(path)
    return sorted(result)


def _no_links(path: Path) -> None:
    """Inspect lexical ancestors before resolve can hide a symlink/junction."""
    path = filesystem_path(path)
    for ancestor in reversed((path, *path.parents)):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("恢复目录及其祖先不能是符号链接、junction 或重解析点")


def _within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


class HistoryService:
    """History batches for one task; infer the protocol from shared state for standalone actions."""

    def __init__(self, rclone, task: MirrorTask, *, protocol: int | None = None, lease_factory=None):
        if not isinstance(task, MirrorTask):
            raise ValueError("历史文件必须关联有效任务")
        versions = tuple(map(portable_name_key, relative_parts(task.versions_path)))
        remote = tuple(map(portable_name_key, relative_parts(task.remote_path)))
        if versions[:len(remote)] == remote or remote[:len(versions)] == versions:
            raise ValueError("历史目录不能与任务镜像目录重叠")
        self.rclone = rclone
        self.task = task
        self.protocol = protocol
        self._lease_factory = lease_factory

    @contextmanager
    def _history_lease(self, *, deleting: bool = False):
        from app.core.pending import PendingOperation
        from app.core.relay import BaselineStore, DatasetCoordinator, verify_remote_scope

        if self.protocol is not None:
            # SyncEngine already holds the appropriate leases and checked scope.
            yield
            return
        config = self.rclone.config
        journal = PendingOperation(BaselineStore(config.path.parent / "baselines", self.task, config))
        coordinator = DatasetCoordinator(self.rclone, self.task, lease_factory=self._lease_factory)
        if coordinator.locked():
            raise RuntimeError("数据集有其他操作或未完成发布；不会接管其锁或处理历史文件。")
        verify_remote_scope(self.rclone, self.task, require_ready=True)
        operation = "history_delete" if deleting else "history_restore"
        with coordinator.control_lease(journal, operation, registering=deleting) as lease:
            if deleting:
                with coordinator.registration_lease(lease):
                    verify_remote_scope(self.rclone, self.task, require_ready=True)
                    yield
            else:
                # An exact published registration prevents other dataset IDs
                # from claiming this archive while its own lease is held.
                verify_remote_scope(self.rclone, self.task, require_ready=True)
                yield

    def _require_batch_names(self, paths: list[str]) -> None:
        from app.core.relay import DatasetCoordinator

        names = [self.task.versions_path, *paths]
        if not any(map(is_legacy_ambiguous, names)):
            return  # An ordinary batch needs no network round-trip for protocol.
        protocol = self.protocol
        if protocol is None:
            state = DatasetCoordinator(self.rclone, self.task).read()
            protocol = state["version"] if state else LEGACY
        if protocol != LITERAL:
            require_legacy_paths(names, location="历史批次路径")

    def list_versions(self) -> list[str]:
        result = self.rclone.lsf_dirs(self.task.versions_path)
        if not result.ok:
            raise RuntimeError(f"读取历史文件失败：{result.err or result.out or result.code}")
        names = set()
        for line in result.out.split("\n"):
            line = line.removesuffix("\r")
            name = line.removesuffix("/")
            try:
                names.add(_version_name(name))
            except ValueError:
                continue
        return sorted(names, reverse=True)

    def list_files(self, version: str) -> list[str]:
        version = _version_name(version)
        result = self.rclone.run([
            "lsjson", f"{C.DEFAULT_REMOTE}:{self.task.versions_path}/{version}",
            "--recursive", "--no-modtime", "--no-mimetype",
        ], timeout=120)
        if not result.ok:
            raise RuntimeError(f"读取历史文件清单失败：{result.err or result.out or result.code}")
        try:
            entries = json.loads(result.out)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("远端返回了无效的历史文件清单") from exc
        if not isinstance(entries, list):
            raise RuntimeError("远端返回了无效的历史文件清单")
        paths, directories = [], []
        for entry in entries:
            if not isinstance(entry, dict) or type(entry.get("IsDir")) is not bool:
                raise ValueError("远端文件清单包含无效项")
            (directories if entry["IsDir"] else paths).append(entry.get("Path"))
        names = validate_history_inventory(paths, directories)
        self._require_batch_names([*directories, *names])
        return names

    def restore(self, version: str, destination: Path, files: Iterable[str] | None = None) -> Path:
        if self.protocol is not None:
            raise ValueError("恢复手机历史必须从真实发布记录核对文件名协议和归属。")
        version = _version_name(version)
        self.list_files(version)
        with self._history_lease():
            return self._restore(version, destination, files)

    def _restore(self, version: str, destination: Path, files: Iterable[str] | None) -> Path:
        # Validate the *entire* inventory even for a selected-file restore. Never
        # pass an untrusted tree to rclone copy/sync, or partially trust a listing.
        available = self.list_files(version)
        if files is None:
            selected = available
        else:
            if isinstance(files, (str, bytes)):
                raise ValueError("请选择文件列表，而不是单个路径字符串")
            selected = list(files)
            for path in selected:
                relative_parts(path)
            if len(set(selected)) != len(selected) or any(path not in available for path in selected):
                raise ValueError("选择的文件不在当前历史批次中或有重复")
        if not selected:
            raise ValueError("该批次没有可恢复的文件")

        destination = Path(os.path.abspath(destination))
        _no_links(destination)
        if not destination.is_dir():
            raise ValueError("请选择一个已存在的本地恢复目录")
        destination = destination.resolve(strict=True)
        local = Path(os.path.abspath(self.task.local_path)).resolve()
        local_history = Path(os.path.abspath(self.task.local_history_path)).resolve()
        if _within(destination, local) or _within(destination, local_history):
            raise ValueError("恢复目录不能位于任务的本地源文件夹或历史目录内")
        # mkdtemp creates an exclusive, new child even when a prior recovery of
        # the same timestamp exists. No user path is ever used as an overwrite.
        target = Path(tempfile.mkdtemp(prefix=f"PhoneNAS-{version}-", dir=destination))
        restored = 0
        try:
            _no_links(target)
            if not _within(target.resolve(strict=True), destination):
                raise ValueError("恢复目录超出所选目录")
            for relative in selected:
                output_display = target.joinpath(*relative_parts(relative))
                output = filesystem_path(output_display)
                _no_links(output)
                if not _within(output_display, target):
                    raise ValueError("恢复文件超出专用恢复目录")
                output.parent.mkdir(parents=True, exist_ok=True)
                _no_links(output)
                # Reserve the name exclusively, preventing any pre-existing file
                # (including another case spelling) from being overwritten.
                with output.open("xb"):
                    pass
                result = self.rclone.copyto(
                    f"{C.DEFAULT_REMOTE}:{self.task.versions_path}/{version}/{relative}", str(output_display),
                )
                if not result.ok:
                    raise RuntimeError(result.err or result.out or f"退出码 {result.code}")
                restored += 1
        except Exception as exc:
            raise RuntimeError(
                f"恢复未全部完成（已完成 {restored}/{len(selected)} 个文件）。"
                f"部分结果保留在 {target}；未完成文件可能是空文件或残缺文件。原因：{exc}"
            ) from exc
        return target

    def delete(self, version: str) -> None:
        version = _version_name(version)
        if version not in self.list_versions():
            raise ValueError("该历史批次已不在当前任务的远端列表中；请刷新后重试")
        self.list_files(version)
        with self._history_lease(deleting=True):
            # Recheck the whole batch, including empty directories, under the
            # shared lock. A timestamp alone is not authority to purge it.
            if self.protocol is None:
                self.list_files(version)
            result = self.rclone.purge(f"{self.task.versions_path}/{version}")
            if not result.ok:
                raise RuntimeError(
                    f"永久删除未确认完成，远端可能已部分删除；请刷新检查：{result.err or result.out or result.code}"
                )


def _absolute_local_path(value: str | os.PathLike[str]) -> Path:
    """Check a local spelling before resolving can hide links or Win32 aliases."""
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or raw.replace("/", "\\").startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError("本地历史和恢复目录必须是普通的绝对路径")
    path = Path(raw)
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError("本地历史和恢复目录必须是非根目录的绝对路径")
    relative_parts("/".join(path.parts[1:]))
    _no_links(path)
    return path.resolve()


def validate_local_history_path(value: str) -> Path:
    """A history root may be absent, but it cannot be a file or under one."""
    path = _absolute_local_path(value)
    for ancestor in reversed((path, *path.parents)):
        try:
            mode = filesystem_path(ancestor).stat().st_mode
        except FileNotFoundError:
            break
        if not stat.S_ISDIR(mode):
            raise ValueError(f"本地历史目录（或其上级）是文件，不是文件夹：{ancestor}")
    return path


def _local_overlap(left: Path, right: Path) -> bool:
    # Refuse aliases even on a case-sensitive host: histories are portable to
    # Windows, and normalization aliases must not evade the isolation check.
    first = tuple(map(portable_name_key, left.parts))
    second = tuple(map(portable_name_key, right.parts))
    length = min(len(first), len(second))
    return first[:length] == second[:length]


class LocalHistoryService:
    """Offline history batches written by a download task's rclone backup-dir."""

    def __init__(self, task: MirrorTask):
        if not isinstance(task, MirrorTask):
            raise ValueError("历史文件必须关联有效任务")
        self.task = task
        self.root = _absolute_local_path(task.local_history_path)
        self.local = _absolute_local_path(task.local_path)
        if _local_overlap(self.root, self.local):
            raise ValueError("本地历史目录不能与任务的本地文件夹重叠")

    def list_versions(self) -> list[str]:
        try:
            _no_links(self.root)
            _no_links(self.local)
            try:
                entries = list(filesystem_path(self.root).iterdir())
            except FileNotFoundError:
                return []
            names = []
            for entry in entries:
                try:
                    name = _version_name(entry.name)
                except ValueError:
                    continue
                _no_links(entry)
                if stat.S_ISDIR(filesystem_path(entry).lstat().st_mode):
                    names.append(name)
            return sorted(names, reverse=True)
        except OSError as exc:
            raise RuntimeError(f"读取本地历史批次失败：{exc}") from exc

    def _inventory(self, version: str) -> tuple[list[str], list[str]]:
        version = _version_name(version)
        batch = self.root / version
        batch_fs = filesystem_path(batch)
        try:
            _no_links(self.root)
            _no_links(self.local)
            _no_links(batch)
            if not stat.S_ISDIR(batch_fs.lstat().st_mode):
                raise ValueError("该历史批次不是普通目录")
            paths, directories = [], []
            pending = [(batch_fs, "")]
            while pending:
                directory, prefix = pending.pop()
                _no_links(directory)
                for entry in directory.iterdir():
                    relative = f"{prefix}/{entry.name}" if prefix else entry.name
                    relative_parts(relative)
                    _no_links(entry)
                    info = filesystem_path(entry).lstat()
                    if stat.S_ISDIR(info.st_mode):
                        directories.append(relative)
                        pending.append((filesystem_path(entry), relative))
                    elif stat.S_ISREG(info.st_mode):
                        paths.append(relative)
                    else:
                        raise ValueError("本地历史文件清单包含非普通文件或目录")
        except FileNotFoundError as exc:
            raise ValueError("该历史批次或文件已不在本地目录中；请刷新后重试") from exc
        except OSError as exc:
            raise RuntimeError(f"读取本地历史文件清单失败：{exc}") from exc
        return validate_history_inventory(paths, directories), directories

    def list_files(self, version: str) -> list[str]:
        files, _ = self._inventory(version)
        return files

    def restore(self, version: str, destination: Path, files: Iterable[str] | None = None) -> Path:
        version = _version_name(version)
        available = self.list_files(version)
        if files is None:
            selected = available
        else:
            if isinstance(files, (str, bytes)):
                raise ValueError("请选择文件列表，而不是单个路径字符串")
            selected = list(files)
            for path in selected:
                relative_parts(path)
            if len(set(selected)) != len(selected) or any(path not in available for path in selected):
                raise ValueError("选择的文件不在当前历史批次中或有重复")
        if not selected:
            raise ValueError("该批次没有可恢复的文件")
        destination = _absolute_local_path(destination)
        if not destination.is_dir():
            raise ValueError("请选择一个已存在的本地恢复目录")
        if _local_overlap(destination, self.local) or _local_overlap(destination, self.root):
            raise ValueError("恢复目录不能与任务本地文件夹或本地历史目录重叠")
        target = Path(tempfile.mkdtemp(prefix=f"PhoneNAS-{version}-", dir=destination))
        restored = 0
        try:
            for relative in selected:
                source = filesystem_path(self.root / version / relative)
                output = filesystem_path(target.joinpath(*relative_parts(relative)))
                _no_links(source)
                info = source.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("本地历史文件已改变；请刷新后重试")
                _no_links(output)
                output.parent.mkdir(parents=True, exist_ok=True)
                _no_links(output)
                with source.open("rb") as incoming:
                    # A source may have changed since inventory (or during open).
                    # Check the live ancestors and opened object before any read.
                    _no_links(source)
                    opened = os.fstat(incoming.fileno())
                    if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(info, opened):
                        raise ValueError("本地历史文件在恢复前已改变；请刷新后重试")
                    with output.open("xb") as outgoing:
                        _no_links(output)
                        shutil.copyfileobj(incoming, outgoing)
                restored += 1
        except Exception as exc:
            raise RuntimeError(
                f"恢复未全部完成（已完成 {restored}/{len(selected)} 个文件）。"
                f"部分结果保留在 {target}；未完成文件可能是空文件或残缺文件。原因：{exc}"
            ) from exc
        return target

    def delete(self, version: str) -> None:
        version = _version_name(version)
        if version not in self.list_versions():
            raise ValueError("该历史批次已不在当前任务的本地列表中；请刷新后重试")
        files, directories = self._inventory(version)
        batch = self.root / version
        try:
            # Never hand a tree to rmtree: only unlink the validated inventory,
            # and inspect each entry and every ancestor again before mutation.
            for relative in files:
                path = filesystem_path(batch.joinpath(*relative_parts(relative)))
                _no_links(path)
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError("本地历史文件已改变；请刷新后重试")
                path.unlink()
            for relative in sorted(directories, key=lambda item: len(item.split("/")), reverse=True):
                path = filesystem_path(batch.joinpath(*relative_parts(relative)))
                _no_links(path)
                if not stat.S_ISDIR(path.lstat().st_mode):
                    raise ValueError("本地历史目录已改变；请刷新后重试")
                path.rmdir()
            _no_links(batch)
            filesystem_path(batch).rmdir()
        except Exception as exc:
            raise RuntimeError(
                f"永久删除未确认完成，本地历史可能已部分删除；请刷新检查 {batch}：{exc}"
            ) from exc
