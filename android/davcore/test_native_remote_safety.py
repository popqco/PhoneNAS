"""Offline safety contracts only; never evidence of an Android/relay pass.

Run with CPython 3.14: python -I -B android/davcore/test_native_remote_safety.py
All socket connection boundaries are disabled, including for failing tests.
"""
from __future__ import annotations

import hashlib
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location(
    "phonenas_native_remote", Path(__file__).with_name("test_native_remote.py")
)
remote = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = remote
_spec.loader.exec_module(remote)


# Public, self-signed, offline fixture only. The key was discarded; never a phone pin.
OFFLINE_PEM = """-----BEGIN CERTIFICATE-----
MIIBxzCCAWygAwIBAgIUN1pFBeOxhf7b+F7WDR0m4mejCa4wCgYIKoZIzj0EAwIw
HjEcMBoGA1UEAwwTb2ZmbGluZS1zYWZldHktb25seTAeFw0yNjExMTUwNzIwMzBa
Fw0zNjExMTIwNzIwMzBaMB4xHDAaBgNVBAMME29mZmxpbmUtc2FmZXR5LW9ubHkw
WTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAARfj8zRtJ4dHGVPgQOQgT15sYuFoXn+
iRTw9M9jMX/Ri7UwWkzTEtXMpnxWzATxbNgas1hbLely5Eaiw6vta1Ygo4GHMIGE
MB0GA1UdDgQWBBSqVOgfhccRcR/nYGgxJZoFN9hj8TAfBgNVHSMEGDAWgBSqVOgf
hccRcR/nYGgxJZoFN9hj8TAPBgNVHREECDAGhwRkbaYLMAwGA1UdEwEB/wQCMAAw
DgYDVR0PAQH/BAQDAgeAMBMGA1UdJQQMMAoGCCsGAQUFBwMBMAoGCCqGSM49BAMC
A0kAMEYCIQCbXuQJGRhWmRbBNZqK3h0dSPmMtID7E1Jfy2d5yl/rggIhAMkIWXB+
2Hx1FPZgziG8CctvRCOUE0WSZ7OJ+A2hyygr
-----END CERTIFICATE-----
"""


