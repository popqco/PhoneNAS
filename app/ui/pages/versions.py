from __future__ import annotations

import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QListWidget,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app import constants as C
from app.core.config import Config
from app.core.history import HistoryService, LocalHistoryService
from app.core.rclone import Rclone
from app.core.tasks import MirrorTask
from app.ui.widgets import Card, ElidedLabel, FlowLayout, Header, button, label


_REMOTE_CONNECTION_NOTICE = "手机历史需要已确认的连接。请到「设置」核对连接后保存，或切换到本地接收历史。"

# Parentless QThreads must outlive a page that is unexpectedly deleted. Normal
# shutdown reaps them after wait() succeeds; never destroy/terminate a live thread.
_LIVE_WORKERS: set[_HistoryWorker] = set()


class _HistoryWorker(QThread):
    def __init__(self, snapshot, task: MirrorTask, operation: str, *, location: str, version=None, destination=None, files=None):
        super().__init__()
        self.snapshot = snapshot
        self.task = task
        self.operation = operation
        # Capture the backend now; a later UI selection must never redirect a job.
        self.location = location
        self.version = version
        self.destination = destination
        self.files = files
        self.outcome: tuple[bool, object] = (False, "历史操作未完成")
        self._cancel_requested = threading.Event()
        self._client_lock = threading.Lock()
        self._client = None
        self.cancel_reason = "操作已取消"

    def cancel(self, reason: str = "操作已取消") -> None:
        self.cancel_reason = reason
        self._cancel_requested.set()
        self.requestInterruption()
        with self._client_lock:
            client = self._client
        if client is not None:
            client.cancel()

    @Slot()
    def owner_destroyed(self) -> None:
        self.cancel("历史页已关闭，操作已取消")

    def _check_cancelled(self) -> None:
        if self._cancel_requested.is_set():
            raise RuntimeError(self.cancel_reason)

    def _perform(self, service, *, local: bool):
        self._check_cancelled()
        if self.operation == "versions":
            value = service.list_versions()
        elif self.operation == "files":
            value = service.list_files(self.version)
        elif self.operation == "restore":
            value = service.restore(self.version, self.destination, self.files)
        elif self.operation == "delete":
            service.delete(self.version)
            value = self.version
        elif self.operation == "open":
            # Validate membership and the complete inventory before opening either
            # location. Mounted path probes and shell launches are worker-only.
            if self.version not in service.list_versions():
                raise ValueError("该历史批次已不在当前任务中，请刷新")
            service.list_files(self.version)
            if local:
                path = Path(self.task.local_history_path) / self.version
            else:
                drive = self.snapshot.get("mount.drive", C.DEFAULT_DRIVE)
                if not isinstance(drive, str) or not re.fullmatch(r"[A-Za-z]:", drive):
                    raise ValueError("挂载盘符无效")
                path = Path(drive + "\\").joinpath(*self.task.versions_path.split("/"), self.version)
            self._check_cancelled()
            if not path.is_dir():
                if local:
                    raise ValueError("此本地历史批次已不存在，请刷新后重试。")
                raise ValueError("此批次不在已挂载的目录中。请先挂载，或在这里选择文件恢复。")
            self._check_cancelled()
            os.startfile(str(path))
            value = path
        else:
            raise ValueError("未知的历史操作")
        if self._cancel_requested.is_set():
            suffix = f"；请检查可能已写入的恢复目录 {value}" if self.operation == "restore" else ""
            raise RuntimeError(self.cancel_reason + suffix)
        return value

    def run(self) -> None:
        secrets = []
        try:
            self._check_cancelled()
            if self.location == "local":
                # Offline history must not construct Rclone, decrypt credentials,
                # create its conf, or probe the phone/mounted drive.
                value = self._perform(LocalHistoryService(self.task), local=True)
            elif self.location == "remote":
                # Credentials are touched only on the online branch, in the worker.
                # Redact raw and URL-encoded passwords from errors before emitting.
                password = getattr(self.snapshot, "password", "")
                if password:
                    secrets.extend((password, quote(password, safe="")))
                # Even constructing a real client is worker-only. Supply an
                # explicit path so it never creates APPDATA/rclone.conf.
                with tempfile.TemporaryDirectory(prefix="PhoneNAS-history-") as root:
                    client = Rclone(self.snapshot, conf_path=Path(root) / "rclone.conf")
                    with self._client_lock:
                        self._client = client
                    if self._cancel_requested.is_set():
                        client.cancel()
                    self._check_cancelled()
                    with client.isolated_conf():
                        client.ensure_conf()
                        self._check_cancelled()
                        value = self._perform(HistoryService(client, self.task), local=False)
            else:
                raise ValueError("未知的历史位置")
            self.outcome = (True, value)
        except Exception as exc:  # Worker exceptions must never escape into Qt.
            detail = str(exc) or type(exc).__name__
            if self._cancel_requested.is_set() and self.cancel_reason not in detail:
                detail = f"{self.cancel_reason}；{detail}"
            for secret in secrets:
                detail = detail.replace(secret, "[密码已隐藏]")
            detail = re.sub(r"(?i)(https?://)[^\s/@]+@", r"\1[凭据已隐藏]@", detail)
            self.outcome = (False, detail)
        finally:
            secrets.clear()
            with self._client_lock:
                self._client = None


