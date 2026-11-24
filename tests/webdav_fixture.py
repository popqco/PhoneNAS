"""Loopback-only WebDAV fixture backed by a trusted, disposable directory."""

from collections.abc import Iterator
from contextlib import contextmanager
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler
from pathlib import Path, PureWindowsPath
from socketserver import TCPServer
from tempfile import mkstemp
from threading import Thread
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET
import os
import shutil
import stat


class _DAVError(Exception):
    pass


def _valid_name(name):
    if not name or name in (".", "..") or any(c in name for c in "/\\\0"):
        return False
    # Reject Win32 aliases and device/stream paths instead of rewriting them.
    return os.name != "nt" or not (
        name.endswith((" ", "."))
        or PureWindowsPath(name).is_reserved()
        or any(c in '<>:"|?*' or ord(c) < 32 for c in name)
    )


def _stat(path):
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    # A disposable fixture only needs ordinary directories and unlinked files.
    if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise _DAVError(403)
    if not (stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
        raise _DAVError(403)
    return info


def _require_parent(path):
    info = _stat(path.parent)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise _DAVError(409)


def _remove(path, info):
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _route(method):
    def handle(self):
        try:
            if self.server.requests is not None:
                self.server.requests.append(self.command)
            if self.headers.get("Host", self.server.authority) != self.server.authority:
                raise _DAVError(400)
            # BaseHTTPRequestHandler.path can collapse leading slashes.
            target = self.requestline.split()[1]
            if self.command == "OPTIONS" and target == "*":
                target = "/"
            path, info = self._lookup(target)
            if self.server.reject is not None:
                status = self.server.reject(self.command, target, self.headers)
                if status is not None:
                    raise _DAVError(status)
            method(self, path, info)
        except _DAVError as error:
            self._reply(error.args[0])
        except UnicodeDecodeError:
            self._reply(400)
        except FileNotFoundError:
            self._reply(404)
        except PermissionError:
            self._reply(403)
        except ConnectionError:
            self.close_connection = True

    return handle


class _Handler(BaseHTTPRequestHandler):
    timeout = 5

    def log_message(self, *_args):
        pass

    def _lookup(self, target, *, destination=False):
        if not target or any(not "!" <= c <= "~" or c in "?#" for c in target):
            raise _DAVError(400)
        if destination and not target.startswith("/"):
            if not target.startswith(self.server.endpoint):
                raise _DAVError(502)
            target = target[len(self.server.endpoint) - 1:]
        if not target.startswith("/"):
            raise _DAVError(400)
        names = [] if target == "/" else [
            unquote(part, errors="strict") for part in target[1:].removesuffix("/").split("/")
        ]
        if not all(map(_valid_name, names)):
            raise _DAVError(403)

        path = self.server.root
        info = _stat(path)
        if info is None or not stat.S_ISDIR(info.st_mode) or path.resolve(strict=True).parts != path.parts:
            raise _DAVError(403)
        for name in names:
            path = path / name
            info = _stat(path)
            if info is not None and os.name == "nt" and path.resolve(strict=True).parts != path.parts:
                raise _DAVError(403)  # Existing name reached via a case or short-name alias.
        return path, info

    def _reply(self, status, body=b"", *, size=None, headers=()):
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Length", str(len(body) if size is None else size))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _length(self, *, required=False):
        if "Transfer-Encoding" in self.headers:
            raise _DAVError(501)
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            if required:
                raise _DAVError(411)
            return 0
        (value, *duplicates) = lengths
        if duplicates or len(value) > 19 or not value.isascii() or not value.isdecimal():
            raise _DAVError(400)
        return int(value)

    def _read_body(self, length, stream=None):
        while length:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                raise _DAVError(400)
            if stream is not None:
                stream.write(chunk)
            length -= len(chunk)

    @_route
    def do_OPTIONS(self, _path, _info):
        self._reply(200, headers=(
            ("DAV", "1"), ("Allow", "OPTIONS, PROPFIND, GET, HEAD, PUT, MKCOL, DELETE, MOVE"),
        ))

    @_route
    def do_PROPFIND(self, path, info):
        if info is None:
            raise _DAVError(404)
        depth = self.headers.get("Depth", "0")
        if depth not in ("0", "1"):
            raise _DAVError(403)
        self._read_body(self._length())  # Only the fixture's fixed property set is returned.
        entries = [(path, info)]
        if depth == "1" and stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir(), key=lambda entry: entry.name):
                if _valid_name(child.name):
                    try:
                        child_info = _stat(child)
                    except _DAVError:
                        continue
                    if child_info is not None:
                        entries.append((child, child_info))
        multistatus = ET.Element("multistatus", xmlns="DAV:")
        for entry, entry_info in entries:
            directory = stat.S_ISDIR(entry_info.st_mode)
            parts = entry.relative_to(self.server.root).parts
            href = "/" + "/".join(quote(part, safe="") for part in parts)
            response = ET.SubElement(multistatus, "response")
            ET.SubElement(response, "href").text = href + ("/" if parts and directory else "")
            propstat = ET.SubElement(response, "propstat")
            prop = ET.SubElement(propstat, "prop")
            resource_type = ET.SubElement(prop, "resourcetype")
            if directory:
                ET.SubElement(resource_type, "collection")
            ET.SubElement(prop, "getcontentlength").text = str(0 if directory else entry_info.st_size)
            ET.SubElement(prop, "getlastmodified").text = formatdate(entry_info.st_mtime, usegmt=True)
            ET.SubElement(propstat, "status").text = "HTTP/1.1 200 OK"
        self._reply(207, ET.tostring(multistatus, encoding="utf-8", xml_declaration=True),
                    headers=(("Content-Type", "application/xml; charset=utf-8"),))

    @_route
    def do_GET(self, path, info):
        if info is None:
            raise _DAVError(404)
        if stat.S_ISDIR(info.st_mode):
            raise _DAVError(405)
        with path.open("rb") as stream:
            self._reply(200, size=info.st_size, headers=(
                ("Content-Type", "application/octet-stream"),
                ("Last-Modified", formatdate(info.st_mtime, usegmt=True)),
            ))
            if self.command == "GET":
                shutil.copyfileobj(stream, self.wfile)

    do_HEAD = do_GET

    @_route
    def do_PUT(self, path, info):
        if info is not None and stat.S_ISDIR(info.st_mode):
            raise _DAVError(405)
        _require_parent(path)
        length = self._length(required=True)
        fd, stage = mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                self._read_body(length, stream)
            os.replace(stage, path)
        finally:
            Path(stage).unlink(missing_ok=True)
        self._reply(201 if info is None else 204)

    @_route
    def do_MKCOL(self, path, info):
        if info is not None:
            raise _DAVError(405)
        if self._length():
            raise _DAVError(415)
        _require_parent(path)
        try:
            path.mkdir()
        except FileExistsError:
            raise _DAVError(405) from None
        self._reply(201)

    @_route
    def do_DELETE(self, path, info):
        if path == self.server.root:
            raise _DAVError(403)
        if info is None:
            raise _DAVError(404)
        _remove(path, info)
        self._reply(204)

    @_route
    def do_MOVE(self, path, info):
        if info is None:
            raise _DAVError(404)
        dest, existing = self._lookup(self.headers.get("Destination", ""), destination=True)
        # Includes self-moves, moves into descendants, overwriting ancestors, and the root.
        if path.is_relative_to(dest) or dest.is_relative_to(path):
            raise _DAVError(403)
        overwrite = self.headers.get("Overwrite", "T")
        if overwrite not in ("T", "F"):
            raise _DAVError(400)
        if existing is not None and overwrite == "F":
            raise _DAVError(412)
        _require_parent(dest)
        if existing is not None and (stat.S_ISDIR(existing.st_mode) or stat.S_ISDIR(info.st_mode)):
            _remove(dest, existing)
        path.replace(dest)
        self._reply(201 if existing is None else 204)


class _Server(TCPServer):
    # Serial request handling keeps filesystem operations ordered and MKCOL exclusive.
    request_queue_size = 32

    def __init__(self, root, requests, reject):
        self.root = root
        self.requests = requests
        self.reject = reject
        super().__init__(("127.0.0.1", 0), _Handler)
        host, port = self.server_address
        self.authority = f"{host}:{port}"
        self.endpoint = f"http://{self.authority}/"


@contextmanager
def serve_webdav(root: Path, *, requests: list[str] | None = None, reject=None) -> Iterator[str]:
    """Yield an ephemeral HTTP endpoint; do not mutate the served tree concurrently."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    with _Server(root, requests, reject) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield server.endpoint
        finally:
            server.shutdown()
            thread.join()
