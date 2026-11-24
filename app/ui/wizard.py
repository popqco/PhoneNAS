from __future__ import annotations

import ntpath
from dataclasses import dataclass, replace
from typing import cast

from PySide6.QtCore import QEvent, QObject, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app import constants as C
from app.core import winfsp
from app.core.config import Config
from app.core.history import validate_local_history_path
from app.core.tasks import (
    MAX_INTERVAL_MINUTES, MAX_RETENTION_DAYS, MIN_INTERVAL_MINUTES, MIN_RETENTION_DAYS,
    MirrorTask, new_task, validate_source, validate_tasks,
)
from app.ui.pages.settings import (
    _AsyncOperations,
    _ConnectionInput,
    _ConnectionResult,
    _OperationThread,
    _ServerCertificateInput,
    _card,
    _feedback,
    _field,
    _operational,
    _scroll,
    _test_connection,
    _write_connection,
)
from app.ui.task_dialog import MultiFolderDialog
from app.ui.widgets import button, label


@dataclass(frozen=True)
class _WizardInput:
    connection: _ConnectionInput
    drive: str
    mount_enabled: bool
    auto_mount: bool
    autostart: bool
    tasks: tuple[MirrorTask, ...]


class _WizardJobs(QObject):
    """Retain wizard jobs independently of the dialog's QObject lifetime."""

    def __init__(self, application: QApplication):
        super().__init__(application)
        self.workers: set[_OperationThread] = set()
        application.aboutToQuit.connect(self.shutdown)

    def create(self, operation) -> _OperationThread:
        worker = _OperationThread(operation, self)
        self.workers.add(worker)
        worker.finished.connect(self._reap)
        return worker

    @Slot()
    def _reap(self) -> None:
        worker = self.sender()
        if worker in self.workers:
            worker.wait()
            self.workers.remove(worker)
            worker.deleteLater()

    @Slot()
    def shutdown(self) -> None:
        for worker in tuple(self.workers):
            worker.cancel()
        for worker in tuple(self.workers):
            worker.wait()
            self.workers.discard(worker)
            worker.deleteLater()


def _wizard_jobs() -> _WizardJobs:
    application = QApplication.instance()
    if application is None:
        raise RuntimeError("连接向导需要 QApplication。")
    registry = getattr(application, "_phonenas_wizard_jobs", None)
    if registry is None:
        registry = _WizardJobs(application)
        application._phonenas_wizard_jobs = registry
    return registry


