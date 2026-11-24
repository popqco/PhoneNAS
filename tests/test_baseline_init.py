from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from app.core.config import Config
from app.core.rclone import Rclone, rclone_exe
from app.core.relay import (BaselineStore, DatasetCoordinator, FileContent,
                            LocalDatasetLease)
from app.core.sync import SyncEngine
from app.core.tasks import new_task


HELLO = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


class RecordingRclone(Rclone):
    """Real temporary alias, with a hard boundary against payload mutations."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []
        self.intercept = None

    def run(self, args, *positional, **kwargs):
        self.calls.append(list(args))
        control = "phone:.phonenas/"
        allowed = (args[0] in ("lsjson", "hashsum", "cat")
                   or args[0] == "mkdir" and args[1].startswith(control)
                   or args[0] == "copyto" and args[2].startswith(control))
        if not allowed:
            raise AssertionError(f"payload command forbidden: {args}")
        if self.intercept is not None:
            result = self.intercept(args)
            if result is not None:
                return result
        return super().run(args, *positional, **kwargs)

    def popen(self, *args, **kwargs):
        raise AssertionError("baseline initialization cannot start a payload transfer")


@unittest.skipUnless(rclone_exe().is_file(), "requires bundled rclone; temporary alias only")
class BaselineInitializationTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory(prefix="PhoneNAS-baseline-init-")
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("RCLONE_")}
        for key in ("APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE", "TMP", "TEMP",
                    "PHONENAS_DATA_DIR"):
            environment[key] = str(self.root / "profile")
        environment_patch = patch.dict(os.environ, environment, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        tempdir_patch = patch.object(tempfile, "tempdir", str(self.root))
        tempdir_patch.start()
        self.addCleanup(tempdir_patch.stop)
        network = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.local = self.root / "local"
        self.remote = self.root / "phone-alias"
        self.local.mkdir()
        self.remote.mkdir()
        self.task = replace(new_task(str(self.local), direction="download",
                                    remote_path="backup/data/mirror",
                                    versions_path="backup/data/versions"),
                            enabled=False, excludes=("*.tmp",))
        self.mirror = self.remote / self.task.remote_path
        self.mirror.mkdir(parents=True)
        for folder in (self.local, self.mirror):
            (folder / "hello.txt").write_bytes(b"hello")
        (self.local / "local-empty").mkdir()
        (self.mirror / "phone-empty").mkdir()
        (self.local / "ignored.tmp").write_bytes(b"local ignored content")
        (self.mirror / "ignored.tmp").write_bytes(b"different ignored content")
        self.history = Path(self.task.local_history_path)
        self.history.mkdir(parents=True)
        (self.history / "history-sentinel.txt").write_bytes(b"never clean history")
        versions = self.remote / self.task.versions_path
        versions.mkdir(parents=True)
        (versions / "phone-history.txt").write_bytes(b"never clean phone history")
        (self.remote / "unrelated.txt").write_bytes(b"unrelated dataset sentinel")
        self.state = self.root / "baselines"
        conf = self.root / "alias.conf"
        conf.write_text(f"[phone]\ntype = alias\nremote = {self.remote.as_posix()}\n", encoding="utf-8")
        self.config = Config(self.root / "config.json")
        self.config.set("nas.url", "https://example.invalid/test-only")
        self.config.set("nas.username", "test-only")
        self.client = RecordingRclone(self.config, conf_path=conf)

    def engine(self, task=None, *, lease_factory=None, operation=""):
        task = task or self.task
        return SyncEngine(self.client, task, state_directory=self.state, operation=operation,
                          lease_factory=lease_factory or (lambda: LocalDatasetLease(self.remote, task)))

    def store(self, task=None):
        return BaselineStore(self.state, task or self.task, self.config)

    def payload(self):
        trees = []
        for root in (self.local, self.remote, self.history):
            entries = []
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root)
                if root == self.remote and relative.parts[0] == ".phonenas":
                    continue
                entries.append((relative.as_posix(), None if path.is_dir() else
                                (path.read_bytes(), path.stat().st_mtime_ns)))
            trees.append(entries)
        return trees

    def mutations(self):
        return [call for call in self.client.calls if call[0] not in ("lsjson", "hashsum", "cat")]

    def test_identical_existing_download_roots_initialize_only_control_and_baseline(self):
        engine = self.engine()
        before = self.payload()
        original_task = self.task.to_dict()
        preview = engine.preview_baseline_initialization()
        from app.core.sync import BaselineInitializationPreview, BaselineInitializationResult
        self.assertIsInstance(preview, BaselineInitializationPreview)
        self.assertEqual(preview.file_count, 1)
        self.assertEqual(preview.generation, "")
        self.assertNotEqual(preview.local_digest, preview.remote_digest)
        self.assertEqual(len(preview.digest), 64)
        self.assertEqual(len(preview.binding_digest), 64)
        with self.assertRaises(FrozenInstanceError):
            preview.file_count = 99
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.state.exists())
        self.assertFalse((self.remote / ".phonenas").exists())
        result = engine.initialize_baseline(preview)
        self.assertIsInstance(result, BaselineInitializationResult)
        self.assertEqual(result.file_count, 1)
        self.assertTrue(result.created_metadata)
        self.assertEqual(len(result.generation), 32)
        with self.assertRaises(FrozenInstanceError):
            result.file_count = 99
        snapshot, generation = self.store().load()
        self.assertEqual(snapshot.files, {"hello.txt": FileContent(5, HELLO)})
        self.assertEqual(snapshot.directories, ("phone-empty",))
        self.assertEqual(snapshot.digest, preview.remote_digest)
        self.assertEqual(generation, result.generation)
        metadata = DatasetCoordinator(self.client, self.task).read()
        self.assertEqual(metadata["phase"], "ready")
        self.assertEqual(metadata["generation"], generation)
        self.assertEqual(metadata["content_digest"], snapshot.digest)
        self.assertEqual([call[0] for call in self.mutations()], ["mkdir", "copyto"])
        self.assertFalse((self.remote / ".phonenas" / "locks" / self.task.dataset_id).exists())
        self.assertEqual(self.payload(), before)
        self.assertEqual(self.task.to_dict(), original_task)
        self.assertFalse(self.config.path.exists())

    def test_different_actual_content_and_missing_roots_are_never_adopted(self):
        path = self.local / "hello.txt"
        before_stat = path.stat()
        path.write_bytes(b"HELLO")
        os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
        before = self.payload()
        with self.assertRaisesRegex(RuntimeError, "一致"):
            self.engine().preview_baseline_initialization()
        self.assertEqual(self.payload(), before)
        path.write_bytes(b"hello")
        path.rename(self.local / "renamed.txt")
        with self.assertRaisesRegex(RuntimeError, "一致"):
            self.engine().preview_baseline_initialization()
        (self.local / "renamed.txt").rename(path)
        for task in (replace(self.task, local_path=str(self.root / "missing-local")),
                     replace(self.task, remote_path="backup/missing-mirror")):
            with self.subTest(local=task.local_path, remote=task.remote_path):
                with self.assertRaises((RuntimeError, ValueError)):
                    self.engine(task).preview_baseline_initialization()
        self.assertFalse((self.root / "missing-local").exists())
        self.assertFalse((self.remote / "backup/missing-mirror").exists())
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.state.exists())
        self.assertFalse((self.remote / ".phonenas").exists())

    def test_pending_locks_invalid_metadata_and_conflicting_baselines_fail_closed(self):
        from app.core.pending import PendingOperation
        from app.core.relay import snapshot_tree
        engine = self.engine()
        approved = engine.preview_baseline_initialization()
        snapshot = snapshot_tree(self.client, f"phone:{self.task.remote_path}", self.task.excludes)
        store = self.store()
        pending = PendingOperation(store)
        pending.save(operation="receive", local=snapshot, remote=snapshot,
                     generation="1" * 32, stamp="2026-11-14_120000", lock_token="2" * 32)
        before = self.payload()
        for inspect in (engine.preview_baseline_initialization, lambda: engine.initialize_baseline(approved)):
            with self.assertRaisesRegex(RuntimeError, "未完成"):
                inspect()
        pending.path.write_text("not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            engine.preview_baseline_initialization()
        pending.clear()
        lock = self.remote / ".phonenas" / "locks" / self.task.dataset_id
        with LocalDatasetLease(self.remote, self.task):
            owner = (lock / "owner.json").read_bytes()
            with self.assertRaisesRegex(RuntimeError, "锁"):
                engine.preview_baseline_initialization()
            with self.assertRaises(RuntimeError):
                engine.initialize_baseline(approved)
            self.assertEqual((lock / "owner.json").read_bytes(), owner)
        coordinator = DatasetCoordinator(self.client, self.task)
        for phase in ("publishing", "interrupted"):
            coordinator.write(phase, "1" * 32, snapshot.digest)
            with self.subTest(phase=phase), self.assertRaisesRegex(RuntimeError, "未完成"):
                engine.preview_baseline_initialization()
        coordinator.write("ready", "1" * 32, "0" * 64)
        with self.assertRaises(RuntimeError):
            engine.preview_baseline_initialization()
        metadata_path = self.remote / self.task.dataset_control_path / "state.json"
        metadata_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            engine.preview_baseline_initialization()
        coordinator.write("ready", "1" * 32, snapshot.digest)
        store.save(snapshot, "2" * 32)
        baseline_bytes = store.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "基线"):
            engine.preview_baseline_initialization()
        self.assertEqual(store.path.read_bytes(), baseline_bytes)
        foreign_task = replace(self.task, sync_identity="7" * 32)
        BaselineStore(self.state, foreign_task, self.config).save(snapshot, "1" * 32)
        foreign_bytes = store.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "基线"):
            engine.preview_baseline_initialization()
        self.assertEqual(store.path.read_bytes(), foreign_bytes)
        store.path.write_text("not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            engine.preview_baseline_initialization()
        self.assertEqual(store.path.read_text(encoding="utf-8"), "not json")
        self.assertEqual(self.payload(), before)

    def test_approval_binds_connection_scope_both_trees_and_locked_rescan(self):
        engine = self.engine()
        approved = engine.preview_baseline_initialization()
        before = self.payload()
        for changes in ({"direction": "upload"}, {"local_history_path": str(self.root / "other-history")},
                        {"excludes": ("*.tmp", "no-such-file")}, {"id": "other-task"},
                        {"sync_identity": "3" * 32}, {"dataset_id": "4" * 32},
                        {"versions_path": "backup/other-versions"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, "变化"):
                self.engine(replace(self.task, **changes)).initialize_baseline(approved)
        for field, changed in (("nas.url", "https://other.invalid/test-only"), ("nas.username", "other")):
            original = self.config.get(field)
            self.config.set(field, changed)
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "变化"):
                engine.initialize_baseline(approved)
            self.config.set(field, original)
        for folder in (self.local, self.mirror):
            added = folder / "new-empty-directory"
            added.mkdir()
            with self.subTest(folder=folder), self.assertRaisesRegex(RuntimeError, "变化"):
                engine.initialize_baseline(approved)
            added.rmdir()
        with self.assertRaisesRegex(RuntimeError, "变化"):
            engine.initialize_baseline(replace(approved, file_count=99))
        with self.assertRaises(ValueError):
            engine.initialize_baseline(approved.digest)
        self.assertEqual(self.payload(), before)
        expected = []
        outer = self
        class ChangedDuringLease(LocalDatasetLease):
            def __enter__(self):
                lease = super().__enter__()
                for folder in (outer.local, outer.mirror):
                    (folder / "hello.txt").write_bytes(b"HELLO")
                expected.append(outer.payload())
                return lease
        racing = self.engine(lease_factory=lambda: ChangedDuringLease(self.remote, self.task))
        with self.assertRaisesRegex(RuntimeError, "变化"):
            racing.initialize_baseline(approved)
        self.assertEqual(self.payload(), expected[0])
        self.assertEqual(self.mutations(), [])
        self.assertIsNone(self.store().load())
        self.assertFalse((self.remote / ".phonenas" / "locks" / self.task.dataset_id).exists())

    def test_existing_ready_reuses_generation_without_rewriting_metadata_or_current_baseline(self):
        from app.core.relay import snapshot_tree
        snapshot = snapshot_tree(self.client, f"phone:{self.task.remote_path}", self.task.excludes)
        coordinator = DatasetCoordinator(self.client, self.task)
        coordinator.write("ready", "5" * 32, snapshot.digest)
        metadata_path = self.remote / self.task.dataset_control_path / "state.json"
        original = (metadata_path.read_bytes(), metadata_path.stat().st_mtime_ns)
        self.client.calls.clear()
        engine = self.engine()
        before = self.payload()
        approved = engine.preview_baseline_initialization()
        self.assertEqual(approved.generation, "5" * 32)
        result = engine.initialize_baseline(approved)
        self.assertEqual((result.generation, result.file_count, result.created_metadata), ("5" * 32, 1, False))
        self.assertEqual((metadata_path.read_bytes(), metadata_path.stat().st_mtime_ns), original)
        self.assertEqual(self.mutations(), [])
        store = self.store()
        baseline_bytes = (store.path.read_bytes(), store.path.stat().st_mtime_ns)
        again = engine.initialize_baseline(engine.preview_baseline_initialization())
        self.assertEqual(again, result)
        self.assertEqual((store.path.read_bytes(), store.path.stat().st_mtime_ns), baseline_bytes)
        self.assertEqual(self.payload(), before)

    def test_unverified_metadata_never_saves_baseline_and_local_save_failure_is_retryable(self):
        from app.core.rclone import Result
        metadata_path = self.remote / self.task.dataset_control_path / "state.json"
        lock = self.remote / ".phonenas" / "locks" / self.task.dataset_id
        engine = self.engine()
        approved = engine.preview_baseline_initialization()
        before = self.payload()
        written = []
        def unverified_write(args):
            if args[0] == "copyto":
                state = json.loads(Path(args[1]).read_text(encoding="utf-8"))
                written.append(state)
                state["generation"] = "6" * 32
                metadata_path.write_text(json.dumps(state), encoding="utf-8")
                return Result(True, "", "", 0)
            return None
        self.client.intercept = unverified_write
        with self.assertRaisesRegex(RuntimeError, "状态|校验|核对"):
            engine.initialize_baseline(approved)
        self.assertEqual(len(written), 1)
        self.assertIsNone(self.store().load())
        self.assertTrue(lock.exists(), "unknown metadata outcome must retain the protective lock")
        self.assertEqual(self.payload(), before)
        self.client.intercept = None
        recovery = self.engine(operation="recover")
        recovery.execute(recovery.preview())
        self.assertFalse(lock.exists())
        metadata_path.unlink()  # Reset only the test's intentionally inconsistent metadata.
        original_unlink = Path.unlink
        def fail_lock_release(path, *args, **kwargs):
            if path == lock / "owner.json":
                raise OSError("simulated lock release failure")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_lock_release):
            with self.assertRaisesRegex(RuntimeError, "锁"):
                engine.initialize_baseline(approved)
        self.assertIsNone(self.store().load(), "lease cleanup failure cannot persist a baseline")
        self.assertTrue(lock.exists())
        recovery = self.engine(operation="recover")
        recovery.execute(recovery.preview())
        self.assertFalse(lock.exists())
        approved = engine.preview_baseline_initialization()
        original_replace = os.replace
        def fail_local_save(source, destination):
            if Path(destination) == self.store().path:
                raise OSError("simulated local baseline save failure")
            return original_replace(source, destination)
        with patch("os.replace", side_effect=fail_local_save):
            with self.assertRaisesRegex(OSError, "local baseline save failure"):
                engine.initialize_baseline(approved)
        self.assertIsNone(self.store().load())
        self.assertFalse(lock.exists(), "verified ready state permits an explicit retry after local save failure")
        metadata = DatasetCoordinator(self.client, self.task).read()
        self.client.calls.clear()
        result = engine.initialize_baseline(engine.preview_baseline_initialization())
        self.assertEqual(result.generation, metadata["generation"])
        self.assertFalse(result.created_metadata)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.payload(), before)


if __name__ == "__main__":
    unittest.main()
