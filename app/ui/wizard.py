from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app import constants as C
from app.core import winfsp
from app.core.autostart import set_autostart
from app.core.config import Config
from app.core.rclone import Rclone


class WizardDialog(QDialog):
    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self.rclone = Rclone(config)
        self.setWindowTitle("PhoneNAS 首次设置")
        self.resize(620, 560)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "把手机 WebDAV 挂载为 Windows 盘符，并把一个工作文件夹单向镜像到手机，"
            "变更和删除的文件会进入版本目录。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        conn = QGroupBox("1. 连接手机 WebDAV")
        conn_form = QFormLayout(conn)
        self.url_edit = QLineEdit(config.url or C.DEFAULT_URL)
        self.user_edit = QLineEdit(config.username)
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.setPlaceholderText("留空则沿用已保存的密码")
        if config.password:
            self.pass_edit.setText("")
        conn_form.addRow("地址", self.url_edit)
        conn_form.addRow("用户名", self.user_edit)
        conn_form.addRow("密码", self.pass_edit)
        test_row = QHBoxLayout()
        self.test_button = QPushButton("测试连接")
        self.test_label = QLabel("")
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.test_label, 1)
        conn_form.addRow("", test_row)
        layout.addWidget(conn)

        deps = QGroupBox("2. 挂载依赖（WinFsp）")
        deps_row = QHBoxLayout(deps)
        self.winfsp_label = QLabel("")
        self.winfsp_button = QPushButton("安装 WinFsp")
        deps_row.addWidget(self.winfsp_label, 1)
        deps_row.addWidget(self.winfsp_button)
        layout.addWidget(deps)

        mount = QGroupBox("3. 挂载与镜像")
        mount_form = QFormLayout(mount)
        self.drive_combo = QComboBox()
        self.drive_combo.addItems(C.DRIVE_LETTERS)
        drive = config.get("mount.drive", C.DEFAULT_DRIVE)
        index = self.drive_combo.findText(drive)
        if index >= 0:
            self.drive_combo.setCurrentIndex(index)
        mount_form.addRow("盘符", self.drive_combo)

        self.local_edit = QLineEdit(config.get("mirror.local_path", ""))
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        local_row = QHBoxLayout()
        local_row.addWidget(self.local_edit, 1)
        local_row.addWidget(browse)
        mount_form.addRow("工作文件夹", local_row)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 1440)
        self.interval_spin.setValue(config.interval_minutes)
        self.interval_spin.setSuffix(" 分钟")
        mount_form.addRow("同步频率", self.interval_spin)

        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(1, 3650)
        self.retention_spin.setValue(config.retention_days)
        self.retention_spin.setSuffix(" 天")
        mount_form.addRow("版本保留", self.retention_spin)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("推荐（排除构建产物，保留 Release）", "recommended")
        self.preset_combo.addItem("不排除任何东西", "none")
        preset = config.get("mirror.exclude_preset", "recommended")
        idx = self.preset_combo.findData(preset)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        mount_form.addRow("排除规则", self.preset_combo)

        self.autostart_check = QCheckBox("开机自动启动 PhoneNAS")
        self.autostart_check.setChecked(bool(config.get("app.start_with_windows", True)))
        mount_form.addRow("", self.autostart_check)
        layout.addWidget(mount)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.test_button.clicked.connect(self._test)
        self.winfsp_button.clicked.connect(self._install_winfsp)
        self._refresh_winfsp()

    # -- slots ------------------------------------------------------------
    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择工作文件夹", self.local_edit.text())
        if folder:
            self.local_edit.setText(folder)

    def _test(self) -> None:
        self.test_button.setEnabled(False)
        self.test_label.setText("正在连接…")
        try:
            res = self.rclone.test_connection(
                self.url_edit.text().strip(),
                self.user_edit.text().strip(),
                self.pass_edit.text() or self.config.password,
            )
        finally:
            self.test_button.setEnabled(True)
        self.test_label.setText("连接成功" if res.ok else "连接失败，请检查地址/账号/Tailscale")

    def _refresh_winfsp(self) -> None:
        installed = winfsp.is_installed()
        self.winfsp_label.setText("WinFsp：已安装" if installed else "WinFsp：未安装")
        self.winfsp_button.setEnabled(not installed)

    def _install_winfsp(self) -> None:
        self.winfsp_button.setEnabled(False)
        self.winfsp_label.setText("正在安装…")
        try:
            _ok, message = winfsp.install()
        finally:
            self._refresh_winfsp()
        QMessageBox.information(self, "PhoneNAS", message)

    def accept(self) -> None:
        url = self.url_edit.text().strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "PhoneNAS", "地址必须以 http:// 或 https:// 开头")
            return
        local = self.local_edit.text().strip()
        if not local:
            QMessageBox.warning(self, "PhoneNAS", "请选择工作文件夹")
            return
        if not winfsp.is_installed():
            answer = QMessageBox.question(
                self, "PhoneNAS", "WinFsp 尚未安装，挂载将无法工作。仍要继续吗？"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.config.set("nas.url", url)
        self.config.set("nas.username", self.user_edit.text().strip())
        if self.pass_edit.text():
            self.config.password = self.pass_edit.text()
        self.config.set("mount.drive", self.drive_combo.currentText())
        self.config.set("mount.auto_mount", True)
        self.config.set("mirror.local_path", local)
        self.config.set("mirror.interval_minutes", self.interval_spin.value())
        self.config.set("mirror.retention_days", self.retention_spin.value())
        preset = self.preset_combo.currentData()
        self.config.set("mirror.exclude_preset", preset)
        self.config.set(
            "mirror.excludes",
            list(C.RECOMMENDED_EXCLUDES) if preset == "recommended" else [],
        )
        self.config.set("app.start_with_windows", self.autostart_check.isChecked())
        self.config.save()
        self.rclone.write_conf(self.config.url, self.config.username, self.config.password)
        set_autostart(self.autostart_check.isChecked())
        super().accept()
