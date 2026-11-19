from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, nullcontext
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from app.core.rclone import Result
from app.core.tasks import MirrorTask
from app.core.history import HistoryService


VERSION = "2026-11-14_153000"
OLDER = "2026-11-13_120000"


def setUpModule():
    sandbox = tempfile.TemporaryDirectory(prefix="PhoneNAS-history-tests-")
    unittest.addModuleCleanup(sandbox.cleanup)
    environment = patch.dict(os.environ, {
        "PHONENAS_DATA_DIR": str(Path(sandbox.name) / "data"),
        "APPDATA": str(Path(sandbox.name) / "appdata"),
        "QT_QPA_PLATFORM": "offscreen",
    })
    environment.start()
    unittest.addModuleCleanup(environment.stop)


class HistoryInventoryTests(unittest.TestCase):
    def test_portable_unicode_names_are_preserved_without_normalization(self):
        from app.core.history import validate_history_inventory

        paths = ["私有符号-\ue000.txt", "未分配-\u0378.txt", "emoji-👩\u200d💻.txt", "text-\u200c.txt"]
        self.assertEqual(validate_history_inventory(paths), sorted(paths))

    def test_rejected_name_identifies_path_and_invisible_character_safely(self):
        from app.core.history import validate_history_inventory

        for character in ("\x85", "\u061c", "\u200e", "\u200f", "\u202a",
                          "\u202e", "\u2066", "\u2069", "\ud800", "\udfff", "\r", "\n"):
            path = f"drawings/{character}260702/part.txt"
            with self.subTest(character=ascii(character)):
                with self.assertRaises(ValueError) as caught:
                    validate_history_inventory([path])
                message = str(caught.exception)
                self.assertIn("drawings/", message)
                self.assertIn("260702/part.txt", message)
                self.assertIn(f"U+{ord(character):04X}", message)
                self.assertNotIn(character, message)
                self.assertNotIn("历史文件路径", message)

    def test_shared_validator_rejects_portable_aliases_including_empty_directories(self):
        from app.core.history import validate_history_inventory

        self.assertEqual(
            validate_history_inventory(["z.txt", "photos/image.jpg"], ["empty", "photos"]),
            ["photos/image.jpg", "z.txt"],
        )
        for files, directories in [
            (["safe.txt", "../escape"], []),
            (["Report.txt", "report.txt"], []),
            (["é/one.txt"], ["e\u0301"]),
            (["node"], ["node/empty"]),
            ([], ["Empty", "empty"]),
            ([], ["NUL"]),
        ]:
            with self.subTest(files=files, directories=directories):
                with self.assertRaises(ValueError):
                    validate_history_inventory(files, directories)


