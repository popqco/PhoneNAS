from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


_TEST_ROOT: Path

from PySide6.QtCore import QCoreApplication, QEvent, QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel, QLineEdit, QRadioButton, QToolButton, QVBoxLayout

from app.core.tasks import MirrorTask
from app.ui.pages.status import StatusPage
from app.ui.widgets import ElidedLabel, apply_theme, button, icon


def setUpModule():
    global _TEST_ROOT
    home = tempfile.TemporaryDirectory(prefix="phonenas-ui-layout-")
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


class UiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def keep(self, widget):
        self.addCleanup(self.dispose, widget)
        return widget

    def dispose(self, widget):
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_elided_label_size_hints_reserve_contents_margins(self):
        title = self.keep(ElidedLabel("A folder title"))
        title.ensurePolished()
        preferred = title.sizeHint()
        minimum = title.minimumSizeHint()

        title.setContentsMargins(7, 9, 11, 13)

        self.assertEqual(title.sizeHint(), preferred + QSize(18, 22))
        self.assertEqual(title.minimumSizeHint(), minimum + QSize(18, 22))
        title.resize(title.minimumSizeHint())
        self.assertGreaterEqual(title.contentsRect().height(), title.fontMetrics().height())

    def test_elided_label_reelides_after_font_change_without_resize(self):
        text = "A long folder title for photographs and documents"
        title = self.keep(ElidedLabel(text))
        title.setContentsMargins(7, 0, 11, 0)
        title.resize(240, 80)
        title.show()
        self.app.processEvents()
        before = title.text()
        size = title.size()
        font = QFont(title.font())
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 6)
        title.setFont(font)
        self.app.processEvents()

        self.assertEqual(title.size(), size)
        self.assertNotEqual(title.text(), before)
        self.assertLessEqual(title.fontMetrics().horizontalAdvance(title.text()), title.contentsRect().width())
        self.assertEqual(title.fullText(), text)
        self.assertEqual(title.toolTip(), text)

    def test_elided_label_reelides_after_style_change_without_resize(self):
        text = "A long folder title for photographs and documents"
        title = self.keep(ElidedLabel(text))
        title.resize(240, 80)
        title.show()
        self.app.processEvents()
        before = title.text()
        size = title.size()

        title.setMargin(45)
        QCoreApplication.sendEvent(title, QEvent(QEvent.Type.StyleChange))
        self.app.processEvents()

        self.assertEqual(title.size(), size)
        self.assertEqual(title.margin(), 45)
        self.assertNotEqual(title.text(), before)
        self.assertLessEqual(
            title.fontMetrics().horizontalAdvance(title.text()),
            title.contentsRect().width() - 2 * title.margin(),
        )
        self.assertEqual(title.fullText(), text)
        self.assertEqual(title.toolTip(), text)

    def test_line_edit_embedded_icons_stay_centered_and_clickable(self):
        edit = self.keep(QLineEdit())
        edit.resize(320, 42)
        edit.setClearButtonEnabled(True)
        search = edit.addAction(icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        searches = []
        search.triggered.connect(lambda: searches.append(edit.text()))
        edit.setText("photographs")
        edit.show()
        self.app.processEvents()
        tools = [tool for tool in edit.findChildren(QToolButton) if tool.isVisible()]
        self.assertEqual(len(tools), 2)
        leading, trailing = sorted(tools, key=lambda tool: tool.x())

        for tool in tools:
            with self.subTest(position=tool.x()):
                self.assertLessEqual(abs(tool.contentsRect().center().y() - tool.rect().center().y()), 1)
                self.assertLessEqual(abs(tool.geometry().center().y() - edit.rect().center().y()), 1)
                self.assertGreaterEqual(tool.contentsRect().height(), tool.iconSize().height())
                self.assertGreaterEqual(tool.contentsRect().width(), tool.iconSize().width())
                self.assertTrue(edit.rect().contains(tool.geometry()))

        QTest.mouseClick(leading, Qt.MouseButton.LeftButton)
        self.assertEqual(searches, ["photographs"])
        QTest.mouseClick(trailing, Qt.MouseButton.LeftButton)
        self.assertEqual(edit.text(), "")

    def test_shared_buttons_allow_tab_space_without_implicit_dialog_default(self):
        host = self.keep(QDialog())
        layout = QVBoxLayout(host)
        first = QLineEdit()
        action = button("", "icon", "sync")
        action.setToolTip("同步全部")
        last = QLineEdit()
        for widget in (first, action, last):
            layout.addWidget(widget)
        activated = []
        action.clicked.connect(lambda: activated.append(True))
        host.show()
        host.activateWindow()
        first.setFocus()
        self.app.processEvents()

        QTest.keyClick(first, Qt.Key.Key_Return)
        self.assertEqual(activated, [], "An icon action must not become a dialog's implicit default")
        QTest.keyClick(first, Qt.Key.Key_Tab)
        self.assertTrue(action.hasFocus())
        QTest.keyClick(action, Qt.Key.Key_Space)
        self.assertEqual(activated, [True])
        action.setEnabled(False)
        first.setFocus()
        QTest.keyClick(first, Qt.Key.Key_Tab)
        self.assertTrue(last.hasFocus())

    def test_tool_button_shows_keyboard_focus_without_geometry_shift(self):
        host = self.keep(QDialog())
        layout = QVBoxLayout(host)
        field = QLineEdit()
        action = QToolButton()
        action.setText("展开选项")
        layout.addWidget(field)
        layout.addWidget(action)
        host.show()
        host.activateWindow()
        field.setFocus()
        self.app.processEvents()
        unfocused = action.grab().toImage()
        geometry = action.geometry()

        QTest.keyClick(field, Qt.Key.Key_Tab)
        self.app.processEvents()

        self.assertTrue(action.hasFocus())
        self.assertEqual(action.geometry(), geometry)
        self.assertNotEqual(action.grab().toImage(), unfocused, "Keyboard focus must be visible")

    def test_check_and_radio_controls_show_keyboard_focus_on_the_indicator(self):
        for control_type in (QCheckBox, QRadioButton):
            with self.subTest(control=control_type.__name__):
                host = self.keep(QDialog())
                layout = QVBoxLayout(host)
                field = QLineEdit()
                control = control_type("启用计划")
                layout.addWidget(field)
                layout.addWidget(control)
                host.show()
                host.activateWindow()
                field.setFocus()
                self.app.processEvents()
                unfocused = control.grab().toImage()
                geometry = control.geometry()

                QTest.keyClick(field, Qt.Key.Key_Tab)
                self.app.processEvents()

                self.assertTrue(control.hasFocus())
                self.assertEqual(control.geometry(), geometry)
                focused = control.grab().toImage()
                changed = any(
                    focused.pixel(x, y) != unfocused.pixel(x, y)
                    for x in range(min(20, focused.width()))
                    for y in range(focused.height())
                )
                self.assertTrue(changed, "The styled indicator must show keyboard focus")
                QTest.keyClick(control, Qt.Key.Key_Space)
                self.assertTrue(control.isChecked())
                host.hide()

    def test_status_manual_only_has_no_fake_next_auto_and_mixed_summary_keeps_manual_actions(self):
        page = self.keep(StatusPage())
        manual = MirrorTask(
            id="manual", name="Manual", enabled=True,
            local_path=str(_TEST_ROOT / "manual"),
            remote_path="backup/manual/mirror", versions_path="backup/manual/versions",
            interval_minutes=20, retention_days=30, exclude_preset="none", excludes=(), auto_sync=False,
        )
        page.set_sync("09:40", "12:00", False)
        page.set_summary((manual,), {})
        self.assertEqual(page.plan_stat.value_label.fullText(), "仅手动")
        self.assertIn("仅手动", page.next_sync_label.text())
        self.assertNotIn("12:00", page.next_sync_label.text())
        self.assertTrue(page.sync_button.isEnabled())
        self.assertFalse(page.pause_button.isEnabled())
        page.set_sync("09:40", "13:00", True)
        self.assertEqual(page.plan_stat.value_label.fullText(), "仅手动")
        self.assertNotIn("已暂停", page.state_label.text())
        rows = [item.text() for item in page.task_list.findChildren(QLabel)]
        self.assertTrue(any("仅手动" in text for text in rows))
        self.assertFalse(any("每 20 分钟" in text for text in rows))
        automatic = replace(manual, id="auto", name="Automatic", auto_sync=True)
        page.set_summary((manual, automatic), {})
        page.set_sync("09:40", "14:00", False)
        self.assertIn("14:00", page.next_sync_label.text())
        self.assertIn("1 个自动", page.task_stat.detail_label.text())
        self.assertIn("1 个仅手动", page.task_stat.detail_label.text())
        self.assertTrue(page.pause_button.isEnabled())
        self.assertTrue(page.sync_button.isEnabled())
        page.set_summary((manual, replace(automatic, enabled=False)), {})
        self.assertEqual(page.plan_stat.value_label.fullText(), "仅手动")
        self.assertNotIn("14:00", page.next_sync_label.text())

    def test_status_sync_all_requires_an_enabled_task(self):
        page = self.keep(StatusPage())
        paused = MirrorTask(
            id="photos", name="Photos", enabled=False,
            local_path=str(_TEST_ROOT / "source"),
            remote_path="backup/photos/mirror", versions_path="backup/photos/versions",
            interval_minutes=20, retention_days=30, exclude_preset="custom", excludes=(),
        )
        requested = []
        page.sync_requested.connect(lambda: requested.append(True))

        self.assertFalse(page.sync_button.isEnabled())
        page.set_summary((paused,), {}, 0)
        self.assertFalse(page.sync_button.isEnabled())
        self.assertEqual(page.enabled_label.text(), "0")
        page.sync_button.click()
        self.assertEqual(requested, [])
        page.set_summary((replace(paused, enabled=True),), {}, 0)
        self.assertTrue(page.sync_button.isEnabled())
        self.assertEqual(page.enabled_label.text(), "1")
        page.sync_button.click()
        self.assertEqual(requested, [True])
        page.set_summary((), {}, 0)
        self.assertFalse(page.sync_button.isEnabled())

    def test_status_summary_accepts_public_sync_availability_without_changing_counts(self):
        page = self.keep(StatusPage())
        task = MirrorTask(
            id="photos", name="Photos", enabled=True,
            local_path=str(_TEST_ROOT / "source"),
            remote_path="backup/photos/mirror", versions_path="backup/photos/versions",
            interval_minutes=20, retention_days=30, exclude_preset="custom", excludes=(),
        )
        page.set_summary((task,), {}, 0, sync_available=False)
        self.assertFalse(page.sync_button.isEnabled())
        self.assertEqual(page.enabled_label.text(), "1")
        self.assertIn("当前不可用", page.state_label.text())
        page.set_summary((task,), {}, 0, sync_available=True)
        self.assertTrue(page.sync_button.isEnabled())
        self.assertNotIn("当前不可用", page.state_label.text())
        page.set_summary((replace(task, enabled=False),), {}, 0, sync_available=True)
        self.assertFalse(page.sync_button.isEnabled())
        self.assertIn("没有已启用", page.state_label.text())

    def test_status_execution_state_tracks_runtime_message_and_returns_to_ready(self):
        page = self.keep(StatusPage())
        task = MirrorTask(
            id="photos", name="Photos", enabled=True,
            local_path=str(_TEST_ROOT / "source"),
            remote_path="backup/photos/mirror", versions_path="backup/photos/versions",
            interval_minutes=20, retention_days=30, exclude_preset="custom", excludes=(),
        )
        page.set_summary((task,), {task.id: {"state": "running", "message": "正在扫描差异"}})
        self.assertIn("同步中", page.state_label.text())
        self.assertIn("正在扫描差异", page.state_label.text())
        self.assertIn("1 个任务正在执行", page.queue_stat.detail_label.text())
        page.set_summary((task,), {task.id: {"state": "running", "message": "正在复制照片"}})
        self.assertIn("正在复制照片", page.state_label.text())
        self.assertNotIn("正在扫描差异", page.state_label.text())
        page.set_summary((task,), {task.id: {"state": "preview", "message": "只检查差异"}})
        self.assertIn("核对差异中", page.state_label.text())
        self.assertIn("只检查差异", page.state_label.text())
        page.set_summary((task,), {task.id: {"state": "cancelling", "message": "等待传输退出"}})
        self.assertIn("正在取消", page.state_label.text())
        page.set_summary((task,), {task.id: {"state": "cancelled", "message": "没有清理历史"}})
        self.assertIn("已取消", page.state_label.text())
        page.set_summary((task,), {})
        self.assertIn("就绪", page.state_label.text())
        self.assertNotIn("添加文件夹", page.state_label.text())
        self.assertIn("当前没有任务正在执行", page.queue_stat.detail_label.text())

    def test_status_execution_summary_exposes_failures_and_protection_reasons(self):
        page = self.keep(StatusPage())
        task = MirrorTask(
            id="photos", name="Photos", enabled=True,
            local_path=str(_TEST_ROOT / "source"),
            remote_path="backup/photos/mirror", versions_path="backup/photos/versions",
            interval_minutes=20, retention_days=30, exclude_preset="custom", excludes=(),
        )
        page.set_summary((task,), {task.id: {"state": "error", "message": "无法读取来源"}})
        self.assertIn("失败", page.state_label.text())
        self.assertIn("无法读取来源", page.state_label.text())
        page.set_summary((task,), {task.id: {"state": "guarded", "message": "删除数量超出保护阈值"}})
        self.assertIn("保护阻止", page.state_label.text())
        self.assertIn("删除数量超出保护阈值", page.state_label.text())
        page.set_summary((task,), {task.id: {"state": "idle", "message": "同步完成"}})
        self.assertNotIn("失败", page.state_label.text())
        self.assertNotIn("保护阻止", page.state_label.text())

    def test_status_paused_schedule_keeps_manual_sync_available_and_counts_queued_work(self):
        page = self.keep(StatusPage())
        task = MirrorTask(
            id="photos", name="Photos", enabled=True,
            local_path=str(_TEST_ROOT / "source"),
            remote_path="backup/photos/mirror", versions_path="backup/photos/versions",
            interval_minutes=20, retention_days=30, exclude_preset="custom", excludes=(),
        )
        page.set_summary((task,), {}, 2)
        self.assertIn("2", page.state_label.text())
        self.assertIn("等待", page.state_label.text())
        page.set_sync("09:40", "12:00", True)
        self.assertIn("已暂停", page.state_label.text())
        self.assertEqual(page.pause_button.text(), "继续自动计划")
        self.assertIn("已暂停", page.next_sync_label.text())
        self.assertTrue(page.sync_button.isEnabled())
        page.set_summary((task,), {}, 1)
        self.assertIn("已暂停", page.state_label.text())
        self.assertIn("1", page.state_label.text())
        self.assertEqual(page.queue_stat.value_label.fullText(), "1")
        page.set_sync("09:40", "12:00", False)
        self.assertNotIn("已暂停", page.state_label.text())
        self.assertIn("等待", page.state_label.text())
        self.assertIn("12:00", page.next_sync_label.text())
        page.set_summary((task,), {}, 0)
        self.assertIn("就绪", page.state_label.text())


if __name__ == "__main__":
    unittest.main()
