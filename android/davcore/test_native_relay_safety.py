"""Offline safety contracts, NOT Android or real-client relay pass evidence.

Run: python -I -B android/davcore/test_native_relay_safety.py
No server, device, production configuration or rclone process may be contacted.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location(
    "phonenas_native_relay", Path(__file__).with_name("test_native_relay.py")
)
relay = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = relay
_spec.loader.exec_module(relay)


class OfflineSafetyTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.create_connection", "socket.getaddrinfo", "socket.socket.connect",
                       "socket.socket.connect_ex", "subprocess.Popen", "subprocess.run"):
            guard = patch(target, side_effect=AssertionError("OFFLINE_NO_EXTERNAL_IO"))
            boundary = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(boundary.assert_not_called)

    def test_endpoint_is_only_literal_http_loopback_with_a_canonical_explicit_port(self):
        for port in (1, 18888, 65535):
            endpoint = relay.validate_endpoint(f"http://127.0.0.1:{port}")
            self.assertEqual((endpoint.host, endpoint.port), ("127.0.0.1", port))
            self.assertEqual(endpoint.url, f"http://127.0.0.1:{port}")
        for invalid in (
            "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536",
            "http://127.0.0.1:018888", "http://127.0.0.1:+18888",
            "https://127.0.0.1:18888", "http://localhost:18888", "http://[::1]:18888",
            "http://127.1:18888", "http://2130706433:18888", "http://127.0.0.2:18888",
            "http://127.000.000.001:18888", "http://127.0.0.1.example:18888",
            "http://100.127.10.41:18888", "http://192.168.0.1:18888",
            "http://user:SECRET@127.0.0.1:18888", "http://@127.0.0.1:18888",
            "http://127.0.0.1:18888/", "http://127.0.0.1:18888/acceptance_rw",
            "http://127.0.0.1:18888?", "http://127.0.0.1:18888#",
            "HTTP://127.0.0.1:18888", " http://127.0.0.1:18888",
            "http://127.0.0.1:18888\n", "http://127.0.0.1:18888\t",
            "http://127.0.0.1:18888\\x", "http://127.0.0.1:18888\x00", None,
        ):
            with self.subTest(endpoint_gate=True), self.assertRaises(relay.SafetyError):
                relay.validate_endpoint(invalid)

    def test_private_json_accepts_only_same_uuid_public_rw_ro_and_no_extra_settings(self):
        identity = "12345678-1234-4234-8234-123456789abc"
        private = {"username": "acceptance", "password": "OFFLINE-ONLY-PRIVATE-PASSWORD",
                   "scope": "isolated-public-acceptance",
                   "root": f"/storage/emulated/0/PhoneNAS-Acceptance-{identity}/rw",
                   "readonly_root": f"/storage/emulated/0/PhoneNAS-Acceptance-{identity}/ro"}
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-relay-safety-") as directory:
            path = Path(directory) / "private.json"
            def load(value):
                path.write_text(json.dumps(value), encoding="utf-8")
                return relay.load_credentials(path)
            credentials = load(private)
            self.assertEqual(credentials.scope, "isolated-public-acceptance")
            self.assertNotIn(private["password"], repr(credentials))
            self.assertNotIn("/storage/", repr(credentials))
            for value in (
                {**private, "scope": "production"}, {**private, "scope": "isolated-app-demo"},
                {**private, "root": "/storage/emulated/0/DCIM"},
                {**private, "root": private["root"] + "/../rw"},
                {**private, "readonly_root": private["readonly_root"].replace(identity, "abcdef" * 5)},
                {**private, "root": private["root"].replace(identity, "0" * 32)},
                {**private, "config": "production.json"}, {**private, "url": "http://production"},
                {**private, "username": "user:password"}, {**private, "password": "short"},
                {**private, "password": private["password"] + "\n"}, [], None,
            ):
                with self.subTest(scope_gate=True), self.assertRaises(relay.SafetyError):
                    load(value)
            path.write_text('{"username":"a","username":"b","password":"SECRET"}', encoding="utf-8")
            with self.assertRaises(relay.SafetyError) as failure:
                relay.load_credentials(path)
            self.assertNotIn("SECRET", str(failure.exception))
        for invalid in ("//server/share/private.json", "\\\\server\\share\\private.json", "C:/private.json:secret"):
            with self.subTest(path_gate=True), self.assertRaises(relay.SafetyError):
                relay.load_credentials(invalid)

    def test_existing_report_precedes_private_reads_client_import_and_environment_changes(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-relay-safety-") as directory:
            report = Path(directory) / "existing.json"
            report.write_text("prior evidence must not change", encoding="utf-8")
            before = dict(os.environ)
            with patch.object(relay, "load_credentials") as load, patch.object(relay, "native_client") as client:
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    code = relay.main(["--url", "http://127.0.0.1:55115", "--credentials", "SECRET-ABSENT",
                                       "--rclone", "SECRET-ABSENT-EXE", "--report", str(report)])
                self.assertEqual(code, 2)
                load.assert_not_called()
                client.assert_not_called()
            self.assertEqual(report.read_text("utf-8"), "prior evidence must not change")
            self.assertEqual(dict(os.environ), before)
            self.assertNotIn("SECRET", output.getvalue())
            self.assertNotIn(directory, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["failureCode"], "REPORT_UNAVAILABLE")

    def test_scope_rejects_production_endpoint_and_foreign_metadata_before_client_io(self):
        run_id, dataset_id = "1" * 32, "2" * 32
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-relay-safety-") as directory:
            root = Path(directory)
            scope = relay.Scope(run_id, dataset_id)
            safe = [["lsjson", f"phone:acceptance_rw/{run_id}/mirror"],
                    ["cat", f"phone:.phonenas/datasets/{dataset_id}/state.json", "--head", "65537"],
                    ["mkdir", "phone:.phonenas/locks"],
                    ["copyto", str(root / "private.json"), f"phone:.phonenas/datasets/{dataset_id}/state.json"]]
            for args in safe:
                scope.validate_args(args, root)
            for args in (
                ["purge", "phone:acceptance_rw"], ["lsjson", "phone:backup/work"],
                ["purge", f"phone:acceptance_rw/{run_id}-other"],
                ["cat", "phone:.phonenas/datasets/" + "3" * 32 + "/state.json"],
                ["purge", "phone:.phonenas/locks"], ["purge", "phone:.phonenas/datasets"],
                ["cat", f"phone:.phonenas/datasets/{dataset_id}/../other/state.json"],
                ["copy", "https://production.invalid/", str(root / "output")],
                ["copy", ":webdav,url=http://production.invalid:", str(root / "output")],
                ["copy", "phone:acceptance_rw/" + run_id, str(root.parent / "outside")],
            ):
                with self.subTest(scope_gate=True), self.assertRaises(relay.SafetyError):
                    scope.validate_args(args, root)
            report_path = root / "report.json"
            with relay.ExclusiveReport(report_path) as report:
                live_scope = relay.Scope(report.run_id, dataset_id)
                control = relay.LoopbackControl(relay.validate_endpoint("http://127.0.0.1:55115"),
                                                relay.Credentials("offline-user", "offline-password-only-123"),
                                                report, live_scope)
                for method, path in (("DELETE", "/acceptance_rw/"),
                                     ("PUT", "/.phonenas/datasets/" + "3" * 32 + "/state.json"),
                                     ("GET", "http://127.0.0.1:55115/acceptance_rw/readme.txt")):
                    with self.subTest(control_gate=True), self.assertRaises(relay.SafetyError):
                        control.request(method, path)
                self.assertEqual(relay.CHECKS, ("preflight", "fixtures", "prepare", "upload", "receive", "return",
                                               "history", "conflict", "adopt", "repeat", "source_integrity", "cleanup"))
                self.assertTrue(hasattr(relay.RelayAcceptance, "manual_return"))
                self.assertFalse(report.finish())  # no fabricated relay PASS from offline registration

    def test_invalid_cli_inputs_stop_before_native_client_and_never_echo_values(self):
        secret = "SECRET-PRIVATE-TOKEN-PHYSICAL-PATH"
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-relay-safety-") as directory:
            private = Path(directory) / "private.json"
            private.write_text(json.dumps({"password": secret}), encoding="utf-8")
            for name, url in (("bad-endpoint", "https://100.127.10.41:18889"),
                              ("bad-private", "http://127.0.0.1:55115")):
                report = Path(directory) / (name + ".json")
                output = io.StringIO()
                with patch.object(relay, "native_client") as client, patch.object(
                    relay, "validate_rclone", return_value=relay.PROJECT / "vendor/rclone.exe"
                ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    code = relay.main(["--url", url, "--credentials", str(private),
                                       "--rclone", str(relay.PROJECT / "vendor/rclone.exe"), "--report", str(report)])
                client.assert_not_called()
                self.assertEqual(code, 2)
                payload = json.loads(report.read_text("utf-8"))
                self.assertEqual(payload["status"], "FAIL")
                combined = output.getvalue() + report.read_text("utf-8")
                self.assertNotIn(secret, combined)
                self.assertNotIn(directory, combined)
                self.assertNotIn("Traceback", combined)
                self.assertFalse(payload["remoteRelayClaim"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(relay.main(["--password=" + secret]), 2)
        self.assertNotIn(secret, output.getvalue())

    def test_private_environment_removes_overrides_contains_core_temps_and_restores_on_failure(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-relay-safety-") as directory:
            root = Path(directory)
            hostile = {"RCLONE_CONFIG": "DO-NOT-READ-PRODUCTION", "rclone_webdav_url": "http://production.invalid",
                       "HTTP_PROXY": "http://proxy.invalid", "all_proxy": "http://proxy.invalid",
                       "NO_PROXY": "*", "PHONENAS_DATA_DIR": "PRODUCTION", "PYTHONPATH": "PRODUCTION"}
            with patch.dict(os.environ, hostile):
                before = dict(os.environ)
                old_tempdir, old_path = tempfile.tempdir, list(sys.path)
                with self.assertRaisesRegex(RuntimeError, "offline controlled failure"):
                    with relay.private_environment(root):
                        self.assertFalse(any(key.upper().startswith("RCLONE_") or key.upper().endswith("PROXY")
                                             for key in os.environ))
                        self.assertTrue(Path(os.environ["PHONENAS_DATA_DIR"]).is_relative_to(root))
                        with tempfile.TemporaryDirectory() as created:
                            self.assertTrue(Path(created).is_relative_to(root))
                        raise RuntimeError("offline controlled failure")
                self.assertEqual(dict(os.environ), before)
                self.assertEqual(tempfile.tempdir, old_tempdir)
                self.assertEqual(sys.path, old_path)

    def test_real_client_boundary_is_delegated_only_with_private_explicit_config(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-relay-safety-") as directory:
            root = Path(directory)
            with relay.ExclusiveReport(root / "report.json") as report:
                scope = relay.Scope(report.run_id, "2" * 32)
                result = object()
                original_run, original_popen = Mock(return_value=result), Mock(return_value=result)
                client = SimpleNamespace(conf_path=root / "private.conf", run=original_run, popen=original_popen)
                relay.bind_private_rclone(client, scope, root, report)
                self.assertIs(client.run(["lsjson", "phone:" + scope.tree + "/mirror"]), result)
                self.assertIs(client.popen(["copy", str(root / "work"), "phone:" + scope.tree + "/mirror"]), result)
                self.assertEqual(original_run.call_args.kwargs["conf_path"], root / "private.conf")
                self.assertEqual(original_popen.call_args.kwargs["conf_path"], root / "private.conf")
                for original in (original_run, original_popen):
                    args = original.call_args.args[0]
                    self.assertEqual(args[args.index("--bind") + 1], "127.0.0.1")
                original_run.reset_mock()
                for args, options in ((["cat", "phone:.phonenas/datasets/" + "3" * 32 + "/state.json"], {}),
                                      (["lsjson", "phone:" + scope.tree], {"conf_path": root / "foreign.conf"}),
                                      (["lsjson", "http://production.invalid/"], {})):
                    with self.assertRaises(relay.SafetyError):
                        client.run(args, **options)
                original_run.assert_not_called()

    def test_cleanup_refuses_foreign_owner_held_lock_and_changed_ready_before_any_delete(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-relay-safety-") as directory:
            root = Path(directory)
            for problem in ("owner", "lock", "metadata"):
                with relay.ExclusiveReport(root / (problem + ".json")) as report:
                    scope = relay.Scope(report.run_id, "2" * 32)
                    control = Mock(created_tree=True, created_dataset=True)
                    if problem == "owner":
                        control.verify_owner.side_effect = relay.SafetyError("OWNER_UNPROVEN")
                    expected = {"phase": "ready", "generation": "4" * 32}
                    coordinator = Mock()
                    coordinator.locked.return_value = problem == "lock"
                    coordinator.read.return_value = {**expected, "phase": "publishing"} if problem == "metadata" else expected
                    acceptance = SimpleNamespace(control=control, scope=scope, root=root, publisher=object(), receiver=object(),
                                                 coordinator=coordinator, ready=expected)
                    with patch.object(relay.Scope, "validate_task"), self.assertRaises(relay.SafetyError):
                        relay.RelayAcceptance.cleanup(acceptance)
                    control.request.assert_not_called()
                    coordinator.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
