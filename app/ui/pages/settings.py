from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import constants as C
from app.core import winfsp
from app.core.autostart import set_autostart
from app.core.config import Config
from app.core.rclone import Rclone


class SettingsPage(QWidget):
    saved = Signal()

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self.rclone = Rclone(config)
        layout = QVBoxLayout(self)

        nas = QGroupBox("NAS 连接")
        form = QFormLayout(nas)
        self.url_edit = QLineEdit()
        self.user_edit = QLineEdit()
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.setPlaceholderText("留空则不修改已保存的密码")
        form.addRow("地址", self.url_edit)
        form.addRow("用户名", self.user_edit)
        form.addRow("密码", self.pass_edit)

        test_row = QHBoxLayout()
        self.test_button = QPushButton("测试连接")
        self.test_label = QLabel("")
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.test_label, 1)
        form.addRow("", test_row)
        layout.addWidget(nas)

        mount = QGroupBox("挂载")
        mount_form = QFormLayout(mount)
        self.drive_combo = QComboBox()
        self.drive_combo.addItems(C.DRIVE_LETTERS)
        self.network_check = QCheckBox("以网络驱动器方式挂载（部分软件更兼容）")
        self.auto_mount_check = QCheckBox("程序启动时自动挂载")
        mount_form.addRow("盘符", self.drive_combo)
        mount_form.addRow("", self.network_check)
        mount_form.addRow("", self.auto_mount_check)
        layout.addWidget(mount)

        app_group = QGroupBox("程序")
        app_form = QFormLayout(app_group)
        self.autostart_check = QCheckBox("开机自动启动")
        self.tray_check = QCheckBox("关闭窗口时最小化到托盘")
        app_form.addRow("", self.autostart_check)
        app_form.addRow("", self.tray_check)
        layout.addWidget(app_group)

        deps = QGroupBox("依赖")
        deps_row = QHBoxLayout(deps)
        self.winfsp_label = QLabel("")
        self.winfsp_button = QPushButton("安装 WinFsp")
        deps_row.addWidget(self.winfsp_label, 1)
        deps_row.addWidget(self.winfsp_button)
        layout.addWidget(deps)

        actions = QHBoxLayout()
        save_button = QPushButton("保存")
        export_button = QPushButton("导出配置…")
        import_button = QPushButton("导入配置…")
        actions.addWidget(save_button)
        actions.addWidget(export_button)
        actions.addWidget(import_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)

        save_button.clicked.connect(self.save)
        export_button.clicked.connect(self._export)
        import_button.clicked.connect(self._import)
        self.test_button.clicked.connect(self._test)
        self.winfsp_button.clicked.connect(self._install_winfsp)
        self.load()

    # -- data -------------------------------------------------------------
    def load(self) -> None:
        self.url_edit.setText(self.config.url)
        self.user_edit.setText(self.config.username)
        self.pass_edit.clear()
        drive = self.config.get("mount.drive", C.DEFAULT_DRIVE)
        index = self.drive_combo.findText(drive)
        if index >= 0:
            self.drive_combo.setCurrentIndex(index)
        self.network_check.setChecked(bool(self.config.get("mount.network_mode", False)))
        self.auto_mount_check.setChecked(bool(self.config.get("mount.auto_mount", True)))
        self.autostart_check.setChecked(bool(self.config.get("app.start_with_windows", True)))
        self.tray_check.setChecked(bool(self.config.get("app.minimize_to_tray", True)))
        self._refresh_winfsp()

    def save(self) -> None:
        url = self.url_edit.text().strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "PhoneNAS", "地址必须以 http:// 或 https:// 开头")
            return
        self.config.set("nas.url", url)
        self.config.set("nas.username", self.user_edit.text().strip())
        if self.pass_edit.text():
            self.config.password = self.pass_edit.text()
        self.config.set("mount.drive", self.drive_combo.currentText())
        self.config.set("mount.network_mode", self.network_check.isChecked())
        self.config.set("mount.auto_mount", self.auto_mount_check.isChecked())
        self.config.set("app.start_with_windows", self.autostart_check.isChecked())
        self.config.set("app.minimize_to_tray", self.tray_check.isChecked())
        self.config.save()
        self.rclone.write_conf(self.config.url, self.config.username, self.config.password)
        ok, message = set_autostart(self.autostart_check.isChecked())
        self.saved.emit()
        QMessageBox.information(self, "PhoneNAS", f"已保存。{message if ok else ''}")
        self.load()

    # -- slots ------------------------------------------------------------
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
        if res.ok:
            self.test_label.setText("连接成功")
        else:
            self.test_label.setText("连接失败")

    def _refresh_winfsp(self) -> None:
        installed = winfsp.is_installed()
        self.winfsp_label.setText("WinFsp：已安装" if installed else "WinFsp：未安装")
        self.winfsp_button.setEnabled(not installed)

    def _install_winfsp(self) -> None:
        self.winfsp_button.setEnabled(False)
        self.winfsp_label.setText("正在安装 WinFsp…")
        try:
            ok, message = winfsp.install()
        finally:
            self._refresh_winfsp()
        QMessageBox.information(self, "PhoneNAS", message)

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出配置", str(Path.home() / "PhoneNAS-config.json"), "JSON (*.json)"
        )
        if not path:
            return
        self.config.export_to(Path(path))
        QMessageBox.information(self, "PhoneNAS", "已导出（不含密码）")

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入配置", str(Path.home()), "JSON (*.json)"
        )
        if not path:
            return
        try:
            self.config.import_from(Path(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "PhoneNAS", f"导入失败：{exc}")
            return
        self.rclone.write_conf(self.config.url, self.config.username, self.config.password)
        self.load()
        self.saved.emit()
        QMessageBox.information(self, "PhoneNAS", "已导入配置，密码请重新填写后保存")
