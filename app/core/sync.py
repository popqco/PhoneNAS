from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app import constants as C
from app.core.config import Config, data_dir
from app.core.log import get_logger
from app.core.progress import SyncProgress
from app.core.rclone import Rclone
from app.core.retention import old_versions, version_timestamp
from app.core.tasks import MirrorTask, validate_source, validate_tasks


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
    args = [
        "sync", source, destination,
        "--backup-dir", history,
        "--transfers", "4", "--checkers", "8", "--create-empty-src-dirs",
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
        progress_event=None,
    ):
        self.rclone = rclone
        self.task = task
        self.mount_drive = mount_drive
        self.log_directory = log_directory
        self.progress = progress or (lambda _text: None)
        self.progress_event = progress_event or (lambda _event: None)
        self._current_progress = SyncProgress("preparing", "正在准备任务…")
        self.cancelled = cancelled or threading.Event()
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
        self._plan = None

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

    def _validate(self) -> None:
        from app.core.history import _no_links
        if self.cancelled.is_set():
            raise RuntimeError("操作已取消")
        validate_tasks((self.task,), self.mount_drive)
        validate_source(self.task, self.mount_drive)
        _no_links(Path(self.task.local_path))
        _no_links(Path(self.task.local_history_path))

    def _context(self):
        from app.core.relay import BaselineStore, DatasetCoordinator
        directory = self.state_directory or data_dir() / "baselines"
        return (
            BaselineStore(directory, self.task, self.rclone.config),
            DatasetCoordinator(self.rclone, self.task, lease_factory=self.lease_factory),
        )

    @staticmethod
    def _baseline_initialization_preview(store, local, remote, metadata, baseline):
        binding_digest = _canonical_digest(store.binding)
        generation = metadata["generation"] if metadata else ""
        return BaselineInitializationPreview(
            digest=_canonical_digest({
                "operation": "initialize_baseline", "binding_digest": binding_digest,
                "baseline_path": str(store.path), "local_digest": local.digest,
                "remote_digest": remote.digest, "metadata": metadata,
                "baseline": [baseline[0].digest, baseline[1]] if baseline else None,
            }),
            file_count=len(local.files), local_digest=local.digest,
            remote_digest=remote.digest, binding_digest=binding_digest,
            generation=generation,
        )

    def _baseline_initialization_state(self, *, check_lock: bool = True):
        from app.core.pending import PendingOperation
        self._stage("inspect_control", "正在核对首次基线的连接、保护锁与发布状态…")
        self._validate()
        store, coordinator = self._context()
        if PendingOperation(store).load() is not None:
            raise RuntimeError("本机有未完成的操作，不能初始化共同基线。")
        baseline = store.load()
        if baseline is None and store.path.exists():
            raise RuntimeError("现有共同基线属于其他连接或范围；请先保留并人工核对，不能覆盖初始化。")
        if check_lock and coordinator.locked():
            raise RuntimeError("数据集锁已占用；不会初始化共同基线或接管旧锁。")
        metadata = coordinator.read()
        if metadata is not None and metadata["phase"] != "ready":
            raise RuntimeError("手机数据集有未完成的发布，不能初始化共同基线。")
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
        preview = self._baseline_initialization_preview(store, local, remote, metadata, baseline)
        return preview, store, coordinator, local, remote, metadata, baseline

    def preview_baseline_initialization(self) -> BaselineInitializationPreview:
        """Read-only approval for adopting two existing, identical file trees."""
        return self._baseline_initialization_state()[0]

    def initialize_baseline(
        self, approved: BaselineInitializationPreview,
    ) -> BaselineInitializationResult:
        """Initialize control state and a local baseline, never transfer payloads."""
        if type(approved) is not BaselineInitializationPreview:
            raise ValueError("必须明确确认首次基线预览")
        fresh, store, coordinator, local, remote, metadata, baseline = self._baseline_initialization_state()
        if fresh != approved:
            raise RuntimeError("首次基线的内容、连接或范围已变化，请重新预览后确认。")
        with coordinator.lease() as lease:
            fresh, store, coordinator, local, remote, metadata, baseline = self._baseline_initialization_state(check_lock=False)
            if fresh != approved:
                raise RuntimeError("首次基线的内容、连接或范围已变化，请重新预览后确认。")
            created_metadata = metadata is None
            generation = metadata["generation"] if metadata else uuid.uuid4().hex
            expected = metadata if metadata is not None else {
                "version": 1, **coordinator.scope, "phase": "ready",
                "generation": generation, "content_digest": remote.digest,
                "writer_identity": self.task.sync_identity,
            }
            try:
                if created_metadata:
                    coordinator.write("ready", generation, content_digest=remote.digest)
                if coordinator.read() != expected:
                    raise RuntimeError("首次基线的数据集状态回读核对失败；没有保存本地基线。")
                # Read both full trees again after the ready response: metadata
                # success alone cannot establish a trustworthy content baseline.
                verified, final_store, _, final_local, final_remote, final_metadata, final_baseline = (
                    self._baseline_initialization_state(check_lock=False)
                )
                if (verified.binding_digest != approved.binding_digest
                        or final_store.path != store.path
                        or verified.local_digest != approved.local_digest
                        or verified.remote_digest != approved.remote_digest
                        or final_metadata != expected or final_baseline != baseline):
                    raise RuntimeError("首次基线的内容或状态已变化；没有保存本地基线。")
                if self.cancelled.is_set():
                    raise RuntimeError("操作已取消")
            except BaseException:
                # A write may have reached the endpoint despite a failed reply.
                # Do not expose uncertain ready metadata to another operation.
                lease.retain()
                raise
        # Save only after successful owner-checked release. A cleanup failure
        # must not leave a newly persisted baseline behind a reported failure.
        # A later publication has a different generation/content and normal sync
        # will reject this last-verified baseline rather than overwrite it.
        if baseline is None:
            store.save(final_remote, generation)
        return BaselineInitializationResult(generation, len(final_remote.files), created_metadata)

    def _inspect(self, *, check_lock: bool = True):
        from app.core.relay import ContentSnapshot
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
        if check_lock and not self.recovering and coordinator.locked():
            raise RuntimeError("此数据集正由另一台电脑操作，或上轮异常退出留下了保护锁；不会自动抢占。")
        if self.recovering and pending["operation"] in ("publish", "return") and not coordinator.locked():
            raise RuntimeError("上次发布的所有权锁已缺失，不能证明仍由原操作独占；请先保留两端文件并核对。")
        metadata = coordinator.read()
        local = self._snapshot("inspect_local", "正在校验本地文件内容…",
                               self.task.local_path, self.task.excludes)
        remote = self._snapshot(
            "inspect_remote", "正在校验手机文件内容；WebDAV 无哈希时需要读取内容…",
            f"{C.DEFAULT_REMOTE}:{self.task.remote_path}", self.task.excludes,
            allow_missing=not self.receiving and self.operation == "publish",
        )
        missing = remote is None
        if remote is None:
            remote = ContentSnapshot({})
        baseline = store.load()
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
            blocked = not source.same_content(original_source)
            if blocked:
                reason = "上次操作后来源又被修改，不能直接继续；请先保留两端文件并核对。"
            if self.receiving and (
                metadata is None or metadata["phase"] != "ready"
                or metadata.get("content_digest") != remote.digest
                or metadata["generation"] != pending["generation"]
            ):
                reason, blocked = "手机发布状态或内容已变化，不能把未完成或未经核对的数据作为续作来源。", True
            if not self.receiving:
                own_publication = metadata is not None and (
                    metadata.get("writer_identity") == self.task.sync_identity
                    and metadata["generation"] == pending["generation"]
                    and metadata.get("content_digest") == original_source.digest
                    and (metadata["phase"] != "ready" or remote.digest == original_source.digest)
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
            missing_replacements = {
                path for path in set(original_source.files) & set(original_destination.files)
                if path not in destination.files
            }
            if missing_replacements:
                history_root = str(Path(self.task.local_history_path) / pending["stamp"]) if self.receiving else (
                    f"{C.DEFAULT_REMOTE}:{self.task.versions_path}/{pending['stamp']}"
                )
                backup = self._snapshot("check_history", "正在核对原历史批次中的旧文件…",
                                        history_root, allow_missing=True)
                if backup is None or any(
                    original_source.files[path] == original_destination.files[path]
                    or backup.files.get(path) != original_destination.files[path]
                    for path in missing_replacements
                ):
                    reason, blocked = "未完成目标出现文件缺失，且原历史批次不能证明旧内容已保留；请先核对再处理。", True
        token = hashlib.sha256(json.dumps([
            self.task.id, self.task.sync_identity, self.task.dataset_id, self.operation,
            local.digest, remote.digest, generation, missing,
            baseline[0].digest if baseline else None, baseline[1] if baseline else None,
            store.binding.get("tls_fingerprint", ""),
        ], ensure_ascii=False).encode("utf-8")).hexdigest()
        report = SyncReport(
            added=sum(mark == "+" for mark in marks.values()),
            updated=sum(mark == "*" for mark in marks.values()),
            deleted=sum(mark == "-" for mark in marks.values()),
            unchanged=sum(mark == "=" for mark in marks.values()),
            changes=tuple((mark, path) for path, mark in marks.items() if mark != "=")[:500],
            digest=token, operation=self.operation, blocked=blocked, reason=reason,
            requires_confirmation=bool(reason) and not blocked,
        )
        self._plan = (local, remote, metadata, source, destination, marks)
        self._stage("difference", report.summary)
        return report

    def preview(self) -> SyncReport:
        return self._inspect()

    def execute(self, report: SyncReport) -> str:
        from app.core.relay import ContentSnapshot
        from app.core.history import _no_links
        if report.blocked:
            raise RuntimeError(report.reason or "内容保护已阻止执行")
        if report.operation != self.operation:
            raise RuntimeError("操作方向与已确认差异不一致")
        store, coordinator = self._context()
        from app.core.pending import PendingOperation
        journal = PendingOperation(store)
        interrupted = journal.load() if self.recovering else None
        context = coordinator.recovery_lease(interrupted["lock_token"]) if interrupted and coordinator.locked() else coordinator.lease()
        self._stage("locking", "正在获取数据集保护锁…")
        with context as lease:
            fresh = self._inspect(check_lock=False)
            if fresh.blocked:
                raise RuntimeError(fresh.reason)
            if fresh.digest != report.digest:
                raise RuntimeError("文件内容或发布批次已变化，请重新查看差异后确认。")
            local, remote, metadata, source, destination, marks = self._plan
            if (metadata and metadata["phase"] == "ready" and local.same_content(remote)
                    and set(source.directories).issubset(destination.directories)):
                self._stage("saving", "文件内容没有变化；正在保存已核对的共同基线…")
                if self.cancelled.is_set():
                    raise RuntimeError("操作已取消")
                store.save(remote, metadata["generation"])
                journal.clear()
                self._stage("releasing", "共同基线已核对；正在释放任务锁…")
                return ""
            self._stage("preparing_history", "正在核对历史批次位置…")
            stamp = interrupted["stamp"] if interrupted else self._history_stamp()
            generation = interrupted["generation"] if interrupted else (
                uuid.uuid4().hex if not self.receiving else metadata["generation"]
            )
            source_root = f"{C.DEFAULT_REMOTE}:{self.task.remote_path}" if self.receiving else self.task.local_path
            destination_root = self.task.local_path if self.receiving else f"{C.DEFAULT_REMOTE}:{self.task.remote_path}"
            history_root = str(Path(self.task.local_history_path) / stamp) if self.receiving else f"{C.DEFAULT_REMOTE}:{self.task.versions_path}/{stamp}"
            payload_started = False
            try:
                with tempfile.TemporaryDirectory(prefix="PhoneNAS-staged-") as temporary:
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
                    if self.cancelled.is_set():
                        raise RuntimeError("操作已取消")
                    if not interrupted:
                        journal.save(
                            operation=self.operation, local=local, remote=remote,
                            generation=generation, stamp=stamp, lock_token=lease.token,
                            prior_metadata=metadata,
                        )
                    payload_started = True
                    if not self.receiving:
                        coordinator.write("publishing", generation, content_digest=source.digest)
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
                    if not origin_after.same_content(source):
                        raise RuntimeError("传输期间来源发生修改；已保留历史，但没有推进共同基线。")
                    self._stage("saving", "正在保存已验证结果与共同基线…")
                    if self.cancelled.is_set():
                        raise RuntimeError("操作已取消")
                    if not self.receiving:
                        coordinator.write("ready", generation, content_digest=verified.digest)
                    store.save(verified, generation)
                    journal.clear()
            except Exception:
                if payload_started and not self.receiving:
                    lease.retain()
                raise
            self._stage("cleanup", "正在检查历史保留并收尾…")
            self._clean_history()
            if self.cancelled.is_set():
                raise RuntimeError("操作已取消")
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
        args = [*args, "--transfers", "4", "--checkers", "8", "--stats", "2s",
                "--use-json-log", "--log-level", "INFO", "--retries", "1",
                "--timeout", "60s", "--contimeout", "15s", "--low-level-retries", "2"]
        proc = self.rclone.popen(args)
        paired = Rclone.paired_process(proc)
        log_handle = None
        succeeded = False
        try:
            if self.log_directory is not None and not paired:
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
                detail = "请检查手机服务、证书和连接。" if paired else "请查看运行日志。"
                raise RuntimeError(f"传输退出码 {code}；{detail}未清理历史文件。")
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

    def _clean_history(self) -> None:
        from app.core.history import HistoryService, LocalHistoryService
        history = LocalHistoryService(self.task) if self.receiving else HistoryService(self.rclone, self.task)
        try:
            for name in old_versions(history.list_versions(), self.task.retention_days):
                self._validate()
                history.delete(name)
        except Exception as exc:
            self._stage("cleanup", f"文件内容已核验；历史清理未完成：{exc}")


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

    def stop(self) -> None:
        self.cancelled.set()
        self.requestInterruption()
        if self.rclone is not None:
            self.rclone.cancel()

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
                )
                report = engine.preview()
                reason = preview_guard(self.task, report)
                if self.preview_only:
                    self.outcome = "preview"
                    self.preview_ready.emit(self.task.id, report, reason)
                    return
                if report.blocked or (reason and self.approved_digest != report.digest):
                    self.outcome = "guarded"
                    self.guarded.emit(self.task.id, report, reason)
                    return
                if self.approved_digest and self.approved_digest != report.digest:
                    self.outcome = "guarded"
                    self.guarded.emit(self.task.id, report, "差异与已确认的预演不同，请重新确认。")
                    return
                stamp = engine.execute(report)
                if self.cancelled.is_set():
                    raise RuntimeError("操作已取消")
                self.outcome = "success"
                self.finished_ok.emit(self.task.id, report, stamp)
        except Exception as exc:  # noqa: BLE001
            if self.cancelled.is_set():
                self.outcome = "cancelled"
                self.cancelled_result.emit(self.task.id, "本次操作已取消；已传输内容不回滚，未清理历史文件。")
            else:
                self.outcome = "failed"
                get_logger().warning("任务 %s: %s", self.task.id, exc)
                self.failed.emit(self.task.id, str(exc))
