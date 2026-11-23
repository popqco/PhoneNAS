from __future__ import annotations

import io
import json
import os
import ssl
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.config import Config
from app.core.rclone import Rclone, Result, rclone_exe
from app.core.relay import (BaselineStore, ContentSnapshot, DatasetCoordinator,
                            FileContent, LocalDatasetLease, _unverified_ssl_context,
                            find_published_dataset, snapshot_tree)
from app.core.tasks import new_task


HELLO = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class IsolatedTest(unittest.TestCase):
    """Every public seam runs without user configuration, devices or sockets."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="PhoneNAS-relay-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("RCLONE_")}
        for key in ("APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE", "TMP", "TEMP",
                    "PHONENAS_DATA_DIR"):
            environment[key] = str(self.root)
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        tempdir = patch.object(tempfile, "tempdir", str(self.root))
        tempdir.start()
        self.addCleanup(tempdir.stop)
        network = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)


class ScriptedRclone:
    """Injected process boundary; commands and results never leave the test."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def run(self, args, timeout=120, **kwargs):
        self.calls.append(list(args))
        if not self.results:
            raise AssertionError(f"unexpected rclone operation: {args}")
        return self.results.pop(0)


class TlsContextTests(unittest.TestCase):
    def test_phone_self_signed_certificate_is_accepted_for_control_requests(self):
        context = _unverified_ssl_context()
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)


