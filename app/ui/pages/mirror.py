from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app import constants as C
from app.core.config import Config


class MirrorPage(QWidget):
    saved = Signal()

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.local_edit = QLineEdit()
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.local_edit, 1)
        row.addWidget(browse)
        form.addRow("本地工作文件夹", row)

        self.remote_edit = QLineEdit()
        form.addRow("远端镜像目录", self.remote_edit)

        self.versions_edit = QLineEdit()
        form.addRow("远端版本目录", self.versions_edit)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 1440)
        self.interval_spin.setSuffix(" 分钟")
        form.addRow("同步频率", self.interval_spin)

        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(1, 3650)
        self.retention_spin.setSuffix(" 天")
        form.addRow("版本保留", self.retention_spin)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("推荐（排除构建产物，保留 Release）", "recommended")
        self.preset_combo.addItem("不排除任何东西", "none")
        self.preset_combo.addItem("自定义", "custom")
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        form.addRow("排除规则", self.preset_combo)

        layout.addLayout(form)

        layout.addWidget(QLabel("排除规则（每行一个，支持 rclone 通配符）"))
        self.excludes_edit = QPlainTextEdit()
        self.excludes_edit.setMaximumBlockCount(200)
        layout.addWidget(self.excludes_edit, 1)

        save = QPushButton("保存并应用")
        save.clicked.connect(self.save)
        layout.addWidget(save, alignment=Qt.AlignmentFlag.AlignRight)

        self.load()

    # -- data -------------------------------------------------------------
    def load(self) -> None:
        self.local_edit.setText(self.config.get("mirror.local_path", ""))
        self.remote_edit.setText(self.config.remote_path)
        self.versions_edit.setText(self.config.versions_path)
        self.interval_spin.setValue(self.config.interval_minutes)
        self.retention_spin.setValue(self.config.retention_days)
        preset = self.config.get("mirror.exclude_preset", "recommended")
        index = self.preset_combo.findData(preset)
        self.preset_combo.setCurrentIndex(index if index >= 0 else 0)
        self.excludes_edit.setPlainText("\n".join(self.config.get("mirror.excludes", []) or []))
        self._preset_changed()

    def save(self) -> None:
        local = self.local_edit.text().strip()
        if not local:
            QMessageBox.warning(self, "PhoneNAS", "请选择本地工作文件夹")
            return
        self.config.set("mirror.local_path", local)
        self.config.set("mirror.remote_path", self.remote_edit.text().strip().strip("/") or "backup/work")
        self.config.set("mirror.versions_path", self.versions_edit.text().strip().strip("/") or "backup/work-versions")
        self.config.set("mirror.interval_minutes", self.interval_spin.value())
        self.config.set("mirror.retention_days", self.retention_spin.value())
        preset = self.preset_combo.currentData()
        self.config.set("mirror.exclude_preset", preset)
        if preset == "recommended":
            self.config.set("mirror.excludes", list(C.RECOMMENDED_EXCLUDES))
        elif preset == "none":
            self.config.set("mirror.excludes", [])
        else:
            lines = [line.strip() for line in self.excludes_edit.toPlainText().splitlines() if line.strip()]
            self.config.set("mirror.excludes", lines)
        self.config.save()
        self.saved.emit()
        QMessageBox.information(self, "PhoneNAS", "镜像设置已保存")

    # -- slots ------------------------------------------------------------
    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择工作文件夹", self.local_edit.text())
        if folder:
            self.local_edit.setText(folder)

    def _preset_changed(self) -> None:
        preset = self.preset_combo.currentData()
        if preset == "recommended":
            self.excludes_edit.setPlainText("\n".join(C.RECOMMENDED_EXCLUDES))
            self.excludes_edit.setEnabled(False)
        elif preset == "none":
            self.excludes_edit.clear()
            self.excludes_edit.setEnabled(False)
        else:
            self.excludes_edit.setEnabled(True)
