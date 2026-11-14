from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import constants as C
from app.core.config import Config
from app.core.log import get_logger
from app.core.rclone import Rclone


class VersionBrowserDialog(QDialog):
    def __init__(self, rclone: Rclone, version: str, local_path: Path, parent=None):
        super().__init__(parent)
        self.rclone = rclone
        self.version = version
        self.local_path = local_path
        self.log = get_logger()
        self.setWindowTitle(f"版本内容 - {version}")
        self.resize(720, 480)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("选择要恢复到工作文件夹的文件（可多选）："))
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        restore = QPushButton("恢复选中")
        close = QPushButton("关闭")
        restore.clicked.connect(self._restore)
        close.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(restore)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self._load()

    def _load(self) -> None:
        base = f"{self.version}"
        res = self.rclone.lsf_files(f"{self.rclone.config.versions_path}/{base}")
        if not res.ok:
            QMessageBox.warning(self, "PhoneNAS", f"读取版本内容失败：{res.err or res.out}")
            return
        for line in res.out.splitlines():
            if line.strip():
                self.list.addItem(line.strip())

    def _restore(self) -> None:
        items = [item.text() for item in self.list.selectedItems()]
        if not items:
            QMessageBox.information(self, "PhoneNAS", "请先选择文件")
            return
        restored = 0
        errors = []
        for rel in items:
            src = f"{C.DEFAULT_REMOTE}:{self.rclone.config.versions_path}/{self.version}/{rel}"
            dst = self.local_path / Path(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            res = self.rclone.copyto(src, str(dst))
            if res.ok:
                restored += 1
            else:
                errors.append(f"{rel}: {res.err or res.out}")
        message = f"已恢复 {restored} 个文件到 {self.local_path}"
        if errors:
            message += "\n\n失败：\n" + "\n".join(errors[:5])
        QMessageBox.information(self, "PhoneNAS", message)


class VersionsPage(QWidget):
    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self.rclone = Rclone(config)
        self.log = get_logger()
        self._versions: list[str] = []

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.refresh_button = QPushButton("刷新")
        self.browse_button = QPushButton("查看内容 / 恢复文件")
        self.restore_all_button = QPushButton("恢复整个版本…")
        self.delete_button = QPushButton("删除版本")
        self.open_button = QPushButton("在资源管理器中打开")
        top.addWidget(self.refresh_button)
        top.addWidget(self.browse_button)
        top.addWidget(self.restore_all_button)
        top.addWidget(self.open_button)
        top.addWidget(self.delete_button)
        top.addStretch(1)
        layout.addLayout(top)

        self.status = QLabel("尚未加载")
        layout.addWidget(self.status)

        self.list = QListWidget()
        layout.addWidget(self.list, 1)

        self.refresh_button.clicked.connect(self.refresh)
        self.browse_button.clicked.connect(self._browse)
        self.restore_all_button.clicked.connect(self._restore_all)
        self.delete_button.clicked.connect(self._delete)
        self.open_button.clicked.connect(self._open_in_explorer)

    # -- data -------------------------------------------------------------
    def refresh(self) -> None:
        self.rclone.ensure_conf()
        res = self.rclone.lsf_dirs(self.config.versions_path)
        self.list.clear()
        self._versions = []
        if not res.ok:
            self.status.setText(f"读取版本失败：{res.err or res.out}")
            return
        names = sorted(
            [line.strip().strip("/") for line in res.out.splitlines() if line.strip()],
            reverse=True,
        )
        self._versions = names
        for name in names:
            self.list.addItem(name)
        self.status.setText(f"共 {len(names)} 个版本" if names else "还没有历史版本")

    def _selected(self) -> str | None:
        item = self.list.currentItem()
        return item.text() if item else None

    # -- actions ----------------------------------------------------------
    def _browse(self) -> None:
        version = self._selected()
        if not version:
            QMessageBox.information(self, "PhoneNAS", "请先选择一个版本")
            return
        dialog = VersionBrowserDialog(self.rclone, version, self.config.local_path, self)
        dialog.exec()

    def _restore_all(self) -> None:
        version = self._selected()
        if not version:
            QMessageBox.information(self, "PhoneNAS", "请先选择一个版本")
            return
        default = str(Path(self.config.local_path).parent / "PhoneNAS-Restore")
        dest = QFileDialog.getExistingDirectory(self, "选择恢复到的目录", default)
        if not dest:
            return
        target = Path(dest) / version
        res = self.rclone.copy(
            f"{C.DEFAULT_REMOTE}:{self.config.versions_path}/{version}", str(target)
        )
        if res.ok:
            QMessageBox.information(self, "PhoneNAS", f"已恢复到 {target}")
        else:
            QMessageBox.warning(self, "PhoneNAS", f"恢复失败：{res.err or res.out}")

    def _delete(self) -> None:
        version = self._selected()
        if not version:
            QMessageBox.information(self, "PhoneNAS", "请先选择一个版本")
            return
        answer = QMessageBox.question(
            self,
            "PhoneNAS",
            f"确定删除版本 {version} 吗？此操作不可恢复。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        res = self.rclone.purge(f"{self.config.versions_path}/{version}")
        if res.ok:
            self.refresh()
        else:
            QMessageBox.warning(self, "PhoneNAS", f"删除失败：{res.err or res.out}")

    def _open_in_explorer(self) -> None:
        version = self._selected()
        if not version:
            QMessageBox.information(self, "PhoneNAS", "请先选择一个版本")
            return
        drive = self.config.get("mount.drive", C.DEFAULT_DRIVE)
        path = Path(drive + "\\") / self.config.versions_path.replace("/", "\\") / version
        if path.exists():
            os.startfile(str(path))
        else:
            QMessageBox.information(
                self,
                "PhoneNAS",
                "该版本不在已挂载的盘符中。请先挂载，或使用“查看内容 / 恢复文件”。",
            )
