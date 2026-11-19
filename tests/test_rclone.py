from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import Config
from app.core.rclone import Rclone


class RcloneIsolationTests(unittest.TestCase):
    def test_private_configuration_does_not_replace_shared_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            config = Config(base / "config.json")
            shared = base / "rclone.conf"
            shared.write_text("existing shared credentials", encoding="utf-8")
            client = Rclone(config, conf_path=shared)
            with patch.object(client, "obscure", return_value="obscured"):
                with client.isolated_conf():
                    private = client.conf_path
                    self.assertNotEqual(private, shared)
                    client.write_conf("http://example.invalid:8888", "user", "secret")
                    self.assertIn("user = user", private.read_text(encoding="utf-8"))
                    self.assertEqual(shared.read_text(), "existing shared credentials")
            self.assertFalse(private.exists())
            self.assertEqual(client.conf_path, shared)

    def test_cancel_is_persistent_and_never_starts_another_process(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            client = Rclone(Config(base / "config.json"), conf_path=base / "rclone.conf")
            client.cancel()
            with patch("app.core.rclone.subprocess.Popen", side_effect=AssertionError("must not launch")):
                self.assertFalse(client.run(["version"]).ok)
                with self.assertRaises(RuntimeError):
                    client.popen(["version"])

    def test_file_operations_use_literal_names_and_keep_filter_syntax(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            client = Rclone(Config(base / "config.json"), conf_path=base / "shared.conf")
            executable = base / "rclone.exe"
            executable.touch()
            private = base / "private.conf"
            rule = "/part[\u2421].txt"
            command = ["lsjson", "phone:PCB\u200b", "--exclude", rule]
            with patch("app.core.rclone.rclone_exe", return_value=executable), \
                    patch.dict(os.environ, {"RCLONE_EXCLUDE": "irrelevant", "RCLONE_WEBdav_URL": "invalid"}), \
                    patch("app.core.rclone.subprocess.Popen") as spawn:
                proc = spawn.return_value
                proc.communicate.return_value = ("[]", "")
                proc.returncode = 0
                proc.poll.return_value = 0
                self.assertTrue(client.run(command, conf_path=private).ok)
                args, options = spawn.call_args
                self.assertEqual(args[0][1:1 + len(command)], command)
                self.assertEqual(args[0][-2:], ["--config", str(private)])
                self.assertNotIn("RCLONE_EXCLUDE", options["env"])
                self.assertFalse(any(key.upper() == "RCLONE_WEBDAV_URL" for key in options["env"]))
                client.popen(["mount", "phone:", "W:", "--read-only"], conf_path=private)
                mount = spawn.call_args.args[0]
                self.assertNotIn("--local-encoding", mount)
                self.assertNotIn("--webdav-encoding", mount)
                self.assertEqual(mount[-2:], ["--config", str(private)])

    def test_configuration_line_injection_is_rejected_before_obscuring(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            client = Rclone(Config(base / "config.json"), conf_path=base / "rclone.conf")
            with patch.object(client, "obscure", side_effect=AssertionError("must not launch")):
                with self.assertRaises(ValueError):
                    client.write_conf("http://example.invalid", "user\n[other]", "password")
            self.assertFalse(client.conf_path.exists())


if __name__ == "__main__":
    unittest.main()
