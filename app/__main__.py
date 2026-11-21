from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app import constants as C
from app.core.config import Config, data_dir
from app.core.log import get_logger, setup_logging
from app.ui.main_window import MainWindow
from app.ui.widgets import apply_theme


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
    parser = argparse.ArgumentParser(description="PhoneNAS 桌面工作台")
    parser.add_argument("--tray", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--smoke-test", action="store_true", help="隔离运行界面检查后退出，不挂载或同步")
    parser.add_argument("--ui-preview", action="store_true", help="使用模拟任务预览真实桌面界面，不连接手机")
    options, qt_args = parser.parse_known_args()
    safe = options.smoke_test or options.ui_preview
    scratch = None
    if safe:
        scratch = tempfile.TemporaryDirectory(prefix="PhoneNAS-ui-check-", ignore_cleanup_errors=True)
        os.environ["APPDATA"] = scratch.name
        os.environ["PHONENAS_DATA_DIR"] = str(Path(scratch.name) / "data")
    setup_logging()
    if options.self_check:
        return _self_check()

    def _excepthook(exc_type, exc, tb):
        get_logger().exception("unhandled exception", exc_info=(exc_type, exc, tb))
        if not options.smoke_test:
            QMessageBox.critical(None, C.APP_NAME, f"程序错误：{exc}")
        else:
            sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(C.APP_NAME)
    app.setApplicationVersion(C.APP_VERSION)
    app.setQuitOnLastWindowClosed(False)
    apply_theme(app)

    icon_path = C.resource_path("app/resources/app.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    lock = QLockFile(str(data_dir() / "PhoneNAS.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.warning(None, C.APP_NAME, "PhoneNAS 已在运行。")
        return 0

    config = Config(Path(scratch.name) / "config.json" if scratch else None)
    if safe:
        from dataclasses import replace
        from app.core.tasks import new_task

        folders = [Path(scratch.name) / name for name in ("项目资料", "产品文档", "设计素材")]
        for folder in folders:
            folder.mkdir()
        config.replace_tasks([
            replace(new_task(str(folders[0])), last_sync_at="2026-11-14 09:40:00", last_file_count=2884, retention_days=90),
            replace(new_task(str(folders[1]), direction="download", remote_path="backup/company-docs/mirror", versions_path="backup/company-docs/versions"), interval_minutes=60, auto_sync=False, last_sync_at="2026-11-14 09:20:00", last_file_count=126),
            replace(new_task(str(folders[2])), interval_minutes=120, retention_days=180, enabled=False),
        ])
    first_run = not safe and not config.path.exists()
    window = MainWindow(config, first_run=first_run, operational=not safe)
    if options.tray and not first_run:
        window.hide()
    else:
        window.show()

    app._phonenas_lock = lock  # type: ignore[attr-defined]
    app._phonenas_window = window  # type: ignore[attr-defined]
    app._phonenas_scratch = scratch  # type: ignore[attr-defined]
    if options.smoke_test:
        QTimer.singleShot(1500, window.quit_app)
    code = app.exec()
    lock.unlock()
    if scratch:
        import logging
        logging.shutdown()
        scratch.cleanup()
    return code


if __name__ == "__main__":
    sys.exit(main())
