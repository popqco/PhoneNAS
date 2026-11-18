from __future__ import annotations

import unittest

from app.core.progress import SyncProgress


class ProgressModelTests(unittest.TestCase):
    def test_transfer_percentage_is_scoped_to_its_phase_and_clears_for_checks(self):
        transferred = SyncProgress("staging", "下载到暂存区；尚未写入目标", bytes_done=1048576, bytes_total=1048576)
        self.assertEqual(transferred.percent, 100)
        self.assertIn("本阶段 100%", transferred.display())
        self.assertIn("1.0 / 1.0 MiB", transferred.display())
        checking = SyncProgress("recheck_source", "正在复核来源内容")
        self.assertIsNone(checking.percent)
        self.assertNotIn("100%", checking.display(65))
        self.assertIn("已用时 01:05", checking.display(65))

    def test_unknown_invalid_or_nontransfer_counters_never_imply_completion(self):
        for done, total in ((0, 0), (None, 100), (True, 100), (-1, 100), (1, False)):
            with self.subTest(done=done, total=total):
                event = SyncProgress("staging", "正在暂存", bytes_done=done, bytes_total=total)
                self.assertIsNone(event.percent)
                self.assertNotIn("%", event.display())
        event = SyncProgress("verify_source", "正在校验", bytes_done=100, bytes_total=100)
        self.assertIsNone(event.percent)
        self.assertNotIn("100%", event.display())
        self.assertNotIn("已用时", event.display(float("nan")))
        self.assertIn("2 / 3 项", SyncProgress("directories", "核对目录", items_done=2, items_total=3).display())


if __name__ == "__main__":
    unittest.main()
