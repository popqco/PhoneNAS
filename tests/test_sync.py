from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.core.tasks import new_task
from app.core.sync import SyncEngine, parse_sync_report, preview_guard


class PreviewTests(unittest.TestCase):
    def test_preview_classifies_changes_without_counting_them_as_transfers(self):
        report = parse_sync_report("= same.txt\n+ new.txt\n* changed.txt\n- deleted.txt\n")
        self.assertEqual((report.added, report.updated, report.deleted, report.unchanged), (1, 1, 1, 1))
        self.assertEqual(report.source_files, 3)
        self.assertEqual(report.destination_files, 3)
        self.assertIn(("+", "new.txt"), report.changes)

    def test_empty_source_cannot_automatically_delete_previous_mirror(self):
        with tempfile.TemporaryDirectory() as root:
            task = new_task(str(Path(root)))
            report = parse_sync_report("- first.txt\n- second.txt\n")
            self.assertIn("空", preview_guard(task, report))

    def test_directional_guard_names_the_actual_destination(self):
        with tempfile.TemporaryDirectory() as root:
            task = replace(new_task(str(Path(root))), direction="download")
            report = parse_sync_report("- first.txt\n- second.txt\n")
            reason = preview_guard(task, report)
            self.assertIn("本地", reason)
            self.assertNotIn("删除手机", reason)

    def test_hard_content_conflict_cannot_be_approved_as_a_threshold_warning(self):
        from app.core.sync import SyncReport
        task = new_task("D:/sandbox-only")
        report = SyncReport(blocked=True, reason="本地有未交回的修改")
        self.assertEqual(preview_guard(task, report), "本地有未交回的修改")

    def test_many_deletions_require_confirmation_even_with_new_files(self):
        with tempfile.TemporaryDirectory() as root:
            task = new_task(str(Path(root)))
            text = "\n".join([f"- old/{i}.txt" for i in range(60)] + ["+ new.txt"])
            self.assertIn("删除", preview_guard(task, parse_sync_report(text)))


class SourceProtectionTests(unittest.TestCase):
    def test_missing_source_never_touches_the_remote(self):
        class NoRemote:
            def run(self, *args, **kwargs):
                raise AssertionError("missing source must not contact remote")

        with tempfile.TemporaryDirectory() as root:
            task = new_task(str(Path(root) / "missing"))
            with self.assertRaises((ValueError, OSError)):
                SyncEngine(NoRemote(), task).preview()


if __name__ == "__main__":
    unittest.main()
