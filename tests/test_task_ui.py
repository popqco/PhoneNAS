from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_TEST_ROOT: Path

from PySide6.QtCore import QCoreApplication, QEvent, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from app.core.config import Config
from app.core.rclone import Result
from app.ui.widgets import apply_theme


def setUpModule():
    global _TEST_ROOT
    home = tempfile.TemporaryDirectory(prefix="PhoneNAS-task-ui-")
    unittest.addModuleCleanup(home.cleanup)
    _TEST_ROOT = Path(home.name)
    environment = patch.dict(os.environ, {
        "QT_QPA_PLATFORM": "offscreen",
        "PHONENAS_DATA_DIR": str(_TEST_ROOT / "data"),
        "APPDATA": str(_TEST_ROOT / "appdata"),
        "LOCALAPPDATA": str(_TEST_ROOT / "localappdata"),
    })
    environment.start()
    unittest.addModuleCleanup(environment.stop)


class TaskUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "work"
        self.source.mkdir()
        self.config = Config(self.base / "config.json")
        self.config.set("nas.url", "https://phone.invalid/dav")

    def keep(self, dialog):
        self.addCleanup(self.dispose, dialog)
        return dialog

    def dispose(self, dialog):
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait_for(self, predicate, timeout=3000):
        deadline = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
            time.sleep(0.001)
        self.assertTrue(predicate(), "UI operation did not finish")

    def test_remote_picker_is_inert_until_clicked_and_returns_only_safe_listed_directory(self):
        from app.ui.remote_folder_dialog import RemoteFolderDialog

        requests = []
        clients = []

        class FakeRclone:
            def __init__(self, config, conf_path=None):
                self.config = config
                self.conf_path = Path(conf_path)
                clients.append(self)

            def ensure_conf(self):
                self.conf_path.write_text("fake private conf", encoding="utf-8")

            def run(self, args, **kwargs):
                requests.append((args, self.config.url, self.conf_path.is_file()))
                return Result(True, json.dumps([
                    {"Path": "backup\u200b", "IsDir": True},
                    {"Path": "work\x7f", "IsDir": True},
                    {"Path": "../escape", "IsDir": True},
                    {"Path": "/absolute", "IsDir": True},
                    {"Path": "other:remote", "IsDir": True},
                    {"Path": "file.txt", "IsDir": False},
                ]), "", 0)

            def cancel(self):
                pass

        before = copy.deepcopy(self.config.data)
        with patch("app.ui.remote_folder_dialog.Rclone", FakeRclone):
            dialog = self.keep(RemoteFolderDialog(self.config))
            dialog.show()
            QTest.qWait(20)
            self.assertFalse(dialog.busy)
            self.assertEqual(requests, [])
            self.assertFalse(dialog.choose_button.isEnabled())
            dialog.refresh_button.click()
            self.wait_for(lambda: not dialog.busy and dialog.tree.topLevelItemCount() == 2)
            self.assertEqual(requests[0][0][:2], ["lsjson", "phone:"])
            self.assertIn("--dirs-only", requests[0][0])
            self.assertIn("--max-depth", requests[0][0])
            self.assertTrue(requests[0][2])
            self.assertIsNot(clients[0].config, self.config)
            self.assertFalse(clients[0].conf_path.exists())
            self.assertEqual([dialog.tree.topLevelItem(i).text(0) for i in range(2)],
                             ["backup<U+200B>", "work<U+007F>"])
            dialog.tree.setCurrentItem(dialog.tree.topLevelItem(1))
            dialog.choose_button.click()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(dialog.remote_path, "work\x7f")
        self.assertEqual(self.config.data, before)
        self.assertFalse(self.config.path.exists())

    def test_remote_navigation_uses_frozen_connection_and_only_immediate_safe_children(self):
        from app.ui.remote_folder_dialog import RemoteFolderDialog

        started, release = threading.Event(), threading.Event()
        requests = []
        responses = {
            "phone:": [
                {"Path": "backup", "IsDir": True},
                {"Path": ".", "IsDir": True},
                {"Path": "..", "IsDir": True},
                {"Path": "backup/nested", "IsDir": True},
                {"Path": "bad\\\\path", "IsDir": True},
                {"Path": " whitespace", "IsDir": True},
                {"Path": "bad\u202ename", "IsDir": True},
            ],
            "phone:backup": [{"Path": "mirror", "IsDir": True}],
        }

        class FrozenRclone:
            def __init__(self, config, conf_path=None):
                self.config = config

            def ensure_conf(self):
                started.set()
                release.wait(2)

            def run(self, args, **kwargs):
                requests.append((args[0], args[1], self.config.url))
                return Result(True, json.dumps(responses[args[1]]), "", 0)

            def cancel(self):
                release.set()

        with patch("app.ui.remote_folder_dialog.Rclone", FrozenRclone):
            dialog = self.keep(RemoteFolderDialog(self.config))
            dialog.refresh_button.click()
            self.wait_for(started.is_set)
            self.config.set("nas.url", "https://changed.invalid/dav")
            release.set()
            self.wait_for(lambda: not dialog.busy)
            self.assertEqual(dialog.tree.topLevelItemCount(), 1)
            self.assertEqual(requests, [("lsjson", "phone:", "https://phone.invalid/dav")])
            dialog.tree.itemDoubleClicked.emit(dialog.tree.topLevelItem(0), 0)
            self.wait_for(lambda: not dialog.busy)
            self.assertEqual(requests[1][:2], ("lsjson", "phone:backup"))
            self.assertEqual(dialog.tree.topLevelItem(0).text(0), "mirror")
            dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
            dialog.choose_button.click()
            self.assertEqual(dialog.remote_path, "backup/mirror")

    def test_new_receive_task_requires_explicit_existing_remote_and_keeps_local_history_outside_data(self):
        from app.ui.task_dialog import TaskDialog

        before = copy.deepcopy(self.config.data)
        dialog = self.keep(TaskDialog(self.config, operational=False))
        dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData("download"))
        dialog.local_edit.setText(str(self.source))
        dialog.name_edit.setText("接收工作文件")
        self.assertEqual(dialog.remote_edit.text(), "")
        self.assertFalse(dialog.enabled_check.isChecked())
        dialog.save_button.click()
        self.assertIsNone(dialog.task)
        self.assertIn("已有", dialog.error_label.text())
        self.assertIn("接收", dialog.local_caption.text())
        dialog.remote_edit.setText("backup/company/mirror")
        self.assertEqual(dialog.versions_edit.text(), "backup/company/versions")
        history = Path(dialog.local_history_edit.text())
        self.assertTrue(history.is_absolute())
        self.assertEqual(history.parent, self.source.parent)
        self.assertNotEqual(history, self.source)
        dialog.interval_spin.setValue(1)
        dialog.retention_spin.setValue(3650)
        dialog.save_button.click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.task.direction, "download")
        self.assertEqual(dialog.task.remote_path, "backup/company/mirror")
        self.assertEqual(dialog.task.versions_path, "backup/company/versions")
        self.assertEqual(dialog.task.local_history_path, str(history))
        self.assertEqual(dialog.task.interval_minutes, 1)
        self.assertEqual(dialog.task.retention_days, 3650)
        self.assertEqual(dialog.interval_spin.maximum(), 1440)
        self.assertEqual(self.config.data, before)
        self.assertFalse(self.config.path.exists())

    def test_task_save_keeps_literal_custom_rule_whitespace_and_unicode(self):
        from app.ui.task_dialog import TaskDialog
        from app.core.tasks import new_task

        rules = (" notes.txt", "literal\u2029separator.txt", "/pair[\u2421].txt")
        task = replace(new_task(str(self.source)), exclude_preset="custom", excludes=rules)
        dialog = self.keep(TaskDialog(self.config, task))
        dialog.name_edit.setText("Renamed only")
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.task.excludes, rules)
        self.assertEqual(dialog.task.sync_identity, task.sync_identity)

    def test_schedule_auto_sync_is_independent_of_enabled_and_preserves_binding_authority(self):
        from app.core.tasks import new_task
        from app.ui.task_dialog import TaskDialog

        original = replace(new_task(str(self.source)), last_sync_at="2026-11-14T12:00:00", last_file_count=12)
        self.config.upsert_task(original)
        before = copy.deepcopy(self.config.data)
        for enabled, auto_sync in ((True, False), (False, True), (False, False)):
            with self.subTest(enabled=enabled, auto_sync=auto_sync):
                dialog = self.keep(TaskDialog(self.config, original, operational=False))
                self.assertEqual(dialog.auto_sync_check.text(), "按计划自动执行")
                self.assertTrue(dialog.tabs.widget(1).isAncestorOf(dialog.auto_sync_check))
                self.assertNotIn("自动", dialog.enabled_check.text())
                self.assertTrue(dialog.auto_sync_check.isChecked())
                dialog.interval_spin.setValue(13)
                dialog.auto_sync_check.setChecked(auto_sync)
                dialog.enabled_check.setChecked(enabled)
                self.assertEqual(dialog.interval_spin.isEnabled(), auto_sync)
                self.assertTrue(dialog.retention_spin.isEnabled())
                dialog.save_button.click()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(dialog.task, replace(original, enabled=enabled, auto_sync=auto_sync, interval_minutes=13))
                reopened = self.keep(TaskDialog(self.config, dialog.task, operational=False))
                self.assertEqual(reopened.auto_sync_check.isChecked(), auto_sync)
                self.assertEqual(reopened.interval_spin.isEnabled(), auto_sync)
        self.assertEqual(self.config.data, before)
        self.assertFalse(self.config.path.exists())

    def test_new_receive_defaults_to_manual_without_turning_automatic_back_on(self):
        from app.ui.task_dialog import TaskDialog

        dialog = self.keep(TaskDialog(self.config, operational=False))
        self.assertTrue(dialog.auto_sync_check.isChecked())
        dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData("download"))
        self.assertFalse(dialog.enabled_check.isChecked())
        self.assertFalse(dialog.auto_sync_check.isChecked())
        self.assertFalse(dialog.interval_spin.isEnabled())
        dialog.enabled_check.setChecked(True)
        self.assertFalse(dialog.auto_sync_check.isChecked())
        dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData("upload"))
        self.assertFalse(dialog.auto_sync_check.isChecked())

    def test_upload_and_direction_edits_preserve_dataset_but_invalidate_old_success_identity(self):
        from app.ui.task_dialog import TaskDialog

        created = self.keep(TaskDialog(self.config, operational=False))
        created.local_edit.setText(str(self.source))
        self.assertEqual(created.direction_combo.currentData(), "upload")
        self.assertTrue(created.local_history_edit.text())
        created.save_button.click()
        self.assertEqual(created.result(), QDialog.DialogCode.Accepted)
        original = replace(created.task, last_sync_at="2026-11-14T12:00:00", last_file_count=12)
        self.config.upsert_task(original)
        before = copy.deepcopy(self.config.data)
        renamed = self.keep(TaskDialog(self.config, original, operational=False))
        renamed.name_edit.setText("发布工作文件")
        renamed.interval_spin.setValue(1440)
        renamed.save_button.click()
        self.assertEqual(renamed.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(renamed.task.sync_identity, original.sync_identity)
        self.assertEqual(renamed.task.last_sync_at, original.last_sync_at)
        edited = self.keep(TaskDialog(self.config, original, operational=False))
        edited.direction_combo.setCurrentIndex(edited.direction_combo.findData("download"))
        self.assertEqual(edited.remote_edit.text(), original.remote_path)
        edited.save_button.click()
        self.assertEqual(edited.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(edited.task.id, original.id)
        self.assertEqual(edited.task.dataset_id, original.dataset_id)
        self.assertNotEqual(edited.task.sync_identity, original.sync_identity)
        self.assertEqual((edited.task.last_sync_at, edited.task.last_file_count), ("", 0))
        self.assertEqual(self.config.data, before)

    def test_receive_task_keeps_manual_history_suggestion_and_rejects_unsafe_remote_paths(self):
        from app.core.tasks import new_task
        from app.ui.task_dialog import TaskDialog

        dialog = self.keep(TaskDialog(self.config, operational=False))
        dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData("download"))
        dialog.local_edit.setText(str(self.source))
        dialog.remote_edit.setText("projects")
        self.assertEqual(dialog.versions_edit.text(), "projects-versions")
        dialog.versions_edit.setText("archive/custom-history")
        for unsafe in ("../escape", "/absolute", "other:remote", "backup/../escape"):
            with self.subTest(remote=unsafe):
                dialog.remote_edit.setText(unsafe)
                dialog.accept()
                self.assertIsNone(dialog.task)
        dialog.remote_edit.setText("backup/shared/mirror")
        self.assertEqual(dialog.versions_edit.text(), "archive/custom-history")
        dialog.local_history_edit.setText(str(self.source / "history"))
        dialog.accept()
        self.assertIsNone(dialog.task)
        dialog.local_history_edit.setText(str(self.base / "safe-history"))
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        expected = new_task(str(self.source), direction="download", remote_path="backup/shared/mirror")
        self.assertEqual(dialog.task.dataset_id, expected.dataset_id)
        self.assertFalse((self.base / "safe-history").exists())

    def test_preview_names_destination_for_each_operation_and_threshold_confirmation_can_proceed(self):
        from app.core.sync import SyncReport
        from app.core.tasks import new_task
        from app.ui.preview_dialog import PreviewDialog

        for operation, direction, text, destination, history_word in (
            ("publish", "upload", "确认并上传", "手机", "手机历史"),
            ("receive", "download", "确认并拉取", "本地", "本地历史"),
            ("return", "download", "确认并交回", "手机", "手机历史"),
            ("adopt", "upload", "确认并拉取", "本地", "本地历史"),
        ):
            with self.subTest(operation=operation):
                task = new_task(str(self.source), direction=direction)
                report = SyncReport(updated=1, changes=(("*", "document.txt"),), operation=operation, requires_confirmation=True)
                dialog = self.keep(PreviewDialog(task, report))
                self.assertIn("查看差异", dialog.windowTitle())
                self.assertEqual(dialog.execute_button.text(), text)
                self.assertTrue(dialog.execute_button.isEnabled())
                self.assertIn(destination, dialog.destination_label.text())
                self.assertIn(history_word, dialog.history_label.text())
                dialog.execute_button.click()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_disabled_task_can_view_differences_but_cannot_confirm_normal_relay_or_recovery(self):
        from app.core.sync import SyncReport
        from app.core.tasks import new_task
        from app.ui.preview_dialog import PreviewDialog

        task = replace(new_task(str(self.source)), enabled=False)
        for operation in ("publish", "receive", "return", "adopt", "recover"):
            with self.subTest(operation=operation):
                report = SyncReport(updated=1, operation="receive" if operation == "recover" else operation)
                dialog = self.keep(PreviewDialog(task, report, operation=operation))
                self.assertFalse(dialog.execute_button.isEnabled())
                self.assertIn("启用", dialog.execute_button.toolTip())
                dialog.accept()
                dialog.done(QDialog.DialogCode.Accepted)
                self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_hard_block_cannot_be_accepted_and_does_not_hide_report_reason(self):
        from app.core.sync import SyncReport
        from app.core.tasks import new_task
        from app.ui.preview_dialog import PreviewDialog

        task = new_task(str(self.source))
        report = SyncReport(updated=2, operation="adopt", blocked=True, reason="本地和手机都发生修改", requires_confirmation=True)
        dialog = self.keep(PreviewDialog(task, report, "保护阈值提示", operation="receive"))
        self.assertEqual(dialog.operation, "receive")
        self.assertFalse(dialog.execute_button.isEnabled())
        self.assertIn("本地和手机都发生修改", dialog.reason_label.text())
        self.assertIn("保护阈值提示", dialog.reason_label.text())
        self.assertIn("副本", dialog.blocked_label.text())
        self.assertIn("不能通过确认覆盖保护", dialog.blocked_label.text())
        dialog.execute_button.click()
        dialog.accept()
        dialog.done(QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_preview_exposes_invisible_unicode_without_changing_report_paths(self):
        from app.core.sync import SyncReport
        from app.core.tasks import new_task
        from app.ui.preview_dialog import PreviewDialog

        path = "emoji-👩\u200d💻/private-\ue000.txt"
        report = SyncReport(added=1, changes=(("+", path),))
        dialog = self.keep(PreviewDialog(new_task(str(self.source)), report))
        shown = dialog.table.item(0, 1)
        self.assertIn("<U+200D>", shown.text())
        self.assertIn("<U+E000>", shown.text())
        self.assertEqual(shown.toolTip(), shown.text())
        self.assertEqual(report.changes, (("+", path),))
        self.assertTrue(dialog.execute_button.isEnabled())

    def test_recovery_preview_keeps_original_destination_and_cannot_bypass_hard_block(self):
        from app.core.sync import SyncReport
        from app.core.tasks import new_task
        from app.ui.preview_dialog import PreviewDialog

        task = new_task(str(self.source), direction="download")
        report = SyncReport(updated=1, operation="return", requires_confirmation=True)
        dialog = self.keep(PreviewDialog(task, report, operation="recover"))
        self.assertEqual(dialog.execute_button.text(), "确认并继续未完成操作")
        self.assertIn("手机", dialog.destination_label.text())
        self.assertIn("未完成", dialog.windowTitle())
        self.assertTrue(dialog.execute_button.isEnabled())
        blocked = self.keep(PreviewDialog(task, replace(report, blocked=True), operation="recover"))
        blocked.accept()
        self.assertFalse(blocked.execute_button.isEnabled())
        self.assertEqual(blocked.result(), QDialog.DialogCode.Rejected)

    def test_wizard_is_inert_on_open_and_receive_choice_saves_connection_without_empty_upload_tasks(self):
        from app.ui.wizard import WizardDialog

        self.config.set("mount.enabled", False)
        before = copy.deepcopy(self.config.data)
        with patch("app.ui.wizard.winfsp.is_installed", side_effect=AssertionError("no implicit probe")), patch(
            "app.ui.wizard.MultiFolderDialog", side_effect=AssertionError("receive must not open upload picker"),
        ), patch("app.ui.pages.settings.Rclone", side_effect=AssertionError("no remote IO")):
            dialog = self.keep(WizardDialog(self.config))
            dialog.show()
            QTest.qWait(20)
            self.assertFalse(dialog.busy)
            self.assertEqual(self.config.data, before)
            self.assertFalse(dialog.add_folders_button.isEnabled())
            dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData("download"))
            self.assertFalse(dialog.add_folders_button.isEnabled())
            self.assertIn("完成", dialog.receive_hint.text())
            self.assertIn("下载接收", dialog.receive_hint.text())
            self.assertIn("已有", dialog.receive_hint.text())
            self.assertEqual((dialog.interval_spin.minimum(), dialog.interval_spin.maximum()), (1, 1440))
            self.assertEqual((dialog.retention_spin.minimum(), dialog.retention_spin.maximum()), (1, 3650))
            dialog.finish_button.click()
            self.wait_for(lambda: dialog.result() == QDialog.DialogCode.Accepted)
            self.assertFalse(dialog.busy)
            self.assertEqual(self.config.tasks, ())
            self.assertTrue(self.config.path.exists())
            self.assertEqual(self.config.url, "https://phone.invalid/dav")

    def test_upload_wizard_choice_preserves_existing_task_and_new_task_defaults_to_paused(self):
        from app.core.tasks import new_task
        from app.ui.wizard import WizardDialog

        existing_source = self.base / "existing"
        existing_source.mkdir()
        existing = new_task(str(existing_source))
        self.config.upsert_task(existing)
        self.config.set("mount.enabled", False)

        class FakeFolderDialog(QDialog):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.folders = [str(self_source)]

            def exec(self):
                return QDialog.DialogCode.Accepted

        self_source = self.source
        with patch("app.ui.wizard.MultiFolderDialog", FakeFolderDialog), patch(
            "app.ui.pages.settings.Rclone", side_effect=AssertionError("no remote IO"),
        ):
            dialog = self.keep(WizardDialog(self.config))
            dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData("upload"))
            self.assertTrue(dialog.add_folders_button.isEnabled())
            dialog.add_folders_button.click()
            self.assertEqual(dialog.folder_list.count(), 1)
            self.assertIn("本地历史", dialog.folder_list.item(0).text())
            self.assertEqual(len(self.config.tasks), 1)
            dialog.interval_spin.setValue(1)
            dialog.finish_button.click()
            self.wait_for(lambda: dialog.result() == QDialog.DialogCode.Accepted)
            self.assertEqual(len(self.config.tasks), 2)
            self.assertEqual(self.config.get_task(existing.id), existing)
            task = next(task for task in self.config.tasks if task.id != existing.id)
            self.assertEqual(task.direction, "upload")
            self.assertFalse(task.enabled)
            self.assertEqual(task.interval_minutes, 1)

    def test_wizard_auto_schedule_is_opt_in_independent_and_applies_only_to_new_uploads(self):
        from app.core.tasks import new_task
        from app.ui.wizard import WizardDialog

        existing = replace(new_task(str(self.base / "existing")), auto_sync=False)
        self.config.upsert_task(existing)
        self.config.set("mount.enabled", False)
        source = self.source

        class FakeFolderDialog(QDialog):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.folders = [str(source)]

            def exec(self):
                return QDialog.DialogCode.Accepted

        with patch("app.ui.wizard.MultiFolderDialog", FakeFolderDialog), patch(
            "app.ui.pages.settings.Rclone", side_effect=AssertionError("no remote IO"),
        ):
            dialog = self.keep(WizardDialog(self.config))
            self.assertFalse(dialog.auto_sync_check.isChecked())
            self.assertFalse(dialog.auto_sync_check.isEnabled())
            dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData("upload"))
            self.assertTrue(dialog.auto_sync_check.isEnabled())
            self.assertFalse(dialog.interval_spin.isEnabled())
            dialog.enable_tasks_check.setChecked(True)
            self.assertFalse(dialog.auto_sync_check.isChecked())
            dialog.auto_sync_check.setChecked(True)
            self.assertTrue(dialog.interval_spin.isEnabled())
            dialog.auto_sync_check.setChecked(False)
            self.assertTrue(dialog.enable_tasks_check.isChecked())
            dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData("download"))
            self.assertFalse(dialog.auto_sync_check.isEnabled())
            dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData("upload"))
            dialog.add_folders_button.click()
            dialog.finish_button.click()
            self.wait_for(lambda: dialog.result() == QDialog.DialogCode.Accepted)
            self.assertEqual(self.config.get_task(existing.id), existing)
            new = next(task for task in self.config.tasks if task.id != existing.id)
            self.assertTrue(new.enabled)
            self.assertFalse(new.auto_sync)
            self.assertEqual(Config(self.config.path).get_task(new.id), new)

            source = self.base / "scheduled"
            source.mkdir()
            scheduled = self.keep(WizardDialog(self.config))
            scheduled.direction_combo.setCurrentIndex(scheduled.direction_combo.findData("upload"))
            scheduled.enable_tasks_check.setChecked(True)
            scheduled.auto_sync_check.setChecked(True)
            scheduled.add_folders_button.click()
            scheduled.finish_button.click()
            self.wait_for(lambda: not scheduled.busy)
            self.assertEqual(scheduled.result(), QDialog.DialogCode.Accepted, scheduled.notice_label.text())
            added = next(task for task in self.config.tasks if task.local_path == str(source))
            self.assertTrue(added.enabled)
            self.assertTrue(added.auto_sync)
            self.assertEqual(self.config.get_task(new.id), new)
            self.assertEqual(self.config.get_task(existing.id), existing)

    def test_wizard_retains_explicit_operation_until_thread_exits_even_when_window_is_deleted(self):
        from app.ui.wizard import WizardDialog

        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def slow_probe():
            started.set()
            release.wait(2)
            finished.set()
            return False

        with patch("app.ui.wizard.winfsp.is_installed", side_effect=slow_probe), patch("sys.excepthook") as callback_errors:
            dialog = WizardDialog(self.config)
            dialog.show()
            dialog.winfsp_check_button.click()
            self.wait_for(started.is_set)
            dialog.close()
            self.assertTrue(dialog.busy)
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            release.set()
            self.wait_for(finished.is_set)
            QTest.qWait(30)
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            callback_errors.assert_not_called()
        self.assertFalse(self.config.path.exists())

    def test_dialogs_fit_small_available_screen_and_long_preview_paths_remain_selectable(self):
        from app.core.sync import SyncReport
        from app.core.tasks import new_task
        from app.ui.preview_dialog import PreviewDialog
        from app.ui.remote_folder_dialog import RemoteFolderDialog
        from app.ui.task_dialog import TaskDialog
        from app.ui.wizard import WizardDialog

        geometry = QRect(0, 0, 620, 470)
        screen = SimpleNamespace(availableGeometry=lambda: geometry)
        long_path = str(self.base / ("long-path-" * 60))
        task = replace(new_task(str(self.source)), local_path=long_path)
        with patch.object(QDialog, "screen", return_value=screen):
            dialogs = [
                self.keep(TaskDialog(self.config, operational=False)),
                self.keep(WizardDialog(self.config, operational=False)),
                self.keep(PreviewDialog(task, SyncReport())),
                self.keep(RemoteFolderDialog(self.config, operational=False)),
            ]
            for dialog in dialogs:
                dialog.show()
                self.app.processEvents()
                with self.subTest(dialog=type(dialog).__name__):
                    self.assertLessEqual(dialog.width(), geometry.width())
                    self.assertLessEqual(dialog.height(), geometry.height())
        preview = dialogs[2]
        self.assertIn(long_path, preview.source_label.text())
        self.assertTrue(preview.source_label.wordWrap())
        self.assertTrue(preview.source_label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse)
        self.assertGreater(preview.scroll.verticalScrollBar().maximum(), 0)

    def test_dialogs_fit_actual_available_screen_with_current_dpi(self):
        from app.core.sync import SyncReport
        from app.core.tasks import new_task
        from app.ui.preview_dialog import PreviewDialog
        from app.ui.remote_folder_dialog import RemoteFolderDialog
        from app.ui.task_dialog import TaskDialog
        from app.ui.wizard import WizardDialog

        for dialog in (
            self.keep(TaskDialog(self.config, operational=False)),
            self.keep(WizardDialog(self.config, operational=False)),
            self.keep(PreviewDialog(new_task(str(self.source)), SyncReport())),
            self.keep(RemoteFolderDialog(self.config, operational=False)),
        ):
            dialog.show()
            self.app.processEvents()
            available = dialog.screen().availableGeometry()
            with self.subTest(dialog=type(dialog).__name__):
                self.assertLessEqual(dialog.width(), available.width())
                self.assertLessEqual(dialog.height(), available.height())

    def test_remote_cancel_reaps_thread_after_picker_is_deleted_without_accepting_late_results(self):
        from app.ui.remote_folder_dialog import RemoteFolderDialog

        started, cancelled, finished = threading.Event(), threading.Event(), threading.Event()
        release = threading.Event()
        private_paths = []

        class SlowRclone:
            def __init__(self, config, conf_path=None):
                self.conf_path = Path(conf_path)
                private_paths.append(self.conf_path)

            def ensure_conf(self):
                self.conf_path.write_text("fake", encoding="utf-8")

            def run(self, args, **kwargs):
                started.set()
                release.wait(2)
                finished.set()
                return Result(True, '[{"Path":"late","IsDir":true}]', "", 0)

            def cancel(self):
                cancelled.set()

        with patch("app.ui.remote_folder_dialog.Rclone", SlowRclone):
            dialog = RemoteFolderDialog(self.config)
            dialog.show()
            dialog.refresh_button.click()
            self.wait_for(started.is_set)
            dialog.close()
            self.assertTrue(cancelled.is_set())
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertEqual(dialog.remote_path, "")
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            release.set()
            self.wait_for(finished.is_set)
            self.wait_for(lambda: bool(private_paths) and not private_paths[0].parent.exists())
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_remote_picker_safe_mode_never_constructs_a_client_even_on_explicit_accept(self):
        from app.ui.remote_folder_dialog import RemoteFolderDialog

        with patch("app.ui.remote_folder_dialog.Rclone", side_effect=AssertionError("no network client")):
            dialog = self.keep(RemoteFolderDialog(self.config, operational=False))
            dialog.show()
            dialog.refresh_button.click()
            dialog.accept()
            self.app.processEvents()
            self.assertFalse(dialog.busy)
            self.assertEqual(dialog.remote_path, "")
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertIn("安全", dialog.status_label.text())


if __name__ == "__main__":
    unittest.main()
