from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
import os
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.core.config import Config
from app.core.rclone import Rclone, rclone_exe
from app.core.relay import DatasetCoordinator
from app.core.tasks import new_task


USER = "isolated-owner"
PASSWORD = "synthetic-tls-contract-test-only"
AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
DAV = b'''<?xml version="1.0" encoding="UTF-8"?>
<D:multistatus xmlns:D="DAV:"><D:response><D:href>/</D:href><D:propstat><D:prop>
<D:resourcetype><D:collection/></D:resourcetype></D:prop>
<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>'''


def make_leaf(root, *, ip="127.0.0.1", label="fixture", ca=False, expired=False):
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PhoneNAS fixture")])
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(days=2))
                   .not_valid_after(now + timedelta(days=-1 if expired else 1))
                   .add_extension(x509.BasicConstraints(ca, None), critical=True)
                   .add_extension(x509.KeyUsage(True, False, False, False, False,
                                               ca, False, False, False), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(IPv4Address(ip))]), critical=False)
                   .sign(key, hashes.SHA256()))
    public = certificate.public_bytes(serialization.Encoding.PEM)
    leaf = root / (label + ".pem")
    secret = root / (label + ".key")
    leaf.write_bytes(public)
    secret.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    return leaf, secret, public.decode(), sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()


