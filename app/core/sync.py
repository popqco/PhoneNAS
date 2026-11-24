from __future__ import annotations

import hashlib
import json
import threading
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from app import constants as C
from app.core.config import Config, data_dir
from app.core.filenames import LEGACY, LITERAL, require_legacy_paths
from app.core.log import get_logger
from app.core.progress import SyncProgress
from app.core.rclone import Rclone
from app.core.retention import old_versions, version_timestamp
from app.core.tasks import MirrorTask, validate_source, validate_tasks
from app.core.winpath import staging_directory

if TYPE_CHECKING:
    from app.core.relay import ContentSnapshot


@dataclass(frozen=True)
class _SyncPlan:
    local: ContentSnapshot
    remote: ContentSnapshot
    metadata: dict | None
    source: ContentSnapshot
    destination: ContentSnapshot
    marks: dict[str, str]
    protocol: int
    backup: ContentSnapshot | None


@dataclass(frozen=True)
class BaselineInitializationPreview:
    digest: str
    file_count: int
    local_digest: str
    remote_digest: str
    binding_digest: str
    generation: str


@dataclass(frozen=True)
class BaselineInitializationResult:
    generation: str
    file_count: int
    created_metadata: bool


def _canonical_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SyncReport:
    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    changes: tuple[tuple[str, str], ...] = ()
    digest: str = ""
    operation: str = "publish"
    blocked: bool = False
    reason: str = ""
    requires_confirmation: bool = False

    @property
    def source_files(self) -> int:
        return self.added + self.updated + self.unchanged

    @property
    def destination_files(self) -> int:
        return self.deleted + self.updated + self.unchanged

    @property
    def summary(self) -> str:
        return f"新增 {self.added} · 更新 {self.updated} · 删除 {self.deleted} · 未变化 {self.unchanged}"


@dataclass(frozen=True)
class _LockReleasePlan:
    report: SyncReport
    token: str
    dataset: bool
    registration: bool
    control_only: bool


def parse_sync_report(text: str) -> SyncReport:
    entries: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        if len(line) < 3 or line[1] != " " or line[0] not in "=+-*!":
            raise ValueError("差异报告含有无法识别的文件名或记录，已停止执行")
        mark, name = line[0], line[2:]
        if mark == "!":
            raise ValueError(f"无法读取或比较文件：{name}")
        if name in entries and entries[name] != mark:
            raise ValueError(f"比较期间文件发生变化：{name}，请重新预演")
        entries[name] = mark
    ordered = tuple(sorted(entries.items()))
    digest = hashlib.sha256(json.dumps(ordered, ensure_ascii=False).encode("utf-8")).hexdigest()
    return SyncReport(
        added=sum(v == "+" for v in entries.values()),
        updated=sum(v == "*" for v in entries.values()),
        deleted=sum(v == "-" for v in entries.values()),
        unchanged=sum(v == "=" for v in entries.values()),
        changes=tuple((mark, name) for name, mark in ordered if mark != "=")[:500],
        digest=digest,
    )


def preview_guard(task: MirrorTask, report: SyncReport) -> str:
    if report.reason:
        return report.reason
    receiving = report.operation in ("receive", "adopt") or (
        report.operation == "publish" and task.direction == "download"
    )
    destination = "本地接收目录" if receiving else "手机镜像"
    if report.source_files == 0 and report.deleted:
        return f"来源文件夹为空，但{destination}仍有文件；需要明确确认本次删除。"
    if report.deleted >= 100 or (
        report.deleted >= 20 and report.deleted / max(1, report.destination_files) >= 0.25
    ):
        return f"本次将删除{destination}中的 {report.deleted} 个文件，超过自动执行保护阈值。"
    if task.last_file_count >= 20 and report.source_files < task.last_file_count * 0.2:
        return "来源文件数量较上次骤减超过 80%，请检查来源后确认。"
    if not task.last_sync_at and report.destination_files:
        return "这是首次同步，但目标目录已有文件；请先核对目录归属和差异。"
    return ""


def build_sync_args(
    task: MirrorTask, timestamp: str, *, dry_run: bool = False,
    report_path: Path | None = None, max_delete: int | None = None,
) -> list[str]:
    receiving = task.direction == "download"
    remote = f"{C.DEFAULT_REMOTE}:{task.remote_path}"
    source, destination = (remote, task.local_path) if receiving else (task.local_path, remote)
    history = str(Path(task.local_history_path) / timestamp) if receiving else f"{C.DEFAULT_REMOTE}:{task.versions_path}/{timestamp}"
    # The phone kernel serializes PUTs with a writers lock, so extra client
    # transfers only contend and slow the upload down.
    args = [
        "sync", source, destination,
        "--backup-dir", history,
        "--transfers", "1", "--checkers", "8", "--create-empty-src-dirs",
        "--check-first", "--delete-after", "--stats", "2s", "--stats-one-line",
        "--use-json-log", "--log-level", "INFO", "--retries", "1",
        "--timeout", "60s", "--contimeout", "15s", "--low-level-retries", "2",
    ]
    for pattern in task.excludes:
        args.extend(["--exclude", pattern])
    if dry_run:
        args.append("--dry-run")
    if report_path is not None:
        args.extend(["--combined", str(report_path)])
    if max_delete is not None:
        args.extend(["--max-delete", str(max_delete)])
    return args


