from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import tempfile
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit

from app.core.config import Config
from app.core.rclone import Rclone, Result, rclone_exe
from app.core.relay import DatasetCoordinator
from app.core.tasks import new_task


# Deliberately public test keys. These self-signed certificates are valid for
# the loopback IP only and never installed in any user/system trust store.
PHONE_CERT = """-----BEGIN CERTIFICATE-----
MIIBejCCASGgAwIBAgIBATAKBggqhkjOPQQDAjAjMSEwHwYDVQQDDBhQaG9uZU5B
UyBsb29wYmFjayB0ZXN0IDEwHhcNMjUwMTAxMDAwMDAwWhcNMzUwMTAxMDAwMDAw
WjAjMSEwHwYDVQQDDBhQaG9uZU5BUyBsb29wYmFjayB0ZXN0IDEwWTATBgcqhkjO
PQIBBggqhkjOPQMBBwNCAAR5hMFEYXrbMpY19n7aFHLR2JkXcJVxm9Ge7uGZ4M/T
TERvx+8gqEWIxW8IFICIvMvP2/TmDrJZ2qqDBWl11rz0o0YwRDAOBgNVHQ8BAf8E
BAMCB4AwEwYDVR0lBAwwCgYIKwYBBQUHAwEwDAYDVR0TAQH/BAIwADAPBgNVHREE
CDAGhwR/AAABMAoGCCqGSM49BAMCA0cAMEQCIBsEqgD36IWfpGnOeJjbz6FFmjnO
irSpivaXZzcAF90TAiBOKBYWo5KY14PG9yCUiRajlCkaQilxOL4EoJ1FAxzVwQ==
-----END CERTIFICATE-----
"""
PHONE_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgegq6mDX7gWHYbnA8
dsmoCe+2RZySCHZ/ivTau/+xAVShRANCAAR5hMFEYXrbMpY19n7aFHLR2JkXcJVx
m9Ge7uGZ4M/TTERvx+8gqEWIxW8IFICIvMvP2/TmDrJZ2qqDBWl11rz0
-----END PRIVATE KEY-----
"""
REPLACEMENT_CERT = """-----BEGIN CERTIFICATE-----
MIIBfDCCASGgAwIBAgIBAjAKBggqhkjOPQQDAjAjMSEwHwYDVQQDDBhQaG9uZU5B
UyBsb29wYmFjayB0ZXN0IDIwHhcNMjUwMTAxMDAwMDAwWhcNMzUwMTAxMDAwMDAw
WjAjMSEwHwYDVQQDDBhQaG9uZU5BUyBsb29wYmFjayB0ZXN0IDIwWTATBgcqhkjO
PQIBBggqhkjOPQMBBwNCAASVk8v9+xT94g2CyCoIIZyPV8hzMcqqceGQ+TOjPF4a
71ZSib19FHUsxFAXecQwzlKUtvy8zbPYpaR0sv83jUsGo0YwRDAOBgNVHQ8BAf8E
BAMCB4AwEwYDVR0lBAwwCgYIKwYBBQUHAwEwDAYDVR0TAQH/BAIwADAPBgNVHREE
CDAGhwR/AAABMAoGCCqGSM49BAMCA0kAMEYCIQDrambP3PQnYh49R9srn0BF6Ubz
hmpSQZuf4465r+4elQIhAICUaTVi8U+WvGlRAuSFljchRaVK40zCpI3bFtZtXTQ5
-----END CERTIFICATE-----
"""
REPLACEMENT_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgP+IljWNhQi9XA5yl
16+V261U8XdlzCvcrHjDcf83SwWhRANCAASVk8v9+xT94g2CyCoIIZyPV8hzMcqq
ceGQ+TOjPF4a71ZSib19FHUsxFAXecQwzlKUtvy8zbPYpaR0sv83jUsG
-----END PRIVATE KEY-----
"""


