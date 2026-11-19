from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QThread, Qt, Slot
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from app import constants as C
from app.core.config import Config
from app.core.filenames import display_path, portable_name_key
from app.core.rclone import Rclone
from app.core.tasks import normalize_remote_path
from app.ui.pages.settings import _operational
from app.ui.widgets import button, label


class _DirectoryListing(QThread):
    """One read-only request. No widgets or shared mount configuration."""

    def __init__(self, snapshot: Config, remote_path: str, parent: QObject):
        super().__init__(parent)
        self.snapshot = snapshot
        self.remote_path = remote_path
        self.paths: tuple[str, ...] = ()
        self.error = ""
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._client: Rclone | None = None

    @Slot()
    def cancel(self) -> None:
        self.cancel_event.set()
        self.requestInterruption()
        with self._lock:
            client = self._client
        if client is not None:
            client.cancel()

    def run(self) -> None:
        password = ""
        try:
            if self.cancel_event.is_set():
                return
            if self.snapshot.load_error:
                raise ValueError("连接配置读取失败，请先修复连接设置。")
            password = self.snapshot.password
            with tempfile.TemporaryDirectory(prefix="PhoneNAS-browse-") as root:
                client = Rclone(self.snapshot, conf_path=Path(root) / "rclone.conf")
                with self._lock:
                    self._client = client
                if self.cancel_event.is_set():
                    client.cancel()
                    return
                client.ensure_conf()
                if self.cancel_event.is_set():
                    return
                result = client.run(
                    ["lsjson", f"{C.DEFAULT_REMOTE}:{self.remote_path}",
                     "--dirs-only", "--max-depth", "1", "--no-modtime", "--no-mimetype",
                     "--timeout", "20s", "--contimeout", "10s", "--retries", "1",
                     "--low-level-retries", "1"],
                    timeout=35, cancel_event=self.cancel_event,
                )
                if self.cancel_event.is_set():
                    return
                if not result.ok:
                    raise ValueError(result.err or result.out or "无法读取手机目录。")
                entries = json.loads(result.out)
                if not isinstance(entries, list):
                    raise ValueError("手机返回的目录列表格式无效。")
                names: dict[str, str] = {}
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("IsDir") is not True:
                        continue
                    name = entry.get("Path")
                    if not isinstance(name, str) or "/" in name or "\\" in name:
                        continue
                    try:
                        normalized = normalize_remote_path(name)
                    except ValueError:
                        continue
                    if name != normalized:
                        continue
                    key = portable_name_key(name)
                    if key in names and names[key] != name:
                        raise ValueError("手机目录有大小写或 Unicode 名称冲突，无法安全选择。")
                    names[key] = name
                self.paths = tuple(
                    f"{self.remote_path}/{name}" if self.remote_path else name
                    for name in sorted(names.values(), key=str.casefold)
                )
        except Exception as exc:  # noqa: BLE001
            text = str(exc) or "无法读取手机目录。"
            self.error = text.replace(password, "[密码已隐藏]") if password else text
        finally:
            with self._lock:
                self._client = None


class _ListingJobs(QObject):
    """Application-owned threads outlive cancelled or deleted picker windows."""

    def __init__(self, application: QApplication):
        super().__init__(application)
        self.workers: set[_DirectoryListing] = set()
        application.aboutToQuit.connect(self.shutdown)

    def create(self, snapshot: Config, path: str) -> _DirectoryListing:
        worker = _DirectoryListing(snapshot, path, self)
        self.workers.add(worker)
        worker.finished.connect(self._reap)
        return worker

    @Slot()
    def _reap(self) -> None:
        worker = self.sender()
        if worker in self.workers:
            worker.wait()
            self.workers.remove(worker)
            worker.deleteLater()

    @Slot()
    def shutdown(self) -> None:
        # Rclone cooperatively kills its process on cancellation. Never destroy a
        # running QThread, even if its window or parent has already disappeared.
        for worker in tuple(self.workers):
            worker.cancel()
        for worker in tuple(self.workers):
            worker.wait()
            self.workers.discard(worker)
            worker.deleteLater()


def _jobs() -> _ListingJobs:
    application = QApplication.instance()
    if application is None:
        raise RuntimeError("目录选择器需要 QApplication。")
    registry = getattr(application, "_phonenas_listing_jobs", None)
    if registry is None:
        registry = _ListingJobs(application)
        application._phonenas_listing_jobs = registry
    return registry