class SyncEngine:
    def __init__(
        self, rclone: Rclone, task: MirrorTask, *, mount_drive: str = "X:",
        log_directory: Path | None = None, progress=None, cancelled: threading.Event | None = None,
        state_directory: Path | None = None, operation: str = "", lease_factory=None,
        progress_event=None, begin_commit=None,
    ):
        self.rclone = rclone
        self.task = task
        self.mount_drive = mount_drive
        self.log_directory = log_directory
        self.progress = progress or (lambda _text: None)
        self.progress_event = progress_event or (lambda _event: None)
        self._current_progress = SyncProgress("preparing", "正在准备任务…")
        self.cancelled = cancelled or threading.Event()
        self.begin_commit = begin_commit or self._check_cancel
        self.state_directory = state_directory
        self.recovering = operation == "recover"
        self.operation = operation or ("receive" if task.direction == "download" else "publish")
        if self.operation not in ("publish", "receive", "return", "adopt", "recover"):
            raise ValueError("未知的同步操作")
        if self.operation == "return" and task.direction != "download":
            raise ValueError("只有接收任务可以交回本地修改")
        if self.operation == "adopt" and task.direction != "upload":
            raise ValueError("只有发布任务需要接收交回的手机更新")
        self.lease_factory = lease_factory
        self._plan: _SyncPlan | None = None

    def _emit_progress(self, event: SyncProgress) -> None:
        self._current_progress = event
        self.progress_event(event)
        self.progress(event.display())

    def _stage(self, stage: str, message: str, *, items_done=None, items_total=None) -> None:
        self._emit_progress(SyncProgress(stage, message, items_done=items_done, items_total=items_total))

    def _snapshot(self, stage: str, message: str, path: str, excludes: tuple = (), *, allow_missing=False):
        from app.core.relay import snapshot_tree
        self._stage(stage, message)
        try:
            return snapshot_tree(self.rclone, path, excludes, allow_missing=allow_missing, cancelled=self.cancelled)
        except ValueError as exc:
            location = "手机目录" if path.startswith(f"{C.DEFAULT_REMOTE}:") else "本地目录"
            raise ValueError(f"{location}检查未通过：{exc}") from exc

    @property
    def receiving(self) -> bool:
        return self.operation in ("receive", "adopt")

    def _check_cancel(self) -> None:
        if self.cancelled.is_set():
            raise RuntimeError("操作已取消")

    def _validate(self) -> None:
        from app.core.history import _no_links, validate_local_history_path
        self._check_cancel()
        validate_tasks((self.task,), self.mount_drive)
        validate_source(self.task, self.mount_drive)
        _no_links(Path(self.task.local_path))
        validate_local_history_path(self.task.local_history_path)

    def _context(self):
        from app.core.relay import BaselineStore, DatasetCoordinator
        directory = self.state_directory or data_dir() / "baselines"
        return (
            BaselineStore(directory, self.task, self.rclone.config),
            DatasetCoordinator(self.rclone, self.task, lease_factory=self.lease_factory),
        )

    def _namespace(self, path: str, *, allow_missing: bool = False):
        from app.core.relay import read_file_namespace

        try:
            return read_file_namespace(self.rclone, path, allow_missing=allow_missing, cancelled=self.cancelled)
        except ValueError as exc:
            location = "手机目录" if path.startswith(f"{C.DEFAULT_REMOTE}:") else "本地目录"
            raise ValueError(f"{location}检查未通过：{exc}") from exc

    def _verify_namespace(self, protocol: int, *, allow_missing: bool, recorded=()) -> str:
        from app.core.history import validate_history_inventory
        from app.core.relay import require_legacy_scope

        if protocol == LEGACY:
            require_legacy_scope(self.task)
            for snapshot in recorded:
                require_legacy_paths(snapshot.directories, location="旧记录中的目录")
                require_legacy_paths(snapshot.files, location="旧记录中的文件")
        # Filters select managed files, not what Win32 (or a case-insensitive
        # DAV target) might address when applying that plan. Check both full
        # namespaces before permitting a write to a filtered tree.
        if protocol != LEGACY and not self.task.excludes:
            return ""
        local = self._namespace(self.task.local_path)
        remote = self._namespace(f"{C.DEFAULT_REMOTE}:{self.task.remote_path}", allow_missing=allow_missing)
        if protocol == LEGACY:
            for files, directories in (items for items in (local, remote) if items is not None):
                require_legacy_paths(directories, location="未过滤目录")
                require_legacy_paths(files, location="未过滤文件")
        if local is not None and remote is not None:
            try:
                validate_history_inventory(local[0].keys() | remote[0].keys(), set(local[1]) | set(remote[1]))
            except ValueError as exc:
                return str(exc)
        return ""

    def _new_protocol(self, store) -> int:
        # An unmarked existing mirror/history, or an orphaned local baseline,
        # is never presumed fresh based on a filtered snapshot.
        if store.path.exists():
            return LEGACY
        remote_root = f"{C.DEFAULT_REMOTE}:{self.task.remote_path}"
        mirror = self._namespace(remote_root, allow_missing=True)
        if mirror is not None and any(mirror):
            return LEGACY
        # Browsing may select a pre-created empty directory. The verified
        # unfiltered namespace must be empty, not just free of files or of
        # managed matches; old batches and even empty children still count.
        for history in (f"{C.DEFAULT_REMOTE}:{self.task.versions_path}", self.task.local_history_path):
            archive = self._namespace(history, allow_missing=True)
            if archive is not None and any(archive):
                return LEGACY
        return LITERAL

    def _verify_protocol(self, store, metadata, pending):
        protocol = metadata["version"] if metadata else pending["version"] if pending else self._new_protocol(store)
        if pending is not None and pending["version"] != protocol:
            raise RuntimeError("旧未完成操作与当前手机的文件名协议不同；保留原记录和锁，不能续作。")
        baseline = store.load(protocol=protocol)
        recorded = (baseline[0],) if baseline else ()
        if pending is not None:
            recorded += (pending["local"], pending["remote"])
        allow_missing = not self.receiving and self.operation == "publish"
        conflict = self._verify_namespace(protocol, allow_missing=allow_missing, recorded=recorded)
        return protocol, baseline, conflict

    def _verify_recovery_history(self, pending, protocol: int):
        from app.core.history import validate_history_inventory
        from app.core.relay import ContentSnapshot

        history_root = self._history_root(pending["stamp"])
        backup = self._snapshot("check_history", "正在核对续作要使用的原历史批次…", history_root, allow_missing=True)
        backup = backup or ContentSnapshot({})
        if protocol == LEGACY:
            require_legacy_paths(backup.directories, location="旧历史批次目录")
            require_legacy_paths(backup.files, location="旧历史批次文件")
        original = pending["local"] if self.receiving else pending["remote"]
        try:
            validate_history_inventory(backup.files.keys() | original.files.keys(),
                                       set(backup.directories) | set(original.directories))
        except ValueError as exc:
            return backup, str(exc)
        # Never overwrite an unrelated or modified copy in the reused batch,
        # even if the corresponding old file is still present at the target.
        if any(item != original.files.get(path) for path, item in backup.files.items()):
            return backup, "原历史批次含有未能证明为旧目标内容的文件，不能安全复用该批次。"
        return backup, ""

    def _history_root(self, stamp: str) -> str:
        if self.receiving:
            return str(Path(self.task.local_history_path) / stamp)
        return f"{C.DEFAULT_REMOTE}:{self.task.versions_path}/{stamp}"

    @staticmethod
    def _baseline_initialization_preview(store, local, remote, metadata, baseline, protocol):
        binding_digest = _canonical_digest(store.binding)
        generation = metadata["generation"] if metadata else ""
        return BaselineInitializationPreview(
            digest=_canonical_digest({
                "operation": "initialize_baseline", "binding_digest": binding_digest,
                "baseline_path": str(store.path), "local_digest": local.digest,
                "remote_digest": remote.digest, "metadata": metadata, "filename_protocol": protocol,
                "baseline": [baseline[0].digest, baseline[1]] if baseline else None,
            }),
            file_count=len(local.files), local_digest=local.digest,
            remote_digest=remote.digest, binding_digest=binding_digest,
            generation=generation,
        )

    def _baseline_initialization_state(self, *, owned_token: str = ""):
        from app.core.pending import PendingOperation
        from app.core.relay import verify_remote_scope
        self._stage("inspect_control", "正在核对首次基线的连接、保护锁与发布状态…")
        self._validate()
        store, coordinator = self._context()
        pending = PendingOperation(store).load()
        if pending is not None and (pending["operation"], pending["lock_token"]) != ("initialize_baseline", owned_token):
            raise RuntimeError("本机有未完成的操作，请先核对续作。")
        if not owned_token and coordinator.locked():
            raise RuntimeError("数据集锁已占用；不会初始化共同基线或接管旧锁。")
        metadata = coordinator.read()
        if metadata is None:
            verify_remote_scope(self.rclone, self.task, registering=True, cancelled=self.cancelled)
        elif metadata["phase"] != "ready":
            raise RuntimeError("手机数据集有未完成的发布，不能初始化共同基线。")
        # Adoption operates on existing trees, so an unmarked tree stays v1.
        protocol = metadata["version"] if metadata else LEGACY
        baseline = store.load(protocol=protocol)
        if baseline is None and store.path.exists():
            raise RuntimeError("现有共同基线属于其他连接或范围；请先保留并人工核对，不能覆盖初始化。")
        conflict = self._verify_namespace(protocol, allow_missing=False, recorded=(baseline[0],) if baseline else ())
        if conflict:
            raise RuntimeError(f"两端名称不一致，不能初始化共同基线：{conflict}")
        local = self._snapshot("inspect_local", "正在校验首次基线的本地文件内容…",
                               self.task.local_path, self.task.excludes)
        remote = self._snapshot("inspect_remote", "正在校验首次基线的手机内容；无服务器哈希时需读取文件…",
                                f"{C.DEFAULT_REMOTE}:{self.task.remote_path}", self.task.excludes)
        if not local.same_content(remote):
            raise RuntimeError("两端实际路径、大小和 SHA256 内容必须完全一致，不能初始化共同基线。")
        if metadata is not None and metadata.get("content_digest") != remote.digest:
            raise RuntimeError("手机内容与已发布批次不一致，不能初始化共同基线。")
        if baseline is not None and (metadata is None or baseline[1] != metadata["generation"]
                                     or baseline[0].digest != remote.digest):
            raise RuntimeError("现有共同基线与当前手机内容或批次冲突，不能重新初始化。")
        preview = self._baseline_initialization_preview(store, local, remote, metadata, baseline, protocol)
        return preview, store, coordinator, local, remote, metadata, baseline, protocol

    def preview_baseline_initialization(self) -> BaselineInitializationPreview:
        """Read-only approval for adopting two existing, identical file trees."""
        return self._baseline_initialization_state()[0]

    def initialize_baseline(
        self, approved: BaselineInitializationPreview,
    ) -> BaselineInitializationResult:
        """Initialize control state and a local baseline, never transfer payloads."""
        from app.core.pending import PendingOperation
        from app.core.relay import verify_remote_scope

        if type(approved) is not BaselineInitializationPreview:
            raise ValueError("必须明确确认首次基线预览")
        fresh, store, coordinator, _, _, _, _, _ = self._baseline_initialization_state()
        if fresh != approved:
            raise RuntimeError("首次基线的内容、连接或范围已变化，请重新预览后确认。")
        journal = PendingOperation(store)
        with coordinator.control_lease(journal, "initialize_baseline", registering=not fresh.generation) as lease:
            fresh, store, coordinator, _, remote, metadata, baseline, protocol = (
                self._baseline_initialization_state(owned_token=lease.token)
            )
            if fresh != approved:
                raise RuntimeError("首次基线的内容、连接或范围已变化，请重新预览后确认。")
            created_metadata = metadata is None
            generation = metadata["generation"] if metadata else uuid.uuid4().hex
            expected = metadata if metadata is not None else {
                "version": protocol, **coordinator.scope, "phase": "ready",
                "generation": generation, "content_digest": remote.digest,
                "writer_identity": self.task.sync_identity,
            }
            started = False
            try:
                if created_metadata:
                    with coordinator.registration_lease(lease):
                        verify_remote_scope(self.rclone, self.task, registering=True, cancelled=self.cancelled)
                        started = True
                        coordinator.write("ready", generation, content_digest=remote.digest, protocol=protocol)
                if coordinator.read() != expected:
                    raise RuntimeError("首次基线的数据集状态回读核对失败；没有保存本地基线。")
                # Read both full trees again after the ready response: metadata
                # success alone cannot establish a trustworthy content baseline.
                verified, final_store, _, _, final_remote, final_metadata, final_baseline, final_protocol = (
                    self._baseline_initialization_state(owned_token=lease.token)
                )
                if (verified.binding_digest != approved.binding_digest
                        or final_store.path != store.path
                        or verified.local_digest != approved.local_digest
                        or verified.remote_digest != approved.remote_digest
                        or final_metadata != expected or final_baseline != baseline or final_protocol != protocol):
                    raise RuntimeError("首次基线的内容或状态已变化；没有保存本地基线。")
                if self.cancelled.is_set():
                    raise RuntimeError("操作已取消")
            except BaseException:
                # A write may have reached the endpoint despite a failed reply.
                if started:
                    lease.retain()
                raise
        # Save only after successful owner-checked release. A cleanup failure
        # must not leave a newly persisted baseline behind a reported failure.
        # A later publication has a different generation/content and normal sync
        # will reject this last-verified baseline rather than overwrite it.
        if baseline is None:
            store.save(final_remote, generation, protocol=protocol)
        return BaselineInitializationResult(generation, len(final_remote.files), created_metadata)

    def _inspect(self, *, check_lock: bool = True, recovery_token: str | None = None):
        from app.core.relay import ContentSnapshot, verify_remote_scope
        self._stage("inspect_control", "正在核对任务路径、保护锁与发布状态…")
        self._validate()
        store, coordinator = self._context()
        from app.core.pending import PendingOperation
        pending = PendingOperation(store).load()
        if self.recovering:
            if pending is None:
                raise RuntimeError("没有可继续的未完成操作。")
            self.operation = pending["operation"]
        elif pending is not None:
            raise RuntimeError("本机有未完成的操作；请在任务更多菜单中选择‘继续未完成操作’，不会自动覆盖部分结果。")
        if recovery_token is not None and (pending is None or pending["lock_token"] != recovery_token):
            raise RuntimeError("未完成记录已变化，请重新查看差异后确认。")
        if check_lock and not self.recovering and coordinator.locked():
            raise RuntimeError("此数据集正由另一台电脑操作，或上轮异常退出留下了保护锁；不会自动抢占。")
        if check_lock and self.recovering and not self.receiving and not coordinator.locked():
            raise RuntimeError("上次发布的所有权锁已缺失，不能证明仍由原操作独占；请先保留两端文件并核对。")
        metadata = coordinator.read()
        if metadata is None and not self.receiving and not self.recovering:
            verify_remote_scope(self.rclone, self.task, registering=True, cancelled=self.cancelled)
        protocol, baseline, conflict = self._verify_protocol(store, metadata, pending)
        allow_missing = not self.receiving and self.operation == "publish"
        local = self._snapshot("inspect_local", "正在校验本地文件内容…",
                               self.task.local_path, self.task.excludes)
        remote = self._snapshot(
            "inspect_remote", "正在校验手机文件内容；WebDAV 无哈希时需要读取内容…",
            f"{C.DEFAULT_REMOTE}:{self.task.remote_path}", self.task.excludes,
            allow_missing=allow_missing,
        )
        missing = remote is None
        if remote is None:
            remote = ContentSnapshot({})
        if protocol == LEGACY:
            for snapshot in (local, remote):
                require_legacy_paths(snapshot.directories, location="当前目录")
                require_legacy_paths(snapshot.files, location="当前文件")
        generation = metadata["generation"] if metadata else ""
        source, destination = (remote, local) if self.receiving else (local, remote)
        marks = {}
        for path in sorted(set(source.files) | set(destination.files)):
            if path not in destination.files:
                marks[path] = "+"
            elif path not in source.files:
                marks[path] = "-"
            else:
                marks[path] = "=" if source.files[path] == destination.files[path] else "*"
        reason, blocked = "", False
        if metadata and metadata["phase"] != "ready":
            reason, blocked = "手机数据集有未完成的发布；先在原电脑核对并完成恢复，不能把部分内容当成完整镜像。", True
        elif metadata and metadata.get("content_digest") != remote.digest:
            reason, blocked = "手机内容与已发布批次不一致，可能被其他工具改写；请先保留文件并核对。", True
        elif self.operation in ("receive", "adopt"):
            if missing or metadata is None:
                reason, blocked = "请先由公司端建立并完成一次受保护的发布，再选择这个已有数据集接收。", True
            elif not baseline and local.files:
                reason, blocked = "本地已有文件，但没有共同内容基线。请保留现有目录，选择新的空接收目录建立基线。", True
            elif baseline and not local.same_content(baseline[0]):
                reason, blocked = "本地有未交回的新增、修改或删除，已停止拉取以避免覆盖；请先查看差异并交回或另存处理。", True
            elif not baseline:
                reason = "首次接收将把手机镜像下载到此本地目录，请核对来源、目标和本地历史位置。"
        else:
            if baseline and (generation != baseline[1] or not remote.same_content(baseline[0])):
                reason, blocked = "手机有其他电脑发布的新修改，已阻止旧副本覆盖；请先接收手机更新。", True
            elif not baseline and self.operation == "return":
                reason, blocked = "没有有效的共同内容基线，不能交回；请先在空目录完成首次接收。", True
            elif not baseline:
                reason = "首次发布或内容基线已失效；请核对差异，明确接管这个手机目录。"
        try:
            from app.core.history import validate_history_inventory
            # Refuse file/directory and Windows spelling transitions as a single
            # batch rather than failing after partially copying other files.
            validate_history_inventory(
                set(source.files) | set(destination.files),
                set(source.directories) | set(destination.directories),
            )
        except ValueError:
            reason, blocked = "两端存在文件/目录换型或大小写、Unicode 路径冲突；请先另存并处理这些路径，再重新核对。", True
        if source.files == {} and destination.files and self.receiving:
            reason, blocked = "手机来源异常为空；已保留本地文件，不自动清空接收目录。", True
        if self.operation in ("return", "adopt") and not reason:
            reason = "这是明确的接力操作，请核对全部新增、修改和删除后确认。"
        if self.recovering:
            original_source = pending["remote"] if self.receiving else pending["local"]
            original_destination = pending["local"] if self.receiving else pending["remote"]
            reason = "将继续上次已确认的操作；只接受原来源内容及原目标/已传输结果的组合，不自动抢占其他电脑的锁。"
            blocked = source.digest != original_source.digest
            if blocked:
                reason = "上次操作后来源又被修改，不能直接继续；请先保留两端文件并核对。"
            if self.receiving and (
                metadata is None or metadata["phase"] != "ready"
                or metadata.get("content_digest") != remote.digest
                or metadata["generation"] != pending["generation"]
            ):
                reason, blocked = "手机发布状态或内容已变化，不能把未完成或未经核对的数据作为续作来源。", True
            if not self.receiving:
                allowed_directories = set(original_source.directories) | set(original_destination.directories)
                own_publication = metadata is not None and (
                    metadata.get("writer_identity") == self.task.sync_identity
                    and metadata["generation"] == pending["generation"]
                    and (
                        metadata["phase"] != "ready" and metadata.get("content_digest") == original_source.digest
                        or metadata["phase"] == "ready" and metadata.get("content_digest") == remote.digest
                        and remote.same_content(original_source)
                        and set(original_source.directories) <= set(remote.directories) <= allowed_directories
                    )
                )
                before_publication = "prior_metadata" in pending and (
                    metadata == pending["prior_metadata"]
                    and remote.digest == original_destination.digest
                )
                if not own_publication and not before_publication:
                    reason, blocked = "手机发布状态不再属于原操作，不能继续或抢占其他电脑的工作。", True
            try:
                validate_history_inventory(set(source.files) | set(destination.files), set(source.directories) | set(destination.directories))
            except ValueError:
                reason, blocked = "续作路径存在文件/目录或命名冲突，请先另存核对。", True
            for path, content in destination.files.items():
                if content not in (original_source.files.get(path), original_destination.files.get(path)):
                    reason, blocked = "未完成目标包含新的其他修改；已停止续作以避免覆盖。", True
                    break
            old_files_no_longer_at_target = {
                path: item for path, item in original_destination.files.items()
                if destination.files.get(path) != item
            }
            backup, history_conflict = self._verify_recovery_history(pending, protocol)
            if history_conflict:
                reason, blocked = history_conflict, True
            elif any(original_source.files.get(path) == item or backup.files.get(path) != item
                     for path, item in old_files_no_longer_at_target.items()):
                reason, blocked = "未完成目标的部分旧文件已改变或缺失，且原历史批次不能证明旧内容已保留；请先核对再处理。", True
        else:
            backup = None
        if conflict:
            reason, blocked = f"两端未过滤的路径存在名称或文件/目录冲突；已停止操作。{conflict}", True
        token = hashlib.sha256(json.dumps([
            store.binding, self.operation, protocol,
            local.digest, remote.digest, metadata, missing,
            baseline[0].digest if baseline else None, baseline[1] if baseline else None,
            [pending["lock_token"], pending["stamp"], pending["generation"], pending["local"].digest,
             pending["remote"].digest, pending.get("prior_metadata"), backup.digest] if pending else None,
        ], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        report = SyncReport(
            added=sum(mark == "+" for mark in marks.values()),
            updated=sum(mark == "*" for mark in marks.values()),
            deleted=sum(mark == "-" for mark in marks.values()),
            unchanged=sum(mark == "=" for mark in marks.values()),
            changes=tuple((mark, path) for path, mark in marks.items() if mark != "=")[:500],
            digest=token, operation=self.operation, blocked=blocked, reason=reason,
            requires_confirmation=bool(reason) and not blocked,
        )
        self._plan = _SyncPlan(local=local, remote=remote, metadata=metadata, source=source,
                               destination=destination, marks=marks, protocol=protocol, backup=backup)
        self._stage("difference", report.summary)
        return report

    def _plan_lock_release(self, journal, coordinator, pending) -> _LockReleasePlan | None:
        from app.core.pending import CONTROL_OPERATIONS

        operation = pending["operation"]
        control_only = operation in CONTROL_OPERATIONS
        if not control_only and (operation not in ("publish", "return") or pending.get("prior_metadata") is not None):
            return None
        token = pending["lock_token"]
        dataset, registration = coordinator.owned_locks(token, registration=pending.get("registration", not control_only))
        if registration and not dataset:
            raise RuntimeError("目录登记锁仍在，但无法确认对应的数据集锁；请保留记录。")
        if control_only:
            reason = f"{CONTROL_OPERATIONS[operation]}：核对并释放本机留下的保护锁，只处理控制记录。"
        elif registration:
            reason = "只释放本机的目录登记锁，让其他任务可继续登记。保留此任务的数据集锁和文件续作记录。"
        else:
            return None
        reason += "请确认原操作已停止；不会重传文件或清理历史。"
        digest = _canonical_digest([journal.binding, "release", operation, token, pending.get("generation"),
                                    pending.get("registration"), dataset, registration])
        report = SyncReport(operation="release", digest=digest, reason=reason, requires_confirmation=True)
        return _LockReleasePlan(report, token, dataset, registration, control_only)

    def preview(self) -> SyncReport:
        if not self.recovering:
            return self._inspect()
        from app.core.pending import CONTROL_OPERATIONS, PendingOperation
        store, coordinator = self._context()
        journal = PendingOperation(store)
        pending = journal.load()
        if pending is None:
            raise RuntimeError("没有可继续的未完成操作。")
        if pending["operation"] in CONTROL_OPERATIONS:
            release = self._plan_lock_release(journal, coordinator, pending)
            assert release is not None
            return release.report
        try:
            report = self._inspect()
        except (OSError, ValueError, RuntimeError):
            self._check_cancel()
            if release := self._plan_lock_release(journal, coordinator, pending):
                return release.report
            raise
        if report.blocked and (release := self._plan_lock_release(journal, coordinator, pending)):
            return release.report
        return report

    def _execute_release(self, report: SyncReport) -> str:
        from app.core.pending import PendingOperation

        if not self.recovering or report.blocked:
            raise RuntimeError("请先在原任务中明确确认保护锁恢复。")
        store, coordinator = self._context()
        journal = PendingOperation(store)
        pending = journal.load()
        plan = self._plan_lock_release(journal, coordinator, pending) if pending is not None else None
        if plan is None or plan.report != report:
            raise RuntimeError("保护锁或本机记录已变化，请重新核对后确认。")
        self.begin_commit()
        self.rclone.config.save_recovered_connection()
        self._stage("releasing", "正在核对并处理本机的保护锁…")
        if plan.dataset:
            with coordinator.recorded_lease(coordinator.recovery_lease(plan.token), journal) as lease:
                locked_plan = self._plan_lock_release(journal, coordinator, pending)
                if locked_plan != plan:
                    raise RuntimeError("保护锁已变化，请重新核对后确认。")
                if plan.registration:
                    coordinator.release_interrupted_registration(lease, required=True)
                if not plan.control_only:
                    lease.retain()
        else:
            journal.clear_control(plan.token)
        return ""

    def execute(self, report: SyncReport) -> str:
        from app.core.history import _no_links
        from app.core.relay import ContentSnapshot, DatasetLeaseRefused, verify_remote_scope

        if report.operation == "release":
            return self._execute_release(report)

        if report.blocked:
            raise RuntimeError(report.reason or "内容保护已阻止执行")
        if not self.recovering and report.operation != self.operation:
            raise RuntimeError("操作方向与已确认差异不一致")
        # In particular do not acquire a recovery lease on an incompatible v1
        # journal. Repeat the same proof under the lock before writing payloads.
        store, coordinator = self._context()
        from app.core.pending import CONTROL_OPERATIONS, PendingOperation
        journal = PendingOperation(store)
        interrupted = journal.load()
        if self.recovering:
            if interrupted is None:
                raise RuntimeError("没有可继续的未完成操作。")
            if interrupted["operation"] in CONTROL_OPERATIONS:
                raise RuntimeError("文件操作已经结束，请重新核对保护锁收尾。")
            self.operation = interrupted["operation"]
            if self.operation != report.operation:
                raise RuntimeError("操作方向与已确认差异不一致")
        elif interrupted is not None:
            raise RuntimeError("本机有未完成的操作；请先核对续作。")
        self._validate()
        has_recovery_lock = interrupted is not None and coordinator.locked()
        if interrupted is not None and not self.receiving and not has_recovery_lock:
            raise RuntimeError("上次发布的所有权锁已缺失；请保留两端文件和未完成记录。")
        metadata = coordinator.read()
        protocol, _, conflict = self._verify_protocol(store, metadata, interrupted)
        if conflict:
            raise RuntimeError(f"目标名称冲突，未写入文件：{conflict}")
        if interrupted is not None:
            _, history_conflict = self._verify_recovery_history(interrupted, protocol)
            if history_conflict:
                raise RuntimeError(history_conflict)
        recovery_token = interrupted["lock_token"] if interrupted else None
        context = (coordinator.recovery_lease(recovery_token) if has_recovery_lock
                   else coordinator.lease(token=recovery_token))
        self._stage("locking", "正在获取数据集保护锁…")
        with coordinator.recorded_lease(context, journal) as lease:
            fresh = self._inspect(check_lock=False, recovery_token=recovery_token)
            if fresh.blocked:
                raise RuntimeError(fresh.reason)
            if fresh.digest != report.digest:
                raise RuntimeError("文件内容或发布批次已变化，请重新查看差异后确认。")
            if interrupted is not None:
                self.rclone.config.save_recovered_connection()
            plan = self._plan
            assert plan is not None
            metadata, protocol = plan.metadata, plan.protocol
            if interrupted is not None and not self.receiving and interrupted.get("prior_metadata") is None:
                # A payload approval must still match before its recovery can
                # release the registration lock. A release-only plan is separate.
                coordinator.release_interrupted_registration(lease)
                if metadata is None:
                    verify_remote_scope(self.rclone, self.task, registering=True, cancelled=self.cancelled)
            source, destination, marks = plan.source, plan.destination, plan.marks
            if (metadata and metadata["phase"] == "ready" and plan.local.same_content(plan.remote)
                    and set(source.directories).issubset(destination.directories)):
                self._stage("saving", "文件内容没有变化；正在保存已核对的共同基线…")
                self.begin_commit()
                if interrupted is None:
                    journal.save_control("finalize", lease.token)
                store.save(plan.remote, metadata["generation"], protocol=protocol)
                if interrupted is not None:
                    journal.save_control("finalize", lease.token)
                self._stage("releasing", "共同基线已核对；正在释放任务锁…")
                return ""
            self._stage("preparing_history", "正在核对历史批次位置…")
            stamp = interrupted["stamp"] if interrupted else self._history_stamp()
            generation = interrupted["generation"] if interrupted else (
                uuid.uuid4().hex if not self.receiving else metadata["generation"]
            )
            source_root = f"{C.DEFAULT_REMOTE}:{self.task.remote_path}" if self.receiving else self.task.local_path
            destination_root = self.task.local_path if self.receiving else f"{C.DEFAULT_REMOTE}:{self.task.remote_path}"
            history_root = self._history_root(stamp)
            payload_started = False
            try:
                with staging_directory() as temporary:
                    staged = Path(temporary) / "files"
                    staged.mkdir()
                    changed = [path for path, mark in marks.items() if mark in ("+", "*")]
                    if changed:
                        selection = Path(temporary) / "selected.txt"
                        selection.write_text("\n".join(changed) + "\n", encoding="utf-8")
                        self._stage("staging", "正在下载到暂存区；尚未写入目标…" if self.receiving else
                                    "正在准备上传暂存文件；尚未写入目标…")
                        self._run_transfer([
                            "copy", source_root, str(staged), "--files-from-raw", str(selection),
                            "--ignore-times", "--no-traverse",
                        ])
                        checked = self._snapshot("check_staged", "正在校验暂存文件；尚未写入目标…", str(staged))
                        if dict(checked.files) != {path: source.files[path] for path in changed}:
                            raise RuntimeError("准备期间来源文件发生变化；目标尚未覆盖，请重新查看差异。")
                    # Revalidate the full destination and source after staging, not just names/counts.
                    current = self._snapshot("recheck_destination", "正在复核目标内容；尚未写入目标…",
                                             destination_root, self.task.excludes, allow_missing=not self.receiving)
                    current = current or ContentSnapshot({})
                    origin = self._snapshot("recheck_source", "正在复核来源内容；手机无哈希时需读取文件，尚未写入目标…",
                                            source_root, self.task.excludes)
                    if current.digest != destination.digest or origin.digest != source.digest:
                        raise RuntimeError("传输前文件内容发生变化；目标尚未覆盖，请重新查看差异。")
                    conflict = self._verify_namespace(protocol, allow_missing=not self.receiving)
                    if conflict:
                        raise RuntimeError(f"目标名称冲突，未写入文件：{conflict}")
                    if interrupted is not None:
                        recent, history_conflict = self._verify_recovery_history(interrupted, protocol)
                        assert plan.backup is not None
                        if history_conflict or recent.digest != plan.backup.digest:
                            raise RuntimeError(history_conflict or "续作的历史批次已改变，未写入目标，请重新核对。")
                    elif self._namespace(history_root, allow_missing=True) is not None:
                        raise RuntimeError("本次历史批次位置已被占用，未写入目标，请重新核对。")
                    self._check_cancel()
                    registering = not self.receiving and metadata is None
                    registration = coordinator.registration_lease(lease) if registering else nullcontext()
                    # Persist the token before acquiring the shared lock: a
                    # process exit after registration must leave usable proof.
                    if interrupted is None:
                        journal.save(
                            operation=self.operation, local=plan.local, remote=plan.remote,
                            generation=generation, stamp=stamp, lock_token=lease.token,
                            prior_metadata=metadata, protocol=protocol,
                        )
                    preflight_error = None
                    try:
                        with registration:
                            try:
                                if registering:
                                    verify_remote_scope(self.rclone, self.task, registering=True, cancelled=self.cancelled)
                                    if protocol == LITERAL and self._new_protocol(store) != LITERAL:
                                        raise RuntimeError("首次发布的镜像、历史或本机状态已变化；未写入文件，请重新查看差异。")
                                self._check_cancel()
                            except Exception as exc:
                                if interrupted is not None:
                                    raise
                                # Only discard the new intent after confirming
                                # the registration lock was released cleanly.
                                preflight_error = exc
                            if preflight_error is None:
                                payload_started = True
                                if not self.receiving:
                                    coordinator.write("publishing", generation, content_digest=source.digest, protocol=protocol)
                    except DatasetLeaseRefused:
                        if interrupted is None and not payload_started:
                            journal.save_control("rejected", lease.token)
                        raise
                    except BaseException:
                        if registering and interrupted is None:
                            # An acquisition or release may have an unknown
                            # outcome; preserve the dataset lock and its proof.
                            lease.retain()
                        raise
                    if preflight_error is not None:
                        journal.save_control("rejected", lease.token)
                        raise preflight_error
                    self._stage("applying", "正在写入本地接收目录…" if self.receiving else "正在上传写入手机镜像…")
                    if not self.receiving:
                        self._require(self.rclone.mkdir(self.task.remote_path), "创建镜像目录失败")
                    else:
                        _no_links(Path(self.task.local_history_path))
                    if changed:
                        self._run_transfer([
                            "copy", str(staged), destination_root, "--ignore-times",
                            "--backup-dir", history_root, "--create-empty-src-dirs",
                        ])
                    removed = [path for path, mark in marks.items() if mark == "-"]
                    self._stage("archive", "正在保留目标端待删除的旧文件…", items_done=0, items_total=len(removed))
                    for index, path in enumerate(removed, 1):
                        if self.cancelled.is_set():
                            raise RuntimeError("操作已取消")
                        self._require(self.rclone.run([
                            "moveto", f"{destination_root.rstrip('/')}/{path}",
                            f"{history_root.rstrip('/')}/{path}",
                            "--retries", "1", "--low-level-retries", "1", "--timeout", "60s",
                        ], timeout=600, cancel_event=self.cancelled), "保留待删除文件失败")
                        self._stage("archive", "正在保留目标端待删除的旧文件…", items_done=index, items_total=len(removed))
                    self._prepare_directories(source, current, destination_root)
                    verified = self._snapshot("verify_destination", "正在最终校验目标文件内容…",
                                              destination_root, self.task.excludes)
                    if not verified.same_content(source) or not set(source.directories).issubset(verified.directories):
                        raise RuntimeError("传输后内容核对未通过；没有记录为成功，也未清理历史文件。")
                    origin_after = self._snapshot("verify_source", "正在最终复核来源内容；手机无哈希时需读取文件…",
                                                 source_root, self.task.excludes)
                    if origin_after.digest != source.digest:
                        raise RuntimeError("传输期间来源发生修改；已保留历史，但没有推进共同基线。")
                    self._stage("saving", "正在保存已验证结果与共同基线…")
                    self.begin_commit()
                    if not self.receiving:
                        coordinator.write("ready", generation, content_digest=verified.digest, protocol=protocol)
                    store.save(verified, generation, protocol=protocol)
                    journal.save_control("finalize", lease.token)
            except Exception:
                if payload_started and not self.receiving:
                    lease.retain()
                raise
            self._stage("cleanup", "正在检查历史保留并收尾…")
            # A v2 shared state proves the phone namespace, not the origin of
            # existing offline history on this computer. Local batches remain
            # available for explicit, verified deletion but are not auto-expired.
            if protocol == LITERAL and not self.receiving:
                self._clean_history(coordinator, lease, journal)
            self._stage("releasing", "正在释放任务锁；尚未报告任务完成…")
            return stamp

    def _prepare_directories(self, source, destination, destination_root: str) -> None:
        from app.core.history import _no_links
        known = set(destination.directories)
        # Successful file copies already create their parent directories.
        for path in source.files:
            parent = path.rpartition("/")[0]
            while parent:
                known.add(parent)
                parent = parent.rpartition("/")[0]
        directories = sorted(source.directories, key=lambda value: (-value.count("/"), value))
        self._stage("directories", "正在核对并补齐目标目录…", items_done=0, items_total=len(directories))
        for index, directory in enumerate(directories, 1):
            if self.cancelled.is_set():
                raise RuntimeError("操作已取消")
            target = f"{destination_root.rstrip('/')}/{directory}"
            if self.receiving:
                _no_links(Path(target))
            if directory not in known:
                self._require(self.rclone.run(["mkdir", target], timeout=60,
                                             cancel_event=self.cancelled), "创建目标目录失败")
                parent = directory
                while parent:
                    known.add(parent)
                    parent = parent.rpartition("/")[0]
            if index == len(directories) or index % 25 == 0:
                self._stage("directories", "正在核对并补齐目标目录…", items_done=index, items_total=len(directories))

    def _history_stamp(self) -> str:
        from app.core.history import HistoryService, LocalHistoryService
        history = LocalHistoryService(self.task) if self.receiving else HistoryService(self.rclone, self.task)
        existing = set(history.list_versions()) if self.receiving else set()
        if not self.receiving:
            result = self.rclone.lsf_dirs(self.task.versions_path)
            if result.ok:
                existing = set(line.rstrip("/") for line in result.out.splitlines())
            elif result.code not in (3, 4):
                raise RuntimeError("无法核对历史批次，已停止执行")
        stamp = version_timestamp()
        if stamp in existing:
            from datetime import timedelta
            moment = datetime.strptime(stamp, "%Y-%m-%d_%H%M%S")
            while stamp in existing:
                moment += timedelta(seconds=1)
                stamp = moment.strftime("%Y-%m-%d_%H%M%S")
        return stamp

    @staticmethod
    def _require(result, message: str) -> None:
        if not result.ok:
            raise RuntimeError(f"{message}：{result.err or result.out or result.code}")

    def _run_transfer(self, args: list[str]) -> None:
        args = [*args, "--transfers", "1", "--checkers", "8", "--stats", "2s",
                "--use-json-log", "--log-level", "INFO", "--retries", "1",
                "--timeout", "60s", "--contimeout", "15s", "--low-level-retries", "2"]
        proc = self.rclone.popen(args)
        log_handle = None
        succeeded = False
        try:
            if self.log_directory is not None:
                self.log_directory.mkdir(parents=True, exist_ok=True)
                log_handle = (self.log_directory / f"sync-{datetime.now():%Y%m%d}.log").open("a", encoding="utf-8")
            if proc.stdout is None:
                raise RuntimeError("无法读取传输输出")
            for line in proc.stdout:
                if log_handle:
                    log_handle.write(f"[{self.task.id}] {line}")
                if self.cancelled.is_set():
                    self.rclone.cancel()
                try:
                    entry = json.loads(line)
                    stats = entry.get("stats")
                    if isinstance(stats, dict):
                        done, total = stats.get("bytes", 0), stats.get("totalBytes", 0)
                        if type(done) is int and done >= 0 and type(total) is int and total > 0:
                            self._emit_progress(replace(self._current_progress, bytes_done=done, bytes_total=total))
                except (ValueError, TypeError, AttributeError):
                    pass
            code = proc.wait()
            if self.cancelled.is_set():
                raise RuntimeError("操作已取消；已完成的传输不回滚，未清理历史文件。")
            if code:
                raise RuntimeError(f"传输退出码 {code}；请查看运行日志。未清理历史文件。")
            succeeded = True
        finally:
            if succeeded:
                self._stage("transfer_finished", "本阶段复制已结束；正在进入后续核验，任务尚未完成…")
            else:
                self._stage("transfer_stopped", "本阶段复制已停止；正在回收进程并处理保护锁…")
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            if proc.stdout:
                proc.stdout.close()
            self.rclone.release(proc)
            if log_handle:
                log_handle.close()

    def _clean_history(self, coordinator, lease, journal) -> None:
        from app.core.history import HistoryService
        from app.core.relay import verify_remote_scope

        history = HistoryService(self.rclone, self.task, protocol=LITERAL)
        failure = None
        try:
            expired = old_versions(history.list_versions(), self.task.retention_days)
            if not expired:
                return
            journal.save_control("finalize", lease.token, registration=True)
            with coordinator.registration_lease(lease):
                try:
                    verify_remote_scope(self.rclone, self.task, require_ready=True, cancelled=self.cancelled)
                    for name in expired:
                        self._validate()
                        history.delete(name)
                except Exception as exc:
                    failure = exc
        except Exception as exc:
            if lease.retained:
                raise RuntimeError(f"文件内容已核验；登记锁释放待确认，请续作核对：{exc}") from exc
            failure = exc
        if failure is not None:
            self._stage("cleanup", f"文件内容已核验；历史清理未完成：{failure}")


class SyncWorker(QThread):
    progress = Signal(str, str)
    progress_event = Signal(str, object)
    finished_ok = Signal(str, object, str)
    preview_ready = Signal(str, object, str)
    guarded = Signal(str, object, str)
    failed = Signal(str, str)
    cancelled_result = Signal(str, str)

    def __init__(
        self, config: Config, task: MirrorTask, parent=None, *,
        preview_only: bool = False, approved_digest: str = "", operation: str = "",
    ):
        super().__init__(parent)
        self.config = config.snapshot()
        self.task = task
        self.preview_only = preview_only
        self.approved_digest = approved_digest
        self.operation = operation or ("receive" if task.direction == "download" else "publish")
        self.cancelled = threading.Event()
        self.rclone: Rclone | None = None
        self.outcome = "pending"
        self._outcome_lock = threading.Lock()
        self._committing = False

    def stop(self) -> bool:
        with self._outcome_lock:
            if self.outcome != "pending" or self._committing or self.cancelled.is_set():
                return False
            self.cancelled.set()
        self.requestInterruption()
        if self.rclone is not None:
            self.rclone.cancel()
        return True

    def _begin_commit(self) -> None:
        with self._outcome_lock:
            if self.cancelled.is_set():
                raise RuntimeError("操作已取消")
            self._committing = True

    def _finish(self, outcome: str) -> str:
        with self._outcome_lock:
            self.outcome = "cancelled" if self.cancelled.is_set() else outcome
            return self.outcome

    def _report_cancelled(self) -> None:
        self.cancelled_result.emit(self.task.id, "本次操作已取消；已传输内容不回滚，未清理历史文件。")

    def run(self) -> None:
        try:
            if self.config.load_error:
                raise ValueError(self.config.load_error)
            validate_tasks(self.config.tasks, self.config.get("mount.drive", C.DEFAULT_DRIVE))
            self.rclone = Rclone(self.config)
            with self.rclone.isolated_conf():
                self.rclone.ensure_conf()
                engine = SyncEngine(
                    self.rclone, self.task,
                    mount_drive=self.config.get("mount.drive", C.DEFAULT_DRIVE),
                    log_directory=data_dir() / "logs", cancelled=self.cancelled,
                    state_directory=data_dir() / "baselines", operation=self.operation,
                    progress=lambda text: self.progress.emit(self.task.id, text),
                    progress_event=lambda event: self.progress_event.emit(self.task.id, event),
                    begin_commit=self._begin_commit,
                )
                report = engine.preview()
                reason = preview_guard(self.task, report)
                if self.preview_only:
                    if self._finish("preview") == "cancelled":
                        self._report_cancelled()
                    else:
                        self.preview_ready.emit(self.task.id, report, reason)
                    return
                if not reason and self.approved_digest and self.approved_digest != report.digest:
                    reason = "差异与已确认的预演不同，请重新确认。"
                if report.blocked or (reason and self.approved_digest != report.digest):
                    if self._finish("guarded") == "cancelled":
                        self._report_cancelled()
                    else:
                        self.guarded.emit(self.task.id, report, reason)
                    return
                stamp = engine.execute(report)
                if self._finish("success") == "cancelled":
                    self._report_cancelled()
                else:
                    self.finished_ok.emit(self.task.id, report, stamp)
        except Exception as exc:  # noqa: BLE001
            if self._finish("failed") == "cancelled":
                self._report_cancelled()
            else:
                get_logger().warning("任务 %s: %s", self.task.id, exc)
                self.failed.emit(self.task.id, str(exc))
