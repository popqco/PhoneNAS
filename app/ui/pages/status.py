from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class StatusPage(QWidget):
    sync_requested = Signal()
    mount_toggle_requested = Signal()
    pause_toggle_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        conn = QGroupBox("连接")
        conn_layout = QVBoxLayout(conn)
        self.nas_label = QLabel("-")
        self.mount_label = QLabel("-")
        self.winfsp_label = QLabel("-")
        conn_layout.addWidget(self.nas_label)
        conn_layout.addWidget(self.mount_label)
        conn_layout.addWidget(self.winfsp_label)
        layout.addWidget(conn)

        sync = QGroupBox("同步")
        sync_layout = QVBoxLayout(sync)
        self.last_sync_label = QLabel("尚未同步")
        self.next_sync_label = QLabel("未计划")
        self.state_label = QLabel("就绪")
        sync_layout.addWidget(self.last_sync_label)
        sync_layout.addWidget(self.next_sync_label)
        sync_layout.addWidget(self.state_label)

        buttons = QHBoxLayout()
        self.sync_button = QPushButton("立即同步")
        self.pause_button = QPushButton("暂停")
        self.mount_button = QPushButton("卸载")
        buttons.addWidget(self.sync_button)
        buttons.addWidget(self.pause_button)
        buttons.addWidget(self.mount_button)
        sync_layout.addLayout(buttons)
        layout.addWidget(sync)

        out = QGroupBox("最近输出")
        out_layout = QVBoxLayout(out)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(500)
        out_layout.addWidget(self.output)
        layout.addWidget(out, 1)

        self.sync_button.clicked.connect(self.sync_requested)
        self.mount_button.clicked.connect(self.mount_toggle_requested)
        self.pause_button.clicked.connect(self.pause_toggle_requested)

    def set_nas(self, url: str) -> None:
        self.nas_label.setText(f"NAS 地址：{url}")

    def set_winfsp(self, installed: bool) -> None:
        self.winfsp_label.setText("WinFsp：已安装" if installed else "WinFsp：未安装")

    def set_mount(self, state: str, drive: str) -> None:
        text = {
            "stopped": "未挂载",
            "starting": "正在挂载…",
            "mounted": f"已挂载到 {drive}",
            "retrying": "挂载中断，正在重试…",
            "error": "挂载失败",
        }.get(state, state)
        self.mount_label.setText(f"挂载状态：{text}")
        self.mount_button.setText("卸载" if state in ("mounted", "starting", "retrying") else "挂载")

    def set_sync(self, last: str, next_run: str, paused: bool) -> None:
        self.last_sync_label.setText(f"上次同步：{last or '尚未同步'}")
        self.next_sync_label.setText(f"下次同步：{'已暂停' if paused else (next_run or '未计划')}")
        self.pause_button.setText("继续" if paused else "暂停")

    def append_output(self, text: str) -> None:
        self.output.appendPlainText(text)

    def set_state(self, text: str) -> None:
        self.state_label.setText(text)
