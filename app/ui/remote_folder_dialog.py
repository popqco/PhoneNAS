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
from app.core.relay import find_published_dataset
from app.core.tasks import normalize_remote_path
from app.ui.pages.settings import _operational
from app.ui.widgets import button, label


class _DirectoryListing(QThread):
    """One read-only request. No widgets or shared mount configuration."""

    def __init__(self, snapshot: Config, remote_path: str, parent: QObject, *, check_dataset=False):
        super().__init__(parent)
        self.snapshot = snapshot
        self.remote_path = remote_path
        self.check_dataset = check_dataset
        self.dataset: dict | None = None
        self.paths: tuple[str, ...] = ()
        self.unavailable_count = 0
        self.missing = False
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
                if self.check_dataset:
                    self.dataset = find_published_dataset(client, self.remote_path, cancelled=self.cancel_event)
                    if self.dataset is None:
                        raise ValueError("该目录没有已完成的 PhoneNAS 发布。请选择发布端实际使用的手机镜像。")
                    return
                command = ["lsjson", f"{C.DEFAULT_REMOTE}:{self.remote_path}"]
                options = ["--no-modtime", "--no-mimetype", "--timeout", "20s", "--contimeout", "10s",
                           "--retries", "1", "--low-level-retries", "1"]
                result = client.run([*command, "--dirs-only", "--max-depth", "1", *options],
                                    timeout=35, cancel_event=self.cancel_event)
                if self.cancel_event.is_set():
                    return
                if not result.ok:
                    self.missing = result.code in (3, 4)
                    raise ValueError("目录不存在。" if self.missing else result.err or "无法读取手机目录。")
                entries = json.loads(result.out)
                if not isinstance(entries, list):
                    raise ValueError("手机返回的目录列表格式无效。")
                if self.remote_path and not entries:
                    # A dirs-only listing of a regular file can also succeed with [].
                    info = client.run([*command, "--stat", *options], timeout=35, cancel_event=self.cancel_event)
                    if self.cancel_event.is_set():
                        return
                    if not info.ok:
                        self.missing = info.code in (3, 4)
                        raise ValueError("目录不存在。" if self.missing else info.err or "无法确认手机目录。")
                    entry = json.loads(info.out)
                    if not isinstance(entry, dict) or entry.get("IsDir") is not True:
                        self.missing = True
                        raise ValueError("该路径不是一个目录。")
                names: dict[str, str] = {}
                for entry in entries:
                    if not isinstance(entry, dict) or type(entry.get("IsDir")) is not bool:
                        raise ValueError("手机返回的目录条目格式无效。")
                    if not entry["IsDir"]:
                        continue
                    name = entry.get("Path")
                    try:
                        if (not isinstance(name, str) or "/" in name or "\\" in name
                                or normalize_remote_path(name) != name or portable_name_key(name) == ".phonenas"):
                            raise ValueError
                    except ValueError:
                        self.unavailable_count += 1
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

    def create(self, snapshot: Config, path: str, *, check_dataset=False) -> _DirectoryListing:
        worker = _DirectoryListing(snapshot, path, self, check_dataset=check_dataset)
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
    """Choose an existing relative directory from a shallow, read-only listing.

    Show, navigation and refresh read directly from WebDAV. The NAS root and
    untrusted entries cannot be accepted as a task directory.
    """

    def __init__(self, config: Config, parent=None, operational: bool = True, *,
                 purpose: str = "mirror", current_path: str = "", require_published: bool = False,
                 browse_only: bool = False):
        super().__init__(parent)
        self._snapshot = config.snapshot()
        self.operational = operational
        self.remote_path = ""
        self.dataset: dict | None = None
        self._require_published = require_published
        self._browse_only = browse_only
        self._folder_label = "手机历史目录" if purpose == "history" else "手机镜像目录"
        try:
            self._initial_selection = normalize_remote_path(current_path)
            if ".phonenas" in map(portable_name_key, self._initial_selection.split("/")):
                self._initial_selection = ""
        except ValueError:
            self._initial_selection = ""
        self._current_path = self._initial_selection.rpartition("/")[0]
        self._loaded = False
        self._shown = False
        self._initial_view = True
        self._closed = False
        self._worker: _DirectoryListing | None = None
        title = f"{'浏览' if browse_only else '选择'}{self._folder_label}"
        self.setWindowTitle(f"{title} · PhoneNAS")
        self.setModal(True)
        available = self.screen().availableGeometry()
        width, height = max(240, available.width() - 32), max(220, available.height() - 48)
        self.setMinimumSize(min(460, width), min(320, height))
        self.resize(min(660, width), min(530, height))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = label(title, "dialogTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        if browse_only:
            meaning = "此路径随已发布的数据集确定；此处仅浏览，不改写任务。"
        elif purpose == "history":
            meaning = "请选择存放手机旧文件的归档根目录，不要选到某个日期批次里。"
        else:
            meaning = "请选择存放当前文件的目录，不要选历史归档。"
        hint = label(f"打开即从手机只读读取，不依赖挂载盘，不会创建、删除或同步文件。{meaning}", "bodyMuted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.path_label = label(f"{C.DEFAULT_REMOTE}:/{display_path(self._current_path)}", "mono")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)
        toolbar = QHBoxLayout()
        self.root_button = button("根目录", "subtle")
        self.up_button = button("上一级", "subtle", "up")
        self.refresh_button = button("刷新当前目录", "secondary", "folder")
        self.root_button.clicked.connect(lambda: self._load(""))
        self.up_button.clicked.connect(self._up)
        self.refresh_button.clicked.connect(lambda: self._load(self._current_path, initial=self._initial_view))
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
        self.cancel_button = button("关闭" if browse_only else "取消")
        self.choose_button = button("选择此目录", "primary", "check")
        self.choose_button.setVisible(not browse_only)
        self.cancel_button.clicked.connect(self.reject)
        self.choose_button.clicked.connect(self.accept)
        footer.addStretch(1)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.choose_button)
        layout.addLayout(footer)
        self._refresh_controls()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._shown:
            self._shown = True
            self._load(self._current_path, initial=True)

    @property
    def busy(self) -> bool:
        return self._worker is not None

    def _refresh_controls(self) -> None:
        allowed = _operational(self) and not self.busy and not self._closed
        self.refresh_button.setEnabled(allowed)
        self.root_button.setEnabled(allowed and bool(self._current_path))
        self.up_button.setEnabled(allowed and bool(self._current_path))
        self.tree.setEnabled(not self.busy and not self._closed)
        selected = self.tree.selectedItems()
        current_ok = bool(self._current_path) and not self._initial_view
        self.choose_button.setEnabled(allowed and not self._browse_only and self._loaded and (bool(selected) or current_ok))
        self.choose_button.setText("使用当前目录" if current_ok and not selected else "使用所选目录")
        if not _operational(self):
            self.status_label.setText("安全预览模式：不会读取或连接手机目录；可返回任务设置手动填写已有目录。")

    def _load(self, path: str, *, initial: bool = False) -> None:
        if self.busy or self._closed or not _operational(self):
            self._refresh_controls()
            return
        if path:
            try:
                if normalize_remote_path(path) != path or ".phonenas" in map(portable_name_key, path.split("/")):
                    return
            except ValueError:
                return
        self._current_path = path
        self._initial_view = initial
        self._loaded = False
        self.tree.clear()
        self.path_label.setText(f"{C.DEFAULT_REMOTE}:/{display_path(path)}")
        self.refresh_button.setText("刷新当前目录")
        self._start_request(path)

    def _start_request(self, path: str, *, check_dataset: bool = False) -> None:
        worker = _jobs().create(self._snapshot, path, check_dataset=check_dataset)
        self._worker = worker
        self.destroyed.connect(worker.cancel)
        worker.finished.connect(self._request_finished)
        status = "正在核对已发布的镜像和归档设置…" if check_dataset else "正在只读读取目录…"
        self.status_label.setText(status + "关闭窗口可取消。")
        self._refresh_controls()
        worker.start()

    @Slot()
    def _request_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None or self._closed:
            return
        if worker.check_dataset:
            if worker.cancel_event.is_set() or not _operational(self):
                self.status_label.setText("已取消读取手机目录。")
            elif worker.error:
                self.status_label.setText(f"不能关联镜像：{worker.error}")
            elif worker.dataset is not None:
                self.dataset = worker.dataset
                self._finish_selection(worker.remote_path)
                return
            self._refresh_controls()
            return
        if self._initial_view and worker.missing and self._current_path and not worker.cancel_event.is_set():
            self._initial_selection = ""
            self._load("")
            return
        if worker.cancel_event.is_set():
            self.status_label.setText("已取消读取手机目录。")
        elif worker.error:
            self.status_label.setText(f"读取失败：{worker.error}\n可重试，或返回上一级。")
            self.refresh_button.setText("重试读取")
        else:
            self._loaded = True
            for path in worker.paths:
                item = QTreeWidgetItem([display_path(path.rsplit("/", 1)[-1])])
                item.setData(0, Qt.ItemDataRole.UserRole, path)
                item.setToolTip(0, display_path(path))
                self.tree.addTopLevelItem(item)
                if self._initial_view and path == self._initial_selection:
                    self.tree.setCurrentItem(item)
            if worker.unavailable_count:
                status = (f"{len(worker.paths)} 个可选子目录；{worker.unavailable_count} 个目录不能选择"
                          "（名称不受支持或为系统保留目录）。")
            elif worker.paths:
                status = f"{len(worker.paths)} 个子目录。单击选中，双击进入。"
            elif self._current_path:
                status = ("当前目录没有子目录。可返回上一级。" if self._initial_view or self._browse_only
                          else "当前目录没有子目录。可使用当前目录或返回上一级。")
            else:
                status = "手机根目录没有子目录，不能选择根目录。"
            if self._initial_view and self._initial_selection and not self.tree.selectedItems():
                status = "未找到原路径，请明确选一个目录，不会自动改用上级。" + status
            self.status_label.setText(status)
        self._refresh_controls()

    def _enter(self, item: QTreeWidgetItem, _column: int) -> None:
        self._load(str(item.data(0, Qt.ItemDataRole.UserRole)))

    def _up(self) -> None:
        self._load(self._current_path.rpartition("/")[0])

    def accept(self) -> None:
        if self._browse_only or self.busy or self._closed or not self._loaded or not _operational(self):
            return
        selected = self.tree.selectedItems()
        path = selected[0].data(0, Qt.ItemDataRole.UserRole) if selected else (
            "" if self._initial_view else self._current_path
        )
        if not isinstance(path, str) or not path:
            return
        try:
            if normalize_remote_path(path) != path or ".phonenas" in map(portable_name_key, path.split("/")):
                return
        except ValueError:
            return
        if self._require_published:
            self._start_request(path, check_dataset=True)
        else:
            self._finish_selection(path)

    def _finish_selection(self, path: str) -> None:
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