class SnapshotTests(IsolatedTest):
    def test_snapshot_is_immutable_deterministic_and_compares_real_content(self):
        supplied = {"z.txt": FileContent(5, HELLO), "a.txt": FileContent(0, EMPTY)}
        snapshot = ContentSnapshot(supplied, ("empty", "folder"))
        supplied.clear()
        same = ContentSnapshot({"a.txt": FileContent(0, EMPTY), "z.txt": FileContent(5, HELLO)},
                               ("folder", "empty"))
        self.assertEqual(snapshot.digest, same.digest)
        self.assertEqual(len(snapshot.digest), 64)
        self.assertEqual(len(snapshot.files), 2)
        with self.assertRaises(TypeError):
            snapshot.files["new"] = FileContent(0, EMPTY)
        with self.assertRaises(FrozenInstanceError):
            snapshot.files["z.txt"].size = 8
        changed = ContentSnapshot({"a.txt": FileContent(0, EMPTY), "z.txt": FileContent(5, EMPTY)})
        self.assertNotEqual(snapshot.digest, changed.digest)
        self.assertFalse(snapshot.same_content(changed))
        without_empty_dirs = ContentSnapshot(dict(same.files))
        self.assertTrue(snapshot.same_content(without_empty_dirs))
        self.assertNotEqual(snapshot.digest, without_empty_dirs.digest)

    def test_snapshot_serialization_and_paths_are_strict(self):
        snapshot = ContentSnapshot({"docs/a.txt": FileContent(5, HELLO.upper())}, ("empty",))
        payload = {"files": {"docs/a.txt": {"size": 5, "sha256": HELLO}},
                   "directories": ["empty"]}
        self.assertEqual(snapshot.to_dict(), payload)
        self.assertEqual(ContentSnapshot.from_dict(payload), snapshot)
        for bad in [None, {}, {**payload, "extra": 1},
                    {**payload, "directories": "empty"},
                    {**payload, "files": {"a": {"size": True, "sha256": HELLO}}},
                    {**payload, "files": {"a": {"size": 5, "sha256": "metadata"}}},
                    {**payload, "files": {"a": {"size": 5, "sha256": HELLO, "mtime": 1}}}]:
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                ContentSnapshot.from_dict(bad)
        for paths, directories in [(["../escape"], ()), (["CON.txt"], ()),
                                   (["a\\nb"], ()), (["A/x", "a/y"], ()),
                                   (["é", "e\u0301"], ()), (["node", "node/child"], ()),
                                   (["node"], ("node",)), ([], ("same", "same"))]:
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                ContentSnapshot({path: FileContent(0, EMPTY) for path in paths}, directories)

    def test_snapshot_uses_sha256_listing_and_download_hashes_when_unavailable(self):
        entries = [{"Path": "docs", "IsDir": True, "Size": -1},
                   {"Path": "docs/a.txt", "IsDir": False, "Size": 5,
                    "Hashes": {"SHA-256": HELLO.upper()}},
                   {"Path": "b.txt", "IsDir": False, "Size": 0, "Hashes": {}}]
        client = ScriptedRclone(Result(True, json.dumps(entries), "", 0),
                                Result(True, f"{EMPTY}  b.txt\n{HELLO}  docs/a.txt\n", "", 0),
                                Result(True, json.dumps(entries), "", 0))
        snapshot = snapshot_tree(client, "phone:backup/data", ("*.tmp", "build/**"))
        self.assertEqual(snapshot.files, {"docs/a.txt": FileContent(5, HELLO),
                                          "b.txt": FileContent(0, EMPTY)})
        self.assertEqual(snapshot.directories, ("docs",))
        listing, hashes, verified_listing = client.calls
        self.assertEqual(verified_listing[:2], listing[:2])
        self.assertEqual(listing[:2], ["lsjson", "phone:backup/data"])
        for option in ("--recursive", "--no-modtime", "--no-mimetype", "--hash-type"):
            self.assertIn(option, listing)
        self.assertEqual(listing[listing.index("--hash-type") + 1], "SHA-256")
        self.assertEqual(hashes[:3], ["hashsum", "SHA256", "phone:backup/data"])
        self.assertIn("--download", hashes)
        self.assertEqual(listing[-4:], ["--exclude", "*.tmp", "--exclude", "build/**"])
        self.assertEqual(hashes[-4:], listing[-4:])
        client = ScriptedRclone(Result(True, json.dumps(entries[:2]), "", 0))
        self.assertEqual(snapshot_tree(client, "phone:backup/data").files["docs/a.txt"].sha256, HELLO)
        self.assertEqual(len(client.calls), 1)

    def test_missing_unreadable_or_invalid_inventory_never_becomes_empty(self):
        client = ScriptedRclone(Result(False, "", "directory not found", 3))
        self.assertIsNone(snapshot_tree(client, "phone:missing", allow_missing=True))
        for code in (-1, 1, 2, 3, 4, 5):
            with self.subTest(code=code):
                client = ScriptedRclone(Result(False, "", "directory not found", code))
                with self.assertRaises(RuntimeError):
                    snapshot_tree(client, "phone:missing", allow_missing=code != 3)
        for text in ("not json", "{}", '[{"Path":"a","IsDir":false,"Size":0,"Size":1}]',
                     "[NaN]", '[{"Path":"a","IsDir":false,"Size":-1}]'):
            with self.subTest(text=text), self.assertRaises((RuntimeError, ValueError)):
                snapshot_tree(ScriptedRclone(Result(True, text, "", 0)), "phone:data")
        entries = [{"Path": "a.txt", "IsDir": False, "Size": 5, "Hashes": {}}]
        for hashes in ("", f"{HELLO}  wrong.txt\n", f"{HELLO}  a.txt\n{HELLO}  a.txt\n",
                       "not-a-real-content-hash  a.txt\n"):
            with self.subTest(hashes=hashes), self.assertRaises(ValueError):
                snapshot_tree(ScriptedRclone(Result(True, json.dumps(entries), "", 0),
                                             Result(True, hashes, "", 0)), "phone:data")
        event = threading.Event()
        event.set()
        client = ScriptedRclone()
        with self.assertRaisesRegex(RuntimeError, "取消"):
            snapshot_tree(client, "phone:data", cancelled=event)
        self.assertEqual(client.calls, [])
        class CancelDuringRead(ScriptedRclone):
            def run(inner, args, timeout=120, **kwargs):
                event.set()
                return Result(True, "[]", "", 0)
        event.clear()
        with self.assertRaisesRegex(RuntimeError, "取消"):
            snapshot_tree(CancelDuringRead(), "phone:data", cancelled=event)

    def test_hashsum_delimiters_do_not_split_portable_unicode_filenames(self):
        path = "notes\u2028and-more.txt"
        entry = {"Path": path, "IsDir": False, "Size": 5}
        client = ScriptedRclone(Result(True, json.dumps([entry]), "", 0),
                                Result(True, f"{HELLO}  {path}\r\n", "", 0),
                                Result(True, json.dumps([entry]), "", 0))
        self.assertEqual(snapshot_tree(client, "phone:data").files[path], FileContent(5, HELLO))

    def test_download_hash_fallback_preserves_unicode_and_rejects_line_injection(self):
        for path in ("private-\ue000.txt", "unassigned-\u0378.txt", "join-\u200c.txt",
                     "emoji-👩\u200d💻.txt", "paragraph-\u2029.txt"):
            with self.subTest(path=ascii(path)):
                entry = {"Path": path, "IsDir": False, "Size": 5}
                client = ScriptedRclone(Result(True, json.dumps([entry]), "", 0),
                                        Result(True, f"{HELLO}  {path}\n", "", 0),
                                        Result(True, json.dumps([entry]), "", 0))
                self.assertEqual(snapshot_tree(client, "phone:data").files[path], FileContent(5, HELLO))
        for path in ("bad\nname.txt", "bad\rname.txt", "bad\u202ename.txt"):
            with self.subTest(path=ascii(path)):
                entry = {"Path": path, "IsDir": False, "Size": 5}
                client = ScriptedRclone(Result(True, json.dumps([entry]), "", 0))
                with self.assertRaises(ValueError):
                    snapshot_tree(client, "phone:data")
                self.assertEqual(len(client.calls), 1)

    def test_download_hash_fallback_rechecks_inventory_after_reading_bytes(self):
        entries = [{"Path": "a.txt", "IsDir": False, "Size": 5, "Hashes": {}}]
        for verified in ([], [{**entries[0], "Size": 6}], [{**entries[0], "Path": "other.txt"}]):
            with self.subTest(verified=verified), self.assertRaises((RuntimeError, ValueError)):
                snapshot_tree(ScriptedRclone(Result(True, json.dumps(entries), "", 0),
                                             Result(True, f"{HELLO}  a.txt\n", "", 0),
                                             Result(True, json.dumps(verified), "", 0)), "phone:data")

    def test_local_reparse_points_are_rejected_before_rclone_without_link_privileges(self):
        source = self.root / "source"
        source.mkdir()
        nested = source / "ignored"
        nested.mkdir()
        real_lstat = Path.lstat
        def marked(path, *args, **kwargs):
            if path == nested:
                return SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)
        # DirEntry.stat is the filesystem boundary for nested junction checks.
        class Entry:
            path = str(nested)
            def stat(inner, follow_symlinks=True):
                self.assertFalse(follow_symlinks)
                return marked(nested)
        class Entries:
            def __enter__(inner):
                return iter([Entry()])
            def __exit__(inner, *args):
                pass
        client = ScriptedRclone()
        with patch("app.core.relay.os.scandir", return_value=Entries()):
            with self.assertRaises(ValueError):
                snapshot_tree(client, str(source), ("ignored/**",))
        with patch.object(Path, "lstat", marked):
            with self.assertRaises(ValueError):
                snapshot_tree(client, str(nested))
        self.assertEqual(client.calls, [])

    def test_snapshot_forwards_cancellation_to_running_rclone(self):
        event = threading.Event()
        seen = []
        class Client(ScriptedRclone):
            def run(inner, args, timeout=120, **kwargs):
                seen.append(kwargs.get("cancel_event"))
                return Result(True, "[]", "", 0)
        snapshot_tree(Client(), "phone:data", cancelled=event)
        self.assertEqual(seen, [event])
        callback = lambda: event.is_set()
        snapshot_tree(Client(), "phone:data", cancelled=callback)
        self.assertFalse(seen[-1].is_set())
        event.set()
        self.assertTrue(seen[-1].is_set())

    def test_local_nested_link_is_rejected_even_when_excluded(self):
        source = self.root / "source"
        source.mkdir()
        target = self.root / "outside"
        target.mkdir()
        link = source / "ignored"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("requires symlink permission")
        client = ScriptedRclone()
        with self.assertRaises(ValueError):
            snapshot_tree(client, str(source), ("ignored/**",))
        self.assertEqual(client.calls, [])


