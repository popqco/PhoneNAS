from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import select
import socket
import socketserver
import ssl
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import SplitResult, urlsplit


_CERT_LIMIT = 64 * 1024
_PEM = re.compile(r"\s*-----BEGIN CERTIFICATE-----\s+([a-zA-Z0-9+/=\r\n]+)\s+-----END CERTIFICATE-----\s*")


def webdav_url(url: str) -> SplitResult:
    """Parse the same safe HTTP(S) address for rclone, urllib and the UI."""
    if (not isinstance(url, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
            or any(c in url for c in "\\<>?#")):
        raise ValueError("WebDAV 地址不能含空白、控制字符、尖括号、反斜线、查询参数或片段")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise ValueError("WebDAV 地址或端口无效") from exc
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError("请输入完整的 HTTP(S) WebDAV 地址和主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("请单独填写账号和密码，不要把凭据放进 WebDAV 地址")
    if port == 0 or parsed.netloc.endswith(":"):
        raise ValueError("WebDAV 端口必须为 1–65535")
    if parsed.netloc.startswith("["):
        if "%" in host or not re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", parsed.netloc):
            raise ValueError("WebDAV IPv6 地址无效")
        ipaddress.IPv6Address(host)
    elif not re.fullmatch(r"[a-zA-Z0-9._-]+(?::[0-9]+)?", parsed.netloc):
        raise ValueError("WebDAV 主机名无效；国际域名请使用 ASCII/IDNA 格式")
    return parsed


def trusted_leaf(pem: str) -> str:
    """Accept one public end-entity certificate, not a CA, bundle or private key."""
    match = _PEM.fullmatch(pem) if isinstance(pem, str) and len(pem) <= _CERT_LIMIT else None
    if not match:
        raise ValueError("请选择单个公开的 WebDAV 服务器证书（PEM 或 DER），不能包含私钥或证书链")
    try:
        der = base64.b64decode("".join(match[1].splitlines()), validate=True)
        canonical = ssl.DER_cert_to_PEM_cert(der)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=canonical)
    except (ValueError, ssl.SSLError) as exc:
        raise ValueError("无法读取 WebDAV 服务器证书") from exc
    if context.cert_store_stats() != {"x509": 1, "crl": 0, "x509_ca": 0}:
        raise ValueError("只能信任单个服务器证书，不能注册 CA 证书")
    return canonical


def read_trusted_leaf(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read(_CERT_LIMIT + 1)
    if len(raw) > _CERT_LIMIT:
        raise ValueError("WebDAV 服务器证书文件过大")
    if raw.startswith(b"\x30"):
        return trusted_leaf(ssl.DER_cert_to_PEM_cert(raw))
    try:
        return trusted_leaf(raw.decode("ascii"))
    except UnicodeError as exc:
        raise ValueError("无法读取 WebDAV 服务器证书") from exc


def certificate_fingerprint(pem: str) -> str:
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest()
    return digest.hex(":").upper()


def tls_context(pem: str = "") -> ssl.SSLContext:
    # Use the ordinary Windows trusted stores, not SSL_CERT_FILE, SSL_CERT_DIR
    # or SSLKEYLOGFILE from the shell. Match Go's refusal of CN-only names.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.hostname_checks_common_name = False
    if pem:
        context.load_verify_locations(cadata=trusted_leaf(pem))
    elif hasattr(ssl, "enum_certificates"):
        for store in ("CA", "ROOT"):
            for cert, encoding, trust in ssl.enum_certificates(store):
                if encoding == "x509_asn" and (trust is True or ssl.Purpose.SERVER_AUTH.oid in trust):
                    context.load_verify_locations(cadata=cert)
    else:
        paths = ssl.get_default_verify_paths()
        cafile = paths.openssl_cafile if Path(paths.openssl_cafile).is_file() else None
        capath = paths.openssl_capath if Path(paths.openssl_capath).is_dir() else None
        if cafile or capath:
            context.load_verify_locations(cafile=cafile, capath=capath)
    return context


class _ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client, server = self.request, self.server
        if not server.track(client):
            return
        try:
            deadline = time.monotonic() + 5
            received = bytearray()
            while b"\r\n\r\n" not in received:
                remaining = deadline - time.monotonic()
                if len(received) >= 8192 or remaining <= 0:
                    return
                client.settimeout(remaining)
                chunk = client.recv(8192 - len(received))
                if not chunk:
                    return
                received.extend(chunk)
            head, _, extra = received.partition(b"\r\n\r\n")
            request, *headers = head.split(b"\r\n")
            cmd = request.split(b" ")
            auth = [value.strip() for header in headers for name, _, value in (header.partition(b":"),)
                    if name.lower() == b"proxy-authorization"]
            if (extra or len(cmd) != 3 or cmd[0] != b"CONNECT" or cmd[1].lower() != server.authority
                    or cmd[2] not in (b"HTTP/1.0", b"HTTP/1.1") or len(auth) != 1
                    or not hmac.compare_digest(auth[0], server.authorization)):
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            # Never use a target or headers supplied by the client to dial or
            # authenticate. Once connected this is just an opaque TLS tunnel.
            try:
                target = socket.create_connection(server.destination, timeout=10)
            except OSError:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            with target:
                if not server.track(target):
                    return
                try:
                    client.settimeout(60)
                    target.settimeout(60)
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    streams = {client: target, target: client}
                    while readable := select.select(list(streams), [], [], 180)[0]:
                        for stream in readable:
                            data = stream.recv(64 * 1024)
                            if not data:
                                return
                            streams[stream].sendall(data)
                finally:
                    server.untrack(target)
        except (OSError, ValueError):
            pass  # Cancelled process, timed out connection or peer hung up.
        finally:
            server.untrack(client)


class _EndpointServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(self, host: str, port: int, auth: str):
        self.destination = (host, port)
        self.authority = (f"[{host}]:{port}" if ":" in host else f"{host}:{port}").lower().encode("ascii")
        self.authorization = b"Basic " + base64.b64encode(("phonenas:" + auth).encode("ascii"))
        self._slots = threading.BoundedSemaphore(32)
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        self._closing = False
        super().__init__(("127.0.0.1", 0), _ConnectHandler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def track(self, stream: socket.socket) -> bool:
        with self._lock:
            if self._closing:
                return False
            self._sockets.add(stream)
            return True

    def untrack(self, stream: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(stream)

    def close_connections(self) -> None:
        with self._lock:
            self._closing = True
            for stream in self._sockets:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()


@contextmanager
def https_endpoint_proxy(endpoint: SplitResult):
    """rclone follows redirects; restrict HTTPS to the configured DNS name/IP and port.

    Go still checks certificates and hostnames end-to-end. This listener is
    per subprocess, loopback-only, and requires an ephemeral proxy credential.
    """
    host = endpoint.hostname
    if endpoint.scheme != "https" or not host:
        raise ValueError("HTTPS 转发需要已验证格式的 HTTPS 地址")
    token = secrets.token_urlsafe(24)
    with _EndpointServer(host, endpoint.port or 443, token) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield f"http://phonenas:{token}@127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.close_connections()
            thread.join()