class FakeRclone:
    """Offline boundary: only record arguments; never spawn rclone or write config."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.dirs = Result(True, f"{OLDER}/\n{VERSION}/\n", "", 0)
        self.files = Result(True, "notes.txt\nphotos/image.jpg\n", "", 0)
        self.inventory = Result(True, json.dumps([
            {"Path": "notes.txt", "IsDir": False, "Size": 5},
            {"Path": "photos/image.jpg", "IsDir": False, "Size": 8},
        ]), "", 0)
        self.copied = Result(True, "", "", 0)
        self.purged = Result(True, "", "", 0)

    def lsf_dirs(self, path):
        self.calls.append(("lsf_dirs", path))
        return self.dirs

    def lsf_files(self, path):
        self.calls.append(("lsf_files", path))
        return self.files

    def run(self, args, timeout=120):
        self.calls.append(("run", tuple(args), timeout))
        if args[:1] == ["cat"] or "--stat" in args:
            return Result(False, "", "missing", 3)
        return self.inventory

    def copyto(self, source, destination):
        self.calls.append(("copyto", source, destination))
        return self.copied

    def purge(self, path):
        self.calls.append(("purge", path))
        return self.purged


def make_task(local_path: Path, **changes) -> MirrorTask:
    return replace(
        MirrorTask(
            id="task-a", name="工作", enabled=True, local_path=str(local_path),
            remote_path="backup/task-a/mirror", versions_path="backup/task-a/versions",
            interval_minutes=20, retention_days=30, exclude_preset="custom",
            excludes=(), last_sync_at="", last_file_count=0,
        ),
        **changes,
    )


class HistoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = make_task(self.root / "source")
        self.client = FakeRclone()
        lease = patch("app.core.relay.DatasetCoordinator._new_lease", return_value=nullcontext())
        lease.start()
        self.addCleanup(lease.stop)

    def test_versions_only_show_strict_real_timestamps_from_selected_task(self):
        self.client.dirs = Result(
            True,
            f"{OLDER}/\n{VERSION}/\njunk/\n2026-02-30_120000/\n"
            f"../{VERSION}/\n/{VERSION}/\n {VERSION}/\n{VERSION} /\n"
            "2026-11-14_250000/\n２０２６-11-14_153000/\n",
            "", 0,
        )
        service = HistoryService(self.client, self.task)
        self.assertEqual(service.list_versions(), [VERSION, OLDER])
        self.assertEqual(self.client.calls, [("lsf_dirs", "backup/task-a/versions")])

    def test_file_inventory_is_validated_json_not_lossy_newline_text(self):
        service = HistoryService(self.client, self.task)
        self.assertEqual(service.list_files(VERSION), ["notes.txt", "photos/image.jpg"])
        self.assertEqual(self.client.calls[-1][0], "run")
        self.assertEqual(self.client.calls[-1][1][:2], (
            "lsjson", f"phone:backup/task-a/versions/{VERSION}",
        ))
        self.client.inventory = Result(True, json.dumps([
            {"Path": "safe.txt", "IsDir": False},
            {"Path": "hidden\nname.txt", "IsDir": False},
        ]), "", 0)
        with self.assertRaises(ValueError):
            service.list_files(VERSION)

    def test_unsafe_remote_files_are_rejected_as_a_whole(self):
        unsafe = [
            "../escape.txt", "dir/../../escape.txt", "/absolute.txt", "C:/drive.txt",
            "C:drive.txt", "//server/share/file.txt", "dir\\file.txt", "dir//file.txt",
            "dir/./file.txt", "dir/../file.txt", "dir/", "file.txt.", "file.txt ",
            "dir./file.txt", "dir /file.txt", "file:stream", "NUL", "CON.txt",
            "aux/data.txt", "COM1.log", "LPT9", "COM¹.txt", "conin$", "CLOCK$.log",
            "bad\x00name", "bad\rname", "bad\tname", "bad\u202ename",
            "question?.txt", "star*.txt", 'quote".txt', "pipe|.txt", "less<.txt",
        ]
        for path in unsafe:
            with self.subTest(path=repr(path)):
                self.client.inventory = Result(True, json.dumps([
                    {"Path": "safe.txt", "IsDir": False}, {"Path": path, "IsDir": False},
                ]), "", 0)
                with self.assertRaises(ValueError):
                    HistoryService(self.client, self.task).list_files(VERSION)
        self.assertFalse(any(call[0] in ("copyto", "purge") for call in self.client.calls))

    def test_windows_case_and_file_directory_aliases_abort_inventory(self):
        collisions = [
            ["Report.txt", "report.txt"], ["dir/A.txt", "DIR/B.txt"],
            ["node", "node/child.txt"], ["node/child.txt", "NODE"],
            ["same.txt", "same.txt"], ["é.txt", "e\u0301.txt"],
        ]
        for paths in collisions:
            with self.subTest(paths=paths):
                self.client.inventory = Result(True, json.dumps([
                    {"Path": path, "IsDir": False} for path in paths
                ]), "", 0)
                with self.assertRaises(ValueError):
                    HistoryService(self.client, self.task).list_files(VERSION)

    def test_invalid_task_roots_and_versions_never_reach_rclone(self):
        roots = ["", "/backup/a", "../other", "phone:backup/a", "backup\\a", "backup/a ", "backup/NUL"]
        for root in roots:
            with self.subTest(root=root):
                self.client.calls.clear()
                with self.assertRaises(ValueError):
                    HistoryService(self.client, replace(self.task, versions_path=root)).list_versions()
                self.assertEqual(self.client.calls, [])
        for remote in ["/mirror", "C:mirror", "backup/task-a/versions", "backup/task-a/versions/inside"]:
            with self.subTest(remote=remote):
                with self.assertRaises(ValueError):
                    HistoryService(self.client, replace(self.task, remote_path=remote)).list_versions()
        service = HistoryService(self.client, self.task)
        for version in [f"../{VERSION}", f"{VERSION}/", f" {VERSION}", "2026-02-30_120000", "junk"]:
            with self.subTest(version=version):
                self.client.calls.clear()
                with self.assertRaises(ValueError):
                    service.list_files(version)
                self.assertEqual(self.client.calls, [])

    def test_restore_uses_only_listed_files_in_a_fresh_exclusive_child(self):
        destination = self.root / "chosen"
        destination.mkdir()
        existing = destination / VERSION
        existing.mkdir()
        (existing / "notes.txt").write_text("must stay", encoding="utf-8")
        service = HistoryService(self.client, self.task)
        target = service.restore(VERSION, destination)
        second = service.restore(VERSION, destination, files=["photos/image.jpg"])
        self.assertEqual(target.parent, destination.resolve())
        self.assertTrue(target.is_dir())
        self.assertNotEqual(target, existing)
        self.assertNotEqual(target, second)
        self.assertEqual((existing / "notes.txt").read_text(encoding="utf-8"), "must stay")
        copies = [call for call in self.client.calls if call[0] == "copyto"]
        self.assertEqual([call[1] for call in copies], [
            f"phone:backup/task-a/versions/{VERSION}/notes.txt",
            f"phone:backup/task-a/versions/{VERSION}/photos/image.jpg",
            f"phone:backup/task-a/versions/{VERSION}/photos/image.jpg",
        ])
        self.assertEqual(Path(copies[0][2]), target / "notes.txt")
        self.assertEqual(Path(copies[1][2]), target / "photos/image.jpg")
        self.assertEqual(Path(copies[2][2]), second / "photos/image.jpg")
        self.assertFalse(any(call[0] == "copy" for call in self.client.calls))

    def test_delete_requires_fresh_membership_and_safe_inventory_before_purge(self):
        service = HistoryService(self.client, self.task)
        self.client.dirs = Result(True, f"{OLDER}/\n", "", 0)
        with self.assertRaises(ValueError):
            service.delete(VERSION)
        self.assertFalse(any(call[0] == "purge" for call in self.client.calls))
        self.client.dirs = Result(True, f"{VERSION}/\n", "", 0)
        self.client.inventory = Result(True, '[{"Path":"../escape","IsDir":false}]', "", 0)
        with self.assertRaises(ValueError):
            service.delete(VERSION)
        self.assertFalse(any(call[0] == "purge" for call in self.client.calls))
        self.client.inventory = Result(True, '[]', "", 0)
        self.client.calls.clear()
        service.delete(VERSION)
        self.assertEqual([call[0] for call in self.client.calls], ["lsf_dirs", "run", "run", "run", "run", "purge"])
        self.assertEqual(self.client.calls[-1], ("purge", f"backup/task-a/versions/{VERSION}"))

    def test_restore_validates_all_files_before_writes_and_reports_partial_failure(self):
        destination = self.root / "recovery"
        destination.mkdir()
        self.client.inventory = Result(True, json.dumps([
            {"Path": "notes.txt", "IsDir": False}, {"Path": "../escape", "IsDir": False},
        ]), "", 0)
        service = HistoryService(self.client, self.task)
        with self.assertRaises(ValueError):
            service.restore(VERSION, destination, ["notes.txt"])
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(any(call[0] == "copyto" for call in self.client.calls))
        self.client.inventory = Result(True, '[{"Path":"notes.txt","IsDir":false}]', "", 0)
        self.client.copied = Result(False, "", "offline fake failure", 1)
        with self.assertRaisesRegex(RuntimeError, "未全部完成.*0/1") as caught:
            service.restore(VERSION, destination)
        self.assertIn("部分结果保留", str(caught.exception))
        self.assertEqual(len(list(destination.iterdir())), 1)

    def test_restore_refuses_source_directory_and_junction_ancestor(self):
        source = Path(self.task.local_path)
        source.mkdir()
        service = HistoryService(self.client, self.task)
        with self.assertRaises(ValueError):
            service.restore(VERSION, source)
        self.assertEqual(list(source.iterdir()), [])
        from types import SimpleNamespace
        real_lstat = Path.lstat
        chosen = self.root / "junction"
        chosen.mkdir()
        def junction_stat(path, *args, **kwargs):
            if path == chosen:
                return SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", junction_stat):
            with self.assertRaises(ValueError):
                service.restore(VERSION, chosen)
        self.assertFalse(any(call[0] == "copyto" for call in self.client.calls))

    def test_online_errors_and_malformed_inventory_never_look_empty(self):
        service = HistoryService(self.client, self.task)
        self.client.dirs = Result(False, "", "offline list failure", 1)
        with self.assertRaisesRegex(RuntimeError, "offline list failure"):
            service.list_versions()
        for result in [Result(False, "", "offline json failure", 1), Result(True, "not json", "", 0), Result(True, '{}', "", 0)]:
            self.client.inventory = result
            with self.assertRaises(RuntimeError):
                service.list_files(VERSION)


class LocalHistoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="PhoneNAS-local-history-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = make_task(self.root / "mirror", direction="download")
        self.history = Path(self.task.local_history_path)

    def service(self, task=None):
        from app.core.history import LocalHistoryService
        return LocalHistoryService(task or self.task)

    def test_versions_are_strict_real_time_directories_and_missing_history_is_empty(self):
        service = self.service()
        self.assertEqual(service.list_versions(), [])
        self.assertFalse(self.history.exists(), "Browsing must not create a history root")
        self.history.mkdir()
        for name in [VERSION, OLDER, "junk", "2026-02-30_120000", "2026-11-14_250000", "２０２６-11-14_153000"]:
            (self.history / name).mkdir()
        (self.history / "2026-11-12_120000").write_text("not a directory", encoding="utf-8")
        with patch("subprocess.Popen", side_effect=AssertionError("Local history must remain offline")):
            self.assertEqual(service.list_versions(), [VERSION, OLDER])

    def test_history_root_must_be_absolute_portable_and_disjoint_from_local_mirror(self):
        for history in [
            "relative/history", str(self.root.anchor), str(self.root),
            self.task.local_path, str(Path(self.task.local_path) / "history"),
            str(self.root / "MIRROR" / "history"), str(self.root / "NUL"),
            str(self.root / "history."), str(self.root / "history:stream"),
            str(self.root / "bad\nname"),
        ]:
            with self.subTest(history=history):
                with self.assertRaises(ValueError):
                    self.service(replace(self.task, local_history_path=history))
        with self.assertRaises(ValueError):
            self.service(replace(
                self.task, local_path=str(self.root / "é"),
                local_history_path=str(self.root / "e\u0301" / "history"),
            )).list_versions()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_files_are_relative_sorted_and_only_from_the_selected_local_batch(self):
        batch = self.history / VERSION
        (batch / "photos").mkdir(parents=True)
        (batch / "empty").mkdir()
        (batch / "z.txt").write_bytes(b"old text")
        (batch / "photos" / "image.jpg").write_bytes(b"old image")
        other = self.history / OLDER
        other.mkdir()
        (other / "other.txt").write_bytes(b"other batch")
        self.assertEqual(self.service().list_files(VERSION), ["photos/image.jpg", "z.txt"])
        self.assertEqual(self.service().list_files(OLDER), ["other.txt"])
        for version in [f"../{VERSION}", f"{VERSION}/", f" {VERSION}", "2026-02-30_120000", "junk"]:
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    self.service().list_files(version)

    def test_restore_copies_old_bytes_into_a_new_exclusive_child_and_never_overwrites(self):
        batch = self.history / VERSION
        (batch / "photos").mkdir(parents=True)
        (batch / "notes.txt").write_bytes(b"old notes\x00\xff")
        (batch / "photos" / "image.jpg").write_bytes(b"old image")
        mirror = Path(self.task.local_path)
        mirror.mkdir()
        (mirror / "notes.txt").write_bytes(b"current notes")
        chosen = self.root / "chosen"
        chosen.mkdir()
        existing = chosen / VERSION
        existing.mkdir()
        (existing / "notes.txt").write_bytes(b"must stay")
        service = self.service()
        with patch("subprocess.Popen", side_effect=AssertionError("Local restore must remain offline")):
            target = service.restore(VERSION, chosen)
            selected = service.restore(VERSION, chosen, files=["photos/image.jpg"])
        self.assertEqual(target.parent, chosen.resolve())
        self.assertNotEqual(target, selected)
        self.assertNotEqual(target, existing)
        self.assertEqual((target / "notes.txt").read_bytes(), b"old notes\x00\xff")
        self.assertEqual((target / "photos" / "image.jpg").read_bytes(), b"old image")
        self.assertEqual((selected / "photos" / "image.jpg").read_bytes(), b"old image")
        self.assertFalse((selected / "notes.txt").exists())
        self.assertEqual((existing / "notes.txt").read_bytes(), b"must stay")
        self.assertEqual((mirror / "notes.txt").read_bytes(), b"current notes")
        self.assertEqual((batch / "notes.txt").read_bytes(), b"old notes\x00\xff")

    def test_restore_rejects_any_mirror_or_history_overlap_before_creating_children(self):
        batch = self.history / VERSION
        batch.mkdir(parents=True)
        (batch / "notes.txt").write_bytes(b"old notes")
        mirror = Path(self.task.local_path)
        (mirror / "nested").mkdir(parents=True)
        for chosen in [mirror, mirror / "nested", self.history, batch, self.root]:
            with self.subTest(chosen=chosen):
                before = sorted(str(path) for path in self.root.rglob("*"))
                with self.assertRaises(ValueError):
                    self.service().restore(VERSION, chosen)
                self.assertEqual(sorted(str(path) for path in self.root.rglob("*")), before)

    def test_restore_rejects_nonlisted_duplicate_or_empty_selections_before_writes(self):
        batch = self.history / VERSION
        batch.mkdir(parents=True)
        (batch / "notes.txt").write_bytes(b"old notes")
        chosen = self.root / "chosen"
        chosen.mkdir()
        for selection in [[], "notes.txt", b"notes.txt", ["missing.txt"], ["notes.txt", "notes.txt"], ["../escape"], ["NOTES.txt"]]:
            with self.subTest(selection=selection):
                with self.assertRaises(ValueError):
                    self.service().restore(VERSION, chosen, selection)
                self.assertEqual(list(chosen.iterdir()), [])
        (batch / "notes.txt").unlink()
        with self.assertRaises(ValueError):
            self.service().restore(VERSION, chosen)
        self.assertEqual(list(chosen.iterdir()), [])

    def test_delete_removes_only_a_freshly_validated_batch_including_empty_directories(self):
        batch = self.history / VERSION
        (batch / "nested" / "empty").mkdir(parents=True)
        (batch / "notes.txt").write_bytes(b"old notes")
        other = self.history / OLDER
        other.mkdir()
        (other / "keep.txt").write_bytes(b"keep other batch")
        unrelated = self.history / "unmanaged"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_bytes(b"keep unmanaged")
        service = self.service()
        self.assertEqual(service.list_versions(), [VERSION, OLDER])
        with patch("subprocess.Popen", side_effect=AssertionError("Local delete must remain offline")):
            service.delete(VERSION)
        self.assertEqual(service.list_versions(), [OLDER])
        self.assertFalse(batch.exists())
        self.assertEqual((other / "keep.txt").read_bytes(), b"keep other batch")
        self.assertEqual((unrelated / "keep.txt").read_bytes(), b"keep unmanaged")
        for version in [VERSION, "unmanaged", f"../{OLDER}"]:
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    service.delete(version)
        self.assertEqual((other / "keep.txt").read_bytes(), b"keep other batch")

    def test_unreadable_local_history_is_an_error_not_an_empty_success(self):
        self.history.mkdir()
        service = self.service()
        real_iterdir = Path.iterdir
        def blocked_iterdir(path):
            if path == self.history:
                raise PermissionError("history permission denied")
            return real_iterdir(path)
        with patch.object(Path, "iterdir", blocked_iterdir):
            with self.assertRaisesRegex(RuntimeError, "history permission denied"):
                service.list_versions()
        self.history.rmdir()
        self.history.write_bytes(b"not a history directory")
        with self.assertRaises(RuntimeError):
            service.list_versions()
        self.assertEqual(self.history.read_bytes(), b"not a history directory")

    def test_selected_restore_and_delete_validate_the_entire_fresh_inventory(self):
        batch = self.history / VERSION
        batch.mkdir(parents=True)
        (batch / "notes.txt").write_bytes(b"old notes")
        chosen = self.root / "chosen"
        chosen.mkdir()
        service = self.service()
        self.assertEqual(service.list_files(VERSION), ["notes.txt"])
        # Two distinct Unicode spellings are legal on NTFS and many Unix disks,
        # but would alias on another destination and must invalidate the tree.
        first, second = batch / "é.txt", batch / "e\u0301.txt"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        for action in [lambda: service.list_files(VERSION), lambda: service.restore(VERSION, chosen, ["notes.txt"]), lambda: service.delete(VERSION)]:
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(list(chosen.iterdir()), [])
        self.assertEqual((batch / "notes.txt").read_bytes(), b"old notes")
        self.assertEqual(first.read_bytes(), b"one")
        self.assertEqual(second.read_bytes(), b"two")

    def test_reparse_points_in_ancestors_batches_or_files_abort_without_following(self):
        batch = self.history / VERSION
        (batch / "nested").mkdir(parents=True)
        (batch / "notes.txt").write_bytes(b"old notes")
        (batch / "nested" / "photo.jpg").write_bytes(b"old image")
        chosen = self.root / "chosen"
        (chosen / "inside").mkdir(parents=True)
        service = self.service()
        real_lstat = Path.lstat
        for unsafe in [self.root, self.history, batch, batch / "nested", batch / "notes.txt", chosen]:
            with self.subTest(unsafe=unsafe):
                def reparse_stat(path, *args, **kwargs):
                    info = real_lstat(path, *args, **kwargs)
                    if path == unsafe:
                        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                    return info
                with patch.object(Path, "lstat", reparse_stat):
                    if unsafe in (self.root, self.history, batch):
                        with self.assertRaises(ValueError):
                            service.list_versions()
                    if unsafe != chosen:
                        with self.assertRaises(ValueError):
                            service.list_files(VERSION)
                        with self.assertRaises(ValueError):
                            service.delete(VERSION)
                    with self.assertRaises(ValueError):
                        service.restore(VERSION, chosen / "inside", ["notes.txt"])
        self.assertEqual((batch / "notes.txt").read_bytes(), b"old notes")
        self.assertEqual((batch / "nested" / "photo.jpg").read_bytes(), b"old image")
        self.assertEqual(list((chosen / "inside").iterdir()), [])

    def test_real_symlinked_batch_never_exposes_or_deletes_its_target(self):
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_bytes(b"outside target")
        self.history.mkdir()
        link = self.history / VERSION
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"This host cannot create temporary symlinks: {exc}")
        chosen = self.root / "chosen"
        chosen.mkdir()
        service = self.service()
        for action in [service.list_versions, lambda: service.list_files(VERSION), lambda: service.restore(VERSION, chosen), lambda: service.delete(VERSION)]:
            with self.assertRaises(ValueError):
                action()
        self.assertTrue(link.is_symlink())
        self.assertEqual(marker.read_bytes(), b"outside target")
        self.assertEqual(list(chosen.iterdir()), [])

    def test_restore_failure_preserves_completed_files_and_reports_partial_output_path(self):
        batch = self.history / VERSION
        batch.mkdir(parents=True)
        (batch / "a.txt").write_bytes(b"first old file")
        (batch / "b.txt").write_bytes(b"second old file")
        chosen = self.root / "chosen"
        chosen.mkdir()
        real_open = Path.open
        def failed_read(path, mode="r", *args, **kwargs):
            if path == batch / "b.txt" and mode == "rb":
                raise OSError("simulated disk read failure")
            return real_open(path, mode, *args, **kwargs)
        with patch.object(Path, "open", failed_read):
            with self.assertRaisesRegex(RuntimeError, "未全部完成.*1/2") as caught:
                self.service().restore(VERSION, chosen)
        target, = chosen.iterdir()
        self.assertIn(str(target), str(caught.exception))
        self.assertIn("部分结果保留", str(caught.exception))
        self.assertIn("simulated disk read failure", str(caught.exception))
        self.assertEqual((target / "a.txt").read_bytes(), b"first old file")
        self.assertEqual((batch / "a.txt").read_bytes(), b"first old file")
        self.assertEqual((batch / "b.txt").read_bytes(), b"second old file")

    def test_delete_failure_reports_partial_deletion_and_keeps_remaining_files(self):
        batch = self.history / VERSION
        batch.mkdir(parents=True)
        (batch / "a.txt").write_bytes(b"first old file")
        (batch / "b.txt").write_bytes(b"second old file")
        real_unlink = Path.unlink
        def failed_delete(path, *args, **kwargs):
            if path == batch / "b.txt":
                raise PermissionError("simulated locked file")
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", failed_delete):
            with self.assertRaisesRegex(RuntimeError, "可能已部分删除") as caught:
                self.service().delete(VERSION)
        self.assertIn(str(batch), str(caught.exception))
        self.assertIn("simulated locked file", str(caught.exception))
        self.assertFalse((batch / "a.txt").exists())
        self.assertEqual((batch / "b.txt").read_bytes(), b"second old file")

    def test_restore_rechecks_source_links_after_opening_before_reading_any_bytes(self):
        batch = self.history / VERSION
        batch.mkdir(parents=True)
        source = batch / "notes.txt"
        source.write_bytes(b"must not be copied after replacement")
        chosen = self.root / "chosen"
        chosen.mkdir()
        changed = False
        real_open, real_lstat = Path.open, Path.lstat
        def replaced_on_open(path, mode="r", *args, **kwargs):
            nonlocal changed
            handle = real_open(path, mode, *args, **kwargs)
            if path == source and mode == "rb":
                changed = True
            return handle
        def replaced_stat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if changed and path == source:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, "open", replaced_on_open), patch.object(Path, "lstat", replaced_stat):
            with self.assertRaisesRegex(RuntimeError, "未全部完成.*0/1"):
                self.service().restore(VERSION, chosen)
        target, = chosen.iterdir()
        self.assertFalse((target / "notes.txt").exists())
        self.assertEqual(source.read_bytes(), b"must not be copied after replacement")

    def test_delete_rechecks_ancestors_between_files_and_never_recurses_into_a_new_junction(self):
        batch = self.history / VERSION
        nested = batch / "nested"
        nested.mkdir(parents=True)
        (batch / "a.txt").write_bytes(b"first old file")
        (nested / "b.txt").write_bytes(b"must remain")
        changed = False
        real_unlink, real_lstat = Path.unlink, Path.lstat
        def replace_after_unlink(path, *args, **kwargs):
            nonlocal changed
            result = real_unlink(path, *args, **kwargs)
            if path == batch / "a.txt":
                changed = True
            return result
        def replaced_stat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if changed and path == nested:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, "unlink", replace_after_unlink), patch.object(Path, "lstat", replaced_stat):
            with self.assertRaisesRegex(RuntimeError, "可能已部分删除"):
                self.service().delete(VERSION)
        self.assertEqual((nested / "b.txt").read_bytes(), b"must remain")

    def test_real_junctioned_history_ancestor_is_rejected_before_resolve_or_delete(self):
        if os.name != "nt":
            self.skipTest("Windows junction boundary")
        import _winapi
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_bytes(b"outside target")
        junction = self.root / "junction"
        _winapi.CreateJunction(str(outside), str(junction))
        try:
            task = replace(self.task, local_history_path=str(junction / "history"))
            with self.assertRaises(ValueError):
                self.service(task)
            self.history.mkdir()
            batch = self.history / VERSION
            _winapi.CreateJunction(str(outside), str(batch))
            try:
                with self.assertRaises(ValueError):
                    self.service().delete(VERSION)
                self.assertEqual(marker.read_bytes(), b"outside target")
            finally:
                batch.rmdir()
        finally:
            junction.rmdir()

    def test_local_inventory_reports_permission_errors_even_at_ancestor_checks(self):
        batch = self.history / VERSION
        batch.mkdir(parents=True)
        (batch / "notes.txt").write_bytes(b"old notes")
        service = self.service()
        real_lstat = Path.lstat
        def blocked_stat(path, *args, **kwargs):
            if path == self.history:
                raise PermissionError("history ancestor denied")
            return real_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", blocked_stat):
            with self.assertRaisesRegex(RuntimeError, "history ancestor denied"):
                service.list_files(VERSION)

    def test_local_inventory_refuses_reserved_names_before_any_device_path_lookup(self):
        batch = self.history / VERSION
        batch.mkdir(parents=True)
        service = self.service()
        real_iterdir, real_lstat = Path.iterdir, Path.lstat
        for name in ["NUL", "CON.txt", "LPT9", "file:stream", "bad\nname", "trailing."]:
            unsafe = batch / name
            with self.subTest(name=name):
                def unsafe_listing(path):
                    if path == batch:
                        return iter([unsafe])
                    return real_iterdir(path)
                def no_unsafe_lookup(path, *args, **kwargs):
                    if path == unsafe:
                        raise AssertionError("Unsafe entry reached a filesystem lookup")
                    return real_lstat(path, *args, **kwargs)
                with patch.object(Path, "iterdir", unsafe_listing), patch.object(Path, "lstat", no_unsafe_lookup):
                    with self.assertRaises(ValueError):
                        service.list_files(VERSION)

    def test_case_colliding_empty_directories_invalidate_listing_restore_and_delete(self):
        batch = self.history / VERSION
        (batch / "Empty").mkdir(parents=True)
        (batch / "notes.txt").write_bytes(b"old notes")
        chosen = self.root / "chosen"
        chosen.mkdir()
        real_iterdir = Path.iterdir
        def colliding_listing(path):
            if path == batch:
                return iter([batch / "Empty", batch / "EMPTY", batch / "notes.txt"])
            return real_iterdir(path)
        service = self.service()
        with patch.object(Path, "iterdir", colliding_listing):
            for action in [lambda: service.list_files(VERSION), lambda: service.restore(VERSION, chosen, ["notes.txt"]), lambda: service.delete(VERSION)]:
                with self.assertRaises(ValueError):
                    action()
        self.assertTrue((batch / "Empty").is_dir())
        self.assertEqual((batch / "notes.txt").read_bytes(), b"old notes")
        self.assertEqual(list(chosen.iterdir()), [])

    def test_restore_rechecks_new_child_safety_before_creating_subdirectories(self):
        batch = self.history / VERSION
        (batch / "nested").mkdir(parents=True)
        (batch / "nested" / "notes.txt").write_bytes(b"old notes")
        chosen = self.root / "chosen"
        chosen.mkdir()
        service = self.service()
        unsafe_child = chosen / "PhoneNAS-replaced"
        real_lstat, real_mkdir = Path.lstat, Path.mkdir
        def changed_child(path, *args, **kwargs):
            if path == unsafe_child:
                return SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400)
            return real_lstat(path, *args, **kwargs)
        def no_child_creation(path, *args, **kwargs):
            if path == unsafe_child or unsafe_child in path.parents:
                raise AssertionError("Restore wrote through an unsafe recovery child")
            return real_mkdir(path, *args, **kwargs)
        with patch("app.core.history.tempfile.mkdtemp", return_value=str(unsafe_child)), patch.object(Path, "lstat", changed_child), patch.object(Path, "mkdir", no_child_creation):
            with self.assertRaisesRegex(RuntimeError, "未全部完成.*0/1") as caught:
                service.restore(VERSION, chosen)
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertEqual(list(chosen.iterdir()), [])


class MemoryConfig:
    """The public Config seam without persistence, secrets or APPDATA access."""

    def __init__(self, tasks):
        self.tasks = tuple(tasks)
        self.snapshot_count = 0

    def get_task(self, task_id):
        return next((task for task in self.tasks if task.id == task_id), None)

    def snapshot(self):
        self.snapshot_count += 1
        return MemoryConfig(self.tasks)

    def get(self, dotted, default=None):
        return default


class FakeWorkerRclone(FakeRclone):
    def __init__(self, config, conf_path=None):
        super().__init__()
        self.config = config
        self.conf_path = conf_path
        self.threads = [threading.get_ident()]
        self.entered = threading.Event()
        self.released = threading.Event()
        self.cancelled = threading.Event()

    @contextmanager
    def isolated_conf(self):
        self.threads.append(threading.get_ident())
        self.calls.append(("isolated_conf",))
        yield self

    def ensure_conf(self):
        self.threads.append(threading.get_ident())
        self.calls.append(("ensure_conf",))

    def cancel(self):
        self.cancelled.set()
        self.released.set()

    def lsf_dirs(self, path):
        self.threads.append(threading.get_ident())
        self.entered.set()
        self.released.wait(5)
        if self.cancelled.is_set():
            return Result(False, "", "操作已取消", -1)
        return super().lsf_dirs(path)


class VersionsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        lease = patch("app.core.relay.DatasetCoordinator._new_lease", return_value=nullcontext())
        lease.start()
        self.addCleanup(lease.stop)
        self.config = MemoryConfig([
            make_task(root / "a"),
            make_task(root / "b", id="task-b", name="照片", remote_path="backup/task-b/mirror", versions_path="backup/task-b/versions"),
        ])

    def test_load_and_safe_mode_never_construct_network_client(self):
        from app.ui.pages.versions import VersionsPage
        with patch("app.ui.pages.versions.Rclone", side_effect=AssertionError("network client on UI thread")):
            page = VersionsPage(self.config, operational=False)
            self.addCleanup(page.deleteLater)
            self.assertEqual(page.task_combo.count(), 2)
            page.load()
            page.load_tasks()
            page.select_task("task-b")
            page.refresh()
            self.assertEqual(page.task_combo.currentData(), "task-b")
            self.assertFalse(page.busy)
            self.assertTrue(page.shutdown())
            self.assertEqual(page.restore_all_button.text(), "恢复这批旧文件")
            self.assertIn("安全", page.status.text())

    def wait_until(self, predicate, timeout=3):
        until = time.monotonic() + timeout
        while not predicate() and time.monotonic() < until:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate(), "Qt operation did not settle")

    def test_refresh_freezes_task_snapshot_and_global_gate_until_thread_exit(self):
        from app.ui.pages.versions import VersionsPage
        clients = []
        def factory(config, conf_path=None):
            client = FakeWorkerRclone(config, conf_path)
            clients.append(client)
            return client
        with patch("app.ui.pages.versions.Rclone", side_effect=factory):
            page = VersionsPage(self.config)
            self.addCleanup(page.deleteLater)
            signals = []
            events = []
            page.operation_started.connect(lambda: signals.append("started"))
            page.operation_finished.connect(lambda: signals.append("finished"))
            page.event_recorded.connect(events.append)
            page.set_sync_busy(True)
            page.refresh()
            self.assertFalse(page.busy)
            self.assertEqual(clients, [])
            page.set_sync_busy(False)
            page.select_task("task-b")
            page.refresh()
            self.assertTrue(page.busy)
            self.wait_until(lambda: bool(clients) and clients[0].entered.is_set())
            page.refresh()
            page.set_sync_busy(False)
            self.assertEqual(len(clients), 1)
            self.assertEqual(signals, ["started"])
            self.assertEqual(self.config.snapshot_count, 1)
            self.assertIsNot(clients[0].config, self.config)
            self.assertNotIn(threading.get_ident(), clients[0].threads)
            clients[0].released.set()
            self.wait_until(lambda: not page.busy)
            self.assertEqual(signals, ["started", "finished"])
            self.assertEqual(page.list.count(), 2)
            self.assertEqual(events[0]["task"], "task-b")
            self.assertEqual(events[0]["kind"], "success")
            self.assertIn(("lsf_dirs", "backup/task-b/versions"), clients[0].calls)
            self.config.tasks = tuple(replace(task, versions_path="backup/task-b/new-versions") if task.id == "task-b" else task for task in self.config.tasks)
            page.load_tasks()
            self.assertEqual(page.list.count(), 0)
            self.assertEqual(page.file_list.count(), 0)
            self.assertFalse(page.restore_all_button.isEnabled())
            self.assertEqual(len(clients), 1)
            self.assertTrue(page.shutdown())

    def test_shutdown_cancels_live_worker_and_errors_are_not_empty_success(self):
        from app.ui.pages.versions import VersionsPage
        clients = []
        def factory(config, conf_path=None):
            client = FakeWorkerRclone(config, conf_path)
            clients.append(client)
            return client
        with patch("app.ui.pages.versions.Rclone", side_effect=factory):
            page = VersionsPage(self.config)
            self.addCleanup(page.deleteLater)
            events = []
            page.event_recorded.connect(events.append)
            page.refresh()
            self.wait_until(lambda: bool(clients) and clients[0].entered.is_set())
            page.shutdown()
            self.wait_until(lambda: not page.busy)
            self.assertTrue(page.shutdown())
            self.assertTrue(clients[0].cancelled.is_set())
            self.assertEqual(events[-1]["kind"], "error")
            self.assertEqual(page.list.count(), 0)

    def test_delete_requires_explicit_no_default_confirmation_for_selected_task(self):
        from app.ui.pages.versions import VersionsPage
        from PySide6.QtWidgets import QMessageBox
        clients = []
        def factory(config, conf_path=None):
            client = FakeWorkerRclone(config, conf_path)
            client.released.set()
            clients.append(client)
            return client
        with patch("app.ui.pages.versions.Rclone", side_effect=factory):
            page = VersionsPage(self.config)
            self.addCleanup(page.deleteLater)
            page.select_task("task-b")
            page.refresh()
            self.wait_until(lambda: not page.busy)
            page.list.setCurrentRow(0)
            self.wait_until(lambda: not page.busy)
            count = len(clients)
            with patch("app.ui.pages.versions.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as question:
                page.delete_button.click()
                self.assertEqual(len(clients), count)
                self.assertIn("照片", question.call_args.args[2])
                self.assertIn("永久删除", question.call_args.args[2])
                self.assertEqual(question.call_args.args[4], QMessageBox.StandardButton.No)
            with patch("app.ui.pages.versions.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                page.delete_button.click()
                self.wait_until(lambda: not page.busy)
            purges = [call for client in clients for call in client.calls if call[0] == "purge"]
            self.assertEqual(purges, [("purge", f"backup/task-b/versions/{VERSION}")])
            self.assertTrue(page.shutdown())

    def test_deadline_cancels_without_releasing_gate_early(self):
        from app.ui.pages.versions import VersionsPage
        clients = []
        def factory(config, conf_path=None):
            client = FakeWorkerRclone(config, conf_path)
            clients.append(client)
            return client
        with patch("app.ui.pages.versions.Rclone", side_effect=factory):
            page = VersionsPage(self.config)
            self.addCleanup(page.deleteLater)
            events = []
            page.event_recorded.connect(events.append)
            page.refresh()
            self.wait_until(lambda: bool(clients) and clients[0].entered.is_set())
            page._timeout.start(1)  # Clock boundary: do not wait real network deadlines.
            self.wait_until(lambda: not page.busy)
            self.assertTrue(clients[0].cancelled.is_set())
            self.assertEqual(events[-1]["kind"], "error")
            self.assertIn("超时", events[-1]["detail"])
            self.assertIn("重试", page.refresh_button.text())
            self.assertNotIn("没有历史", page.status.text())
            self.assertTrue(page.shutdown())


if __name__ == "__main__":
    unittest.main()
