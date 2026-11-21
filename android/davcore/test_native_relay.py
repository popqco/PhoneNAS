"""Finite REAL desktop-client relay against an already authorized USB forward.

Run from a standalone process (no device/forward/configuration setup is performed)::

    python -I -B android/davcore/test_native_relay.py \
        --url http://127.0.0.1:55115 --credentials <private-json> \
        --rclone <project>/vendor/rclone.exe --report <new-report.json>

Only the fixed /acceptance_rw export is writable. The private JSON must contain
username, password, scope="isolated-public-acceptance", root equal to
/storage/emulated/0/PhoneNAS-Acceptance-<uuid>/rw, and readonly_root with the SAME
UUID and /ro. These are assertions about the already configured acceptance shares,
not instructions to read/modify phone settings. Both known readme fixtures must
match before any writes. No demo/production profile, discovery, TLS downgrade or
remote/tailnet claim is supported: this run is loopback HTTP transport ONLY.

SyncEngine, Rclone, HistoryService and the default WebDAV lease are the existing
product implementations, not models. The old Rclone API has no per-client env
argument: this one-shot process temporarily replaces its OWN environment before
importing app.core, strips all RCLONE_* and proxy settings, and restores it on
exit. Every child has an explicit private --config; all profiles, baselines,
history, logs, aliases and core-created temporary files live in TemporaryDirectory.
Only the allowlisted digest report survives. This is not a desktop feature change.

Cleanup never edits state to "ready", steals a retained lock, changes a mapping,
or deletes a parent control/export root. Unproved ownership or a live/retained
lease stops cleanup and FAILs the run; report UUIDs identify the isolated residue.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from contextlib import contextmanager
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[2]
PUBLIC_SCOPE = "isolated-public-acceptance"

FAILURE_CODES = frozenset({
    "UNSAFE_ENDPOINT", "UNSAFE_CREDENTIALS", "UNSAFE_SCOPE", "UNSAFE_LOCAL_PATH",
    "UNSAFE_RCLONE", "UNSAFE_REQUEST", "REPORT_UNAVAILABLE", "REPORT_IO",
    "CLIENT_IMPORT", "FIXTURE_MISMATCH", "OWNER_UNPROVEN", "LOCK_HELD",
    "METADATA_CHANGED", "REDIRECT_REFUSED", "RESPONSE_LIMIT", "TRANSPORT",
    "CONTRACT", "INTERRUPTED", "INTERNAL",
})
CHECKS = ("preflight", "fixtures", "prepare", "upload", "receive", "return",
          "history", "conflict", "adopt", "repeat", "source_integrity", "cleanup")


class SafetyError(Exception):
    def __init__(self, code):
        self.code = code if type(code) is str and code in FAILURE_CODES else "INTERNAL"
        super().__init__(self.code)


def require(condition, code="CONTRACT"):
    if not condition:
        raise SafetyError(code)


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int

    @property
    def url(self):
        return f"http://{self.host}:{self.port}"


def validate_endpoint(url):
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", url) if type(url) is str else None
    require(match is not None and 1 <= int(match[1]) <= 65535, "UNSAFE_ENDPOINT")
    return Endpoint("127.0.0.1", int(match[1]))


def local_file(value):
    """Reject implicit network/device/ADS/traversal IO before touching the path."""
    raw = os.fspath(value)
    require(type(raw) is str and bool(raw) and not raw.replace("\\", "/").startswith("//")
            and not any(ord(char) < 32 for char in raw), "UNSAFE_LOCAL_PATH")
    tail = raw[2:] if re.match(r"^[a-zA-Z]:", raw) else raw
    require(":" not in tail and not any(part in (".", "..") for part in re.split(r"[/\\]", tail)),
            "UNSAFE_LOCAL_PATH")
    path = Path(os.path.abspath(raw))
    for ancestor in reversed((path, *path.parents)):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400
                and (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)), "UNSAFE_LOCAL_PATH")
    return path


def unique_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "UNSAFE_CREDENTIALS")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(SafetyError("UNSAFE_CREDENTIALS")))


@dataclass(frozen=True)
class Credentials:
    username: str = field(repr=False)
    password: str = field(repr=False)
    scope: str = PUBLIC_SCOPE


def load_credentials(path):
    try:
        checked = local_file(path)
        with checked.open("rb") as stream:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "UNSAFE_CREDENTIALS")
            raw = stream.read(16 * 1024 + 1)
        require(len(raw) <= 16 * 1024, "UNSAFE_CREDENTIALS")
        value = unique_json(raw)
        require(type(value) is dict and set(value) == {
            "username", "password", "scope", "root", "readonly_root"}, "UNSAFE_CREDENTIALS")
        user, password = value["username"], value["password"]
        require(type(user) is str and type(password) is str and bool(user) and ":" not in user
                and len(user.encode("utf-8")) <= 128 and 20 <= len(password.encode("utf-8")) <= 2048
                and not any(ord(char) < 32 or ord(char) == 127 for char in user + password),
                "UNSAFE_CREDENTIALS")
        require(value["scope"] == PUBLIC_SCOPE and type(value["root"]) is str, "UNSAFE_SCOPE")
        match = re.fullmatch(r"(/storage/emulated/0/PhoneNAS-Acceptance-([0-9a-f-]{32,36}))/rw", value["root"])
        require(match is not None and value["readonly_root"] == match[1] + "/ro", "UNSAFE_SCOPE")
        identity = uuid.UUID(match[2])
        require(identity.int != 0 and match[2] in (identity.hex, str(identity)), "UNSAFE_SCOPE")
        # Physical Android paths deliberately do not escape this validation seam.
        return Credentials(user, password)
    except SafetyError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise SafetyError("UNSAFE_CREDENTIALS") from None


def failure_code(error):
    if isinstance(error, SafetyError):
        return error.code
    if isinstance(error, KeyboardInterrupt):
        return "INTERRUPTED"
    if isinstance(error, (OSError, http.client.HTTPException)):
        return "TRANSPORT"
    if isinstance(error, AssertionError):
        return "CONTRACT"
    return "INTERNAL"


class ExclusiveReport:
    """Exclusive RUNNING reservation before credentials, clients or network IO."""
    def __init__(self, path):
        self.path = local_file(path)
        self.stream = None
        self.run_id = uuid.uuid4().hex
        self.dataset_id = ""
        self.checks = []
        self.started = time.monotonic()
        self.finished = False

    def __enter__(self):
        try:
            self.stream = self.path.open("x+", encoding="utf-8", newline="\n")
            self.persist("RUNNING")
        except (OSError, ValueError):
            if self.stream is not None:
                self.stream.close()
            raise SafetyError("REPORT_UNAVAILABLE") from None
        return self

    def require_reserved(self):
        require(self.stream is not None and not self.stream.closed and not self.finished, "REPORT_UNAVAILABLE")

    def persist(self, status):
        payload = {"schema": "phonenas-native-relay-v1", "status": status,
                   "transport": "http-loopback-authorized-usb-forward", "remoteRelayClaim": False,
                   "client": "existing-SyncEngine-Rclone-HistoryService",
                   "safeUUID": self.run_id, "datasetUUID": self.dataset_id,
                   "elapsed_ms": round((time.monotonic() - self.started) * 1000, 1), "checks": self.checks}
        try:
            self.stream.seek(0)
            json.dump(payload, self.stream, ensure_ascii=True, separators=(",", ":"))
            self.stream.write("\n")
            self.stream.truncate()
            self.stream.flush()
            os.fsync(self.stream.fileno())
        except (OSError, ValueError):
            raise SafetyError("REPORT_IO") from None

    def record(self, name, status, *, code=None, sha256=None):
        self.require_reserved()
        require(name in (*CHECKS, "run") and status in ("PASS", "FAIL"), "INTERNAL")
        require(not any(item["check"] == name for item in self.checks), "INTERNAL")
        entry = {"check": name, "status": status}
        if code is not None:
            entry["failureCode"] = code if type(code) is str and code in FAILURE_CODES else "INTERNAL"
        if sha256 is not None:
            require(type(sha256) is str and re.fullmatch(r"[0-9a-f]{64}", sha256), "INTERNAL")
            entry["sha256"] = sha256
        self.checks.append(entry)
        self.persist("RUNNING")

    def check(self, name, action):
        self.require_reserved()
        try:
            value = action()
        except BaseException as error:
            self.record(name, "FAIL", code=failure_code(error))
            raise
        self.record(name, "PASS", sha256=value if type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) else None)
        return value

    def finish(self):
        self.require_reserved()
        passed = (tuple(item["check"] for item in self.checks) == CHECKS
                  and all(item["status"] == "PASS" for item in self.checks))
        self.persist("PASS" if passed else "FAIL")
        self.finished = True
        return passed

    def __exit__(self, kind, error, traceback):
        try:
            if not self.finished:
                if not any(item["status"] == "FAIL" for item in self.checks):
                    self.record("run", "FAIL", code=failure_code(error))
                self.finish()
        finally:
            if self.stream is not None:
                self.stream.close()
        return False


def checked_uuid(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) and uuid.UUID(value).int != 0,
            "UNSAFE_SCOPE")
    return value


@dataclass(frozen=True)
class Scope:
    run_id: str
    dataset_id: str

    def __post_init__(self):
        checked_uuid(self.run_id)
        checked_uuid(self.dataset_id)

    @property
    def tree(self):
        return "acceptance_rw/" + self.run_id

    @property
    def dataset(self):
        return ".phonenas/datasets/" + self.dataset_id

    @property
    def lock(self):
        return ".phonenas/locks/" + self.dataset_id

    def validate_task(self, task, private_root):
        require(task.remote_path == self.tree + "/mirror" and task.versions_path == self.tree + "/versions"
                and task.dataset_id == self.dataset_id and task.dataset_control_path == self.dataset
                and task.excludes == () and not task.auto_sync, "UNSAFE_SCOPE")
        checked_uuid(task.id)
        checked_uuid(task.sync_identity)
        root = local_file(private_root)
        for value in (task.local_path, task.local_history_path):
            path = local_file(value)
            require(path != root and path.is_relative_to(root), "UNSAFE_LOCAL_PATH")

    def validate_args(self, args, private_root):
        require(type(args) is list and bool(args), "UNSAFE_REQUEST")
        require(args[0] in {"obscure", "lsjson", "hashsum", "cat", "mkdir", "copy", "copyto",
                            "moveto", "lsf", "purge", "rmdir"}, "UNSAFE_REQUEST")
        if args[0] == "obscure":
            require(len(args) == 2, "UNSAFE_REQUEST")
            return  # Password is an opaque argv value, never interpreted as a path.
        for argument in args[1:]:
            require(type(argument) is str and not any(ord(char) < 32 for char in argument), "UNSAFE_REQUEST")
            if argument.startswith("phone:"):
                target = argument[6:]
                require(not any(part in ("", ".", "..") for part in target.split("/"))
                        and not any(char in target for char in "\\:%?#"), "UNSAFE_SCOPE")
                roots = (self.tree, self.dataset, self.lock)
                allowed = any(target == root or target.startswith(root + "/") for root in roots)
                parent = args[0] == "mkdir" and target in (".phonenas/datasets", ".phonenas/locks")
                require(allowed or parent, "UNSAFE_SCOPE")
            elif (argument.startswith(":") or re.match(r"^[^/\\:]+:", argument)) and not re.match(r"^[a-zA-Z]:[/\\]", argument):
                raise SafetyError("UNSAFE_SCOPE")
            elif Path(argument).is_absolute():
                path = local_file(argument)
                require(path != private_root and path.is_relative_to(private_root), "UNSAFE_LOCAL_PATH")


class LoopbackControl:
    """Owner/fixture transport only. Payload relay stays in the actual client."""
    def __init__(self, endpoint, credentials, report, scope):
        report.require_reserved()
        self.endpoint = validate_endpoint(endpoint.url)
        require(credentials.scope == PUBLIC_SCOPE and scope.run_id == report.run_id, "UNSAFE_SCOPE")
        self.credentials, self.report, self.scope = credentials, report, scope
        self.owner = json.dumps({"version": 1, "safeUUID": scope.run_id, "dataset_id": scope.dataset_id,
                                 "token": uuid.uuid4().hex}, separators=(",", ":")).encode("ascii")
        self.created_tree = self.created_dataset = False

    def request(self, method, path, body=None):
        self.report.require_reserved()
        tree, dataset = "/" + self.scope.tree + "/", "/" + self.scope.dataset + "/"
        owners = (tree + "acceptance-owner.json", dataset + "acceptance-owner.json")
        allowed = {"GET": (*owners, "/acceptance_rw/readme.txt", "/acceptance_ro/readme.txt"),
                   "MKCOL": (tree, dataset), "PUT": owners, "DELETE": (tree, dataset)}
        require(method in allowed and path in allowed[method], "UNSAFE_REQUEST")
        require(body is None or method == "PUT" and body == self.owner, "UNSAFE_REQUEST")
        import base64
        headers = {"Authorization": "Basic " + base64.b64encode(
            (self.credentials.username + ":" + self.credentials.password).encode("utf-8")).decode("ascii"),
                   "Connection": "close"}
        if method == "PUT":
            headers.update({"If-None-Match": "*", "Content-Type": "application/json"})
        connection = http.client.HTTPConnection(self.endpoint.host, self.endpoint.port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            require(not 300 <= response.status < 400, "REDIRECT_REFUSED")
            data = response.read(65537)
            require(len(data) <= 65536, "RESPONSE_LIMIT")
            return response.status, data
        finally:
            connection.close()

    def fixtures(self):
        for name, kind in (("acceptance_rw", "test"), ("acceptance_ro", "readonly")):
            status, body = self.request("GET", "/" + name + "/readme.txt")
            require(status == 200 and body == (
                f"PhoneNAS isolated {kind} demo. No personal folders are exported.\n").encode("ascii"),
                "FIXTURE_MISMATCH")

    def verify_owner(self, root):
        require(root in (self.scope.tree, self.scope.dataset), "UNSAFE_SCOPE")
        status, body = self.request("GET", "/" + root + "/acceptance-owner.json")
        require(status == 200 and hmac.compare_digest(body, self.owner), "OWNER_UNPROVEN")

    def claim(self, root):
        require(root in (self.scope.tree, self.scope.dataset), "UNSAFE_SCOPE")
        require(self.request("MKCOL", "/" + root + "/")[0] == 201, "OWNER_UNPROVEN")
        if root == self.scope.tree:
            self.created_tree = True
        else:
            self.created_dataset = True
        require(self.request("PUT", "/" + root + "/acceptance-owner.json", self.owner)[0] == 201,
                "OWNER_UNPROVEN")
        self.verify_owner(root)


def validate_rclone(path):
    checked = local_file(path)
    require(Path(path).is_absolute() and checked == PROJECT / "vendor" / "rclone.exe"
            and checked.is_file(), "UNSAFE_RCLONE")
    return checked


@contextmanager
def private_environment(root):
    """Compatibility seam for the old client; never changes OS/user settings."""
    root = local_file(root)
    saved, old_tempdir, old_path = dict(os.environ), tempfile.tempdir, list(sys.path)
    environment = {key: value for key, value in saved.items()
                   if not key.upper().startswith(("RCLONE_", "PYTHON"))
                   and not key.upper().endswith("PROXY")}
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PHONENAS_DATA_DIR",
                 "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "TEMP", "TMP", "TMPDIR"):
        target = root / ("temp" if name in ("TEMP", "TMP", "TMPDIR") else "private-profile")
        target.mkdir(exist_ok=True)
        environment[name] = str(target)
    try:
        os.environ.clear()
        os.environ.update(environment)
        tempfile.tempdir = str(root / "temp")
        sys.path.insert(0, str(PROJECT))
        yield
    finally:
        sys.path[:] = old_path
        tempfile.tempdir = old_tempdir
        os.environ.clear()
        os.environ.update(saved)


@dataclass(frozen=True)
class ConnectionProfile:
    """The existing Rclone/BaselineStore connection interface; no Config.load IO."""
    url: str
    username: str = field(repr=False)
    password: str = field(repr=False)


def bind_private_rclone(client, scope, root, report):
    """Delegate every operation to real Rclone; only constrain argv/config/time."""
    original_run, original_popen = client.run, client.popen
    conf = client.conf_path
    deadline = time.monotonic() + 300
    flags = ["--retries", "1", "--low-level-retries", "1", "--timeout", "15s",
             "--contimeout", "5s", "--max-duration", "45s", "--bind", "127.0.0.1",
             "--cache-dir", str(root / "cache")]

    def guard(args, conf_path):
        report.require_reserved()
        require(time.monotonic() < deadline, "TRANSPORT")
        require(conf_path is None or Path(conf_path) == conf, "UNSAFE_LOCAL_PATH")
        require(client.conf_path == conf, "UNSAFE_LOCAL_PATH")
        scope.validate_args(args, root)
        return [*args, *flags]

    def run(args, timeout=120, conf_path=None, cancel_event=None):
        return original_run(guard(args, conf_path), timeout=min(timeout, 60),
                            conf_path=conf, cancel_event=cancel_event)

    def popen(args, log_path=None, conf_path=None):
        require(log_path is None, "UNSAFE_LOCAL_PATH")
        return original_popen(guard(args, conf_path), conf_path=conf)

    client.run, client.popen = run, popen


def write_alias(client, endpoint, credentials):
    # Use the REAL writer/obscurer, then explicitly bind the engine's phone alias
    # to this one loopback WebDAV backend (never a local directory/model remote).
    client.write_conf(endpoint.url, credentials.username, credentials.password)
    path = client.conf_path
    text = path.read_text("utf-8")
    require(text.startswith("[phone]\n") and "url = " + endpoint.url + "/\n" in text, "UNSAFE_SCOPE")
    text = text.replace("[phone]\n", "[acceptance_endpoint]\n", 1)
    path.write_text(text + "\n[phone]\ntype = alias\nremote = acceptance_endpoint:\n", encoding="utf-8")


@contextmanager
def native_client(endpoint, credentials, executable, report):
    report.require_reserved()
    require(not any(name == "app" or name.startswith("app.") for name in sys.modules), "CLIENT_IMPORT")
    # Validate the candidate BEFORE tempfile probes/writes could follow a UNC TMP.
    candidate = os.environ.get("TEMP") or os.environ.get("TMP") or os.environ.get("TMPDIR")
    parent = local_file(candidate or (str(Path.home() / "AppData/Local/Temp") if os.name == "nt" else "/tmp"))
    with tempfile.TemporaryDirectory(prefix="PhoneNAS-relay-" + report.run_id + "-", dir=parent) as directory:
        root = Path(directory)
        with private_environment(root):
            try:
                from app.core import rclone as rclone_module
                from app.core.history import HistoryService
                from app.core.relay import DatasetCoordinator, snapshot_tree
                from app.core.sync import SyncEngine
                from app.core.tasks import new_task
            except ImportError:
                raise SafetyError("CLIENT_IMPORT") from None
            original_exe = rclone_module.rclone_exe
            rclone_module.rclone_exe = lambda: executable
            clients = []
            try:
                profile = ConnectionProfile(endpoint.url, credentials.username, credentials.password)
                for name in ("publisher", "receiver"):
                    home = root / name
                    home.mkdir()
                    clients.append(rclone_module.Rclone(profile, conf_path=home / "private.conf"))
                acceptance = RelayAcceptance(root, endpoint, credentials, report, *clients,
                                             new_task, SyncEngine, HistoryService, DatasetCoordinator, snapshot_tree)
                for client in clients:
                    bind_private_rclone(client, acceptance.scope, root, report)
                    write_alias(client, endpoint, credentials)
                yield acceptance
            finally:
                for client in clients:
                    client.cancel()
                rclone_module.rclone_exe = original_exe


def digest_tree(root):
    result, pending = {}, [local_file(root)]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            checked = local_file(path)
            info = checked.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(checked)
            else:
                require(stat.S_ISREG(info.st_mode), "UNSAFE_LOCAL_PATH")
                with checked.open("rb") as stream:
                    result[checked.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def digest_inventory(inventory):
    return hashlib.sha256(json.dumps(inventory, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode("ascii")).hexdigest()


class RelayAcceptance:
    """Finite scenarios; all payload/control semantics use the actual client."""
    def __init__(self, root, endpoint, credentials, report, publisher_client, receiver_client,
                 new_task, SyncEngine, HistoryService, DatasetCoordinator, snapshot_tree, *,
                 control_factory=None, lease_factory=None):
        self.root, self.report = root, report
        self.publisher_client, self.receiver_client = publisher_client, receiver_client
        self.SyncEngine, self.HistoryService, self.snapshot_tree = SyncEngine, HistoryService, snapshot_tree
        self.office, self.home = root / "publisher" / "work", root / "receiver" / "work"
        self.office.mkdir()
        self.home.mkdir()
        self.original_bytes = {"docs/guide.txt": b"original version\n", "keep.txt": b"unchanged\n",
                               "removed.txt": b"old content for history\n", "中文 空格%#.txt": b"portable filename\n"}
        for relative, content in self.original_bytes.items():
            path = self.office / relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(content)
        (self.office / "empty").mkdir()
        self.original = {path: hashlib.sha256(content).hexdigest() for path, content in self.original_bytes.items()}
        require(digest_tree(self.office) == self.original)
        tree = "acceptance_rw/" + report.run_id
        self.publisher = replace(new_task(str(self.office), "Isolated relay publisher",
                                          remote_path=tree + "/mirror", versions_path=tree + "/versions"),
                                 excludes=(), exclude_preset="custom", auto_sync=False)
        self.receiver = replace(new_task(str(self.home), "Isolated relay receiver", direction="download",
                                         remote_path=self.publisher.remote_path, versions_path=self.publisher.versions_path,
                                         dataset_id=self.publisher.dataset_id),
                                excludes=(), exclude_preset="custom", auto_sync=False)
        self.scope = Scope(report.run_id, self.publisher.dataset_id)
        for task in (self.publisher, self.receiver):
            self.scope.validate_task(task, root)
        report.dataset_id = self.scope.dataset_id
        report.persist("RUNNING")
        self.coordinator = DatasetCoordinator(publisher_client, self.publisher)
        self.control = (control_factory or LoopbackControl)(endpoint, credentials, report, self.scope)
        self.lease_factory = lease_factory
        self.ready = None
        self.returned = None
        self.return_stamp = ""
        self.restored = None
        self.adopt_stamp = ""

    def engine(self, task, operation=""):
        self.scope.validate_task(task, self.root)
        client = self.publisher_client if task == self.publisher else self.receiver_client
        directory = self.root / ("publisher" if task == self.publisher else "receiver")
        # TLS entry may inject only the real WebDAV lease's HTTP boundary; USB
        # keeps the product default. Never use a local or model dataset lease.
        lease = (lambda: self.lease_factory(client, task)) if self.lease_factory else None
        return self.SyncEngine(client, task, state_directory=directory / "baselines",
                               log_directory=directory / "logs", operation=operation, lease_factory=lease)

    def mirror(self, expected):
        snapshot = self.snapshot_tree(self.publisher_client, "phone:" + self.publisher.remote_path)
        require(snapshot is not None and {name: value.sha256 for name, value in snapshot.files.items()} == expected)
        return snapshot

    def read_ready(self, *, advanced=False):
        require(not self.coordinator.locked(), "LOCK_HELD")
        state = self.coordinator.read()
        snapshot = self.mirror(self.returned if self.returned is not None else self.original)
        require(state is not None and state["phase"] == "ready" and state["content_digest"] == snapshot.digest,
                "METADATA_CHANGED")
        require(state.get("writer_identity") in (self.publisher.sync_identity, self.receiver.sync_identity),
                "METADATA_CHANGED")
        if not advanced:
            require(state == self.ready, "METADATA_CHANGED")
        self.ready = state

    @staticmethod
    def approve(engine, counts):
        plan = engine.preview()
        require(not plan.blocked and (plan.added, plan.updated, plan.deleted) == counts)
        return engine.execute(plan)

    @staticmethod
    def blocked(engine):
        plan = engine.preview()
        require(plan.blocked)
        try:
            engine.execute(plan)
        except RuntimeError:
            return
        raise SafetyError("CONTRACT")

    def fixtures(self):
        self.control.fixtures()

    def prepare(self):
        for path in (self.scope.tree, self.scope.dataset, self.scope.lock):
            result = self.publisher_client.run(["lsjson", "phone:" + path, "--stat", "--no-modtime", "--no-mimetype"])
            require(not result.ok and result.code in (3, 4), "OWNER_UNPROVEN")
        self.control.claim(self.scope.tree)
        require(self.publisher_client.mkdir(".phonenas/datasets").ok, "OWNER_UNPROVEN")
        self.control.claim(self.scope.dataset)

    def upload(self):
        self.approve(self.engine(self.publisher), (4, 0, 0))
        require(digest_tree(self.office) == self.original)  # upload is copy, never source deletion
        self.read_ready(advanced=True)
        return digest_inventory(self.original)

    def receive(self):
        self.approve(self.engine(self.receiver), (4, 0, 0))
        require(digest_tree(self.home) == self.original and digest_tree(self.office) == self.original)
        require(not any((self.home / name).exists() for name in ("mirror", "versions", "work", "acceptance_rw")))
        require((self.home / "empty").is_dir())
        self.read_ready()
        return digest_inventory(digest_tree(self.home))

    def manual_return(self):
        path = self.home / "docs/guide.txt"
        info = path.stat()
        edited = b"receiver version\n"
        require(len(edited) == info.st_size)
        path.write_bytes(edited)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))  # no metadata-only equality shortcut
        (self.home / "added.txt").write_bytes(b"new work from receiver\n")
        (self.home / "removed.txt").unlink()
        expected = {**self.original, "docs/guide.txt": hashlib.sha256(edited).hexdigest(),
                    "added.txt": hashlib.sha256(b"new work from receiver\n").hexdigest()}
        del expected["removed.txt"]
        require(digest_tree(self.home) == expected and expected != self.original)
        self.blocked(self.engine(self.receiver))  # do not overwrite unreturned local edits
        self.mirror(self.original)
        require(digest_tree(self.home) == expected)
        self.return_stamp = self.approve(self.engine(self.receiver, "return"), (1, 1, 1))
        require(bool(self.return_stamp) and digest_tree(self.home) == expected
                and digest_tree(self.office) == self.original)
        self.returned = expected
        self.read_ready(advanced=True)
        return digest_inventory(expected)

    def history(self):
        history = self.HistoryService(self.publisher_client, self.publisher)
        require(self.return_stamp in history.list_versions())
        require(set(history.list_files(self.return_stamp)) == {"docs/guide.txt", "removed.txt"})
        destination = self.root / "history-restore"
        destination.mkdir()
        self.restored = history.restore(self.return_stamp, destination)
        expected = {name: self.original[name] for name in ("docs/guide.txt", "removed.txt")}
        require(digest_tree(self.restored) == expected and digest_tree(self.office) == self.original
                and digest_tree(self.home) == self.returned)
        self.read_ready()
        return digest_inventory(expected)

    def conflict(self):
        self.blocked(self.engine(self.publisher))  # stale publish cannot overwrite receiver's return
        self.mirror(self.returned)
        path = self.office / "docs/guide.txt"
        path.write_bytes(b"unreturned publisher conflict\n")
        before = digest_tree(self.office)
        self.blocked(self.engine(self.publisher, "adopt"))
        require(digest_tree(self.office) == before)
        self.mirror(self.returned)
        path.write_bytes(self.original_bytes["docs/guide.txt"])  # discard only this script's test edit
        require(digest_tree(self.office) == self.original)
        self.read_ready()
        return digest_inventory(self.returned)

    def adopt(self):
        self.adopt_stamp = self.approve(self.engine(self.publisher, "adopt"), (1, 1, 1))
        require(bool(self.adopt_stamp) and digest_tree(self.office) == self.returned
                and digest_tree(self.home) == self.returned)
        old = Path(self.publisher.local_history_path) / self.adopt_stamp
        require(digest_tree(old) == {name: self.original[name] for name in ("docs/guide.txt", "removed.txt")})
        self.read_ready()
        return digest_inventory(digest_tree(self.office))

    def repeat(self):
        versions = self.HistoryService(self.publisher_client, self.publisher).list_versions()
        before = dict(self.ready)
        for task, operation in ((self.receiver, "receive"), (self.receiver, "return"), (self.publisher, "publish")):
            engine = self.engine(task, operation)
            plan = engine.preview()
            require(not plan.blocked and (plan.added, plan.updated, plan.deleted, plan.unchanged) == (0, 0, 0, 4))
            require(engine.execute(plan) == "")
            require(digest_tree(self.office) == self.returned and digest_tree(self.home) == self.returned)
            self.read_ready()
            require(self.ready == before)
        require(self.HistoryService(self.publisher_client, self.publisher).list_versions() == versions)
        require(not any((self.home / name).exists() for name in ("mirror", "versions", "work", "acceptance_rw")))
        return digest_inventory(self.returned)

    def source_integrity(self):
        require(digest_tree(self.office) == self.returned and digest_tree(self.home) == self.returned)
        # Explicitly clear ONLY the temporary receiver; the phone is its source.
        # Existing-baseline protection must refuse an unapproved destructive pull.
        for path in list(self.home.rglob("*")):
            checked = local_file(path)
            if checked.is_file():
                checked.unlink()
        require(digest_tree(self.home) == {})
        self.blocked(self.engine(self.receiver))
        require(digest_tree(self.home) == {} and digest_tree(self.office) == self.returned)
        self.mirror(self.returned)
        require(digest_tree(self.restored) == {name: self.original[name] for name in ("docs/guide.txt", "removed.txt")})
        self.read_ready()
        return digest_inventory(self.returned)

    def cleanup(self):
        control = self.control
        roots = [root for created, root in ((control.created_dataset, self.scope.dataset),
                                            (control.created_tree, self.scope.tree)) if created]
        if not roots:
            return
        for task in (self.publisher, self.receiver):
            self.scope.validate_task(task, self.root)
        # Verify EVERYTHING before the first DELETE; never repair ready/mapping or
        # remove any active/retained lock, even when its UUID was made by this run.
        for root in roots:
            control.verify_owner(root)
        require(not self.coordinator.locked(), "LOCK_HELD")
        require(self.coordinator.read() == self.ready, "METADATA_CHANGED")
        for root in roots:
            control.verify_owner(root)
            require(not self.coordinator.locked(), "LOCK_HELD")
            require(control.request("DELETE", "/" + root + "/")[0] in (200, 204), "OWNER_UNPROVEN")
            require(control.request("GET", "/" + root + "/acceptance-owner.json")[0] == 404, "OWNER_UNPROVEN")
            probe = self.publisher_client.run(["lsjson", "phone:" + root, "--stat", "--no-modtime", "--no-mimetype"])
            require(not probe.ok and probe.code in (3, 4), "OWNER_UNPROVEN")
            if root == self.scope.dataset:
                control.created_dataset = False
            else:
                control.created_tree = False


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise SafetyError("UNSAFE_REQUEST")


def main(argv=None):
    try:
        parser = SafeParser(description=__doc__, allow_abbrev=False)
        parser.add_argument("--url", required=True)
        parser.add_argument("--credentials", required=True, type=Path)
        parser.add_argument("--rclone", required=True, type=Path)
        parser.add_argument("--report", required=True, type=Path)
        args = parser.parse_args(argv)
        with ExclusiveReport(args.report) as report:
            def preflight():
                endpoint = validate_endpoint(args.url)
                executable = validate_rclone(args.rclone)
                credentials = load_credentials(args.credentials)
                return endpoint, credentials, executable
            endpoint, credentials, executable = report.check("preflight", preflight)
            with native_client(endpoint, credentials, executable, report) as acceptance:
                try:
                    for name in CHECKS[1:-1]:
                        action = acceptance.manual_return if name == "return" else getattr(acceptance, name)
                        report.check(name, action)
                finally:
                    report.check("cleanup", acceptance.cleanup)
            passed = report.finish()
        print(json.dumps({"status": "PASS" if passed else "FAIL", "safeUUID": report.run_id,
                          "transport": "http-loopback-authorized-usb-forward", "remoteRelayClaim": False}))
        return 0 if passed else 1
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    except BaseException as error:
        print(json.dumps({"status": "FAIL", "failureCode": failure_code(error), "remoteRelayClaim": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
