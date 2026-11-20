from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime
from time import monotonic

from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from app import constants as C
from app.core import winfsp
from app.core.autostart import set_autostart
from app.core.config import Config
from app.core.history import validate_local_history_path
from app.core.log import get_logger
from app.core.mount import MountSupervisor
from app.core.progress import SyncProgress
from app.core.scheduler import Scheduler
from app.core.sync import SyncReport, SyncWorker
from app.core.tasks import MirrorTask, new_task, validate_source, validate_tasks
from app.ui.pages.logs import LogsPage
from app.ui.pages.mirror import MirrorPage
from app.ui.pages.settings import SettingsPage
from app.ui.pages.status import StatusPage
from app.ui.pages.versions import VersionsPage
from app.ui.preview_dialog import PreviewDialog
from app.ui.task_dialog import MultiFolderDialog, TaskDialog
from app.ui.widgets import Badge, IconBox, icon, label
from app.ui.wizard import WizardDialog


_SCHEDULE_APPROVAL = "1.2.4-filenames"
_SCHEDULE_NOTICE = "文件名校验规则已更新，自动计划已暂停。请先核对任务差异，再点击「继续自动计划」。"
_SYNC_ALL_TEXT = "逐项核对并同步"
_SYNC_ALL_TIP = "依次查看各个已启用任务的差异；确认后才执行，关闭预演会跳过该项。"


