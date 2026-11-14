from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QSystemTrayIcon,
    QWidget,
)

from app import constants as C
from app.core import winfsp
from app.core.config import Config
from app.core.log import get_logger
from app.core.mount import MountSupervisor
from app.core.scheduler import Scheduler
from app.core.sync import SyncWorker
from app.ui.pages.logs import LogsPage
from app.ui.pages.mirror import MirrorPage
from app.ui.pages.settings import SettingsPage
from app.ui.pages.status import StatusPage
from app.ui.pages.versions import VersionsPage
from app.ui.wizard import WizardDialog


class MainWindow(QMainWindow):
    def __init__(self, config: Config, first_run: bool = False):
        super().__init__()
        self.config = config
        self.log = get_logger()
        self.mount = MountSupervisor(config, self)
        self.scheduler = Scheduler(config.interval_minutes, self)
        self.sync_worker: SyncWorker | None = None
        self._queued_sync = False
        self._tray_hint_shown = False
        self._last_drive = self.config.get("mount.drive", C.DEFAULT_DRIVE)

        self.setWindowTitle(f"{C.APP_NAME} {C.APP_VERSION}")
        self.resize(980, 680)
        self._build_ui()
        self._build_tray()
        self._connect()
        self.refresh_status()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(15000)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start()

        if first_run:
            QTimer.singleShot(250, self.open_wizard)
        else:
            if self.config.get("mount.auto_mount", True):
                QTimer.singleShot(400, self.mount.start)
            self.scheduler.start()
            QTimer.singleShot(2500, self._startup_sync_if_due)

    # -- ui ---------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(150)
        self.sidebar.setObjectName("sidebar")
        self.stack = QStackedWidget()

        self.status_page = StatusPage()
        self.mirror_page = MirrorPage(self.config)
        self.versions_page = VersionsPage(self.config)
        self.logs_page = LogsPage()
        self.settings_page = SettingsPage(self.config)

        for name, page in (
            ("状态", self.status_page),
            ("镜像", self.mirror_page),
            ("版本", self.versions_page),
            ("日志", self.logs_page),
            ("设置", self.settings_page),
        ):
            self.sidebar.addItem(name)
            self.stack.addWidget(page)

        self.sidebar.currentRowChanged.connect(self._nav_changed)
        self.sidebar.setCurrentRow(0)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("就绪")

    def _build_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self._make_icon(), self)
        self.tray.setToolTip(C.APP_NAME)
        menu = QMenu(self)
        self.act_open = QAction("打开主界面", self)
        self.act_sync = QAction("立即同步", self)
        self.act_mount = QAction("挂载/卸载", self)
        self.act_pause = QAction("暂停/继续", self)
        self.act_quit = QAction("退出", self)
        for action in (self.act_open, self.act_sync, self.act_mount, self.act_pause):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self.act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)

        self.act_open.triggered.connect(self.show_window)
        self.act_sync.triggered.connect(lambda: self.run_sync(manual=True))
        self.act_mount.triggered.connect(self.toggle_mount)
        self.act_pause.triggered.connect(self.toggle_pause)
        self.act_quit.triggered.connect(self.quit_app)
        self.tray.show()

    def _connect(self) -> None:
        self.status_page.sync_requested.connect(lambda: self.run_sync(manual=True))
        self.status_page.mount_toggle_requested.connect(self.toggle_mount)
        self.status_page.pause_toggle_requested.connect(self.toggle_pause)
        self.mirror_page.saved.connect(self._on_mirror_saved)
        self.settings_page.saved.connect(self._on_settings_saved)
        self.mount.state_changed.connect(self._on_mount_state)
        self.mount.log_line.connect(self.status_page.append_output)
        self.scheduler.tick.connect(lambda: self.run_sync(manual=False))

    def _make_icon(self) -> QIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#2f6fed"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(4, 4, 56, 56, 14, 14)
        painter.setPen(QColor("#ffffff"))
        font = painter.font()
        font.setPixelSize(30)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "N")
        painter.end()
        return QIcon(pm)

    # -- actions ----------------------------------------------------------
    def _nav_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if self.stack.currentWidget() is self.versions_page:
            self.versions_page.refresh()
        elif self.stack.currentWidget() is self.logs_page:
            self.logs_page.refresh()

    def open_wizard(self) -> None:
        dialog = WizardDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings_page.load()
            self.mirror_page.load()
            self.scheduler.set_interval(self.config.interval_minutes)
            self.refresh_status()
            if self.mount.state == "stopped":
                self.mount.start()
            if not self.scheduler.paused:
                self.scheduler.start()
            QTimer.singleShot(600, lambda: self.run_sync(manual=True))

    def _startup_sync_if_due(self) -> None:
        if not self.config.get("mirror.enabled", True):
            return
        last = self.config.get("app.last_sync_at", "")
        due = True
        if last:
            try:
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                due = (datetime.now() - last_dt).total_seconds() >= self.config.interval_minutes * 60
            except ValueError:
                due = True
        if due:
            self.run_sync(manual=False)

    def run_sync(self, manual: bool = True) -> None:
        if self.sync_worker is not None and self.sync_worker.isRunning():
            if manual:
                self._queued_sync = True
                self.status_page.set_state("当前同步结束后再执行一次")
            return
        self._queued_sync = False
        self.sync_worker = SyncWorker(self.config, self)
        worker = self.sync_worker
        worker.progress.connect(self.status_page.append_output)
        worker.finished_ok.connect(self._on_sync_ok)
        worker.failed.connect(self._on_sync_failed)
        worker.finished.connect(self._on_worker_finished)
        self.status_page.set_state("正在同步…")
        self.statusBar().showMessage("正在同步…")
        worker.start()

    def _on_sync_ok(self, summary: str, stamp: str) -> None:
        self.config.set("app.last_sync_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.config.save()
        self.status_page.set_state(summary)
        self.statusBar().showMessage(summary)
        self.refresh_status()
        if self.tray:
            self.tray.showMessage(C.APP_NAME, summary, QSystemTrayIcon.MessageIcon.Information, 4000)

    def _on_sync_failed(self, message: str) -> None:
        self.status_page.set_state(f"同步失败：{message}")
        self.statusBar().showMessage("同步失败")
        if self.tray:
            self.tray.showMessage(C.APP_NAME, f"同步失败：{message}", QSystemTrayIcon.MessageIcon.Warning, 6000)

    def _on_worker_finished(self) -> None:
        self.sync_worker = None
        if self._queued_sync:
            self._queued_sync = False
            QTimer.singleShot(500, lambda: self.run_sync(manual=True))

    def toggle_mount(self) -> None:
        if self.mount.state in ("mounted", "starting", "retrying"):
            self.mount.stop()
        else:
            self.mount.start()
        QTimer.singleShot(300, self.refresh_status)

    def toggle_pause(self) -> None:
        if self.scheduler.paused:
            self.scheduler.resume()
        else:
            self.scheduler.pause()
        self.refresh_status()

    def _on_mount_state(self, state: str) -> None:
        self.refresh_status()
        if state == "mounted":
            self.statusBar().showMessage(f"已挂载 {self.mount.drive}")
        elif state == "error":
            self.statusBar().showMessage("挂载失败")
            if self.tray:
                self.tray.showMessage(
                    C.APP_NAME,
                    "挂载失败，请检查 NAS 地址或 WinFsp 是否安装",
                    QSystemTrayIcon.MessageIcon.Warning,
                    6000,
                )

    def _on_mirror_saved(self) -> None:
        self.scheduler.set_interval(self.config.interval_minutes)
        self.refresh_status()

    def _on_settings_saved(self) -> None:
        new_drive = self.config.get("mount.drive", C.DEFAULT_DRIVE)
        if new_drive != self._last_drive and self.mount.state != "stopped":
            self._last_drive = new_drive
            self.mount.restart()
        self.refresh_status()

    def refresh_status(self) -> None:
        self.status_page.set_nas(self.config.url)
        self.status_page.set_winfsp(winfsp.is_installed())
        self.status_page.set_mount(self.mount.state, self.mount.drive)
        last = self.config.get("app.last_sync_at", "")
        next_run = self.scheduler.next_run_at
        next_text = next_run.strftime("%H:%M:%S") if next_run else ""
        self.status_page.set_sync(last, next_text, self.scheduler.paused)
        if self.tray:
            state_text = {
                "mounted": f"已挂载 {self.mount.drive}",
                "stopped": "未挂载",
                "starting": "正在挂载",
                "retrying": "正在重试挂载",
                "error": "挂载失败",
            }.get(self.mount.state, self.mount.state)
            self.tray.setToolTip(f"{C.APP_NAME} - {state_text}")
            self.act_mount.setText("卸载" if self.mount.state in ("mounted", "starting", "retrying") else "挂载")
            self.act_pause.setText("继续" if self.scheduler.paused else "暂停")

    # -- window -----------------------------------------------------------
    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.config.get("app.minimize_to_tray", True) and self.tray is not None:
            self.hide()
            event.ignore()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(
                    C.APP_NAME,
                    "PhoneNAS 仍在后台运行，双击托盘图标可重新打开。",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
            return
        self.quit_app()

    def quit_app(self) -> None:
        self.scheduler.stop()
        self.mount.stop()
        if self.sync_worker is not None and self.sync_worker.isRunning():
            self.sync_worker.stop()
            self.sync_worker.wait(3000)
        if self.tray:
            self.tray.hide()
        QApplication.quit()