class VersionsPage(QWidget):
    operation_started = Signal()
    operation_finished = Signal()
    event_recorded = Signal(dict)

    _TIMEOUTS = {"versions": 90_000, "files": 150_000, "restore": 1_800_000, "delete": 480_000, "open": 180_000}

    def __init__(self, config: Config, parent=None, *, operational: bool = True):
        super().__init__(parent)
        self.config = config
        self.operational = operational
        self._worker: _HistoryWorker | None = None
        self._sync_busy = False
        self._status_before_sync: str | None = None
        self._shutting_down = False
        self._generation = 0
        self._context = None
        self._tasks: dict[str, MirrorTask] = {}
        self._versions: list[str] = []
        self._files: list[str] = []
        self._loaded_task: MirrorTask | None = None
        self._loaded_location: str | None = None
        self._files_version: str | None = None

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._timed_out)
        self._reap_timer = QTimer(self)
        self._reap_timer.setInterval(25)
        self._reap_timer.timeout.connect(self._worker_finished)
        self._build_ui()
        self.load_tasks()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._about_to_quit)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 9, 20)
        layout.setSpacing(18)
        header = Header("历史文件", "这里只保存同步时被覆盖或删除的旧文件，按时间归为一批；不是完整快照。")
        self.refresh_button = button("刷新", "secondary", "sync")
        self.cancel_button = button("取消操作", "subtle", "close")
        header.actions.addWidget(self.cancel_button)
        header.actions.addWidget(self.refresh_button)
        layout.addWidget(header)

        task_card = Card()
        task_layout = QVBoxLayout(task_card)
        task_layout.setContentsMargins(18, 15, 18, 15)
        task_row = QHBoxLayout()
        task_row.setSpacing(16)
        task_row.addWidget(label("文件夹任务", "field"))
        self.task_combo = QComboBox()
        self.task_combo.setObjectName("historyTaskCombo")
        self.task_combo.setMinimumWidth(0)
        task_row.addWidget(self.task_combo, 1)
        task_layout.addLayout(task_row)
        location_row = QHBoxLayout()
        location_row.setSpacing(16)
        location_row.addWidget(label("历史位置", "field"))
        self.location_combo = QComboBox()
        self.location_combo.setObjectName("historyLocationCombo")
        self.location_combo.addItem("手机发布历史", "remote")
        self.location_combo.addItem("本地接收历史", "local")
        location_row.addWidget(self.location_combo, 1)
        task_layout.addLayout(location_row)
        path_row = QHBoxLayout()
        self.path_label = ElidedLabel("", "small")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_path_button = button("复制路径", "subtle")
        self.copy_path_button.setObjectName("copyHistoryPath")
        path_row.addWidget(self.path_label, 1)
        path_row.addWidget(self.copy_path_button)
        task_layout.addLayout(path_row)
        layout.addWidget(task_card)

        self.status = label("尚未加载", "bodyMuted")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        left = Card()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(16, 16, 16, 16)
        left_layout.addWidget(label("旧文件批次", "heading"))
        left_layout.addWidget(label("选择一个时间戳，查看这批旧文件", "small"))
        self.list = QListWidget()
        self.list.setObjectName("historyVersions")
        self.list.setMinimumWidth(180)
        self.list.setSpacing(3)
        left_layout.addWidget(self.list, 1)
        right = Card()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(16, 16, 16, 16)
        right_top = QHBoxLayout()
        right_top.addWidget(label("批次内的旧文件", "heading"))
        right_top.addStretch(1)
        self.open_button = button("打开目录", "subtle", "folder")
        right_top.addWidget(self.open_button)
        right_layout.addLayout(right_top)
        self.files_status = label("尚未选择批次", "small")
        self.files_status.setWordWrap(True)
        right_layout.addWidget(self.files_status)
        self.file_list = QListWidget()
        self.file_list.setObjectName("historyFiles")
        self.file_list.setMinimumWidth(160)
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        right_layout.addWidget(self.file_list, 1)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        layout.addWidget(split, 1)

        actions = FlowLayout(spacing=10)
        self.restore_button = button("恢复选中文件", "secondary", "restore")
        self.restore_all_button = button("恢复这批旧文件", "primary", "restore")
        self.delete_button = button("永久删除这批旧文件", "danger", "trash")
        actions.addWidget(self.restore_button)
        actions.addWidget(self.restore_all_button)
        actions.addWidget(self.delete_button)
        layout.addLayout(actions)
        self.busy_hint = label("", "bodyMuted")
        self.busy_hint.setObjectName("historyBusyHint")
        self.busy_hint.setWordWrap(True)
        layout.addWidget(self.busy_hint)
        note = label(
            "恢复保护：在所选目录下新建独占子目录，不覆盖原文件，也不自动恢复到同步源。"
            "一批旧文件不能代表当时的完整目录。永久删除只清除此任务的所选批次，且无法撤销。",
            "small",
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.refresh_button.clicked.connect(self.refresh)
        self.cancel_button.clicked.connect(self._cancel_operation)
        self.task_combo.currentIndexChanged.connect(self._task_changed)
        self.location_combo.currentIndexChanged.connect(self._location_changed)
        self.copy_path_button.clicked.connect(self._copy_path)
        self.list.currentRowChanged.connect(self._version_changed)
        self.file_list.itemSelectionChanged.connect(self._update_actions)
        self.restore_button.clicked.connect(self._restore_selected)
        self.restore_all_button.clicked.connect(self._restore_all)
        self.delete_button.clicked.connect(self._delete)
        self.open_button.clicked.connect(self._open_in_explorer)

    @property
    def busy(self) -> bool:
        # A result is not completion: keep the global gate until wait() confirms
        # the QThread itself has actually exited and its private conf is removed.
        return self._worker is not None

    def _online_allowed(self) -> bool:
        if not self.operational or self._shutting_down:
            return False
        ancestor = self
        while ancestor is not None:
            if ancestor.property("safeMode"):
                return False
            ancestor = ancestor.parentWidget()
        return True

    def _remote_needs_confirmation(self) -> bool:
        return self._location() == "remote" and self.config.connection_needs_confirmation

    def _action_allowed(self) -> bool:
        return (not self.busy and not self._sync_busy and self._online_allowed()
                and not self._remote_needs_confirmation())

    def _current_task(self) -> MirrorTask | None:
        task_id = self.task_combo.currentData()
        task = self.config.get_task(task_id) if task_id else None
        # A task edited without a corresponding load_tasks() must still never
        # reuse an old root's loaded selection against the new task definition.
        if task != self._tasks.get(task_id):
            return None
        return task

    def _location(self) -> str:
        return self.location_combo.currentData()

    def _history_path(self) -> str:
        task = self._current_task()
        if task is None:
            return ""
        return task.local_history_path if self._location() == "local" else task.versions_path

    def _copy_path(self) -> None:
        path = self._history_path()
        if path:
            QApplication.clipboard().setText(path)

    def load_tasks(self) -> None:
        selected = self.task_combo.currentData()
        self._generation += 1
        if self.busy:
            self._cancel_operation()
        self._clear_loaded()
        self._tasks = {task.id: task for task in self.config.tasks}
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        for task in self._tasks.values():
            suffix = "（已暂停同步）" if not task.enabled else ""
            self.task_combo.addItem(task.name + suffix, task.id)
        index = self.task_combo.findData(selected)
        self.task_combo.setCurrentIndex(index if index >= 0 else (0 if self._tasks else -1))
        self.task_combo.blockSignals(False)
        self._task_changed()

    def load(self) -> None:
        self.load_tasks()

    def select_task(self, task_id: str) -> None:
        """Select locally; the caller may explicitly refresh after selecting."""
        index = self.task_combo.findData(task_id)
        if index >= 0:
            self.task_combo.setCurrentIndex(index)

    def _clear_loaded(self) -> None:
        self._loaded_task = None
        self._loaded_location = None
        self._versions = []
        self.list.clear()
        self._clear_files()

    def _clear_files(self) -> None:
        self._files_version = None
        self._files = []
        self.file_list.clear()
        self.files_status.setText("尚未选择批次")

    def _task_changed(self, *_args) -> None:
        task = self._current_task()
        default_location = "local" if task and task.direction == "download" else "remote"
        self.location_combo.blockSignals(True)
        self.location_combo.setCurrentIndex(self.location_combo.findData(default_location))
        self.location_combo.blockSignals(False)
        self._location_changed()

    def _location_changed(self, *_args) -> None:
        self._generation += 1
        self._clear_loaded()
        self.status.setProperty("failed", False)
        path = self._history_path()
        self.path_label.setText(f"{self.location_combo.currentText()} · {path}" if path else "尚无可用文件夹任务")
        self.copy_path_button.setToolTip(f"复制完整路径：{path}" if path else "尚无可复制的历史路径")
        self._status_before_sync = None
        self.status.setText(self._idle_status())
        if self._sync_busy and self._online_allowed():
            self._status_before_sync = self.status.text()
            self.status.setText("同步或其他连接操作正在进行，暂不能操作历史文件。")
        self._update_actions()

    def _idle_status(self) -> str:
        if not self._online_allowed():
            return "离线安全模式：仅预览界面，不读取本地或手机历史，也不启动网络操作。"
        if self._remote_needs_confirmation():
            return "手机连接待确认。"
        if self._current_task() is None:
            return "请先添加一个文件夹任务。"
        if self._location() == "local":
            return "点击刷新，读取此任务的本地接收历史（无需连接手机）。"
        return "点击刷新，读取此任务的手机发布历史。"

    def set_sync_busy(self, busy: bool) -> None:
        was_busy = self._sync_busy
        self._sync_busy = bool(busy)
        if self._sync_busy and not was_busy and not self.busy and not self.status.property("failed"):
            self._status_before_sync = self.status.text()
            self.status.setText("同步或其他连接操作正在进行，暂不能操作历史文件。")
        elif not self._sync_busy and self._status_before_sync is not None:
            if not self.status.property("failed"):
                self.status.setText(self._status_before_sync)
            self._status_before_sync = None
        self._update_actions()

    def _selected(self) -> str | None:
        item = self.list.currentItem()
        version = item.text() if item else None
        if self._loaded_task != self._current_task() or self._loaded_location != self._location() or version not in self._versions:
            return None
        return version

    def _update_actions(self, *_args) -> None:
        available = self._action_allowed()
        version = self._selected()
        files_ready = version is not None and self._files_version == version
        self.task_combo.setEnabled(not self.busy and not self._shutting_down)
        self.location_combo.setEnabled(not self.busy and not self._shutting_down)
        self.copy_path_button.setEnabled(bool(self._history_path()))
        self.refresh_button.setEnabled(available and self._current_task() is not None)
        self.refresh_button.setText("重试刷新" if self.status.property("failed") else "刷新")
        self.cancel_button.setVisible(self.busy)
        self.cancel_button.setEnabled(self.busy and not self._shutting_down)
        self.list.setEnabled(available)
        self.file_list.setEnabled(available and files_ready)
        self.restore_button.setEnabled(available and files_ready and bool(self.file_list.selectedItems()))
        self.restore_all_button.setEnabled(available and files_ready and bool(self._files))
        self.delete_button.setEnabled(available and version is not None)
        self.open_button.setEnabled(available and version is not None)
        if self._shutting_down:
            reason = "正在结束历史操作，请等待后台退出；已完成的恢复或删除不会回滚。"
        elif not self._online_allowed():
            reason = "安全预览模式：不读取本地或手机历史，不执行恢复、删除或网络操作。"
        elif self.busy:
            reason = "历史操作正在进行，完成并退出后台后才可再次操作。取消不会回滚已完成的恢复或删除。"
        elif self._sync_busy:
            reason = "同步或其他连接操作正在进行，历史操作暂不可用；结束后可继续。"
        elif self._remote_needs_confirmation():
            reason = _REMOTE_CONNECTION_NOTICE
        elif self._current_task() is None:
            reason = "请先添加或重新选择文件夹任务。"
        else:
            reason = ""
        self.busy_hint.setText(reason)
        self.busy_hint.setVisible(bool(reason))
        for control in (self.refresh_button, self.restore_button, self.restore_all_button, self.delete_button, self.open_button):
            control.setToolTip(reason)

    def refresh(self) -> None:
        if not self._action_allowed():
            return
        task = self._current_task()
        if task is None:
            self.load_tasks()
            return
        self._clear_loaded()
        self._start_operation("versions")

    def _version_changed(self, *_args) -> None:
        self._clear_files()
        version = self._selected()
        self._update_actions()
        if version is not None:
            self._start_operation("files", version=version)

    def _start_operation(self, operation: str, *, version=None, destination=None, files=None) -> bool:
        if not self._action_allowed():
            return False
        task = self._current_task()
        if task is None:
            self._clear_loaded()
            self.status.setText("任务已更改，请刷新后重试。")
            self._update_actions()
            return False
        if operation != "versions" and (version is None or version != self._selected()):
            return False
        try:
            location = self._location()
            snapshot = self.config.snapshot()
            worker = _HistoryWorker(snapshot, task, operation, location=location, version=version, destination=destination, files=files)
        except Exception as exc:
            self.status.setText(f"无法开始历史操作：{exc}")
            self.status.setProperty("failed", True)
            self._update_actions()
            return False
        self._worker = worker
        self._context = (self._generation, task, location, operation, version)
        _LIVE_WORKERS.add(worker)
        worker.finished.connect(self._worker_finished)
        self.destroyed.connect(worker.owner_destroyed)
        self.status.setProperty("failed", False)
        self.status.setText({
            "versions": "正在读取历史文件批次…", "files": "正在读取并验证文件清单…",
            "restore": "正在恢复旧文件到独立新目录…", "delete": "正在检查并永久删除所选批次…",
            "open": "正在检查并打开此任务的历史目录…",
        }[operation])
        self._update_actions()
        self.operation_started.emit()
        self._timeout.start(self._TIMEOUTS[operation])
        worker.start()
        return True

    @Slot()
    def _worker_finished(self) -> None:
        worker = self._worker
        if worker is None:
            self._reap_timer.stop()
            return
        if worker.isRunning() or not worker.wait(0):
            self._reap_timer.start()
            return
        self._reap_timer.stop()
        self._timeout.stop()
        context = self._context
        self._context = None
        self._worker = None
        _LIVE_WORKERS.discard(worker)
        ok, value = worker.outcome
        generation, task, location, operation, version = context
        current = generation == self._generation and task == self._current_task() and location == self._location()
        if current and not self._shutting_down:
            self._status_before_sync = None
            self._apply_outcome(task, operation, version, ok, value)
        self.event_recorded.emit({
            "task": task.id, "task_name": task.name, "history_location": location,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "kind": "success" if ok else "error", "operation": f"history_{operation}",
            "text": self._event_text(operation, ok), "detail": str(value),
        })
        worker.deleteLater()
        self._update_actions()
        self.operation_finished.emit()

    @staticmethod
    def _event_text(operation: str, ok: bool) -> str:
        action = {"versions": "读取历史批次", "files": "读取历史文件清单", "restore": "恢复历史文件", "delete": "永久删除历史批次", "open": "打开历史目录"}[operation]
        return action + ("完成" if ok else "未完成")

    def _apply_outcome(self, task, operation, version, ok, value) -> None:
        self.status.setProperty("failed", not ok)
        if not ok:
            if operation in ("versions", "delete"):
                self._clear_loaded()
            elif operation == "files":
                self._clear_files()
                self.files_status.setText("读取失败，不代表空目录；请刷新后重试。")
            self.status.setText(f"{self._event_text(operation, False)}：{value}。可点击重试刷新。")
            return
        if operation == "versions":
            self._loaded_task = task
            self._loaded_location = self._location()
            self._versions = list(value)
            self.list.addItems(self._versions)
            self.status.setText(f"此任务有 {len(value)} 批旧文件；选择左侧时间戳查看内容。" if value else "此任务目前没有历史旧文件批次（不是完整快照）。")
        elif operation == "files":
            self._files = list(value)
            self._files_version = version
            self.file_list.addItems(self._files)
            for index, path in enumerate(self._files):
                self.file_list.item(index).setToolTip(path)
            self.files_status.setText(f"{version} · {len(value)} 个旧文件，可多选恢复")
            self.status.setText("恢复将在所选目录内新建专用子目录；不会覆盖本地源文件。")
        elif operation == "restore":
            self.status.setText(f"所选旧文件已恢复到 {value}。这不是完整快照，请检查后再自行取用。")
        elif operation == "delete":
            self._clear_loaded()
            self.status.setText(f"已永久删除此任务的 {version} 批次；点击刷新查看剩余批次。")
        elif operation == "open":
            self.status.setText(f"已打开此任务的历史目录：{value}")

    def _restore_selected(self) -> None:
        files = tuple(item.text() for item in self.file_list.selectedItems())
        if files:
            self._choose_restore(files)

    def _restore_all(self) -> None:
        self._choose_restore(None)

    def _choose_restore(self, files) -> None:
        task, version = self._current_task(), self._selected()
        if task is None or version is None or self._files_version != version or not self._action_allowed():
            return
        generation = self._generation
        directory = QFileDialog.getExistingDirectory(
            self, "选择恢复位置（自动新建子目录，不覆盖原文件）", str(Path(task.local_path).parent),
        )
        if not directory:
            return
        if generation != self._generation or task != self._current_task() or version != self._selected():
            self.status.setText("任务或批次已更改，请重新选择恢复文件。")
            return
        self._start_operation("restore", version=version, destination=Path(directory), files=files)

    def _delete(self) -> None:
        task, version = self._current_task(), self._selected()
        if task is None or version is None or not self._action_allowed():
            return
        generation = self._generation
        answer = QMessageBox.question(
            self, "永久删除历史文件",
            f"永久删除任务“{task.name}”的 {version} 这批旧文件？\n\n"
            f"历史位置：{self.location_combo.currentText()}\n{self._history_path()}\n\n"
            "仅删除此任务所选历史目录中的旧文件，不是删除镜像；此操作无法撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if generation != self._generation or task != self._current_task() or version != self._selected():
            self.status.setText("任务或批次已更改，未执行删除。请刷新重试。")
            return
        self._start_operation("delete", version=version)

    def _open_in_explorer(self) -> None:
        version = self._selected()
        if version is not None:
            self._start_operation("open", version=version)

    def _cancel_operation(self, *_args) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status.setText("正在取消历史操作并等待后台退出…；恢复/删除可能已产生部分结果。")
            self.cancel_button.setEnabled(False)

    def _timed_out(self) -> None:
        if self._worker is not None:
            self._worker.cancel("历史操作超时，已请求取消")
            self.status.setText("历史操作超时，正在等待后台退出；恢复/删除可能已产生部分结果。")
            self.cancel_button.setEnabled(False)

    def shutdown(self) -> bool:
        self._shutting_down = True
        self._timeout.stop()
        if self._worker is not None:
            self._worker.cancel("应用退出，历史操作已取消")
            if self._worker.wait(50):
                self._worker_finished()
        self._update_actions()
        return not self.busy

    def _about_to_quit(self) -> None:
        self.shutdown()
        if self._worker is not None:
            # MainWindow normally polls shutdown before QApplication.quit. This
            # final fallback also cancels when another owner quits the app.
            self._worker.wait(2000)
            self._worker_finished()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.shutdown():
            event.accept()
        else:
            event.ignore()
