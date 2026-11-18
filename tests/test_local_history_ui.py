from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.core.config import Config
from app.core.tasks import new_task
from app.ui.pages.versions import VersionsPage


VERSION = "2026-11-14_153000"


class LocalHistoryUiTests(unittest.TestCase):
    """History behavior through the public page, with offline service boundaries."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])
        from app.ui.widgets import apply_theme
        apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="PhoneNAS-local-history-ui-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {
            "PHONENAS_DATA_DIR": str(self.root / "data"),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.config = Config(self.root / "config.json")
        self.config.set("nas.url", "https://phone.invalid/dav")
        self.task = new_task(str(self.root / "received"), "接收图纸", direction="download")
        self.config.upsert_task(self.task)
        boundary = patch("subprocess.Popen", side_effect=AssertionError("No subprocess is allowed"))
        boundary.start()
        self.addCleanup(boundary.stop)

    def keep(self, page):
        self.addCleanup(self.dispose, page)
        return page

    def dispose(self, page):
        page.shutdown()
        self.wait_for(lambda: not page.busy)
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(5)
        self.app.processEvents()
        self.assertTrue(predicate(), "History operation did not settle")

    def test_each_task_defaults_to_its_direction_and_both_history_locations_can_be_chosen(self):
        from contextlib import contextmanager

        calls = []

        class FakeLocalHistory:
            def __init__(self, task):
                self.task = task

            def list_versions(self):
                calls.append(("local", self.task.id))
                return [VERSION]

        class FakeRclone:
            def __init__(self, config, conf_path=None):
                calls.append(("client", config is self_config, Path(conf_path)))

            @contextmanager
            def isolated_conf(self):
                yield self

            def ensure_conf(self):
                calls.append(("ensure_conf",))

            def cancel(self):
                pass

        class FakeRemoteHistory:
            def __init__(self, client, task):
                self.task = task

            def list_versions(self):
                calls.append(("remote", self.task.id))
                return [VERSION]

        publisher = new_task(str(self.root / "published"), "发布图纸")
        self.config.upsert_task(publisher)
        self_config = self.config
        with (
            patch("app.ui.pages.versions.LocalHistoryService", FakeLocalHistory),
            patch("app.ui.pages.versions.HistoryService", FakeRemoteHistory),
            patch("app.ui.pages.versions.Rclone", FakeRclone),
        ):
            page = self.keep(VersionsPage(self.config))
            self.assertEqual(page.location_combo.currentText(), "本地接收历史")
            self.assertEqual(page.location_combo.currentData(), "local")
            self.assertEqual(page.location_combo.count(), 2)
            self.assertEqual(calls, [])

            page.select_task(publisher.id)
            self.assertEqual(page.location_combo.currentText(), "手机发布历史")
            self.assertEqual(page.location_combo.currentData(), "remote")
            self.assertEqual(calls, [])
            page.refresh_button.click()
            self.wait_for(lambda: not page.busy)
            self.assertIn(("remote", publisher.id), calls)
            self.assertIn(("ensure_conf",), calls)
            clients = [call for call in calls if call[0] == "client"]
            self.assertFalse(clients[0][1], "Remote history must receive an isolated config snapshot")
            self.assertFalse(clients[0][2].exists())

            page.location_combo.setCurrentIndex(page.location_combo.findData("local"))
            self.assertEqual(page.list.count(), 0, "A location change must discard the old selection")
            self.assertFalse(page.delete_button.isEnabled())
            before = list(calls)
            self.app.processEvents()
            self.assertEqual(calls, before, "Changing location must not auto-refresh")
            page.refresh_button.click()
            self.wait_for(lambda: not page.busy)
            self.assertIn(("local", publisher.id), calls, "Publishers may keep local histories after adopting returns")
            self.assertEqual(sum(call[0] == "client" for call in calls), 1)

            page.select_task(self.task.id)
            self.assertEqual(page.location_combo.currentData(), "local")
            page.location_combo.setCurrentIndex(page.location_combo.findData("remote"))
            page.refresh_button.click()
            self.wait_for(lambda: not page.busy)
            self.assertIn(("remote", self.task.id), calls, "Receiving tasks may also browse published histories")

    def test_sync_busy_hint_clears_when_idle_and_does_not_overwrite_history_errors(self):
        class FakeLocalHistory:
            fail = False

            def __init__(self, task):
                pass

            def list_versions(self):
                if self.fail:
                    raise RuntimeError("本地历史不可读")
                return [VERSION]

        with (
            patch("app.ui.pages.versions.LocalHistoryService", FakeLocalHistory),
            patch("app.ui.pages.versions.Rclone", side_effect=AssertionError("Must remain offline")),
        ):
            page = self.keep(VersionsPage(self.config))
            page.set_sync_busy(True)
            self.assertFalse(page.refresh_button.isEnabled())
            self.assertIn("进行", page.status.text())
            page.set_sync_busy(False)
            self.assertTrue(page.refresh_button.isEnabled())
            self.assertNotIn("正在进行", page.status.text())
            self.assertNotIn("暂不能", page.status.text())
            self.assertIn("本地", page.status.text())

            FakeLocalHistory.fail = True
            page.refresh_button.click()
            self.wait_for(lambda: not page.busy)
            error = page.status.text()
            self.assertIn("本地历史不可读", error)
            self.assertIn("重试", page.refresh_button.text())
            page.set_sync_busy(True)
            self.assertFalse(page.refresh_button.isEnabled())
            self.assertIn("本地历史不可读", page.status.text())
            page.set_sync_busy(False)
            self.assertEqual(page.status.text(), error)
            self.assertIn("重试", page.refresh_button.text())

    def test_long_paths_remain_copyable_and_history_actions_fit_a_narrow_page(self):
        from PySide6.QtCore import Qt

        with patch("app.ui.pages.versions.Rclone", side_effect=AssertionError("No network during layout")):
            page = self.keep(VersionsPage(self.config))
            page.resize(540, 700)
            page.show()
            self.app.processEvents()
            self.assertIn(self.task.local_history_path, page.path_label.toolTip())
            clipboard = self.app.clipboard()
            original = clipboard.text()
            self.addCleanup(clipboard.setText, original)
            page.copy_path_button.click()
            self.assertEqual(clipboard.text(), self.task.local_history_path)
            self.assertIn("无需连接手机", page.status.text())
            for action in (page.restore_button, page.restore_all_button, page.delete_button):
                self.assertTrue(page.rect().contains(action.geometry()), action.text())
            self.assertLessEqual(page.width(), 540, "History controls must not force the page wider")
            self.assertTrue(page.path_label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse)
            self.assertFalse(page.busy)

    def test_local_files_restore_open_and_delete_use_only_the_selected_task_and_batch(self):
        from PySide6.QtWidgets import QMessageBox

        calls = []
        history = Path(self.task.local_history_path) / VERSION
        history.mkdir(parents=True)
        chosen = self.root / "recovery"
        chosen.mkdir()
        restored = chosen / "PhoneNAS-recovered"
        long_file = "drawings/" + "a" * 100 + ".txt"

        class FakeLocalHistory:
            def __init__(self, task):
                self.task = task

            def list_versions(self):
                calls.append(("versions", self.task))
                return [VERSION]

            def list_files(self, version):
                calls.append(("files", self.task, version))
                return [long_file, "notes.txt"]

            def restore(self, version, destination, files=None):
                calls.append(("restore", self.task, version, destination, files))
                return restored

            def delete(self, version):
                calls.append(("delete", self.task, version))

        with (
            patch("app.ui.pages.versions.LocalHistoryService", FakeLocalHistory),
            patch("app.ui.pages.versions.Rclone", side_effect=AssertionError("Must remain offline")) as rclone,
            patch.object(Config, "password", new_callable=PropertyMock, side_effect=AssertionError("Local operations must not decrypt credentials")),
            patch("app.ui.pages.versions.QFileDialog.getExistingDirectory", return_value=str(chosen)),
            patch("app.ui.pages.versions.os.startfile", create=True) as open_path,
        ):
            page = self.keep(VersionsPage(self.config))
            events = []
            page.event_recorded.connect(events.append)
            page.refresh_button.click()
            self.wait_for(lambda: not page.busy)
            page.list.setCurrentRow(0)
            self.wait_for(lambda: not page.busy)
            self.assertEqual(page.file_list.count(), 2)
            self.assertEqual(page.file_list.item(0).toolTip(), long_file)
            page.file_list.item(0).setSelected(True)
            page.restore_button.click()
            self.wait_for(lambda: not page.busy)
            self.assertIn(("restore", self.task, VERSION, chosen, (long_file,)), calls)
            self.assertIn(str(restored), page.status.text())
            page.restore_all_button.click()
            self.wait_for(lambda: not page.busy)
            self.assertIn(("restore", self.task, VERSION, chosen, None), calls)
            page.open_button.click()
            self.wait_for(lambda: not page.busy)
            open_path.assert_called_once_with(str(history))
            with patch("app.ui.pages.versions.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as question:
                page.delete_button.click()
                self.assertNotIn(("delete", self.task, VERSION), calls)
                self.assertIn("本地接收历史", question.call_args.args[2])
                self.assertEqual(question.call_args.args[4], QMessageBox.StandardButton.No)
            with patch("app.ui.pages.versions.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                page.delete_button.click()
                self.wait_for(lambda: not page.busy)
            self.assertIn(("delete", self.task, VERSION), calls)
            self.assertEqual(page.list.count(), 0)
            self.assertTrue(all(event["task_name"] == self.task.name for event in events))
            self.assertTrue(all(event["history_location"] == "local" for event in events))
            rclone.assert_not_called()

    def test_cancelling_local_history_keeps_busy_gate_until_the_service_has_exited(self):
        entered, release = threading.Event(), threading.Event()
        tasks = []

        class SlowLocalHistory:
            def __init__(self, task):
                tasks.append(task)

            def list_versions(self):
                entered.set()
                release.wait(3)
                return [VERSION]

        with (
            patch("app.ui.pages.versions.LocalHistoryService", SlowLocalHistory),
            patch("app.ui.pages.versions.Rclone", side_effect=AssertionError("Must remain offline")) as rclone,
        ):
            page = self.keep(VersionsPage(self.config))
            self.addCleanup(release.set)
            signals, events = [], []
            page.operation_started.connect(lambda: signals.append("started"))
            page.operation_finished.connect(lambda: signals.append("finished"))
            page.event_recorded.connect(events.append)
            page.refresh_button.click()
            self.wait_for(entered.is_set)
            self.assertTrue(page.busy)
            self.assertFalse(page.location_combo.isEnabled())
            self.assertFalse(page.refresh_button.isEnabled())
            self.assertFalse(page.busy_hint.isHidden())
            self.assertIn("进行", page.busy_hint.text())
            # A programmatic selection change must not redirect the frozen job or
            # publish its late result into the newly selected history location.
            page.location_combo.setCurrentIndex(page.location_combo.findData("remote"))
            page.cancel_button.click()
            page.set_sync_busy(False)
            self.assertTrue(page.busy)
            self.assertEqual(signals, ["started"])
            self.assertFalse(page.shutdown(), "Shutdown must retain a running local thread")
            self.assertTrue(page.busy)
            release.set()
            self.wait_for(lambda: not page.busy)
            self.assertTrue(page.shutdown())
            self.assertEqual(signals, ["started", "finished"])
            self.assertEqual(tasks, [self.task])
            self.assertEqual(page.list.count(), 0)
            self.assertEqual(events[0]["kind"], "error")
            self.assertEqual(events[0]["history_location"], "local")
            self.assertEqual(events[0]["task_name"], self.task.name)
            rclone.assert_not_called()

    def test_safe_preview_disables_local_history_with_a_reason_near_controls(self):
        with (
            patch("app.ui.pages.versions.LocalHistoryService", side_effect=AssertionError("Safe mode must not read local files")) as local,
            patch("app.ui.pages.versions.Rclone", side_effect=AssertionError("Safe mode must not connect")) as rclone,
        ):
            page = self.keep(VersionsPage(self.config, operational=False))
            page.refresh()
            self.assertFalse(page.busy)
            self.assertFalse(page.refresh_button.isEnabled())
            self.assertIn("安全", page.busy_hint.text())
            self.assertFalse(page.busy_hint.isHidden())
            self.assertIn("本地", page.busy_hint.text())
            local.assert_not_called()
            rclone.assert_not_called()

    def test_remote_history_errors_hide_credentials_in_the_page_and_recorded_events(self):
        from contextlib import contextmanager

        secret = "fake-password-not-for-logs"
        class FakeRclone:
            def __init__(self, config, conf_path=None):
                pass

            @contextmanager
            def isolated_conf(self):
                yield self

            def ensure_conf(self):
                raise RuntimeError(f"connection failed for https://admin:{secret}@phone.invalid; password={secret}")

            def cancel(self):
                pass

        with (
            patch("app.ui.pages.versions.Rclone", FakeRclone),
            patch.object(Config, "password", new_callable=PropertyMock, return_value=secret),
        ):
            page = self.keep(VersionsPage(self.config))
            events = []
            page.event_recorded.connect(events.append)
            page.location_combo.setCurrentIndex(page.location_combo.findData("remote"))
            page.refresh_button.click()
            self.wait_for(lambda: not page.busy)
            self.assertEqual(events[0]["kind"], "error")
            self.assertNotIn(secret, page.status.text())
            self.assertNotIn(secret, str(events))
            self.assertIn("隐藏", page.status.text())

    def test_download_history_lists_local_batches_offline_without_constructing_rclone(self):
        calls = []
        main_thread = threading.get_ident()

        class FakeLocalHistory:
            def __init__(self, task):
                calls.append(("construct", task, threading.get_ident()))

            def list_versions(self):
                calls.append(("versions", threading.get_ident()))
                return [VERSION]

        with (
            patch("app.ui.pages.versions.LocalHistoryService", FakeLocalHistory),
            patch("app.ui.pages.versions.Rclone", side_effect=AssertionError("Local history must not construct Rclone")) as rclone,
            patch.object(Config, "password", new_callable=PropertyMock, side_effect=AssertionError("Local history must not decrypt credentials")),
        ):
            page = self.keep(VersionsPage(self.config))
            events = []
            page.event_recorded.connect(events.append)
            self.assertEqual(calls, [], "Constructing a page must not read any history")
            page.refresh_button.click()
            self.wait_for(lambda: not page.busy)

            self.assertEqual(page.list.count(), 1, page.status.text())
            self.assertEqual(page.list.item(0).text(), VERSION)
            self.assertEqual(calls[0][1], self.task)
            self.assertNotEqual(calls[0][2], main_thread)
            self.assertNotEqual(calls[1][1], main_thread)
            rclone.assert_not_called()
            self.assertEqual(events[0]["task"], self.task.id)
            self.assertEqual(events[0]["task_name"], "接收图纸")
            self.assertEqual(events[0]["kind"], "success")
            self.assertFalse(self.config.path.exists(), "Browsing must not persist config")


if __name__ == "__main__":
    unittest.main()