@contextmanager
def dav_server(pair, dispatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def work(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            auth = self.headers.get("Authorization")
            # Keep only booleans; the trace must not retain credentials.
            seen.append((self.command, self.path, auth is not None, auth == AUTH, bool(data)))
            status, headers, body = dispatch(self.command, self.path, data, auth == AUTH)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        do_PROPFIND = do_GET = do_PUT = do_MKCOL = do_DELETE = work

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    try:
        if pair is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*pair[:2])
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"{'https' if pair else 'http'}://127.0.0.1:{server.server_port}", seen
        finally:
            server.shutdown()
            thread.join()
    finally:
        server.server_close()


def listing(method, path, data, authorised):
    if not authorised:
        return 401, {}, b""
    return (207, {"Content-Type": "application/xml"}, DAV) if method == "PROPFIND" else (200, {}, b"files")


class NasTLSTests(unittest.TestCase):
    def setUp(self):
        space = tempfile.TemporaryDirectory(prefix="PhoneNAS-pinned-test-")
        self.addCleanup(space.cleanup)
        self.root = Path(space.name)
        self.leaf = make_leaf(self.root)
        self.binary = rclone_exe()
        supplied = os.environ.get("PHONENAS_TEST_RCLONE")
        if supplied:
            self.binary = Path(supplied)
        self.addCleanup(patch.stopall)
        patch("app.core.rclone.rclone_exe", return_value=self.binary).start()
        patch("app.core.rclone.get_logger").start()

    def client(self, url, leaf=None, *, pinned=True):
        config = Config(self.root / "config.json")
        config.set("nas.url", url)
        config.set("nas.username", USER)
        config.password = PASSWORD
        if pinned:
            pair = leaf or self.leaf
            config.set("version", 4)
            config.set("nas.tls_certificate", pair[2])
            config.set("nas.tls_fingerprint", pair[3])
        return Rclone(config, self.root / "rclone.conf")

    def require_rclone(self):
        if not self.binary.is_file():
            self.skipTest("bundled Windows rclone is not installed")

    def test_pinned_rclone_and_control_use_the_same_leaf(self):
        self.require_rclone()
        owner = {}

        def dispatch(method, path, data, authorised):
            if not authorised:
                return 401, {}, b""
            if method == "MKCOL":
                return 201, {}, b""
            if method == "PUT":
                owner[path] = data
                return 201, {}, b""
            if method == "GET" and path.endswith("owner.json"):
                return 200, {}, owner[path]
            if method == "DELETE":
                owner.clear()
                return 204, {}, b""
            return listing(method, path, data, authorised)

        with dav_server(self.leaf, dispatch) as (url, seen):
            client = self.client(url)
            client.ensure_conf()
            result = client.run(["lsd", "phone:", "--retries", "1", "--low-level-retries", "1"])
            self.assertTrue(result.ok, "rclone did not trust the explicitly verified leaf")
            proc = client.popen(["lsd", "phone:", "--retries", "1", "--low-level-retries", "1"])
            try:
                proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                client.release(proc)
            with DatasetCoordinator(client, new_task(str(self.root / "work"))).lease():
                self.assertTrue(owner)
            self.assertFalse(owner)
        self.assertTrue(all(correct for _, _, _, correct, _ in seen))
        self.assertTrue(any(method == "DELETE" for method, *_ in seen))
        self.assertFalse((self.root / "rclone.conf").exists(), "pinned auth wrote a shared rclone profile")

    def test_mount_cache_survives_same_identity_but_is_not_reused_after_repair(self):
        self.require_rclone()
        url = "https://127.0.0.1:18889"
        new_leaf = make_leaf(self.root, label="repaired")
        client = self.client(url)

        def launch_cache():
            with patch("app.core.rclone.data_dir", return_value=self.root), patch(
                "app.core.rclone.subprocess.Popen"
            ) as launch:
                process = client.popen(["mount", "phone:", "W:", "--vfs-cache-mode", "writes"])
                try:
                    arguments = launch.call_args.args[0]
                    return Path(arguments[arguments.index("--cache-dir") + 1])
                finally:
                    client.release(process)

        cache = launch_cache()
        self.assertEqual(cache.parent, self.root / "paired-vfs")
        cache.mkdir(parents=True)
        (cache / "old-queued-write").write_bytes(b"keep isolated")
        self.assertEqual(launch_cache(), cache)
        client.config.set("nas.tls_certificate", new_leaf[2])
        client.config.set("nas.tls_fingerprint", new_leaf[3])
        self.assertNotEqual(launch_cache(), cache)
        self.assertEqual((cache / "old-queued-write").read_bytes(), b"keep isolated")

    def test_replace_leaf_rejects_before_any_http_on_both_clients(self):
        self.require_rclone()
        changed = make_leaf(self.root, label="replacement")
        with dav_server(changed, listing) as (url, seen):
            client = self.client(url)
            client.ensure_conf()
            result = client.run(["lsd", "phone:", "--retries", "1", "--low-level-retries", "1"], timeout=15)
            self.assertFalse(result.ok)
            lease = DatasetCoordinator(client, new_task(str(self.root / "work")))._new_lease()
            with self.assertRaises(RuntimeError):
                lease._request("GET", url + "/")
            self.assertEqual(seen, [])

    def test_credentials_do_not_follow_redirect_to_other_port_or_http(self):
        self.require_rclone()
        for transport in (self.leaf, None):
            with self.subTest(transport="https" if transport else "http"):
                with dav_server(transport, listing) as (other, destination):
                    def redirect(method, path, data, authorised):
                        return 307, {"Location": other + "/redirected"}, b""

                    with dav_server(self.leaf, redirect) as (url, source):
                        client = self.client(url)
                        with patch.dict(os.environ, {"RCLONE_WEBDAV_AUTH_REDIRECT": "true",
                                                    "RCLONE_ALLOW_HTTPS_DOWNGRADE": "true",
                                                    "HTTP_PROXY": "http://127.0.0.1:1"}):
                            client.run(["lsd", "phone:", "--retries", "1", "--low-level-retries", "1"], timeout=15)
                        self.assertTrue(source and source[0][3], "the verified source was not reached")
                        self.assertFalse(any(item[2] for item in destination))
                        if transport is None:
                            self.assertEqual(destination, [])
                        lease = DatasetCoordinator(client, new_task(str(self.root / "work")))._new_lease()
                        before = len(destination)
                        self.assertEqual(lease._request("GET", url + "/")[0], 307)
                        self.assertEqual(len(destination), before)

    def test_expired_leaf_can_be_loaded_but_cannot_connect(self):
        self.require_rclone()
        pair = make_leaf(self.root, label="expired", expired=True)
        with dav_server(pair, listing) as (url, seen):
            client = self.client(url, pair)
            client.config.save()
            saved = Config(client.config.path)
            self.assertEqual(saved.load_error, "")
            result = client.run(["lsd", "phone:"])
            self.assertFalse(result.ok)
            self.assertIn("过期", result.err)
            self.assertEqual(seen, [])

    def test_invalid_leaf_or_fingerprint_is_rejected_before_launch(self):
        for name, pair in (
            ("wrong-ip", make_leaf(self.root, ip="127.0.0.2", label="ip")),
            ("ca", make_leaf(self.root, label="ca", ca=True)),
            ("wrong-fingerprint", (*self.leaf[:3], "0" * 64)),
            ("more-than-one-certificate", (*self.leaf[:2], self.leaf[2] * 2, self.leaf[3])),
        ):
            with self.subTest(case=name):
                client = self.client("https://127.0.0.1:18889", pair)
                with self.assertRaises(ValueError):
                    client.config.save()


if __name__ == "__main__":
    unittest.main()
