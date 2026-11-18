from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.ui.widgets import Badge, Card, DeviceIllustration, ElidedLabel, Header, StatCard, button, icon, label


class StatusPage(QWidget):
    sync_requested = Signal()
    mount_toggle_requested = Signal()
    pause_toggle_requested = Signal()
    tasks_requested = Signal()
    open_drive_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workspacePage")
        self.setMinimumWidth(0)
        self._paused = False
        self._mount_state = "stopped"
        self._drive = ""
        self._tasks = ()
        self._states = {}
        self._queue_count = 0
        self._sync_available = True
        self._summary_key = None
        self._stat_columns = None
        self._last = ""
        self._next = ""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("pageScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll)
        self.canvas = QWidget()
        self.canvas.setObjectName("pageCanvas")
        self.canvas.setMinimumWidth(0)
        body = QVBoxLayout(self.canvas)
        body.setContentsMargins(0, 0, 9, 20)
        body.setSpacing(22)
        self.header = Header("总览", "连接状态、文件夹保护与执行计划，一眼看清。")
        self.tasks_button = button("管理同步任务", "primary", "folders")
        self.tasks_button.clicked.connect(self.tasks_requested)
        self.header.actions.addWidget(self.tasks_button)
        body.addWidget(self.header)
        self.stat_widget = QWidget()
        self.stats_layout = QGridLayout(self.stat_widget)
        self.stats_layout.setContentsMargins(0, 0, 0, 0)
        self.stats_layout.setSpacing(13)
        self.task_stat = StatCard("folders", "0", "个已启用", "共 0 个独立任务")
        self.queue_stat = StatCard("activity", "0", "个等待", "当前没有任务正在执行")
        self.plan_stat = StatCard("clock", "未计划", "", "每个文件夹单独计时")
        self.stats = (self.task_stat, self.queue_stat, self.plan_stat)
        for index, stat in enumerate(self.stats):
            self.stats_layout.addWidget(stat, 0, index)
            self.stats_layout.setColumnStretch(index, 1)
        body.addWidget(self.stat_widget)

        columns = QWidget()
        columns.setMinimumWidth(0)
        self.columns_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, columns)
        self.columns_layout.setContentsMargins(0, 0, 0, 0)
        self.columns_layout.setSpacing(20)
        self.connection_card = Card()
        connection = QVBoxLayout(self.connection_card)
        connection.setContentsMargins(22, 21, 22, 21)
        connection.setSpacing(16)
        head = QHBoxLayout()
        head.addWidget(label("手机存储与网络盘", "heading"), 1)
        self.mount_badge = Badge("未挂载")
        head.addWidget(self.mount_badge)
        connection.addLayout(head)
        connection.addWidget(DeviceIllustration())
        connection.addWidget(label("WebDAV 地址", "field"))
        self.nas_label = ElidedLabel("尚未配置地址", "mono")
        connection.addWidget(self.nas_label)
        self.mount_label = label("本机网络盘未挂载", "bodyMuted")
        self.mount_label.setWordWrap(True)
        connection.addWidget(self.mount_label)
        self.winfsp_label = label("WinFsp：尚未检查", "bodyMuted")
        connection.addWidget(self.winfsp_label)
        connection_actions = QWidget()
        self.connection_actions = QBoxLayout(QBoxLayout.Direction.LeftToRight, connection_actions)
        self.connection_actions.setContentsMargins(0, 0, 0, 0)
        self.connection_actions.setSpacing(9)
        self.mount_button = button("挂载网络盘", "secondary", "disk")
        self.open_drive_button = button("打开网络盘", "secondary", "folder")
        self.open_drive_button.setEnabled(False)
        self.mount_button.clicked.connect(self.mount_toggle_requested)
        self.open_drive_button.clicked.connect(self.open_drive_requested)
        self.connection_actions.addWidget(self.mount_button)
        self.connection_actions.addWidget(self.open_drive_button)
        connection.addWidget(connection_actions)
        note = label("本机盘符已挂载，不等于手机服务在线。同步通过共享 WebDAV 连接执行，连接情况以实际任务结果为准。", "small")
        note.setWordWrap(True)
        connection.addWidget(note)
        connection.addStretch(1)
        self.columns_layout.addWidget(self.connection_card, 1)

        self.protection_card = Card()
        protection = QVBoxLayout(self.protection_card)
        protection.setContentsMargins(22, 21, 22, 21)
        protection.setSpacing(16)
        protection_header = QHBoxLayout()
        protection_header.addWidget(label("文件夹保护", "heading"), 1)
        protection_header.addWidget(Badge("单向镜像 + 旧文件历史", "blue"))
        protection.addLayout(protection_header)
        summary_row = QHBoxLayout()
        summary_row.setSpacing(10)
        self.enabled_label = label("0", "heroMetric")
        self.total_label = label("/ 0 个文件夹已启用", "bodyMuted")
        summary_row.addWidget(self.enabled_label)
        summary_row.addWidget(self.total_label, 1, Qt.AlignmentFlag.AlignVCenter)
        protection.addLayout(summary_row)
        policy = label("每个文件夹拥有独立的目标目录、同步频率、排除规则与保留天数。被覆盖或删除的旧文件会先移入专属历史目录。", "bodyMuted")
        policy.setWordWrap(True)
        protection.addWidget(policy)
        self.task_list = QWidget()
        self.task_list.setMinimumWidth(0)
        self.task_list_layout = QVBoxLayout(self.task_list)
        self.task_list_layout.setContentsMargins(0, 0, 0, 0)
        self.task_list_layout.setSpacing(12)
        protection.addWidget(self.task_list)
        protection.addStretch(1)
        action_widget = QWidget()
        self.sync_actions = QBoxLayout(QBoxLayout.Direction.LeftToRight, action_widget)
        self.sync_actions.setContentsMargins(0, 0, 0, 0)
        self.sync_actions.setSpacing(9)
        self.sync_button = button("同步全部", "primary", "sync")
        self.pause_button = button("暂停计划", "secondary", "pause")
        self.sync_button.clicked.connect(self.sync_requested)
        self.pause_button.clicked.connect(self.pause_toggle_requested)
        self.sync_actions.addWidget(self.sync_button)
        self.sync_actions.addWidget(self.pause_button)
        protection.addWidget(action_widget)
        self.columns_layout.addWidget(self.protection_card, 1)
        body.addWidget(columns)

        self.execution_card = Card()
        execution = QVBoxLayout(self.execution_card)
        execution.setContentsMargins(21, 18, 21, 18)
        execution.setSpacing(12)
        execution.addWidget(label("计划与最近结果", "heading"))
        self.state_label = label("就绪。添加文件夹后，可先预演再同步。", "bodyMuted")
        self.state_label.setWordWrap(True)
        execution.addWidget(self.state_label)
        dates = QWidget()
        self.dates_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, dates)
        self.dates_layout.setContentsMargins(0, 0, 0, 0)
        self.dates_layout.setSpacing(20)
        self.last_sync_label = label("上次成功：尚未同步", "small")
        self.last_sync_label.setWordWrap(True)
        self.next_sync_label = label("下次计划：未计划", "small")
        self.next_sync_label.setWordWrap(True)
        self.dates_layout.addWidget(self.last_sync_label, 1)
        self.dates_layout.addWidget(self.next_sync_label, 1)
        execution.addWidget(dates)
        self.output = QPlainTextEdit()
        self.output.setObjectName("logText")
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(40)
        self.output.setMaximumHeight(98)
        self.output.setMinimumHeight(75)
        self.output.setPlaceholderText("尚无运行输出。完整运行记录请到“活动记录”查看。")
        self.output.hide()
        execution.addWidget(self.output)
        footnote = label("这里只展示最近输出，完整任务事件与日志在“活动记录”中保留。", "small")
        footnote.setWordWrap(True)
        execution.addWidget(footnote)
        body.addWidget(self.execution_card)
        body.addStretch(1)
        self.scroll.setWidget(self.canvas)
        self.set_summary((), {})

    def set_nas(self, url: str) -> None:
        self.nas_label.setText(url or "尚未配置地址")

    def set_winfsp(self, installed: bool) -> None:
        self.winfsp_label.setText("WinFsp：已安装" if installed else "WinFsp：未安装，挂载网络盘前需要安装")
        self.winfsp_label.setWordWrap(True)

    def set_mount(self, state: str, drive: str) -> None:
        self._mount_state, self._drive = state, drive
        text, tone = {
            "stopped": ("未挂载", "neutral"),
            "starting": ("挂载中", "blue"),
            "mounted": ("已挂载", "neutral"),
            "retrying": ("重新挂载中", "amber"),
            "error": ("挂载失败", "error"),
        }.get(state, (state or "未知", "neutral"))
        self.mount_badge.set_status(text, tone)
        self.mount_label.setText(f"本机网络盘：{drive} · 已挂载" if state == "mounted" else f"本机网络盘：{text}")
        self.mount_button.setText("卸载网络盘" if state in ("mounted", "starting", "retrying") else "挂载网络盘")
        self.open_drive_button.setEnabled(state == "mounted" and bool(drive))

    def set_sync(self, last: str, next_run: str, paused: bool) -> None:
        self._last, self._next, self._paused = last, next_run, bool(paused)
        self.last_sync_label.setText(f"上次成功：{last or '尚未同步'}")
        self._update_plan()
        self._update_state()

    def _update_plan(self) -> None:
        enabled = tuple(task for task in self._tasks if task.enabled)
        automatic = any(task.auto_sync for task in enabled)
        if not automatic:
            value = "仅手动" if enabled else "未计划"
            self.next_sync_label.setText(f"下次自动计划：{value}")
            self.plan_stat.set_value(value, "", "手动执行仍可使用" if enabled else "没有已启用的自动任务")
        else:
            value = "已暂停" if self._paused else self._next or "未计划"
            self.next_sync_label.setText(f"下次自动计划：{value}")
            self.plan_stat.set_value(value, "自动计划" if self._paused else "下次执行" if self._next else "", "手动同步仍可使用" if self._paused else "仅自动任务参与计划")
        self.pause_button.setEnabled(self._sync_available and automatic)
        self.pause_button.setText("继续自动计划" if self._paused else "暂停自动计划")
        self.pause_button.setIcon(icon("play" if self._paused else "pause", "#7b8caa", 18))

    def set_summary(self, tasks, states, queue_count: int = 0, *, sync_available: bool = True) -> None:
        self._tasks = tuple(tasks or ())
        self._states = dict(states or {})
        enabled = sum(bool(task.enabled) for task in self._tasks)
        automatic = sum(task.enabled and task.auto_sync for task in self._tasks)
        running = sum(value.get("state") in ("running", "preview", "cancelling") for value in self._states.values())
        queued = max(0, int(queue_count))
        self._queue_count = queued
        self._sync_available = bool(sync_available)
        self.task_stat.set_value(str(enabled), "个已启用", f"共 {len(self._tasks)} 个任务 · {automatic} 个自动 · {enabled - automatic} 个仅手动")
        self.queue_stat.set_value(str(queued), "个等待", f"{running} 个任务正在执行" if running else "当前没有任务正在执行")
        self.enabled_label.setText(str(enabled))
        self.total_label.setText(f"/ {len(self._tasks)} 个文件夹已启用")
        self.sync_button.setEnabled(sync_available and enabled > 0)
        self._update_plan()
        self._update_state()
        key = (self._tasks, tuple((task.id, self._states.get(task.id, {}).get("state", "idle")) for task in self._tasks))
        if key == self._summary_key:
            return
        self._summary_key = key
        while self.task_list_layout.count():
            item = self.task_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._tasks:
            empty = label("还没有同步任务。点击“管理同步任务”，从一个重要文件夹开始。", "bodyMuted")
            empty.setWordWrap(True)
            self.task_list_layout.addWidget(empty)
        for task in self._tasks[:4]:
            row = QWidget()
            row.setMinimumWidth(0)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 6, 0, 6)
            layout.setSpacing(12)
            words = QVBoxLayout()
            words.setSpacing(5)
            words.addWidget(ElidedLabel(task.name, "taskTitle"))
            schedule = f"每 {task.interval_minutes} 分钟" if task.auto_sync else "仅手动"
            words.addWidget(label(f"{schedule} · 保留 {task.retention_days} 天", "small"))
            layout.addLayout(words, 1)
            state = self._states.get(task.id, {}).get("state", "idle")
            text, tone = {
                "running": ("同步中", "blue"),
                "preview": ("核对差异中", "blue"),
                "queued": ("等待中", "amber"),
                "cancelling": ("正在取消", "amber"),
                "cancelled": ("已取消", "neutral"),
                "error": ("失败", "error"),
                "guarded": ("保护阻止", "amber"),
            }.get(state, ("已停用" if not task.enabled else "已启用" if task.auto_sync else "仅手动", "neutral"))
            layout.addWidget(Badge(text, tone))
            self.task_list_layout.addWidget(row)
        if len(self._tasks) > 4:
            self.task_list_layout.addWidget(label(f"另有 {len(self._tasks) - 4} 个任务，在同步任务页查看全部。", "small"))

    def _update_state(self) -> None:
        for state, title in (("cancelling", "正在取消"), ("running", "同步中"), ("preview", "核对差异中"), ("error", "执行失败"), ("guarded", "保护阻止"), ("cancelled", "已取消")):
            for task in self._tasks:
                runtime = self._states.get(task.id, {})
                if runtime.get("state") == state:
                    message = runtime.get("message")
                    self.set_state(f"{title} · {task.name}" + (f"：{message}" if message else ""))
                    return
        if not self._sync_available:
            self.set_state("同步当前不可用，请检查运行状态与全局同步设置。")
        elif self._tasks and not any(task.enabled for task in self._tasks):
            self.set_state("没有已启用的同步任务，请先启用一个文件夹。")
        elif any(task.enabled for task in self._tasks) and not any(task.enabled and task.auto_sync for task in self._tasks):
            self.set_state("仅手动执行，未开启自动计划。可查看差异，再手动执行。" + (f"{self._queue_count} 个任务等待中。" if self._queue_count else ""))
        elif self._paused:
            self.set_state("自动计划已暂停，手动同步仍可使用。" + (f"{self._queue_count} 个任务等待中。" if self._queue_count else ""))
        elif self._queue_count:
            self.set_state(f"{self._queue_count} 个任务等待中。")
        else:
            self.set_state("就绪。可先查看差异，再执行任务。" if self._tasks else "就绪。添加文件夹后，可先查看差异，再执行任务。")

    def set_state(self, text: str) -> None:
        self.state_label.setText(text or "就绪")

    def append_output(self, text: str) -> None:
        if not text:
            return
        self.output.setVisible(True)
        bar = self.output.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 2
        value = bar.value()
        self.output.appendPlainText(str(text))
        bar.setValue(bar.maximum() if at_bottom else value)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        width = self.width()
        self.columns_layout.setDirection(QBoxLayout.Direction.TopToBottom if width < 880 else QBoxLayout.Direction.LeftToRight)
        direction = QBoxLayout.Direction.TopToBottom if width < 600 else QBoxLayout.Direction.LeftToRight
        self.dates_layout.setDirection(direction)
        self.connection_actions.setDirection(direction if width < 600 else QBoxLayout.Direction.LeftToRight)
        self.sync_actions.setDirection(direction if width < 600 else QBoxLayout.Direction.LeftToRight)
        columns = 1 if width < 600 else 3
        if columns != self._stat_columns:
            self._stat_columns = columns
            for stat in self.stats:
                self.stats_layout.removeWidget(stat)
            for index in range(3):
                self.stats_layout.setColumnStretch(index, 1 if columns == 3 or index == 0 else 0)
            for index, stat in enumerate(self.stats):
                self.stats_layout.addWidget(stat, index // columns, index % columns)