class BaselineTests(IsolatedTest):
    def test_baseline_roundtrip_is_private_atomic_and_bound_to_task_scope(self):
        task = new_task(str(self.root / "source"))
        config = SimpleNamespace(url="https://example.invalid/dav", username="alice")
        state = self.root / "state"
        snapshot = ContentSnapshot({"a.txt": FileContent(5, HELLO)}, ("empty",))
        generation = "3d096a03-4ea0-4160-8eeb-bc2ee7425b15"
        store = BaselineStore(state, task, config)
        self.assertIsNone(store.load())
        self.assertFalse(state.exists())
        store.save(snapshot, generation)
        self.assertEqual(store.load(), (snapshot, generation))
        self.assertEqual(list(state.iterdir()), [state / f"{task.id}.json"])
        original = (state / f"{task.id}.json").read_bytes()
        rearchived = replace(task, local_history_path=str(self.root / "new-history"))
        self.assertEqual(BaselineStore(state, rearchived, config).load(), (snapshot, generation))
        changes = {"remote_path": "other/mirror", "versions_path": "other/history",
                   "local_path": str(self.root / "other"),
                   "dataset_id": "fedcba9876543210fedcba9876543210",
                   "sync_identity": "0123456789abcdef0123456789abcdef",
                   "excludes": ("*.tmp",), "direction": "download"}
        for key, value in changes.items():
            with self.subTest(key=key):
                self.assertIsNone(BaselineStore(state, replace(task, **{key: value}), config).load())
        for changed_config in (SimpleNamespace(url="https://other.invalid", username="alice"),
                               SimpleNamespace(url=config.url, username="bob")):
            self.assertIsNone(BaselineStore(state, task, changed_config).load())
        self.assertEqual((state / f"{task.id}.json").read_bytes(), original)
        with patch("app.core.relay.os.replace", side_effect=OSError("failed atomic replace")):
            with self.assertRaises(OSError):
                store.save(ContentSnapshot({}), generation)
        self.assertEqual(store.load(), (snapshot, generation))
        self.assertEqual(list(state.iterdir()), [state / f"{task.id}.json"])

    def test_baseline_corruption_and_linked_state_fail_closed(self):
        task = new_task(str(self.root / "source"))
        config = SimpleNamespace(url="https://example.invalid/dav", username="alice")
        state = self.root / "state"
        store = BaselineStore(state, task, config)
        generation = "3d096a03-4ea0-4160-8eeb-bc2ee7425b15"
        store.save(ContentSnapshot({"a": FileContent(5, HELLO)}), generation)
        path = state / f"{task.id}.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for raw in ("not json", "{}", json.dumps({**original, "generation": "not-uuid"}),
                    json.dumps({**original, "content_digest": EMPTY}),
                    json.dumps({**original, "binding": {**original["binding"], "excludes": [1]}})):
            path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw[:50]), self.assertRaises(RuntimeError):
                store.load()
        path.write_text(json.dumps(original), encoding="utf-8")
        real_lstat = Path.lstat
        def linked(candidate, *args, **kwargs):
            if candidate == state:
                return SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400)
            return real_lstat(candidate, *args, **kwargs)
        with patch.object(Path, "lstat", linked):
            with self.assertRaises((RuntimeError, ValueError)):
                store.load()
            with self.assertRaises((RuntimeError, ValueError)):
                store.save(ContentSnapshot({}), generation)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)


class PublishedDatasetTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.task = replace(new_task(str(self.root / "source"), remote_path="Backup/Café",
                                     versions_path="History/保存"), excludes=("z/**", "*.tmp", "a/**"))
        self.state = {"version": 2, "dataset_id": self.task.dataset_id,
                      "remote_path": self.task.remote_path, "versions_path": self.task.versions_path,
                      "excludes": list(self.task.excludes), "phase": "ready",
                      "generation": "3d096a03-4ea0-4160-8eeb-bc2ee7425b15",
                      "content_digest": HELLO, "writer_identity": self.task.sync_identity}

    @staticmethod
    def _record(state):
        raw = json.dumps(state)
        stat = {"Name": "state.json", "Path": "state.json", "IsDir": False,
                "Size": len(raw.encode("utf-8"))}
        return Result(True, json.dumps(stat), "", 0), Result(True, raw, "", 0)

    @staticmethod
    def _listing(*entries):
        return Result(True, json.dumps(entries), "", 0)

    def test_canonical_record_is_read_exactly_and_not_preferred_over_another_owner(self):
        for version in (1, 2):
            with self.subTest(version=version):
                state = {**self.state, "version": version}
                listing = self._listing({"Path": self.task.dataset_id, "IsDir": True})
                client = ScriptedRclone(*self._record(state), listing)
                self.assertEqual(find_published_dataset(client, self.task.remote_path), state)
                stat, cat, _ = client.calls
                target = f"phone:{self.task.dataset_control_path}/state.json"
                self.assertEqual(stat[:2], ["lsjson", target])
                self.assertIn("--stat", stat)
                self.assertEqual(cat[:2], ["cat", target])
                self.assertEqual(cat[cat.index("--head") + 1], "65537")
        other = {**self.state, "dataset_id": "f" * 32}
        listing = self._listing({"Path": other["dataset_id"], "IsDir": True})
        client = ScriptedRclone(*self._record(self.state), listing, *self._record(other))
        with self.assertRaisesRegex(RuntimeError, "多个数据集"):
            find_published_dataset(client, self.task.remote_path)

    def test_missing_canonical_record_scans_only_sorted_exact_uuid_directories(self):
        first, match, last = (char * 32 for char in "abc")
        unrelated = {**self.state, "dataset_id": first, "remote_path": "elsewhere/mirror"}
        explicit = {**self.state, "dataset_id": match}
        for code in (3, 4):
            with self.subTest(code=code):
                missing = Result(False, "", "missing", code)
                listing = self._listing(
                    {"Path": last, "IsDir": True},
                    {"Path": match, "Name": match, "IsDir": True},
                    {"Path": "e" * 32, "IsDir": False},
                    {"Path": self.task.dataset_id, "IsDir": True},
                    {"Path": first, "IsDir": True},
                )
                client = ScriptedRclone(missing, listing, *self._record(unrelated),
                                         *self._record(explicit), missing)
                self.assertEqual(find_published_dataset(client, self.task.remote_path), explicit)
                root = "phone:.phonenas/datasets"
                self.assertEqual([call[:2] for call in client.calls], [
                    ["lsjson", f"{root}/{self.task.dataset_id}/state.json"],
                    ["lsjson", root],
                    ["lsjson", f"{root}/{first}/state.json"],
                    ["cat", f"{root}/{first}/state.json"],
                    ["lsjson", f"{root}/{match}/state.json"],
                    ["cat", f"{root}/{match}/state.json"],
                    ["lsjson", f"{root}/{last}/state.json"],
                ])
                self.assertIn("--dirs-only", client.calls[1])
                self.assertNotIn("--recursive", client.calls[1])
                self.assertEqual(client.calls[1][client.calls[1].index("--max-depth") + 1], "1")

    def test_input_must_be_exact_normalized_nonreserved_mirror(self):
        for invalid in (None, "", "/", " Backup/Café", "Backup/Café/", "Backup//Café",
                        "Backup\\Café", "phone:Backup/Café", "Backup/../mirror", "Backup/CON",
                        ".PHONENAS/datasets", "Backup/.PhoneNAS/mirror"):
            with self.subTest(invalid=invalid):
                client = ScriptedRclone()
                with self.assertRaises(ValueError):
                    find_published_dataset(client, invalid)
                self.assertEqual(client.calls, [])

    def test_present_canonical_state_must_be_exact_ready_and_well_formed(self):
        for change in ({"remote_path": "backup/Café"}, {"remote_path": "Backup/Cafe\u0301"},
                       {"remote_path": "Other/mirror"}, {"versions_path": "History//保存"},
                       {"versions_path": "History/.PhoneNAS/保存"}, {"versions_path": "Backup"},
                       {"versions_path": "backup/Other"}, {"excludes": "*.tmp"},
                       {"excludes": ["a", "bad\nrule"]}, {"excludes": [3]}, {"excludes": [""]},
                       {"dataset_id": self.task.dataset_id.upper()}, {"dataset_id": "bad"},
                       {"dataset_id": "a" * 32}, {"generation": "bad"}, {"writer_identity": "bad"},
                       {"version": True}, {"version": 3}, {"phase": "publishing"},
                       {"phase": "interrupted"}, {"phase": "unknown"}, {"content_digest": ""},
                       {"content_digest": HELLO.upper()}, {"content_digest": "abcdef"}, {"extra": 1}):
            with self.subTest(change=change):
                client = ScriptedRclone(*self._record({**self.state, **change}), self._listing())
                with self.assertRaises(RuntimeError):
                    find_published_dataset(client, self.task.remote_path)
                self.assertEqual([call[0] for call in client.calls[:2]], ["lsjson", "cat"])

        for payload in (None, {key: value for key, value in self.state.items() if key != "content_digest"}):
            with self.subTest(payload=payload):
                client = ScriptedRclone(*self._record(payload), self._listing())
                with self.assertRaises(RuntimeError):
                    find_published_dataset(client, self.task.remote_path)

    def test_fallback_does_not_pick_duplicates_aliases_or_unreadable_candidates(self):
        first, second = "a" * 32, "b" * 32
        explicit = {**self.state, "dataset_id": first}
        second_state = {**self.state, "dataset_id": second}
        missing = Result(False, "", "missing", 3)
        listing = self._listing({"Path": second, "IsDir": True}, {"Path": first, "IsDir": True})
        for results in (self._record(second_state),
                        self._record({**second_state, "remote_path": "backup/Café"}),
                        self._record({**second_state, "remote_path": "Backup/Cafe\u0301"}),
                        self._record({**second_state, "phase": "interrupted"}),
                        self._record({**second_state, "dataset_id": first}),
                        (Result(False, "", "authentication failed", 1),)):
            with self.subTest(results=results):
                client = ScriptedRclone(missing, listing, *self._record(explicit), *results)
                with self.assertRaises(RuntimeError):
                    find_published_dataset(client, self.task.remote_path)
        for alias in ("backup/Café", "Backup/Cafe\u0301"):
            with self.subTest(alias=alias):
                only_alias = self._listing({"Path": first, "IsDir": True})
                client = ScriptedRclone(missing, only_alias, *self._record({**explicit, "remote_path": alias}))
                with self.assertRaises(RuntimeError):
                    find_published_dataset(client, self.task.remote_path)
        duplicate = self._listing(*[{"Path": first, "IsDir": True}] * 2)
        client = ScriptedRclone(missing, duplicate)
        with self.assertRaises(RuntimeError):
            find_published_dataset(client, self.task.remote_path)
        self.assertEqual(len(client.calls), 2)
        for path, name in (("../../escape", "escape"), ("F" * 32, "F" * 32), (first, "../elsewhere")):
            with self.subTest(path=path, name=name):
                listing = self._listing({"Path": path, "Name": name, "IsDir": True})
                client = ScriptedRclone(missing, listing)
                with self.assertRaisesRegex(RuntimeError, "不安全"):
                    find_published_dataset(client, self.task.remote_path)
                self.assertEqual(len(client.calls), 2)

    def test_absence_is_not_inferred_from_failed_reads_or_invalid_json(self):
        missing = Result(False, "", "missing", 3)
        denied = Result(False, "", "not found but unauthorized", 1)
        for listing in (missing, Result(False, "", "missing", 4), self._listing()):
            with self.subTest(listing=listing):
                self.assertIsNone(find_published_dataset(ScriptedRclone(missing, listing), self.task.remote_path))
        for results in ((denied,), (missing, denied), (missing, Result(True, "null", "", 0)),
                        (self._record(self.state)[0], Result(False, "", "disappeared", 4)),
                        (self._record(self.state)[0], Result(True, '{"phase":"ready","phase":"ready"}', "", 0))):
            with self.subTest(results=results):
                client = ScriptedRclone(*results)
                with self.assertRaises(RuntimeError):
                    find_published_dataset(client, self.task.remote_path)
                self.assertEqual(len(client.calls), len(results))

    def test_remote_read_and_scan_limits_fail_closed(self):
        stat, _ = self._record(self.state)
        for change in ({"IsDir": True}, {"Size": True}, {"Size": -1}, {"Size": 65537},
                       {"Path": "STATE.JSON"}):
            with self.subTest(change=change):
                bad_stat = Result(True, json.dumps({**json.loads(stat.out), **change}), "", 0)
                client = ScriptedRclone(bad_stat)
                with self.assertRaises(RuntimeError):
                    find_published_dataset(client, self.task.remote_path)
                self.assertEqual(len(client.calls), 1)
        # rclone output is checked in bytes even if stat underreported the size.
        oversized = Result(True, "保" * 21846, "", 0)
        with self.assertRaisesRegex(RuntimeError, "大小"):
            find_published_dataset(ScriptedRclone(stat, oversized), self.task.remote_path)
        missing = Result(False, "", "missing", 4)
        with self.assertRaisesRegex(RuntimeError, "大小"):
            find_published_dataset(ScriptedRclone(missing, oversized), self.task.remote_path)
        listing = self._listing(*({"Path": char * 32, "IsDir": True} for char in "abc"))
        client = ScriptedRclone(missing, listing)
        with patch("app.core.relay._DATASET_SCAN_LIMIT", 2), self.assertRaisesRegex(RuntimeError, "数量"):
            find_published_dataset(client, self.task.remote_path)
        self.assertEqual(len(client.calls), 2)

    def test_cancellation_stops_pending_and_in_flight_reads(self):
        event = threading.Event()
        event.set()
        client = ScriptedRclone()
        with self.assertRaisesRegex(RuntimeError, "取消"):
            find_published_dataset(client, self.task.remote_path, cancelled=event)
        self.assertEqual(client.calls, [])

        class CancelOnLastRead(ScriptedRclone):
            def run(inner, args, timeout=120, **kwargs):
                probe = kwargs["cancel_event"]
                self.assertFalse(probe.is_set())
                result = super().run(args, timeout, **kwargs)
                if not inner.results:
                    event.set()
                    self.assertTrue(probe.is_set())
                return result

        missing = Result(False, "", "missing", 3)
        for results in ((missing,), self._record(self.state), (missing, self._listing())):
            with self.subTest(results=results):
                event.clear()
                client = CancelOnLastRead(*results)
                with self.assertRaisesRegex(RuntimeError, "取消"):
                    find_published_dataset(client, self.task.remote_path, cancelled=event.is_set)
                self.assertEqual(len(client.calls), len(results))


