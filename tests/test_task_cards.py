from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel

from app.core.config import Config
from app.core.tasks import new_task
from app.ui.pages.mirror import MirrorPage, TaskCard


def setUpModule():
    global _TEST_HOME, _TEST_ROOT, _TEST_ENV
    _TEST_HOME = tempfile.TemporaryDirectory(prefix="PhoneNAS-task-cards-")
    _TEST_ROOT = Path(_TEST_HOME.name)
    _TEST_ENV = patch.dict(os.environ, {
        "QT_QPA_PLATFORM": "offscreen",
        "PHONENAS_DATA_DIR": str(_TEST_ROOT / "data"),
        "APPDATA": str(_TEST_ROOT / "appdata"),
        "LOCALAPPDATA": str(_TEST_ROOT / "localappdata"),
    })
    _TEST_ENV.start()


def tearDownModule():
    _TEST_ENV.stop()
    _TEST_HOME.cleanup()


class TaskCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.task = new_task(str(self.base / "work"), "Work")

    def keep(self, widget):
        self.addCleanup(self.dispose, widget)
        return widget

    def dispose(self, widget):
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_card_buttons_request_explicit_upload_and_download_intents_without_syncing(self):
        for direction in ("upload", "download"):
            with self.subTest(direction=direction):
                task = replace(self.task, direction=direction)
                card = self.keep(TaskCard(task))
                requested = []
                for name in ("upload", "download", "sync", "preview", "return", "adopt"):
                    getattr(card, name + "_requested").connect(
                        lambda task_id, intent=name: requested.append((intent, task_id))
                    )

                self.assertEqual(card.upload_button.text(), "上传到手机")
                self.assertEqual(card.download_button.text(), "下载到本地")
                self.assertFalse(card.upload_button.isHidden())
                self.assertFalse(card.download_button.isHidden())
                self.assertTrue(card.upload_button.isEnabled())
                self.assertTrue(card.download_button.isEnabled())
                self.assertIs(card.sync_button, card.upload_button if direction == "upload" else card.download_button)
                card.upload_button.click()
                card.download_button.click()

                self.assertEqual(requested, [("upload", task.id), ("download", task.id)])
                self.assertEqual(card.property("state"), "idle")
                self.assertEqual(card.task, task)

    def test_both_manual_intents_obey_enable_and_busy_gates_not_the_automatic_plan(self):
        for direction in ("upload", "download"):
            for auto_sync in (True, False):
                with self.subTest(direction=direction, auto_sync=auto_sync):
                    card = self.keep(TaskCard(replace(self.task, direction=direction, auto_sync=auto_sync)))
                    requested = []
                    card.upload_requested.connect(lambda task_id: requested.append(("upload", task_id)))
                    card.download_requested.connect(lambda task_id: requested.append(("download", task_id)))
                    for state in ("running", "queued", "preview", "cancelling"):
                        card.set_runtime({"state": state}, "12:00", False)
                        for control in (card.upload_button, card.download_button):
                            self.assertFalse(control.isEnabled(), state)
                            control.click()
                    card.set_runtime({"external_busy": True}, "12:00", False)
                    self.assertFalse(card.upload_button.isEnabled())
                    self.assertFalse(card.download_button.isEnabled())
                    card.upload_button.click()
                    card.download_button.click()
                    card.set_interaction_busy(True)
                    card.set_runtime({}, "12:00", True)
                    self.assertFalse(card.upload_button.isEnabled())
                    self.assertFalse(card.download_button.isEnabled())
                    card.upload_button.click()
                    card.download_button.click()
                    self.assertEqual(requested, [])

                    card.set_interaction_busy(False)
                    for state in ("idle", "cancelled", "guarded", "error"):
                        card.set_runtime({"state": state}, "12:00", True)
                        for control in (card.upload_button, card.download_button):
                            self.assertTrue(control.isEnabled(), state)
                            self.assertIn(control.text(), control.toolTip())
                            self.assertIn("先查看差异", control.toolTip())
                            self.assertIn("确认", control.toolTip())
                            self.assertNotIn("默认方向", control.toolTip())
                        card.upload_button.click()
                        card.download_button.click()
                    self.assertEqual(requested, [("upload", self.task.id), ("download", self.task.id)] * 4)

    def test_disabled_task_cannot_request_either_manual_transfer(self):
        for auto_sync in (True, False):
            with self.subTest(auto_sync=auto_sync):
                card = self.keep(TaskCard(replace(self.task, enabled=False, auto_sync=auto_sync)))
                requested = []
                card.upload_requested.connect(requested.append)
                card.download_requested.connect(requested.append)

                for control in (card.upload_button, card.download_button):
                    self.assertFalse(control.isEnabled())
                    self.assertIn("启用任务", control.toolTip())
                    control.click()
                self.assertEqual(requested, [])
                self.assertTrue(card.edit_button.isEnabled())
                self.assertTrue(card.toggle_button.isEnabled())

    def test_default_direction_names_the_source_destination_and_actual_history_location(self):
        for direction, expected_direction, action, source, target, history in (
            ("upload", "本地 → 手机", "上传到手机", str(self.base / "work"), "/backup/work/mirror", "历史 · 手机 /backup/work/versions"),
            ("download", "手机 → 本地", "下载到本地", "/backup/work/mirror", str(self.base / "work"), f"历史 · 本地 {self.base / 'local-history'}"),
        ):
            with self.subTest(direction=direction):
                task = replace(
                    self.task, direction=direction, remote_path="backup/work/mirror",
                    versions_path="backup/work/versions", local_history_path=str(self.base / "local-history"),
                )
                card = self.keep(TaskCard(task))

                self.assertIn(expected_direction, card.direction_label.text())
                self.assertEqual(card.source_label.fullText(), source)
                self.assertEqual(card.target_label.fullText(), target)
                self.assertEqual(card.history_label.fullText(), history)
                self.assertEqual(card.sync_button.text(), action)
                self.assertEqual(card.preview_button.text(), "查看差异")
                self.assertEqual(card.history_button.text(), "历史")
                self.assertEqual(card.edit_button.text(), "设置")

    def test_manual_cards_show_manual_mode_without_an_interval_or_next_plan_even_when_plans_pause(self):
        for enabled in (True, False):
            for paused in (True, False):
                with self.subTest(enabled=enabled, paused=paused):
                    task = replace(self.task, auto_sync=False, enabled=enabled)
                    card = self.keep(TaskCard(task))
                    card.set_runtime({"message": "等待手动操作"}, "12:00", paused)
                    metadata = [item.text() for item in card.meta_widget.findChildren(QLabel)]

                    self.assertIn("仅手动", metadata)
                    self.assertNotIn(f"每 {task.interval_minutes} 分钟", metadata)
                    self.assertEqual(card.message_label.fullText(), "等待手动操作")
                    self.assertNotEqual(card.badge.text(), "计划暂停")
                    self.assertEqual(card.toggle_button.toolTip(), "停用任务，禁止手动操作" if enabled else "启用任务（仅手动，不自动运行）")
                    self.assertEqual(card.upload_button.isEnabled(), enabled)
                    self.assertEqual(card.download_button.isEnabled(), enabled)

        automatic = self.keep(TaskCard(self.task))
        automatic.set_runtime({}, "12:00", False)
        self.assertIn(f"每 {self.task.interval_minutes} 分钟", [item.text() for item in automatic.meta_widget.findChildren(QLabel)])
        self.assertIn("下次计划 12:00", automatic.message_label.fullText())
        automatic.set_runtime({}, "12:00", True)
        self.assertEqual(automatic.badge.text(), "计划暂停")
        self.assertNotIn("下次计划", automatic.message_label.fullText())

    def test_all_manual_workspace_has_no_automatic_schedule_even_when_plans_are_paused(self):
        config = Config(self.base / "config.json")
        other = new_task(str(self.base / "other"), "Other")
        config.replace_tasks((replace(self.task, auto_sync=False), replace(other, auto_sync=False, enabled=False)))
        page = self.keep(MirrorPage(config))
        requests = []
        page.pause_requested.connect(lambda: requests.append("pause-plan"))

        for paused in (False, True):
            with self.subTest(paused=paused):
                page.set_runtime({}, {self.task.id: "12:00", other.id: "12:30"}, paused, "stopped", "", [])
                self.assertEqual(page.schedule_stat.value_label.fullText(), "仅手动")
                self.assertEqual(page.schedule_stat.suffix_label.text(), "")
                self.assertNotIn("12:", page.schedule_stat.detail_label.text())
                self.assertTrue(page.pause_button.isEnabled())
                page.pause_button.click()
        self.assertEqual(requests, ["pause-plan", "pause-plan"])
        self.assertFalse(config.path.exists())

    def test_mixed_workspace_schedule_uses_only_enabled_automatic_tasks(self):
        config = Config(self.base / "config.json")
        manual = replace(self.task, auto_sync=False)
        automatic = new_task(str(self.base / "automatic"), "Automatic")
        disabled = replace(new_task(str(self.base / "disabled"), "Disabled"), enabled=False)
        config.replace_tasks((manual, automatic, disabled))
        page = self.keep(MirrorPage(config))
        next_runs = {manual.id: "09:00", automatic.id: "12:00", disabled.id: "08:00", "removed-task": "07:00"}
        page.set_runtime({}, next_runs, False, "stopped", "", [])

        self.assertEqual(page.schedule_stat.value_label.fullText(), "12:00")
        self.assertEqual(page.schedule_stat.suffix_label.text(), "下次同步")
        self.assertNotIn("下次计划", page.cards[manual.id].message_label.fullText())
        self.assertIn("下次计划 12:00", page.cards[automatic.id].message_label.fullText())
        page.set_runtime({}, next_runs, True, "stopped", "", [])
        self.assertEqual(page.schedule_stat.value_label.fullText(), "已暂停")
        self.assertEqual(page.schedule_stat.suffix_label.text(), "自动计划")
        for card in page.cards.values():
            self.assertNotIn("下次计划", card.message_label.fullText())
        page.set_runtime({}, {manual.id: "09:00", disabled.id: "08:00"}, False, "stopped", "", [])
        self.assertEqual(page.schedule_stat.value_label.fullText(), "未计划")
        self.assertFalse(config.path.exists())

    def test_page_forwards_upload_and_download_intents_without_legacy_or_preview_requests(self):
        for direction in ("upload", "download"):
            with self.subTest(direction=direction):
                config = Config(self.base / "config.json")
                task = replace(self.task, direction=direction)
                config.upsert_task(task)
                page = self.keep(MirrorPage(config))
                requested = []
                for name in ("upload", "download", "sync", "preview", "return", "adopt"):
                    getattr(page, name + "_requested").connect(
                        lambda task_id, intent=name: requested.append((intent, task_id))
                    )
                card = page.cards[task.id]
                card.upload_button.click()
                card.download_button.click()

                self.assertEqual(requested, [("upload", task.id), ("download", task.id)])
                self.assertEqual(card.property("state"), "idle")
                self.assertEqual(config.get_task(task.id), task)
                self.assertFalse(config.path.exists())

                card.sync_requested.emit(task.id)
                self.assertEqual(requested[-1], ("sync", task.id), "Keep the legacy forwarding API")

    def test_page_forwards_only_the_explicit_relay_action_for_each_direction(self):
        for direction, menu_text, expected in (
            ("download", "交回本地修改…", "return"),
            ("upload", "接收手机更新…", "adopt"),
        ):
            with self.subTest(direction=direction):
                config = Config(self.base / "config.json")
                config.upsert_task(replace(self.task, direction=direction))
                page = self.keep(MirrorPage(config))
                requested = []
                page.return_requested.connect(lambda task_id: requested.append(("return", task_id)))
                page.adopt_requested.connect(lambda task_id: requested.append(("adopt", task_id)))
                page.sync_requested.connect(lambda task_id: requested.append(("sync", task_id)))
                card = page.cards[self.task.id]
                actions = [action for action in card.more_menu.actions() if action.text() == menu_text]

                self.assertEqual(len(actions), 1)
                self.assertTrue(actions[0].isEnabled())
                actions[0].trigger()

                self.assertEqual(requested, [(expected, self.task.id)])
                self.assertEqual(card.task.direction, direction)
                self.assertFalse(config.path.exists())

    def test_disabled_tasks_cannot_request_relay_but_paused_plans_allow_enabled_manual_operations(self):
        for direction in ("upload", "download"):
            with self.subTest(direction=direction):
                disabled = self.keep(TaskCard(replace(self.task, direction=direction, enabled=False)))
                requested = []
                disabled.return_requested.connect(requested.append)
                disabled.adopt_requested.connect(requested.append)
                disabled.set_runtime({}, "12:00", True)

                self.assertFalse(disabled.relay_action.isEnabled())
                self.assertIn("启用任务", disabled.relay_action.toolTip())
                disabled.relay_action.trigger()
                self.assertEqual(requested, [])

                enabled = self.keep(TaskCard(replace(self.task, direction=direction)))
                enabled.set_runtime({}, "12:00", True)
                enabled.upload_requested.connect(lambda task_id: requested.append(("upload", task_id)))
                enabled.download_requested.connect(lambda task_id: requested.append(("download", task_id)))
                enabled.preview_requested.connect(lambda task_id: requested.append(("diff", task_id)))
                enabled.return_requested.connect(lambda task_id: requested.append(("return", task_id)))
                enabled.adopt_requested.connect(lambda task_id: requested.append(("adopt", task_id)))
                enabled.sync_button.click()
                enabled.preview_button.click()
                enabled.relay_action.trigger()

                relay = "adopt" if direction == "upload" else "return"
                self.assertEqual(requested, [(direction, self.task.id), ("diff", self.task.id), (relay, self.task.id)])
                self.assertNotIn("下次计划", enabled.message_label.fullText())

    def test_page_busy_gate_keeps_only_current_cancel_available_until_cancellation_finishes(self):
        config = Config(self.base / "config.json")
        other = new_task(str(self.base / "other"), "Other")
        config.replace_tasks((self.task, other))
        page = self.keep(MirrorPage(config))
        active = page.cards[self.task.id]
        idle = page.cards[other.id]
        requested = []
        page.cancel_requested.connect(requested.append)
        page.set_interaction_busy(True)
        page.set_runtime({self.task.id: {"state": "running", "operation": "publish"}}, {}, False, "stopped", "", [])

        for card in (active, idle):
            for control in (
                card.upload_button, card.download_button, card.preview_button, card.edit_button, card.toggle_button,
                card.history_button, card.more_button, card.edit_action, card.remove_action,
                card.relay_action, card.history_action,
            ):
                self.assertFalse(control.isEnabled())
        for control in (page.add_button, page.empty_add_button, page.batch_button, page.pause_button):
            self.assertFalse(control.isEnabled())
        self.assertFalse(active.cancel_button.isHidden())
        self.assertTrue(active.cancel_button.isEnabled())
        self.assertTrue(idle.cancel_button.isHidden())
        active.cancel_button.click()
        self.assertEqual(requested, [self.task.id])
        self.assertEqual(active.property("state"), "running", "A cancel request must not invent runtime state")

        page.set_runtime({self.task.id: {"state": "cancelling"}}, {}, False, "stopped", "", [])
        self.assertEqual(active.badge.text(), "正在取消")
        self.assertFalse(active.cancel_button.isHidden())
        self.assertFalse(active.cancel_button.isEnabled())
        active.cancel_button.click()
        self.assertEqual(requested, [self.task.id])

        page.set_runtime({self.task.id: {"state": "cancelled"}}, {}, False, "stopped", "", [])
        page.set_interaction_busy(False)
        self.assertEqual(active.badge.text(), "已取消")
        self.assertTrue(active.cancel_button.isHidden())
        self.assertTrue(active.progress.isHidden())
        self.assertTrue(active.sync_button.isEnabled())
        self.assertTrue(idle.edit_button.isEnabled())
        self.assertTrue(idle.toggle_button.isEnabled())
        self.assertTrue(page.add_button.isEnabled())
        self.assertIs(page.cards[self.task.id], active)
        self.assertIs(page.cards[other.id], idle)
        self.assertFalse(config.path.exists())

    def test_runtime_names_the_operation_and_shows_the_actual_active_relay_direction(self):
        cases = (
            ("upload", "publish", "正在上传", "本地 → 手机", str(self.base / "work"), "/backup/work/mirror", "历史 · 手机 /backup/work/versions"),
            ("download", "receive", "正在拉取", "手机 → 本地", "/backup/work/mirror", str(self.base / "work"), f"历史 · 本地 {self.base / 'local-history'}"),
            ("download", "return", "正在交回", "本地 → 手机", str(self.base / "work"), "/backup/work/mirror", "历史 · 手机 /backup/work/versions"),
            ("upload", "adopt", "正在接收手机更新", "手机 → 本地", "/backup/work/mirror", str(self.base / "work"), f"历史 · 本地 {self.base / 'local-history'}"),
        )
        for direction, operation, status, arrow, source, target, history in cases:
            with self.subTest(operation=operation):
                task = replace(
                    self.task, direction=direction, remote_path="backup/work/mirror",
                    versions_path="backup/work/versions", local_history_path=str(self.base / "local-history"),
                )
                card = self.keep(TaskCard(task))
                card.set_runtime({"state": "running", "operation": operation}, "12:00", False)

                self.assertEqual(card.badge.text(), status)
                self.assertIn(arrow, card.direction_label.text())
                self.assertEqual(card.source_label.fullText(), source)
                self.assertEqual(card.target_label.fullText(), target)
                self.assertEqual(card.history_label.fullText(), history)
                self.assertEqual(card.sync_button.text(), "上传到手机" if direction == "upload" else "下载到本地")
                self.assertEqual(card.task.direction, direction)
                self.assertTrue(card.cancel_button.isEnabled())
                self.assertNotIn("下次计划", card.message_label.fullText())

    def test_guarded_download_keeps_local_changes_reason_instead_of_a_success_summary(self):
        card = self.keep(TaskCard(replace(self.task, direction="download")))
        reason = "本地有未交回的新增、修改或删除，已停止拉取以避免覆盖；请先查看差异并交回或另存处理。"
        runtime = {
            "state": "guarded", "operation": "receive", "blocked": True,
            "reason": reason, "message": "差异核对完成 · 新增 1 · 更新 1",
        }
        card.set_runtime(runtime, "", False)

        self.assertEqual(card.message_label.fullText(), reason)
        self.assertEqual(card.message_label.toolTip(), reason)
        self.assertNotIn("完成", card.badge.text())
        self.assertTrue(card.cancel_button.isHidden())
        self.assertTrue(card.progress.isHidden())
        self.assertTrue(card.preview_button.isEnabled())
        self.assertTrue(card.relay_action.isEnabled())
        self.assertEqual(runtime["state"], "guarded")
        self.assertEqual(runtime["message"], "差异核对完成 · 新增 1 · 更新 1")

    def test_workspace_pause_is_labeled_as_an_automatic_plan_not_a_transfer_cancel(self):
        config = Config(self.base / "config.json")
        config.upsert_task(self.task)
        page = self.keep(MirrorPage(config))
        requests = []
        page.pause_requested.connect(lambda: requests.append("pause-plan"))
        page.cancel_requested.connect(lambda task_id: requests.append(("cancel", task_id)))
        page.set_runtime({}, {self.task.id: "12:00"}, False, "stopped", "", [])

        self.assertEqual(page.pause_button.text(), "暂停自动计划")
        page.pause_button.click()
        self.assertEqual(requests, ["pause-plan"])
        page.set_runtime({}, {self.task.id: "12:00"}, True, "stopped", "", [])
        self.assertEqual(page.pause_button.text(), "继续自动计划")
        self.assertTrue(page.cards[self.task.id].sync_button.isEnabled())
        self.assertTrue(page.cards[self.task.id].preview_button.isEnabled())
        self.assertTrue(page.cards[self.task.id].cancel_button.isHidden())
        self.assertTrue(config.get_task(self.task.id).enabled)

    def test_progress_uses_only_valid_reported_percentages_and_updates_existing_cards(self):
        config = Config(self.base / "config.json")
        config.upsert_task(self.task)
        page = self.keep(MirrorPage(config))
        card = page.cards[self.task.id]
        transfer = "正在传输 · 37% · 3.7 / 10.0 MiB"
        page.set_runtime({self.task.id: {"state": "running", "message": transfer}}, {}, False, "stopped", "", [])

        self.assertIs(page.cards[self.task.id], card)
        self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
        self.assertEqual(card.message_label.fullText(), transfer)
        page.set_runtime({self.task.id: {"state": "running", "message": transfer, "progress": 37}}, {}, False, "stopped", "", [])
        self.assertEqual((card.progress.minimum(), card.progress.maximum(), card.progress.value()), (0, 100, 37))
        for invalid in ("not-a-percentage", float("nan"), float("inf"), True):
            with self.subTest(progress=invalid):
                page.set_runtime({self.task.id: {"state": "running", "progress": invalid}}, {}, False, "stopped", "", [])
                self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
                self.assertIs(page.cards[self.task.id], card)

    def test_progress_explains_phase_scope_and_unknown_totals_accessibly_without_changing_layout(self):
        card = self.keep(TaskCard(self.task))
        message = "正在准备已确认的修改… · 本阶段 37% · 3.7 / 10.0 MiB"
        card.set_runtime({"state": "running", "message": message, "progress": 37}, "", False)

        self.assertIn("本阶段", card.progress.accessibleName())
        self.assertIn("不是整体", card.progress.toolTip())
        self.assertIn(message, card.progress.accessibleDescription())
        self.assertLessEqual(card.progress.height(), 6)
        self.assertEqual(card.message_label.toolTip(), message)
        self.assertNotIn("完成", card.badge.text())
        card.set_runtime({"state": "running", "message": "正在核对目标内容…"}, "", False)
        self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
        self.assertIn("无可用总量", card.progress.accessibleDescription())
        self.assertIn("已用时", card.progress.toolTip())
        self.assertNotIn(message, card.progress.accessibleDescription())

    def test_runtime_external_busy_inhibits_all_context_actions_but_not_an_active_cancel(self):
        card = self.keep(TaskCard(self.task))
        requested = []
        card.sync_requested.connect(lambda task_id: requested.append(("sync", task_id)))
        card.toggle_requested.connect(lambda task_id: requested.append(("toggle", task_id)))
        card.edit_requested.connect(lambda task_id: requested.append(("edit", task_id)))
        card.remove_requested.connect(lambda task_id: requested.append(("remove", task_id)))
        card.cancel_requested.connect(lambda task_id: requested.append(("cancel", task_id)))
        card.set_runtime({"state": "preview", "external_busy": True}, "", False)

        for control in (
            card.sync_button, card.preview_button, card.edit_button, card.toggle_button,
            card.more_button, card.history_button, card.edit_action, card.remove_action,
            card.relay_action, card.history_action,
        ):
            self.assertFalse(control.isEnabled())
        self.assertFalse(card.cancel_button.isHidden())
        self.assertTrue(card.cancel_button.isEnabled())
        card.sync_button.click()
        card.toggle_button.click()
        card.edit_action.trigger()
        card.remove_action.trigger()
        card.cancel_button.click()
        self.assertEqual(requested, [("cancel", self.task.id)])
        card.set_interaction_busy(True)
        card.set_runtime({"state": "idle", "external_busy": False}, "", False)
        self.assertFalse(card.sync_button.isEnabled())
        card.set_interaction_busy(False)
        self.assertTrue(card.sync_button.isEnabled())
        self.assertTrue(card.edit_action.isEnabled())
        self.assertTrue(card.remove_action.isEnabled())
        self.assertTrue(card.cancel_button.isHidden())

    def test_queued_cards_offer_no_cancel_or_queue_removal_and_cancelling_counts_as_active(self):
        config = Config(self.base / "config.json")
        other = new_task(str(self.base / "other"), "Other")
        config.replace_tasks((self.task, other))
        page = self.keep(MirrorPage(config))
        page.set_runtime({
            self.task.id: {"state": "cancelling"},
            other.id: {"state": "queued"},
        }, {}, False, "stopped", "", [])
        queued = page.cards[other.id]

        self.assertEqual(page.schedule_stat.value_label.fullText(), "执行中")
        self.assertEqual(page.schedule_stat.detail_label.text(), "1 个正在执行 · 1 个等待")
        self.assertEqual(queued.badge.text(), "等待执行")
        self.assertTrue(queued.cancel_button.isHidden())
        self.assertFalse(queued.cancel_button.isEnabled())
        self.assertFalse(queued.sync_button.isEnabled())
        self.assertFalse(queued.toggle_button.isEnabled())
        self.assertFalse(queued.remove_action.isEnabled())
        self.assertTrue(queued.progress.isHidden())

    def test_recovery_is_an_explicit_forwarded_request_subject_to_task_and_busy_gates(self):
        for direction in ("upload", "download"):
            with self.subTest(direction=direction):
                config = Config(self.base / "config.json")
                config.upsert_task(replace(self.task, direction=direction))
                page = self.keep(MirrorPage(config))
                requested = []
                page.recover_requested.connect(requested.append)
                card = page.cards[self.task.id]
                action = next(action for action in card.more_menu.actions() if action.text() == "继续未完成操作…")
                self.assertIs(action, card.recover_action)
                self.assertEqual(action.toolTip(), "仅由原电脑核对后继续，不抢占其他电脑的锁")
                action.trigger()
                self.assertEqual(requested, [self.task.id])
                self.assertEqual(card.property("state"), "idle")
                page.set_interaction_busy(True)
                self.assertFalse(action.isEnabled())
                action.trigger()
                self.assertEqual(requested, [self.task.id])
                page.set_interaction_busy(False)
                self.assertTrue(action.isEnabled())
                disabled = self.keep(TaskCard(replace(self.task, direction=direction, enabled=False)))
                self.assertFalse(disabled.recover_action.isEnabled())
                self.assertIn("启用任务", disabled.recover_action.toolTip())
                card.set_runtime({"state": "running", "operation": "recover"}, "", False)
                self.assertEqual(card.badge.text(), "正在继续未完成操作")
                self.assertFalse(action.isEnabled())
                self.assertFalse(config.path.exists())

    def test_terminal_relay_result_restores_the_default_direction_for_the_next_manual_action(self):
        task = replace(self.task, direction="download", last_sync_at="2026-11-14 12:00:00")
        card = self.keep(TaskCard(task))
        card.set_runtime({"state": "running", "operation": "return"}, "", False)
        card.set_runtime({"state": "idle", "operation": "return", "message": "交回完成"}, "12:30", False)

        self.assertEqual(card.direction_label.text(), "手机 → 本地")
        self.assertEqual(card.source_label.fullText(), "/" + task.remote_path)
        self.assertEqual(card.target_label.fullText(), task.local_path)
        self.assertEqual(card.history_label.fullText(), "历史 · 本地 " + task.local_history_path)
        self.assertEqual(card.sync_button.text(), "下载到本地")
        self.assertTrue(card.sync_button.isEnabled())
        self.assertIn("交回完成", card.message_label.fullText())
        self.assertIn("下次计划 12:30", card.message_label.fullText())
        self.assertTrue(card.cancel_button.isHidden())
        self.assertTrue(card.progress.isHidden())
        self.assertEqual(task.last_sync_at, "2026-11-14 12:00:00")

    def test_recovery_endpoints_follow_the_effective_operation_or_task_default_without_guessing(self):
        for direction, effective, expected_source, expected_target, history_prefix in (
            ("download", None, "/backup/work/mirror", str(self.base / "work"), "历史 · 本地 "),
            ("upload", "adopt", "/backup/work/mirror", str(self.base / "work"), "历史 · 本地 "),
            ("download", "return", str(self.base / "work"), "/backup/work/mirror", "历史 · 手机 "),
        ):
            with self.subTest(direction=direction, effective=effective):
                task = replace(self.task, direction=direction, remote_path="backup/work/mirror")
                card = self.keep(TaskCard(task))
                runtime = {"state": "preview", "operation": "recover"}
                if effective is not None:
                    runtime["effective_operation"] = effective
                card.set_runtime(runtime, "", False)

                self.assertEqual(card.source_label.fullText(), expected_source)
                self.assertEqual(card.target_label.fullText(), expected_target)
                self.assertTrue(card.history_label.fullText().startswith(history_prefix))
                self.assertTrue(card.cancel_button.isEnabled())
                self.assertFalse(card.recover_action.isEnabled())


if __name__ == "__main__":
    unittest.main()
