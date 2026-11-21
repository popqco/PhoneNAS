"""Scratch-only HTTP contract probe for the existing rclone WebDAV candidate.

This is not the Android file engine. It never reads PhoneNAS configuration,
connects to the phone, or serves any caller-supplied directory.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import uuid
from urllib.parse import quote
import xml.etree.ElementTree as ET


class Candidate:
    def __init__(self, executable: Path, temporary: Path):
        self.executable = executable
        self.root = temporary / "scratch-files"
        self.root.mkdir()
        self.config = temporary / "empty.conf"
        self.config.write_text("", encoding="utf-8")
        self.log = temporary / "candidate.log"
        token = uuid.uuid4().hex
        self.authorization = "Basic " + base64.b64encode(
            f"probe:{token}".encode("ascii")
        ).decode("ascii")
        self.password = token
        self.proc = None
        self.port = 0
        self.read_only = False

    def start(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self.proc = subprocess.Popen(
            [str(self.executable), "--config", str(self.config), "serve", "webdav",
             str(self.root), "--addr", f"127.0.0.1:{self.port}",
             "--user", "probe", "--pass", self.password,
             "--log-file", str(self.log), "--log-level", "INFO",
             "--dir-cache-time", "0s", "--vfs-cache-mode", "off",
             "--server-read-timeout", "5s", "--server-write-timeout", "5s",
             *(["--read-only"] if self.read_only else [])],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("Candidate exited before accepting requests")
            try:
                if self.request("OPTIONS", "/", authenticated=False)[0] == 401:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        raise RuntimeError("Candidate did not become ready within 15 seconds")

    def stop(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
            self.proc = None

    def request(self, method, path, body=None, headers=None, *, authenticated=True):
        supplied = dict(headers or {})
        if authenticated:
            supplied["Authorization"] = self.authorization
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def truncated_put(self, path):
        request = (
            f"PUT {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Authorization: {self.authorization}\r\n"
            "Content-Length: 1024\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", self.port), timeout=8) as connection:
            connection.sendall(request + b"incomplete-upload")
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            status = response.status
            response.read()
        return status


def interrupted_overwrite(candidate):
    original = b"previous-complete-file"
    status, _, _ = candidate.request("PUT", "/existing.bin", original)
    assert status in (200, 201, 204), f"initial PUT returned {status}"
    status = candidate.truncated_put("/existing.bin")
    after_status, _, after = candidate.request("GET", "/existing.bin")
    assert not 200 <= status < 300, f"incomplete PUT incorrectly returned {status}"
    assert after_status == 200 and after == original, (
        f"incomplete overwrite changed the old file: GET={after_status}, "
        f"old_bytes={len(original)}, observed_bytes={len(after)}"
    )


def authentication_required(candidate):
    for method in ("GET", "HEAD", "OPTIONS", "PROPFIND", "PUT", "MKCOL", "DELETE"):
        status, _, _ = candidate.request(method, "/private", authenticated=False)
        assert status == 401, f"unauthenticated {method} returned {status}"


def atomic_mkcol(candidate):
    candidate.request("MKCOL", "/race/")
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(
            lambda _: candidate.request("MKCOL", "/race/exclusive/")[0], range(8)
        ))
    assert statuses.count(201) == 1 and statuses.count(405) == 7, (
        f"MKCOL race expected one 201 and seven 405: {statuses}"
    )


def persistent_dataset_lock(candidate):
    for path in ("/.phonenas/", "/.phonenas/locks/", "/.phonenas/locks/dataset/"):
        status = candidate.request("MKCOL", path)[0]
        assert status == 201, f"MKCOL {path} returned {status}"
    owner = b'{"token":"scratch-token-proof"}'
    owner_path = "/.phonenas/locks/dataset/owner.json"
    assert candidate.request("PUT", owner_path, owner)[0] in (200, 201, 204)
    candidate.stop()
    candidate.start()
    status = candidate.request("MKCOL", "/.phonenas/locks/dataset/")[0]
    assert status == 405, f"existing lock after restart returned {status}, expected 405"
    assert candidate.request("GET", owner_path)[2] == owner, "owner changed on restart"
    status = candidate.request("DELETE", "/.phonenas/locks/dataset/")[0]
    assert status in (200, 204), f"DELETE lock returned {status}"
    assert candidate.request("GET", owner_path)[0] == 404, "deleted owner remains visible"


def encoded_range_and_history(candidate):
    filename = "中文 空格%#?.bin"
    path = "/" + quote(filename, safe="")
    contents = b"0123456789abcdef"
    assert candidate.request("PUT", path, contents)[0] in (200, 201, 204)
    status, headers, body = candidate.request("GET", path, headers={"Range": "bytes=3-7"})
    assert status == 206 and body == b"34567", f"Range returned {status}, {body!r}"
    assert headers.get("Content-Range") == "bytes 3-7/16"
    assert candidate.request("HEAD", path)[0] == 200
    status, _, body = candidate.request("PROPFIND", "/", headers={"Depth": "1"})
    assert status == 207, f"PROPFIND returned {status}"
    hrefs = [item.text for item in ET.fromstring(body).iter("{DAV:}href")]
    assert path in hrefs, f"encoded filename absent from listing: {hrefs}"
    assert candidate.request("MKCOL", "/versions/")[0] == 201
    destination = f"http://127.0.0.1:{candidate.port}/versions/" + quote(filename, safe="")
    status = candidate.request("MOVE", path, headers={"Destination": destination})[0]
    assert status in (201, 204), f"MOVE returned {status}"
    assert candidate.request("GET", path)[0] == 404
    assert candidate.request("GET", "/versions/" + quote(filename, safe=""))[2] == contents


def interrupted_new_file(candidate):
    status = candidate.truncated_put("/unfinished-new.bin")
    after_status = candidate.request("GET", "/unfinished-new.bin")[0]
    assert not 200 <= status < 300, f"incomplete new PUT returned {status}"
    assert after_status == 404, f"incomplete new file is visible: GET={after_status}"


def read_only_denies_mutation(candidate):
    fixture = b"known-good-readonly-contents"
    status = candidate.request("PUT", "/readonly-fixture.bin", fixture)[0]
    assert status in (200, 201, 204), f"read-only fixture setup returned {status}"
    candidate.stop()
    candidate.read_only = True
    candidate.start()
    try:
        before = candidate.request("GET", "/readonly-fixture.bin")
        assert before[0] == 200 and before[2] == fixture, "read-only fixture is not readable"
        for method, path, body in (
            ("PUT", "/readonly-fixture.bin", b"forbidden overwrite"),
            ("PUT", "/forbidden.bin", b"no"),
            ("MKCOL", "/forbidden/", None),
            ("DELETE", "/readonly-fixture.bin", None),
        ):
            status = candidate.request(method, path, body)[0]
            assert 400 <= status < 500, f"read-only {method} returned {status}"
        after = candidate.request("GET", "/readonly-fixture.bin")
        assert after[0] == 200 and after[2] == fixture, "read-only file changed or became unreadable"
        assert candidate.request("GET", "/forbidden.bin")[0] == 404, "read-only PUT created a file"
        assert candidate.request("PROPFIND", "/forbidden/")[0] == 404, "read-only MKCOL created a directory"
    finally:
        candidate.stop()
        candidate.read_only = False
        candidate.start()


def noncanonical_path_policy(candidate):
    for path in ("/../outside.bin", "/%2e%2e/outside.bin", "/..%5coutside.bin"):
        status = candidate.request("PUT", path, b"escape")[0]
        assert not 200 <= status < 300, (
            f"noncanonical path accepted: {path}, HTTP {status}; "
            "this alone does not establish an escape from the export root"
        )
    assert candidate.request("GET", "/")[0] == 200


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rclone", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    executable = args.rclone.resolve(strict=True)
    version = subprocess.run(
        [str(executable), "version"], check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.splitlines()[0]
    result = {"candidate": "rclone serve webdav", "version": version,
              "scope": "loopback scratch only", "checks": []}
    with tempfile.TemporaryDirectory(prefix="PhoneNAS-dav-contract-") as temporary:
        candidate = Candidate(executable, Path(temporary))
        try:
            candidate.start()
            for check in (authentication_required, atomic_mkcol, persistent_dataset_lock,
                          encoded_range_and_history, interrupted_overwrite,
                          interrupted_new_file, read_only_denies_mutation,
                          noncanonical_path_policy):
                try:
                    check(candidate)
                    entry = {"name": check.__name__, "passed": True}
                except (AssertionError, OSError, http.client.HTTPException) as exc:
                    entry = {"name": check.__name__, "passed": False, "evidence": str(exc)}
                result["checks"].append(entry)
                print(json.dumps(entry, ensure_ascii=False), flush=True)
        finally:
            candidate.stop()
    result["passed"] = all(check["passed"] for check in result["checks"])
    if args.report:
        with args.report.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write("\n")
    print(json.dumps({"passed": result["passed"], "checks": len(result["checks"])}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
