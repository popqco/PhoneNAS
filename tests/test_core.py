from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app import constants as C
from app.core.config import Config
from app.core.retention import old_versions, parse_version_name
from app.core.sync import build_sync_args


class ConfigTests(unittest.TestCase):
    def test_defaults_and_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config(path)
            self.assertEqual(config.url, C.DEFAULT_URL)
            config.set("mirror.local_path", r"D:\TestFolder")
            config.password = "secret"
            config.save()

            loaded = Config(path)
            self.assertEqual(loaded.local_path, Path(r"D:\TestFolder"))
            self.assertEqual(loaded.password, "secret")

    def test_export_strips_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            export = Path(tmp) / "export.json"
            config = Config(path)
            config.password = "secret"
            config.export_to(export)
            text = export.read_text(encoding="utf-8")
            payload = json.loads(text)
            self.assertEqual(payload["nas"]["password_enc"], "")
            self.assertNotIn("secret", text)

    def test_import_keeps_existing_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            imported = Path(tmp) / "incoming.json"
            config = Config(path)
            config.password = "secret"
            config.save()
            imported.write_text('{"nas": {"url": "http://10.0.0.2:8888"}}', encoding="utf-8")
            config.import_from(imported)
            self.assertEqual(config.url, "http://10.0.0.2:8888")
            self.assertEqual(config.password, "secret")


class RetentionTests(unittest.TestCase):
    def test_parse_version_name(self) -> None:
        self.assertEqual(
            parse_version_name("2026-11-14_153000"),
            datetime(2026, 11, 14, 15, 30, 0),
        )
        self.assertIsNone(parse_version_name("not-a-version"))

    def test_old_versions(self) -> None:
        names = ["2026-10-01_120000", "2026-11-13_120000", "2026-11-14_120000", "junk"]
        result = old_versions(names, 30, datetime(2026, 11, 14, 12, 0, 0))
        self.assertEqual(result, ["2026-10-01_120000"])

    def test_retention_boundary(self) -> None:
        names = ["2026-11-01_120000"]
        now = datetime(2026, 11, 14, 12, 0, 0)
        # exactly at the cutoff is kept, strictly older is removed
        self.assertEqual(old_versions(names, 13, now), [])
        self.assertEqual(old_versions(names, 12, now), ["2026-11-01_120000"])


class SyncArgsTests(unittest.TestCase):
    def test_args_contain_backup_dir_and_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp) / "config.json")
            config.set("mirror.local_path", r"D:\Work")
            config.set("mirror.remote_path", "backup/work")
            config.set("mirror.versions_path", "backup/work-versions")
            config.set("mirror.excludes", ["build/**", "*.tmp"])
            args = build_sync_args(config, "2026-11-14_153000")
            self.assertEqual(args[0], "sync")
            self.assertIn("phone:backup/work", args)
            self.assertIn("--backup-dir", args)
            self.assertIn("phone:backup/work-versions/2026-11-14_153000", args)
            self.assertIn("build/**", args)
            self.assertIn("*.tmp", args)


if __name__ == "__main__":
    unittest.main()
