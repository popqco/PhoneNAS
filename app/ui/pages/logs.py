from __future__ import annotations

from collections import deque
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.log import log_dir
from app.ui.widgets import Badge, Card, ElidedLabel, Header, button, label


_MAX_TAIL_BYTES = 256 * 1024


def _tail(path: Path, lines: int) -> str:
    """Bounded tail: rotated or large logs never load wholly into the GUI."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            start = max(0, end - _MAX_TAIL_BYTES)
            handle.seek(start)
            content = handle.read(_MAX_TAIL_BYTES)
        if start:
            _, _, content = content.partition(b"\n")
        return "\n".join(content.decode("utf-8-sig", errors="replace").splitlines()[-lines:])
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"读取失败：{exc}"


class _LogReader(QThread):
    loaded = Signal(str, object, str)

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self) -> None:
        # Resolve the process-isolated logging directory only when explicitly
        # refreshed. Constructing this page never creates or reads APPDATA.
        try:
            folder = Path(log_dir())
            files = {"app.log": _tail(folder / "app.log", 700)}
            recent = sorted(folder.glob("sync-*.log"), key=lambda path: path.name, reverse=True)[:6]
            for path in recent:
                if self.isInterruptionRequested():
                    return
                files[path.name] = _tail(path, 400)
            self.loaded.emit(str(folder), files, "")
        except OSError as exc:
            self.loaded.emit("", {}, f"无法读取日志目录：{exc}")


class LogsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workspacePage")
        self.setMinimumWidth(0)
        self._folder: Path | None = None
        self._reader: _LogReader | None = None
        self._files: dict[str, str] = {}
        self._events: deque[dict] = deque(maxlen=500)
        self._event_keys: deque[str] = deque(maxlen=500)
        self._pending_events: list[str] = []
        self._event_timer = QTimer(self)
        self._event_timer.setSingleShot(True)
        self._event_timer.setInterval(60)
        self._event_timer.timeout.connect(self._flush_events)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 9, 20)
        layout.setSpacing(22)
        self.header = Header("活动记录", "真实的任务事件与运行日志。这里只读，不修改任何文件。")
        self.refresh_button = button("刷新日志", "secondary", "sync")
        self.open_button = button("打开日志目录", "secondary", "folder")
        self.open_button.setEnabled(False)
        self.refresh_button.clicked.connect(self.refresh)
        self.open_button.clicked.connect(self._open_folder)
        self.header.actions.addWidget(self.refresh_button)
        self.header.actions.addWidget(self.open_button)
        layout.addWidget(self.header)
        self.card = Card()
        card = QVBoxLayout(self.card)
        card.setContentsMargins(20, 18, 20, 18)
        card.setSpacing(13)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setMinimumWidth(0)
        card.addWidget(self.tabs, 1)
        activity = QWidget()
        activity_layout = QVBoxLayout(activity)
        activity_layout.setContentsMargins(0, 10, 0, 0)
        activity_layout.setSpacing(13)
        events_head = QHBoxLayout()
        events_head.addWidget(label("最近任务与连接事件", "heading"), 1)
        self.events_badge = Badge("0 条记录", "neutral")
        events_head.addWidget(self.events_badge)
        activity_layout.addLayout(events_head)
        self.events_text = QPlainTextEdit()
        self.events_text.setObjectName("logText")
        self.events_text.setReadOnly(True)
        self.events_text.setMaximumBlockCount(2000)
        self.events_text.setMinimumHeight(170)
        self.events_text.setPlaceholderText("还没有任务活动。预演、同步和连接事件会显示在这里。")
        self.events_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        activity_layout.addWidget(self.events_text, 1)
        activity_layout.addWidget(label("保留最近 500 条事件；详细过程可切换到“运行日志”。", "small"))
        self.tabs.addTab(activity, "最近活动")
        logs = QWidget()
        logs_layout = QVBoxLayout(logs)
        logs_layout.setContentsMargins(0, 10, 0, 0)
        logs_layout.setSpacing(13)
        file_row = QHBoxLayout()
        file_row.setSpacing(10)
        self.file_combo = QComboBox()
        self.file_combo.setObjectName("logFileSelector")
        self.file_combo.setMinimumHeight(40)
        self.file_combo.setMinimumWidth(170)
        self.file_combo.setMaximumWidth(320)
        self.file_combo.addItem("app.log")
        self.file_combo.currentTextChanged.connect(self._show_file)
        self.path_label = ElidedLabel("刷新后显示当前运行环境的日志目录", "mono")
        file_row.addWidget(self.file_combo)
        file_row.addWidget(self.path_label, 1)
        logs_layout.addLayout(file_row)
        self.text = QPlainTextEdit()
        self.text.setObjectName("logText")
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(2400)
        self.text.setMinimumHeight(170)
        self.text.setPlaceholderText("点击“刷新日志”，读取当前运行环境的真实日志。")
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        logs_layout.addWidget(self.text, 1)
        self.log_status = label("只读取日志尾部，避免大文件阻塞界面。", "small")
        self.log_status.setWordWrap(True)
        logs_layout.addWidget(self.log_status)
        self.tabs.addTab(logs, "运行日志")
        layout.addWidget(self.card, 1)

    def refresh(self) -> None:
        if self._reader is not None:
            return
        self.refresh_button.setEnabled(False)
        self.log_status.setText("正在读取最近日志…")
        reader = _LogReader(self)
        self._reader = reader
        reader.loaded.connect(self._loaded)
        reader.finished.connect(self._reader_finished)
        reader.finished.connect(reader.deleteLater)
        reader.start()

    def _reader_finished(self) -> None:
        self._reader = None
        self.refresh_button.setEnabled(True)

    def _loaded(self, folder: str, files: dict, error: str) -> None:
        if folder:
            self._folder = Path(folder)
            self.path_label.setText(folder)
            self.open_button.setEnabled(True)
        selected = self.file_combo.currentText()
        self._files = dict(files)
        names = list(self._files)
        if names != [self.file_combo.itemText(index) for index in range(self.file_combo.count())]:
            self.file_combo.blockSignals(True)
            self.file_combo.clear()
            self.file_combo.addItems(names)
            index = self.file_combo.findText(selected)
            self.file_combo.setCurrentIndex(max(0, index))
            self.file_combo.blockSignals(False)
        self._show_file(self.file_combo.currentText())
        self.log_status.setText(error or f"已读取 {len(files)} 个日志文件的最近内容 · 单文件最多 256 KiB")

    def _show_file(self, name: str) -> None:
        content = self._files.get(name, "")
        self._set_text_preserving_view(self.text, content or "（日志为空，尚无运行输出）")

    @staticmethod
    def _set_text_preserving_view(editor: QPlainTextEdit, content: str) -> None:
        if editor.toPlainText() == content:
            return
        bar = editor.verticalScrollBar()
        old_value = bar.value()
        bottom = old_value >= bar.maximum() - 2
        cursor = editor.textCursor()
        position, anchor = cursor.position(), cursor.anchor()
        editor.setPlainText(content)
        limit = max(0, editor.document().characterCount() - 1)
        cursor.setPosition(min(anchor, limit))
        cursor.setPosition(min(position, limit), cursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        bar.setValue(bar.maximum() if bottom else min(old_value, bar.maximum()))

    def append_event(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        item = dict(event)
        key = str(item.get("id") or repr(sorted((str(k), str(v)) for k, v in item.items())))
        if key in self._event_keys:
            return
        self._events.append(item)
        self._event_keys.append(key)
        stamp = str(item.get("time") or item.get("timestamp") or "")
        task = str(item.get("task_name") or item.get("task") or "")
        text = str(item.get("text") or item.get("message") or "任务活动")
        detail = str(item.get("detail") or "")
        level = str(item.get("level") or item.get("state") or "")
        heading = "  ".join(part for part in (stamp, f"[{level}]" if level else "", task, text) if part)
        self._pending_events.append(heading + ("\n    " + detail if detail else ""))
        self.events_badge.setText(f"{len(self._events)} 条记录")
        if not self._event_timer.isActive():
            self._event_timer.start()

    def _flush_events(self) -> None:
        if not self._pending_events:
            return
        content = "\n\n".join(self._pending_events)
        self._pending_events.clear()
        bar = self.events_text.verticalScrollBar()
        old_value = bar.value()
        bottom = old_value >= bar.maximum() - 2
        cursor = self.events_text.textCursor()
        self.events_text.appendPlainText(content)
        self.events_text.setTextCursor(cursor)
        bar.setValue(bar.maximum() if bottom else old_value)

    def _open_folder(self) -> None:
        if self._folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._folder)))

    def shutdown(self) -> bool:
        """The owner can retry quitting after the reader's finished signal."""
        self._event_timer.stop()
        reader = self._reader
        if reader is None:
            return True
        reader.requestInterruption()
        return False

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self.shutdown():
            event.ignore()
            return
        super().closeEvent(event)