class RemoteFolderDialog(QDialog):
    """Choose an existing relative directory. Opening this dialog does no I/O.

    Only explicit refresh/navigation clicks issue a shallow lsjson request. The
    accepted ``remote_path`` is an existing directory returned by that request;
    the NAS root, hand-entered paths and untrusted traversal entries cannot pass.
    """

    def __init__(self, config: Config, parent=None, operational: bool = True):
        super().__init__(parent)
        self.config = config
        self.operational = operational
        self.remote_path = ""
        self._current_path = ""
        self._loaded = False
        self._closed = False
        self._worker: _DirectoryListing | None = None
        self.setWindowTitle("选择手机上已有的数据集镜像 · PhoneNAS")
        self.setModal(True)
        available = self.screen().availableGeometry()
        width, height = max(240, available.width() - 32), max(220, available.height() - 48)
        self.setMinimumSize(min(460, width), min(320, height))
        self.resize(min(660, width), min(530, height))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = label("选择已有的镜像目录", "dialogTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        hint = label("仅浏览手机上已有的目录，不会创建、删除或同步任何文件。点击“读取手机目录”后才会连接。请选择数据集的当前镜像，不是历史目录。", "bodyMuted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.path_label = label(f"{C.DEFAULT_REMOTE}:/", "mono")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)
        toolbar = QHBoxLayout()
        self.root_button = button("根目录", "subtle")
        self.up_button = button("上一级", "subtle", "up")
        self.refresh_button = button("读取手机目录", "secondary", "folder")
        self.root_button.clicked.connect(lambda: self._load(""))
        self.up_button.clicked.connect(self._up)
        self.refresh_button.clicked.connect(lambda: self._load(self._current_path))
        toolbar.addWidget(self.root_button)
        toolbar.addWidget(self.up_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)
        self.tree = QTreeWidget()
        self.tree.setObjectName("remoteDirectories")
        self.tree.setHeaderLabels(["目录（双击进入）"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._refresh_controls)
        self.tree.itemDoubleClicked.connect(self._enter)
        layout.addWidget(self.tree, 1)
        self.status_label = label("尚未读取手机目录。", "bodyMuted")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)
        footer = QHBoxLayout()
        self.cancel_button = button("取消")
        self.choose_button = button("选择此目录", "primary", "check")
        self.cancel_button.clicked.connect(self.reject)
        self.choose_button.clicked.connect(self.accept)
        footer.addStretch(1)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.choose_button)
        layout.addLayout(footer)
        self._refresh_controls()

    @property
    def busy(self) -> bool:
        return self._worker is not None

    def _refresh_controls(self) -> None:
        allowed = _operational(self) and not self.busy and not self._closed
        self.refresh_button.setEnabled(allowed)
        self.root_button.setEnabled(allowed and bool(self._current_path))
        self.up_button.setEnabled(allowed and bool(self._current_path))
        self.tree.setEnabled(not self.busy and not self._closed)
        selected = self.tree.currentItem()
        self.choose_button.setEnabled(
            not self.busy and not self._closed and self._loaded
            and (selected is not None or bool(self._current_path))
        )
        self.choose_button.setText("选择此目录" if selected else "使用当前目录")
        if not _operational(self):
            self.status_label.setText("安全预览模式：不会读取或连接手机目录；可返回任务设置手动填写已有目录。")

    def _load(self, path: str) -> None:
        if self.busy or self._closed or not _operational(self):
            self._refresh_controls()
            return
        if path:
            try:
                if normalize_remote_path(path) != path:
                    return
            except ValueError:
                return
        self._loaded = False
        self.tree.clear()
        worker = _jobs().create(self.config.snapshot(), path)
        self._worker = worker
        self.destroyed.connect(worker.cancel)
        worker.finished.connect(self._listing_finished)
        self.status_label.setText("正在只读读取目录…关闭窗口会取消本次读取。")
        self._refresh_controls()
        worker.start()

    @Slot()
    def _listing_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None or self._closed:
            return
        if worker.cancel_event.is_set():
            self.status_label.setText("已取消读取手机目录。")
        elif worker.error:
            self.status_label.setText(f"读取失败：{worker.error}")
        else:
            self._current_path = worker.remote_path
            self._loaded = True
            self.path_label.setText(f"{C.DEFAULT_REMOTE}:/{display_path(self._current_path)}")
            for path in worker.paths:
                item = QTreeWidgetItem([display_path(path.rsplit("/", 1)[-1])])
                item.setData(0, Qt.ItemDataRole.UserRole, path)
                item.setToolTip(0, display_path(path))
                self.tree.addTopLevelItem(item)
            self.status_label.setText(f"{len(worker.paths)} 个子目录。选中镜像目录后确认；双击可进入。" if worker.paths else "当前目录没有子目录。可选择当前目录或返回上一级。")
            self.refresh_button.setText("刷新当前目录")
        self._refresh_controls()

    def _enter(self, item: QTreeWidgetItem, _column: int) -> None:
        self._load(str(item.data(0, Qt.ItemDataRole.UserRole)))

    def _up(self) -> None:
        self._load(self._current_path.rpartition("/")[0])

    def accept(self) -> None:
        if self.busy or self._closed or not self._loaded:
            return
        item = self.tree.currentItem()
        path = item.data(0, Qt.ItemDataRole.UserRole) if item else self._current_path
        if not isinstance(path, str) or not path:
            return
        try:
            if normalize_remote_path(path) != path:
                return
        except ValueError:
            return
        self.remote_path = path
        self._closed = True
        super().accept()

    def reject(self) -> None:
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
        super().reject()

    def done(self, result: int) -> None:
        if result == QDialog.DialogCode.Accepted:
            if not self.remote_path:
                self.accept()
                return
        else:
            self._closed = True
            if self._worker is not None:
                self._worker.cancel()
        super().done(result)

    def event(self, event) -> bool:
        result = super().event(event)
        if event.type() == QEvent.Type.DynamicPropertyChange and hasattr(self, "refresh_button"):
            if not _operational(self) and self._worker is not None:
                self._worker.cancel()
            self._refresh_controls()
        return result
