from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QApplication

from app.core.progress import SyncProgress
from app.core.rclone import Rclone, Result, rclone_exe
from app.core.relay import DatasetCoordinator, LocalDatasetLease
from app.core.sync import SyncEngine, SyncWorker
from app.core.tasks import new_task
import test_sync_integration as integration


class SyncProgressTests(unittest.TestCase):
    def test_progress_is_immutable_and_only_transfer_bytes_have_a_phase_percentage(self):
        event = SyncProgress("staging", "下载到暂存区", bytes_done=6, bytes_total=24)
        self.assertEqual(event.percent, 25)
        self.assertIn("本阶段 25%", event.display())
        self.assertIn("下载到暂存区", event.display())
        self.assertIn("01:05", event.display(elapsed_seconds=65))
        with self.assertRaises(FrozenInstanceError):
            event.bytes_done = 24
        self.assertEqual(SyncProgress("applying", "写入目标", 30, 24).percent, 100)
        for stage in ("inspect_local", "inspect_remote", "check_staged", "recheck_destination",
                      "recheck_source", "archive", "directories", "verify_destination",
                      "verify_source", "saving", "cleanup", "releasing"):
            with self.subTest(stage=stage):
                # Even accidental counters must not turn non-transfer work into
                # a fabricated overall percentage.
                event = SyncProgress(stage, "正在核对", 24, 24)
                self.assertIsNone(event.percent)
                self.assertNotIn("%", event.display())

    def test_unknown_or_invalid_byte_totals_remain_indeterminate(self):
        for done, total in ((None, None), (0, 0), (1, 0), (-1, 24), (1, -1),
                            (True, 24), (1, True), (1.5, 24), (1, "24")):
            with self.subTest(done=done, total=total):
                event = SyncProgress("staging", "正在准备", done, total)
                self.assertIsNone(event.percent)
                self.assertNotIn("%", event.display())
        self.assertEqual(SyncProgress("applying", "写入目标", 0, 24).percent, 0)

    def test_directory_counts_are_items_not_bytes_or_percentage(self):
        event = SyncProgress("directories", "补齐目录", items_done=3, items_total=8)
        self.assertEqual((event.bytes_done, event.bytes_total), (None, None))
        self.assertIsNone(event.percent)
        self.assertIn("3 / 8", event.display())
        self.assertNotIn("%", event.display())
        for elapsed in (None, -1, float("nan"), float("inf")):
            self.assertEqual(event.display(elapsed_seconds=elapsed), event.display())


class CompletedCopy:
    """Controlled subprocess JSON after a real alias copy, never a fake plan."""

    def __init__(self, result, size, lines=None):
        if lines is None:
            lines = [json.dumps({"stats": {"bytes": size, "totalBytes": size}})]
        self.stdout = io.StringIO("\n".join(lines) + "\n")
        self.code = result.code

    def wait(self, timeout=None):
        return self.code

    def poll(self):
        return self.code

    def kill(self):
        pass


