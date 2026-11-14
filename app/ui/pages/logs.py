from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.log import log_dir


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.readlines()
        return "".join(content[-lines:])
    except OSError as exc:
        return f"读取失败：{exc}"


class LogsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.refresh_button = QPushButton("刷新")
        self.open_button = QPushButton("打开日志目录")
        self.path_label = QLabel(str(log_dir()))
        self.path_label.setTextInteractionFlags(self.path_label.textInteractionFlags())
        top.addWidget(self.refresh_button)
        top.addWidget(self.open_button)
        top.addWidget(self.path_label, 1)
        layout.addLayout(top)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(2000)
        layout.addWidget(self.text, 1)

        self.refresh_button.clicked.connect(self.refresh)
        self.open_button.clicked.connect(lambda: os.startfile(str(log_dir())))

    def refresh(self) -> None:
        folder = log_dir()
        app_log = _tail(folder / "app.log", 400)
        sync_logs = sorted(folder.glob("sync-*.log"), reverse=True)
        sync_tail = _tail(sync_logs[0], 200) if sync_logs else ""
        parts = ["===== app.log =====", app_log or "(空)"]
        if sync_logs:
            parts += [f"===== {sync_logs[0].name} =====", sync_tail or "(空)"]
        self.text.setPlainText("\n".join(parts))
