from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app import constants as C
from app.core import winfsp
from app.core.autostart import set_autostart
from app.core.config import Config
from app.core.nas_tls import read_nas_trust
from app.core.rclone import Rclone
from app.core.tasks import validate_tasks
from app.ui.widgets import Card, button, label


@dataclass(frozen=True)
class _ConnectionInput:
    url: str
    username: str
    password: str
    certificate: str = ""
    fingerprint: str = ""
    use_phone_certificate: bool = False


@dataclass(frozen=True)
class _PreferencesInput:
    connection: _ConnectionInput
    drive: str
    mount_enabled: bool
    network_mode: bool
    auto_mount: bool
    autostart: bool
    minimize_to_tray: bool
    mirror_enabled: bool
    read_only: bool = True


@dataclass(frozen=True)
class _ConnectionResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class _SavedSettings:
    config: Config
    autostart_message: str = ""
    autostart_ok: bool = True


class _OperationCancelled(Exception):
    pass


class _OperationThread(QThread):
    """One job, no widgets. The owner retains this object until finished."""

    def __init__(self, operation: Callable[[_OperationThread], Any], parent=None):
        super().__init__(parent)
        self._operation = operation
        self.cancel_event = threading.Event()
        self._client: Rclone | None = None
        self._client_lock = threading.Lock()
        self._secrets: list[str] = []
        self._commit_lock = threading.Lock()
        self._cancelable = True
        self.result: Any = None
        self.error = ""
        self.cancelled = False
        self.committed = False

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set() or self.isInterruptionRequested():
            raise _OperationCancelled()

    def attach_client(self, client: Rclone) -> None:
        with self._client_lock:
            self._client = client
            cancelled = self.cancel_event.is_set()
        if cancelled:
            client.cancel()
            self.check_cancelled()

    def redact(self, message: object) -> str:
        text = str(message).strip()
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, "[密码已隐藏]")
        return text

    def begin_commit(self) -> None:
        # Once persistence or an external installer starts, wait for its result.
        # Never report cancellation after an irreversible operation succeeded.
        with self._commit_lock:
            self.check_cancelled()
            self._cancelable = False

    def cancel(self) -> None:
        with self._commit_lock:
            if not self._cancelable:
                return
            self.cancel_event.set()
            self.requestInterruption()
        with self._client_lock:
            client = self._client
        if client is not None:
            client.cancel()

    def run(self) -> None:
        try:
            self.check_cancelled()
            self.result = self._operation(self)
            # A cancelled connection must never become a green success. A save
            # already committed to disk must still be applied to the live model.
            if self.cancel_event.is_set() and not self.committed:
                self.result = None
                self.cancelled = True
        except _OperationCancelled:
            self.cancelled = True
        except Exception as exc:  # noqa: BLE001
            self.error = self.redact(exc) or type(exc).__name__
        finally:
            self._operation = None  # type: ignore[assignment]
            self._secrets.clear()
            with self._client_lock:
                self._client = None


def _operational(widget: QWidget) -> bool:
    """Also honour the main window's dynamic safe-mode property."""
    current = widget
    while current is not None:
        if getattr(current, "operational", True) is False:
            return False
        if current.property("safeMode") or current.property("safe_mode"):
            return False
        if current.property("operational") is False:
            return False
        current = current.parent()  # type: ignore[assignment]
    return True


def _feedback(target: QLabel, text: str, role: str = "muted") -> None:
    target.setText(text)
    target.setProperty("role", role)
    target.style().unpolish(target)
    target.style().polish(target)


def _card(title: str, subtitle: str = "") -> tuple[Card, QVBoxLayout]:
    card = Card()
    layout = QVBoxLayout(card)
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(14)
    layout.addWidget(label(title, "heading"))
    if subtitle:
        description = label(subtitle, "bodyMuted")
        description.setWordWrap(True)
        layout.addWidget(description)
    return card, layout