class ProgressAlias(Rclone):
    """Control only external I/O; snapshots, hashes, writes and leases stay real."""

    def __init__(self, config, conf_path, remote_root):
        super().__init__(config, conf_path=conf_path)
        self.alias_text = conf_path.read_text(encoding="utf-8")
        self.remote_root = remote_root
        self.calls = []
        self.copies = []
        self.hash_entered = threading.Event()
        self.hash_release = threading.Event()
        self.gate_after_copies = None
        self.hash_failure = False
        self.payload_size = 0
        self.stats_lines = None
        self.failed_copy_destination = None

    def ensure_conf(self):
        # SyncWorker still uses the real per-operation isolated_conf context.
        self.conf_path.write_text(self.alias_text, encoding="utf-8")

    def run(self, args, *positional, **kwargs):
        self.calls.append(tuple(args))
        if (self.gate_after_copies is not None
                and len(self.copies) >= self.gate_after_copies
                and not self.hash_entered.is_set()
                and args[:3] == ["hashsum", "SHA256", self.remote_root]):
            self.hash_entered.set()
            if not self.hash_release.wait(30):
                raise AssertionError("test did not release the remote hash boundary")
            if self.hash_failure:
                return Result(False, "", "isolated remote hash failure", 1)
        result = super().run(args, *positional, **kwargs)
        if result.ok and args[:2] == ["lsjson", self.remote_root]:
            # Local aliases may supply server hashes. Remove only that capability
            # so real snapshot validation must download and hash actual bytes.
            entries = json.loads(result.out)
            for entry in entries:
                entry.pop("Hashes", None)
            result.out = json.dumps(entries)
        return result

    def popen(self, args, *positional, **kwargs):
        if args[:1] != ["copy"]:
            raise AssertionError(f"unexpected streaming command: {args}")
        result = self.run(args, *positional, **kwargs)
        if not result.ok:
            raise RuntimeError(result.err or result.out)
        self.copies.append((args[1], args[2]))
        if args[2] == self.failed_copy_destination:
            # A process can finish copying its bytes and still exit nonzero.
            # Preserve the real copy but control this external exit status.
            result = Result(False, result.out, "isolated copy exit failure", 9)
        return CompletedCopy(result, self.payload_size, self.stats_lines)

    def cancel(self):
        super().cancel()
        self.hash_release.set()


class GatedLease(LocalDatasetLease):
    """The actual owner-checked release cannot finish until the test allows it."""

    def __init__(self, root, task, *, gate_on_error=False):
        super().__init__(root, task)
        self.release_entered = threading.Event()
        self.release_allowed = threading.Event()
        self.gate_on_error = gate_on_error

    def __exit__(self, exc_type, exc, traceback):
        if exc is None or self.gate_on_error:
            self.release_entered.set()
            if not self.release_allowed.wait(30):
                raise AssertionError("test did not release the lease boundary")
        return super().__exit__(exc_type, exc, traceback)


class WorkerEvents(QObject):
    def __init__(self):
        super().__init__()
        self.text = []
        self.structured = []
        self.ok = []
        self.errors = []
        self.cancelled = []

    @Slot(str, str)
    def on_text(self, task_id, text):
        self.text.append((task_id, text))

    @Slot(str, object)
    def on_progress(self, task_id, event):
        self.structured.append((task_id, event))

    @Slot(str, object, str)
    def on_ok(self, task_id, report, stamp):
        self.ok.append((task_id, report, stamp))

    @Slot(str, str)
    def on_error(self, task_id, message):
        self.errors.append((task_id, message))

    @Slot(str, str)
    def on_cancelled(self, task_id, message):
        self.cancelled.append((task_id, message))


