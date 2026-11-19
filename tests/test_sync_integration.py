from __future__ import annotations

import os
import tempfile
from unittest.mock import patch
import unittest
from dataclasses import replace
from pathlib import Path

from app.core.config import Config
from app.core.rclone import Rclone, rclone_exe
from app.core.sync import SyncEngine
from app.core.tasks import new_task


@unittest.skipUnless(rclone_exe().is_file(), "requires bundled rclone; all paths remain in a temporary local sandbox")
class LocalMirrorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="PhoneNAS-integration-")
        self.base = Path(self.sandbox.name)
        self.env = patch.dict(os.environ, {
            "PHONENAS_DATA_DIR": str(self.base / "app-data"),
            "APPDATA": str(self.base / "profile"),
        })
        self.env.start()
        for key in tuple(os.environ):
            if key.upper().startswith("RCLONE_"):
                os.environ.pop(key, None)
        self.remote = self.base / "pretend-phone"
        self.remote.mkdir()
        self.conf = self.base / "alias.conf"
        self.conf.write_text(f"[phone]\ntype = alias\nremote = {self.remote.as_posix()}\n", encoding="utf-8")
        self.client = Rclone(Config(self.base / "config.json"), conf_path=self.conf)

    def tearDown(self):
        self.env.stop()
        self.sandbox.cleanup()

    def engine(self, task, *, operation="", state_name="baseline"):
        from app.core.relay import LocalDatasetLease
        return SyncEngine(
            self.client, task, state_directory=self.base / state_name,
            log_directory=self.base / "logs", operation=operation,
            lease_factory=lambda: LocalDatasetLease(self.remote, task),
        )

    def test_unicode_names_publish_receive_and_archive_without_renaming(self):
        source = self.base / "office"
        source.mkdir()
        names = ("private-\ue000.txt", "emoji-👩\u200d💻.txt", "join-\u200c.txt", "unassigned-\u0378.txt",
                 "symbol-␡260702.txt", "paragraph-\u2029.txt", "PCB\u200b.txt", "symbol-\x7f260702.txt",
                 "quote-\u201b.txt", "slash-\uff0f.txt")
        for name in names:
            (source / name).write_bytes(b"original bytes")
        publisher = new_task(str(source))
        upload = self.engine(publisher)
        plan = upload.preview()
        self.assertFalse(plan.blocked)
        self.assertEqual(plan.added, len(names))
        upload.execute(plan)
        target = self.base / "home"
        target.mkdir()
        receiver = new_task(str(target), direction="download", remote_path=publisher.remote_path,
                            versions_path=publisher.versions_path, dataset_id=publisher.dataset_id)
        pull = self.engine(receiver)
        pull.execute(pull.preview())
        for name in names:
            self.assertEqual((source / name).read_bytes(), b"original bytes")
            self.assertEqual((self.remote / publisher.remote_path / name).read_bytes(), b"original bytes")
            self.assertEqual((target / name).read_bytes(), b"original bytes")
        (source / names[0]).write_bytes(b"changed bytes")
        stamp = upload.execute(upload.preview())
        self.assertEqual((self.remote / publisher.versions_path / stamp / names[0]).read_bytes(), b"original bytes")
        received_stamp = pull.execute(pull.preview())
        self.assertEqual((Path(receiver.local_history_path) / received_stamp / names[0]).read_bytes(), b"original bytes")

    def test_preview_names_bad_local_or_phone_path_before_any_write(self):
        source = self.base / "office"
        source.mkdir()
        publisher = new_task(str(source))
        bad_name = "invisible-\u202e260702.txt"
        for side in ("本地", "手机"):
            with self.subTest(side=side):
                root = source if side == "本地" else self.remote / publisher.remote_path
                root.mkdir(parents=True, exist_ok=True)
                path = root / bad_name
                path.write_bytes(b"must stay untouched")
                with self.assertRaises(ValueError) as caught:
                    self.engine(publisher).preview()
                message = str(caught.exception)
                self.assertIn(side, message)
                self.assertIn("260702.txt", message)
                self.assertIn("U+202E", message)
                self.assertNotIn("\u202e", message)
                self.assertEqual(path.read_bytes(), b"must stay untouched")
                self.assertFalse((self.remote / ".phonenas").exists())
                self.assertFalse((self.base / "baseline").exists())
                path.unlink()

    def test_download_archives_old_local_files_without_writing_phone_history(self):
        source = self.base / "office"
        source.mkdir()
        (source / "drawing.txt").write_text("office baseline", encoding="utf-8")
        publisher = new_task(str(source))
        upload = self.engine(publisher)
        upload.execute(upload.preview())
        target = self.base / "home"
        target.mkdir()
        receiver = new_task(
            str(target), direction="download", remote_path=publisher.remote_path,
            versions_path=publisher.versions_path, dataset_id=publisher.dataset_id,
        )
        pull = self.engine(receiver)
        first = pull.preview()
        self.assertEqual((first.added, first.deleted), (1, 0))
        pull.execute(first)
        (source / "drawing.txt").write_text("office next published revision", encoding="utf-8")
        upload.execute(upload.preview())
        report = pull.preview()
        self.assertFalse(report.blocked)
        self.assertEqual(report.updated, 1)
        stamp = pull.execute(report)
        self.assertEqual((target / "drawing.txt").read_text(), "office next published revision")
        self.assertEqual((Path(receiver.local_history_path) / stamp / "drawing.txt").read_text(), "office baseline")

    def test_home_edits_block_download_then_return_and_company_adopts(self):
        office = self.base / "office"
        home = self.base / "home"
        office.mkdir()
        home.mkdir()
        (office / "drawing.txt").write_text("company revision", encoding="utf-8")
        (office / "removed.txt").write_text("keep this old file", encoding="utf-8")
        publisher = new_task(str(office))
        upload = self.engine(publisher)
        upload.execute(upload.preview())
        receiver = new_task(str(home), direction="download", remote_path=publisher.remote_path,
                            versions_path=publisher.versions_path, dataset_id=publisher.dataset_id)
        pull = self.engine(receiver)
        pull.execute(pull.preview())
        original = (home / "drawing.txt").stat()
        (home / "drawing.txt").write_text("editing at home!", encoding="utf-8")
        os.utime(home / "drawing.txt", ns=(original.st_atime_ns, original.st_mtime_ns))
        (home / "removed.txt").unlink()
        (home / "added.txt").write_text("new home work", encoding="utf-8")
        guarded = self.engine(receiver).preview()
        self.assertTrue(guarded.blocked)
        with self.assertRaises(RuntimeError):
            self.engine(receiver).execute(guarded)
        returned = self.engine(receiver, operation="return")
        plan = returned.preview()
        self.assertEqual((plan.added, plan.updated, plan.deleted), (1, 1, 1))
        self.assertFalse(plan.blocked)
        stamp = returned.execute(plan)
        self.assertEqual((self.remote / publisher.remote_path / "drawing.txt").read_text(), "editing at home!")
        self.assertEqual((self.remote / publisher.versions_path / stamp / "drawing.txt").read_text(), "company revision")
        stale_company = self.engine(publisher).preview()
        self.assertTrue(stale_company.blocked)
        self.assertIn("接收手机", stale_company.reason)
        adopt = self.engine(publisher, operation="adopt")
        adopted = adopt.preview()
        self.assertFalse(adopted.blocked)
        adopt.execute(adopted)
        self.assertEqual((office / "drawing.txt").read_text(), "editing at home!")
        self.assertFalse((office / "removed.txt").exists())
        self.assertEqual((office / "added.txt").read_text(), "new home work")
        self.assertFalse(self.engine(publisher).preview().blocked)
        self.assertFalse(self.engine(receiver).preview().blocked)

    def test_same_size_same_timestamp_local_edit_is_never_silently_replaced(self):
        office = self.base / "office"
        home = self.base / "home"
        office.mkdir()
        home.mkdir()
        (office / "file.txt").write_bytes(b"AAAA")
        publisher = new_task(str(office))
        upload = self.engine(publisher)
        upload.execute(upload.preview())
        receiver = new_task(str(home), direction="download", remote_path=publisher.remote_path,
                            versions_path=publisher.versions_path, dataset_id=publisher.dataset_id)
        pull = self.engine(receiver)
        pull.execute(pull.preview())
        info = (home / "file.txt").stat()
        (home / "file.txt").write_bytes(b"BBBB")
        os.utime(home / "file.txt", ns=(info.st_atime_ns, info.st_mtime_ns))
        report = self.engine(receiver).preview()
        self.assertTrue(report.blocked)
        self.assertIn("未交回", report.reason)
        self.assertEqual((home / "file.txt").read_bytes(), b"BBBB")

    def test_content_changes_after_approval_fail_before_destination_write(self):
        office = self.base / "office"
        office.mkdir()
        path = office / "file.txt"
        path.write_bytes(b"AAAA")
        publisher = new_task(str(office))
        engine = self.engine(publisher)
        engine.execute(engine.preview())
        path.write_bytes(b"BBBB")
        approval = engine.preview()
        path.write_bytes(b"CCCC")
        with self.assertRaisesRegex(RuntimeError, "变化"):
            engine.execute(approval)
        self.assertEqual((self.remote / publisher.remote_path / "file.txt").read_bytes(), b"AAAA")

    def test_no_change_publish_does_not_invalidate_home_return_baseline(self):
        office = self.base / "office"
        home = self.base / "home"
        office.mkdir()
        home.mkdir()
        (office / "file.txt").write_bytes(b"AAAA")
        publisher = new_task(str(office))
        upload = self.engine(publisher)
        upload.execute(upload.preview())
        receiver = new_task(str(home), direction="download", remote_path=publisher.remote_path,
                            versions_path=publisher.versions_path, dataset_id=publisher.dataset_id)
        pull = self.engine(receiver)
        pull.execute(pull.preview())
        upload.execute(upload.preview())
        (home / "file.txt").write_bytes(b"BBBB")
        report = self.engine(receiver, operation="return").preview()
        self.assertFalse(report.blocked, report.reason)

    def test_file_directory_transition_is_blocked_before_any_overwrite(self):
        office = self.base / "office"
        office.mkdir()
        (office / "part").write_bytes(b"old file")
        publisher = new_task(str(office))
        upload = self.engine(publisher)
        upload.execute(upload.preview())
        (office / "part").unlink()
        (office / "part").mkdir()
        (office / "part" / "new.txt").write_bytes(b"new nested file")
        report = upload.preview()
        self.assertTrue(report.blocked)
        self.assertEqual((self.remote / publisher.remote_path / "part").read_bytes(), b"old file")

    def test_interrupted_publish_requires_explicit_content_bound_resume(self):
        office = self.base / "office"
        office.mkdir()
        (office / "file.txt").write_bytes(b"AAAA")
        publisher = new_task(str(office))
        upload = self.engine(publisher)
        upload.execute(upload.preview())
        (office / "file.txt").write_bytes(b"BBBB")
        approval = upload.preview()
        original = upload._run_transfer
        calls = 0
        def fail_during_destination(args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated interrupted payload")
            return original(args)
        with patch.object(upload, "_run_transfer", side_effect=fail_during_destination):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                upload.execute(approval)
        self.assertEqual((self.remote / publisher.remote_path / "file.txt").read_bytes(), b"AAAA")
        recovery = self.engine(publisher, operation="recover")
        resume = recovery.preview()
        self.assertFalse(resume.blocked, resume.reason)
        self.assertTrue(resume.requires_confirmation)
        recovery.execute(resume)
        self.assertEqual((self.remote / publisher.remote_path / "file.txt").read_bytes(), b"BBBB")
        self.assertFalse((self.remote / ".phonenas" / "locks" / publisher.dataset_id).exists())
        self.assertFalse(self.engine(publisher).preview().blocked)

    def test_publish_intent_survives_failure_before_metadata_and_repeated_recovery(self):
        from app.core.pending import PendingOperation
        from app.core.rclone import Result
        from app.core.relay import BaselineStore, DatasetCoordinator

        office = self.base / "office"
        office.mkdir()
        (office / "file.txt").write_bytes(b"AAAA")
        publisher = new_task(str(office))
        upload = self.engine(publisher)
        upload.execute(upload.preview())
        (office / "file.txt").write_bytes(b"BBBB")
        original = self.client.run

        def fail_metadata(args, *positional, **kwargs):
            if args[:1] == ["copyto"] and args[2].endswith("/state.json"):
                return Result(False, "", "simulated metadata write failure", -1)
            return original(args, *positional, **kwargs)

        with patch.object(self.client, "run", side_effect=fail_metadata):
            with self.assertRaisesRegex(RuntimeError, "metadata write failure"):
                upload.execute(upload.preview())
        journal = PendingOperation(BaselineStore(self.base / "baseline", publisher, self.client.config))
        intent = journal.load()
        self.assertEqual((self.remote / publisher.remote_path / "file.txt").read_bytes(), b"AAAA")
        recovery = self.engine(publisher, operation="recover")
        report = recovery.preview()
        self.assertFalse(report.blocked, report.reason)
        self.assertTrue(report.requires_confirmation)
        with patch.object(self.client, "run", side_effect=fail_metadata):
            with self.assertRaisesRegex(RuntimeError, "metadata write failure"):
                recovery.execute(report)
        self.assertEqual(journal.load(), intent)
        recovery = self.engine(publisher, operation="recover")
        report = recovery.preview()
        self.assertFalse(report.blocked, report.reason)
        stamp = recovery.execute(report)
        self.assertEqual(stamp, intent["stamp"])
        self.assertEqual(DatasetCoordinator(self.client, publisher).read()["generation"], intent["generation"])
        self.assertEqual((self.remote / publisher.remote_path / "file.txt").read_bytes(), b"BBBB")
        self.assertIsNone(journal.load())

    def test_missing_replacement_requires_original_history_proof_across_retries(self):
        from app.core.pending import PendingOperation
        from app.core.relay import BaselineStore

        office = self.base / "office"
        office.mkdir()
        (office / "first.txt").write_bytes(b"AAAA")
        (office / "second.txt").write_bytes(b"CCCC")
        publisher = new_task(str(office))
        upload = self.engine(publisher)
        upload.execute(upload.preview())
        (office / "first.txt").write_bytes(b"BBBB")
        (office / "second.txt").write_bytes(b"DDDD")
        destination = f"phone:{publisher.remote_path}"
        original = self.client.popen

        def interrupt_after_backup(args, *positional, **kwargs):
            if args[:1] == ["copy"] and args[2] == destination:
                history = args[args.index("--backup-dir") + 1]
                for name in ("first.txt", "second.txt"):
                    moved = self.client.run(["moveto", f"{destination}/{name}", f"{history}/{name}"])
                    self.assertTrue(moved.ok, moved.err)
                copied = self.client.copyto(str(Path(args[1]) / "second.txt"), f"{destination}/second.txt")
                self.assertTrue(copied.ok, copied.err)
                raise RuntimeError("simulated interruption after backup")
            return original(args, *positional, **kwargs)

        with patch.object(self.client, "popen", side_effect=interrupt_after_backup):
            with self.assertRaisesRegex(RuntimeError, "interruption after backup"):
                upload.execute(upload.preview())
        journal = PendingOperation(BaselineStore(self.base / "baseline", publisher, self.client.config))
        intent = journal.load()
        backup = self.remote / publisher.versions_path / intent["stamp"] / "first.txt"
        backup.write_bytes(b"ZZZZ")
        recovery = self.engine(publisher, operation="recover")
        invalid = recovery.preview()
        self.assertTrue(invalid.blocked)
        with self.assertRaises(RuntimeError):
            recovery.execute(invalid)
        backup.write_bytes(b"AAAA")
        recovery = self.engine(publisher, operation="recover")
        report = recovery.preview()
        self.assertFalse(report.blocked, report.reason)

        def interrupt_again(args, *positional, **kwargs):
            if args[:1] == ["copy"] and args[2] == destination:
                raise RuntimeError("simulated repeat interruption")
            return original(args, *positional, **kwargs)

        with patch.object(self.client, "popen", side_effect=interrupt_again):
            with self.assertRaisesRegex(RuntimeError, "repeat interruption"):
                recovery.execute(report)
        self.assertEqual(journal.load(), intent)
        recovery = self.engine(publisher, operation="recover")
        report = recovery.preview()
        self.assertFalse(report.blocked, report.reason)
        self.assertEqual(recovery.execute(report), intent["stamp"])
        self.assertEqual((self.remote / publisher.remote_path / "first.txt").read_bytes(), b"BBBB")
        self.assertEqual((self.remote / publisher.remote_path / "second.txt").read_bytes(), b"DDDD")
        self.assertEqual(backup.read_bytes(), b"AAAA")
        self.assertEqual(backup.with_name("second.txt").read_bytes(), b"CCCC")
        self.assertIsNone(journal.load())

    def test_interrupted_receive_requires_ready_matching_published_content(self):
        from app.core.relay import DatasetCoordinator

        office, home = self.base / "office", self.base / "home"
        office.mkdir()
        home.mkdir()
        (office / "file.txt").write_bytes(b"AAAA")
        publisher = new_task(str(office))
        upload = self.engine(publisher)
        upload.execute(upload.preview())
        receiver = new_task(str(home), direction="download", remote_path=publisher.remote_path,
                            versions_path=publisher.versions_path, dataset_id=publisher.dataset_id)
        pull = self.engine(receiver)
        pull.execute(pull.preview())
        (office / "file.txt").write_bytes(b"BBBB")
        upload.execute(upload.preview())
        original = self.client.popen

        def interrupt_destination(args, *positional, **kwargs):
            if args[:1] == ["copy"] and args[2] == str(home):
                raise RuntimeError("simulated interrupted receive")
            return original(args, *positional, **kwargs)

        with patch.object(self.client, "popen", side_effect=interrupt_destination):
            with self.assertRaisesRegex(RuntimeError, "interrupted receive"):
                pull.execute(pull.preview())
        coordinator = DatasetCoordinator(self.client, publisher)
        published = coordinator.read()
        for phase, digest in (("publishing", published["content_digest"]), ("ready", "0" * 64)):
            with self.subTest(phase=phase):
                coordinator.write(phase, published["generation"], content_digest=digest,
                                  protocol=published["version"])
                recovery = self.engine(receiver, operation="recover")
                report = recovery.preview()
                self.assertTrue(report.blocked, report.reason)
                with self.assertRaises(RuntimeError):
                    recovery.execute(report)
                self.assertEqual((home / "file.txt").read_bytes(), b"AAAA")
        coordinator.write("ready", published["generation"], content_digest=published["content_digest"],
                          protocol=published["version"])
        recovery = self.engine(receiver, operation="recover")
        report = recovery.preview()
        self.assertFalse(report.blocked, report.reason)
        recovery.execute(report)
        self.assertEqual((home / "file.txt").read_bytes(), b"BBBB")

    def test_missing_or_external_remote_never_clears_download_destination(self):
        office = self.base / "office"
        home = self.base / "home"
        office.mkdir()
        home.mkdir()
        (office / "file.txt").write_bytes(b"AAAA")
        publisher = new_task(str(office))
        upload = self.engine(publisher)
        upload.execute(upload.preview())
        receiver = new_task(str(home), direction="download", remote_path=publisher.remote_path,
                            versions_path=publisher.versions_path, dataset_id=publisher.dataset_id)
        pull = self.engine(receiver)
        pull.execute(pull.preview())
        mirror = self.remote / publisher.remote_path
        (mirror / "file.txt").write_bytes(b"BBBB")
        report = self.engine(receiver).preview()
        self.assertTrue(report.blocked)
        self.assertEqual((home / "file.txt").read_bytes(), b"AAAA")
        (mirror / "file.txt").unlink()
        mirror.rmdir()
        with self.assertRaises(RuntimeError):
            self.engine(receiver).preview()
        self.assertEqual((home / "file.txt").read_bytes(), b"AAAA")

    def test_two_mirrors_keep_their_own_history_without_cross_deletions(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            remote = base / "pretend-phone"
            remote.mkdir()
            conf = base / "rclone.conf"
            conf.write_text(f"[phone]\ntype = alias\nremote = {remote.as_posix()}\n", encoding="utf-8")
            client = Rclone(Config(base / "config.json"), conf_path=conf)
            sources = [base / "first", base / "second"]
            for folder in sources:
                folder.mkdir()
            (sources[0] / "document.txt").write_text("first revision", encoding="utf-8")
            (sources[0] / "removed.txt").write_text("keep my old contents", encoding="utf-8")
            (sources[1] / "private.txt").write_text("second task only", encoding="utf-8")
            first, second = [new_task(str(folder)) for folder in sources]
            from app.core.relay import LocalDatasetLease
            engine_first = SyncEngine(client, first, log_directory=base / "logs", state_directory=base / "baselines", lease_factory=lambda: LocalDatasetLease(remote, first))
            engine_second = SyncEngine(client, second, log_directory=base / "logs", state_directory=base / "baselines", lease_factory=lambda: LocalDatasetLease(remote, second))
            baseline = engine_first.preview()
            self.assertEqual((baseline.added, baseline.deleted), (2, 0))
            engine_first.execute(baseline)
            engine_second.execute(engine_second.preview())
            self.assertEqual(engine_first.preview().added, 0)
            (sources[0] / "document.txt").write_text("a newer, longer revision", encoding="utf-8")
            (sources[0] / "removed.txt").unlink()
            changes = engine_first.preview()
            self.assertEqual((changes.updated, changes.deleted), (1, 1))
            stamp = engine_first.execute(changes)
            self.assertEqual((remote / first.remote_path / "document.txt").read_text(), "a newer, longer revision")
            self.assertFalse((remote / first.remote_path / "removed.txt").exists())
            history = remote / first.versions_path / stamp
            self.assertEqual((history / "document.txt").read_text(), "first revision")
            self.assertEqual((history / "removed.txt").read_text(), "keep my old contents")
            self.assertEqual((remote / second.remote_path / "private.txt").read_text(), "second task only")
            self.assertFalse((remote / second.versions_path / stamp / "document.txt").exists())


if __name__ == "__main__":
    unittest.main()
