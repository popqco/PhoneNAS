from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QHeaderView, QScrollArea, QSizePolicy,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app import constants as C
from app.core.history import display_path
from app.core.sync import SyncReport
from app.core.tasks import MirrorTask
from app.ui.widgets import Card, button, label


def _wrapped(text: str, role: str = ""):
    words = label(text, role)
    words.setWordWrap(True)
    words.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    words.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return words


class PreviewDialog(QDialog):
    """Display an already-computed report. This window never starts a worker."""

    def __init__(
        self, task: MirrorTask, report: SyncReport, reason: str = "", parent=None, *,
        operation: str = "",
    ):
        super().__init__(parent)
        self.recovering = operation == "recover"
        self.operation = report.operation if self.recovering else operation or report.operation
        self.blocked = bool(report.blocked)
        incoming = self.operation in ("receive", "adopt")
        known_operation = self.operation in ("publish", "receive", "return", "adopt")
        self._can_confirm = known_operation and not self.blocked and task.enabled
        self.setWindowTitle(f"查看差异 · {'继续未完成操作 · ' if self.recovering else ''}{task.name}")
        available = self.screen().availableGeometry()
        width, height = max(240, available.width() - 32), max(220, available.height() - 48)
        self.setMinimumSize(min(520, width), min(350, height))
        self.resize(min(780, width), min(640, height))
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        root.addWidget(_wrapped("先看看，这次会发生什么。", "dialogTitle"))
        root.addWidget(_wrapped("只读扫描已完成。查看差异不会上传、拉取、覆盖或删除任何文件。", "subtitle"))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setMinimumWidth(0)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        mapping = Card()
        mapping_layout = QVBoxLayout(mapping)
        mapping_layout.setContentsMargins(15, 12, 15, 12)
        mapping_layout.setSpacing(8)
        mapping_layout.addWidget(_wrapped(task.name, "heading"))
        operation_text = {
            "publish": "上传发布 · 本地 → 手机",
            "receive": "下载接收 · 手机 → 本地",
            "return": "交回本地修改 · 本地 → 手机",
            "adopt": "接收手机更新 · 手机 → 本地",
        }.get(self.operation, "无法识别的操作 · 已禁止执行")
        mapping_layout.addWidget(_wrapped("继续未完成操作 · " + operation_text if self.recovering else operation_text, "field"))
        remote = f"{C.DEFAULT_REMOTE}:{task.remote_path}"
        self.source_label = _wrapped(
            f"来源 · 手机当前镜像\n{remote}" if incoming else f"来源 · 本地工作目录\n{task.local_path}", "mono",
        )
        self.destination_label = _wrapped(
            f"目标 · 本地接收目录（将被更新）\n{task.local_path}" if incoming else f"目标 · 手机当前镜像（将被更新）\n{remote}", "mono",
        )
        self.history_label = _wrapped(
            f"本地历史 · 本地被覆盖 / 删除的旧文件\n{task.local_history_path}" if incoming else f"手机历史 · 手机被覆盖 / 删除的旧文件\n{C.DEFAULT_REMOTE}:{task.versions_path}", "mono",
        )
        mapping_layout.addWidget(self.source_label)
        mapping_layout.addWidget(self.destination_label)
        mapping_layout.addWidget(self.history_label)
        layout.addWidget(mapping)
        layout.addWidget(_wrapped(report.summary, "heading"))
        reasons = list(dict.fromkeys(text for text in (report.reason, reason) if text))
        self.reason_label = _wrapped("\n".join(reasons), "error")
        self.reason_label.setVisible(bool(reasons))
        layout.addWidget(self.reason_label)
        self.blocked_label = _wrapped(
            "保护已阻止本次操作。先保留并处理本地 / 手机改动，不能通过确认覆盖保护。"
            "请先将本地修改另存为副本，并保留手机上的新文件；核对冲突或完成接收 / 交回后，再重新查看差异。",
            "error",
        )
        self.blocked_label.setVisible(self.blocked)
        layout.addWidget(self.blocked_label)
        if report.requires_confirmation and not self.blocked:
            layout.addWidget(_wrapped("本次操作需要明确确认。请核对目标目录与每项变动，确认后才会继续执行。", "bodyMuted"))
        self.table = QTableWidget(0, 2)
        destination = "本地" if incoming else "手机"
        self.table.setHorizontalHeaderLabels([f"对{destination}目标的变动", "相对文件路径"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setMinimumWidth(0)
        self.table.setMinimumHeight(150)
        self.table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        names = {
            "+": f"新增到{destination}",
            "*": f"更新{destination} · 保留旧文件",
            "-": f"删除{destination} · 移入历史",
        }
        self.table.setRowCount(len(report.changes))
        for row, (mark, path) in enumerate(report.changes):
            self.table.setItem(row, 0, QTableWidgetItem(names.get(mark, mark)))
            path_item = QTableWidgetItem(display_path(path))
            path_item.setToolTip(display_path(path))
            self.table.setItem(row, 1, path_item)
        layout.addWidget(self.table, 1)
        note = _wrapped(
            f"最多展示 500 条差异。目标中被覆盖或删除的旧文件保留 {task.retention_days} 天；"
            "历史文件不是完整时间点快照。执行前会再次扫描，差异变化需重新确认。"
            "全局暂停自动计划不影响已启用任务的手动操作；停用任务需先启用。",
            "small",
        )
        layout.addWidget(note)
        self.scroll.setWidget(content)
        root.addWidget(self.scroll, 1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.close_button = button("关闭", "secondary")
        text = "确认并继续未完成操作" if self.recovering else "确认并拉取" if incoming else "确认并交回" if self.operation == "return" else "确认并上传"
        self.execute_button = button(text, "primary", "sync")
        self.close_button.clicked.connect(self.reject)
        self.execute_button.clicked.connect(self.accept)
        self.execute_button.setEnabled(self._can_confirm)
        if self.blocked:
            self.execute_button.setToolTip("先保留并处理本地 / 手机改动，再重新查看差异；不能通过确认覆盖保护。")
        elif not task.enabled:
            self.execute_button.setToolTip("任务已停用，请先启用任务后再确认执行。")
            layout.addWidget(_wrapped("任务已停用，仍可查看差异；执行前请返回主窗口启用任务。", "bodyMuted"))
        elif not known_operation:
            self.execute_button.setToolTip("无法识别的操作，已禁止执行。")
        actions.addWidget(self.close_button)
        actions.addWidget(self.execute_button)
        root.addLayout(actions)

    def accept(self) -> None:
        if self._can_confirm:
            super().accept()

    def done(self, result: int) -> None:
        if result != QDialog.DialogCode.Accepted or self._can_confirm:
            super().done(result)