@unittest.skipUnless(rclone_exe().is_file(), "requires bundled rclone; temporary local alias only")
class SyncProgressIntegrationTests(unittest.TestCase):
    # Reuse the existing sandbox, alias config and real lease fixture without
    # inheriting (and rediscovering) its unrelated integration test cases.
    engine = integration.LocalMirrorIntegrationTests.engine

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        integration.LocalMirrorIntegrationTests.setUp(self)
        self.isolation = ExitStack()
        self.isolation.enter_context(patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.base / "local-profile"),
            "USERPROFILE": str(self.base / "profile"),
            "HOME": str(self.base / "profile"),
            "TEMP": str(self.base), "TMP": str(self.base),
        }))
        self.isolation.enter_context(patch.object(tempfile, "tempdir", str(self.base)))
        self.isolation.enter_context(patch("socket.create_connection", side_effect=(
            AssertionError("progress tests must not access the network"))))
        self.client.config.set("nas.url", "https://phone.invalid/progress-tests-only")

    def tearDown(self):
        self.isolation.close()
        integration.LocalMirrorIntegrationTests.tearDown(self)

    def observed_engine(self, task, *, progress_event=None, progress=None, cancelled=None):
        return SyncEngine(
            self.client, task, state_directory=self.base / "baseline",
            log_directory=self.base / "logs", progress=progress,
            progress_event=progress_event, cancelled=cancelled,
            lease_factory=lambda: LocalDatasetLease(self.remote, task),
        )

    def wait_for(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()
        self.assertTrue(predicate(), "worker did not reach the requested boundary")

    def finish_worker(self, worker, client, lease=None):
        client.hash_release.set()
        if lease is not None:
            lease.release_allowed.set()
        self.wait_for(lambda: not worker.isRunning())
        self.assertTrue(worker.wait(1000))
        self.app.processEvents()

    def prepare_receive(self, *, directories=(), local_directories=()):
        office = self.base / "office"
        office.mkdir()
        payload = b"verified progress payload\n"
        (office / "payload.bin").write_bytes(payload)
        for directory in directories:
            (office / directory).mkdir(parents=True, exist_ok=True)
        publisher = new_task(str(office))
        upload = self.engine(publisher)
        upload.execute(upload.preview())
        destination = self.base / "receive"
        destination.mkdir()
        for directory in local_directories:
            (destination / directory).mkdir(parents=True, exist_ok=True)
        task = new_task(str(destination), direction="download",
                        remote_path=publisher.remote_path,
                        versions_path=publisher.versions_path,
                        dataset_id=publisher.dataset_id)
        client = ProgressAlias(self.client.config, self.conf, f"phone:{task.remote_path}")
        client.payload_size = len(payload)
        self.client = client
        report = self.engine(task).preview()
        client.config.upsert_task(task)
        return task, report, client, destination, payload

    def receive_worker(self):
        task, report, client, destination, payload = self.prepare_receive()
        worker = SyncWorker(client.config, task, approved_digest=report.digest)
        events = WorkerEvents()
        worker.progress.connect(events.on_text)
        worker.progress_event.connect(events.on_progress)
        worker.finished_ok.connect(events.on_ok)
        worker.failed.connect(events.on_error)
        worker.cancelled_result.connect(events.on_cancelled)
        return worker, events, client, destination, payload

    def worker_boundaries(self, client, lease=None):
        stack = ExitStack()
        stack.enter_context(patch("app.core.sync.Rclone", return_value=client))
        # Supply the existing public lease-factory seam while leaving the real
        # coordinator metadata reads/writes and all engine planning untouched.
        stack.enter_context(patch("app.core.relay.DatasetCoordinator", side_effect=(
            lambda rclone, task, **kwargs: DatasetCoordinator(
                rclone, task, lease_factory=(lambda: lease) if lease is not None else
                (lambda: LocalDatasetLease(self.remote, task)))
        )))
        return stack

    def assert_indeterminate(self, event, stage=None):
        self.assertIsInstance(event, SyncProgress)
        if stage is not None:
            self.assertEqual(event.stage, stage)
        self.assertEqual((event.bytes_done, event.bytes_total), (None, None))
        self.assertIsNone(event.percent)
        self.assertNotIn("%", event.display())
        self.assertTrue(event.message)

    def test_preview_reports_busy_before_first_control_read(self):
        source = self.base / "office"
        source.mkdir()
        task = new_task(str(source))
        events = []
        before_control = []
        original = self.client.run
        def observe_control(args, *positional, **kwargs):
            if (not before_control and args[0] in ("lsjson", "cat")
                    and args[1].startswith("phone:.phonenas/")):
                before_control.append(tuple(events))
            return original(args, *positional, **kwargs)
        engine = self.observed_engine(task, progress_event=events.append)
        with patch.object(self.client, "run", side_effect=observe_control):
            report = engine.preview()
        self.assertFalse(report.blocked)
        self.assertTrue(before_control, "fixture did not observe the control-read boundary")
        self.assertTrue(before_control[0], "preview must report busy before potentially slow control reads")
        self.assert_indeterminate(before_control[0][-1])

    def test_staging_complete_does_not_report_transfer_while_remote_source_recheck_is_blocked(self):
        worker, events, client, destination, payload = self.receive_worker()
        client.gate_after_copies = 1
        lease = GatedLease(self.remote, worker.task)
        with self.worker_boundaries(client, lease):
            worker.start()
            try:
                self.wait_for(lambda: client.hash_entered.is_set() or not worker.isRunning())
                self.assertTrue(client.hash_entered.is_set(), events.errors)
                checkpoint = {
                    "running": worker.isRunning(), "outcome": worker.outcome,
                    "ok": list(events.ok), "errors": list(events.errors),
                    "destination": list(destination.iterdir()),
                    "message": events.text[-1][1] if events.text else "",
                    "event": events.structured[-1][1],
                }
                client.hash_release.set()
                self.wait_for(lambda: lease.release_entered.is_set() or not worker.isRunning())
                self.assertTrue(lease.release_entered.is_set(), events.errors)
                release_checkpoint = (worker.outcome, list(events.ok), events.structured[-1][1])
            finally:
                self.finish_worker(worker, client, lease)
        self.assertTrue(checkpoint["running"])
        self.assertEqual(checkpoint["outcome"], "pending")
        self.assertEqual(checkpoint["ok"], [])
        self.assertEqual(checkpoint["errors"], [])
        self.assertEqual(checkpoint["destination"], [])
        self.assertNotIn("正在传输", checkpoint["message"],
                         "staging reached 100%, but source hashing is pending and the destination is empty")
        self.assertNotIn("100%", checkpoint["message"])
        self.assert_indeterminate(checkpoint["event"], "recheck_source")
        self.assertIn("来源", checkpoint["event"].message)
        self.assertEqual(release_checkpoint[:2], ("pending", []))
        self.assert_indeterminate(release_checkpoint[2], "releasing")
        self.assertEqual(events.errors, [])
        self.assertEqual(events.cancelled, [])
        self.assertEqual(worker.outcome, "success")
        self.assertEqual(len(events.ok), 1)
        self.assertEqual((destination / "payload.bin").read_bytes(), payload)
        self.assertEqual({task_id for task_id, _ in events.structured}, {worker.task.id})
        self.assertEqual({task_id for task_id, _ in events.text}, {worker.task.id})
        for stage in ("staging", "applying"):
            completed = [event for _, event in events.structured
                         if event.stage == stage and event.percent == 100]
            self.assertTrue(completed, f"controlled copy JSON did not reach {stage}")

    def test_failed_destination_copy_clears_completed_bytes_before_delayed_lease_release(self):
        worker, events, client, destination, payload = self.receive_worker()
        client.failed_copy_destination = str(destination)
        lease = GatedLease(self.remote, worker.task, gate_on_error=True)
        with self.worker_boundaries(client, lease):
            worker.start()
            try:
                self.wait_for(lambda: lease.release_entered.is_set() or not worker.isRunning())
                self.assertTrue(lease.release_entered.is_set(), events.errors)
                checkpoint = {
                    "running": worker.isRunning(), "outcome": worker.outcome,
                    "ok": list(events.ok), "errors": list(events.errors),
                    "cancelled": list(events.cancelled),
                    "event": events.structured[-1][1],
                    "text": events.text[-1][1],
                }
            finally:
                self.finish_worker(worker, client, lease)
        self.assertTrue(checkpoint["running"])
        self.assertEqual(checkpoint["outcome"], "pending")
        self.assertEqual(checkpoint["ok"], [])
        self.assertEqual(checkpoint["errors"], [])
        self.assertEqual(checkpoint["cancelled"], [])
        self.assertTrue(any(event.stage == "applying" and event.percent == 100
                            for _, event in events.structured))
        self.assertEqual((destination / "payload.bin").read_bytes(), payload)
        self.assertEqual(worker.outcome, "failed")
        self.assertEqual(events.ok, [])
        self.assertEqual(events.cancelled, [])
        self.assertEqual(len(events.errors), 1)
        self.assertIn("9", events.errors[0][1])
        self.assertIsNone(checkpoint["event"].percent,
                          "copy exited nonzero, but stale 100% remained while lease cleanup was blocked")
        self.assertNotIn(checkpoint["event"].stage, ("staging", "applying"))
        self.assert_indeterminate(checkpoint["event"])
        self.assertNotIn("100%", checkpoint["text"])

    def test_worker_hash_failure_after_staging_never_emits_success_or_writes_destination(self):
        worker, events, client, destination, _ = self.receive_worker()
        client.gate_after_copies = 1
        client.hash_failure = True
        with self.worker_boundaries(client):
            worker.start()
            try:
                self.wait_for(lambda: client.hash_entered.is_set() or not worker.isRunning())
                self.assertTrue(client.hash_entered.is_set(), events.errors)
                self.assertEqual((worker.outcome, events.ok, list(destination.iterdir())),
                                 ("pending", [], []))
                self.assert_indeterminate(events.structured[-1][1], "recheck_source")
            finally:
                self.finish_worker(worker, client)
        self.assertEqual(worker.outcome, "failed")
        self.assertEqual(events.ok, [])
        self.assertEqual(events.cancelled, [])
        self.assertEqual(len(events.errors), 1)
        self.assertIn("isolated remote hash failure", events.errors[0][1])
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(any(event.stage == "saving" for _, event in events.structured))

    def test_worker_cancellation_at_remote_recheck_never_emits_success(self):
        worker, events, client, destination, _ = self.receive_worker()
        client.gate_after_copies = 1
        with self.worker_boundaries(client):
            worker.start()
            try:
                self.wait_for(lambda: client.hash_entered.is_set() or not worker.isRunning())
                self.assertTrue(client.hash_entered.is_set(), events.errors)
                self.assertEqual(worker.outcome, "pending")
                self.assertEqual(events.ok, [])
                self.assert_indeterminate(events.structured[-1][1], "recheck_source")
                worker.stop()
            finally:
                self.finish_worker(worker, client)
        self.assertEqual(worker.outcome, "cancelled")
        self.assertEqual(events.ok, [])
        self.assertEqual(events.errors, [])
        self.assertEqual(len(events.cancelled), 1)
        self.assertIn("取消", events.cancelled[0][1])
        self.assertEqual(list(destination.iterdir()), [])

    def test_final_source_hash_is_checked_after_apply_even_when_destination_is_verified(self):
        worker, events, client, destination, payload = self.receive_worker()
        client.gate_after_copies = 2
        source_file = self.remote / worker.task.remote_path / "payload.bin"
        with self.worker_boundaries(client):
            worker.start()
            try:
                self.wait_for(lambda: client.hash_entered.is_set() or not worker.isRunning())
                self.assertTrue(client.hash_entered.is_set(), events.errors)
                self.assertEqual((destination / "payload.bin").read_bytes(), payload)
                self.assertEqual(worker.outcome, "pending")
                self.assertEqual(events.ok, [])
                self.assert_indeterminate(events.structured[-1][1], "verify_source")
                previous = source_file.stat()
                source_file.write_bytes(b"X" * len(payload))
                os.utime(source_file, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            finally:
                self.finish_worker(worker, client)
        self.assertEqual(worker.outcome, "failed")
        self.assertEqual(events.ok, [])
        self.assertEqual(len(events.errors), 1)
        self.assertIn("来源", events.errors[0][1])
        self.assertIn("修改", events.errors[0][1])
        self.assertEqual((destination / "payload.bin").read_bytes(), payload)
        self.assertFalse(any(event.stage == "saving" for _, event in events.structured))

    def test_public_engine_stage_order_byte_reset_legacy_text_and_directory_optimization(self):
        source = self.base / "office"
        (source / "existing").mkdir(parents=True)
        (source / "known-empty").mkdir()
        (source / "existing" / "stable.bin").write_bytes(b"unchanged")
        (source / "existing" / "edited.bin").write_bytes(b"old revision")
        (source / "removed.bin").write_bytes(b"must be archived")
        task = new_task(str(source))
        initial = self.engine(task)
        initial.execute(initial.preview())
        (source / "existing" / "edited.bin").write_bytes(b"new revision")
        (source / "removed.bin").unlink()
        (source / "copied-parent" / "nested").mkdir(parents=True)
        (source / "copied-parent" / "nested" / "added.bin").write_bytes(b"new file")
        (source / "missing-empty" / "leaf").mkdir(parents=True)
        client = ProgressAlias(self.client.config, self.conf, f"phone:{task.remote_path}")
        client.stats_lines = [
            "not-json", json.dumps({"stats": None}),
            json.dumps({"stats": {"bytes": "bad", "totalBytes": 24}}),
            json.dumps({"stats": {"bytes": True, "totalBytes": 24}}),
            json.dumps({"stats": {"bytes": -1, "totalBytes": 24}}),
            json.dumps({"stats": {"bytes": 6, "totalBytes": 24}}),
            json.dumps({"stats": {"bytes": 24, "totalBytes": 24}}),
        ]
        self.client = client
        events, text = [], []
        engine = self.observed_engine(task, progress_event=events.append, progress=text.append)
        approval = engine.preview()
        self.assertEqual((approval.added, approval.updated, approval.deleted), (1, 1, 1))
        self.assertEqual([event.stage for event in events
                          if event.stage in ("inspect_local", "inspect_remote")],
                         ["inspect_local", "inspect_remote"])
        for event in events:
            self.assert_indeterminate(event)
        events.clear()
        text.clear()
        client.calls.clear()
        stamp = engine.execute(approval)
        expected = iter(("inspect_local", "inspect_remote", "staging", "check_staged",
                         "recheck_destination", "recheck_source", "applying", "archive",
                         "directories", "verify_destination", "verify_source", "saving",
                         "cleanup", "releasing"))
        next_stage = next(expected, None)
        for event in events:
            if event.stage == next_stage:
                next_stage = next(expected, None)
            self.assertIsInstance(event, SyncProgress)
            self.assertIn(event.display(), text)
            if event.stage not in ("staging", "applying"):
                self.assert_indeterminate(event)
        self.assertIsNone(next_stage, "required sync stages were missing or out of order")
        for stage in ("staging", "applying"):
            phase = [event for event in events if event.stage == stage]
            self.assert_indeterminate(phase[0], stage)
            self.assertEqual([(event.bytes_done, event.bytes_total, event.percent)
                              for event in phase if event.bytes_done is not None],
                             [(6, 24, 25), (24, 24, 100)])
        for index, event in enumerate(events[:-1]):
            if event.percent == 100:
                self.assert_indeterminate(events[index + 1])
        self.assertTrue(text)
        self.assertTrue(all(isinstance(message, str) and message for message in text))
        self.assertTrue(any("本阶段 25%" in message for message in text))
        self.assertNotIn("100%", text[-1])
        archive = [event for event in events if event.stage == "archive"]
        self.assertEqual((archive[-1].items_done, archive[-1].items_total), (1, 1))
        directories = [event for event in events if event.stage == "directories"]
        self.assertEqual(directories[0].items_done, 0)
        self.assertEqual(directories[-1].items_done, directories[-1].items_total)
        mirror = self.remote / task.remote_path
        self.assertEqual((mirror / "existing" / "stable.bin").read_bytes(), b"unchanged")
        self.assertEqual((mirror / "existing" / "edited.bin").read_bytes(), b"new revision")
        self.assertEqual((mirror / "copied-parent" / "nested" / "added.bin").read_bytes(), b"new file")
        self.assertTrue((mirror / "known-empty").is_dir())
        self.assertTrue((mirror / "missing-empty" / "leaf").is_dir())
        self.assertFalse((mirror / "removed.bin").exists())
        history = self.remote / task.versions_path / stamp
        self.assertEqual((history / "existing" / "edited.bin").read_bytes(), b"old revision")
        self.assertEqual((history / "removed.bin").read_bytes(), b"must be archived")
        mkdir_targets = {args[1] for args in client.calls if args[0] == "mkdir"}
        for known in ("existing", "known-empty", "copied-parent", "copied-parent/nested"):
            self.assertNotIn(f"phone:{task.remote_path}/{known}", mkdir_targets)
        self.assertTrue(any(target.startswith(f"phone:{task.remote_path}/missing-empty")
                            for target in mkdir_targets))

    def test_missing_empty_directories_are_created_even_if_all_files_are_unchanged(self):
        source = self.base / "office"
        source.mkdir()
        (source / "stable.bin").write_bytes(b"unchanged payload")
        task = new_task(str(source))
        initial = self.engine(task)
        initial.execute(initial.preview())
        (source / "new-empty" / "nested").mkdir(parents=True)
        self.client = ProgressAlias(self.client.config, self.conf, f"phone:{task.remote_path}")
        events = []
        engine = self.observed_engine(task, progress_event=events.append)
        report = engine.preview()
        self.assertEqual((report.added, report.updated, report.deleted, report.unchanged), (0, 0, 0, 1))
        stamp = engine.execute(report)
        self.assertTrue(stamp)
        self.assertEqual(self.client.copies, [])
        mirror = self.remote / task.remote_path
        self.assertTrue((mirror / "new-empty" / "nested").is_dir())
        self.assertEqual((mirror / "stable.bin").read_bytes(), b"unchanged payload")
        self.assertIn("verify_destination", [event.stage for event in events])
        self.assertIn("verify_source", [event.stage for event in events])
        for event in events:
            self.assert_indeterminate(event)
        self.client.calls.clear()
        self.assertEqual(engine.execute(engine.preview()), "")
        self.assertFalse(any(args[0] == "mkdir" for args in self.client.calls))

    def test_cancel_is_checked_even_when_every_directory_is_already_known(self):
        source = self.base / "office"
        source.mkdir()
        task = new_task(str(source))
        mirror = self.remote / task.remote_path
        mirror.mkdir(parents=True)
        for name in ("known-a", "known-b"):
            (source / name).mkdir()
            (mirror / name).mkdir()
        self.client = ProgressAlias(self.client.config, self.conf, f"phone:{task.remote_path}")
        cancelled = threading.Event()
        events = []
        def cancel_directories(event):
            events.append(event)
            if event.stage == "directories":
                cancelled.set()
        engine = self.observed_engine(task, progress_event=cancel_directories, cancelled=cancelled)
        report = engine.preview()
        with self.assertRaisesRegex(RuntimeError, "取消"):
            engine.execute(report)
        self.assertIn("directories", [event.stage for event in events])
        self.assertNotIn("saving", [event.stage for event in events])
        targets = {args[1] for args in self.client.calls if args[0] == "mkdir"}
        for name in ("known-a", "known-b"):
            self.assertTrue((mirror / name).is_dir())
            self.assertNotIn(f"phone:{task.remote_path}/{name}", targets)
        for event in events:
            self.assert_indeterminate(event)

    @unittest.skipUnless(os.name == "nt", "uses the real Windows junction boundary")
    def test_receiving_directory_optimization_does_not_follow_a_new_junction(self):
        import _winapi
        task, report, client, destination, payload = self.prepare_receive(
            directories=("redirect/new-empty",), local_directories=("redirect",))
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "sentinel.bin").write_bytes(b"must remain untouched")
        redirected = destination / "redirect"
        junction_created = False
        events = []
        def redirect_during_directories(event):
            nonlocal junction_created
            events.append(event)
            if event.stage == "directories" and not junction_created:
                redirected.rmdir()
                _winapi.CreateJunction(str(outside), str(redirected))
                junction_created = True
        engine = self.observed_engine(task, progress_event=redirect_during_directories)
        try:
            with self.assertRaisesRegex(ValueError, "重解析点|junction|符号链接"):
                engine.execute(report)
            self.assertTrue(junction_created)
            self.assertEqual((destination / "payload.bin").read_bytes(), payload)
            self.assertEqual(list(outside.iterdir()), [outside / "sentinel.bin"])
            self.assertEqual((outside / "sentinel.bin").read_bytes(), b"must remain untouched")
            self.assertNotIn("saving", [event.stage for event in events])
            self.assertFalse(any(args[0] == "mkdir" and Path(args[1]) == redirected / "new-empty"
                                 for args in client.calls))
        finally:
            if junction_created:
                redirected.rmdir()


if __name__ == "__main__":
    unittest.main()
