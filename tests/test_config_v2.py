from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import constants as C
from app.core.config import Config, data_dir
from app.core.tasks import MirrorTask, new_task


class ConfigV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="PhoneNAS-config-tests-")
        self.addCleanup(scratch.cleanup)
        environment = patch.dict(os.environ, {
            "PHONENAS_DATA_DIR": str(Path(scratch.name) / "data"),
            "APPDATA": str(Path(scratch.name) / "appdata"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_first_run_has_no_example_connection_or_automatic_mount_or_start(self) -> None:
        fresh = Config()
        path = fresh.path
        self.assertFalse(path.exists())
        self.assertEqual(fresh.url, "")
        self.assertEqual(fresh.username, "")
        self.assertEqual(fresh.tasks, ())
        for key in ("mount.auto_mount", "app.start_with_windows"):
            self.assertIs(fresh.get(key), False)
        fresh.save()
        loaded = Config(path)
        self.assertEqual(loaded.load_error, "")
        self.assertEqual(loaded.data, fresh.data)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["nas"]["url"], "")
        self.assertEqual(payload["nas"]["username"], "")
        self.assertIs(payload["mount"]["auto_mount"], False)
        self.assertIs(payload["app"]["start_with_windows"], False)

    def test_sparse_saved_profiles_keep_legacy_defaults_and_v1_local_source(self) -> None:
        root = Config().path.parent
        legacy = Path(C.DEFAULT_LOCAL_PATH) if os.name == "nt" else root / "legacy-source"
        actual_resolve = Path.resolve

        def resolve(path: Path, *args, **kwargs):
            if path == Path("X:/") or path == legacy or (
                path.parent == legacy.parent and path.name.startswith(f"{legacy.name}.PhoneNAS-history-")
            ):
                return path
            return actual_resolve(path, *args, **kwargs)

        for version in (None, 1, 2, 3):
            with self.subTest(version=version), patch.object(Path, "resolve", resolve), \
                    patch.object(C, "DEFAULT_LOCAL_PATH", str(legacy)):
                path = root / f"sparse-{version}.json"
                original = b"{}" if version is None else f'{{"version":{version}}}'.encode("utf-8")
                path.write_bytes(original)
                config = Config(path)
                self.assertEqual(config.load_error, "")
                self.assertEqual(config.url, C.DEFAULT_URL)
                self.assertEqual(config.username, "admin")
                self.assertIs(config.get("mount.auto_mount"), True)
                self.assertIs(config.get("app.start_with_windows"), True)
                if version in (None, 1):
                    self.assertEqual(len(config.tasks), 1)
                    self.assertEqual(config.tasks[0].local_path, C.DEFAULT_LOCAL_PATH)
                    self.assertEqual(config.tasks[0].remote_path, "backup/work")
                    self.assertEqual(config.tasks[0].versions_path, "backup/work-versions")
                else:
                    self.assertEqual(config.tasks, ())
                self.assertEqual(path.read_bytes(), original)

    def test_explicit_saved_connection_and_startup_settings_survive_load_and_save(self) -> None:
        root = Config().path.parent
        expected = {
            "nas.url": "http://saved.invalid:8888",
            "nas.username": "device-user",
            "nas.password_enc": "opaque-dpapi",
            "mount.enabled": False,
            "mount.auto_mount": False,
            "app.start_with_windows": False,
        }
        for version in (1, 2, 3):
            with self.subTest(version=version):
                path = root / f"explicit-{version}.json"
                payload = {"version": version, "mirror": {"local_path": str(root / "source")} if version == 1 else {}}
                for dotted, value in expected.items():
                    section, key = dotted.split(".")
                    payload.setdefault(section, {})[key] = value
                path.write_text(json.dumps(payload), encoding="utf-8")
                config = Config(path)
                self.assertEqual(config.load_error, "")
                self.assertEqual({key: config.get(key) for key in expected}, expected)
                config.save()
                reloaded = Config(path)
                self.assertEqual(reloaded.load_error, "")
                self.assertEqual({key: reloaded.get(key) for key in expected}, expected)

    def test_read_only_mount_defaults_are_safe_for_fresh_configs_and_compatible_for_migrations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fresh = Config(root / "fresh.json")
            self.assertIs(fresh.get("mount.read_only"), True)
            source = str(root / "legacy-source")
            for version in (1, 2, 3):
                for explicit in (None, False, True):
                    mirror = {"local_path": source} if version == 1 else {"tasks": []}
                    payload = {"version": version, "mirror": mirror, "mount": {}}
                    if explicit is not None:
                        payload["mount"]["read_only"] = explicit
                    path = root / f"v{version}-{explicit}.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    config = Config(path)
                    self.assertEqual(config.load_error, "")
                    expected = explicit if explicit is not None else version == 3
                    with self.subTest(version=version, explicit=explicit):
                        self.assertIs(config.get("mount.read_only"), expected)
                    config.save()
                    self.assertIs(Config(path).get("mount.read_only"), expected)
            path = root / "wrong-type.json"
            path.write_text('{"version":3,"mount":{"read_only":"yes"}}', encoding="utf-8")
            self.assertTrue(Config(path).load_error)

    def test_v3_auto_sync_defaults_for_old_tasks_and_roundtrips_manual_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = new_task(str(root / "offline-source"))
            payload = task.to_dict()
            payload.pop("auto_sync", None)
            path = root / "config.json"
            path.write_text(json.dumps({"version": 3, "mirror": {"tasks": [payload]}}), encoding="utf-8")
            original = path.read_bytes()
            config = Config(path)
            self.assertEqual(config.load_error, "")
            self.assertIs(config.tasks[0].auto_sync, True)
            self.assertFalse(config.migrated)
            self.assertEqual(path.read_bytes(), original)
            manual = replace(config.tasks[0], auto_sync=False)
            config.upsert_task(manual)
            config.save()
            loaded = Config(path)
            self.assertEqual(loaded.get("version"), 3)
            self.assertEqual(loaded.tasks, (manual,))
            self.assertIs(loaded.tasks[0].enabled, True)
            self.assertIs(loaded.tasks[0].auto_sync, False)
            self.assertIs(json.loads(path.read_text(encoding="utf-8"))["mirror"]["tasks"][0]["auto_sync"], False)

    def test_task_roundtrip_and_snapshot_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            config = Config(root / "config.json")
            self.assertEqual(config.tasks, ())
            task = replace(new_task(str(source), "工作资料"), interval_minutes=7,
                           retention_days=45, excludes=("*.tmp", "cache/**"))
            config.replace_tasks([task])
            config.set("nas.url", "http://offline.invalid:8888/")
            config.save()

            loaded = Config(config.path)
            self.assertEqual(loaded.get("version"), 3)
            self.assertEqual(loaded.tasks, (task,))
            self.assertEqual(loaded.get_task(task.id), task)
            self.assertIsNone(loaded.get_task("missing"))
            self.assertEqual(loaded.url, "http://offline.invalid:8888")
            self.assertEqual(loaded.load_error, "")
            self.assertFalse(loaded.migrated)
            self.assertEqual(MirrorTask.from_dict(task.to_dict()), task)

            snapshot = loaded.snapshot()
            loaded.upsert_task(replace(task, name="修改后"))
            loaded.set("nas.username", "changed")
            self.assertEqual(snapshot.get_task(task.id).name, "工作资料")
            self.assertEqual(snapshot.username, "")
            loaded.remove_task(task.id)
            self.assertEqual(loaded.tasks, ())
            self.assertEqual(snapshot.tasks, (task,))

    def test_v1_migration_is_stable_read_only_and_backs_up_before_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy-source"
            source.mkdir()
            path = root / "config.json"
            legacy = {
                "version": 1,
                "nas": {"url": "http://offline.invalid:8888", "username": "old-user", "password_enc": "opaque-dpapi"},
                "mount": {"drive": "Y:", "network_mode": True},
                "mirror": {"enabled": False, "future_option": {"keep": True},
                           "local_path": str(source), "remote_path": "backup/work",
                           "versions_path": "backup/work-versions", "interval_minutes": 13,
                           "retention_days": 67, "exclude_preset": "custom", "excludes": ["cache/**", "*.tmp"]},
                "app": {"last_sync_at": "2026-11-12 10:11:12", "minimize_to_tray": False},
            }
            original = json.dumps(legacy, ensure_ascii=False, indent=2).encode("utf-8")
            path.write_bytes(original)
            config = Config(path)
            again = Config(path)
            self.assertTrue(config.migrated)
            self.assertEqual(config.load_error, "")
            self.assertEqual(config.get("version"), 3)
            self.assertEqual(config.tasks, again.tasks)
            self.assertEqual(len(config.tasks), 1)
            task = config.tasks[0]
            for key in ("enabled", "local_path", "remote_path", "versions_path", "interval_minutes",
                        "retention_days", "exclude_preset"):
                self.assertEqual(getattr(task, key), legacy["mirror"][key])
            self.assertIs(task.auto_sync, True)
            self.assertEqual(task.excludes, ("cache/**", "*.tmp"))
            self.assertEqual(task.last_sync_at, "2026-11-12 10:11:12")
            self.assertEqual(config.get("nas.password_enc"), "opaque-dpapi")
            self.assertEqual(config.get("mount.drive"), "Y:")
            self.assertFalse(config.get("app.minimize_to_tray"))
            self.assertEqual(config.get("mirror.future_option"), {"keep": True})
            self.assertIsNone(config.get("mirror.local_path"))
            backup = path.with_suffix(".v1-backup")
            self.assertFalse(backup.exists())
            self.assertEqual(path.read_bytes(), original)

            config.save()
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 3)
            backup.write_bytes(b"existing-backup-must-not-change")
            config.save()
            self.assertEqual(backup.read_bytes(), b"existing-backup-must-not-change")
            self.assertEqual(Config(path).tasks, config.tasks)

    def test_v2_migration_preserves_tasks_is_read_only_and_backs_up_exact_original_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = replace(new_task(str(root / "offline-source")), enabled=False, interval_minutes=1440,
                           retention_days=3650, last_sync_at="2026-11-12T10:11:12", last_file_count=42)
            legacy_task = task.to_dict()
            for field in ("direction", "dataset_id", "local_history_path", "sync_identity", "auto_sync"):
                legacy_task.pop(field)
            legacy = {"version": 2, "mount": {"drive": "Y:"},
                      "mirror": {"enabled": False, "tasks": [legacy_task], "future_option": "keep"}}
            original = bytes.fromhex("efbbbf") + json.dumps(legacy, ensure_ascii=False, indent=3).encode("utf-8") + b"\r\n"
            path = root / "config.json"
            path.write_bytes(original)
            config = Config(path)
            again = Config(path)
            backup = root / "config.v2-backup"
            self.assertEqual(config.load_error, "")
            self.assertTrue(config.migrated)
            self.assertEqual(config.get("version"), 3)
            self.assertEqual(config.tasks, again.tasks)
            migrated = config.tasks[0]
            for field, value in legacy_task.items():
                self.assertEqual(migrated.to_dict()[field], value)
            self.assertIs(migrated.auto_sync, True)
            self.assertEqual(migrated.direction, "upload")
            self.assertEqual(migrated.dataset_id, task.dataset_id)
            self.assertEqual(migrated.local_history_path, task.local_history_path)
            self.assertEqual(config.get("mirror.future_option"), "keep")
            self.assertFalse(backup.exists())
            self.assertEqual(path.read_bytes(), original)
            config.save()
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(Config(path).tasks, config.tasks)
            self.assertFalse(Config(path).migrated)
            backup.write_bytes(b"do-not-overwrite")
            config.save()
            self.assertEqual(backup.read_bytes(), b"do-not-overwrite")

    def test_blocked_migration_backup_prevents_overwrite_and_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            original = b'{"version":2,"mirror":{"tasks":[]}}'
            path.write_bytes(original)
            config = Config(path)
            blocked = root / "config.v2-backup"
            blocked.mkdir()
            with self.assertRaises(OSError):
                config.save()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(config.get("version"), 3)
            self.assertFalse(path.with_suffix(".tmp").exists())
            blocked.rmdir()
            config.save()
            self.assertEqual(blocked.read_bytes(), original)
            self.assertEqual(Config(path).get("version"), 3)

    def test_export_removes_password_and_validated_import_keeps_existing_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            config = Config(root / "config.json")
            task = new_task(str(source))
            config.replace_tasks([task])
            config.password = "offline-secret"
            encrypted = config.get("nas.password_enc")
            config.save()
            exported = root / "export.json"
            config.export_to(exported)
            text = exported.read_text(encoding="utf-8")
            payload = json.loads(text)
            self.assertEqual(payload["nas"]["password_enc"], "")
            self.assertNotIn("offline-secret", text)
            self.assertNotIn(encrypted, text)
            self.assertEqual(payload["mirror"]["tasks"][0]["id"], task.id)
            payload["nas"]["username"] = "imported"
            exported.write_text(json.dumps(payload), encoding="utf-8")
            config.import_from(exported)
            self.assertEqual(config.password, "offline-secret")
            self.assertEqual(config.username, "imported")

            before = config.snapshot()
            original = config.path.read_bytes()
            backups = list(root.glob("config.replaced-*.backup"))
            payload["mirror"]["tasks"].append(replace(task, id="other-id").to_dict())
            exported.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                config.import_from(exported)
            self.assertEqual(config.tasks, before.tasks)
            self.assertEqual(config.data, before.data)
            self.assertEqual(config.path.read_bytes(), original)
            self.assertCountEqual(root.glob("config.replaced-*.backup"), backups)

    def test_schedule_approval_stays_local_to_the_saved_config_not_an_import_or_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            approval = "1.2.4-filenames"
            config = Config(path)
            config.set("app.schedule_approval", approval)
            config.save()
            config = Config(path)
            self.assertEqual(config.get("app.schedule_approval"), approval)

            exported = root / "exported.json"
            config.export_to(exported)
            payload = json.loads(exported.read_text(encoding="utf-8"))
            self.assertNotIn("schedule_approval", payload["app"])
            self.assertEqual(config.get("app.schedule_approval"), approval)

            payload["app"]["schedule_approval"] = approval
            exported.write_text(json.dumps(payload), encoding="utf-8")
            config.import_from(exported)
            self.assertIsNone(config.get("app.schedule_approval"))
            self.assertIsNone(Config(path).get("app.schedule_approval"))

    def test_import_backs_up_exact_saved_bytes_before_replacing_a_healthy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            original = (b'\xef\xbb\xbf{\r\n  "version" : 3,\r\n  "nas" : {"username": "saved"},\r\n'
                        b'  "future_option" : [1, 2]\r\n}\r\n')
            path.write_bytes(original)
            config = Config(path)
            self.assertEqual(config.load_error, "")
            config.set("nas.username", "unsaved")
            incoming = root / "incoming.json"
            incoming.write_text('{"version":3,"nas":{"username":"imported"}}', encoding="utf-8")

            config.import_from(incoming)

            backups = list(root.glob("config.replaced-*.backup"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(config.username, "imported")
            self.assertEqual(Config(path).username, "imported")

    def test_failed_import_backup_leaves_the_config_untouched_and_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            original = b'{"version":3,"nas":{"username":"original"}}\n'
            path.write_bytes(original)
            config = Config(path)
            before = config.snapshot()
            incoming = root / "incoming.json"
            incoming.write_text('{"version":3,"nas":{"username":"imported"}}', encoding="utf-8")
            real_open = Path.open

            def fail_backup_write(file: Path, mode: str = "r", *args, **kwargs):
                stream = real_open(file, mode, *args, **kwargs)
                if file.name.startswith("config.replaced-") and mode == "xb":
                    write = stream.write

                    def partial_write(data: bytes) -> int:
                        write(data[:1])
                        raise OSError("simulated backup write failure")

                    stream.write = partial_write
                return stream

            with patch.object(Path, "open", fail_backup_write), self.assertRaisesRegex(OSError, "backup write failure"):
                config.import_from(incoming)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(config.data, before.data)
            self.assertEqual(config.load_error, before.load_error)
            self.assertFalse(path.with_suffix(".tmp").exists())
            self.assertEqual(list(root.glob("*.backup")), [])
            config.import_from(incoming)
            backups = list(root.glob("config.replaced-*.backup"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(config.username, "imported")

    def test_import_resets_success_and_baseline_authority_without_changing_dataset_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = Config(root / "publisher.json")
            task = replace(new_task(str(root / "working"), direction="download"),
                           last_sync_at="2026-11-14T10:30:00", last_file_count=37)
            source.replace_tasks([task])
            source.set("app.last_sync_at", "2026-11-14T10:30:00")
            source.save()
            incoming = root / "incoming.json"
            source.export_to(incoming)
            destination = Config(root / "subscriber.json")
            destination.import_from(incoming)
            self.assertEqual(list(root.glob("subscriber.replaced-*.backup")), [])
            imported = destination.tasks[0]
            self.assertEqual(imported.id, task.id)
            self.assertEqual(imported.dataset_id, task.dataset_id)
            self.assertEqual(imported.remote_path, task.remote_path)
            self.assertEqual(imported.versions_path, task.versions_path)
            self.assertEqual(imported.direction, "download")
            self.assertEqual(imported.last_sync_at, "")
            self.assertEqual(imported.last_file_count, 0)
            self.assertEqual(destination.get("app.last_sync_at"), "")
            self.assertNotEqual(imported.sync_identity, task.sync_identity)
            self.assertRegex(imported.sync_identity, r"^[0-9a-f]{32}$")
            self.assertEqual(Config(destination.path).tasks, destination.tasks)
            destination.mark_task_success(imported.id, "2026-11-14T10:31:00", 40)
            destination.import_from(incoming)
            self.assertNotEqual(destination.tasks[0].sync_identity, imported.sync_identity)
            self.assertEqual(destination.tasks[0].last_sync_at, "")
            self.assertEqual(destination.tasks[0].last_file_count, 0)

    def test_corrupt_or_future_config_blocks_tasks_and_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for content in ("{broken", "[]", '{"version": 99}',
                            '{"version": 2, "mirror": {"tasks": "not-a-list"}}',
                            '{"version": 2, "mirror": {"enabled": "yes", "tasks": []}}'):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    config = Config(path)
                    self.assertTrue(config.load_error)
                    self.assertEqual(config.tasks, ())
                    self.assertFalse(config.get("mirror.enabled"))
                    snapshot = config.snapshot()
                    self.assertEqual(snapshot.load_error, config.load_error)
                    self.assertEqual(snapshot.tasks, ())
                    with self.assertRaises(ValueError):
                        config.save()
                    self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_success_updates_only_target_and_invalid_replacement_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [root / "one", root / "two"]
            for source in sources:
                source.mkdir()
            first, second = [new_task(str(source)) for source in sources]
            config = Config(root / "config.json")
            config.replace_tasks((first, second))
            config.mark_task_success(first.id, "2026-11-14 10:30:00", file_count=12)
            self.assertEqual(config.get_task(first.id).last_sync_at, "2026-11-14 10:30:00")
            self.assertEqual(config.get_task(first.id).last_file_count, 12)
            self.assertEqual(config.get_task(second.id), second)
            before = config.tasks
            with self.assertRaises(ValueError):
                config.replace_tasks([first, replace(second, remote_path=first.versions_path)])
            self.assertEqual(config.tasks, before)
            with self.assertRaises(ValueError):
                config.mark_task_success(first.id, "2026-11-14 10:40:00", file_count=-1)
            self.assertEqual(config.tasks, before)
            config.mark_task_success("removed-task", "2026-11-14 10:40:00")
            self.assertEqual(config.tasks, before)

    def test_legacy_first_task_properties_and_empty_tasks_do_not_fall_back_to_old_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = new_task(str(root / "offline-disk"))
            config = Config(root / "config.json")
            config.replace_tasks([task])
            self.assertEqual(config.local_path, Path(task.local_path))
            self.assertEqual(config.remote_path, task.remote_path)
            self.assertEqual(config.versions_path, task.versions_path)
            self.assertEqual(config.interval_minutes, task.interval_minutes)
            self.assertEqual(config.retention_days, task.retention_days)
            config.remove_task(task.id)
            with self.assertRaises(ValueError):
                _ = config.local_path
            with self.assertRaises(ValueError):
                _ = config.remote_path
            config.save()
            self.assertEqual(Config(config.path).tasks, ())

    def test_explicit_data_directory_override_wins_over_portable_and_appdata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            override = root / "scratch" / "data"
            with patch.dict(os.environ, {"PHONENAS_DATA_DIR": str(override), "APPDATA": str(root / "forbidden")}), \
                    patch("app.core.config.is_portable", return_value=True), \
                    patch("app.core.config.app_root", side_effect=AssertionError("override must win")):
                self.assertEqual(data_dir(), override.resolve())
                config = Config()
                self.assertEqual(config.path, override.resolve() / "config.json")
                config.save()
            self.assertTrue((override / "config.json").exists())
            self.assertFalse((root / "forbidden").exists())

    def test_import_replacing_corrupt_config_backs_up_original_only_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            corrupt = b'{"version": 9000, "future-data": "keep"}'
            path.write_bytes(corrupt)
            config = Config(path)
            incoming = root / "incoming.json"
            incoming.write_text('{"version": 2, "mirror": {"tasks": false}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                config.import_from(incoming)
            self.assertEqual(list(root.glob("*.backup")), [])
            self.assertEqual(path.read_bytes(), corrupt)
            incoming.write_text('{"version": 2, "mirror": {"tasks": []}}', encoding="utf-8")
            config.import_from(incoming)
            backups = list(root.glob("*.backup"))
            self.assertEqual(len(backups), 1)
            self.assertIn("replaced-", backups[0].name)
            self.assertEqual(backups[0].read_bytes(), corrupt)
            self.assertEqual(config.load_error, "")
            self.assertEqual(Config(path).tasks, ())

    def test_snapshot_and_apply_snapshot_do_not_load_or_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config(path)
            with patch.object(Config, "load", side_effect=AssertionError("unexpected load")), \
                    patch.object(Config, "save", side_effect=AssertionError("unexpected save")):
                draft = config.snapshot()
                draft.set("nas.username", "draft")
                config.apply_snapshot(draft)
            draft.set("nas.username", "not shared")
            self.assertEqual(config.username, "draft")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
