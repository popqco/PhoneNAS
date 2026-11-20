from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from app.core.tasks import MirrorTask, new_task, normalize_remote_path, validate_source, validate_tasks


class TaskIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="PhoneNAS-task-tests-")
        self.addCleanup(scratch.cleanup)
        environment = patch.dict(os.environ, {
            "PHONENAS_DATA_DIR": str(Path(scratch.name) / "data"),
            "APPDATA": str(Path(scratch.name) / "appdata"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_auto_sync_is_optional_strict_bool_and_preserves_positional_construction(self) -> None:
        task = MirrorTask("one", "One", True, "D:/offline/one", "backup/one/mirror",
                          "backup/one/versions", 5, 30, "none", ())
        self.assertIs(task.auto_sync, True)
        self.assertIs(new_task("D:/offline/new").auto_sync, True)
        legacy = task.to_dict()
        del legacy["auto_sync"]
        self.assertIs(MirrorTask.from_dict(legacy).auto_sync, True)
        manual = replace(task, auto_sync=False)
        self.assertIs(manual.enabled, True)
        self.assertEqual(MirrorTask.from_dict(manual.to_dict()), manual)
        for value in (None, 0, 1, "true", "false", [], {}):
            payload = task.to_dict()
            payload["auto_sync"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                MirrorTask.from_dict(payload)
            with self.subTest(direct=value), self.assertRaises(ValueError):
                validate_tasks([replace(task, auto_sync=value)])

    def test_direction_selects_action_and_keeps_phone_history_for_manual_return(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / "source")
            upload = new_task(source)
            self.assertEqual(upload.direction, "upload")
            self.assertEqual(upload.action_text, "立即上传")
            self.assertEqual(upload.direction_label, "上传到手机")

            download = new_task(source, direction="download")
            self.assertEqual(download.direction, "download")
            self.assertEqual(download.action_text, "立即拉取")
            self.assertEqual(download.direction_label, "从手机拉取")
            self.assertEqual(download.versions_path, "PhoneNasHistory/source-" + download.id[:8])
            self.assertEqual(MirrorTask.from_dict(download.to_dict()), download)
            legacy = upload.to_dict()
            del legacy["direction"]
            self.assertEqual(MirrorTask.from_dict(legacy).direction, "upload")
            for direction in ("", "Upload", " download ", "two-way", None, True):
                with self.subTest(direction=direction), self.assertRaises(ValueError):
                    new_task(source, direction=direction)

    def test_explicit_dataset_binding_is_stable_across_pcs_but_task_ids_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            publisher = new_task(str(Path(tmp) / "publisher"), remote_path="backup/team/mirror")
            subscriber = new_task(str(Path(tmp) / "subscriber"), direction="download",
                                  remote_path="backup/team/mirror", versions_path=publisher.versions_path)
            self.assertNotEqual(publisher.id, subscriber.id)
            self.assertEqual(publisher.dataset_id, subscriber.dataset_id)
            self.assertEqual(publisher.versions_path, "backup/team/versions")
            self.assertEqual(publisher.dataset_control_path, f".phonenas/datasets/{publisher.dataset_id}")
            self.assertRegex(publisher.dataset_id, r"^[0-9a-f]{32}$")
            legacy = publisher.to_dict()
            del legacy["dataset_id"]
            legacy["id"] = "a-different-pc-task"
            legacy["local_path"] = str(Path(tmp) / "legacy-subscriber")
            legacy["remote_path"] = " BACKUP\\team//mirror/ "
            self.assertEqual(MirrorTask.from_dict(legacy).dataset_id, publisher.dataset_id)
            explicit = new_task(str(Path(tmp) / "explicit"), direction="download",
                                remote_path="backup/team/mirror", versions_path="backup/manual-history",
                                dataset_id="A07F6F74-0B28-4E16-9A64-26DF8D847812")
            self.assertEqual(explicit.dataset_id, "a07f6f740b284e169a6426df8d847812")
            self.assertEqual(explicit.versions_path, "backup/manual-history")
            self.assertEqual(MirrorTask.from_dict(explicit.to_dict()), explicit)
            for identity in ("not-a-uuid", "../escape", ".phonenas", "A" * 31, " " + "a" * 32,
                             "{" + "a" * 32 + "}", 1, None):
                payload = publisher.to_dict()
                payload["dataset_id"] = identity
                with self.subTest(dataset_id=identity), self.assertRaises(ValueError):
                    MirrorTask.from_dict(payload)

    def test_local_history_is_grouped_for_new_tasks_and_legacy_paths_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "work"
            for direction in ("upload", "download"):
                task = new_task(str(local), direction=direction)
                expected = local.parent / "PhoneNAS-本地历史" / f"work-{task.id[:8]}"
                self.assertEqual(Path(task.local_history_path), expected)
                self.assertTrue(Path(task.local_history_path).is_absolute())
                self.assertFalse(expected.parent.exists())
                self.assertEqual(task.history_location,
                                 task.versions_path if direction == "upload" else str(expected))
                self.assertEqual(MirrorTask.from_dict(task.to_dict()), task)
                legacy = task.to_dict()
                del legacy["local_history_path"]
                old_default = str(local.with_name(f"work.PhoneNAS-history-{task.id[:8]}"))
                self.assertEqual(MirrorTask.from_dict(legacy).local_history_path, old_default)
                direct = MirrorTask(**{key: value for key, value in legacy.items() if key != "dataset_id"})
                self.assertEqual(direct.local_history_path, old_default)
                validate_tasks([task])
            draft = new_task("")
            self.assertEqual(draft.local_history_path, "")
            with self.assertRaises(ValueError):
                validate_tasks([draft])

    def test_sync_identity_is_persisted_per_binding_and_legacy_defaults_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = new_task(str(Path(tmp) / "first"))
            second = new_task(str(Path(tmp) / "second"), remote_path=first.remote_path,
                              versions_path=first.versions_path, dataset_id=first.dataset_id)
            self.assertRegex(first.sync_identity, r"^[0-9a-f]{32}$")
            self.assertNotEqual(first.sync_identity, second.sync_identity)
            self.assertEqual(MirrorTask.from_dict(first.to_dict()).sync_identity, first.sync_identity)
            legacy = first.to_dict()
            del legacy["sync_identity"]
            loaded = MirrorTask.from_dict(legacy)
            self.assertEqual(loaded.sync_identity, MirrorTask.from_dict(legacy).sync_identity)
            self.assertEqual(replace(loaded, name="renamed").sync_identity, loaded.sync_identity)
            for value in (None, 12, "invalid", "../authority"):
                payload = first.to_dict()
                payload["sync_identity"] = value
                with self.subTest(sync_identity=value), self.assertRaises(ValueError):
                    MirrorTask.from_dict(payload)

    def test_schedule_and_retention_bounds_reject_instead_of_silently_clamping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = new_task(str(Path(tmp) / "source"))
            for field, lower, upper in (("interval_minutes", 1, 1440), ("retention_days", 1, 3650)):
                for value in (lower, upper):
                    data = task.to_dict()
                    data[field] = value
                    parsed = MirrorTask.from_dict(data)
                    self.assertEqual(getattr(parsed, field), value)
                    validate_tasks([parsed])
                for value in (0, upper + 1, True, 1.5, "20"):
                    data = task.to_dict()
                    data[field] = value
                    with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                        MirrorTask.from_dict(data)
                    with self.subTest(direct_field=field, value=value), self.assertRaises(ValueError):
                        validate_tasks([replace(task, **{field: value})])

    def test_all_local_history_roots_are_isolated_from_payloads_histories_and_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = new_task(str(root / "first"))
            second = replace(new_task(str(root / "second"), direction="download"), enabled=False)
            validate_tasks([first, second])
            for history in (first.local_path, str(Path(first.local_path) / "nested"), str(root),
                            second.local_path, str(Path(second.local_path) / "nested"),
                            second.local_history_path, str(Path(second.local_history_path) / "nested"),
                            "relative/history", " ", "X:\\history", "\\x00"):
                with self.subTest(history=history), self.assertRaises(ValueError):
                    validate_tasks([replace(first, local_history_path=history), second])
            for local in (first.local_history_path, str(Path(first.local_history_path) / "nested")):
                with self.subTest(local=local), self.assertRaises(ValueError):
                    validate_tasks([first, replace(second, local_path=local)])
            with self.assertRaises(ValueError):
                validate_tasks([first, second], mount_drive=first.local_history_path)
            for history in (None, 42, Path(first.local_history_path)):
                payload = first.to_dict()
                payload["local_history_path"] = history
                with self.subTest(history_type=history), self.assertRaises(ValueError):
                    MirrorTask.from_dict(payload)
            self.assertFalse(Path(first.local_history_path).exists())
            self.assertFalse(Path(second.local_history_path).exists())

    def test_remote_control_namespace_and_same_dataset_duplicates_are_reserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = new_task(str(Path(tmp) / "first"))
            second = new_task(str(Path(tmp) / "second"), direction="download")
            validate_tasks([first, second])
            for field in ("remote_path", "versions_path"):
                for remote in (".phonenas", ".PhoneNAS/datasets/other", "backup/.phonenas/payload",
                               first.dataset_control_path):
                    with self.subTest(field=field, remote=remote), self.assertRaises(ValueError):
                        validate_tasks([replace(first, **{field: remote})])
            with self.assertRaises(ValueError):
                validate_tasks([first, replace(second, dataset_id=first.dataset_id)])
            with self.assertRaises(ValueError):
                validate_tasks([replace(first, enabled=False), replace(second, dataset_id=first.dataset_id)])
            with self.assertRaises(ValueError):
                validate_tasks([first, new_task(str(Path(tmp) / "third"), direction="download",
                                               remote_path=first.remote_path, versions_path=first.versions_path)])

    def test_generated_tasks_and_normalized_remote_roots_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "left"
            right = Path(tmp) / "right"
            left.mkdir()
            right.mkdir()
            first = new_task(str(left), "设计文件 / v2")
            second = new_task(str(right), "设计文件 / v2")
            validate_tasks([first, second])
            self.assertEqual(validate_source(first), left.resolve())
            self.assertNotEqual(first.id, second.id)
            self.assertNotEqual(first.remote_path, second.remote_path)
            self.assertEqual(first.remote_path, f"PhoneNasBackup/设计文件 - v2-{first.id[:8]}")
            self.assertEqual(first.versions_path, f"PhoneNasHistory/设计文件 - v2-{first.id[:8]}")
            self.assertEqual(Path(second.remote_path).name, f"设计文件 - v2-{second.id[:8]}")
            self.assertEqual(normalize_remote_path(" backup\\docs//mirror/ "), "backup/docs/mirror")
            self.assertEqual(replace(first, name="改名").remote_path, first.remote_path)
            with self.assertRaises(FrozenInstanceError):
                first.name = "cannot mutate"

            for bad in ("", " ", "/", "\\", ".", "..", "backup/../work", "phone:work", "backup/a\x00"):
                with self.subTest(remote=bad), self.assertRaises(ValueError):
                    normalize_remote_path(bad)

    def test_local_aliases_parent_child_mount_and_all_remote_roots_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            nested = source / "child"
            other = root / "other"
            sibling = root / "source-two"
            nested.mkdir(parents=True)
            other.mkdir()
            sibling.mkdir()
            first = new_task(str(source), "one")
            second = new_task(str(other), "two")
            validate_tasks([first, second, new_task(str(sibling))])
            for local in (str(source), str(nested), str(source).upper()):
                with self.subTest(local=local), self.assertRaises(ValueError):
                    validate_tasks([first, replace(second, local_path=local)])
            for mirror, versions in ((first.remote_path, second.versions_path),
                                     (first.remote_path + "/child", second.versions_path),
                                     (second.remote_path, "PhoneNasBackup"),
                                     (first.versions_path + "/old", second.versions_path),
                                     (second.remote_path, second.remote_path)):
                with self.subTest(mirror=mirror, versions=versions), self.assertRaises(ValueError):
                    validate_tasks([first, replace(second, remote_path=mirror, versions_path=versions)])
            for local in ("", "relative/path", "X:\\docs", "x:/docs"):
                with self.subTest(local=local), self.assertRaises(ValueError):
                    validate_tasks([replace(first, local_path=local)])
            with self.assertRaises(ValueError):
                validate_tasks([first, replace(second, id=first.id)])

    def test_task_dictionary_has_strict_types_and_direct_model_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = new_task(tmp)
            self.assertEqual(MirrorTask.from_dict(task.to_dict()), task)
            cases = {"id": "", "name": " ", "enabled": 1, "local_path": Path(tmp),
                     "remote_path": None, "interval_minutes": True, "retention_days": 0,
                     "excludes": "*.tmp", "last_sync_at": 0, "last_file_count": -1,
                     "unknown_field": "unexpected"}
            for key, value in cases.items():
                data = task.to_dict()
                data[key] = value
                with self.subTest(field=key), self.assertRaises(ValueError):
                    MirrorTask.from_dict(data)
            data = task.to_dict()
            del data["id"]
            with self.assertRaises(ValueError):
                MirrorTask.from_dict(data)
            with self.assertRaises(ValueError):
                validate_tasks([replace(task, enabled="true")])
            for excludes in (["mutable-list"], None, "*.tmp"):
                with self.subTest(excludes=excludes), self.assertRaises(ValueError):
                    validate_tasks([replace(task, excludes=excludes)])
            data = task.to_dict()
            data["remote_path"] = " backup\\docs//mirror/ "
            self.assertEqual(MirrorTask.from_dict(data).remote_path, "backup/docs/mirror")
            with self.assertRaises(ValueError):
                validate_tasks([replace(task, remote_path="backup//docs/mirror/")])

    @unittest.skipUnless(os.name == "nt", "Windows drive roots")
    def test_unavailable_mount_drive_does_not_block_an_unrelated_source_or_allow_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            alias = root / "mount-alias"
            unavailable = root / "unavailable-source"
            mount = Path("X:/")
            actual_resolve = Path.resolve

            def resolve(path: Path, *args, **kwargs):
                if path in (mount, unavailable):
                    raise OSError(0, "offline fixture", str(path), 1005)
                if path == alias:
                    return mount / "source"
                return actual_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", resolve):
                valid = new_task(str(source))
                for drive in ("X:", "x:/", "X:\\"):
                    with self.subTest(drive=drive):
                        validate_tasks([valid], mount_drive=drive)
                        self.assertEqual(validate_source(valid, mount_drive=drive), actual_resolve(source))
                        for local in (str(mount / "direct"), str(alias)):
                            with self.subTest(local=local), self.assertRaisesRegex(ValueError, "挂载盘"):
                                validate_source(new_task(local), mount_drive=drive)
                with self.assertRaisesRegex(ValueError, "挂载盘"):
                    validate_tasks([replace(valid, local_history_path=str(alias))])
                with self.assertRaisesRegex(ValueError, "无法解析本地来源"):
                    validate_source(new_task(str(unavailable)))

    @unittest.skipUnless(os.name == "nt", "Windows drive roots")
    def test_reachable_drive_alias_cannot_be_used_by_its_canonical_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            task = new_task(str(source))
            canonical = source.resolve()
            actual_resolve = Path.resolve

            def resolve(path: Path, *args, **kwargs):
                if path == Path("X:/"):
                    return canonical  # Simulate a drive alias to the test directory.
                return actual_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", resolve), self.assertRaisesRegex(ValueError, "挂载目录"):
                validate_source(task)

    def test_configured_directory_mount_resolves_separately_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            mount = root / "mount"
            source.mkdir()
            mount.mkdir()
            task = new_task(str(source))
            canonical = source.resolve()
            self.assertEqual(validate_source(task, mount_drive=str(mount)), canonical)
            with self.assertRaisesRegex(ValueError, "挂载目录"):
                validate_tasks([new_task(str(mount / "child"))], mount_drive=str(mount))
            actual_resolve = Path.resolve

            def resolve(path: Path, *args, **kwargs):
                if path == mount:
                    raise OSError(0, "offline fixture", str(path), 1005)
                return actual_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", resolve), self.assertRaisesRegex(ValueError, "无法解析.*挂载目录"):
                validate_source(task, mount_drive=str(mount))

    def test_real_junction_aliases_and_mounted_directory_aliases_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            alias = root / "alias"
            mount = root / "mount"
            if os.name == "nt":
                for path in (alias, mount):
                    result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(path), str(source)],
                                            capture_output=True, check=False)
                    if result.returncode:
                        self.fail(f"Cannot create offline test junction: {result.stderr or result.stdout}")
            else:
                alias.symlink_to(source, target_is_directory=True)
                mount.symlink_to(source, target_is_directory=True)
            try:
                first = new_task(str(source))
                linked = new_task(str(alias))
                self.assertEqual(validate_source(linked), source.resolve())
                with self.assertRaises(ValueError):
                    validate_tasks([first, linked])
                with self.assertRaises(ValueError):
                    validate_source(first, mount_drive=str(mount))
                with self.assertRaises(ValueError):
                    validate_tasks([first], mount_drive=str(mount))
                with self.assertRaises(ValueError):
                    validate_tasks([replace(first, local_history_path=str(alias))])
                isolated = new_task(str(root / "offline"))
                history_alias = replace(first, local_path=str(root / "other"), local_history_path=str(alias))
                with self.assertRaises(ValueError):
                    validate_tasks([history_alias, replace(isolated, local_path=str(source))])
            finally:
                if os.name == "nt":
                    alias.rmdir()
                    mount.rmdir()
                else:
                    alias.unlink()
                    mount.unlink()

    @unittest.skipUnless(os.name == "nt", "Windows namespace aliases")
    def test_windows_extended_namespaces_cannot_bypass_isolation_or_mount_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            prefix = chr(92) * 2 + "?" + chr(92)
            first = new_task(str(source))
            alias = new_task(prefix + str(source))
            with self.assertRaises(ValueError):
                validate_tasks([first, alias])
            for local in (prefix + "X:" + chr(92) + "docs",
                          chr(92) * 2 + "." + chr(92) + "X:" + chr(92) + "docs"):
                with self.subTest(local=local), self.assertRaises(ValueError):
                    validate_tasks([replace(first, local_path=local)])

    def test_missing_or_non_directory_sources_cannot_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file = root / "not-a-folder"
            file.write_text("offline", encoding="utf-8")
            for local in (str(root / "missing"), str(file), "", "relative/path", "X:\\docs"):
                with self.subTest(local=local), self.assertRaises(ValueError):
                    validate_source(new_task(local))


if __name__ == "__main__":
    unittest.main()
