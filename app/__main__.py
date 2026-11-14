from __future__ import annotations

import sys

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app import constants as C
from app.core.config import Config, data_dir
from app.core.log import get_logger, setup_logging
from app.ui.main_window import MainWindow


def _self_check() -> int:
    from app.core import winfsp
    from app.core.rclone import Rclone, rclone_exe

    lines = []
    exe = rclone_exe()
    lines.append(f"rclone_exe={exe}")
    lines.append(f"rclone_exists={exe.exists()}")
    try:
        lines.append(f"rclone_version={Rclone(Config()).version()}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"rclone_version_error={exc}")
    lines.append(f"winfsp_installed={winfsp.is_installed()}")
    msi = winfsp.msi_path()
    lines.append(f"winfsp_msi={msi} exists={msi.exists()}")
    (data_dir() / "self-check.txt").write_text("\n".join(lines), encoding="utf-8")
    return 0


def main() -> int:
    setup_logging()
    if "--self-check" in sys.argv:
        return _self_check()

    def _excepthook(exc_type, exc, tb):
        get_logger().exception("unhandled exception", exc_info=(exc_type, exc, tb))
        QMessageBox.critical(None, C.APP_NAME, f"程序错误：{exc}")

    sys.excepthook = _excepthook

    app = QApplication(sys.argv)
    app.setApplicationName(C.APP_NAME)
    app.setApplicationVersion(C.APP_VERSION)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")

    qss = C.resource_path("app/ui/style.qss")
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))

    icon_path = C.resource_path("app/resources/app.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    lock = QLockFile(str(data_dir() / "PhoneNAS.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.warning(None, C.APP_NAME, "PhoneNAS 已在运行。")
        return 0

    config = Config()
    first_run = not config.path.exists()
    window = MainWindow(config, first_run=first_run)
    if "--tray" in sys.argv and not first_run:
        window.hide()
    else:
        window.show()

    app._phonenas_lock = lock  # type: ignore[attr-defined]
    app._phonenas_window = window  # type: ignore[attr-defined]

    if "--smoke-test" in sys.argv:
        QTimer.singleShot(1500, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
