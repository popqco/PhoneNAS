"""Finite, opt-in Android acceptance against a pinned Tailnet TLS listener.

No ADB, discovery, watchdog, default desktop config, proxy or insecure TLS.
Default credentials: {"username": "...", "password": "...", "root": DEMO_ROOT}.
Public acceptance credentials additionally require scope=isolated-public-acceptance,
root=/storage/emulated/0/PhoneNAS-Acceptance-<uuid>/rw and readonly_root with /ro
under the SAME UUID. Supply BOTH fixed acceptance aliases on the command line.
Fixtures must already exist; this script never prepares the phone or its shares.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import ssl
import time
from pathlib import Path
import re
import socket
import stat
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote, unquote
import xml.etree.ElementTree as ET


FAILURE_CODES = frozenset({
    "UNSAFE_ENDPOINT", "UNSAFE_CREDENTIALS", "UNSAFE_SCOPE", "UNSAFE_LOCAL_PATH",
    "CERTIFICATE_INVALID", "FINGERPRINT_INVALID", "FINGERPRINT_MISMATCH", "SAN_IP_MISMATCH",
    "REPORT_UNAVAILABLE", "REPORT_IO", "UNSAFE_REQUEST", "TLS_VERIFICATION", "TLS_PEER_PIN",
    "TRANSPORT", "RESPONSE_LIMIT", "REDIRECT_REFUSED", "CONTRACT", "FIXTURE_MISMATCH",
    "OWNER_UNPROVEN", "LOCK_RACE", "INTERRUPTED_SUCCESS", "INTERRUPT_UNCONFIRMED",
    "RELAY_UNAVAILABLE", "RELAY_FAILED", "RELAY_TIMEOUT", "INTERRUPTED", "INTERNAL",
})


class SafetyError(Exception):
    def __init__(self, code: str):
        self.code = code if type(code) is str and code in FAILURE_CODES else "INTERNAL"
        super().__init__(self.code)


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int = 18889

    @property
    def url(self) -> str:
        return f"https://{self.host}:{self.port}"


def validate_endpoint(url: str) -> Endpoint:
    # Parse nothing until the complete raw spelling is canonical. urlsplit strips
    # tabs/newlines, accepts empty ?/#, aliases of numeric hosts and userinfo.
    match = re.fullmatch(r"https://(100\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}):18889", url) if type(url) is str else None
    if match is None:
        raise SafetyError("UNSAFE_ENDPOINT")
    try:
        address = ipaddress.IPv4Address(match[1])
    except ipaddress.AddressValueError:
        raise SafetyError("UNSAFE_ENDPOINT") from None
    if address not in ipaddress.IPv4Network("100.64.0.0/10") or str(address) != match[1]:
        raise SafetyError("UNSAFE_ENDPOINT")
    return Endpoint(str(address))


DEMO_ROOT = "/storage/emulated/0/Android/data/com.phonenas.server.probe/files/contract-test"
PUBLIC_SCOPE = "isolated-public-acceptance"


def local_file(path: Path) -> Path:
    """No UNC, device namespaces, ADS, links/junctions, or implicitly remote IO."""
    raw = os.fspath(path)
    if (not raw or raw.replace("\\", "/").startswith("//")
            or any(ord(char) < 32 for char in raw)):
        raise SafetyError("UNSAFE_LOCAL_PATH")
    tail = raw[2:] if re.match(r"^[a-zA-Z]:", raw) else raw
    if ":" in tail or any(part in (".", "..") for part in re.split(r"[/\\]", tail)):
        raise SafetyError("UNSAFE_LOCAL_PATH")
    absolute = Path(os.path.abspath(raw))
    for ancestor in reversed((absolute, *absolute.parents)):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise SafetyError("UNSAFE_LOCAL_PATH")
    return absolute


def read_limited(path: Path, limit: int) -> bytes:
    checked = local_file(path)
    with checked.open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SafetyError("UNSAFE_LOCAL_PATH")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise SafetyError("UNSAFE_CREDENTIALS")
    return raw


def unique_json(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SafetyError("UNSAFE_CREDENTIALS")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(SafetyError("UNSAFE_CREDENTIALS")))


@dataclass(frozen=True)
class Credentials:
    username: str = field(repr=False)
    password: str = field(repr=False)
    share_prefix: str
    readonly_prefix: str
    scope: str
    storage_profile: str = "default"


def load_credentials(path: Path, *, share_prefix: str | None = None,
                     readonly_prefix: str | None = None, storage_profile: str = "default") -> Credentials:
    if storage_profile not in ("default", "android-public"):
        raise SafetyError("UNSAFE_SCOPE")
    try:
        data = unique_json(read_limited(path, 16 * 1024))
        if type(data) is not dict or not {"username", "password", "root"}.issubset(data):
            raise SafetyError("UNSAFE_CREDENTIALS")
        if set(data) - {"username", "password", "root", "readonly_root", "scope"}:
            raise SafetyError("UNSAFE_CREDENTIALS")
        username, password = data["username"], data["password"]
        if (type(username) is not str or type(password) is not str or not username
                or ":" in username or len(username.encode("utf-8")) > 128
                or not 20 <= len(password.encode("utf-8")) <= 2048
                or any(ord(char) < 32 or ord(char) == 127 for char in username + password)):
            raise SafetyError("UNSAFE_CREDENTIALS")
        if share_prefix is None and readonly_prefix is None:
            if (storage_profile != "default" or data["root"] != DEMO_ROOT or "readonly_root" in data
                    or data.get("scope", "isolated-app-demo") != "isolated-app-demo"):
                raise SafetyError("UNSAFE_SCOPE")
            return Credentials(username, password, "/test/", "/readonly/", "isolated-app-demo")
        if (share_prefix != "/acceptance_rw/" or readonly_prefix != "/acceptance_ro/"
                or data.get("scope") != PUBLIC_SCOPE or type(data["root"]) is not str):
            raise SafetyError("UNSAFE_SCOPE")
        match = re.fullmatch(r"(/storage/emulated/0/PhoneNAS-Acceptance-([0-9a-f-]{32,36}))/rw", data["root"])
        if match is None or data.get("readonly_root") != match[1] + "/ro":
            raise SafetyError("UNSAFE_SCOPE")
        identity = uuid.UUID(match[2])
        if match[2] not in (identity.hex, str(identity)) or identity.int == 0:
            raise SafetyError("UNSAFE_SCOPE")
        return Credentials(username, password, share_prefix, readonly_prefix, PUBLIC_SCOPE, storage_profile)
    except SafetyError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SafetyError("UNSAFE_CREDENTIALS") from None


@dataclass(frozen=True)
class Trust:
    context: ssl.SSLContext = field(repr=False)
    fingerprint: str
    pem: str = field(repr=False)


def load_certificate(path: Path, fingerprint: str, endpoint: Endpoint) -> Trust:
    if type(fingerprint) is not str or re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint) is None:
        raise SafetyError("FINGERPRINT_INVALID")
    try:
        text = read_limited(path, 32 * 1024).decode("ascii").strip()
        if re.fullmatch(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----", text) is None:
            raise SafetyError("CERTIFICATE_INVALID")
        der = ssl.PEM_cert_to_DER_cert(text)
        actual = hashlib.sha256(der).hexdigest()
        if not hmac.compare_digest(actual, fingerprint.lower()):
            raise SafetyError("FINGERPRINT_MISMATCH")
        # CPython 3.14's decoder is offline. Decode a private copy of the bytes
        # already hashed, not a path an external process could replace mid-check.
        with tempfile.TemporaryDirectory(prefix="PhoneNAS-cert-check-") as directory:
            copy = Path(directory) / "public.pem"
            copy.write_text(text + "\n", encoding="ascii")
            decoded = ssl._ssl._test_decode_cert(str(copy))
        if ("IP Address", endpoint.host) not in decoded.get("subjectAltName", ()):
            raise SafetyError("SAN_IP_MISMATCH")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.hostname_checks_common_name = False
        # Android identity is a self-signed end-entity, not a CA. Treat exactly
        # this cert as an explicit trust anchor; retain signature/time/IP checks.
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        context.load_verify_locations(cadata=text)
        if context.cert_store_stats()["x509"] != 1:
            raise SafetyError("CERTIFICATE_INVALID")
        return Trust(context, actual, text + "\n")
    except SafetyError:
        raise
    except (OSError, ValueError, UnicodeError, ssl.SSLError):
        raise SafetyError("CERTIFICATE_INVALID") from None


CHECK_NAMES = frozenset({
    "preflight", "tls_fixture", "prepare", "authentication", "files_range_and_names",
    "exclusive_lock", "interrupted_uploads", "readonly", "move_overwrite", "path_policy",
    "large_content", "cleanup", "relay", "run", "public_question_mark_rejected",
})


def failure_code(error: BaseException) -> str:
    if isinstance(error, SafetyError):
        return error.code
    if isinstance(error, ssl.SSLCertVerificationError):
        return "TLS_VERIFICATION"
    if isinstance(error, (ssl.SSLError, OSError, http.client.HTTPException)):
        return "TRANSPORT"
    if isinstance(error, AssertionError):
        return "CONTRACT"
    if isinstance(error, KeyboardInterrupt):
        return "INTERRUPTED"
    return "INTERNAL"


class ExclusiveReport:
    """Reserve + persist RUNNING before callers may construct a network client.

    Report fields are generated here, never arbitrary exception/server strings.
    Holding the original file handle also avoids following a replaced path.
    """
    def __init__(self, path: Path, *, storage_profile: str = "default"):
        if storage_profile not in ("default", "android-public"):
            raise SafetyError("UNSAFE_SCOPE")
        self.storage_profile = storage_profile
        self.path = local_file(path)
        self.stream = None
        self.started = time.monotonic()
        self.run_id = uuid.uuid4().hex
        self.checks = []
        self.finished = False

    def __enter__(self):
        try:
            self.stream = self.path.open("x+", encoding="utf-8", newline="\n")
            self.persist("RUNNING")
        except (OSError, ValueError):
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            raise SafetyError("REPORT_UNAVAILABLE") from None
        return self

    @property
    def reserved(self):
        return self.stream is not None and not self.stream.closed and not self.finished

    def require_reserved(self):
        if not self.reserved:
            raise SafetyError("REPORT_UNAVAILABLE")

    def persist(self, status):
        payload = {"status": status, "safeUUID": self.run_id,
                   "storageProfile": self.storage_profile,
                   "filenameContract": ("android-public-capability-not-full-filename-contract"
                                        if self.storage_profile == "android-public" else "original-native-eight-checks"),
                   "elapsed_ms": round((time.monotonic() - self.started) * 1000, 1),
                   "checks": self.checks}
        try:
            self.stream.seek(0)
            json.dump(payload, self.stream, ensure_ascii=True, separators=(",", ":"))
            self.stream.write("\n")
            self.stream.truncate()
            self.stream.flush()
            os.fsync(self.stream.fileno())
        except (OSError, ValueError):
            raise SafetyError("REPORT_IO") from None

    def record(self, name, status, elapsed_ms, *, code=None, sha256=None, http=None):
        self.require_reserved()
        if name not in CHECK_NAMES or status not in ("PASS", "FAIL", "SKIPPED"):
            raise SafetyError("INTERNAL")
        entry = {"check": name, "status": status, "elapsed_ms": round(max(0, elapsed_ms), 1)}
        if code is not None:
            entry["failureCode"] = code if code in FAILURE_CODES else "INTERNAL"
        if sha256 is not None:
            if type(sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
                raise SafetyError("INTERNAL")
            entry["sha256"] = sha256
        if http is not None:
            if type(http) is not int or not 100 <= http <= 599:
                raise SafetyError("INTERNAL")
            entry["httpStatus"] = http
        self.checks.append(entry)
        self.persist("RUNNING")

    def check(self, name, action):
        self.require_reserved()
        started = time.monotonic()
        try:
            value = action()
        except Exception as error:
            self.record(name, "FAIL", (time.monotonic() - started) * 1000, code=failure_code(error))
            return False
        self.record(name, "PASS", (time.monotonic() - started) * 1000,
                    sha256=value if type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) else None)
        return True

    def finish(self):
        passed = bool(self.checks) and all(item["status"] == "PASS" for item in self.checks)
        self.persist("PASS" if passed else "FAIL")
        self.finished = True
        return passed

    def __exit__(self, exc_type, exc, traceback):
        try:
            if not self.finished:
                self.record("run", "FAIL", 0, code=failure_code(exc) if exc is not None else "INTERNAL")
                self.finish()
        finally:
            self.stream.close()
        return False


METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PROPFIND", "PUT", "MKCOL", "DELETE", "MOVE",
                     "COPY", "POST", "PATCH", "PROPPATCH", "LOCK", "UNLOCK"})
READS = frozenset({"GET", "HEAD", "OPTIONS", "PROPFIND"})
MAX_RESPONSE = 3 * 1024 * 1024


def require(condition, code="CONTRACT"):
    if not condition:
        raise SafetyError(code)


def canonical_target(path: str) -> bool:
    if (type(path) is not str or not path.startswith("/") or path.startswith("//")
            or any(ord(char) < 33 or ord(char) > 126 for char in path)
            or any(char in path for char in "?#\\")
            or re.search(r"%(?![0-9a-fA-F]{2})", path)
            or re.search(r"%(?:2f|5c)", path, re.IGNORECASE)):
        return False
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeError:
        return False
    return (all(part not in ("", ".", "..") for part in decoded.strip("/").split("/"))
            and not any(ord(char) < 32 or char == "\\" for char in decoded)
            and "//" not in decoded)


class TLSClient:
    """Direct, no-proxy HTTPS; never emits server headers, bodies, or credentials.

    Each connection verifies the supplied trust anchor + IP, then pins the actual
    peer DER *before* sending Authorization. Redirects are never followed.
    """
    def __init__(self, endpoint: Endpoint, credentials: Credentials, trust: Trust, report: ExclusiveReport):
        report.require_reserved()
        self.endpoint = validate_endpoint(endpoint.url)
        self.credentials = credentials
        self.trust = trust
        self.report = report
        self.prefix = credentials.share_prefix + "contract-" + report.run_id
        self.lock = "/.phonenas/locks/" + uuid.uuid4().hex
        self.owner = json.dumps({"safeUUID": report.run_id, "token": uuid.uuid4().hex},
                                separators=(",", ":")).encode("ascii")
        self.owns_tree = self.owns_lock = False
        self.probe_body = ("PhoneNAS acceptance " + report.run_id).encode("ascii")

    def file(self, name):
        require(type(name) is str and name not in ("", ".", "..") and "/" not in name and "\\" not in name,
                "UNSAFE_REQUEST")
        return self.prefix + "/" + quote(name, safe="")

    def destination(self, path):
        require(canonical_target(path), "UNSAFE_REQUEST")
        return self.endpoint.url + path

    def validate_request(self, method, path, headers, *, readonly_probe=False):
        self.report.require_reserved()
        require(method in METHODS and canonical_target(path), "UNSAFE_REQUEST")
        own_tree = path == self.prefix or path.startswith(self.prefix + "/")
        own_lock = path == self.lock or path.startswith(self.lock + "/")
        fixtures = (self.credentials.share_prefix + "readme.txt", self.credentials.readonly_prefix + "readme.txt")
        if not own_tree and not own_lock:
            if path in ("/.phonenas/", "/.phonenas/locks/"):
                require(method == "MKCOL", "UNSAFE_REQUEST")
            elif readonly_probe:
                ro = self.credentials.readonly_prefix
                require(path == ro + "readme.txt" or path == ro + "contract-" + self.report.run_id,
                        "UNSAFE_REQUEST")
            else:
                require(method in READS and path in fixtures, "UNSAFE_REQUEST")
        allowed = {"depth", "range", "destination", "overwrite", "if-none-match", "content-type"}
        for key, value in headers.items():
            require(type(key) is str and key.lower() in allowed and type(value) is str
                    and not any(ord(char) < 32 or ord(char) > 126 for char in value), "UNSAFE_REQUEST")
        destination = next((value for key, value in headers.items() if key.lower() == "destination"), None)
        if method in ("MOVE", "COPY"):
            require(destination is not None and destination.startswith(self.endpoint.url + "/"), "UNSAFE_REQUEST")
            target = destination[len(self.endpoint.url):]
            require(canonical_target(target), "UNSAFE_REQUEST")
            require(target.startswith(self.prefix + "/") or (
                readonly_probe and target == self.credentials.readonly_prefix + "contract-" + self.report.run_id
            ), "UNSAFE_REQUEST")

    def _connection(self):
        self.report.require_reserved()
        connection = http.client.HTTPSConnection(self.endpoint.host, self.endpoint.port,
                                                  timeout=15, context=self.trust.context)
        try:
            connection.connect()
            peer = connection.sock.getpeercert(binary_form=True)
            require(hmac.compare_digest(hashlib.sha256(peer).hexdigest(), self.trust.fingerprint), "TLS_PEER_PIN")
            return connection
        except BaseException:
            connection.close()
            raise

    def request(self, method, path, body=None, headers=None, *, authenticated=True,
                wrong_password=False, readonly_probe=False):
        supplied = dict(headers or {})
        self.validate_request(method, path, supplied, readonly_probe=readonly_probe)
        require(body is None or type(body) is bytes and len(body) <= 2 * 1024 * 1024, "UNSAFE_REQUEST")
        if authenticated:
            password = self.credentials.password + "-wrong-" + self.report.run_id if wrong_password else self.credentials.password
            supplied["Authorization"] = "Basic " + base64.b64encode(
                (self.credentials.username + ":" + password).encode("utf-8")).decode("ascii")
        supplied["Connection"] = "close"
        connection = self._connection()
        try:
            connection.request(method, path, body, supplied)
            response = connection.getresponse()
            require(not 300 <= response.status < 400, "REDIRECT_REFUSED")
            raw = response.read(MAX_RESPONSE + 1)
            require(len(raw) <= MAX_RESPONSE, "RESPONSE_LIMIT")
            return response.status, {key.lower(): value for key, value in response.getheaders()}, raw
        finally:
            connection.close()

    def prepare(self):
        require(self.request("MKCOL", self.prefix + "/")[0] == 201, "OWNER_UNPROVEN")
        require(self.request("PUT", self.file("owner.json"), self.owner,
                             {"If-None-Match": "*"})[0] == 201, "OWNER_UNPROVEN")
        status, _, body = self.request("GET", self.file("owner.json"))
        require(status == 200 and body == self.owner, "OWNER_UNPROVEN")
        self.owns_tree = True

    def cleanup(self):
        # Never DELETE a parent, preexisting UUID, or an unproved acquisition.
        for owned, path in (("owns_lock", self.lock), ("owns_tree", self.prefix)):
            if not getattr(self, owned):
                continue
            status, _, body = self.request("GET", path + "/owner.json")
            require(status == 200 and body == self.owner, "OWNER_UNPROVEN")
            require(self.request("DELETE", path + "/")[0] in (200, 204), "OWNER_UNPROVEN")
            setattr(self, owned, False)

    def truncated_put(self, path):
        """Send TLS close_notify, not SSLSocket.shutdown (which exposes ciphertext).

        MemoryBIO lets us close the TLS write direction while retaining the TLS
        reader for an HTTP failure. A missing acknowledgement is NEVER success;
        caller must additionally verify the old file and absence of a half-file.
        """
        self.validate_request("PUT", path, {})
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = self.trust.context.wrap_bio(incoming, outgoing, server_side=False,
                                           server_hostname=self.endpoint.host)
        with socket.create_connection((self.endpoint.host, self.endpoint.port), timeout=15) as wire:
            deadline = time.monotonic() + 20

            def flush():
                while outgoing.pending:
                    wire.sendall(outgoing.read())

            def feed():
                require(time.monotonic() < deadline, "INTERRUPT_UNCONFIRMED")
                wire.settimeout(max(0.1, min(5, deadline - time.monotonic())))
                flush()
                data = wire.recv(65536)
                if not data:
                    incoming.write_eof()
                    return False
                incoming.write(data)
                return True

            while True:
                try:
                    tls.do_handshake()
                    flush()
                    break
                except ssl.SSLWantReadError:
                    require(feed(), "TLS_VERIFICATION")
            require(hmac.compare_digest(hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest(),
                                        self.trust.fingerprint), "TLS_PEER_PIN")
            authorization = "Basic " + base64.b64encode(
                (self.credentials.username + ":" + self.credentials.password).encode("utf-8")).decode("ascii")
            message = (f"PUT {path} HTTP/1.1\r\nHost: {self.endpoint.host}:{self.endpoint.port}\r\n"
                       f"Authorization: {authorization}\r\nContent-Length: 1048576\r\n"
                       "Connection: close\r\n\r\n").encode("ascii") + b"incomplete-upload"
            offset = 0
            while offset < len(message):
                try:
                    offset += tls.write(message[offset:])
                    flush()
                except ssl.SSLWantReadError:
                    require(feed(), "INTERRUPT_UNCONFIRMED")
            try:
                tls.unwrap()  # sends close_notify; response may arrive before peer notify
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                pass
            flush()
            wire.shutdown(socket.SHUT_WR)  # FIN only AFTER TLS close_notify was flushed
            plain = bytearray()
            while len(plain) <= 65536:
                try:
                    data = tls.read(65536 - len(plain) + 1)
                    if not data:
                        break
                    plain.extend(data)
                except ssl.SSLWantReadError:
                    try:
                        if not feed():
                            break
                    except (TimeoutError, ConnectionError):
                        break
                except (ssl.SSLZeroReturnError, ssl.SSLEOFError, ConnectionError):
                    break
            require(len(plain) <= 65536, "RESPONSE_LIMIT")
            # Reject a success line anywhere, including behind an interim reply.
            statuses = re.findall(rb"(?:\A|\r\n)HTTP/1\.[01] ([0-9]{3})(?: |\r)", bytes(plain))
            require(not any(200 <= int(status) < 400 for status in statuses), "INTERRUPTED_SUCCESS")
            if plain:
                require(bool(statuses) and all(100 <= int(status) < 200 or 400 <= int(status) <= 599
                                               for status in statuses), "INTERRUPT_UNCONFIRMED")

    def tls_fixture(self):
        for prefix, kind in ((self.credentials.share_prefix, "test"), (self.credentials.readonly_prefix, "readonly")):
            expected = f"PhoneNAS isolated {kind} demo. No personal folders are exported.\n".encode("ascii")
            status, _, body = self.request("GET", prefix + "readme.txt")
            require(status == 200 and body == expected, "FIXTURE_MISMATCH")
        return self.trust.fingerprint


def authentication(client):
    secret = client.file("authentication-secret.bin")
    require(client.request("PUT", secret, client.probe_body)[0] == 201)
    missing = client.file("authentication-missing-" + client.report.run_id)
    for authenticated, wrong in ((False, False), (True, True)):
        for method in sorted(METHODS):
            samples = []
            for path in (secret, missing):
                headers = {"Depth": "0"} if method == "PROPFIND" else {}
                if method in ("MOVE", "COPY"):
                    headers.update({"Destination": client.destination(client.file("auth-target")), "Overwrite": "F"})
                status, response_headers, body = client.request(
                    method, path, b"forbidden" if method in ("PUT", "POST", "PATCH") else None,
                    headers, authenticated=authenticated, wrong_password=wrong)
                require(status == 401 and bool(response_headers.get("www-authenticate")))
                joined = body + json.dumps(response_headers, ensure_ascii=True).encode("utf-8")
                for token in (client.probe_body, client.credentials.password.encode("utf-8"), b"/storage/emulated/", secret.encode("ascii")):
                    require(token not in joined)
                samples.append((body, {key: value for key, value in response_headers.items()
                                      if key in ("content-length", "content-type", "www-authenticate", "etag", "last-modified")}))
            require(samples[0] == samples[1])  # existing vs absent must not reveal existence
    require(client.request("GET", secret)[2] == client.probe_body)
    require(client.request("GET", missing)[0] == 404)


def files_range_and_names(client):
    contents = b"0123456789abcdef"
    # No fallback on a failed PUT: only the explicitly requested public profile
    # changes the capability being asserted. Default retains the original name.
    path = client.file("中文 空格%#.bin" if client.credentials.storage_profile == "android-public"
                       else "中文 空格%#?.bin")
    require(client.request("PUT", path, contents)[0] == 201)
    status, _, body = client.request("GET", path)
    require(status == 200 and body == contents)
    status, headers, body = client.request("GET", path, headers={"Range": "bytes=3-7"})
    require(status == 206 and body == b"34567" and headers.get("content-range") == "bytes 3-7/16")
    status, headers, body = client.request("HEAD", path)
    require(status == 200 and body == b"" and headers.get("content-length") == "16")
    status, _, xml = client.request("PROPFIND", client.prefix + "/", headers={"Depth": "1"})
    require(status == 207)
    hrefs = [element.text for element in ET.fromstring(xml).iter("{DAV:}href")]
    require(path in hrefs)
    moved = client.file("历史文件.bin")
    require(client.request("MOVE", path, headers={"Destination": client.destination(moved), "Overwrite": "F"})[0] == 201)
    require(client.request("GET", moved)[2] == contents and client.request("GET", path)[0] == 404)


def exclusive_lock(client):
    for parent in ("/.phonenas/", "/.phonenas/locks/"):
        require(client.request("MKCOL", parent)[0] in (201, 405))
    barrier = threading.Barrier(8)

    def acquire(_):
        barrier.wait(timeout=20)
        return client.request("MKCOL", client.lock + "/")[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(acquire, index) for index in range(8)]
        statuses = []
        for future in futures:
            try:
                statuses.append(future.result(timeout=35))
            except Exception:
                statuses.append(0)
    if statuses.count(201) == 1:
        require(client.request("PUT", client.lock + "/owner.json", client.owner,
                               {"If-None-Match": "*"})[0] == 201, "OWNER_UNPROVEN")
        status, _, body = client.request("GET", client.lock + "/owner.json")
        require(status == 200 and body == client.owner, "OWNER_UNPROVEN")
        client.owns_lock = True
    require(statuses.count(201) == 1 and statuses.count(405) == 7, "LOCK_RACE")
    require(client.request("MKCOL", client.lock + "/")[0] == 405)
    require(client.request("GET", client.lock + "/owner.json")[2] == client.owner)
    require(client.request("DELETE", client.lock + "/")[0] in (200, 204))
    client.owns_lock = False


def interrupted_uploads(client):
    old = b"previous-complete-file"
    path = client.file("complete.bin")
    require(client.request("PUT", path, old)[0] == 201)
    failure = None
    try:
        client.truncated_put(path)
    except Exception as error:
        failure = error
    status, _, after = client.request("GET", path)  # required even if half-close raised
    require(status == 200 and after == old)
    if failure is not None:
        raise failure
    new = client.file("unfinished.bin")
    try:
        client.truncated_put(new)
    except Exception as error:
        failure = error
    require(client.request("GET", new)[0] == 404)
    status, _, xml = client.request("PROPFIND", client.prefix + "/", headers={"Depth": "1"})
    require(status == 207 and b".phonenas-temp-" not in xml)
    if failure is not None:
        raise failure
    return hashlib.sha256(after).hexdigest()


def readonly(client):
    path = client.credentials.readonly_prefix + "readme.txt"
    status, _, original = client.request("GET", path)
    require(status == 200 and original)
    target = client.credentials.readonly_prefix + "contract-" + client.report.run_id
    for method in sorted(METHODS - READS):
        headers = {"Destination": client.destination(target), "Overwrite": "T"} if method in ("MOVE", "COPY") else {}
        candidate = target if method == "MKCOL" else path
        status = client.request(method, candidate, b"forbidden" if method in ("PUT", "POST", "PATCH") else None,
                                headers, readonly_probe=True)[0]
        require(status == 403)
        after_status, _, after = client.request("GET", path)
        require(after_status == 200 and after == original)
    require(client.request("GET", target, readonly_probe=True)[0] == 404)
    return hashlib.sha256(original).hexdigest()


def move_overwrite(client):
    source, target = client.file("source.bin"), client.file("target.bin")
    for path, contents in ((source, b"new-complete"), (target, b"old-complete")):
        require(client.request("PUT", path, contents)[0] == 201)
    # Absolute HTTPS Destination specifically exercises the server's r.TLS path.
    destination = client.destination(target)
    require(client.request("MOVE", source, headers={"Destination": destination, "Overwrite": "F"})[0] == 412)
    require(client.request("GET", target)[2] == b"old-complete")
    require(client.request("GET", source)[2] == b"new-complete")
    require(client.request("MOVE", source, headers={"Destination": destination, "Overwrite": "T"})[0] == 204)
    require(client.request("GET", target)[2] == b"new-complete" and client.request("GET", source)[0] == 404)


def path_policy(client):
    # Do not send a traversal outside our UUID even to a buggy server. Reserved
    # components stay inside scratch if confinement is broken and are cleaned
    # only under this run's verified owner. Production/share roots are untouched.
    for suffix in (".phonenas/alias", ".phonenas-temp-hidden"):
        require(client.request("PUT", client.prefix + "/" + suffix, b"no")[0] in (400, 403, 404))
    encoded = client.file("literal%2e%2e.bin")
    require(client.request("PUT", encoded, b"literal-encoded-name")[0] == 201)
    require(client.request("GET", encoded)[2] == b"literal-encoded-name")


def large_content(client):
    contents = bytes(range(256)) * 8192
    path = client.file("two-megabyte.bin")
    require(client.request("PUT", path, contents)[0] == 201)
    status, _, readback = client.request("GET", path)
    digest = hashlib.sha256(readback).hexdigest()
    require(status == 200 and len(readback) == 2097152
            and digest == "91d3beb88a9b2f778a6c44a1c53b63d3c79931845a9aef84b3fb414610bd1938")
    return digest


def public_question_mark_rejected(client):
    """Explicit Android public-FUSE capability, never a filename workaround.

    The only attempted write is the literal question-mark name in our scratch.
    Snapshot hrefs before/after to reject an exported temp or renamed alias, and
    read both fixtures again even if the server wrongly accepted the PUT.
    """
    require(client.credentials.scope == PUBLIC_SCOPE
            and client.credentials.storage_profile == "android-public", "UNSAFE_SCOPE")
    fixtures = (client.credentials.share_prefix + "readme.txt", client.credentials.readonly_prefix + "readme.txt")
    originals = []
    for fixture in fixtures:
        status, _, body = client.request("GET", fixture)
        require(status == 200 and body, "FIXTURE_MISMATCH")
        originals.append(body)
    status, _, before = client.request("PROPFIND", client.prefix + "/", headers={"Depth": "1"})
    require(status == 207)
    before_hrefs = sorted(element.text or "" for element in ET.fromstring(before).iter("{DAV:}href"))
    path = client.file("question?.bin")
    put_status = client.request("PUT", path, b"public-question-mark-must-not-publish")[0]
    get_status = client.request("GET", path)[0]
    listing_status, _, after = client.request("PROPFIND", client.prefix + "/", headers={"Depth": "1"})
    fixture_unchanged = True
    for fixture, original in zip(fixtures, originals):
        status, _, body = client.request("GET", fixture)
        fixture_unchanged = fixture_unchanged and status == 200 and body == original
    require(put_status == 403 and get_status == 404)
    require(listing_status == 207 and b".phonenas-temp-" not in after)
    after_hrefs = sorted(element.text or "" for element in ET.fromstring(after).iter("{DAV:}href"))
    require(before_hrefs == after_hrefs)
    require(fixture_unchanged, "FIXTURE_MISMATCH")


NATIVE_CHECKS = (authentication, files_range_and_names, exclusive_lock, interrupted_uploads,
                 readonly, move_overwrite, path_policy, large_content)


def checks_for_profile(credentials):
    require(credentials.storage_profile in ("default", "android-public"), "UNSAFE_SCOPE")
    if credentials.storage_profile == "android-public":
        require(credentials.scope == PUBLIC_SCOPE, "UNSAFE_SCOPE")
        return NATIVE_CHECKS + (public_question_mark_rejected,)
    return NATIVE_CHECKS


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes hostile argument values (possibly credentials).
        raise SafetyError("UNSAFE_REQUEST")


def main(argv=None):
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--url", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--fingerprint", required=True, help="Exact SHA256 hex from the phone, not the network")
    parser.add_argument("--report", type=Path, required=True, help="New exclusive local report; parent must exist")
    parser.add_argument("--share-prefix", help="Public mode only: /acceptance_rw/")
    parser.add_argument("--readonly-prefix", help="Public mode only: /acceptance_ro/")
    parser.add_argument("--storage-profile", choices=("android-public",), default="default",
                        help="Explicit public-only capability profile; rejects ?, not a full filename contract")
    try:
        args = parser.parse_args(argv)
        # Reserve before reading credentials, constructing clients or any IO to a
        # peer. Existing/inaccessible reports cannot result in one network call.
        with ExclusiveReport(args.report, storage_profile=args.storage_profile) as report:
            client = None
            try:
                endpoint = validate_endpoint(args.url)
                credentials = load_credentials(args.credentials, share_prefix=args.share_prefix,
                                               readonly_prefix=args.readonly_prefix, storage_profile=args.storage_profile)
                trust = load_certificate(args.certificate, args.fingerprint, endpoint)
                report.record("preflight", "PASS", 0, sha256=trust.fingerprint)
                client = TLSClient(endpoint, credentials, trust, report)
                if report.check("tls_fixture", client.tls_fixture) and report.check("prepare", client.prepare):
                    for check in checks_for_profile(credentials):
                        report.check(check.__name__, lambda check=check: check(client))
            except (Exception, KeyboardInterrupt) as error:
                report.record("preflight" if client is None else "run", "FAIL", 0, code=failure_code(error))
            finally:
                if client is not None:
                    report.check("cleanup", client.cleanup)
            passed = report.finish()
        print(json.dumps({"status": "PASS" if passed else "FAIL", "safeUUID": report.run_id,
                          "storageProfile": report.storage_profile}, separators=(",", ":")))
        return 0 if passed else 1
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({"status": "FAIL", "failureCode": failure_code(error)}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
