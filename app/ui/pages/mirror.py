from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.config import Config
from app.core.tasks import MirrorTask
from app.ui.widgets import (
    Badge,
    Card,
    DeviceIllustration,
    ElidedLabel,
    FlowLayout,
    Header,
    IconBox,
    StatCard,
    button,
    icon,
    label,
    polish,
)


def _meta(glyph: str, text: str) -> QWidget:
    result = QWidget()
    layout = QHBoxLayout(result)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    image = QLabel()
    image.setPixmap(icon(glyph, "#8798b3", 14).pixmap(14))
    layout.addWidget(image)
    layout.addWidget(label(text, "small"))
    return result


class TaskCard(Card):
    sync_requested = Signal(str)
    upload_requested = Signal(str)
    download_requested = Signal(str)
    preview_requested = Signal(str)
    return_requested = Signal(str)
    adopt_requested = Signal(str)
    cancel_requested = Signal(str)
    recover_requested = Signal(str)
    edit_requested = Signal(str)
    toggle_requested = Signal(str)
    remove_requested = Signal(str)
    history_requested = Signal(str)

    def __init__(self, task: MirrorTask, index: int = 0, parent=None):
        super().__init__(parent)
        self.task = task
        self.setProperty("variant", "task")
        self.setProperty("state", "idle")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._narrow = None
        self._runtime = {}
        self._interaction_busy = False
        self._displayed_operation = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        body = QWidget()
        body.setMinimumWidth(0)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 18, 18, 13)
        body_layout.setSpacing(13)
        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(IconBox("folder", ("blue", "sand", "teal")[index % 3], 44))
        words = QVBoxLayout()
        words.setSpacing(5)
        self.name_label = ElidedLabel(task.name, "taskTitle")
        uploading = task.direction == "upload"
        remote_path = "/" + task.remote_path
        self.direction_label = label("本地 → 手机" if uploading else "手机 → 本地", "small")
        self.direction_label.setToolTip(task.direction_label)
        self.source_label = ElidedLabel(task.local_path if uploading else remote_path, "mono")
        self.source_label.setAccessibleName("来源 · 本地" if uploading else "来源 · 手机")
        words.addWidget(self.name_label)
        words.addWidget(self.direction_label)
        words.addWidget(self.source_label)
        top.addLayout(words, 1)
        self.badge = Badge()
        top.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        body_layout.addLayout(top)
        self.meta_widget = QWidget()
        self.metadata = FlowLayout(self.meta_widget, spacing=16)
        self.metadata.addWidget(_meta("clock", f"每 {task.interval_minutes} 分钟" if task.auto_sync else "仅手动"))
        self.metadata.addWidget(_meta("history", f"保留 {task.retention_days} 天"))
        preset = {"recommended": "推荐排除", "none": "不排除", "custom": "自定义排除"}.get(task.exclude_preset, "排除规则")
        self.metadata.addWidget(_meta("shield", preset))
        body_layout.addWidget(self.meta_widget)
        self.target_label = ElidedLabel(remote_path if uploading else task.local_path, "mono")
        self.target_label.setAccessibleName("目标 · 手机" if uploading else "目标 · 本地")
        body_layout.addWidget(self.target_label)
        history_path = "/" + task.history_location if uploading else task.history_location
        self.history_label = ElidedLabel(f"历史 · {'手机' if uploading else '本地'} {history_path}", "small")
        body_layout.addWidget(self.history_label)
        layout.addWidget(body)

        self.footer = QFrame()
        self.footer.setObjectName("taskFooter")
        self.footer_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self.footer)
        self.footer_layout.setContentsMargins(15, 7, 11, 7)
        self.footer_layout.setSpacing(8)
        self.last_label = ElidedLabel("", "small")
        self.footer_layout.addWidget(self.last_label, 1)
        self.actions_widget = QWidget()
        self.actions_widget.setMinimumWidth(0)
        self.actions = FlowLayout(self.actions_widget, spacing=2)
        self.preview_button = button("查看差异", "subtle", "eye")
        self.edit_button = button("设置", "subtle", "settings")
        self.upload_button = button("上传到手机", "subtle", "sync")
        self.download_button = button("下载到本地", "subtle", "sync")
        # Legacy callers may inspect the default button; clicks are explicit intents.
        self.sync_button = self.upload_button if uploading else self.download_button
        self.cancel_button = button("取消本次任务", "subtle")
        self.cancel_button.setToolTip("停止本次操作；已传输内容不会自动回滚，不影响后续自动计划")
        self.history_button = button("历史", "subtle", "history")
        self.toggle_button = button("", "icon", "pause" if task.enabled else "play")
        self.more_button = button("", "icon", "more")
        for action_button in (self.upload_button, self.download_button, self.preview_button, self.history_button, self.edit_button, self.cancel_button, self.toggle_button, self.more_button):
            action_button.setMinimumHeight(34)
            action_button.setIconSize(QSize(16, 16))
            self.actions.addWidget(action_button)
        self.preview_button.setToolTip("先扫描差异，不改动文件")
        self.history_button.setToolTip("查看被覆盖或删除的旧文件；单批历史不是完整快照")
        if task.auto_sync:
            self.toggle_button.setToolTip("停用任务，暂停自动计划及手动写入" if task.enabled else "启用任务，恢复自动计划及手动操作")
        else:
            self.toggle_button.setToolTip("停用任务，禁止手动操作" if task.enabled else "启用任务（仅手动，不自动运行）")
        self.toggle_button.setAccessibleName(self.toggle_button.toolTip())
        self.more_button.setToolTip("更多任务操作")
        self.more_button.setAccessibleName("更多任务操作")
        self.more_menu = QMenu(self.more_button)
        self.relay_action = QAction(icon("sync"), "接收手机更新…" if uploading else "交回本地修改…", self.more_menu)
        self.relay_action.setToolTip("仅手动执行；先查看差异并确认，不改变任务的默认方向")
        self.more_menu.addAction(self.relay_action)
        self.recover_action = QAction(icon("sync"), "继续未完成操作…", self.more_menu)
        self.more_menu.addAction(self.recover_action)
        self.more_menu.addSeparator()
        self.history_action = QAction(icon("history"), "查看旧文件历史", self.more_menu)
        self.edit_action = QAction(icon("edit"), "编辑任务设置", self.more_menu)
        self.remove_action = QAction(icon("trash", "#b45151"), "移除任务（只删除配置）…", self.more_menu)
        self.more_menu.addAction(self.history_action)
        self.more_menu.addAction(self.edit_action)
        self.more_menu.addSeparator()
        self.more_menu.addAction(self.remove_action)
        self.more_button.clicked.connect(self._show_menu)
        self.footer_layout.addWidget(self.actions_widget)
        layout.addWidget(self.footer)
        self.message_label = ElidedLabel("", "small")
        self.message_label.setContentsMargins(16, 7, 16, 9)
        layout.addWidget(self.message_label)
        self.progress = QProgressBar()
        self.progress.setObjectName("taskProgress")
        self.progress.setAccessibleName("本阶段进度")
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.preview_button.clicked.connect(lambda: self.preview_requested.emit(task.id))
        self.edit_button.clicked.connect(lambda: self.edit_requested.emit(task.id))
        self.upload_button.clicked.connect(lambda: self.upload_requested.emit(task.id))
        self.download_button.clicked.connect(lambda: self.download_requested.emit(task.id))
        self.cancel_button.clicked.connect(lambda: self.cancel_requested.emit(task.id))
        self.history_button.clicked.connect(lambda: self.history_requested.emit(task.id))
        self.toggle_button.clicked.connect(lambda: self.toggle_requested.emit(task.id))
        self.history_action.triggered.connect(lambda: self.history_requested.emit(task.id))
        self.edit_action.triggered.connect(lambda: self.edit_requested.emit(task.id))
        self.remove_action.triggered.connect(lambda: self.remove_requested.emit(task.id))
        self.relay_action.triggered.connect(lambda: (self.adopt_requested if uploading else self.return_requested).emit(task.id))
        self.recover_action.triggered.connect(lambda: self.recover_requested.emit(task.id))
        self.set_runtime({}, "", False)

    def _show_menu(self) -> None:
        self.more_menu.popup(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))

    def set_runtime(self, runtime: dict, next_run: str, paused: bool) -> None:
        self._runtime = dict(runtime)
        state = runtime.get("state", "idle")
        default_operation = "publish" if self.task.direction == "upload" else "receive"
        operation = runtime.get("operation") or default_operation
        actions = {"publish": "上传", "receive": "拉取", "return": "交回", "adopt": "接收手机更新", "recover": "继续未完成操作"}
        action = actions.get(operation, "同步")
        # When actions become available again, their endpoints describe the next
        # default operation, never the opposite-direction relay that just ended.
        effective_operation = (runtime.get("effective_operation") or default_operation) if operation == "recover" else operation
        self._set_operation_labels(effective_operation if state in ("running", "preview", "cancelling") else default_operation)
        statuses = {
            "running": ("正在" + action, "blue"),
            "queued": ("等待执行", "amber"),
            "error": (action + "失败", "error"),
            "guarded": ("保护已阻止", "amber"),
            "preview": ("正在查看差异", "blue"),
            "cancelling": ("正在取消", "amber"),
            "cancelled": ("已取消", "neutral"),
        }
        if state in statuses:
            badge_text, tone = statuses[state]
        elif not self.task.enabled:
            badge_text, tone = "已暂停", "neutral"
        elif paused and self.task.auto_sync:
            badge_text, tone = "计划暂停", "neutral"
        elif self.task.last_sync_at:
            badge_text, tone = "上次成功", "good"
        else:
            badge_text, tone = "尚未同步", "neutral"
        self.badge.set_status(badge_text, tone)
        polish(self, state=state)
        last = self.task.last_sync_at or "尚未同步"
        self.last_label.setText(f"上次同步 {last}" if self.task.last_sync_at else "尚未完成首次同步")
        message = str(runtime.get("reason") or runtime.get("message") or "") if state == "guarded" else str(runtime.get("message") or "")
        if next_run and self.task.enabled and self.task.auto_sync and not paused and state not in ("running", "queued", "preview", "cancelling"):
            message = " · ".join(part for part in (message, "下次计划 " + next_run) if part)
        self.message_label.setText(message)
        self.message_label.setVisible(bool(message))
        self.message_label.setToolTip(message)
        polish(self.message_label, role="errorText" if state == "error" else "small")
        self._update_interaction()
        progress = runtime.get("progress")
        self.progress.setVisible(state in ("running", "preview", "cancelling"))
        try:
            amount = None if progress is None or isinstance(progress, bool) else max(0, min(100, round(float(progress))))
        except (TypeError, ValueError, OverflowError):
            amount = None
        if amount is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(amount)
        explanation = "本阶段进度，不是整体任务完成度；后续仍可能需要核对、保存或清理。"
        if amount is None:
            explanation += " 当前无可用总量，以阶段说明和已用时反馈运行状态。"
        self.progress.setToolTip(explanation)
        self.progress.setAccessibleDescription(" · ".join(part for part in (message, explanation) if part))

    def _set_operation_labels(self, operation: str) -> None:
        if operation == self._displayed_operation:
            return
        self._displayed_operation = operation
        uploading = operation not in ("receive", "adopt")
        remote_path = "/" + self.task.remote_path
        self.direction_label.setText("本地 → 手机" if uploading else "手机 → 本地")
        self.direction_label.setToolTip({
            "publish": "上传到手机", "receive": "从手机拉取",
            "return": "交回本地修改", "adopt": "接收手机更新",
        }.get(operation, self.task.direction_label))
        self.source_label.setText(self.task.local_path if uploading else remote_path)
        self.source_label.setAccessibleName("来源 · 本地" if uploading else "来源 · 手机")
        self.target_label.setText(remote_path if uploading else self.task.local_path)
        self.target_label.setAccessibleName("目标 · 手机" if uploading else "目标 · 本地")
        history_path = "/" + self.task.versions_path if uploading else self.task.local_history_path
        self.history_label.setText(f"历史 · {'手机' if uploading else '本地'} {history_path}")

    def set_interaction_busy(self, busy: bool) -> None:
        """Gate task operations without changing runtime state or current cancellation."""
        self._interaction_busy = bool(busy)
        self._update_interaction()

    def _update_interaction(self) -> None:
        state = self._runtime.get("state", "idle")
        busy = self._interaction_busy or bool(self._runtime.get("external_busy")) or state in ("running", "queued", "preview", "cancelling")
        for control in (
            self.preview_button, self.edit_button, self.edit_action, self.remove_action,
            self.toggle_button, self.history_button, self.history_action, self.more_button,
        ):
            control.setEnabled(not busy)
        for control in (self.upload_button, self.download_button):
            control.setEnabled(not busy and self.task.enabled)
            if not self.task.enabled:
                control.setToolTip("请先启用任务，再手动执行")
            elif busy:
                control.setToolTip("请等待当前操作结束")
            else:
                control.setToolTip(control.text() + "；先查看差异，确认后再执行")
        self.relay_action.setEnabled(not busy and self.task.enabled)
        self.recover_action.setEnabled(not busy and self.task.enabled)
        self.recover_action.setToolTip("请先启用任务，再核对未完成操作" if not self.task.enabled else "仅由原电脑核对后继续，不抢占其他电脑的锁")
        if not self.task.enabled:
            self.relay_action.setToolTip("请先启用任务，再执行接力操作")
        elif busy:
            self.relay_action.setToolTip("请等待当前操作结束")
        else:
            self.relay_action.setToolTip("仅手动执行；先查看差异并确认，不改变任务的默认方向")
        self.cancel_button.setVisible(state in ("running", "preview", "cancelling"))
        self.cancel_button.setEnabled(state in ("running", "preview"))
        self.cancel_button.setText("正在取消…" if state == "cancelling" else "取消本次任务")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        narrow = self.width() < 740
        if narrow != self._narrow:
            self._narrow = narrow
            self.footer_layout.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
            self.footer_layout.setAlignment(self.actions_widget, Qt.AlignmentFlag.AlignLeft if narrow else Qt.AlignmentFlag.AlignRight)
            self.meta_widget.setContentsMargins(0 if self.width() < 550 else 56, 0, 0, 0)