class OfflineSafetyTests(unittest.TestCase):
    def setUp(self):
        for boundary in ("create_connection",):
            guard = patch.object(socket, boundary, side_effect=AssertionError("OFFLINE_NO_NETWORK"))
            boundary_mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(boundary_mock.assert_not_called)
        for boundary in ("connect", "connect_ex"):
            guard = patch.object(socket.socket, boundary, side_effect=AssertionError("OFFLINE_NO_NETWORK"))
            boundary_mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(boundary_mock.assert_not_called)

    def test_only_canonical_literal_tailnet_https_port_is_accepted(self):
        self.assertEqual(remote.validate_endpoint("https://100.64.0.1:18889").host, "100.64.0.1")
        self.assertEqual(remote.validate_endpoint("https://100.127.255.254:18889").port, 18889)
        rejected = (
            "http://100.64.0.1:18889", "https://127.0.0.1:18889",
            "https://localhost:18889", "https://100.64.0.1.example:18889",
            "https://100.63.255.255:18889", "https://100.128.0.0:18889",
            "https://192.168.1.3:18889", "https://[::1]:18889",
            "https://100.64.0.1", "https://100.64.0.1:18888",
            "https://100.64.0.1:018889", "https://100.064.0.1:18889",
            "https://1681915905:18889", "https://0x64400001:18889",
            "https://u:p@100.64.0.1:18889", "https://@100.64.0.1:18889",
            "https://100.64.0.1:18889/", "https://100.64.0.1:18889/test",
            "https://100.64.0.1:18889?", "https://100.64.0.1:18889#",
            "https://100.64.0.1:18889?secret=value", "https://100.64.0.1:18889#x",
            " https://100.64.0.1:18889", "https://100.64.0.1:18889\n",
            "https://100.64.0.1:18889\t", "HTTPS://100.64.0.1:18889",
            "https://100.64.0.1.:18889", "https://100.64.0.1%2f:18889",
            "https://100.64.0.1:18889\\evil", "https://100.64.0.1:18889\x00",
        )
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(remote.SafetyError):
                remote.validate_endpoint(url)

    def test_credentials_gate_fixed_demo_or_explicit_public_scope_only(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            path = Path(directory) / "private.json"
            demo = {
                "username": "phonenas", "password": "offline-password-not-for-a-phone",
                "root": "/storage/emulated/0/Android/data/com.phonenas.server.probe/files/contract-test",
            }

            def load(value, **options):
                path.write_text(json.dumps(value), encoding="utf-8")
                return remote.load_credentials(path, **options)

            credentials = load(demo)
            self.assertEqual((credentials.share_prefix, credentials.readonly_prefix), ("/test/", "/readonly/"))
            invalid = (
                {**demo, "root": "/storage/emulated/0/DCIM"},
                {**demo, "root": demo["root"] + "/../contract-test"},
                {**demo, "password": "short"}, {**demo, "password": "x" * 20 + "\n"},
                {**demo, "username": "a:b"}, {**demo, "scope": "production"},
                {**demo, "remote": "production.conf"}, [], None,
            )
            for value in invalid:
                with self.subTest(value_type=type(value).__name__), self.assertRaises(remote.SafetyError):
                    load(value)
            identity = "12345678-1234-4234-8234-123456789abc"
            public = {**demo, "scope": "isolated-public-acceptance",
                      "root": "/storage/emulated/0/PhoneNAS-Acceptance-" + identity + "/rw",
                      "readonly_root": "/storage/emulated/0/PhoneNAS-Acceptance-" + identity + "/ro"}
            explicit = {"share_prefix": "/acceptance_rw/", "readonly_prefix": "/acceptance_ro/"}
            self.assertEqual(load(public, **explicit).share_prefix, "/acceptance_rw/")
            unsafe_public = (
                {}, {"share_prefix": "/acceptance_rw/"},
                {**explicit, "share_prefix": "/test/"},
                {**explicit, "readonly_prefix": "/production/"},
                {**explicit, "share_prefix": "/acceptance_rw/../DCIM/"},
            )
            for options in unsafe_public:
                with self.subTest(options=options), self.assertRaises(remote.SafetyError):
                    load(public, **options)
            for value in (
                {**public, "scope": "demo"},
                {**public, "root": public["root"].replace("/rw", "/ro")},
                {**public, "readonly_root": public["readonly_root"].replace(identity, "abcd" * 8)},
                {**public, "root": public["root"] + "/../rw"},
                {**public, "root": "/storage/emulated/0/PhoneNAS-Acceptance-not-a-uuid/rw"},
            ):
                with self.subTest(public_gate=True), self.assertRaises(remote.SafetyError):
                    load(value, **explicit)
            with self.assertRaises(remote.SafetyError):
                load(demo, **explicit)
            path.write_text('{"username":"a","username":"b","password":"do-not-leak"}', encoding="utf-8")
            with self.assertRaises(remote.SafetyError) as failure:
                remote.load_credentials(path)
            self.assertNotIn("do-not-leak", str(failure.exception))
        for unsafe_path in ("//server/share/credentials.json", "\\\\server\\share\\c.json", "C:/secret.json:stream"):
            with self.subTest(unsafe_path=unsafe_path), self.assertRaises(remote.SafetyError):
                remote.load_credentials(Path(unsafe_path))

    def test_report_is_reserved_exclusively_and_failure_output_is_allowlisted(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            path = Path(directory) / "report.json"
            with remote.ExclusiveReport(path) as report:
                self.assertEqual(json.loads(path.read_text("utf-8"))["status"], "RUNNING")
                with self.assertRaises(remote.SafetyError):
                    with remote.ExclusiveReport(path):
                        self.fail("an existing report must never be reused")
                report.check("authentication", lambda: (_ for _ in ()).throw(
                    RuntimeError("password=SECRET Authorization: Basic ABC body=PRIVATE")
                ))
                report.finish()
            output = path.read_text("utf-8")
            self.assertEqual(json.loads(output)["status"], "FAIL")
            self.assertEqual(json.loads(output)["checks"][0]["failureCode"], "INTERNAL")
            for secret in ("SECRET", "ABC", "PRIVATE", "Authorization", directory):
                self.assertNotIn(secret, output)
            before = path.read_bytes()
            with self.assertRaises(remote.SafetyError):
                with remote.ExclusiveReport(path):
                    pass
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(str(remote.SafetyError("password=SECRET")), "INTERNAL")

    def test_certificate_requires_exact_pin_ip_san_and_only_explicit_trust(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            path = Path(directory) / "public.pem"
            path.write_text(OFFLINE_PEM, encoding="ascii")
            pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(OFFLINE_PEM)).hexdigest()
            endpoint = remote.validate_endpoint("https://100.127.10.41:18889")
            trust = remote.load_certificate(path, pin.upper(), endpoint)
            self.assertEqual(trust.fingerprint, pin)
            self.assertEqual(trust.context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(trust.context.check_hostname)
            self.assertFalse(trust.context.hostname_checks_common_name)
            self.assertGreaterEqual(trust.context.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertEqual(trust.context.get_ca_certs(binary_form=True), [])  # non-CA phone identity
            self.assertEqual(trust.context.cert_store_stats()["x509"], 1)
            for invalid_pin in ("0" * 64, pin + "\n", "sha256:" + pin, ":".join([pin[i:i+2] for i in range(0, 64, 2)]), ""):
                with self.subTest(pin_gate=True), self.assertRaises(remote.SafetyError):
                    remote.load_certificate(path, invalid_pin, endpoint)
            with self.assertRaises(remote.SafetyError):
                remote.load_certificate(path, pin, remote.validate_endpoint("https://100.64.0.1:18889"))
            for invalid in (OFFLINE_PEM + OFFLINE_PEM, OFFLINE_PEM + "PRIVATE KEY", "not a cert"):
                path.write_text(invalid, encoding="ascii")
                with self.subTest(pem_gate=True), self.assertRaises(remote.SafetyError):
                    remote.load_certificate(path, pin, endpoint)

    def test_tls_client_requires_report_reservation_and_confines_outbound_paths(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            base = Path(directory)
            cert = base / "public.pem"
            cert.write_text(OFFLINE_PEM, encoding="ascii")
            private = base / "private.json"
            private.write_text(json.dumps({"username": "phonenas", "password": "offline-only-password-123456",
                                           "root": remote.DEMO_ROOT}), encoding="utf-8")
            endpoint = remote.validate_endpoint("https://100.127.10.41:18889")
            trust = remote.load_certificate(cert, hashlib.sha256(ssl.PEM_cert_to_DER_cert(OFFLINE_PEM)).hexdigest(), endpoint)
            credentials = remote.load_credentials(private)
            unreserved = remote.ExclusiveReport(base / "not-open.json")
            with self.assertRaises(remote.SafetyError):
                remote.TLSClient(endpoint, credentials, trust, unreserved)
            with remote.ExclusiveReport(base / "reserved.json") as report:
                client = remote.TLSClient(endpoint, credentials, trust, report)
                unsafe = ("/", "/production/file", "/test/", "/test/readme.txt", "/.phonenas/locks/another/",
                          client.prefix + "/../outside", client.prefix + "/%2e%2e/outside",
                          client.prefix + "/a%2fb", client.prefix + "/a%5Cb", client.prefix + "-lookalike/file",
                          "https://100.127.10.41:18889" + client.prefix + "/file")
                for path in unsafe:
                    with self.subTest(path=path), self.assertRaises(remote.SafetyError):
                        client.request("PUT", path, b"must never leave this process")
                with self.assertRaises(remote.SafetyError):
                    client.request("MOVE", client.file("source"), headers={"Destination": "http://100.127.10.41:18889" + client.file("dest")})
                report.record("preflight", "PASS", 0)
                report.finish()
            with self.assertRaises(remote.SafetyError):
                client.request("GET", client.file("safe"))

    def test_native_suite_has_eight_explicit_finite_contracts(self):
        self.assertEqual(tuple(check.__name__ for check in remote.NATIVE_CHECKS), (
            "authentication", "files_range_and_names", "exclusive_lock", "interrupted_uploads",
            "readonly", "move_overwrite", "path_policy", "large_content",
        ))
        # This validates registration only, not server behavior or a real-device pass.

    def test_cli_refuses_existing_report_and_bad_private_inputs_before_network(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            base = Path(directory)
            report = base / "report.json"
            report.write_text("keep this existing evidence", encoding="utf-8")
            private = base / "private.json"
            private.write_text('{"password":"NEVER-PRINT-THIS-SECRET"}', encoding="utf-8")
            cert = base / "public.pem"
            cert.write_text(OFFLINE_PEM, encoding="ascii")
            args = ["--url", "https://100.127.10.41:18889", "--credentials", str(private),
                    "--certificate", str(cert), "--fingerprint", "0" * 64, "--report", str(report)]
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = remote.main(args)
            self.assertNotEqual(code, 0)
            self.assertIn("REPORT_UNAVAILABLE", output.getvalue())
            self.assertEqual(report.read_text("utf-8"), "keep this existing evidence")
            report.unlink()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = remote.main(args)
            payload = json.loads(report.read_text("utf-8"))
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["status"], "FAIL")
            self.assertNotIn("NEVER-PRINT-THIS-SECRET", output.getvalue() + report.read_text("utf-8"))
            self.assertNotIn("Traceback", output.getvalue())
            self.assertNotIn("OFFLINE_NO_NETWORK", output.getvalue() + report.read_text("utf-8"))

    def test_peer_pin_is_checked_before_credentials_and_redirect_is_never_followed(self):
        # Mock only the outbound HTTPS boundary, not a host/phone replacement.
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            base = Path(directory)
            cert = base / "cert.pem"
            cert.write_text(OFFLINE_PEM, encoding="ascii")
            private = base / "private.json"
            private.write_text(json.dumps({"username": "phonenas", "password": "no-leak-offline-password-12345",
                                           "root": remote.DEMO_ROOT}), encoding="utf-8")
            endpoint = remote.validate_endpoint("https://100.127.10.41:18889")
            der = ssl.PEM_cert_to_DER_cert(OFFLINE_PEM)
            trust = remote.load_certificate(cert, hashlib.sha256(der).hexdigest(), endpoint)
            with remote.ExclusiveReport(base / "report.json") as report:
                client = remote.TLSClient(endpoint, remote.load_credentials(private), trust, report)
                connection = Mock()
                connection.sock.getpeercert.return_value = b"not the pinned certificate"
                with patch.object(remote.http.client, "HTTPSConnection", return_value=connection):
                    with self.assertRaises(remote.SafetyError) as failure:
                        client.request("GET", client.file("safe"))
                self.assertEqual(failure.exception.code, "TLS_PEER_PIN")
                connection.request.assert_not_called()  # no Authorization before pin check
                connection.sock.getpeercert.return_value = der
                connection.getresponse.return_value.status = 302
                with patch.object(remote.http.client, "HTTPSConnection", return_value=connection) as transport:
                    with self.assertRaises(remote.SafetyError) as failure:
                        client.request("GET", client.file("safe"))
                self.assertEqual(failure.exception.code, "REDIRECT_REFUSED")
                self.assertEqual(transport.call_count, 1)
                report.record("preflight", "PASS", 0)
                report.finish()

    def test_cleanup_never_deletes_unknown_or_changed_owner(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            base = Path(directory)
            endpoint = remote.validate_endpoint("https://100.127.10.41:18889")
            credentials = remote.Credentials("phonenas", "offline-password-123456", "/test/", "/readonly/", "isolated-app-demo")
            trust = remote.Trust(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), "0" * 64, "")
            with remote.ExclusiveReport(base / "report.json") as report:
                client = remote.TLSClient(endpoint, credentials, trust, report)
                connection = Mock()
                connection.sock.getpeercert.return_value = b"unused"
                with patch.object(remote.http.client, "HTTPSConnection", return_value=connection) as transport:
                    client.cleanup()
                transport.assert_not_called()
                # Owner is deliberately invalid; even a guessed own UUID must not authorize DELETE.
                client.owns_tree = True
                der = b"offline-peer"
                client.trust = remote.Trust(trust.context, hashlib.sha256(der).hexdigest(), "")
                connection.sock.getpeercert.return_value = der
                response = connection.getresponse.return_value
                response.status = 200
                response.read.return_value = b'{"token":"someone-else"}'
                response.getheaders.return_value = []
                with patch.object(remote.http.client, "HTTPSConnection", return_value=connection):
                    with self.assertRaises(remote.SafetyError) as failure:
                        client.cleanup()
                self.assertEqual(failure.exception.code, "OWNER_UNPROVEN")
                self.assertEqual([call.args[0] for call in connection.request.call_args_list], ["GET"])
                report.record("preflight", "PASS", 0)
                report.finish()

    def test_cli_argument_errors_cannot_echo_secret_values(self):
        secret = "SECRET-IN-AN-UNKNOWN-OPTION"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = remote.main(["--password=" + secret])
        self.assertEqual(code, 2)
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["failureCode"], "UNSAFE_REQUEST")

    def test_existing_report_gate_never_reads_credentials_or_certificate(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            report = Path(directory) / "existing.json"
            report.write_text("original", encoding="utf-8")
            # Deliberately unavailable files cannot hide a report gate regression.
            arguments = ["--url", "https://100.127.10.41:18889", "--credentials", "absent-private.json",
                         "--certificate", "absent-public.pem", "--fingerprint", "0" * 64, "--report", str(report)]
            reads = []
            original = Path.open

            def track_open(path, *args, **kwargs):
                if path.name.startswith("absent-"):
                    reads.append(path.name)
                return original(path, *args, **kwargs)

            with patch.object(Path, "open", track_open), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(remote.main(arguments), 2)
            self.assertEqual(reads, [])
            self.assertEqual(report.read_text("utf-8"), "original")

    def test_maximum_response_and_safe_uuid_cannot_be_overridden_by_http_data(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            base = Path(directory)
            with remote.ExclusiveReport(base / "report.json") as report:
                endpoint = remote.validate_endpoint("https://100.127.10.41:18889")
                der = b"only-offline-boundary"
                trust = remote.Trust(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), hashlib.sha256(der).hexdigest(), "")
                credentials = remote.Credentials("phonenas", "offline-password-123456", "/test/", "/readonly/", "isolated-app-demo")
                client = remote.TLSClient(endpoint, credentials, trust, report)
                connection = Mock()
                connection.sock.getpeercert.return_value = der
                response = connection.getresponse.return_value
                response.status = 200
                response.read.return_value = b"x" * (remote.MAX_RESPONSE + 1)
                with patch.object(remote.http.client, "HTTPSConnection", return_value=connection):
                    with self.assertRaises(remote.SafetyError) as failure:
                        client.request("GET", client.file("safe"))
                self.assertEqual(failure.exception.code, "RESPONSE_LIMIT")
                self.assertEqual(len(report.run_id), 32)
                with self.assertRaises(remote.SafetyError):
                    report.record("secret-server-checkname", "PASS", 0)
                report.record("preflight", "PASS", 0)
                report.finish()

    def test_truncated_tls_success_reply_is_rejected_and_old_file_is_read_back(self):
        # Only transport/TLS boundaries are fake; no HTTP server or device substitute.
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            peer = b"offline-pin"
            trust = remote.Trust(context, hashlib.sha256(peer).hexdigest(), "")
            credentials = remote.Credentials("phonenas", "offline-password-123456", "/test/", "/readonly/", "isolated-app-demo")
            with remote.ExclusiveReport(Path(directory) / "report.json") as report:
                client = remote.TLSClient(remote.validate_endpoint("https://100.127.10.41:18889"), credentials, trust, report)
                tls = Mock()
                tls.getpeercert.return_value = peer
                tls.write.side_effect = lambda data: len(data)
                tls.unwrap.side_effect = ssl.SSLWantReadError()
                tls.read.side_effect = [b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", b""]
                wire = Mock()
                wire.__enter__ = Mock(return_value=wire)
                wire.__exit__ = Mock(return_value=False)
                connection = Mock()
                connection.sock.getpeercert.return_value = peer
                created, readback = Mock(), Mock()
                created.status, created.read.return_value, created.getheaders.return_value = 201, b"", []
                readback.status, readback.read.return_value, readback.getheaders.return_value = 200, b"previous-complete-file", []
                connection.getresponse.side_effect = [created, readback]
                with patch.object(context, "wrap_bio", return_value=tls), patch.object(
                    remote.socket, "create_connection", return_value=wire
                ), patch.object(remote.http.client, "HTTPSConnection", return_value=connection):
                    with self.assertRaises(remote.SafetyError) as failure:
                        remote.interrupted_uploads(client)
                self.assertEqual(failure.exception.code, "INTERRUPTED_SUCCESS")
                self.assertEqual([call.args[0] for call in connection.request.call_args_list], ["PUT", "GET"])
                self.assertEqual(connection.request.call_args_list[-1].args[1], client.file("complete.bin"))
                report.record("preflight", "PASS", 0)
                report.finish()

    def test_storage_profile_is_explicit_and_only_valid_in_public_scope(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            path = Path(directory) / "private.json"
            demo = {"username": "phonenas", "password": "offline-profile-password-123456", "root": remote.DEMO_ROOT}
            public = {**demo, "scope": remote.PUBLIC_SCOPE,
                      "root": "/storage/emulated/0/PhoneNAS-Acceptance-12345678123442348234123456789abc/rw",
                      "readonly_root": "/storage/emulated/0/PhoneNAS-Acceptance-12345678123442348234123456789abc/ro"}
            aliases = {"share_prefix": "/acceptance_rw/", "readonly_prefix": "/acceptance_ro/"}
            path.write_text(json.dumps(demo), encoding="utf-8")
            self.assertEqual(remote.load_credentials(path).storage_profile, "default")
            with self.assertRaises(remote.SafetyError) as failure:
                remote.load_credentials(path, storage_profile="android-public")
            self.assertEqual(failure.exception.code, "UNSAFE_SCOPE")
            path.write_text(json.dumps(public), encoding="utf-8")
            self.assertEqual(remote.load_credentials(path, **aliases).storage_profile, "default")
            self.assertEqual(remote.load_credentials(path, **aliases, storage_profile="android-public").storage_profile,
                             "android-public")
            with self.assertRaises(remote.SafetyError):
                remote.load_credentials(path, **aliases, storage_profile="automatic")

    def test_cli_profile_is_recorded_and_demo_scope_fails_before_network(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            base = Path(directory)
            private = base / "private.json"
            private.write_text(json.dumps({"username": "phonenas", "password": "offline-profile-password-123456",
                                           "root": remote.DEMO_ROOT}), encoding="utf-8")
            report = base / "report.json"
            args = ["--url", "https://100.127.10.41:18889", "--credentials", str(private),
                    "--certificate", str(base / "absent.pem"), "--fingerprint", "0" * 64,
                    "--report", str(report), "--storage-profile", "android-public"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(remote.main(args), 1)
            data = json.loads(report.read_text("utf-8"))
            self.assertEqual(data["storageProfile"], "android-public")
            self.assertEqual(data["filenameContract"], "android-public-capability-not-full-filename-contract")
            self.assertEqual(data["checks"][0]["failureCode"], "UNSAFE_SCOPE")
            self.assertEqual(data["status"], "FAIL")

    def test_profile_names_use_real_encoded_paths_and_default_keeps_question_mark(self):
        for profile, leaf in (
            ("default", "%E4%B8%AD%E6%96%87%20%E7%A9%BA%E6%A0%BC%25%23%3F.bin"),
            ("android-public", "%E4%B8%AD%E6%96%87%20%E7%A9%BA%E6%A0%BC%25%23.bin"),
        ):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
                credentials = remote.Credentials("phonenas", "offline-profile-password-123456", "/acceptance_rw/",
                                                 "/acceptance_ro/", remote.PUBLIC_SCOPE, profile)
                peer = b"offline-profile-peer"
                trust = remote.Trust(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), hashlib.sha256(peer).hexdigest(), "")
                with remote.ExclusiveReport(Path(directory) / "report.json", storage_profile=profile) as report:
                    client = remote.TLSClient(remote.validate_endpoint("https://100.127.10.41:18889"), credentials, trust, report)
                    path = client.prefix + "/" + leaf
                    responses = []
                    listing = ('<d:multistatus xmlns:d="DAV:"><d:response><d:href>' + path +
                               '</d:href></d:response></d:multistatus>').encode("ascii")
                    for status, headers, body in (
                        (201, [], b""), (200, [], b"0123456789abcdef"),
                        (206, [("Content-Range", "bytes 3-7/16")], b"34567"),
                        (200, [("Content-Length", "16")], b""), (207, [], listing),
                        (201, [], b""), (200, [], b"0123456789abcdef"), (404, [], b""),
                    ):
                        response = Mock(status=status)
                        response.read.return_value = body
                        response.getheaders.return_value = headers
                        responses.append(response)
                    connection = Mock()
                    connection.sock.getpeercert.return_value = peer
                    connection.getresponse.side_effect = responses
                    with patch.object(remote.http.client, "HTTPSConnection", return_value=connection):
                        remote.files_range_and_names(client)
                    requests = connection.request.call_args_list
                    self.assertEqual(requests[0].args[:2], ("PUT", path))
                    self.assertEqual(requests[2].args[3]["Range"], "bytes=3-7")
                    self.assertEqual(requests[5].args[3]["Destination"], client.destination(client.file("历史文件.bin")))
                    expected_checks = tuple(check.__name__ for check in remote.NATIVE_CHECKS)
                    if profile == "android-public":
                        expected_checks += ("public_question_mark_rejected",)
                    self.assertEqual(tuple(check.__name__ for check in remote.checks_for_profile(credentials)), expected_checks)
                    report.record("preflight", "PASS", 0)
                    report.finish()

    def test_public_question_mark_contract_requires_rejection_no_alias_or_temp_and_unchanged_fixtures(self):
        for violation in (None, "put_success", "get_visible", "temporary_export", "mapped_alias", "fixture_changed"):
            with self.subTest(violation=violation), tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
                credentials = remote.Credentials("phonenas", "offline-profile-password-123456", "/acceptance_rw/",
                                                 "/acceptance_ro/", remote.PUBLIC_SCOPE, "android-public")
                peer = b"offline-profile-peer"
                trust = remote.Trust(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), hashlib.sha256(peer).hexdigest(), "")
                with remote.ExclusiveReport(Path(directory) / "report.json", storage_profile="android-public") as report:
                    client = remote.TLSClient(remote.validate_endpoint("https://100.127.10.41:18889"), credentials, trust, report)
                    listing = b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/safe-owner</d:href></d:response></d:multistatus>'
                    after_listing = listing
                    if violation in ("temporary_export", "mapped_alias"):
                        name = ".phonenas-temp-leaked" if violation == "temporary_export" else "hidden-alias.bin"
                        after_listing = listing.replace(b'</d:multistatus>',
                            b'<d:response><d:href>' + name.encode("ascii") + b'</d:href></d:response></d:multistatus>')
                    specs = [(200, b"rw-fixture"), (200, b"ro-fixture"), (207, listing),
                             (201 if violation == "put_success" else 403, b""),
                             (200 if violation == "get_visible" else 404, b""), (207, after_listing),
                             (200, b"changed" if violation == "fixture_changed" else b"rw-fixture"), (200, b"ro-fixture")]
                    responses = []
                    for status, body in specs:
                        response = Mock(status=status)
                        response.read.return_value, response.getheaders.return_value = body, []
                        responses.append(response)
                    connection = Mock()
                    connection.sock.getpeercert.return_value = peer
                    connection.getresponse.side_effect = responses
                    with patch.object(remote.http.client, "HTTPSConnection", return_value=connection):
                        if violation is None:
                            remote.public_question_mark_rejected(client)
                        else:
                            with self.assertRaises(remote.SafetyError):
                                remote.public_question_mark_rejected(client)
                    requests = connection.request.call_args_list
                    self.assertEqual(requests[3].args[:2], ("PUT", client.prefix + "/question%3F.bin"))
                    self.assertEqual(requests[4].args[:2], ("GET", client.prefix + "/question%3F.bin"))
                    self.assertEqual([call.args[1] for call in requests[-2:]],
                                     ["/acceptance_rw/readme.txt", "/acceptance_ro/readme.txt"])
                    report.record("preflight", "PASS", 0)
                    report.finish()

    def test_default_question_mark_failure_stays_red_without_fallback(self):
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-safety-") as directory:
            credentials = remote.Credentials("phonenas", "offline-profile-password-123456", "/acceptance_rw/",
                                             "/acceptance_ro/", remote.PUBLIC_SCOPE)
            peer = b"offline-default-contract"
            trust = remote.Trust(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), hashlib.sha256(peer).hexdigest(), "")
            with remote.ExclusiveReport(Path(directory) / "report.json") as report:
                client = remote.TLSClient(remote.validate_endpoint("https://100.127.10.41:18889"), credentials, trust, report)
                connection = Mock()
                connection.sock.getpeercert.return_value = peer
                response = connection.getresponse.return_value
                response.status, response.read.return_value, response.getheaders.return_value = 403, b"", []
                with patch.object(remote.http.client, "HTTPSConnection", return_value=connection):
                    self.assertFalse(report.check("files_range_and_names", lambda: remote.files_range_and_names(client)))
                self.assertEqual(connection.request.call_count, 1)
                self.assertTrue(connection.request.call_args.args[1].endswith("%25%23%3F.bin"))
                self.assertFalse(report.finish())
            data = json.loads((Path(directory) / "report.json").read_text("utf-8"))
            self.assertEqual(data["status"], "FAIL")
            self.assertEqual(data["storageProfile"], "default")
        demo = remote.Credentials("phonenas", "offline-profile-password-123456", "/test/", "/readonly/",
                                  "isolated-app-demo", "android-public")
        with self.assertRaises(remote.SafetyError):
            remote.checks_for_profile(demo)


if __name__ == "__main__":
    unittest.main(verbosity=2)
