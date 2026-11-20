"""Filename protocol tests with real rclone and a disposable loopback WebDAV server."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.core import rclone
from app.core.config import Config
from app.core.history import HistoryService, LocalHistoryService
from app.core.sync import SyncEngine
from app.core.tasks import new_task
from app.core.winpath import extended_path
from webdav_fixture import serve_webdav


READY_GENERATION = "a" * 32
INTERRUPTED_GENERATION = "b" * 32
LOCK_TOKEN = "c" * 32


def _put(root: Path, files: dict[str, bytes]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _tree(root: Path) -> dict:
    return {path.relative_to(root).as_posix(): None if path.is_dir() else
            (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (root, *root.rglob("*")) if path.exists()}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _v1_snapshot(files: dict[str, bytes]) -> dict:
    """Historical inventory: callers supply the exact 1.2.3/hotfix1 persisted spelling."""
    directories = {name[:index] for name in files for index, char in enumerate(name) if char == "/"}
    return {"files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                      for name, data in files.items()}, "directories": sorted(directories)}


def _v1_digest(snapshot: dict) -> str:
    # v1 digests use [size, sha256] pairs, not the file objects stored in JSON.
    digest_input = {"files": {name: [info["size"], info["sha256"]]
                              for name, info in snapshot["files"].items()},
                    "directories": snapshot["directories"]}
    return hashlib.sha256(json.dumps(digest_input, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class WebDAVFilenameTests(unittest.TestCase):
    def setUp(self):
        binary = rclone.rclone_exe()
        if not binary.is_file():
            self.skipTest("requires bundled rclone; fixture uses only loopback WebDAV")
        if not getattr(type(self), "vendor_verified", False):
            with binary.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            self.assertEqual(actual, "acd80683e23151e93bbe0d6ffa74d26e542ec510332f630beecb9944f8d1a623",
                             "run build/fetch-vendor.ps1 to use the pinned Windows rclone for protocol testing")
            type(self).vendor_verified = True
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="PhoneNAS-webdav-"))).resolve()
        self.remote = self.root / "phone"
        self.remote.mkdir()
        self.state = self.root / "baselines"
        self.requests = []
        endpoint = stack.enter_context(serve_webdav(self.remote, requests=self.requests))

        profile = self.root / "profile"
        profile.mkdir()
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("RCLONE_") and not key.lower().endswith("_proxy")}
        for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMP", "TEMP", "TMPDIR",
                    "PHONENAS_DATA_DIR"):
            environment[key] = str(profile)
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        stack.enter_context(patch.object(tempfile, "tempdir", str(profile)))
        conf = self.root / "rclone.conf"
        conf.write_text(f"[phone]\ntype = webdav\nvendor = other\nurl = {endpoint}\n", encoding="utf-8")
        self.config = Config(self.root / "config.json")
        self.config.set("nas.url", endpoint)
        self.client = rclone.Rclone(self.config, conf_path=conf)

    def test_picker_loads_live_parent_and_returns_existing_literal_path_without_writes(self):
        import time

        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QDialog

        from app.ui.remote_folder_dialog import RemoteFolderDialog

        app = QApplication.instance() or QApplication([])
        initial = "PhoneNasHistory/设计\u200b"
        (self.remote / initial).mkdir(parents=True)
        before = _tree(self.remote)
        dialog = RemoteFolderDialog(self.config, purpose="history", current_path=initial)
        try:
            dialog.show()
            deadline = time.monotonic() + 10
            while dialog.busy and time.monotonic() < deadline:
                QTest.qWait(20)
            self.assertFalse(dialog.busy, dialog.status_label.text())
            self.assertIn("PhoneNasHistory", dialog.path_label.text())
            self.assertEqual(dialog.tree.topLevelItemCount(), 1, dialog.status_label.text())
            item = dialog.tree.selectedItems()[0]
            self.assertEqual(item.text(0), "设计<U+200B>")
            self.assertEqual(item.data(0, Qt.ItemDataRole.UserRole), initial)
            dialog.choose_button.click()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(dialog.remote_path, initial)
            self.assertIn("PROPFIND", self.requests)
            self.assertLessEqual(set(self.requests), {"OPTIONS", "PROPFIND", "HEAD", "GET"})
            self.assertEqual(_tree(self.remote), before)
        finally:
            dialog.close()
            dialog.deleteLater()
            app.processEvents()

    def engine(self, task, *, operation=""):
        return SyncEngine(self.client, task, operation=operation, state_directory=self.state,
                          mount_drive=str(self.root / "unused-mount"))

    def run_sync(self, engine):
        preview = engine.preview()
        self.assertFalse(preview.blocked, preview.reason)
        return engine.execute(preview)

    def control(self, task):
        return self.remote / f".phonenas/datasets/{task.dataset_id}/state.json"

    def lock(self, task):
        return self.remote / f".phonenas/locks/{task.dataset_id}"

    def baseline(self, task):
        return self.state / f"{task.id}.json"

    def pending(self, task):
        return self.state / f"{task.id}.pending.json"

    def _v1_scope(self, task):
        return {"dataset_id": task.dataset_id, "remote_path": task.remote_path,
                "versions_path": task.versions_path, "excludes": list(task.excludes)}

    def _v1_binding(self, task):
        return {**self._v1_scope(task), "task_id": task.id, "direction": task.direction,
                "url": self.config.url, "username": self.config.username,
                "local_path": task.local_path, "local_history_path": task.local_history_path,
                "sync_identity": task.sync_identity}

    def _write_v1_control(self, task, snapshot, *, generation=READY_GENERATION, phase="ready"):
        state = {"version": 1, **self._v1_scope(task), "phase": phase, "generation": generation,
                 "content_digest": _v1_digest(snapshot), "writer_identity": task.sync_identity}
        _write_json(self.control(task), state)
        return state

    def _write_v1_baseline(self, task, snapshot, generation=READY_GENERATION):
        _write_json(self.baseline(task), {"version": 1, "binding": self._v1_binding(task),
                                        "snapshot": snapshot, "generation": generation,
                                        "content_digest": _v1_digest(snapshot)})

    def _write_v1_pending(self, task, *, operation, local, remote, prior_metadata=None,
                          generation=INTERRUPTED_GENERATION):
        path = self.pending(task)
        # Keep the old batch inside the retention period, including when tests run years later.
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        _write_json(path, {"version": 1, "binding": self._v1_binding(task), "operation": operation,
                           "local": local, "remote": remote, "generation": generation,
                           "stamp": stamp, "lock_token": LOCK_TOKEN, "prior_metadata": prior_metadata})
        return path, stamp

    def _write_v1_interrupted_publish(self, task, *, local, remote):
        prior = self._write_v1_control(task, remote)
        self._write_v1_baseline(task, remote)
        pending = self._write_v1_pending(task, operation="publish", local=local, remote=remote,
                                         prior_metadata=prior)
        self._write_v1_control(task, local, generation=INTERRUPTED_GENERATION, phase="publishing")
        _write_json(self.lock(task) / "owner.json", {"version": 1, "dataset_id": task.dataset_id,
                                                    "writer_identity": task.sync_identity, "token": LOCK_TOKEN})
        return pending

    def _assert_read_only_preview(self, engine, *, refusal=None):
        before = _tree(self.root)
        offset = len(self.requests)
        try:
            if refusal is None:
                return engine.preview()
            try:
                report = engine.preview()
            except RuntimeError as exc:
                self.assertRegex(str(exc), refusal)
            else:
                self.assertTrue(report.blocked, report.reason)
                self.assertRegex(report.reason, refusal)
        finally:
            self.assertEqual(_tree(self.root), before)
            writes = set(self.requests[offset:]) - {"OPTIONS", "GET", "HEAD", "PROPFIND"}
            self.assertEqual(writes, set(), "a refused/preview-only operation sent a DAV write")

    def test_filtered_target_alias_is_not_overwritten(self):
        source = self.root / "office"
        source.mkdir()
        task = replace(new_task(str(source)), excludes=("/kept.txt",))
        _put(source, {"Kept.txt": b"should not overwrite"})
        before = {"kept.txt": b"excluded target"}
        _put(self.remote / task.remote_path, before)
        report = self.engine(task).preview()
        self.assertTrue(report.blocked)
        self.assertIn("冲突", report.reason)
        self.assertEqual(_files(self.remote / task.remote_path), before)
        self.assertFalse(self.control(task).exists())
        self.assertFalse(self.lock(task).exists())
        self.assertTrue(set(self.requests) <= {"OPTIONS", "GET", "HEAD", "PROPFIND"})

    def test_exact_webdav_upload_download_archive_and_restore_with_literal_glob(self):
        source, target, recovery = (self.root / name for name in ("office", "home", "restore"))
        for folder in (source, target, recovery):
            folder.mkdir()
        original = {
            "PCB/file.txt": b"ordinary",
            "PCB\u200b/file.txt": b"zero width",
            "part\x7f/old.txt": b"DEL directory",
            "part\u2421/old.txt": b"visible DEL picture directory",
            "\u201bquote/\uff0f.txt": b"literal quote and fullwidth slash",
            "pair\x7f.txt": b"literal DEL that the glob must keep",
        }
        excluded = "pair\u2421.txt"
        _put(source, {**original, excluded: b"excluded at source"})
        empty = "empty\u200b"
        (source / empty).mkdir()
        publisher = replace(new_task(str(source), remote_path="backup/literal/mirror"),
                            excludes=("/pair[\u2421].txt",))
        mirror = self.remote / publisher.remote_path
        upload = self.engine(publisher)
        first = upload.preview()
        self.assertFalse(first.blocked, first.reason)
        self.assertEqual(set(first.changes), {("+", name) for name in original})
        upload.execute(first)
        self.assertEqual(_files(mirror), original)
        self.assertTrue((mirror / empty).is_dir())
        self.assertEqual(_read_json(self.control(publisher))["version"], 2)
        self.assertEqual(_read_json(self.baseline(publisher))["version"], 2)

        # Out-of-scope files at all three roots must stay independent, even beside DEL.
        _put(mirror, {excluded: b"excluded on phone"})
        _put(target, {excluded: b"excluded at receiver"})
        receiver = replace(new_task(str(target), direction="download", remote_path=publisher.remote_path,
                                    versions_path=publisher.versions_path, dataset_id=publisher.dataset_id),
                           excludes=publisher.excludes)
        pull = self.engine(receiver)
        self.run_sync(pull)
        self.assertEqual(_files(target), {**original, excluded: b"excluded at receiver"})
        self.assertTrue((target / empty).is_dir())
        self.assertEqual(_read_json(self.baseline(receiver))["version"], 2)

        removed = "part\x7f/old.txt"
        (source / removed).unlink()
        (mirror / excluded).write_bytes(b"excluded phone edit")
        (target / excluded).write_bytes(b"excluded local edit")
        changed = {name: content + b" v2" for name, content in original.items() if name != removed}
        _put(source, changed)
        approved = upload.preview()
        self.assertFalse(approved.blocked, approved.reason)
        self.assertEqual(set(approved.changes), {("-", removed)} | {("*", name) for name in changed})
        popen = self.client.popen

        def interrupt_before_writing(args, *positional, **kwargs):
            if args[0] == "copy" and args[2] == f"phone:{publisher.remote_path}":
                self.assertEqual(_files(Path(args[1])), changed)
                raise RuntimeError("simulated interruption after staging")
            return popen(args, *positional, **kwargs)

        with patch.object(self.client, "popen", side_effect=interrupt_before_writing):
            with self.assertRaisesRegex(RuntimeError, "after staging"):
                upload.execute(approved)
        intent = _read_json(self.pending(publisher))
        state = _read_json(self.control(publisher))
        self.assertEqual((intent["version"], intent["prior_metadata"]["version"]), (2, 2))
        self.assertEqual((state["version"], state["phase"], state["generation"]),
                         (2, "publishing", intent["generation"]))
        self.assertEqual(_files(mirror), {**original, excluded: b"excluded phone edit"})
        self.assertTrue(self.lock(publisher).is_dir())

        published = self.run_sync(self.engine(publisher, operation="recover"))
        self.assertEqual(published, intent["stamp"])
        self.assertFalse(self.pending(publisher).exists())
        self.assertFalse(self.lock(publisher).exists())
        received = self.run_sync(pull)
        self.assertEqual(_files(source), {**changed, excluded: b"excluded at source"})
        self.assertEqual(_files(mirror), {**changed, excluded: b"excluded phone edit"})
        self.assertEqual(_files(target), {**changed, excluded: b"excluded local edit"})
        self.assertEqual(_files(self.remote / publisher.versions_path / published), original)
        self.assertEqual(_files(Path(receiver.local_history_path) / received), original)

        phone_history = HistoryService(self.client, publisher)
        self.assertCountEqual(phone_history.list_files(published), original)
        self.assertEqual(_files(phone_history.restore(published, recovery)), original)
        local_history = LocalHistoryService(receiver)
        self.assertEqual(_files(local_history.restore(received, recovery)), original)

    def test_legacy_v1_parent_spelling_with_different_leaves_stops_before_writing(self):
        cases = (("original", {}), ("hotfix1", {"hotfix\u200b.txt": b"existing zero-width name"}))
        for origin, additional in cases:
            with self.subTest(origin=origin):
                source = self.root / origin
                common = {"ordinary.txt": b"already synchronized", **additional}
                _put(source, {**common, "part\x7f/new.txt": b"new local file"})
                task = new_task(str(source), remote_path=f"backup/{origin}/mirror")
                old = {**common, "part\u2421/old.txt": b"old remote file"}
                _put(self.remote / task.remote_path, old)
                _put(self.remote / task.versions_path / "2026-11-18_101000",
                     {"ordinary.txt": b"existing history"})
                # The original client persisted its encoded spelling, hotfix1 the
                # physical phone spelling. Both give U+2421 for this old path.
                snapshot = _v1_snapshot(old)
                self._write_v1_control(task, snapshot)
                self._write_v1_baseline(task, snapshot)
                self._assert_read_only_preview(self.engine(task), refusal="文件名协议|旧")
                self.assertFalse(self.lock(task).exists())

    @unittest.skipUnless(os.name == "nt", "MAX_PATH staging regression is Windows-specific")
    def test_first_publish_cleans_staging_when_source_is_near_max_path(self):
        source = self.root / "deep-office"
        source.mkdir()
        staging_prefix = len(str(Path(tempfile.gettempdir()) / "PhoneNAS-staged-aaaaaaaa" / "files")) + 1
        relative = ""
        for count in range(1, 9):
            candidate = "/".join(["d" * 32] * count + ["probe.txt"])
            source_length = len(str(source)) + 1 + len(candidate)
            if source_length < 260 and staging_prefix + len(candidate) > 260:
                relative = candidate
                break
        if not relative:
            self.skipTest("temporary paths cannot exercise the MAX_PATH boundary")
        target = source.joinpath(*relative.split("/"))
        self.assertLess(len(str(target)), 260)
        Path(extended_path(target.parent)).mkdir(parents=True, exist_ok=True)
        Path(extended_path(target)).write_bytes(b"deep source")

        task = new_task(str(source), remote_path="m")
        engine = self.engine(task)
        report = engine.preview()
        self.assertFalse(report.blocked, report.reason)
        self.assertEqual(report.changes, (("+", relative),))
        engine.execute(report)

        remote_file = self.remote / task.remote_path / relative
        self.assertEqual(Path(extended_path(remote_file)).read_bytes(), b"deep source")
        self.assertEqual(list(Path(tempfile.gettempdir()).glob("PhoneNAS-staged-*")), [])

    def test_legacy_v1_ordinary_files_can_publish_and_receive_without_upgrading(self):
        source, target = self.root / "office", self.root / "home"
        old = {"folder/ordinary.txt": b"old ordinary bytes"}
        new = {"folder/ordinary.txt": b"new ordinary bytes"}
        _put(source, new)
        _put(target, old)
        task = new_task(str(source), remote_path="backup/ordinary/mirror")
        peer = new_task(str(target), direction="download", remote_path=task.remote_path,
                        versions_path=task.versions_path, dataset_id=task.dataset_id)
        mirror = self.remote / task.remote_path
        _put(mirror, old)
        common = _v1_snapshot(old)
        self._write_v1_control(task, common)
        self._write_v1_baseline(task, common)
        self._write_v1_baseline(peer, common)

        publish = self.engine(task)
        report = self._assert_read_only_preview(publish)
        self.assertFalse(report.blocked, report.reason)
        self.assertEqual(report.changes, (("*", "folder/ordinary.txt"),))
        published = publish.execute(report)
        received = self.run_sync(self.engine(peer))
        self.assertEqual(_files(mirror), new)
        self.assertEqual(_files(target), new)
        self.assertEqual(_files(self.remote / task.versions_path / published), old)
        self.assertEqual(_files(Path(peer.local_history_path) / received), old)
        for path in (self.control(task), self.baseline(task), self.baseline(peer)):
            with self.subTest(path=path):
                self.assertEqual(_read_json(path)["version"], 1)
        self.assertFalse(self.lock(task).exists())
        recovery = self.root / "restore"
        recovery.mkdir()
        self.assertEqual(_files(HistoryService(self.client, task).restore(published, recovery)), old)

    def test_legacy_v1_interrupted_ordinary_publish_can_resume_with_original_lock(self):
        source = self.root / "office"
        old = {"ordinary.txt": b"old ordinary bytes"}
        new = {"ordinary.txt": b"new ordinary bytes"}
        _put(source, new)
        task = new_task(str(source), remote_path="backup/resume/mirror")
        mirror = self.remote / task.remote_path
        _put(mirror, old)
        pending, stamp = self._write_v1_interrupted_publish(task, local=_v1_snapshot(new),
                                                            remote=_v1_snapshot(old))
        recovery = self.engine(task, operation="recover")
        report = self._assert_read_only_preview(recovery)
        self.assertFalse(report.blocked, report.reason)
        self.assertTrue(report.requires_confirmation)
        self.assertEqual(recovery.execute(report), stamp)
        self.assertEqual(_files(mirror), new)
        self.assertEqual(_files(self.remote / task.versions_path / stamp), old)
        state = _read_json(self.control(task))
        self.assertEqual((state["version"], state["phase"], state["generation"]),
                         (1, "ready", INTERRUPTED_GENERATION))
        self.assertEqual(_read_json(self.baseline(task))["version"], 1)
        self.assertFalse(pending.exists())
        self.assertFalse(self.lock(task).exists())

    def test_recovery_after_ready_preserves_preexisting_empty_destination_directory(self):
        source = self.root / "office"
        initial = {"notes.txt": b"original notes"}
        _put(source, initial)
        task = new_task(str(source))
        self.run_sync(self.engine(task))
        (source / "kept-empty").mkdir()
        self.run_sync(self.engine(task))
        (source / "kept-empty").rmdir()
        (source / "notes.txt").write_bytes(b"revised notes")
        publish = self.engine(task)
        preview = publish.preview()
        with patch("app.core.relay.BaselineStore.save", side_effect=OSError("fixture cannot save baseline")):
            with self.assertRaisesRegex(OSError, "cannot save baseline"):
                publish.execute(preview)
        self.assertEqual(_read_json(self.control(task))["phase"], "ready")
        self.assertTrue(self.pending(task).is_file())
        self.assertTrue(self.lock(task).is_dir())
        self.run_sync(self.engine(task, operation="recover"))
        self.assertEqual(_files(self.remote / task.remote_path), {"notes.txt": b"revised notes"})
        self.assertTrue((self.remote / task.remote_path / "kept-empty").is_dir())
        self.assertFalse(self.lock(task).exists())
        self.assertFalse(self.pending(task).exists())

    def test_recovery_never_replaces_a_modified_existing_history_file(self):
        source = self.root / "office"
        new, old = {"notes.txt": b"new draft"}, {"notes.txt": b"previous draft"}
        _put(source, new)
        task = new_task(str(source))
        _put(self.remote / task.remote_path, old)
        _, stamp = self._write_v1_interrupted_publish(task, local=_v1_snapshot(new), remote=_v1_snapshot(old))
        _put(self.remote / task.versions_path / stamp, {"notes.txt": b"unrelated recovered copy"})
        self._assert_read_only_preview(self.engine(task, operation="recover"), refusal="旧目标内容")

    def test_legacy_v1_interrupted_sensitive_publish_preserves_persisted_intent_and_lock(self):
        ordinary = {"ordinary.txt": b"ordinary bytes"}
        new = {**ordinary, "part\x7f/new.txt": b"new local file"}
        old = {**ordinary, "part\u2421/old.txt": b"old remote file"}
        # Original 1.2.3 inventories rendered a local DEL as U+2421; hotfix1
        # could instead have recorded a literal local DEL before applying payload.
        cases = (("original", {**ordinary, "part\u2421/new.txt": b"new local file"}), ("hotfix1", new))
        for origin, recorded_local in cases:
            with self.subTest(origin=origin):
                source = self.root / origin
                _put(source, new)
                task = new_task(str(source), remote_path=f"backup/{origin}/mirror")
                _put(self.remote / task.remote_path, old)
                self._write_v1_interrupted_publish(task, local=_v1_snapshot(recorded_local), remote=_v1_snapshot(old))
                self._assert_read_only_preview(self.engine(task, operation="recover"), refusal="文件名协议|旧")

    def test_v2_dataset_rejects_v1_peer_baseline_and_journal_even_with_ordinary_names(self):
        source, target = self.root / "office", self.root / "home"
        ordinary = {"folder/ordinary.txt": b"published ordinary bytes"}
        _put(source, ordinary)
        task = new_task(str(source), remote_path="backup/v2-peer/mirror")
        peer = new_task(str(target), direction="download", remote_path=task.remote_path,
                        versions_path=task.versions_path, dataset_id=task.dataset_id)
        target.mkdir()
        self.run_sync(self.engine(task))
        control = _read_json(self.control(task))
        self.assertEqual(control["version"], 2)
        generation = control["generation"]
        pull = self.engine(peer)
        self.run_sync(pull)
        self.assertEqual(self.run_sync(pull), "")
        self.assertEqual(_read_json(self.baseline(peer))["version"], 2)
        common = _v1_snapshot(ordinary)
        self._write_v1_baseline(peer, common, generation)
        self._assert_read_only_preview(self.engine(peer), refusal="文件名协议")

        # Isolate the old journal from the old baseline; without a version fence
        # the matching ready generation and source contents would permit resuming.
        self.baseline(peer).unlink()
        self._write_v1_pending(peer, operation="receive", local=common, remote=common, generation=generation)
        self._assert_read_only_preview(self.engine(peer, operation="recover"), refusal="文件名协议")


    def test_phone_history_cannot_restore_or_delete_while_dataset_is_locked(self):
        source, destination = self.root / "office", self.root / "restore"
        source.mkdir()
        destination.mkdir()
        task = new_task(str(source))
        stamp = "2026-11-18_101000"
        _put(self.remote / task.versions_path / stamp, {"notes.txt": b"old notes"})
        owner = self.lock(task) / "owner.json"
        _write_json(owner, {"version": 1, "dataset_id": task.dataset_id,
                            "writer_identity": task.sync_identity, "token": LOCK_TOKEN})
        before = _tree(self.root)
        history = HistoryService(self.client, task)
        with self.assertRaisesRegex(RuntimeError, "锁"):
            history.restore(stamp, destination)
        with self.assertRaisesRegex(RuntimeError, "锁"):
            history.delete(stamp)
        self.assertEqual(_tree(self.root), before)
        self.assertTrue(set(self.requests) <= {"OPTIONS", "GET", "HEAD", "PROPFIND"})

    def test_new_receive_does_not_expire_unproven_local_history(self):
        from datetime import timedelta

        source, destination = self.root / "office", self.root / "home"
        _put(source, {"notes.txt": b"new publication"})
        destination.mkdir()
        task = new_task(str(source))
        self.run_sync(self.engine(task))
        receiver = new_task(str(destination), direction="download", remote_path=task.remote_path,
                            versions_path=task.versions_path, dataset_id=task.dataset_id)
        stamp = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d_%H%M%S")
        old = Path(receiver.local_history_path) / stamp / "notes.txt"
        _put(old.parent, {old.name: b"offline backup with no protocol marker"})
        self.run_sync(self.engine(receiver))
        self.assertEqual(old.read_bytes(), b"offline backup with no protocol marker")
        self.assertEqual((destination / "notes.txt").read_bytes(), b"new publication")

    def test_legacy_v1_or_unmarked_ambiguous_history_is_not_restored_or_deleted(self):
        source, destination = self.root / "office", self.root / "restore"
        source.mkdir()
        destination.mkdir()
        task = new_task(str(source), remote_path="backup/old-history/mirror")
        current = {"ordinary.txt": b"ordinary current mirror"}
        _put(source, current)
        _put(self.remote / task.remote_path, current)
        self._write_v1_control(task, _v1_snapshot(current))
        stamp = "2026-11-18_101000"
        batch = self.remote / task.versions_path / stamp
        _put(batch, {
            "ordinary.txt": b"ordinary archived file",
            "part\u2421/old.txt": b"might have been a literal DEL on the original computer",
        })

        # The current mirror need not contain any sensitive names. Even selecting
        # only an ordinary file must not turn an ambiguous batch into a partial restore.
        for persisted in (True, False):
            if not persisted:
                self.control(task).unlink()
                (batch / "part\u2421/old.txt").unlink()  # The ambiguous empty directory also matters.
            for name in ("restore", "delete"):
                with self.subTest(persisted=persisted, action=name):
                    history = HistoryService(self.client, task)
                    before = _tree(self.remote)
                    offset = len(self.requests)
                    try:
                        with self.assertRaisesRegex(RuntimeError, "旧|文件名协议"):
                            if name == "restore":
                                history.restore(stamp, destination, files=["ordinary.txt"])
                            else:
                                history.delete(stamp)
                    finally:
                        self.assertEqual(list(destination.iterdir()), [])
                        self.assertEqual(_tree(self.remote), before)
                        writes = set(self.requests[offset:]) - {"OPTIONS", "GET", "HEAD", "PROPFIND"}
                        self.assertEqual(writes, set(), "a refused history operation sent a DAV write")


if __name__ == "__main__":
    unittest.main()