class MirrorPage(QWidget):
    sync_requested = Signal(str)
    upload_requested = Signal(str)
    download_requested = Signal(str)
    preview_requested = Signal(str)
    return_requested = Signal(str)
    adopt_requested = Signal(str)
    cancel_requested = Signal(str)
    recover_requested = Signal(str)
    add_requested = Signal()
    batch_requested = Signal()
    edit_requested = Signal(str)
    toggle_requested = Signal(str)
    remove_requested = Signal(str)
    history_requested = Signal(str)
    pause_requested = Signal()
    open_drive_requested = Signal()

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self.cards: dict[str, TaskCard] = {}
        self._states: dict[str, dict] = {}
        self._next_runs: dict[str, str] = {}
        self._paused = False
        self._interaction_busy = False
        self._mount_state = "stopped"
        self._drive = ""
        self._events: list[dict] = []
        self._event_snapshot = None
        self._filter = "all"
        self._responsive_mode = None
        self._stat_columns = None
        self.setObjectName("workspacePage")
        self.setMinimumWidth(0)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("pageScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self.scroll)
        self.canvas = QWidget()
        self.canvas.setObjectName("pageCanvas")
        self.canvas.setMinimumWidth(0)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.body = QVBoxLayout(self.canvas)
        self.body.setContentsMargins(0, 0, 9, 20)
        self.body.setSpacing(22)
        self.header = Header("同步任务", "公司发布，家里接收。每个文件夹都有清晰方向和独立计划。")
        self.pause_button = button("暂停自动计划", "secondary", "pause")
        self.pause_button.setToolTip("只控制后续自动派发，不会取消当前操作；已启用任务仍可手动执行")
        self.add_button = button("添加文件夹", "primary", "plus")
        self.pause_button.clicked.connect(self.pause_requested)
        self.add_button.clicked.connect(self.add_requested)
        self.header.actions.addWidget(self.pause_button)
        self.header.actions.addWidget(self.add_button)
        self.body.addWidget(self.header)

        self.stat_widget = QWidget()
        self.stats_layout = QGridLayout(self.stat_widget)
        self.stats_layout.setContentsMargins(0, 0, 0, 0)
        self.stats_layout.setSpacing(13)
        self.task_stat = StatCard("folders", "0", "个文件夹", "0 个已启用 · 0 个已暂停")
        self.history_stat = StatCard("shield", "独立保留", "旧文件", "每个任务单独设置保留天数")
        self.schedule_stat = StatCard("clock", "未计划", "", "任务串行执行，不抢占带宽")
        self.stats = (self.task_stat, self.history_stat, self.schedule_stat)
        for index, stat in enumerate(self.stats):
            self.stats_layout.addWidget(stat, 0, index)
            self.stats_layout.setColumnStretch(index, 1)
        self.body.addWidget(self.stat_widget)

        self.workspace = QWidget()
        self.workspace.setMinimumWidth(0)
        self.workspace_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self.workspace)
        self.workspace_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_layout.setSpacing(21)
        self.list_panel = QWidget()
        self.list_panel.setMinimumWidth(0)
        self.list_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.list_layout = QVBoxLayout(self.list_panel)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(13)
        toolbar = QWidget()
        self.toolbar_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, toolbar)
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_layout.setSpacing(11)
        self.filters_widget = QWidget()
        self.filters_widget.setObjectName("segmented")
        filter_layout = QHBoxLayout(self.filters_widget)
        filter_layout.setContentsMargins(3, 3, 3, 3)
        filter_layout.setSpacing(3)
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self.filter_buttons = {}
        for key, text in (("all", "全部"), ("active", "已启用"), ("paused", "已暂停")):
            item = button(text, "segment")
            item.setMinimumHeight(31)
            item.setCheckable(True)
            item.setChecked(key == "all")
            item.clicked.connect(lambda _checked=False, value=key: self._set_filter(value))
            self.filter_group.addButton(item)
            self.filter_buttons[key] = item
            filter_layout.addWidget(item)
        self.toolbar_layout.addWidget(self.filters_widget, 0, Qt.AlignmentFlag.AlignLeft)
        self.toolbar_layout.addStretch(1)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("taskSearch")
        self.search_edit.setProperty("role", "search")
        self.search_edit.setPlaceholderText("搜索任务或路径…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(150)
        self.search_edit.setMaximumWidth(270)
        self.search_edit.setMinimumHeight(38)
        self.search_edit.addAction(icon("search", "#8b9bb3", 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search_edit.textChanged.connect(self._apply_filter)
        self.toolbar_layout.addWidget(self.search_edit, 1)
        self.list_layout.addWidget(toolbar)
        self.cards_widget = QWidget()
        self.cards_widget.setMinimumWidth(0)
        self.cards_layout = QVBoxLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(13)
        self.list_layout.addWidget(self.cards_widget)
        self.empty_card = Card()
        empty_layout = QVBoxLayout(self.empty_card)
        empty_layout.setContentsMargins(28, 32, 28, 32)
        empty_layout.setSpacing(12)
        empty_layout.addWidget(IconBox("folders", size=48), 0, Qt.AlignmentFlag.AlignHCenter)
        self.empty_title = label("从一个重要文件夹开始", "heading")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_note = label("添加文件夹，为它建立独立的镜像与旧文件历史。", "bodyMuted")
        self.empty_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_note.setWordWrap(True)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_note)
        self.empty_add_button = button("添加第一个文件夹", "primary", "plus")
        self.empty_add_button.clicked.connect(self.add_requested)
        empty_layout.addWidget(self.empty_add_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self.list_layout.addWidget(self.empty_card)
        self.batch_button = button("一次添加多个文件夹", "dashed", "plus")
        self.batch_button.setMinimumHeight(49)
        self.batch_button.clicked.connect(self.batch_requested)
        self.list_layout.addWidget(self.batch_button)
        note = label("每个任务使用独立的镜像和历史目录。移除任务只删除配置，不删除文件。", "small")
        note.setWordWrap(True)
        note_row = QHBoxLayout()
        note_row.setSpacing(7)
        note_image = QLabel()
        note_image.setPixmap(icon("info", "#9aaac0", 15).pixmap(15))
        note_row.addWidget(note_image, 0, Qt.AlignmentFlag.AlignTop)
        note_row.addWidget(note, 1)
        self.list_layout.addLayout(note_row)
        self.list_layout.addStretch(1)
        self.workspace_layout.addWidget(self.list_panel, 1)
        self._build_rail()
        self.workspace_layout.addWidget(self.rail, 0, Qt.AlignmentFlag.AlignTop)
        self.body.addWidget(self.workspace)
        self.body.addStretch(1)
        self.scroll.setWidget(self.canvas)
        self.load()

    def _build_rail(self) -> None:
        self.rail = QWidget()
        self.rail.setMinimumWidth(0)
        self.rail.setMaximumWidth(272)
        rail_layout = QVBoxLayout(self.rail)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        rail_layout.setSpacing(16)
        self.device_card = Card()
        device = QVBoxLayout(self.device_card)
        device.setContentsMargins(18, 18, 18, 18)
        device.setSpacing(12)
        title_row = QHBoxLayout()
        title_row.addWidget(label("你的手机 NAS", "heading"), 1)
        self.mount_badge = Badge("未挂载")
        title_row.addWidget(self.mount_badge)
        device.addLayout(title_row)
        illustration = DeviceIllustration()
        illustration.setAccessibleName("电脑与手机接力存储")
        device.addWidget(illustration)
        device.addWidget(label("共享 WebDAV 连接", "small"))
        self.nas_label = ElidedLabel("尚未配置地址", "mono")
        device.addWidget(self.nas_label)
        drive_row = QHBoxLayout()
        drive_row.addWidget(label("本机网络盘", "bodyMuted"))
        self.drive_label = label("未挂载", "bodyMuted")
        drive_row.addWidget(self.drive_label, 1, Qt.AlignmentFlag.AlignRight)
        device.addLayout(drive_row)
        self.open_drive_button = button("打开网络盘", "secondary", "folder")
        self.open_drive_button.clicked.connect(self.open_drive_requested)
        device.addWidget(self.open_drive_button)
        disclaimer = label("挂载是本机盘符状态，不代表手机服务在线。连接结果以实际任务执行为准。", "small")
        disclaimer.setWordWrap(True)
        device.addWidget(disclaimer)
        rail_layout.addWidget(self.device_card)
        self.events_card = Card()
        self.events_layout = QVBoxLayout(self.events_card)
        self.events_layout.setContentsMargins(18, 18, 18, 18)
        self.events_layout.setSpacing(15)
        self.events_layout.addWidget(label("最近活动", "heading"))
        self.events_body = QWidget()
        self.events_body_layout = QVBoxLayout(self.events_body)
        self.events_body_layout.setContentsMargins(0, 0, 0, 0)
        self.events_body_layout.setSpacing(14)
        self.events_layout.addWidget(self.events_body)
        rail_layout.addWidget(self.events_card)
        rail_layout.addStretch(1)

    def load(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        position = self.scroll.verticalScrollBar().value()
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.cards = {}
        for index, task in enumerate(self.config.tasks):
            card = TaskCard(task, index, self.cards_widget)
            for name in ("sync_requested", "upload_requested", "download_requested", "preview_requested", "return_requested", "adopt_requested", "cancel_requested", "recover_requested", "edit_requested", "toggle_requested", "remove_requested", "history_requested"):
                getattr(card, name).connect(getattr(self, name))
            card.set_runtime(self._states.get(task.id, {}), self._next_runs.get(task.id, ""), self._paused)
            card.set_interaction_busy(self._interaction_busy)
            self.cards_layout.addWidget(card)
            self.cards[task.id] = card
        self.nas_label.setText(self.config.url or "尚未配置地址")
        self._apply_filter()
        self._update_summary()
        self._update_device()
        self._update_events()
        self.scroll.verticalScrollBar().setValue(position)

    def set_runtime(self, states: dict[str, dict], next_runs: dict[str, str], paused: bool, mount_state: str, drive: str, events: list[dict]) -> None:
        self._states = dict(states or {})
        self._next_runs = dict(next_runs or {})
        self._paused = bool(paused)
        self._mount_state = str(mount_state)
        self._drive = str(drive)
        self._events = list(events or [])
        for task_id, card in self.cards.items():
            card.set_runtime(self._states.get(task_id, {}), self._next_runs.get(task_id, ""), self._paused)
        self._update_summary()
        self._update_device()
        self._update_events()

    def set_interaction_busy(self, busy: bool) -> None:
        """Gate workspace actions while preserving the active task's cancel action."""
        self._interaction_busy = bool(busy)
        for control in (self.add_button, self.empty_add_button, self.batch_button, self.pause_button):
            control.setEnabled(not self._interaction_busy)
        for card in self.cards.values():
            card.set_interaction_busy(self._interaction_busy)

    def _set_filter(self, key: str) -> None:
        self._filter = key
        self._apply_filter()

    def _apply_filter(self, *_args) -> None:
        query = self.search_edit.text().strip().casefold()
        visible = 0
        for card in self.cards.values():
            task = card.task
            matches = (self._filter == "all" or (self._filter == "active" and task.enabled) or (self._filter == "paused" and not task.enabled))
            matches = matches and (not query or query in f"{task.name} {task.local_path} {task.remote_path}".casefold())
            card.setVisible(matches)
            visible += int(matches)
        empty = not self.cards
        self.empty_card.setVisible(visible == 0)
        self.empty_title.setText("从一个重要文件夹开始" if empty else "没有匹配的任务")
        self.empty_note.setText("添加文件夹，为它建立独立的镜像与旧文件历史。" if empty else "试试其他关键词，或切换到“全部”查看任务。")
        self.empty_add_button.setVisible(empty)

    def _update_summary(self) -> None:
        tasks = tuple(self.config.tasks)
        enabled = sum(bool(task.enabled) for task in tasks)
        self.task_stat.set_value(str(len(tasks)), "个文件夹", f"{enabled} 个已启用 · {len(tasks) - enabled} 个已暂停")
        running = sum(value.get("state") in ("running", "preview", "cancelling") for value in self._states.values())
        queued = sum(value.get("state") == "queued" for value in self._states.values())
        if running:
            self.schedule_stat.set_value("执行中", "", f"{running} 个正在执行 · {queued} 个等待")
        elif tasks and not any(task.auto_sync for task in tasks):
            self.schedule_stat.set_value("仅手动", "", "已启用任务可手动上传或下载，执行前先查看差异并确认")
        elif self._paused:
            self.schedule_stat.set_value("已暂停", "自动计划", "已启用任务仍可手动查看差异、上传或拉取")
        else:
            next_runs = [
                str(value) for key, value in self._next_runs.items()
                if value and key in self.cards and self.cards[key].task.enabled and self.cards[key].task.auto_sync
            ]
            next_run = min(next_runs) if next_runs else "未计划"
            self.schedule_stat.set_value(next_run, "下次同步" if next_runs else "", f"{queued} 个任务等待执行" if queued else "任务串行执行，不抢占带宽")
        self.pause_button.setText("继续自动计划" if self._paused else "暂停自动计划")
        self.pause_button.setIcon(icon("play" if self._paused else "pause", "#7b8caa", 18))
        self.filter_buttons["all"].setText(f"全部 {len(tasks)}")
        self.filter_buttons["active"].setText(f"已启用 {enabled}")
        self.filter_buttons["paused"].setText(f"已暂停 {len(tasks) - enabled}")

    def _update_device(self) -> None:
        states = {
            "stopped": ("未挂载", "neutral"),
            "starting": ("挂载中", "blue"),
            "mounted": ("已挂载", "neutral"),
            "retrying": ("重试中", "amber"),
            "error": ("挂载失败", "error"),
        }
        text, tone = states.get(self._mount_state, (self._mount_state or "未知", "neutral"))
        self.mount_badge.set_status(text, tone)
        self.drive_label.setText(self._drive if self._mount_state == "mounted" else text)
        self.open_drive_button.setEnabled(self._mount_state == "mounted" and bool(self._drive))
        self.open_drive_button.setToolTip("打开本机已挂载的盘符" if self._mount_state == "mounted" else "请先在总览页挂载网络盘")

    def _update_events(self) -> None:
        recent = self._events[:4]
        snapshot = repr(recent)
        if snapshot == self._event_snapshot:
            return
        self._event_snapshot = snapshot
        while self.events_body_layout.count():
            item = self.events_body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not recent:
            empty = label("还没有任务活动。查看差异或执行任务后，结果会出现在这里。", "bodyMuted")
            empty.setWordWrap(True)
            self.events_body_layout.addWidget(empty)
            return
        for event in recent:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(9)
            dot = label("•", "eventDot")
            dot.setAlignment(Qt.AlignmentFlag.AlignTop)
            layout.addWidget(dot)
            words = QVBoxLayout()
            words.setSpacing(4)
            title = str(event.get("task_name") or event.get("task") or event.get("text") or event.get("message") or "任务活动")
            primary = label(title, "bodyMuted")
            primary.setWordWrap(True)
            words.addWidget(primary)
            time = str(event.get("time") or event.get("timestamp") or "")
            description = str(event.get("text") or event.get("message") or event.get("detail") or "")
            if title == description:
                description = str(event.get("detail") or "")
            secondary = label(" · ".join(part for part in (description, time) if part), "small")
            secondary.setWordWrap(True)
            words.addWidget(secondary)
            layout.addLayout(words, 1)
            self.events_body_layout.addWidget(row)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        width = self.width()
        narrow = width < 1020
        if narrow != self._responsive_mode:
            self._responsive_mode = narrow
            self.workspace_layout.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
            self.rail.setMaximumWidth(16777215 if narrow else 272)
            self.rail.setMinimumWidth(0 if narrow else 250)
            self.workspace_layout.setAlignment(self.rail, Qt.AlignmentFlag(0) if narrow else Qt.AlignmentFlag.AlignTop)
        # The filter/search toolbar wraps before it can widen the scroll canvas.
        self.toolbar_layout.setDirection(QBoxLayout.Direction.TopToBottom if width < 680 else QBoxLayout.Direction.LeftToRight)
        self.toolbar_layout.setAlignment(self.search_edit, Qt.AlignmentFlag.AlignLeft if width < 680 else Qt.AlignmentFlag(0))
        columns = 1 if width < 600 else 3
        if columns != self._stat_columns:
            self._stat_columns = columns
            for stat in self.stats:
                self.stats_layout.removeWidget(stat)
            for index in range(3):
                self.stats_layout.setColumnStretch(index, 1 if columns == 3 or index == 0 else 0)
            for index, stat in enumerate(self.stats):
                self.stats_layout.addWidget(stat, index // columns, index % columns)
