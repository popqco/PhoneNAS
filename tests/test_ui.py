from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from app.core.config import Config
from app.core.sync import SyncReport
from app.core.tasks import new_task
from app.ui.main_window import MainWindow
from app.ui.pages.settings import SettingsPage, _ConnectionInput, _write_connection
from app.ui.widgets import apply_theme


class ProgressWorker(QObject):
    """Worker boundary with real Qt signals, without starting any I/O."""

    progress = Signal(str, str)
    progress_event = Signal(str, object)
    finished_ok = Signal(str, object, str)
    failed = Signal(str, str)
    cancelled_result = Signal(str, str)
    guarded = Signal(str, object, str)
    preview_ready = Signal(str, object, str)
    finished = Signal()

    def __init__(self, config, task, parent=None, **kwargs):
        super().__init__(parent)
        self.task = task
        self.preview_only = kwargs.get("preview_only", False)
        self.approved_digest = kwargs.get("approved_digest", "")
        self.operation = kwargs.get("operation") or ("receive" if task.direction == "download" else "publish")
        self.outcome = "pending"
        self.running = False
        self.stop_calls = 0
        self.stop_result = True

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def stop(self):
        self.stop_calls += 1
        return self.stop_result

    def finish(self):
        self.running = False
        self.finished.emit()

    def complete_preview(self, report: SyncReport, reason: str = ""):
        self.outcome = "preview"
        self.preview_ready.emit(self.task.id, report, reason)
        self.finish()


class DesktopRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.enterContext(patch.dict(os.environ, {
            "PHONENAS_DATA_DIR": str(self.base / "app-data"),
            "APPDATA": str(self.base / "profile"),
        }))
        self.source = self.base / "source"
        self.source.mkdir()
        self.config = Config(self.base / "config.json")
        self.config.set("nas.url", "http://fixture.invalid:8888")
        self.task = replace(new_task(str(self.source)), last_sync_at="2026-11-14 09:40:00", last_file_count=5)
        self.config.upsert_task(self.task)

    def simulated_window(self, *, operational=False, first_run=False, with_tray=False):
        self.enterContext(patch("app.ui.main_window.SyncWorker", ProgressWorker))
        self.enterContext(patch("app.ui.main_window.MountSupervisor.start", side_effect=AssertionError("must not mount")))
        with (patch("app.ui.main_window.QSystemTrayIcon.isSystemTrayAvailable", return_value=with_tray),
              patch("app.ui.main_window.QSystemTrayIcon.show")):
            window = MainWindow(self.config, operational=operational or with_tray, first_run=first_run)
        self.addCleanup(self.dispose_window, window)
        window.operational = True
        window.refresh_status()
        return window

    def progress_window(self, *, preview_only=False):
        window = self.simulated_window()
        if preview_only:
            window.preview_task(self.task.id)
        else:
            window.scheduler.request(self.task.id, manual=False)
            self.app.processEvents()
        self.assertIsInstance(window.sync_worker, ProgressWorker)
        self.assertEqual(window.sync_worker.preview_only, preview_only)
        return window, window.sync_worker

    def dispose_window(self, window):
        window.operational = False
        window.sync_worker = None
        window.quit_app()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_structured_worker_bytes_show_phase_progress_on_card_statusbar_and_overview(self):
        from app.core.progress import SyncProgress

        window, worker = self.progress_window()
        card = window.mirror_page.cards[self.task.id]
        worker.progress_event.emit(self.task.id, SyncProgress(
            "staging", "正在准备已确认的修改；尚未覆盖目标…",
            bytes_done=37 * 1048576, bytes_total=100 * 1048576,
        ))

        self.assertEqual((card.progress.minimum(), card.progress.maximum(), card.progress.value()), (0, 100, 37))
        for feedback in (card.message_label.fullText(), window.statusBar().currentMessage(), window.status_page.state_label.text()):
            self.assertIn("正在准备已确认的修改", feedback)
            self.assertIn("本阶段 37%", feedback)
            self.assertIn("37.0 / 100.0 MiB", feedback)
        self.assertIs(window.mirror_page.cards[self.task.id], card)
        self.assertEqual(self.config.get_task(self.task.id).last_sync_at, self.task.last_sync_at)

    def test_legacy_worker_output_is_logged_without_replacing_structured_phase_feedback(self):
        from app.core.progress import SyncProgress

        window, worker = self.progress_window()
        card = window.mirror_page.cards[self.task.id]
        worker.progress_event.emit(self.task.id, SyncProgress("verify_destination", "正在核对目标内容…"))
        structured = card.message_label.fullText()
        legacy = "正在传输 · 99% · unrelated legacy output"
        with self.assertLogs("PhoneNAS", level="INFO") as captured:
            worker.progress.emit(self.task.id, legacy)

        self.assertEqual(card.message_label.fullText(), structured)
        self.assertEqual(window.statusBar().currentMessage(), structured)
        self.assertIn(structured, window.status_page.state_label.text())
        self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
        self.assertIn(legacy, window.status_page.output.toPlainText())
        self.assertIn(legacy, "\n".join(captured.output))

    def test_blocking_phase_gets_elapsed_feedback_without_extra_output_or_log_heartbeats(self):
        from app.core.progress import SyncProgress

        window, worker = self.progress_window()
        card = window.mirror_page.cards[self.task.id]
        worker.progress_event.emit(self.task.id, SyncProgress("inspect_remote", "正在校验手机文件内容…"))
        output = window.status_page.output.toPlainText()
        with self.assertNoLogs("PhoneNAS", level="INFO"):
            for _ in range(25):
                QTest.qWait(100)
                if "已用时" in card.message_label.fullText():
                    break

        self.assertIn("已用时 00:0", card.message_label.fullText())
        self.assertEqual(window.statusBar().currentMessage(), card.message_label.fullText())
        self.assertIn(card.message_label.fullText(), window.status_page.state_label.text())
        self.assertEqual(window.status_page.output.toPlainText(), output)
        self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
        for invented in ("%", "MiB", "项", "/s", "文件数"):
            self.assertNotIn(invented, card.message_label.fullText())

    def test_same_phase_samples_keep_elapsed_and_missing_bytes_reset_the_bar_immediately(self):
        from app.core.progress import SyncProgress

        window, worker = self.progress_window()
        card = window.mirror_page.cards[self.task.id]
        with patch("app.ui.main_window.monotonic", return_value=10.0) as clock:
            worker.progress_event.emit(self.task.id, SyncProgress("staging", "正在准备修改…", 25, 100, 1, 4))
            clock.return_value = 75.0
            worker.progress_event.emit(self.task.id, SyncProgress("staging", "正在准备下一个文件…", 50, 100, 2, 4))
            self.assertIn("已用时 01:05", card.message_label.fullText())
            self.assertIn("本阶段 50%", card.message_label.fullText())
            self.assertIn("2 / 4 项", card.message_label.fullText())

            clock.return_value = 78.0
            worker.progress_event.emit(self.task.id, SyncProgress("staging", "正在等待可用的字节统计…"))
            self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
            self.assertIn("已用时 01:08", card.message_label.fullText())
            for stale in ("%", "MiB", "项"):
                self.assertNotIn(stale, card.message_label.fullText())

            clock.return_value = 90.0
            worker.progress_event.emit(self.task.id, SyncProgress("check_staged", "正在校验暂存内容…"))
            self.assertNotIn("已用时", card.message_label.fullText())
            self.assertEqual(window.statusBar().currentMessage(), card.message_label.fullText())
            self.assertIn(card.message_label.fullText(), window.status_page.state_label.text())

    def test_cancelling_replaces_phase_feedback_immediately_and_ignores_late_updates(self):
        from app.core.progress import SyncProgress

        window, worker = self.progress_window()
        card = window.mirror_page.cards[self.task.id]
        worker.progress_event.emit(self.task.id, SyncProgress("applying", "正在上传文件…", 75, 100))
        stop = worker.stop

        def stop_while_running():
            self.assertEqual(window.states[self.task.id]["state"], "running")
            return stop()

        worker.stop_result = None  # Older doubles may not return a value.
        with (patch("app.ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes),
              patch.object(worker, "stop", side_effect=stop_while_running)):
            card.cancel_button.click()

        self.assertEqual(worker.stop_calls, 1)
        self.assertEqual(card.badge.text(), "正在取消")
        self.assertFalse(card.cancel_button.isEnabled())
        self.assertEqual(window.statusBar().currentMessage(), card.message_label.fullText())
        self.assertIn("正在取消", window.statusBar().currentMessage())
        self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
        cancelled_feedback = card.message_label.fullText()
        worker.progress_event.emit(self.task.id, SyncProgress("verify_source", "迟到的核对进度"))
        worker.progress.emit(self.task.id, "正在传输 · 100% · 迟到的传输输出")
        QTest.qWait(1200)
        self.assertEqual(card.message_label.fullText(), cancelled_feedback)
        self.assertEqual(window.statusBar().currentMessage(), cancelled_feedback)
        self.assertIn(cancelled_feedback, window.status_page.state_label.text())
        self.assertEqual(self.config.get_task(self.task.id).last_sync_at, self.task.last_sync_at)

    def test_rejected_worker_stop_preserves_active_feedback(self):
        from app.core.progress import SyncProgress

        window, worker = self.progress_window()
        worker.stop_result = False  # Final verification/saving cannot be interrupted.
        worker.progress_event.emit(self.task.id, SyncProgress("saving", "正在保存已核对的结果…"))
        card = window.mirror_page.cards[self.task.id]
        previous = window.states[self.task.id].copy()
        feedback = window.statusBar().currentMessage()
        with patch("app.ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            card.cancel_button.click()
        self.assertEqual(worker.stop_calls, 1)
        self.assertEqual(window.states[self.task.id], previous)
        self.assertEqual(window.statusBar().currentMessage(), feedback)
        self.assertTrue(window._progress_timer.isActive())
        self.assertTrue(card.cancel_button.isEnabled())

    def test_cancel_confirmation_cannot_outlive_the_worker_or_its_pending_state(self):
        from app.core.progress import SyncProgress

        for late_result in ("success_signal", "success_outcome", "thread_stopped", "replacement", "quitting"):
            with self.subTest(late_result=late_result):
                self.config.upsert_task(self.task)
                window, worker = self.progress_window()
                worker.progress_event.emit(self.task.id, SyncProgress("applying", "正在写入目标…", 2, 3))
                active_worker = worker
                expected = {}

                def finish_in_modal(*_args):
                    nonlocal active_worker
                    if late_result == "success_signal":
                        worker.outcome = "success"
                        worker.finished_ok.emit(self.task.id, SyncReport(added=1), "2026-11-14_120000")
                    elif late_result == "success_outcome":
                        worker.outcome = "success"  # The queued result has not reached the GUI yet.
                    elif late_result == "thread_stopped":
                        worker.running = False
                    elif late_result == "replacement":
                        window.scheduler.request(self.task.id, manual=True)
                        worker.failed.emit(self.task.id, "先前尝试结束")
                        worker.finish()
                        self.app.processEvents()
                        active_worker = window.sync_worker
                        self.assertIsNot(active_worker, worker)
                        self.assertEqual(active_worker.task.id, worker.task.id)
                    else:
                        window._quitting = True
                    expected.update(state=window.states[self.task.id].copy(), feedback=window.statusBar().currentMessage(),
                                    timer=window._progress_timer.isActive())
                    return QMessageBox.StandardButton.Yes

                with patch("app.ui.main_window.QMessageBox.question", side_effect=finish_in_modal):
                    window.cancel_task(self.task.id)

                self.assertIs(window.sync_worker, active_worker)
                self.assertEqual(worker.stop_calls, 0)
                self.assertEqual(active_worker.stop_calls, 0)
                self.assertEqual(window.states[self.task.id], expected["state"])
                self.assertEqual(window.statusBar().currentMessage(), expected["feedback"])
                self.assertEqual(window._progress_timer.isActive(), expected["timer"])
                window._quitting = False
                window.quit_app()

    def test_terminal_worker_results_clear_phase_feedback_and_never_accept_late_progress(self):
        from app.core.progress import SyncProgress

        for outcome in ("preview_ready", "guarded", "failed", "cancelled_result", "finished_ok"):
            with self.subTest(outcome=outcome):
                self.config.upsert_task(self.task)
                window, worker = self.progress_window(preview_only=outcome == "preview_ready")
                card = window.mirror_page.cards[self.task.id]
                worker.progress_event.emit(self.task.id, SyncProgress("applying", "本阶段传输结束；仍需核对内容…", 100, 100))
                self.assertEqual(card.progress.value(), 100)
                self.assertEqual(self.config.get_task(self.task.id).last_sync_at, self.task.last_sync_at)
                self.assertNotIn("完成", card.badge.text())
                if outcome == "finished_ok":
                    worker.finished_ok.emit(self.task.id, SyncReport(added=1, unchanged=7), "2026-11-14_120000")
                elif outcome in ("preview_ready", "guarded"):
                    getattr(worker, outcome).emit(self.task.id, SyncReport(added=1), "需确认差异" if outcome == "guarded" else "")
                else:
                    getattr(worker, outcome).emit(self.task.id, "操作已停止且未记为成功")

                self.assertTrue(card.progress.isHidden())
                self.assertNotIn("已用时", card.message_label.fullText())
                self.assertNotIn("本阶段", card.message_label.fullText())
                self.assertIn(window.statusBar().currentMessage().removeprefix("任务失败："), card.message_label.fullText())
                terminal_feedback = (card.message_label.fullText(), window.statusBar().currentMessage(), window.status_page.state_label.text())
                worker.progress_event.emit(self.task.id, SyncProgress("cleanup", "迟到的清理进度"))
                worker.progress.emit(self.task.id, "正在传输 · 99% · 迟到的旧输出")
                QTest.qWait(1100)
                self.assertEqual((card.message_label.fullText(), window.statusBar().currentMessage(), window.status_page.state_label.text()), terminal_feedback)
                self.assertTrue(window.busy, "Serial ownership lasts until worker.finished, not a result signal")
                self.assertFalse(card.preview_button.isEnabled())
                saved = self.config.get_task(self.task.id)
                if outcome == "finished_ok":
                    self.assertNotEqual(saved.last_sync_at, self.task.last_sync_at)
                    self.assertEqual(saved.last_file_count, 8)
                else:
                    self.assertEqual((saved.last_sync_at, saved.last_file_count), (self.task.last_sync_at, self.task.last_file_count))

    def test_finished_task_relaunch_has_fresh_elapsed_and_keeps_other_tasks_serial(self):
        from app.core.progress import SyncProgress

        window, worker = self.progress_window()
        other = new_task(str(self.base / "other"), "Other")
        self.config.upsert_task(other)
        window.scheduler.update_tasks(self.config.tasks)
        with patch("app.ui.main_window.monotonic", return_value=10.0) as clock:
            worker.progress_event.emit(self.task.id, SyncProgress("staging", "第一轮正在准备…", 80, 100))
            clock.return_value = 75.0
            worker.progress_event.emit(self.task.id, SyncProgress("staging", "第一轮继续准备…", 90, 100))
            self.assertIn("已用时 01:05", window.mirror_page.cards[self.task.id].message_label.fullText())
            window.scheduler.request(other.id, manual=False)
            self.app.processEvents()
            self.assertIs(window.sync_worker, worker)
            worker.failed.emit(self.task.id, "第一轮已停止")
            window.run_sync(self.task.id)
            self.app.processEvents()
            self.assertIs(window.sync_worker, worker, "A terminal result must not release the serial slot early")
            worker.finish()
            self.app.processEvents()
            second_worker = window.sync_worker
            self.assertIsInstance(second_worker, ProgressWorker)
            self.assertEqual(second_worker.task.id, other.id)
            second_card = window.mirror_page.cards[other.id]
            self.assertEqual((second_card.progress.minimum(), second_card.progress.maximum()), (0, 0))
            clock.return_value = 100.0
            second_worker.progress_event.emit(other.id, SyncProgress("staging", "第二个任务开始准备…"))
            self.assertNotIn("已用时", second_card.message_label.fullText())
            self.assertNotIn("第一轮", second_card.message_label.fullText())
            second_worker.failed.emit(other.id, "第二个任务已停止")
            second_worker.finish()
            self.app.processEvents()
            fresh_worker = window.sync_worker
            self.assertEqual(fresh_worker.task.id, self.task.id)
            self.assertTrue(fresh_worker.preview_only)
            self.assertEqual(fresh_worker.approved_digest, "")
            fresh_card = window.mirror_page.cards[self.task.id]
            self.assertEqual(fresh_card.message_label.fullText(), "正在校验差异…")
            self.assertEqual(window.statusBar().currentMessage(), fresh_card.message_label.fullText())
            self.assertIn(fresh_card.message_label.fullText(), window.status_page.state_label.text())
            clock.return_value = 125.0
            fresh_worker.progress_event.emit(self.task.id, SyncProgress("staging", "同一任务重新开始准备…"))
            self.assertNotIn("已用时", fresh_card.message_label.fullText())
            self.assertNotIn("%", fresh_card.message_label.fullText())
            self.assertEqual(window.scheduler.queue.active_id, self.task.id)

    def test_changing_nas_identity_invalidates_old_success_counts(self):
        _write_connection(self.config, _ConnectionInput("http://other.invalid:8888", "admin", ""))
        task = self.config.get_task(self.task.id)
        self.assertEqual((task.last_sync_at, task.last_file_count), ("", 0))

    def test_imported_success_is_not_authority_for_an_unknown_target(self):
        exported = self.base / "export.json"
        self.config.export_to(exported)
        self.config.import_from(exported)
        task = self.config.get_task(self.task.id)
        self.assertEqual((task.last_sync_at, task.last_file_count), ("", 0))

    def test_global_sync_can_be_enabled_through_settings_after_legacy_disable(self):
        self.config.set("mirror.enabled", False)
        page = SettingsPage(self.config)
        self.assertFalse(page.mirror_enabled_check.isChecked())
        page.mirror_enabled_check.setChecked(True)
        with patch("app.ui.pages.settings.set_autostart", return_value=(True, "test only")):
            page.save()
            for _ in range(100):
                if not page.busy:
                    break
                QTest.qWait(10)
        self.assertFalse(page.busy)
        self.assertTrue(self.config.get("mirror.enabled"))
        self.assertTrue(page.shutdown())
        page.deleteLater()
        self.app.processEvents()

    def test_upgrade_pause_survives_delayed_start_and_only_resumes_after_persisted_approval(self):
        self.config.set("mount.auto_mount", False)
        self.config.save()
        tasks = self.config.tasks
        callbacks = {}
        with patch.object(QTimer, "singleShot", side_effect=lambda ms, _context, slot: callbacks.__setitem__(ms, slot)):
            window = self.simulated_window(operational=True)
        self.assertTrue(window.scheduler.paused)
        self.assertFalse(window._schedule_notice.isHidden())
        self.assertIn("文件名校验", window._schedule_notice.text())
        self.assertIn("继续自动计划", window.mirror_page.pause_button.text())
        self.assertFalse(window.scheduler._started)
        callbacks[2500]()
        self.app.processEvents()
        self.assertTrue(window.scheduler._started)
        self.assertTrue(window.scheduler.paused)
        self.assertEqual(window.scheduler.queue.queued_ids, ())
        self.assertIsNone(window.sync_worker)
        self.assertEqual(self.config.get("app.schedule_approval", ""), "")

        def answer(button):
            def click(box):
                self.assertIs(box.defaultButton(), box.button(QMessageBox.StandardButton.No))
                return button.value
            return patch.object(QMessageBox, "exec", new=click)

        with answer(QMessageBox.StandardButton.No):
            window.mirror_page.pause_button.click()
        self.assertTrue(window.scheduler.paused)
        self.assertEqual(Config(self.config.path).get("app.schedule_approval", ""), "")
        with (answer(QMessageBox.StandardButton.Yes), patch.object(window, "_message") as warning,
              patch.object(self.config, "save", side_effect=OSError("test save failure"))):
            window.mirror_page.pause_button.click()
            warning.assert_called_once()
        self.assertTrue(window.scheduler.paused)
        self.assertEqual(self.config.get("app.schedule_approval", ""), "")
        with answer(QMessageBox.StandardButton.Yes):
            window.mirror_page.pause_button.click()
        self.assertFalse(window.scheduler.paused)
        self.assertTrue(window._schedule_notice.isHidden())
        reloaded = Config(self.config.path)
        self.assertEqual(reloaded.get("app.schedule_approval", ""), "1.2.4-filenames")
        self.assertEqual(reloaded.tasks, tasks)
        self.app.processEvents()
        self.assertFalse(window.sync_worker.preview_only)
        self.assertEqual(window.sync_worker.approved_digest, "")
        window.toggle_pause()
        self.assertTrue(window.scheduler.paused)
        self.assertEqual(Config(self.config.path).get("app.schedule_approval", ""), "1.2.4-filenames")
        with patch.object(QTimer, "singleShot"):
            self.config = reloaded
            restarted = self.simulated_window(operational=True)
        self.assertFalse(restarted.scheduler.paused)
        self.assertTrue(restarted._schedule_notice.isHidden())

    def test_import_reapplies_pause_before_updating_due_tasks(self):
        self.config.set("mount.auto_mount", False)
        self.config.set("app.schedule_approval", "1.2.4-filenames")
        self.config.save()
        with patch.object(QTimer, "singleShot"):
            window = self.simulated_window(operational=True)
        window._start_scheduler()
        self.assertFalse(window.scheduler.paused)
        exported = self.base / "exported.json"
        self.config.export_to(exported)
        self.config.import_from(exported)
        window.settings_page.saved.emit()
        self.app.processEvents()
        self.assertTrue(window.scheduler.paused)
        self.assertIsNone(window.sync_worker)
        self.assertFalse(window._schedule_notice.isHidden())
        self.assertEqual(self.config.get("app.schedule_approval", ""), "")
        self.assertEqual((self.config.tasks[0].auto_sync, self.config.tasks[0].interval_minutes),
                         (self.task.auto_sync, self.task.interval_minutes))

    def test_delayed_existing_profile_callbacks_do_not_restart_after_quit_begins(self):
        callbacks = {}
        with patch.object(QTimer, "singleShot", side_effect=lambda ms, _context, slot: callbacks.__setitem__(ms, slot)):
            window = self.simulated_window(operational=True)
        window.preview_task(self.task.id)
        worker = window.sync_worker
        window.mount._retry_timer.start(5000)  # Simulate a pending retry without mounting.
        window.quit_app()
        self.assertIs(window.sync_worker, worker)
        self.assertFalse(window.mount._retry_timer.isActive())
        callbacks[400]()
        callbacks[2500]()
        self.app.processEvents()
        self.assertFalse(window.scheduler._started)
        self.assertTrue(window.scheduler.paused)
        self.assertIs(window.sync_worker, worker)
        worker.finish()

    def test_saving_first_task_after_skipped_setup_starts_the_schedule(self):
        config = Config(self.base / "first.json")
        config.set("mount.auto_mount", False)
        with patch("app.ui.main_window.MainWindow.open_wizard"), patch("app.ui.main_window.MountSupervisor.start", side_effect=AssertionError("must not mount")):
            window = MainWindow(config, first_run=True)
            self.assertFalse(window.scheduler._started)
            window._store_task(self.task)
            self.assertTrue(window.scheduler._started)
            window.quit_app()
            self.app.processEvents()
            window.deleteLater()
            self.app.processEvents()

    def test_completed_statusbar_and_history_events_use_readable_results(self):
        with patch("app.ui.main_window.MountSupervisor.start", side_effect=AssertionError("must not mount")):
            window = MainWindow(self.config, operational=False)
            window.statusBar().showMessage("Copied (new) 100% ETA 0s")
            from app.core.sync import SyncReport
            window._on_sync_ok(self.task.id, SyncReport(added=1), "2026-11-14_120000")
            self.assertIn("完成", window.statusBar().currentMessage())
            self.assertNotIn("ETA", window.statusBar().currentMessage())
            window._history_event({"task": self.task.id, "task_name": "工作图纸", "text": "恢复完成"})
            self.assertEqual(window.events[0]["task"], "工作图纸")
            window.quit_app()
            self.app.processEvents()
            window.deleteLater()
            self.app.processEvents()

    def test_safe_window_delayed_callbacks_never_spawn_or_access_real_data(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        with patch("app.ui.main_window.MountSupervisor.start", side_effect=AssertionError("must not mount")), patch("app.ui.main_window.SyncWorker.start", side_effect=AssertionError("must not synchronize")):
            window = MainWindow(self.config, operational=False)
            window.show()
            QTest.qWait(2800)
            self.assertIsNone(window.sync_worker)
            self.assertFalse(window.scheduler._started)
            window.quit_app()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_accepting_setup_with_mount_disabled_never_starts_mount(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        self.config.set("mount.enabled", False)
        self.config.set("mount.auto_mount", True)
        self.config.set("mirror.enabled", False)
        with patch("app.ui.main_window.WizardDialog") as wizard, patch("app.ui.main_window.set_autostart"), patch("app.ui.main_window.MountSupervisor.start") as mount:
            window = MainWindow(self.config, first_run=True)
            wizard.return_value.exec.return_value = 1
            window.open_wizard()
            mount.assert_not_called()
            window.quit_app()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_overview_and_tray_sync_all_require_each_task_to_confirm_a_fresh_digest(self):
        download = replace(new_task(str(self.base / "receive"), direction="download"), auto_sync=False)
        disabled = replace(new_task(str(self.base / "disabled")), enabled=False)
        self.config.replace_tasks((self.task, download, disabled))
        for entry in ("overview", "tray"):
            with self.subTest(entry=entry):
                window = self.simulated_window(with_tray=entry == "tray")
                window.scheduler.pause()
                if entry == "overview":
                    window.status_page.sync_button.click()
                else:
                    self.assertTrue(window.isHidden())
                    window.tray.contextMenu().actions()[1].trigger()
                    self.assertTrue(window.isVisible())
                self.app.processEvents()
                first = window.sync_worker
                self.assertEqual((first.task.id, first.operation, first.preview_only, first.approved_digest),
                                 (self.task.id, "publish", True, ""))
                self.assertEqual(window.scheduler.queue.queued_ids, (download.id,))
                preview_report = SyncReport(operation="publish", digest="first-preview")
                with patch("app.ui.main_window.PreviewDialog") as preview:
                    def reject_first():
                        self.app.processEvents()  # Simulate the nested modal loop.
                        self.assertIsNone(window.sync_worker)
                        self.assertEqual(window.scheduler.queue.active_id, self.task.id)
                        return QDialog.DialogCode.Rejected
                    preview.return_value.exec.side_effect = reject_first
                    first.complete_preview(preview_report)
                    self.assertIsNone(window.sync_worker)
                self.app.processEvents()
                second = window.sync_worker
                self.assertEqual((second.task.id, second.operation, second.preview_only, second.approved_digest),
                                 (download.id, "receive", True, ""))
                with patch("app.ui.main_window.PreviewDialog") as preview:
                    preview.return_value.exec.return_value = QDialog.DialogCode.Accepted
                    second.complete_preview(SyncReport(operation="receive", digest="second-preview"))
                apply = window.sync_worker
                self.assertEqual((apply.task.id, apply.operation, apply.preview_only, apply.approved_digest),
                                 (download.id, "receive", False, "second-preview"))
                self.assertEqual(window.scheduler.queue.active_id, download.id)
                apply.finish()
                self.app.processEvents()
                self.assertIsNone(window.sync_worker)
                self.assertEqual(window.scheduler.queue.queued_ids, ())
                self.assertTrue(window.scheduler.paused)

    def test_skipping_overdue_auto_task_in_sync_all_does_not_immediately_write_it(self):
        self.config.set("app.schedule_approval", "1.2.4-filenames")
        self.config.set("mount.auto_mount", False)
        with patch.object(QTimer, "singleShot"):
            window = self.simulated_window(operational=True)
        window._start_scheduler()
        self.assertFalse(window.scheduler.paused)
        self.assertEqual(window.scheduler.queue.queued_ids, (self.task.id,))
        window.status_page.sync_button.click()
        self.app.processEvents()
        worker = window.sync_worker
        self.assertTrue(worker.preview_only)
        with patch("app.ui.main_window.PreviewDialog") as preview:
            preview.return_value.exec.return_value = QDialog.DialogCode.Rejected
            worker.complete_preview(SyncReport(digest="skipped"))
        window.scheduler._on_timeout()
        self.app.processEvents()
        self.assertIsNone(window.sync_worker)
        self.assertEqual(window.scheduler.queue.queued_ids, ())
        self.assertFalse(window.scheduler.paused)
        self.assertEqual(self.config.get_task(self.task.id), self.task)

    def test_manual_confirmation_cannot_use_empty_blocked_or_outdated_preview(self):
        window = self.simulated_window()
        window.scheduler.pause()
        for case in ("empty", "blocked", "wrong_operation", "changed_task", "quitting"):
            with self.subTest(case=case):
                window.run_sync(self.task.id)
                self.app.processEvents()
                worker = window.sync_worker
                self.assertTrue(worker.preview_only)

                def accept():
                    if case == "changed_task":
                        self.config.upsert_task(replace(self.task, name="Updated"))
                    if case == "quitting":
                        window.quit_app()
                    return QDialog.DialogCode.Accepted

                with patch("app.ui.main_window.PreviewDialog") as preview:
                    preview.return_value.exec.side_effect = accept
                    worker.complete_preview(SyncReport(
                        operation="receive" if case == "wrong_operation" else "publish",
                        digest="" if case == "empty" else "approved",
                        blocked=case == "blocked",
                    ))
                self.assertIsNone(window.sync_worker)
                self.assertIsNone(window.scheduler.queue.active_id)
                self.config.upsert_task(self.task)

    def test_confirmed_relay_claims_serial_slot_and_keeps_cancel_visible(self):
        window = self.simulated_window()
        window.relay_task(self.task.id, "adopt")
        worker = window.sync_worker
        self.assertTrue(worker.preview_only)
        self.assertIsNone(window.scheduler.queue.active_id)
        with patch("app.ui.main_window.PreviewDialog") as preview:
            preview.return_value.exec.return_value = QDialog.DialogCode.Accepted
            worker.complete_preview(SyncReport(operation="adopt", digest="approved"), "核对后接收")
        self.assertEqual(window.scheduler.queue.active_id, self.task.id)
        self.assertEqual(window.sync_worker.operation, "adopt")
        self.assertEqual(window.sync_worker.approved_digest, "approved")
        window.scheduler.queue.due()
        window.refresh_status()
        card = window.mirror_page.cards[self.task.id]
        self.assertFalse(card.cancel_button.isHidden())
        self.assertNotEqual(card.badge.text(), "等待执行")

    def test_explicit_upload_and_download_buttons_preview_actual_direction(self):
        for direction, expected in (("upload", ("publish", "adopt")), ("download", ("return", "receive"))):
            with self.subTest(direction=direction):
                self.config.upsert_task(replace(self.task, direction=direction))
                with patch("app.ui.main_window.MainWindow._launch") as launch:
                    window = MainWindow(self.config, operational=False)
                    window.operational = True
                    card = window.mirror_page.cards[self.task.id]
                    card.upload_button.click()
                    self.assertEqual(launch.call_args.kwargs, {"preview_only": True, "operation": expected[0]})
                    card.download_button.click()
                    self.assertEqual(launch.call_args.kwargs, {"preview_only": True, "operation": expected[1]})
                    self.assertIsNone(window.scheduler.queue.active_id)
                    self.assertEqual(window.scheduler.queue.queued_ids, ())
                    window.quit_app()
                    window.deleteLater()
                    self.app.processEvents()

    def test_due_automatic_request_does_not_hide_running_manual_preview_cancel(self):
        from types import SimpleNamespace
        window = MainWindow(self.config, operational=False)
        window.sync_worker = SimpleNamespace(task=self.task)
        window.states[self.task.id] = {"state": "preview", "message": "正在校验内容"}
        window.scheduler.queue.enqueue(self.task.id)
        window.refresh_status()
        card = window.mirror_page.cards[self.task.id]
        self.assertFalse(card.cancel_button.isHidden())
        self.assertIn("查看差异", card.badge.text())
        window.sync_worker = None
        window.quit_app()
        window.deleteLater()
        self.app.processEvents()

    def test_safe_window_never_mounts_and_completed_cards_keep_next_plan(self):
        with patch("app.ui.main_window.MountSupervisor.start", side_effect=AssertionError("must not mount")):
            window = MainWindow(self.config, operational=False)
            card = window.mirror_page.cards[self.task.id]
            card.set_runtime({"state": "idle", "message": "同步完成"}, "12:00", False)
            self.assertIn("下次计划 12:00", card.message_label.fullText())
            self.assertIn("同步完成", card.message_label.fullText())
            window.quit_app()
            self.app.processEvents()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