class LoopbackDAV(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _reply(self, status, body=b"", **headers):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _handle(self):
        self.server.requests.append((self.command, self.path, self.headers.get("Authorization")))
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.server.redirect and self.path == self.server.redirect_path:
            return self._reply(self.server.redirect_status, Location=self.server.redirect)
        if self.command == "PROPFIND":
            if self.path.startswith("/.phonenas/locks/") and self.headers.get("Depth") == "1":
                children = (self.path, self.path + "owner.json")
                rows = "".join(f"<D:response><D:href>{path}</D:href></D:response>" for path in children)
                return self._reply(207, f'<D:multistatus xmlns:D="DAV:">{rows}</D:multistatus>'.encode())
            self._reply(207, b'<D:multistatus xmlns:D="DAV:"><D:response><D:href>/'
                        b'</D:href><D:propstat><D:status>HTTP/1.1 200 OK</D:status>'
                        b'<D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>'
                        b'</D:propstat></D:response></D:multistatus>')
        elif self.command == "GET":
            self._reply(200, self.server.owner)
        elif self.command == "PUT":
            self.server.owner = body
            self._reply(201)
        elif self.command == "DELETE":
            self._reply(204)
        else:
            self._reply(201)

    do_GET = do_PROPFIND = do_MKCOL = do_PUT = do_DELETE = _handle


@contextmanager
def dav_server(root, *, identity=(PHONE_CERT, PHONE_KEY), redirect="", redirect_path="/", redirect_status=307):
    with ThreadingHTTPServer(("127.0.0.1", 0), LoopbackDAV) as server:
        server.requests = []
        server.owner = b""
        server.redirect, server.redirect_path, server.redirect_status = redirect, redirect_path, redirect_status
        scheme = "http"
        if identity is not None:
            cert, key = root / "fixture-cert.pem", root / "fixture-key.pem"
            cert.write_text(identity[0], encoding="ascii")
            key.write_text(identity[1], encoding="ascii")
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            scheme = "https"
        host, port = server.server_address
        server.url = f"{scheme}://{host}:{port}"
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


@unittest.skipUnless(rclone_exe().is_file(), "requires bundled rclone and uses only loopback WebDAV")
class WebDAVHttpsTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="PhoneNAS-https-tests-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        env = {name: value for name, value in os.environ.items()
               if not name.upper().startswith(("RCLONE_", "PHONENAS_", "SSL_CERT_")) and not name.lower().endswith("_proxy")}
        for key in ("APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE", "TEMP", "TMP", "PHONENAS_DATA_DIR"):
            env[key] = str(self.root)
        self.enterContext(patch.dict(os.environ, env, clear=True))
        self.enterContext(patch.object(tempfile, "tempdir", str(self.root)))
        self.config = Config(self.root / "config.json")
        self.client = Rclone(self.config, conf_path=self.root / "rclone.conf")

    def test_unenrolled_certificate_stops_connection_test_and_lock(self):
        with dav_server(self.root) as phone:
            self.config.set("nas.url", phone.url)
            cert = self.root / "fixture-cert.pem"
            with patch.dict(os.environ, {"RCLONE_NO_CHECK_CERTIFICATE": "true", "RCLONE_CA_CERT": str(cert),
                                         "SSL_CERT_FILE": str(cert)}):
                result = self.client.test_connection(phone.url, "alice", "test-only")
                self.assertFalse(result.ok, "The unknown certificate must not be trusted")
                self.assertEqual(phone.requests, [])
                self.client.write_conf(phone.url, "alice", "test-only")
                task = new_task(str(self.root / "work"))
                with self.assertRaisesRegex(RuntimeError, "证书|certificate"):
                    with DatasetCoordinator(self.client, task).lease():
                        self.fail("a lock must not be acquired using unverified HTTPS")
                with patch.object(self.client, "run", return_value=Result(True, "", "", 0)), \
                        self.assertRaisesRegex(RuntimeError, "证书校验失败"):
                    with DatasetCoordinator(self.client, task).lease():
                        self.fail("urllib must not trust the external SSL_CERT_FILE")
                self.assertEqual(phone.requests, [])


    def enroll(self, url):
        self.config.set("nas.url", url)
        self.config.set("nas.username", "alice")
        self.config.set_trusted_certificate(url, PHONE_CERT)
        self.config.save()
        self.config.load()
        self.client.write_conf(url, "alice", "test-only")

    def test_explicit_phone_leaf_supports_rclone_and_verified_atomic_lease(self):
        with dav_server(self.root) as phone:
            self.enroll(phone.url)
            with patch.dict(os.environ, {"RCLONE_NO_CHECK_CERTIFICATE": "true", "SSL_CERT_FILE": "untrusted.pem",
                                         "HTTPS_PROXY": "http://127.0.0.1:1", "NO_PROXY": "*"}):
                result = self.client.test_connection(phone.url, "alice", "test-only")
                self.assertTrue(result.ok, result.err)
                task = new_task(str(self.root / "work"))
                with DatasetCoordinator(self.client, task).lease():
                    self.assertTrue(phone.owner)
        self.assertEqual([method for method, path, _ in phone.requests if task.dataset_id in path],
                         ["MKCOL", "PUT", "GET", "PROPFIND", "DELETE"])
        self.assertTrue(all(auth and auth.startswith("Basic ") for _, _, auth in phone.requests))

    def test_enrolled_leaf_does_not_accept_a_different_certificate_or_wrong_san(self):
        for identity, wrong_name in (((REPLACEMENT_CERT, REPLACEMENT_KEY), False), ((PHONE_CERT, PHONE_KEY), True)):
            with self.subTest(wrong_name=wrong_name), dav_server(self.root, identity=identity) as phone:
                url = phone.url.replace("127.0.0.1", "localhost") if wrong_name else phone.url
                self.enroll(url)
                result = self.client.test_connection(url, "alice", "test-only")
                self.assertFalse(result.ok)
                self.assertRegex(result.err, "certificate|x509")
                # Skip only the rclone lock-parent operation to exercise urllib's own verification.
                with patch.object(self.client, "run", return_value=Result(True, "", "", 0)), \
                        self.assertRaisesRegex(RuntimeError, "证书校验失败"):
                    with DatasetCoordinator(self.client, new_task(str(self.root / "work"))).lease():
                        self.fail("urllib must verify both the certificate and SAN")
                self.assertEqual(phone.requests, [])

    def test_rclone_refuses_redirect_to_another_port_or_plaintext_even_with_same_leaf(self):
        for target_identity in ((PHONE_CERT, PHONE_KEY), None):
            with self.subTest(https=target_identity is not None), dav_server(self.root, identity=target_identity) as target:
                with dav_server(self.root, redirect=target.url + "/") as phone:
                    self.enroll(phone.url)
                    result = self.client.test_connection(phone.url, "alice", "test-only")
                    self.assertFalse(result.ok, result.out)
                    self.assertTrue(phone.requests)
                    self.assertEqual(target.requests, [])

    def test_urllib_rejects_redirect_without_forwarding_lock_or_credentials(self):
        with dav_server(self.root) as target, dav_server(self.root) as phone:
            self.enroll(phone.url)
            task = new_task(str(self.root / "work"))
            phone.redirect = target.url + "/"
            phone.redirect_path = f"/.phonenas/locks/{task.dataset_id}/"
            with self.assertRaisesRegex(RuntimeError, "HTTP 307"):
                with DatasetCoordinator(self.client, task).lease():
                    self.fail("a redirected lock must not be acquired")
            self.assertEqual(target.requests, [])
            self.assertEqual([m for m, p, _ in phone.requests if p == phone.redirect_path], ["MKCOL"])

    def test_unsaved_other_url_and_mismatched_rclone_conf_do_not_use_enrolled_trust(self):
        with dav_server(self.root) as phone, dav_server(self.root) as other:
            self.enroll(phone.url)
            saved_credentials = self.client.conf_path.read_bytes()
            result = self.client.test_connection(other.url, "alice", "test-only")
            self.assertFalse(result.ok)
            self.assertRegex(result.err, "certificate|x509")
            self.assertEqual(self.client.conf_path.read_bytes(), saved_credentials)
            self.config.set("nas.url", other.url)
            with patch("app.core.rclone.subprocess.Popen", side_effect=AssertionError("must not launch")) as spawn:
                result = self.client.run(["lsd", "phone:"])
                self.assertFalse(result.ok)
                self.assertIn("地址与当前连接配置不一致", result.err)
                with self.assertRaisesRegex(ValueError, "地址与当前连接配置不一致"):
                    self.client.popen(["mount", "phone:", "W:"])
                spawn.assert_not_called()
            self.assertEqual(phone.requests, [])
            self.assertEqual(other.requests, [])

    def test_mount_shares_enrollment_and_releases_temporary_transport(self):
        with dav_server(self.root) as phone:
            self.enroll(phone.url)
            with patch("app.core.rclone.subprocess.Popen") as spawn:
                proc = self.client.popen(["mount", "phone:", "W:", "--read-only"])
            self.addCleanup(self.client.release, proc)
            command = spawn.call_args.args[0]
            leaf = Path(command[command.index("--ca-cert") + 1])
            proxy = urlsplit(command[command.index("--http-proxy") + 1])
            self.assertEqual(leaf.read_text(encoding="ascii"), PHONE_CERT)
            self.assertEqual(proxy.hostname, "127.0.0.1")
            self.assertNotIn("--no-check-certificate", command)
            self.assertNotIn("--webdav-encoding", command)
            expected = urlsplit(phone.url).netloc
            authorization = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
            for target, auth in ((expected, "bad"), ("127.0.0.1:1", authorization)):
                with self.subTest(target=target), socket.create_connection((proxy.hostname, proxy.port), timeout=5) as conn:
                    request = f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
                    conn.sendall(request.encode("ascii"))
                    self.assertIn(b"403 Forbidden", conn.recv(256))
            self.assertEqual(phone.requests, [])
            self.client.release(proc)
            self.assertFalse(leaf.exists())
            with self.assertRaises(OSError):
                socket.create_connection((proxy.hostname, proxy.port), timeout=1)

    def test_import_and_export_cannot_enroll_or_transfer_tls_trust(self):
        url = "https://127.0.0.1:12345/dav"
        self.config.set("nas.url", url)
        self.config.set_trusted_certificate(url, PHONE_CERT)
        self.config.save()
        exported = self.root / "export.json"
        self.config.export_to(exported)
        exported_data = json.loads(exported.read_text(encoding="utf-8"))
        self.assertNotIn("tls_cert", exported_data["nas"])
        self.assertNotIn("tls_url", exported_data["nas"])
        exported_data["nas"].update(tls_url=url, tls_cert=REPLACEMENT_CERT)
        exported.write_text(json.dumps(exported_data), encoding="utf-8")
        self.config.import_from(exported)
        self.assertEqual(self.config.trusted_certificate_for(url), PHONE_CERT)
        other = Config(self.root / "other.json")
        other.import_from(exported)
        self.assertEqual(other.trusted_certificate_for(url), "")
        self.config.set("nas.url", url + "-other")
        self.config.save()
        self.config.load()
        self.config.set("nas.url", url)
        self.config.save()
        self.assertEqual(Config(self.config.path).trusted_certificate_for(url), "")


class TlsSettingsTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        scratch = tempfile.TemporaryDirectory(prefix="PhoneNAS-tls-ui-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.enterContext(patch.dict(os.environ, {"PHONENAS_DATA_DIR": str(self.root), "APPDATA": str(self.root)}))
        self.config = Config(self.root / "config.json")
        self.cert_file = self.root / "PhoneNAS-server.crt"
        self.cert_file.write_text(PHONE_CERT, encoding="ascii")
        self.endpoint = "https://127.0.0.1:18889"
        self.fingerprint = "AC:80:5E:FC:8A:6B:10:E2:7B:5E:53:40:66:92:41:F2:2F:7A:46:0B:2E:BC:1B:08:EB:E7:27:20:C4:3B:9F:C9"

    def _select(self, form, confirmed=True):
        from PySide6.QtWidgets import QMessageBox

        def answer(_parent, _title, message, _buttons, default):
            self.assertEqual(default, QMessageBox.StandardButton.No)
            self.assertIn(self.fingerprint, message)
            self.assertIn(self.endpoint, message)
            return QMessageBox.StandardButton.Yes if confirmed else QMessageBox.StandardButton.No

        with patch("app.ui.pages.settings.QFileDialog.getOpenFileName", return_value=(str(self.cert_file), "")), \
                patch("app.ui.pages.settings.QMessageBox.question", side_effect=answer) as modal:
            form.certificate_input.choose.click()
            modal.assert_called_once()

    def _wait(self, form):
        from PySide6.QtTest import QTest
        for _ in range(100):
            if not form.busy:
                return
            QTest.qWait(10)
        self.fail("settings worker did not finish")

    def test_settings_enrollment_requires_confirmation_and_tests_before_save(self):
        from app.ui.pages.settings import SettingsPage

        page = SettingsPage(self.config)
        self.addCleanup(page.deleteLater)
        page.url_edit.setText(self.endpoint)
        self._select(page, confirmed=False)
        self.assertNotIn(self.fingerprint, page.certificate_input.summary.text())
        self._select(page)
        self.assertIn(self.fingerprint, page.certificate_input.summary.text())
        received = []

        def observe(client, url, username, password, **_kwargs):
            received.append((url, client.config.trusted_certificate_for(url)))
            return Result(True, "", "", 0)

        with patch("app.ui.pages.settings.Rclone.test_connection", autospec=True, side_effect=observe):
            page.test_button.click()
            self._wait(page)
            self.assertEqual(received, [(self.endpoint, PHONE_CERT)])
            self.assertIn("连接成功", page.test_label.text())
            self.assertFalse(self.config.path.exists())
            self.assertEqual(self.config.trusted_certificate_for(self.endpoint), "")
            page.url_edit.setText(self.endpoint + "/other")
            page.test_button.click()
            self._wait(page)
            self.assertEqual(received[-1], (self.endpoint + "/other", ""))
            page.url_edit.setText(self.endpoint)
        with patch("app.ui.pages.settings.set_autostart", return_value=(True, "")):
            page.save_button.click()
            self._wait(page)
        self.assertEqual(Config(self.config.path).trusted_certificate_for(self.endpoint), PHONE_CERT)
        self.assertIn(self.fingerprint, page.certificate_input.summary.text())
        page.certificate_input.clear.click()
        with patch("app.ui.pages.settings.set_autostart", return_value=(True, "")):
            page.save_button.click()
            self._wait(page)
        self.assertEqual(Config(self.config.path).trusted_certificate_for(self.endpoint), "")

    def test_changing_only_tls_trust_restarts_an_active_mount_after_settings_save(self):
        from PySide6.QtTest import QTest
        from app.ui.main_window import MainWindow

        self.config.set("nas.url", self.endpoint)
        self.config.set_trusted_certificate(self.endpoint, PHONE_CERT)
        self.config.save()
        with patch("app.ui.main_window.MountSupervisor.start") as start, \
                patch("app.ui.main_window.winfsp.is_installed", return_value=True), \
                patch("app.ui.pages.settings.set_autostart", return_value=(True, "")):
            self.config.set("mount.auto_mount", False)
            window = MainWindow(self.config, operational=True)
            try:
                page = window.settings_page
                # Simulate an already-mounted process without touching WinFsp or a drive.
                window.mount._state = "mounted"
                page.certificate_input.clear.click()
                page.save_button.click()
                self._wait(page)
                self.assertEqual(Config(self.config.path).trusted_certificate_for(self.endpoint), "", page.notice_label.text())
                self.assertEqual(window.mount.state, "stopped")
                QTest.qWait(420)
                start.assert_called_once()
                self.assertEqual(Config(self.config.path).trusted_certificate_for(self.endpoint), "")
            finally:
                window.quit_app()
                window.deleteLater()
                self.app.processEvents()

    def test_wizard_can_save_an_explicit_first_run_certificate(self):
        from app.ui.wizard import WizardDialog

        wizard = WizardDialog(self.config)
        self.addCleanup(wizard.deleteLater)
        self.assertEqual(wizard.url_edit.text(), "")
        self.assertEqual(wizard.user_edit.text(), "")
        wizard.url_edit.setText(self.endpoint)
        wizard.mount_check.setChecked(False)
        self._select(wizard)
        with patch("app.ui.wizard.winfsp.is_installed", return_value=False):
            wizard.finish_button.click()
            self._wait(wizard)
        self.assertEqual(Config(self.config.path).trusted_certificate_for(self.endpoint), PHONE_CERT)


if __name__ == "__main__":
    unittest.main()