class WizardDialog(QDialog, _AsyncOperations):
    """First connection, with optional independent folder tasks and their schedules."""

    operation_started = Signal()
    operation_finished = Signal()

    def __init__(self, config: Config, parent=None, operational: bool = True):
        super().__init__(parent)
        self.config = config
        self.operational = operational
        self._init_operations()
        self._pending_tasks: list[MirrorTask] = []
        self._winfsp_installed: bool | None = None
        self._winfsp_checked = False
        self._dependency_confirmed = False
        self._accepted_pending = False
        self._reject_pending = False
        self._missing_dependency_pending = False
        self.setWindowTitle("连接你的手机 NAS")
        available = self.screen().availableGeometry()
        width, height = max(240, available.width() - 32), max(220, available.height() - 48)
        self.setMinimumSize(min(540, width), min(380, height))
        self.resize(min(740, width), min(720, height))

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(16)
        heading = label("把手机变成随身文件空间。", "dialogTitle")
        heading.setWordWrap(True)
        root.addWidget(heading)
        intro = label("先连接 WebDAV，再选择用途。上传发布可添加本地来源；下载接收需在完成连接设置后选择手机已有数据集。", "subtitle")
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.safe_label = label("安全预览模式 · 不会连接网络、保存配置或更改系统。", "bodyMuted")
        self.safe_label.setWordWrap(True)
        root.addWidget(self.safe_label)
        self.load_error_label = label("", "error")
        self.load_error_label.setWordWrap(True)
        root.addWidget(self.load_error_label)

        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 4, 0)
        body.setSpacing(16)
        connection, connection_layout = _card("01  连接手机 WebDAV", "填写手机端 WebDAV 显示的地址。可先测试，测试不会保存账号或更改挂载连接。")
        self.url_edit = QLineEdit(config.url)
        self.url_edit.setPlaceholderText("https://手机地址:端口/路径")
        self.user_edit = QLineEdit(config.username)
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.setPlaceholderText("留空则沿用已保存的密码")
        connection_layout.addLayout(_field("WebDAV 地址", self.url_edit))
        credentials = QHBoxLayout()
        credentials.setSpacing(16)
        credentials.addLayout(_field("用户名", self.user_edit), 1)
        credentials.addLayout(_field("密码", self.pass_edit), 1)
        connection_layout.addLayout(credentials)
        self.certificate_input = _ServerCertificateInput(self.url_edit, config)
        connection_layout.addWidget(self.certificate_input)
        test_row = QHBoxLayout()
        self.test_button = button("测试连接", glyph="link")
        self.test_label = label("尚未测试当前输入", "muted")
        self.test_label.setWordWrap(True)
        self.test_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cancel_test_button = button("取消测试", "subtle")
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.test_label, 1)
        test_row.addWidget(self.cancel_test_button)
        connection_layout.addLayout(test_row)
        body.addWidget(connection)

        mount, mount_layout = _card("02  网络盘（可选）", "WinFsp 只用于把手机存储挂载为盘符；未安装也能先保存连接和同步任务。")
        mount_row = QHBoxLayout()
        self.drive_combo = QComboBox()
        self.drive_combo.addItems(C.DRIVE_LETTERS)
        self.drive_combo.setCurrentText(config.get("mount.drive", C.DEFAULT_DRIVE))
        mount_row.addLayout(_field("Windows 盘符", self.drive_combo))
        mount_row.addStretch(1)
        mount_layout.addLayout(mount_row)
        self.mount_check = QCheckBox("启用网络盘挂载")
        self.mount_check.setChecked(bool(config.get("mount.enabled", True)))
        self.auto_mount_check = QCheckBox("程序启动时自动挂载")
        self.auto_mount_check.setChecked(bool(config.get("mount.auto_mount", True)))
        mount_layout.addWidget(self.mount_check)
        mount_layout.addWidget(self.auto_mount_check)
        dependency_row = QHBoxLayout()
        self.winfsp_label = label("WinFsp · 尚未检测", "bodyMuted")
        self.winfsp_label.setWordWrap(True)
        self.winfsp_check_button = button("重新检测", "subtle")
        self.winfsp_button = button("安装 WinFsp", glyph="disk")
        dependency_row.addWidget(self.winfsp_label, 1)
        dependency_row.addWidget(self.winfsp_check_button)
        dependency_row.addWidget(self.winfsp_button)
        mount_layout.addLayout(dependency_row)
        body.addWidget(mount)

        folders, folder_layout = _card("03  选择用途与文件夹（可稍后添加）", "先确定这台电脑的用途，不是自动双向同步。上传发布以本地为来源，下载接收以手机镜像为来源。")
        self.direction_combo = QComboBox()
        self.direction_combo.setObjectName("wizardPurpose")
        self.direction_combo.addItem("请选择用途（也可稍后添加任务）", "")
        self.direction_combo.addItem("上传发布", "upload")
        self.direction_combo.addItem("下载接收", "download")
        folder_layout.addLayout(_field("这台电脑的用途", self.direction_combo))
        self.receive_hint = label("完成连接设置后，请在主窗口添加“下载接收”任务，并显式选择手机上已有的数据集镜像和本地接收目录。这里不会创建空的手机目录，也不会自动交回本地修改。", "bodyMuted")
        self.receive_hint.setWordWrap(True)
        self.receive_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.receive_hint.setVisible(False)
        folder_layout.addWidget(self.receive_hint)
        folder_actions = QHBoxLayout()
        self.folder_summary = label("尚未选择文件夹 · 可以直接完成连接设置", "bodyMuted")
        self.folder_summary.setWordWrap(True)
        self.add_folders_button = button("选择多个文件夹", glyph="plus")
        folder_actions.addWidget(self.folder_summary, 1)
        folder_actions.addWidget(self.add_folders_button)
        folder_layout.addLayout(folder_actions)
        self.folder_list = QListWidget()
        self.folder_list.setMinimumHeight(100)
        self.folder_list.setMaximumHeight(210)
        self.folder_list.setWordWrap(True)
        self.folder_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        folder_layout.addWidget(self.folder_list)
        self.remove_folder_button = button("移除所选", "subtle")
        remove_row = QHBoxLayout()
        remove_row.addWidget(self.remove_folder_button)
        remove_row.addStretch(1)
        folder_layout.addLayout(remove_row)
        defaults = label("新增任务的默认策略", "field")
        folder_layout.addWidget(defaults)
        policy = QGridLayout()
        policy.setHorizontalSpacing(18)
        policy.setVerticalSpacing(12)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES)
        self.interval_spin.setValue(20)
        self.interval_spin.setSuffix(" 分钟")
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(MIN_RETENTION_DAYS, MAX_RETENTION_DAYS)
        self.retention_spin.setValue(30)
        self.retention_spin.setSuffix(" 天")
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("推荐 · 排除构建产物，保留 Release", "recommended")
        self.preset_combo.addItem("不排除 · 同步所有文件", "none")
        policy.addLayout(_field("同步间隔", self.interval_spin), 0, 0)
        policy.addLayout(_field("旧文件保留", self.retention_spin), 0, 1)
        policy.addLayout(_field("排除规则", self.preset_combo), 1, 0, 1, 2)
        policy.setColumnStretch(0, 1)
        policy.setColumnStretch(1, 1)
        folder_layout.addLayout(policy)
        self.enable_tasks_check = QCheckBox("启用新增上传任务（允许手动执行）")
        self.enable_tasks_check.setChecked(False)
        folder_layout.addWidget(self.enable_tasks_check)
        self.auto_sync_check = QCheckBox("按计划自动执行新增上传任务")
        self.auto_sync_check.setObjectName("wizardAutoSync")
        self.auto_sync_check.setChecked(False)
        folder_layout.addWidget(self.auto_sync_check)
        folder_note = label("默认间隔 20 分钟 / 保留 30 天，可在此调整。已有任务不会改变；手机镜像、手机历史和数据目录外的本地历史相互隔离。新增任务默认停用且不开启自动计划，请先查看差异；仅启用任务不会开启自动执行。", "bodyMuted")
        folder_note.setWordWrap(True)
        folder_layout.addWidget(folder_note)
        self.existing_tasks_label = label("", "small")
        self.existing_tasks_label.setWordWrap(True)
        folder_layout.addWidget(self.existing_tasks_label)
        body.addWidget(folders)

        self.autostart_check = QCheckBox("开机自动启动 PhoneNAS")
        self.autostart_check.setChecked(bool(config.get("app.start_with_windows", True)))
        body.addWidget(self.autostart_check)
        body.addStretch(1)
        root.addWidget(_scroll(content), 1)
        self.notice_label = label("向导仅保存连接与任务，不传输数据。主窗口随后应用挂载与计划；新增任务默认停用且不开启自动计划。请先查看差异；仅主动启用并勾选自动执行后，才可能立即按计划排队。", "bodyMuted")
        self.notice_label.setWordWrap(True)
        self.notice_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.notice_label)
        actions = QHBoxLayout()
        self.cancel_button = button("稍后设置")
        self.finish_button = button("完成设置", "primary", "check")
        self.finish_button.setDefault(True)
        self.finish_button.setAutoDefault(False)
        self.cancel_button.setMinimumHeight(38)
        self.finish_button.setMinimumHeight(38)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        actions.addWidget(self.finish_button)
        root.addLayout(actions)

        self._editors = (
            self.url_edit, self.user_edit, self.pass_edit, self.certificate_input, self.drive_combo,
            self.mount_check, self.auto_mount_check, self.autostart_check,
            self.direction_combo, self.folder_list, self.interval_spin, self.retention_spin,
            self.preset_combo, self.enable_tasks_check, self.auto_sync_check,
        )
        self.finish_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self.test_button.clicked.connect(self._test)
        self.cancel_test_button.clicked.connect(self._cancel_test)
        self.winfsp_check_button.clicked.connect(self._refresh_winfsp)
        self.winfsp_button.clicked.connect(self._install_winfsp)
        self.add_folders_button.clicked.connect(self._browse)
        self.direction_combo.currentIndexChanged.connect(self._purpose_changed)
        self.auto_sync_check.toggled.connect(self._refresh_controls)
        self.remove_folder_button.clicked.connect(self._remove_folder)
        self.folder_list.currentRowChanged.connect(self._refresh_controls)
        for edit in (self.url_edit, self.user_edit, self.pass_edit):
            edit.textChanged.connect(self._connection_edited)
        self.certificate_input.changed.connect(self._connection_edited)
        for signal in (self.interval_spin.valueChanged, self.retention_spin.valueChanged, self.preset_combo.currentIndexChanged):
            signal.connect(self._refresh_folders)
        self._refresh_folders()

    def _start_operation(self, name, operation, completion) -> None:
        worker = _wizard_jobs().create(operation)
        self._worker = worker
        self._completion = completion
        self._operation_name = name
        self.destroyed.connect(worker.cancel)
        worker.finished.connect(self._operation_ended)
        self._refresh_controls()
        self.operation_started.emit()
        worker.start()

    @Slot()
    def _operation_ended(self) -> None:
        # Register the mixin callback as a QObject slot so deleting this dialog
        # disconnects it even while the application-owned worker is finishing.
        super()._operation_ended()

    def _connection_input(self) -> _ConnectionInput:
        return _ConnectionInput(
            self.certificate_input.endpoint, self.user_edit.text().strip(), self.pass_edit.text(),
            self.certificate_input.certificate,
        )

    def _connection_edited(self) -> None:
        if not self.busy:
            _feedback(self.test_label, "尚未测试当前输入")

    def _refresh_controls(self, *_args) -> None:
        editable = not self.busy and not self._shutting_down
        allowed = editable and not self._sync_busy and _operational(self)
        for editor in self._editors:
            editor.setEnabled(editable)
        upload = self.direction_combo.currentData() == "upload"
        self.add_folders_button.setEnabled(editable and upload)
        self.remove_folder_button.setEnabled(editable and upload and self.folder_list.currentRow() >= 0)
        for editor in (self.retention_spin, self.preset_combo, self.enable_tasks_check, self.auto_sync_check):
            editor.setEnabled(editable and upload)
        self.interval_spin.setEnabled(editable and upload and self.auto_sync_check.isChecked())
        self.test_button.setEnabled(allowed)
        self.winfsp_check_button.setEnabled(allowed)
        self.winfsp_button.setEnabled(allowed and self._winfsp_installed is not True)
        self.finish_button.setEnabled(allowed and not self.config.load_error)
        self.finish_button.setText("正在保存…" if self._operation_name == "save" else "完成设置")
        self.cancel_test_button.setVisible(self.busy and self._operation_name == "test")
        self.cancel_test_button.setEnabled(not self._shutting_down)
        self.cancel_button.setText("正在结束操作…" if self._reject_pending else "稍后设置")
        self.cancel_button.setEnabled(not self._reject_pending)
        self.safe_label.setVisible(not _operational(self))
        error = self.config.load_error
        self.load_error_label.setText(f"原配置读取失败，为防止覆盖不能在向导中保存。请在设置页导入合法配置恢复。\n{error}" if error else "")
        self.load_error_label.setVisible(bool(error))

    def _purpose_changed(self, *_args) -> None:
        self._refresh_folders()
        if self.direction_combo.currentData() == "download":
            _feedback(self.notice_label, "完成连接设置后，在主窗口添加“下载接收”任务，选择手机上已有的数据集和本地接收目录。向导不会建立空的远端或上传任务。")
        else:
            _feedback(self.notice_label, "向导仅保存配置，不传输数据。新上传任务默认停用；完成后请先查看差异，再启用任务。")

    def _refresh_folders(self, *_args) -> None:
        row = self.folder_list.currentRow()
        self.folder_list.clear()
        for task in self._pending_tasks:
            self.folder_list.addItem(f"{task.name}\n本地来源：{task.local_path}\n手机镜像：{task.remote_path}\n手机历史：{task.versions_path}\n本地历史：{task.local_history_path}")
        if 0 <= row < self.folder_list.count():
            self.folder_list.setCurrentRow(row)
        count = len(self._pending_tasks)
        purpose = self.direction_combo.currentData()
        upload = purpose == "upload"
        self.folder_summary.setText(
            f"将添加 {count} 个上传发布任务" if upload and count else
            "尚未选择来源文件夹 · 可以直接完成连接设置" if upload else
            "下载接收将在连接保存后添加；本页不创建远端目录或接收任务。" if purpose == "download" else
            "请先选择用途，或直接完成连接设置，稍后添加任务。"
        )
        self.receive_hint.setVisible(purpose == "download")
        self.folder_list.setVisible(upload and bool(count))
        self.remove_folder_button.setVisible(upload and bool(count))
        self.existing_tasks_label.setText(f"已配置 {len(self.config.tasks)} 个任务，将原样保留。" if self.config.tasks else "")
        self.existing_tasks_label.setVisible(bool(self.config.tasks))
        self._refresh_controls()

    def _browse(self) -> None:
        if self.busy or self._shutting_down or self.direction_combo.currentData() != "upload":
            return
        dialog = MultiFolderDialog(self, staging=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        folders = tuple(dialog.folders)
        dialog.deleteLater()
        # Creating task IDs and rendering strings is in-memory only. All path
        # resolution, source I/O, and cross-task checks happen in the save job.
        known = {ntpath.normcase(ntpath.normpath(task.local_path)) for task in (*self.config.tasks, *self._pending_tasks)}
        skipped = 0
        for path in folders:
            local = str(path).strip()
            if not local:
                continue
            key = ntpath.normcase(ntpath.normpath(local))
            if key in known:
                skipped += 1
                continue
            known.add(key)
            self._pending_tasks.append(new_task(local))
        self._refresh_folders()
        _feedback(self.notice_label, "所选文件夹已加入待添加清单。保存时会校验来源和目标，尚未创建任务。" + (f"已跳过 {skipped} 个重复文件夹。" if skipped else ""))

    def _remove_folder(self) -> None:
        if self.busy or self._shutting_down:
            return
        row = self.folder_list.currentRow()
        if 0 <= row < len(self._pending_tasks):
            del self._pending_tasks[row]
            self._refresh_folders()

    def _test(self) -> None:
        if not self._guard_operation():
            return
        request = self._connection_input()
        snapshot = self.config.snapshot()
        _feedback(self.test_label, "正在连接…请等待本次测试结束")
        self._start_operation("test", lambda worker: _test_connection(worker, snapshot, request), self._tested)

    def _tested(self, worker: _OperationThread) -> None:
        cancelled = worker.cancelled or worker.cancel_event.is_set()
        if worker.error or cancelled:
            _feedback(self.test_label, "已取消连接测试" if cancelled else f"连接失败：{worker.error}", "muted" if cancelled else "error")
            return
        result = cast(_ConnectionResult, worker.result)
        _feedback(self.test_label, result.message if result.ok else f"连接失败：{result.message}", "good" if result.ok else "error")

    def _cancel_test(self) -> None:
        if self._worker is not None and self._operation_name == "test":
            self._worker.cancel()
            _feedback(self.test_label, "正在取消连接测试…")
            self.cancel_test_button.setEnabled(False)

    def _refresh_winfsp(self) -> None:
        if not self._guard_operation():
            return
        _feedback(self.winfsp_label, "WinFsp · 正在检测…")
        self._start_operation("probe", lambda worker: winfsp.is_installed(), self._winfsp_probed)

    def _winfsp_probed(self, worker: _OperationThread) -> None:
        if worker.error or worker.cancelled:
            _feedback(self.winfsp_label, f"WinFsp · 检测失败：{worker.error}" if worker.error else "WinFsp · 检测已取消", "error" if worker.error else "muted")
            return
        self._winfsp_installed = bool(worker.result)
        self._winfsp_checked = True
        _feedback(self.winfsp_label, "WinFsp · 已安装" if self._winfsp_installed else "WinFsp · 未安装（可稍后安装）", "good" if self._winfsp_installed else "bodyMuted")

    def _install_winfsp(self) -> None:
        if not self._guard_operation():
            return
        answer = QMessageBox.question(
            self, "安装 WinFsp", "将启动 WinFsp 安装程序，并请求 Windows 管理员授权。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes or not self._guard_operation():
            return
        _feedback(self.winfsp_label, "正在安装 WinFsp…请完成 Windows 授权，安装结束前请勿退出。")
        def install(worker: _OperationThread):
            worker.begin_commit()
            return winfsp.install()

        self._start_operation("install", install, self._winfsp_installed_result)

    def _winfsp_installed_result(self, worker: _OperationThread) -> None:
        if worker.error or worker.cancelled:
            _feedback(self.winfsp_label, f"WinFsp 安装失败：{worker.error}" if worker.error else "安装操作已结束，请重新检测 WinFsp 状态。", "error" if worker.error else "muted")
            self._winfsp_checked = False
            return
        ok, message = worker.result
        self._winfsp_installed = True if ok else None
        self._winfsp_checked = bool(ok)
        _feedback(self.winfsp_label, message, "good" if ok else "error")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._refresh_controls()

    def event(self, event) -> bool:
        result = super().event(event)
        if event.type() == QEvent.Type.DynamicPropertyChange and hasattr(self, "_editors"):
            self._refresh_controls()
        return result

    def accept(self) -> None:
        if not self._guard_operation():
            return
        if self.config.load_error:
            _feedback(self.notice_label, "原配置读取失败，请先在设置页导入合法配置恢复。", "error")
            return
        preset = self.preset_combo.currentData()
        tasks = tuple(replace(
            task,
            interval_minutes=self.interval_spin.value(),
            retention_days=self.retention_spin.value(),
            exclude_preset=preset,
            excludes=tuple(C.RECOMMENDED_EXCLUDES) if preset == "recommended" else (),
            enabled=self.enable_tasks_check.isChecked(),
            auto_sync=self.auto_sync_check.isChecked(),
        ) for task in self._pending_tasks) if self.direction_combo.currentData() == "upload" else ()
        request = _WizardInput(
            self._connection_input(), self.drive_combo.currentText(),
            self.mount_check.isChecked(), self.auto_mount_check.isChecked(),
            self.autostart_check.isChecked(), tasks,
        )
        draft = self.config.snapshot()
        allow_missing_winfsp = self._dependency_confirmed

        def save_wizard(worker: _OperationThread):
            worker._secrets.append(request.connection.password)
            _write_connection(draft, request.connection)
            draft.set("mount.drive", request.drive)
            draft.set("mount.enabled", request.mount_enabled)
            draft.set("mount.auto_mount", request.auto_mount)
            draft.set("app.start_with_windows", request.autostart)
            combined = (*draft.tasks, *request.tasks)
            validate_tasks(combined, request.drive)
            for task in request.tasks:
                worker.check_cancelled()
                validate_source(task, request.drive)
                validate_local_history_path(task.local_history_path)
            draft.replace_tasks(combined)
            worker.check_cancelled()
            installed = winfsp.is_installed() if request.mount_enabled else None
            if request.mount_enabled and not installed and not allow_missing_winfsp:
                return None, installed
            worker.begin_commit()
            draft.save()
            worker.committed = True
            return draft, installed

        _feedback(self.notice_label, "正在校验文件夹、任务隔离与连接设置…")
        self._start_operation("save", save_wizard, self._saved)

    def _saved(self, worker: _OperationThread) -> None:
        if worker.error:
            _feedback(self.notice_label, f"无法完成设置：{worker.error}", "error")
            return
        if worker.cancelled:
            _feedback(self.notice_label, "已取消保存，未应用向导设置。")
            return
        draft, installed = worker.result
        if installed is not None:
            self._winfsp_installed = bool(installed)
            self._winfsp_checked = True
            _feedback(self.winfsp_label, "WinFsp · 已安装" if installed else "WinFsp · 未安装（可稍后安装）", "good" if installed else "bodyMuted")
        if draft is None:
            # Ask only after the job has finished and released its gate.
            self._missing_dependency_pending = True
            return
        self.config.apply_snapshot(cast(Config, draft))
        self.pass_edit.clear()
        self._accepted_pending = True

    def _after_operation_ended(self) -> None:
        if self._accepted_pending:
            self._accepted_pending = False
            self._shutting_down = True
            super().accept()
        elif self._reject_pending:
            super().reject()
        elif self._missing_dependency_pending and not self._shutting_down:
            self._missing_dependency_pending = False
            QTimer.singleShot(0, self._confirm_missing_dependency)

    def _confirm_missing_dependency(self) -> None:
        if not self._guard_operation():
            return
        answer = QMessageBox.question(
            self, "稍后安装 WinFsp？", "WinFsp 尚未安装，网络盘暂时无法挂载。可以先保存连接和任务，稍后在设置页安装。仍要完成设置吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._dependency_confirmed = True
            self.accept()
        else:
            _feedback(self.notice_label, "可以安装 WinFsp 后重试，或取消勾选网络盘挂载后仅保存连接和任务。")

    def reject(self) -> None:
        if self.busy:
            self._reject_pending = True
            self.shutdown()
            return
        self._shutting_down = True
        super().reject()

    def done(self, result: int) -> None:
        if self.busy:
            if result != QDialog.DialogCode.Accepted:
                self.reject()
            return
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.busy:
            self.reject()
            event.ignore()
        else:
            self._shutting_down = True
            super().closeEvent(event)