class CoordinatorTests(IsolatedTest):
    def test_coordinator_state_roundtrips_only_inside_control_scope(self):
        task = new_task(str(self.root / "source"))
        generation = "3d096a03-4ea0-4160-8eeb-bc2ee7425b15"
        uploads = []
        class StateClient(ScriptedRclone):
            def run(inner, args, timeout=120, **kwargs):
                if args[0] == "copyto":
                    uploads.append(json.loads(Path(args[1]).read_text(encoding="utf-8")))
                return super().run(args, timeout, **kwargs)
        client = StateClient(Result(False, "", "missing", 4), Result(True, "", "", 0),
                             Result(True, "", "", 0))
        coordinator = DatasetCoordinator(client, task)
        self.assertIsNone(coordinator.read())
        self.assertEqual(client.calls[0][0], "cat")
        self.assertIn("--head", client.calls[0])
        coordinator.write("ready", generation, HELLO)
        self.assertEqual(client.calls[1], ["mkdir", f"phone:{task.dataset_control_path}"])
        self.assertEqual(client.calls[2][0], "copyto")
        self.assertEqual(client.calls[2][-1], "--ignore-times")
        self.assertEqual(client.calls[2][2], f"phone:{task.dataset_control_path}/state.json")
        state = uploads[0]
        self.assertEqual(state["phase"], "ready")
        self.assertEqual(state["generation"], generation)
        self.assertEqual(state["content_digest"], HELLO)
        self.assertEqual(state["writer_identity"], task.sync_identity)
        client.results.append(Result(True, json.dumps(state), "", 0))
        self.assertEqual(coordinator.read(), state)
        client.results.append(Result(True, json.dumps({**state, "writer_identity": generation}), "", 0))
        self.assertEqual(coordinator.read()["writer_identity"], generation)
        self.assertFalse(Path(client.calls[2][1]).exists())
        self.assertFalse(any(task.remote_path == value for call in client.calls for value in call))

    def test_metadata_scope_malformed_and_network_errors_fail_closed_without_writes(self):
        task = new_task(str(self.root / "source"))
        valid = {"version": 1, "dataset_id": task.dataset_id, "remote_path": task.remote_path,
                 "versions_path": task.versions_path, "excludes": list(task.excludes),
                 "generation": "3d096a03-4ea0-4160-8eeb-bc2ee7425b15", "phase": "ready"}
        for key, value in (("version", True), ("dataset_id", "0123456789abcdef0123456789abcdef"),
                           ("remote_path", "other"), ("versions_path", "../escape"),
                           ("excludes", []), ("generation", "bad"), ("phase", "unknown"),
                           ("content_digest", "mtime-size"), ("writer_identity", "bad")):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                client = ScriptedRclone(Result(True, json.dumps({**valid, key: value}), "", 0))
                DatasetCoordinator(client, task).read()
                self.assertEqual(len(client.calls), 1)
        for result in (Result(False, "", "not found but auth failed", 1),
                       Result(True, "{}", "", 0), Result(True, " " * 65537, "", 0)):
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                DatasetCoordinator(ScriptedRclone(result), task).read()
        client = ScriptedRclone(Result(False, "", "missing", 3), Result(True, "{}", "", 0),
                                Result(False, "", "missing", 4), Result(False, "", "auth failed", 1))
        coordinator = DatasetCoordinator(client, task)
        self.assertFalse(coordinator.locked())
        self.assertTrue(coordinator.locked())
        self.assertFalse(coordinator.locked())
        with self.assertRaises(RuntimeError):
            coordinator.locked()
        self.assertTrue(all(call[0] == "lsjson" and "--stat" in call for call in client.calls))
        for bad_root in (".phonenas", ".PHONENAS/payload", "../escape", "safe/CON", "node "):
            with self.subTest(root=bad_root), self.assertRaises(ValueError):
                DatasetCoordinator(ScriptedRclone(), replace(task, remote_path=bad_root))

    def test_foreign_lock_owner_is_never_removed_on_context_exit(self):
        task = new_task(str(self.root / "source"))
        remote = self.root / "remote"
        remote.mkdir()
        coordinator = DatasetCoordinator(ScriptedRclone(), task,
                                         lease_factory=lambda: LocalDatasetLease(remote, task))
        lock = remote / ".phonenas" / "locks" / task.dataset_id
        with self.assertRaisesRegex(RuntimeError, "所有权"):
            with coordinator.lease():
                owner_path = lock / "owner.json"
                owner = json.loads(owner_path.read_text(encoding="utf-8"))
                owner["token"] = "0123456789abcdef0123456789abcdef"
                owner_path.write_text(json.dumps(owner), encoding="utf-8")
        self.assertTrue(lock.exists())
        self.assertEqual(json.loads(owner_path.read_text())["token"], owner["token"])

    def test_offline_leases_are_exclusive_retainable_and_owner_checked(self):
        task = new_task(str(self.root / "source"))
        remote = self.root / "remote"
        remote.mkdir()
        client = ScriptedRclone()
        coordinator = DatasetCoordinator(client, task,
                                         lease_factory=lambda: LocalDatasetLease(remote, task))
        lock = remote / ".phonenas" / "locks" / task.dataset_id
        with coordinator.lease() as lease:
            owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
            self.assertEqual(owner["dataset_id"], task.dataset_id)
            self.assertEqual(owner["writer_identity"], task.sync_identity)
            with self.assertRaises(RuntimeError):
                with coordinator.lease():
                    self.fail("another owner entered the dataset")
        self.assertFalse(lock.exists())
        with self.assertRaisesRegex(RuntimeError, "failed precheck"):
            with coordinator.lease():
                raise RuntimeError("failed precheck")
        self.assertFalse(lock.exists())
        with coordinator.lease() as lease:
            lease.retain()
        self.assertTrue(lock.exists())
        with self.assertRaises(RuntimeError):
            with coordinator.lease():
                pass
        self.assertEqual(client.calls, [])

    def test_explicit_recovery_requires_exact_owner_token_and_excludes_concurrent_recovery(self):
        task = new_task(str(self.root / "source"))
        remote = self.root / "remote"
        remote.mkdir()
        coordinator = DatasetCoordinator(ScriptedRclone(), task,
                                         lease_factory=lambda: LocalDatasetLease(remote, task))
        lock = remote / ".phonenas" / "locks" / task.dataset_id
        with coordinator.lease() as original:
            token = original.token
            original.retain()
        with self.assertRaises(RuntimeError):
            with coordinator.recovery_lease("0123456789abcdef0123456789abcdef"):
                self.fail("wrong acquisition token authorized recovery")
        wrong_identity = replace(task, sync_identity="fedcba9876543210fedcba9876543210")
        other = DatasetCoordinator(ScriptedRclone(), wrong_identity,
                                    lease_factory=lambda: LocalDatasetLease(remote, wrong_identity))
        with self.assertRaises(RuntimeError):
            with other.recovery_lease(token):
                self.fail("another PC authorized recovery")
        with self.assertRaisesRegex(RuntimeError, "recovery failed"):
            with coordinator.recovery_lease(token) as recovering:
                self.assertEqual(recovering.token, token)
                with self.assertRaises(RuntimeError):
                    with coordinator.recovery_lease(token):
                        self.fail("simultaneous recovery authorized")
                raise RuntimeError("recovery failed")
        self.assertTrue(lock.exists())
        self.assertFalse((lock / "recovery").exists())
        with coordinator.recovery_lease(token) as recovering:
            recovering.retain()
        self.assertTrue(lock.exists())
        self.assertFalse((lock / "recovery").exists())
        with coordinator.recovery_lease(token):
            pass
        self.assertFalse(lock.exists())
        with self.assertRaises(RuntimeError):
            with coordinator.recovery_lease(token):
                self.fail("missing interrupted lock was recreated")
        self.assertFalse(lock.exists())

    def test_webdav_lease_uses_exclusive_mkcol_and_uncancelled_owner_checked_cleanup(self):
        task = new_task(str(self.root / "source"))
        client = ScriptedRclone(Result(True, "", "", 0))
        client.config = SimpleNamespace(url="https://example.invalid/base%20folder",
                                        username="alice", password="test-only")
        requests = []
        owner = {}
        class Response(io.BytesIO):
            def __init__(inner, status, raw=b""):
                super().__init__(raw)
                inner.status = status
        class Transport:
            def open(inner, request, timeout):
                requests.append(request)
                self.assertEqual(timeout, 15)
                self.assertTrue(request.get_header("Authorization").startswith("Basic "))
                if request.method == "MKCOL":
                    return Response(201)
                if request.method == "PUT":
                    owner.update(json.loads(request.data))
                    return Response(201)
                if request.method == "GET":
                    return Response(200, json.dumps(owner).encode("utf-8"))
                if request.method == "DELETE":
                    return Response(204)
                self.fail("unexpected HTTP method")
        with patch("urllib.request.build_opener", return_value=Transport()) as builder:
            with DatasetCoordinator(client, task).lease():
                client.results.clear()  # Cleanup cannot use a cancelled rclone instance.
        self.assertEqual(client.calls, [["mkdir", "phone:.phonenas/locks"]])
        self.assertEqual([request.method for request in requests], ["MKCOL", "PUT", "GET", "DELETE"])
        self.assertEqual(requests[0].full_url,
                         f"https://example.invalid/base%20folder/.phonenas/locks/{task.dataset_id}/")
        self.assertEqual(owner["writer_identity"], task.sync_identity)
        self.assertEqual(owner["dataset_id"], task.dataset_id)
        self.assertIn("token", owner)
        self.assertTrue(builder.called)

    def test_webdav_existing_lock_redirect_and_foreign_token_never_authorize_delete(self):
        task = new_task(str(self.root / "source"))
        config = SimpleNamespace(url="https://example.invalid/dav", username="alice", password="test-only")
        class Response(io.BytesIO):
            def __init__(inner, status, data=b""):
                super().__init__(data)
                inner.status = status
        for status in (200, 204, 301, 302, 307, 308, 401, 405, 500):
            seen = []
            class Transport:
                def open(inner, request, timeout):
                    seen.append(request.method)
                    return Response(status)
            client = ScriptedRclone(Result(True, "", "", 0))
            client.config = config
            with self.subTest(status=status), patch("urllib.request.build_opener", return_value=Transport()):
                with self.assertRaises(RuntimeError):
                    with DatasetCoordinator(client, task).lease():
                        self.fail("non-201 MKCOL authorized access")
                self.assertEqual(seen, ["MKCOL"])
        seen = []
        class ForeignOwner:
            def open(inner, request, timeout):
                seen.append(request.method)
                if request.method in ("MKCOL", "PUT"):
                    return Response(201)
                return Response(200, b'{"token":"someone-else"}')
        client = ScriptedRclone(Result(True, "", "", 0))
        client.config = config
        with patch("urllib.request.build_opener", return_value=ForeignOwner()):
            with self.assertRaises(RuntimeError):
                with DatasetCoordinator(client, task).lease():
                    pass
        self.assertEqual(seen, ["MKCOL", "PUT", "GET"])

    def test_webdav_recovery_is_explicit_exact_token_bound_and_serialized(self):
        task = new_task(str(self.root / "source"))
        token = "3d096a034ea041608eebbc2ee7425b15"
        config = SimpleNamespace(url="https://example.invalid/dav", username="alice", password="test-only")
        client = ScriptedRclone()
        client.config = config
        calls = []
        owners = {"primary": {"version": 1, "dataset_id": task.dataset_id,
                              "writer_identity": task.sync_identity, "token": token}}
        class Response(io.BytesIO):
            def __init__(inner, status, data=b""):
                super().__init__(data)
                inner.status = status
        class Transport:
            def open(inner, request, timeout):
                calls.append((request.method, request.full_url))
                child = "/recovery/" in request.full_url
                key = "child" if child else "primary"
                if request.method == "GET":
                    return Response(200, json.dumps(owners.get(key, {})).encode("utf-8"))
                if request.method == "MKCOL":
                    if "child" in owners:
                        return Response(405)
                    owners["child"] = {}
                    return Response(201)
                if request.method == "PUT":
                    owners[key] = json.loads(request.data)
                    return Response(201)
                if request.method == "DELETE":
                    if child:
                        owners.pop("child", None)
                    else:
                        owners.clear()
                    return Response(204)
                self.fail("unexpected recovery request")
        coordinator = DatasetCoordinator(client, task)
        with patch("urllib.request.build_opener", return_value=Transport()):
            with self.assertRaises(RuntimeError):
                with coordinator.recovery_lease("0123456789abcdef0123456789abcdef"):
                    self.fail("wrong token authorized HTTP recovery")
            with self.assertRaisesRegex(RuntimeError, "precheck"):
                with coordinator.recovery_lease(token):
                    with self.assertRaises(RuntimeError):
                        with coordinator.recovery_lease(token):
                            self.fail("concurrent HTTP recovery")
                    raise RuntimeError("precheck")
            self.assertEqual(set(owners), {"primary"})
            with coordinator.recovery_lease(token):
                pass
        self.assertEqual(owners, {})
        self.assertEqual(client.calls, [])
        self.assertTrue(any(method == "MKCOL" and "/recovery/" in url for method, url in calls))