def _field(text: str, edit: QWidget) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setSpacing(7)
    caption = label(text, "field")
    caption.setBuddy(edit)
    layout.addWidget(caption)
    edit.setMinimumHeight(38)
    layout.addWidget(edit)
    return layout


def _scroll(content: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setObjectName("pageScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(content)
    return scroll


class _PhoneCertificateFields(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pem = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.enabled_check = QCheckBox("使用 PhoneNAS 手机端的专用 HTTPS 证书")
        self.enabled_check.setObjectName("nasUsePhoneCertificate")
        layout.addWidget(self.enabled_check)
        self.fields = QWidget()
        form = QVBoxLayout(self.fields)
        form.setContentsMargins(0, 0, 0, 0)
        hint = label("导入手机服务端导出的公开 PEM，并与手机页面独立核对 SHA-256 指纹。不会安装到系统信任。", "small")
        hint.setWordWrap(True)
        form.addWidget(hint)
        row = QHBoxLayout()
        self.pick = button("导入公开证书", "subtle")
        self.pick.setObjectName("nasImportPublicCertificate")
        self.status = label("尚未导入", "small")
        self.status.setWordWrap(True)
        row.addWidget(self.pick)
        row.addWidget(self.status, 1)
        form.addLayout(row)
        self.fingerprint = QLineEdit()
        self.fingerprint.setObjectName("nasCertificateSha256")
        self.fingerprint.setMaxLength(100)
        form.addLayout(_field("SHA-256 指纹", self.fingerprint))
        layout.addWidget(self.fields)
        self.fields.setVisible(False)
        self.enabled_check.toggled.connect(self.fields.setVisible)
        self.enabled_check.toggled.connect(self.changed)
        self.fingerprint.textChanged.connect(self.changed)
        self.pick.clicked.connect(self._pick)

    def load(self, config: Config) -> None:
        self._pem = config.get("nas.tls_certificate", "")
        self.fingerprint.setText(config.get("nas.tls_fingerprint", ""))
        self.status.setText("已导入公开证书" if self._pem else "尚未导入")
        self.enabled_check.setChecked(config.get("version") == 4)

    def apply_to(self, request: _ConnectionInput) -> _ConnectionInput:
        if not self.enabled_check.isChecked():
            return request
        return replace(request, certificate=self._pem, fingerprint=self.fingerprint.text(), use_phone_certificate=True)

    def _pick(self) -> None:
        if not _operational(self):
            return
        name, _ = QFileDialog.getOpenFileName(self, "导入 PhoneNAS 公开证书", "", "PEM 证书 (*.pem *.cer *.crt);;所有文件 (*)")
        if not name:
            return
        try:
            path = Path(name)
            if not path.is_file():
                raise ValueError()
            with path.open("rb") as stream:
                data = stream.read(16 * 1024 + 1)
            if len(data) > 16 * 1024 or b"PRIVATE KEY" in data:
                raise ValueError()
            self._pem = data.decode("ascii")
        except (OSError, ValueError):
            self._pem = ""
            self.status.setText("无法读取公开 PEM；请检查文件后重选")
        else:
            self.status.setText("已读取，请核对手机上的完整指纹")
        self.changed.emit()


def _validate_connection(request: _ConnectionInput) -> None:
    if any(ord(char) < 32 for char in request.url + request.username + request.password):
        raise ValueError("地址、用户名和密码不能包含换行或控制字符")
    try:
        parsed = urlsplit(request.url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"WebDAV 地址无效：{exc}") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("请输入完整的 WebDAV 地址，以 http:// 或 https:// 开头并包含主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("请在下方填写账号和密码，不要把凭据放进 WebDAV 地址")
    if parsed.query or parsed.fragment or any(char.isspace() for char in request.url):
        raise ValueError("WebDAV 地址不能包含空白、查询参数或片段")
    if port is not None and port < 1:
        raise ValueError("WebDAV 端口必须为 1–65535")


def _apply_connection(config: Config, request: _ConnectionInput, *, allow_unconfigured: bool = False) -> bool:
    if allow_unconfigured and not request.url and not config.url and not request.use_phone_certificate:
        return False  # Save unrelated preferences before the first connection.
    _validate_connection(request)
    previous = (config.url, config.username, config.get("nas.tls_fingerprint", ""))
    trust = None
    if request.use_phone_certificate:
        trust = read_nas_trust(request.url, request.certificate, request.fingerprint)
        if trust is None:
            raise ValueError("请先导入公开证书并填写核对后的指纹")
    fingerprint = trust.fingerprint if trust is not None else ""
    identity = (request.url, request.username, fingerprint)
    changed_target = previous != identity
    if changed_target and (previous[2] or fingerprint) and not request.password:
        raise ValueError("更换配对身份或取消证书绑定时，请重新输入密码")
    if trust is not None and (not request.username or ":" in request.username or
                              not (request.password or config.get("nas.password_enc"))):
        raise ValueError("请输入 PhoneNAS 用户名和密码")
    config.set("nas.url", request.url)
    config.set("nas.username", request.username)
    config.set("nas.tls_certificate", trust.pem if trust is not None else "")
    config.set("nas.tls_fingerprint", fingerprint)
    config.set("version", 4 if trust is not None else 3)
    if request.password:
        config.password = request.password
    return changed_target


def _write_connection(config: Config, request: _ConnectionInput, *, allow_unconfigured: bool = False) -> None:
    if _apply_connection(config, request, allow_unconfigured=allow_unconfigured):
        config.replace_tasks(
            replace(task, last_sync_at="", last_file_count=0, sync_identity=uuid.uuid4().hex)
            for task in config.tasks
        )


def _test_connection(
    worker: _OperationThread, snapshot: Config, request: _ConnectionInput
) -> _ConnectionResult:
    worker._secrets.append(request.password)
    worker.check_cancelled()
    _apply_connection(snapshot, request)
    password = request.password or snapshot.password
    worker._secrets.append(password)
    client = Rclone(snapshot)
    worker.attach_client(client)
    # Neither testing nor saving touches the mounted client's shared conf.
    with client.isolated_conf():
        worker.check_cancelled()
        result = client.test_connection(
            request.url, request.username, password, cancel_event=worker.cancel_event
        )
    worker.check_cancelled()
    if result.ok:
        return _ConnectionResult(True, "连接成功 · 已验证 WebDAV，输入尚未保存")
    detail = result.err or result.out or f"rclone 退出码 {result.code}"
    return _ConnectionResult(False, worker.redact(detail))


class _AsyncOperations:
    """Shared by settings and the wizard; busy describes only their own job."""

    def _init_operations(self) -> None:
        self._worker: _OperationThread | None = None
        self._completion: Callable[[_OperationThread], None] | None = None
        self._operation_name = ""
        self._sync_busy = False
        self._shutting_down = False

    @property
    def busy(self) -> bool:
        return self._worker is not None

    def set_sync_busy(self, busy: bool) -> None:
        self._sync_busy = bool(busy)
        self._refresh_controls()

    def _guard_operation(self) -> bool:
        if self._shutting_down or self.busy:
            return False
        if not _operational(self):
            _feedback(self.notice_label, "安全预览模式：不会保存配置、连接网络或更改系统。")
            self._refresh_controls()
            return False
        if self._sync_busy:
            if hasattr(self, "busy_hint"):
                # The dedicated hint clears as soon as the global gate opens;
                # never replace a saved error/result with a transient busy notice.
                self._refresh_controls()
            else:
                _feedback(self.notice_label, "同步或历史操作进行中，请在完成后再操作。")
            return False
        return True

    def _start_operation(
        self,
        name: str,
        operation: Callable[[_OperationThread], Any],
        completion: Callable[[_OperationThread], None],
    ) -> None:
        worker = _OperationThread(operation, self)
        self._worker = worker
        self._completion = completion
        self._operation_name = name
        worker.finished.connect(self._operation_ended)
        self._refresh_controls()
        self.operation_started.emit()
        worker.start()

    @Slot()
    def _operation_ended(self) -> None:
        worker = self._worker
        if worker is None:
            return
        try:
            if self._completion is not None:
                self._completion(worker)
        finally:
            self._completion = None
            self._worker = None
            self._operation_name = ""
            worker.deleteLater()
            self._refresh_controls()
            self.operation_finished.emit()
            self._after_operation_ended()

    def _after_operation_ended(self) -> None:
        pass

    def shutdown(self) -> bool:
        """Request cooperative cancellation. False means retain the UI and retry."""
        self._shutting_down = True
        if self._worker is not None:
            self._worker.cancel()
            _feedback(self.notice_label, "正在结束后台操作，请稍候…")
            self._refresh_controls()
            return False
        self._refresh_controls()
        return True


class SettingsPage(QWidget, _AsyncOperations):
    saved = Signal()
    operation_started = Signal()
    operation_finished = Signal()

    def __init__(self, config: Config, parent=None, operational: bool = True):
        super().__init__(parent)
        self.config = config
        self.operational = operational
        self._init_operations()
        self._winfsp_installed: bool | None = None
        self._winfsp_checked = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 9, 20)
        root.setSpacing(18)
        root.addWidget(label("简单的设置，清楚的边界。", "pageTitle"))
        subtitle = label("连接与挂载是全局设置；同步计划、排除规则与历史保留属于每个文件夹。", "subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)
        self.load_error_label = label("", "error")
        self.load_error_label.setWordWrap(True)
        root.addWidget(self.load_error_label)
        self.safe_label = label("安全预览模式 · 不保存配置、不访问网络、不安装或修改系统。", "bodyMuted")
        self.safe_label.setWordWrap(True)
        root.addWidget(self.safe_label)

        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 4, 0)
        body.setSpacing(16)
        connection, connection_layout = _card("NAS 连接", "填写手机 WebDAV 的地址和账号。密码只保存在本机，不随配置导出。")
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("http://手机地址:端口/路径")
        self.user_edit = QLineEdit()
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.setPlaceholderText("留空则沿用已保存的密码")
        connection_layout.addLayout(_field("WebDAV 地址", self.url_edit))
        credentials = QHBoxLayout()
        credentials.setSpacing(16)
        credentials.addLayout(_field("用户名", self.user_edit), 1)
        credentials.addLayout(_field("密码", self.pass_edit), 1)
        connection_layout.addLayout(credentials)
        self.phone_certificate = _PhoneCertificateFields()
        connection_layout.addWidget(self.phone_certificate)
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

        columns = QGridLayout()
        columns.setSpacing(16)
        mount, mount_layout = _card("网络盘挂载", "所有同步任务共用一个 WebDAV 连接，任务不依赖盘符读取。")
        self.drive_combo = QComboBox()
        self.drive_combo.addItems(C.DRIVE_LETTERS)
        mount_layout.addLayout(_field("Windows 盘符", self.drive_combo))
        self.mount_check = QCheckBox("启用网络盘挂载")
        self.read_only_check = QCheckBox("只读挂载（推荐）")
        self.read_only_check.setObjectName("mountReadOnly")
        self.network_check = QCheckBox("以网络驱动器方式挂载")
        self.auto_mount_check = QCheckBox("程序启动时自动挂载")
        for check in (self.mount_check, self.read_only_check, self.network_check, self.auto_mount_check):
            mount_layout.addWidget(check)
        mount_note = label(
            "只读挂载仅限制通过此盘符写入，不改变同步或交回操作。下载接收（订阅端）只是本机任务方向，"
            "不是手机服务器的只读权限；真正的读写权限须在手机 WebDAV 账号端配置。取消只读后可通过盘符改写手机文件，请谨慎操作。",
            "small",
        )
        mount_note.setWordWrap(True)
        mount_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mount_layout.addWidget(mount_note)
        mount_layout.addStretch(1)
        columns.addWidget(mount, 0, 0)
        app_card, app_layout = _card("程序偏好", "保存后生效，不改变任何文件夹的同步策略。")
        self.autostart_check = QCheckBox("开机自动启动 PhoneNAS")
        self.tray_check = QCheckBox("关闭窗口时最小化到托盘")
        self.mirror_enabled_check = QCheckBox("启用文件夹同步（全局）")
        app_layout.addWidget(self.autostart_check)
        app_layout.addWidget(self.tray_check)
        app_layout.addWidget(self.mirror_enabled_check)
        app_layout.addStretch(1)
        columns.addWidget(app_card, 0, 1)
        columns.setColumnStretch(0, 1)
        columns.setColumnStretch(1, 1)
        body.addLayout(columns)

        dependencies, dependency_layout = _card("挂载依赖", "WinFsp 只用于挂载网络盘。安装会请求 Windows 管理员授权，仅在你点击确认后启动。")
        self.winfsp_label = label("WinFsp · 尚未检测", "bodyMuted")
        dependency_layout.addWidget(self.winfsp_label)
        dependency_actions = QHBoxLayout()
        self.winfsp_check_button = button("重新检测", "subtle")
        self.winfsp_button = button("安装 WinFsp", glyph="disk")
        dependency_actions.addWidget(self.winfsp_check_button)
        dependency_actions.addStretch(1)
        dependency_actions.addWidget(self.winfsp_button)
        dependency_layout.addLayout(dependency_actions)
        body.addWidget(dependencies)
        body.addStretch(1)
        root.addWidget(_scroll(content), 1)

        self.notice_label = label("保存只更新配置；网络盘会在同步和历史操作空闲后应用新设置。", "bodyMuted")
        self.notice_label.setWordWrap(True)
        self.notice_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.notice_label)
        self.busy_hint = label("", "bodyMuted")
        self.busy_hint.setObjectName("settingsBusyHint")
        self.busy_hint.setWordWrap(True)
        root.addWidget(self.busy_hint)
        actions = QHBoxLayout()
        self.export_button = button("导出配置", glyph="up")
        self.import_button = button("导入配置", glyph="down")
        self.save_button = button("保存设置", "primary", "check")
        for action in (self.export_button, self.import_button, self.save_button):
            action.setMinimumHeight(38)
        actions.addWidget(self.export_button)
        actions.addWidget(self.import_button)
        actions.addStretch(1)
        actions.addWidget(self.save_button)
        root.addLayout(actions)

        self._editors = (
            self.url_edit, self.user_edit, self.pass_edit, self.phone_certificate, self.drive_combo,
            self.mount_check, self.read_only_check, self.network_check, self.auto_mount_check,
            self.autostart_check, self.tray_check, self.mirror_enabled_check,
        )
        self.save_button.clicked.connect(self.save)
        self.export_button.clicked.connect(self._export)
        self.import_button.clicked.connect(self._import)
        self.test_button.clicked.connect(self._test)
        self.cancel_test_button.clicked.connect(self._cancel_test)
        self.winfsp_check_button.clicked.connect(self._refresh_winfsp)
        self.winfsp_button.clicked.connect(self._install_winfsp)
        for edit in (self.url_edit, self.user_edit, self.pass_edit):
            edit.textChanged.connect(self._connection_edited)
        self.phone_certificate.changed.connect(self._connection_edited)
        self.load()

    def load(self) -> None:
        # Reading the already-loaded model does not decrypt passwords or touch disk.
        self.url_edit.setText(self.config.url)
        self.user_edit.setText(self.config.username)
        self.pass_edit.clear()
        self.phone_certificate.load(self.config)
        self.drive_combo.setCurrentText(self.config.get("mount.drive", C.DEFAULT_DRIVE))
        self.mount_check.setChecked(bool(self.config.get("mount.enabled", True)))
        self.read_only_check.setChecked(bool(self.config.get("mount.read_only", True)))
        self.network_check.setChecked(bool(self.config.get("mount.network_mode", False)))
        self.auto_mount_check.setChecked(bool(self.config.get("mount.auto_mount", True)))
        self.autostart_check.setChecked(bool(self.config.get("app.start_with_windows", True)))
        self.tray_check.setChecked(bool(self.config.get("app.minimize_to_tray", True)))
        self.mirror_enabled_check.setChecked(bool(self.config.get("mirror.enabled", True)))
        error = self.config.load_error
        self.load_error_label.setText(
            f"配置读取失败，已禁止保存覆盖原文件。请导入合法配置恢复；原文件备份由配置模块保留。\n{error}"
            if error else ""
        )
        self.load_error_label.setVisible(bool(error))
        self._refresh_controls()

    def _connection_input(self) -> _ConnectionInput:
        return self.phone_certificate.apply_to(_ConnectionInput(
            self.url_edit.text().strip().rstrip("/"),
            self.user_edit.text().strip(), self.pass_edit.text(),
        ))

    def _connection_edited(self) -> None:
        if not self.busy:
            _feedback(self.test_label, "尚未测试当前输入")

    def _refresh_controls(self) -> None:
        editable = not self.busy and not self._shutting_down
        allowed = editable and not self._sync_busy and _operational(self)
        for editor in self._editors:
            editor.setEnabled(editable)
        self.save_button.setEnabled(allowed and not self.config.load_error)
        self.import_button.setEnabled(allowed)  # A valid import can recover load_error.
        self.export_button.setEnabled(allowed and not self.config.load_error)
        self.test_button.setEnabled(allowed)
        self.winfsp_check_button.setEnabled(allowed)
        self.winfsp_button.setEnabled(allowed and self._winfsp_installed is not True)
        self.cancel_test_button.setVisible(self.busy and self._operation_name == "test")
        self.cancel_test_button.setEnabled(not self._shutting_down)
        self.safe_label.setVisible(not _operational(self))
        if self._shutting_down:
            reason = "正在结束后台操作，请等待退出。"
        elif not _operational(self):
            reason = "安全预览模式：不会保存配置、连接网络或更改系统。"
        elif self.busy:
            reason = "设置后台操作正在进行，请等待完成后再操作。"
        elif self._sync_busy:
            reason = "同步或历史操作正在进行，设置操作暂不可用；完成后可继续。"
        else:
            reason = ""
        self.busy_hint.setText(reason)
        self.busy_hint.setVisible(bool(reason))
        for action in (self.save_button, self.import_button, self.export_button, self.test_button, self.winfsp_check_button, self.winfsp_button):
            action.setToolTip(reason)
        self.save_button.setText("正在保存…" if self._operation_name == "save" else "保存设置")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._refresh_controls()
        if not self._winfsp_checked and _operational(self):
            QTimer.singleShot(0, self._refresh_winfsp)

    def event(self, event) -> bool:
        result = super().event(event)
        if event.type() == QEvent.Type.DynamicPropertyChange and hasattr(self, "_editors"):
            self._refresh_controls()
        return result

    def save(self) -> None:
        if not self._guard_operation():
            return
        if self.config.load_error:
            _feedback(self.notice_label, "原配置读取失败，不能保存覆盖。请先导入合法配置恢复。", "error")
            return
        request = _PreferencesInput(
            self._connection_input(), self.drive_combo.currentText(),
            self.mount_check.isChecked(), self.network_check.isChecked(),
            self.auto_mount_check.isChecked(), self.autostart_check.isChecked(),
            self.tray_check.isChecked(), self.mirror_enabled_check.isChecked(),
            read_only=self.read_only_check.isChecked(),
        )
        draft = self.config.snapshot()

        def save_config(worker: _OperationThread) -> _SavedSettings:
            worker._secrets.append(request.connection.password)
            _write_connection(draft, request.connection, allow_unconfigured=True)
            for key, value in (
                ("mount.drive", request.drive), ("mount.enabled", request.mount_enabled),
                ("mount.read_only", request.read_only),
                ("mount.network_mode", request.network_mode), ("mount.auto_mount", request.auto_mount),
                ("app.start_with_windows", request.autostart), ("app.minimize_to_tray", request.minimize_to_tray),
                ("mirror.enabled", request.mirror_enabled),
            ):
                draft.set(key, value)
            validate_tasks(draft.tasks, request.drive)
            worker.begin_commit()
            draft.save()
            worker.committed = True
            if worker.cancel_event.is_set():
                return _SavedSettings(draft, "已保存配置；退出过程中未更改开机自启。")
            try:
                ok, message = set_autostart(request.autostart)
            except Exception as exc:  # noqa: BLE001
                ok, message = False, worker.redact(exc)
            return _SavedSettings(draft, message, ok)

        _feedback(self.notice_label, "正在校验并保存设置…")
        self._start_operation("save", save_config, self._saved_settings)

    def _saved_settings(self, worker: _OperationThread) -> None:
        if self._failed(worker, "保存"):
            return
        result = cast(_SavedSettings, worker.result)
        self.config.apply_snapshot(result.config)
        self.load()
        self.saved.emit()
        text = "已保存。网络盘将在当前任务空闲后应用新设置。"
        if result.autostart_message:
            text += f"\n{'开机自启：' if result.autostart_ok else '开机自启未能更新：'}{result.autostart_message}"
        _feedback(self.notice_label, text, "good" if result.autostart_ok else "error")

    def _failed(self, worker: _OperationThread, action: str) -> bool:
        if worker.error:
            _feedback(self.notice_label, f"{action}失败：{worker.error}", "error")
            return True
        if worker.cancelled:
            _feedback(self.notice_label, f"已取消{action}。")
            return True
        return False

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
        _feedback(self.winfsp_label, "WinFsp · 已安装" if self._winfsp_installed else "WinFsp · 未安装（挂载网络盘需要此组件）", "good" if self._winfsp_installed else "bodyMuted")

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
        if self._failed(worker, "WinFsp 安装"):
            _feedback(self.winfsp_label, "WinFsp · 请重新检测安装状态")
            self._winfsp_checked = False
            return
        ok, message = worker.result
        self._winfsp_installed = True if ok else None
        self._winfsp_checked = bool(ok)
        _feedback(self.winfsp_label, message, "good" if ok else "error")

    def _export(self) -> None:
        if not self._guard_operation() or self.config.load_error:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出配置（不含密码）", str(Path.home() / "PhoneNAS-config.json"), "JSON (*.json)")
        if not path or not self._guard_operation():
            return
        snapshot = self.config.snapshot()
        _feedback(self.notice_label, "正在导出配置（不含密码）…")
        def export_config(worker: _OperationThread) -> None:
            worker.begin_commit()
            snapshot.export_to(Path(path))
            worker.committed = True

        self._start_operation("export", export_config, self._exported)

    def _exported(self, worker: _OperationThread) -> None:
        if not self._failed(worker, "导出"):
            _feedback(self.notice_label, "已导出配置，不含密码。导出文件可能包含你的本地目录和连接地址，请妥善保管。", "good")

    def _import(self) -> None:
        if not self._guard_operation():
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入配置", str(Path.home()), "JSON (*.json)")
        if not path or not self._guard_operation():
            return
        answer = QMessageBox.question(
            self, "导入配置", "导入会替换当前配置，包括任务清单。不会删除电脑或手机上的文件。确认导入所选文件？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes or not self._guard_operation():
            return
        draft = self.config.snapshot()

        def import_config(worker: _OperationThread) -> Config:
            worker.begin_commit()
            # Config validates and atomically saves, including load_error recovery.
            draft.import_from(Path(path))
            worker.committed = True
            return draft

        _feedback(self.notice_label, "正在校验并导入配置…")
        self._start_operation("import", import_config, self._imported)

    def _imported(self, worker: _OperationThread) -> None:
        if self._failed(worker, "导入"):
            return
        self.config.apply_snapshot(cast(Config, worker.result))
        self.load()
        self.saved.emit()
        _feedback(self.notice_label, "已导入合法配置。网络盘将在空闲后应用；请确认密码和任务设置，开机自启可在保存后应用。", "good")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.shutdown():
            event.accept()
        else:
            event.ignore()