class MainWindow(QMainWindow):
    def __init__(self, config: Config, first_run: bool = False, *, operational: bool = True):
        super().__init__()
        self.config = config
        self.operational = operational
        self.setProperty("safeMode", not operational)
        self.log = get_logger()
        self.mount = MountSupervisor(config, self)
        self.scheduler = Scheduler(config.tasks if config.get("mirror.enabled", True) else (), self)
        self.sync_worker: SyncWorker | None = None
        self.states: dict[str, dict] = {}
        self.events: list[dict] = []
        self._preview_result: tuple[SyncReport, str] | None = None
        self._settings_pending = False
        self._connection_signature = self._connection_settings()
        self._tray_hint_shown = False
        self._quitting = False
        self._wizard: QDialog | None = None
        self._first_run = first_run and not config.path.exists()
        self._enforce_schedule_approval()
        self.setWindowTitle(f"{C.APP_NAME} {C.APP_VERSION}" + (" · 界面检查（不连接手机）" if not operational else ""))
        self.resize(1320, 880)
        self.setMinimumSize(780, 520)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(1320, max(780, available.width() - 40)), min(880, max(520, available.height() - 60)))
        self._build_ui()
        self._build_tray()
        self._connect()
        self.refresh_status()
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(15000)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start()
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(1000)
        self._progress_timer.timeout.connect(self._refresh_progress)
        self._quit_timer = QTimer(self)
        self._quit_timer.setInterval(150)
        self._quit_timer.timeout.connect(self._finish_quit)
        if operational:
            if config.load_error:
                QTimer.singleShot(200, self, self._show_config_error)
            elif self._first_run:
                QTimer.singleShot(250, self, self.open_wizard)
            else:
                QTimer.singleShot(400, self, self._start_automatic_mount)
                QTimer.singleShot(2500, self, self._start_scheduler)

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("navigationSidebar")
        sidebar.setFixedWidth(198)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 26, 14, 20)
        side.setSpacing(15)
        brand = QHBoxLayout()
        brand.setSpacing(10)
        brand.addWidget(IconBox("network", "blue", 34))
        names = QVBoxLayout()
        names.setSpacing(3)
        names.addWidget(label("PhoneNAS", "heading"))
        names.addWidget(label("YOUR POCKET NAS", "eyebrow"))
        brand.addLayout(names)
        side.addLayout(brand)
        side.addSpacing(16)
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.sidebar.setAccessibleName("页面导航；上下方向键切换页面")
        self.sidebar.setIconSize(QSize(18, 18))
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack = QStackedWidget()
        self.status_page = StatusPage()
        self.mirror_page = MirrorPage(self.config)
        self.versions_page = VersionsPage(self.config, operational=self.operational)
        self.logs_page = LogsPage()
        self.settings_page = SettingsPage(self.config, operational=self.operational)
        pages = [
            ("总览", "grid", self.status_page),
            ("同步任务", "folders", self.mirror_page),
            ("历史文件", "history", self.versions_page),
            ("活动记录", "activity", self.logs_page),
            ("设置", "settings", self.settings_page),
        ]
        self._page_titles = [name for name, _, _ in pages]
        for name, glyph, page in pages:
            item = QListWidgetItem(icon(glyph), name)
            item.setSizeHint(QSize(160, 46))
            self.sidebar.addItem(item)
            self.stack.addWidget(page)
        side.addWidget(self.sidebar, 1)
        device_divider = QFrame()
        device_divider.setObjectName("divider")
        device_divider.setFixedHeight(1)
        side.addWidget(device_divider)
        self.side_mount = label("手机 NAS · 尚未挂载", "small")
        self.side_mount.setWordWrap(True)
        side.addWidget(self.side_mount)
        side.addWidget(label(f"版本 {C.APP_VERSION}", "small"))
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(28, 0, 28, 20)
        body_layout.setSpacing(22)
        top = QFrame()
        top.setObjectName("topbar")
        top.setMinimumHeight(65)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        self.breadcrumb = label("我的空间  /  同步任务", "small")
        top_layout.addWidget(self.breadcrumb, 1)
        self.top_badge = Badge("未挂载", "neutral")
        top_layout.addWidget(self.top_badge)
        if not self.operational:
            top_layout.addWidget(Badge("界面检查 · 不连接手机", "blue"))
        body_layout.addWidget(top)
        self._schedule_notice = label(_SCHEDULE_NOTICE, "bodyMuted")
        self._schedule_notice.setWordWrap(True)
        body_layout.addWidget(self._schedule_notice)
        self.status_page.sync_button.setText(_SYNC_ALL_TEXT)
        self.status_page.sync_button.setToolTip(_SYNC_ALL_TIP)
        body_layout.addWidget(self.stack, 1)
        layout.addWidget(sidebar)
        layout.addWidget(body, 1)
        self.setCentralWidget(central)
        self.sidebar.currentRowChanged.connect(self._nav_changed)
        self.sidebar.setCurrentRow(1)
        self.statusBar().setSizeGripEnabled(True)
        self.statusBar().showMessage("就绪" if self.operational else "界面检查模式：不挂载、不执行同步、不读写正式配置")

    def _build_tray(self) -> None:
        self.tray = None
        if not self.operational or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        app_icon = QIcon(str(C.resource_path("app/resources/app.ico")))
        self.tray = QSystemTrayIcon(app_icon, self)
        menu = QMenu(self)
        actions = [
            ("打开主界面", self.show_window),
            (_SYNC_ALL_TEXT, self.run_sync),
            ("挂载 / 卸载", self.toggle_mount),
            ("暂停 / 继续自动计划", self.toggle_pause),
        ]
        for text, slot in actions:
            action = QAction(text, self)
            action.triggered.connect(lambda _checked=False, callback=slot: callback())
            menu.addAction(action)
        menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _connect(self) -> None:
        self.scheduler.ready.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)
        self.mount.state_changed.connect(lambda _state: self.refresh_status())
        self.mount.log_line.connect(lambda text: self._event("手机 NAS", text, "disk"))
        self.status_page.sync_requested.connect(self.run_sync)
        self.status_page.pause_toggle_requested.connect(self.toggle_pause)
        self.status_page.mount_toggle_requested.connect(self.toggle_mount)
        self.status_page.tasks_requested.connect(lambda: self.sidebar.setCurrentRow(1))
        self.status_page.open_drive_requested.connect(self.open_drive)
        self.mirror_page.sync_requested.connect(lambda task_id: self.run_sync(task_id))
        self.mirror_page.upload_requested.connect(lambda task_id: self.manual_transfer(task_id, uploading=True))
        self.mirror_page.download_requested.connect(lambda task_id: self.manual_transfer(task_id, uploading=False))
        self.mirror_page.preview_requested.connect(self.preview_task)
        self.mirror_page.return_requested.connect(lambda task_id: self.relay_task(task_id, "return"))
        self.mirror_page.adopt_requested.connect(lambda task_id: self.relay_task(task_id, "adopt"))
        self.mirror_page.cancel_requested.connect(self.cancel_task)
        self.mirror_page.recover_requested.connect(lambda task_id: self.relay_task(task_id, "recover"))
        self.mirror_page.add_requested.connect(self.add_task)
        self.mirror_page.batch_requested.connect(self.add_folders)
        self.mirror_page.edit_requested.connect(self.edit_task)
        self.mirror_page.toggle_requested.connect(self.toggle_task)
        self.mirror_page.remove_requested.connect(self.remove_task)
        self.mirror_page.history_requested.connect(self.show_history)
        self.mirror_page.pause_requested.connect(self.toggle_pause)
        self.mirror_page.open_drive_requested.connect(self.open_drive)
        self.settings_page.saved.connect(self._on_settings_saved)
        for page in (self.settings_page, self.versions_page):
            page.operation_started.connect(self._operation_started)
            page.operation_finished.connect(self._operation_finished)
        self.versions_page.event_recorded.connect(self._history_event)

    @property
    def busy(self) -> bool:
        return bool(self.sync_worker or self.settings_page.busy or self.versions_page.busy or self._wizard)

    def _message(self, title: str, text: str, *, warning: bool = False) -> None:
        (QMessageBox.warning if warning else QMessageBox.information)(self, title, text)

    def _can_start(self) -> bool:
        return self.operational and not self._quitting and not self.config.load_error

    def _allowed(self) -> bool:
        if self._quitting:
            return False
        if not self.operational:
            self.statusBar().showMessage("界面检查模式：此操作不会连接手机或修改真实配置。")
            return False
        if self.config.load_error:
            self._message("配置无效", self.config.load_error, warning=True)
            return False
        return True

    @property
    def _schedule_approved(self) -> bool:
        return self.config.get("app.schedule_approval", "") == _SCHEDULE_APPROVAL

    def _enforce_schedule_approval(self) -> None:
        if self.operational and not self._schedule_approved:
            self.scheduler.queue.pause()

    def _save_schedule_approval(self) -> bool:
        previous = self.config.get("app.schedule_approval", "")
        try:
            self.config.set("app.schedule_approval", _SCHEDULE_APPROVAL)
            self.config.save()
        except Exception as exc:
            self.config.set("app.schedule_approval", previous)
            self._message("未能继续自动计划", f"确认未能保存，自动计划仍已暂停：{exc}", warning=True)
            return False
        return True

    def _start_scheduler(self) -> None:
        if self._can_start() and not self.scheduler._started:
            self._enforce_schedule_approval()
            self.scheduler.start()

    def _start_mount(self) -> None:
        if self._can_start() and self.config.get("mount.enabled", True):
            self.mount.start()

    def _start_automatic_mount(self) -> None:
        if self.config.get("mount.auto_mount", True):
            self._start_mount()

    def _show_config_error(self) -> None:
        if self.operational and not self._quitting and self.config.load_error:
            self._message("配置需要处理", self.config.load_error, warning=True)

    def _exec_dialog(self, dialog: QDialog, *, accepted_code: int = QDialog.DialogCode.Accepted) -> bool:
        self._wizard = dialog
        self._set_operation_gates()
        try:
            return dialog.exec() == accepted_code
        finally:
            self._wizard = None
            self._set_operation_gates()
            QTimer.singleShot(0, self, self._dispatch)

    def _editable(self) -> bool:
        if not self._allowed():
            return False
        if self.busy:
            self._message("正在执行任务", "请等当前同步、预演或历史操作结束后再修改配置。")
            return False
        return True

    def _nav_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if hasattr(self, "breadcrumb") and index >= 0:
            self.breadcrumb.setText(f"我的空间  /  {self._page_titles[index]}")
        if self.stack.currentWidget() is self.versions_page and self.operational:
            self.versions_page.refresh()
        elif self.stack.currentWidget() is self.logs_page:
            self.logs_page.refresh()

    def add_task(self) -> None:
        if not self._editable():
            return
        dialog = TaskDialog(self.config, parent=self)
        if self._exec_dialog(dialog) and dialog.task is not None:
            self._store_task(dialog.task)

    def edit_task(self, task_id: str) -> None:
        if not self._editable():
            return
        task = self.config.get_task(task_id)
        if task is None:
            return
        dialog = TaskDialog(self.config, task, self)
        if self._exec_dialog(dialog) and dialog.task is not None:
            self._store_task(dialog.task)

    def _store_task(self, task: MirrorTask) -> None:
        try:
            draft = self.config.snapshot()
            draft.upsert_task(task)
            draft.save()
            self.config.apply_snapshot(draft)
            self._reload_tasks()
            self._event(task.name, "任务设置已保存", "settings")
        except Exception as exc:
            self._message("保存失败", str(exc), warning=True)

    def add_folders(self) -> None:
        if not self._editable():
            return
        dialog = MultiFolderDialog(self)
        if not self._exec_dialog(dialog):
            return
        try:
            added = [replace(new_task(folder), auto_sync=False) for folder in dialog.folders]
            tasks = [*self.config.tasks, *added]
            validate_tasks(tasks, self.config.get("mount.drive", C.DEFAULT_DRIVE))
            for task in added:
                validate_source(task, self.config.get("mount.drive", C.DEFAULT_DRIVE))
                validate_local_history_path(task.local_history_path)
            draft = self.config.snapshot()
            draft.replace_tasks(tasks)
            draft.save()
            self.config.apply_snapshot(draft)
            self._reload_tasks()
            self._event("同步任务", f"已添加 {len(added)} 个独立文件夹", "folders")
        except Exception as exc:
            self._message("无法添加文件夹", str(exc), warning=True)

    def toggle_task(self, task_id: str) -> None:
        if not self._editable():
            return
        task = self.config.get_task(task_id)
        if task is not None:
            self._store_task(replace(task, enabled=not task.enabled))

    def remove_task(self, task_id: str) -> None:
        if not self._editable():
            return
        task = self.config.get_task(task_id)
        if task is None:
            return
        answer = QMessageBox.question(
            self, "只移除任务，不删除文件",
            f"移除“{task.name}”的同步计划？\n手机上的当前镜像和历史文件会保留，不会自动删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        draft = self.config.snapshot()
        try:
            draft.remove_task(task_id)
            draft.save()
            self.config.apply_snapshot(draft)
        except Exception as exc:
            self._message("移除失败", str(exc), warning=True)
            return
        self.states.pop(task_id, None)
        self._reload_tasks()
        self._event(task.name, "已移除任务配置，手机文件保留", "folders")

    def _reload_tasks(self) -> None:
        self._enforce_schedule_approval()
        self.scheduler.update_tasks(self.config.tasks if self.config.get("mirror.enabled", True) else ())
        self._start_scheduler()
        self.mirror_page.load()
        self.versions_page.load_tasks()
        self.refresh_status()

    def run_sync(self, task_id: str | None = None) -> None:
        if not self._allowed():
            return
        if not self.config.get("mirror.enabled", True):
            self._message("计划已禁用", "全局同步未启用，请先在设置中启用。")
            return
        if task_id is None:
            if not self.isVisible() or self.isMinimized():
                self.show_window()
            if self.busy:
                self.statusBar().showMessage("请等当前操作完成，再逐项核对并同步。")
                return
            if not any(task.enabled for task in self.config.tasks):
                self.statusBar().showMessage("没有已启用的任务，请先添加或启用任务。")
                return
        else:
            task = self.config.get_task(task_id)
            if task is None or not task.enabled:
                self._message("任务已暂停", "请先启用该任务，再执行同步。")
                return
        self.scheduler.request(task_id, manual=True)
        self.refresh_status()

    def preview_task(self, task_id: str) -> None:
        if not self._editable():
            return
        task = self.config.get_task(task_id)
        if task is not None:
            self._launch(task, preview_only=True)

    def manual_transfer(self, task_id: str, *, uploading: bool) -> None:
        task = self.config.get_task(task_id)
        if task is None:
            return
        operation = ("publish" if task.direction == "upload" else "return") if uploading else (
            "receive" if task.direction == "download" else "adopt"
        )
        self.relay_task(task_id, operation)

    def relay_task(self, task_id: str, operation: str) -> None:
        if not self._editable():
            return
        task = self.config.get_task(task_id)
        if task is None or not task.enabled:
            self.statusBar().showMessage("请先启用任务，再核对接力操作。")
            return
        self._launch(task, preview_only=True, operation=operation)

    def _can_cancel(self, worker: SyncWorker, task_id: str) -> bool:
        return (
            self.operational and not self._quitting and self.sync_worker is worker
            and worker.task.id == task_id and self.states.get(task_id, {}).get("state") in ("running", "preview")
            and worker.outcome == "pending" and worker.isRunning()
        )

    def cancel_task(self, task_id: str) -> None:
        worker = self.sync_worker
        if worker is None or not self._can_cancel(worker, task_id):
            return
        answer = QMessageBox.question(
            self, "取消本次操作", "停止当前操作？已经传输的文件不回滚，未完成的操作不会记录为成功，也不会清理历史文件。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        # The modal loop may have delivered a result, started another worker or begun shutdown.
        if answer != QMessageBox.StandardButton.Yes or not self._can_cancel(worker, task_id):
            return
        if worker.stop() is False:
            return
        self.states[task_id] = {"state": "cancelling", "message": "正在取消；等待传输进程退出…", "progress": None}
        self._progress_timer.stop()
        self.statusBar().showMessage(self.states[task_id]["message"])
        self.refresh_status()

    def _dispatch(self) -> None:
        if not self._can_start() or self.busy or not self.config.get("mirror.enabled", True):
            return
        request = self.scheduler.queue.take_next_request()
        if request is None:
            self.refresh_status()
            return
        task_id, manual = request
        task = self.config.get_task(task_id)
        if task is None or not task.enabled:
            self.scheduler.complete(task_id)
            return
        self._launch(task, preview_only=manual)

    def _launch(self, task: MirrorTask, *, preview_only: bool = False, operation: str = "", approved_digest: str = "") -> None:
        self._progress_timer.stop()
        self._preview_result = None
        worker = SyncWorker(
            self.config, task, self, preview_only=preview_only, operation=operation,
            approved_digest=approved_digest,
        )
        self.sync_worker = worker
        self.states[task.id] = {"state": "preview" if preview_only else "running", "message": "正在校验差异…", "progress": None, "operation": worker.operation}
        self.statusBar().showMessage(self.states[task.id]["message"])
        worker.progress.connect(self._on_progress)
        worker.progress_event.connect(self._on_progress_event)
        worker.finished_ok.connect(self._on_sync_ok)
        worker.failed.connect(self._on_failed)
        worker.cancelled_result.connect(self._on_cancelled)
        worker.guarded.connect(self._on_guarded)
        worker.preview_ready.connect(self._on_preview)
        worker.finished.connect(self._on_worker_finished)
        self._set_operation_gates()
        self.refresh_status()
        worker.start()

    def _on_progress_event(self, task_id: str, event: SyncProgress) -> None:
        worker = self.sync_worker
        state = self.states.get(task_id, {})
        if self._quitting or worker is None or worker.task.id != task_id or state.get("state") not in ("running", "preview"):
            return
        previous = state.get("progress_event")
        if previous is None or previous.stage != event.stage:
            state["phase_started_at"] = monotonic()
        state["progress_event"] = event
        state["progress"] = event.percent
        self._progress_timer.start()
        self._refresh_progress()

    def _refresh_progress(self) -> None:
        worker = self.sync_worker
        state = self.states.get(worker.task.id, {}) if worker is not None else {}
        event = state.get("progress_event")
        if event is None or state.get("state") not in ("running", "preview") or self._quitting:
            self._progress_timer.stop()
            return
        elapsed = max(0.0, monotonic() - state["phase_started_at"])
        state["message"] = event.display(elapsed)
        self.statusBar().showMessage(state["message"])
        self.refresh_status()

    def _on_progress(self, task_id: str, text: str) -> None:
        self.status_page.append_output(text)
        self.log.info("[%s] %s", task_id, text)
        state = self.states.get(task_id, {})
        worker = self.sync_worker
        if (self._quitting or worker is None or worker.task.id != task_id
                or state.get("state") not in ("running", "preview")
                or state.get("progress_event") is not None):
            return
        state["message"] = text
        state["progress"] = None
        self.statusBar().showMessage(text)
        self.refresh_status()

    def _on_sync_ok(self, task_id: str, report: SyncReport, stamp: str) -> None:
        self._progress_timer.stop()
        action = {"publish": "上传", "receive": "拉取", "return": "交回", "adopt": "接收手机更新"}.get(report.operation, "同步")
        message = f"{action}完成 · {report.summary}"
        self.states[task_id] = {"state": "idle", "message": message, "progress": None, "operation": report.operation}
        self.statusBar().showMessage(message)
        try:
            self.config.mark_task_success(task_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), report.source_files)
            self.config.save()
        except Exception as exc:
            self.states[task_id]["message"] = f"镜像完成，但保存执行记录失败：{exc}"
        task = self.config.get_task(task_id)
        self._event(task.name if task else task_id, f"{action}完成", "check", report.summary)

    def _on_failed(self, task_id: str, message: str) -> None:
        self._progress_timer.stop()
        self.states[task_id] = {"state": "error", "message": message, "progress": None}
        task = self.config.get_task(task_id)
        self._event(task.name if task else task_id, "执行失败", "info", message)
        self.statusBar().showMessage(f"任务失败：{message}")

    def _on_cancelled(self, task_id: str, message: str) -> None:
        self._progress_timer.stop()
        self.states[task_id] = {"state": "cancelled", "message": message, "progress": None}
        task = self.config.get_task(task_id)
        self.statusBar().showMessage(message)
        self._event(task.name if task else task_id, "本次操作已取消", "info", message)

    def _on_guarded(self, task_id: str, report: SyncReport, reason: str) -> None:
        self._progress_timer.stop()
        self.states[task_id] = {"state": "guarded", "message": reason, "progress": None, "operation": report.operation}
        self.statusBar().showMessage(reason)
        task = self.config.get_task(task_id)
        self._event(task.name if task else task_id, "保护已阻止执行", "shield", reason)

    def _on_preview(self, task_id: str, report: SyncReport, reason: str) -> None:
        worker = self.sync_worker
        if worker is None or worker.task.id != task_id or not worker.preview_only:
            return
        self._progress_timer.stop()
        message = f"差异核对完成 · {report.summary}"
        self.states[task_id] = {"state": "guarded" if reason else "idle", "message": reason if report.blocked else message, "progress": None, "operation": report.operation}
        self.statusBar().showMessage(reason if report.blocked else message)
        self._preview_result = (report, reason)
        self.refresh_status()

    def _confirm_preview(self, task: MirrorTask, operation: str, report: SyncReport, reason: str) -> bool:
        dialog = PreviewDialog(task, report, reason, self, operation=operation)
        if not self._exec_dialog(dialog) or report.blocked or not self._can_start():
            return False
        if (not report.digest or (operation != "recover" and report.operation != operation)
                or not task.enabled or self.config.get_task(task.id) != task or self._settings_pending
                or self.busy or not self.config.get("mirror.enabled", True)):
            self.statusBar().showMessage("任务或差异已变化，请重新核对后再执行。")
            return False
        queue = self.scheduler.queue
        if queue.active_id != task.id and not queue.claim(task.id):
            self.statusBar().showMessage("任务状态已变化，请重新核对后再执行。")
            return False
        # The digest authorizes only this worker. Its fresh scan must match before applying.
        self._launch(task, operation=operation, approved_digest=report.digest)
        return True

    def _on_worker_finished(self) -> None:
        worker = self.sync_worker
        if worker is None:
            return
        task = worker.task
        preview_only = worker.preview_only
        operation = worker.operation
        preview_result = self._preview_result
        self._preview_result = None
        self._progress_timer.stop()
        state = self.states.get(task.id, {})
        state.pop("progress_event", None)
        state.pop("phase_started_at", None)
        self.sync_worker = None
        worker.deleteLater()
        if preview_only:
            if (preview_result is not None and self._can_start() and self.config.get_task(task.id) == task
                    and self._confirm_preview(task, operation, *preview_result)):
                return  # Keep the slot until the confirmed write finishes.
            self.scheduler.queue.skip_preview(task.id)
        else:
            self.scheduler.complete(task.id)
            self.mirror_page.load()
        self._operation_finished()

    def show_history(self, task_id: str) -> None:
        self.versions_page.select_task(task_id)
        self.sidebar.setCurrentRow(2)

    def _operation_started(self) -> None:
        self._set_operation_gates()

    def _operation_finished(self) -> None:
        if self._settings_pending and not self.busy:
            self._apply_settings()
        self._set_operation_gates()
        self.refresh_status()
        QTimer.singleShot(0, self, self._dispatch)

    def _set_operation_gates(self) -> None:
        syncing = self.sync_worker is not None or self._wizard is not None or self._quitting
        self.settings_page.set_sync_busy(syncing or self.versions_page.busy)
        self.versions_page.set_sync_busy(syncing or self.settings_page.busy)
        self.mirror_page.set_interaction_busy(self.busy or self._quitting)

    def _connection_settings(self) -> tuple:
        return tuple(self.config.get(key) for key in (
            "nas.url", "nas.username", "nas.password_enc", "mount.drive", "mount.network_mode", "mount.enabled", "mount.read_only",
        ))

    def _on_settings_saved(self) -> None:
        self._settings_pending = True
        if not self.busy:
            self._apply_settings()

    def _apply_settings(self) -> None:
        self._settings_pending = False
        if not self._can_start():
            return
        self._reconcile_mount()
        self._reload_tasks()
        self._event("设置", "连接与任务配置已更新", "settings")

    def _reconcile_mount(self, *, setup: bool = False) -> None:
        signature = self._connection_settings()
        changed = signature != self._connection_signature
        was_mounted = self.mount.state in ("mounted", "starting", "retrying")
        if changed:
            self.mount.stop()
            self._connection_signature = signature
        if setup:
            if changed and was_mounted:
                QTimer.singleShot(350, self, self._start_automatic_mount)
            else:
                self._start_automatic_mount()
        elif changed and was_mounted:
            QTimer.singleShot(350, self, self._start_mount)

    def open_wizard(self) -> None:
        if not self._editable():
            return
        existing = {task.id for task in self.config.tasks}
        dialog = WizardDialog(self.config, self, operational=self.operational)
        if self._exec_dialog(dialog) and self._can_start():
            # A fresh setup can opt in by explicitly creating an automatic task.
            # An existing or imported task cannot supply that approval.
            if self._first_run and not self._schedule_approved and any(
                task.id not in existing and task.enabled and task.auto_sync for task in self.config.tasks
            ) and self._save_schedule_approval():
                self.scheduler.resume()
            self._first_run = False
            self.settings_page.load()
            self._reload_tasks()
            set_autostart(bool(self.config.get("app.start_with_windows", True)))
            self._reconcile_mount(setup=True)

    def toggle_mount(self) -> None:
        if not self._allowed():
            return
        if self.settings_page.busy:
            return
        if self.mount.state in ("mounted", "starting", "retrying"):
            self.mount.stop()
        elif not winfsp.is_installed():
            self._message("需要 WinFsp", "请到设置页安装内置 WinFsp 后再挂载。", warning=True)
        else:
            self.mount.start()
        self.refresh_status()

    def toggle_pause(self) -> None:
        if not self._allowed():
            return
        if self.scheduler.paused:
            if not self._schedule_approved:
                if self.busy:
                    self.statusBar().showMessage("请等当前操作完成，再继续自动计划。")
                    return
                dialog = QMessageBox(
                    QMessageBox.Icon.Warning, "继续自动计划",
                    _SCHEDULE_NOTICE + "\n\n确认后，已到期的自动任务可能立即执行。是否继续？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self,
                )
                dialog.setDefaultButton(QMessageBox.StandardButton.No)
                dialog.button(QMessageBox.StandardButton.Yes).setText("继续自动计划")
                if (not self._exec_dialog(dialog, accepted_code=QMessageBox.StandardButton.Yes.value)
                        or not self._can_start() or self.busy or not self._save_schedule_approval()):
                    return
            self.scheduler.resume()
        else:
            self.scheduler.pause()
        self.refresh_status()

    def open_drive(self) -> None:
        if not self._allowed():
            return
        if self.mount.state != "mounted":
            self._message("网络盘尚未挂载", "请先在总览或托盘菜单挂载网络盘。")
            return
        os.startfile(self.mount.drive + "\\")

    def _event(self, task: str, text: str, kind: str = "check", detail: str = "") -> None:
        event = {"time": datetime.now().strftime("%H:%M:%S"), "task": task, "text": text, "kind": kind, "detail": detail}
        self.events.insert(0, event)
        del self.events[100:]
        self.logs_page.append_event(event)
        self.log.info("%s · %s %s", task, text, detail)
        self.refresh_status()

    def _history_event(self, event: dict) -> None:
        task = self.config.get_task(event.get("task", ""))
        name = event.get("task_name") or (task.name if task else "历史文件")
        self._event(name, event.get("text", "历史操作完成"), event.get("kind", "history"), event.get("detail", ""))

    def refresh_status(self) -> None:
        if not hasattr(self, "status_page"):
            return
        queue = self.scheduler.queue
        runtime = {task_id: dict(value) for task_id, value in self.states.items()}
        for task_id in queue.queued_ids:
            if task_id != queue.active_id and not (self.sync_worker and self.sync_worker.task.id == task_id):
                runtime[task_id] = {"state": "queued", "message": "等待前一个任务完成", "progress": None}
        next_runs = {}
        for task in self.config.tasks:
            due = queue.next_for(task.id)
            next_runs[task.id] = due.strftime("%H:%M") if due else ""
        last = max((t.last_sync_at for t in self.config.tasks), default="")
        next_run = queue.next_run_at
        self._schedule_notice.setVisible(
            self._can_start() and queue.paused and not self._schedule_approved
            and bool(self.config.get("mirror.enabled", True))
            and any(task.enabled and task.auto_sync for task in self.config.tasks)
        )
        self.status_page.set_nas(self.config.url)
        self.status_page.set_winfsp(winfsp.is_installed())
        self.status_page.set_mount(self.mount.state, self.mount.drive)
        self.status_page.set_sync(last, next_run.strftime("%H:%M") if next_run else "", queue.paused)
        self.status_page.set_summary(
            self.config.tasks, runtime, len(queue.queued_ids),
            sync_available=self.operational and not self.busy and not self._quitting and bool(self.config.get("mirror.enabled", True)),
        )
        self.mirror_page.set_runtime(runtime, next_runs, queue.paused, self.mount.state, self.mount.drive, self.events)
        state_text = {"mounted": f"已挂载 {self.mount.drive}", "starting": "正在挂载", "retrying": "正在重试挂载", "error": "挂载失败", "stopped": "未挂载"}.get(self.mount.state, self.mount.state)
        self.top_badge.set_status(state_text, "good" if self.mount.state == "mounted" else "neutral")
        self.side_mount.setText(f"手机 NAS · {state_text}")
        if self.tray:
            self.tray.setToolTip(f"{C.APP_NAME} · {state_text} · {len(self.config.tasks)} 个任务")

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._quitting:
            event.accept()
            return
        if self.config.get("app.minimize_to_tray", True) and self.tray is not None:
            self.hide()
            event.ignore()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(C.APP_NAME, "PhoneNAS 仍在后台运行，可从托盘打开或退出。")
            return
        event.ignore()
        self.quit_app()

    def quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.scheduler.stop()
        self._status_timer.stop()
        self._progress_timer.stop()
        if self.sync_worker:
            self.sync_worker.stop()
        self.settings_page.shutdown()
        self.versions_page.shutdown()
        self.mount.stop()  # Disarm retries even while other workers are still finishing.
        self._quit_timer.start()
        self._finish_quit()

    def _finish_quit(self) -> None:
        if self.sync_worker is not None:
            return
        if not self.settings_page.shutdown() or not self.versions_page.shutdown() or not self.logs_page.shutdown():
            return
        if self._wizard is not None:
            shutdown = getattr(self._wizard, "shutdown", None)
            if shutdown is not None and not shutdown():
                return
            self._wizard.reject()
            return
        self._quit_timer.stop()
        if self.tray:
            self.tray.hide()
        self.close()
        QApplication.quit()