@unittest.skipUnless(rclone_exe().is_file(), "requires bundled rclone (local alias only)")
class LocalAliasTests(IsolatedTest):
    def test_actual_hashes_empty_dirs_state_and_lock_probe_stay_in_temporary_alias(self):
        remote = self.root / "fake-phone"
        remote.mkdir()
        conf = self.root / "rclone.conf"
        conf.write_text(f"[phone]\ntype = alias\nremote = {remote.as_posix()}\n", encoding="utf-8")
        client = Rclone(Config(self.root / "config.json"), conf_path=conf)
        task = new_task(str(self.root / "local"), remote_path="backup/mirror",
                        versions_path="backup/history")
        source = remote / task.remote_path
        source.mkdir(parents=True)
        (source / "a.txt").write_bytes(b"hello")
        (source / "ignored.tmp").write_bytes(b"ignored")
        (source / "empty").mkdir()
        initial = snapshot_tree(client, f"phone:{task.remote_path}", ("*.tmp",))
        self.assertEqual(initial.files, {"a.txt": FileContent(5, HELLO)})
        self.assertIn("empty", initial.directories)
        unchanged_time = (source / "a.txt").stat().st_mtime_ns
        (source / "a.txt").write_bytes(b"HELLO")
        os.utime(source / "a.txt", ns=(unchanged_time, unchanged_time))
        changed = snapshot_tree(client, f"phone:{task.remote_path}", ("*.tmp",))
        self.assertFalse(initial.same_content(changed))
        self.assertNotEqual(initial.digest, changed.digest)
        self.assertIsNone(snapshot_tree(client, "phone:missing", allow_missing=True))
        coordinator = DatasetCoordinator(client, task,
                                         lease_factory=lambda: LocalDatasetLease(remote, task))
        self.assertIsNone(coordinator.read())
        self.assertFalse(coordinator.locked())
        self.assertFalse((remote / ".phonenas").exists())
        with coordinator.lease():
            self.assertTrue(coordinator.locked())
            coordinator.write("ready", "3d096a03-4ea0-4160-8eeb-bc2ee7425b15", changed.digest)
            self.assertEqual(coordinator.read()["content_digest"], changed.digest)
        self.assertFalse(coordinator.locked())
        self.assertEqual((source / "a.txt").read_bytes(), b"HELLO")
        self.assertEqual((source / "ignored.tmp").read_bytes(), b"ignored")
        self.assertEqual(sorted(item.name for item in source.iterdir()), ["a.txt", "empty", "ignored.tmp"])


if __name__ == "__main__":
    unittest.main()
