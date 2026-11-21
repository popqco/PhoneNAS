"""Real HTTP contracts for the isolated Android debug export over USB forwarding.

Only a literal loopback URL is accepted. Every write is below a new UUID tree
in the debug `test` share or a new UUID lock. No production config is loaded.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import socket
import uuid
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET


class Client:
    def __init__(self, url: str, username: str, password: str):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or not parsed.port or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or parsed.username):
            raise ValueError("Only a literal IPv4 loopback forwarded debug URL is allowed")
        self.port = parsed.port
        self.authorization = "Basic " + base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        self.prefix = "/test/contract-" + uuid.uuid4().hex
        self.lock = "/.phonenas/locks/" + uuid.uuid4().hex
        self.owns_tree = False
        self.owns_lock = False

    def request(self, method, path, body=None, headers=None, *, authenticated=True):
        supplied = dict(headers or {})
        if authenticated:
            supplied["Authorization"] = self.authorization
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(method, path, body, supplied)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def truncated_put(self, path):
        message = (
            f"PUT {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Authorization: {self.authorization}\r\nContent-Length: 1024\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", self.port), timeout=15) as connection:
            connection.sendall(message + b"incomplete-upload")
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            try:
                response.begin()
            except http.client.RemoteDisconnected:
                return None  # USB forwarding may close without an HTTP acknowledgement.
            status = response.status
            response.read()
        return status

    def file(self, name):
        return self.prefix + "/" + quote(name, safe="")

    def prepare(self):
        status = self.request("MKCOL", self.prefix + "/")[0]
        assert status == 201, f"Could not exclusively create scratch tree: {status}"
        self.owns_tree = True


def authentication(client):
    for method in ("GET", "HEAD", "OPTIONS", "PROPFIND", "PUT", "MKCOL", "DELETE"):
        status = client.request(method, client.prefix, authenticated=False)[0]
        assert status == 401, f"unauthenticated {method} returned {status}"


def files_range_and_names(client):
    contents = b"0123456789abcdef"
    path = client.file("中文 空格%#?.bin")
    assert client.request("PUT", path, contents)[0] == 201
    assert client.request("GET", path)[2] == contents
    status, headers, body = client.request("GET", path, headers={"Range": "bytes=3-7"})
    assert status == 206 and body == b"34567", f"incorrect byte range: {status}"
    assert headers.get("Content-Range") == "bytes 3-7/16"
    assert client.request("HEAD", path)[2] == b""
    status, _, xml = client.request("PROPFIND", client.prefix + "/", headers={"Depth": "1"})
    assert status == 207, f"PROPFIND returned {status}"
    hrefs = [element.text for element in ET.fromstring(xml).iter("{DAV:}href")]
    assert path in hrefs, "encoded filename missing from listing"
    moved = client.file("历史文件.bin")
    status = client.request("MOVE", path, headers={
        "Destination": f"http://127.0.0.1:{client.port}" + moved,
        "Overwrite": "F",
    })[0]
    assert status == 201, f"new MOVE returned {status}"
    assert client.request("GET", moved)[2] == contents
    assert client.request("GET", path)[0] == 404


def exclusive_lock(client):
    for parent in ("/.phonenas/", "/.phonenas/locks/"):
        status = client.request("MKCOL", parent)[0]
        assert status in (201, 405), f"control parent creation returned {status}"
    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(
            lambda _: client.request("MKCOL", client.lock + "/")[0], range(8)
        ))
    client.owns_lock = statuses.count(201) == 1
    assert statuses.count(201) == 1 and statuses.count(405) == 7, f"lock race: {statuses}"
    owner = b'{"token":"scratch-proof-never-production"}'
    assert client.request("PUT", client.lock + "/owner.json", owner)[0] == 201
    assert client.request("MKCOL", client.lock + "/")[0] == 405
    assert client.request("GET", client.lock + "/owner.json")[2] == owner
    assert client.request("DELETE", client.lock + "/")[0] in (200, 204)
    client.owns_lock = False


def interrupted_uploads(client):
    old = b"previous-complete-file"
    path = client.file("complete.bin")
    assert client.request("PUT", path, old)[0] == 201
    status = client.truncated_put(path)
    assert status is None or not 200 <= status < 300
    status, _, after = client.request("GET", path)
    assert status == 200 and after == old, "interrupted overwrite changed the old complete file"
    new = client.file("unfinished.bin")
    status = client.truncated_put(new)
    assert status is None or not 200 <= status < 300
    assert client.request("GET", new)[0] == 404, "new half-file became visible"
    status, _, xml = client.request("PROPFIND", client.prefix + "/", headers={"Depth": "1"})
    assert status == 207 and b".phonenas-temp-" not in xml, "internal staging was exported"


def readonly(client):
    status, _, original = client.request("GET", "/readonly/readme.txt")
    assert status == 200 and original, "read-only fixture missing or unreadable"
    for method, path, data in (
        ("PUT", "/readonly/readme.txt", b"forbidden"),
        ("MKCOL", "/readonly/contract-" + uuid.uuid4().hex, None),
        ("DELETE", "/readonly/readme.txt", None),
    ):
        status = client.request(method, path, data)[0]
        assert status == 403, f"read-only {method} returned {status}"
    after_status, _, after = client.request("GET", "/readonly/readme.txt")
    assert after_status == 200 and after == original, "read-only fixture was modified"


def move_overwrite(client):
    source, target = client.file("source.bin"), client.file("target.bin")
    for path, value in ((source, b"new-complete"), (target, b"old-complete")):
        assert client.request("PUT", path, value)[0] == 201
    destination = f"http://127.0.0.1:{client.port}" + target
    status = client.request("MOVE", source, headers={"Destination": destination, "Overwrite": "F"})[0]
    assert status == 412, f"non-overwrite MOVE returned {status}"
    assert client.request("GET", target)[2] == b"old-complete"
    assert client.request("GET", source)[2] == b"new-complete"
    status = client.request("MOVE", source, headers={"Destination": destination, "Overwrite": "T"})[0]
    assert status == 204 and client.request("GET", target)[2] == b"new-complete"


def path_policy(client):
    for path in ("/test/../outside", "/test/%2e%2e/outside", "/test/..%5coutside",
                 "/test/.phonenas/alias", "/test/.phonenas-temp-hidden"):
        status = client.request("PUT", path, b"no")[0]
        assert status in (400, 403, 404), f"noncanonical/reserved request accepted: {status}"
    assert client.request("DELETE", "/test/")[0] == 403, "share root deletion must be forbidden"


def large_content(client):
    contents = bytes(range(256)) * 8192
    path = client.file("two-megabyte.bin")
    assert client.request("PUT", path, contents)[0] == 201
    status, _, readback = client.request("GET", path)
    assert status == 200 and hashlib.sha256(readback).digest() == hashlib.sha256(contents).digest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    credentials = json.loads(args.credentials.read_text("utf-8"))
    client = Client(args.url, credentials["username"], credentials["password"])
    result = {"scope": "USB loopback / isolated native debug export", "checks": []}
    client.prepare()
    try:
        for check in (authentication, files_range_and_names, exclusive_lock,
                      interrupted_uploads, readonly, move_overwrite, path_policy, large_content):
            try:
                check(client)
                entry = {"name": check.__name__, "passed": True}
            except (AssertionError, OSError, http.client.HTTPException, ET.ParseError) as error:
                entry = {"name": check.__name__, "passed": False, "evidence": str(error)}
            result["checks"].append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    finally:
        if client.owns_tree:
            result["scratch_cleanup_status"] = client.request("DELETE", client.prefix + "/")[0]
        if client.owns_lock:
            result["scratch_lock_cleanup_status"] = client.request("DELETE", client.lock + "/")[0]
    result["passed"] = all(entry["passed"] for entry in result["checks"])
    with args.report.open("x", encoding="utf-8") as report:
        json.dump(result, report, ensure_ascii=False, indent=2)
        report.write("\n")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
