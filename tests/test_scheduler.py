from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from PySide6.QtCore import QCoreApplication, QElapsedTimer
from PySide6.QtTest import QTest

from app.core.scheduler import Scheduler, TaskQueue
from app.core.tasks import MirrorTask


def task(task_id: str, interval: int = 5, last: str = "", enabled: bool = True) -> MirrorTask:
    return MirrorTask(task_id, task_id, enabled, f"D:/offline/{task_id}",
                      f"backup/{task_id}/mirror", f"backup/{task_id}/versions",
                      interval, 30, "none", (), last)


class TaskQueueTests(unittest.TestCase):
    def test_automatic_requests_only_schedule_enabled_auto_tasks(self) -> None:
        now = datetime(2026, 11, 14, 10, 0)
        automatic = task("automatic")
        manual = replace(task("manual"), auto_sync=False)
        disabled = task("disabled", enabled=False)
        disabled_manual = replace(task("disabled-manual", enabled=False), auto_sync=False)
        queue = TaskQueue([manual, disabled, automatic, disabled_manual], now=now)
        self.assertIsNone(queue.next_for(manual.id))
        self.assertIsNone(queue.next_for(disabled.id))
        self.assertIsNone(queue.next_for(disabled_manual.id))
        self.assertEqual(queue.next_run_at, now)
        self.assertEqual(queue.due(now + timedelta(hours=1)), [automatic.id])
        self.assertFalse(queue.enqueue(manual.id))
        self.assertFalse(queue.enqueue(disabled.id, manual=True))
        self.assertFalse(queue.enqueue(disabled_manual.id, manual=True))
        self.assertEqual(queue.enqueue_all(), [])
        self.assertEqual(queue.queued_ids, (automatic.id,))

        manual_queue = TaskQueue([manual, disabled, disabled_manual], now=now)
        self.assertIsNone(manual_queue.next_run_at)
        self.assertEqual(manual_queue.due(now + timedelta(days=1)), [])
        self.assertEqual(manual_queue.enqueue_all(), [])
        self.assertEqual(manual_queue.enqueue_all(manual=True), [manual.id])
        self.assertEqual(manual_queue.take_next(now), manual.id)
        self.assertTrue(manual_queue.enqueue(manual.id, manual=True))
        self.assertFalse(manual_queue.enqueue(manual.id, manual=True))
        self.assertEqual(manual_queue.queued_ids, (manual.id,))

    def test_switch_to_manual_drops_auto_queue_but_keeps_active_and_explicit_requests(self) -> None:
        now = datetime(2026, 11, 14, 10, 0)
        active, stale, explicit = task("active"), task("stale"), task("explicit")
        queue = TaskQueue([active, stale, explicit], now=now)
        self.assertEqual(queue.due(now), [active.id, stale.id, explicit.id])
        self.assertEqual(queue.take_next(now), active.id)
        self.assertTrue(queue.enqueue(explicit.id, manual=True))
        self.assertTrue(queue.enqueue(active.id, manual=True))
        queue.update_tasks([replace(item, auto_sync=False) for item in (active, stale, explicit)], now=now)
        self.assertEqual(queue.active_id, active.id)
        self.assertEqual(queue.queued_ids, (explicit.id, active.id))
        self.assertIsNone(queue.next_run_at)
        for item in (active, stale, explicit):
            self.assertIsNone(queue.next_for(item.id))
        self.assertIsNone(queue.take_next(now))
        queue.pause()
        queue.complete(active.id, now)
        self.assertEqual(queue.take_next(now), explicit.id)
        queue.complete(explicit.id, now)
        self.assertEqual(queue.take_next(now), active.id)
        queue.complete(active.id, now)
        queue.resume(now + timedelta(hours=1))
        self.assertIsNone(queue.take_next(now))
        self.assertEqual(queue.queued_ids, ())

    def test_manual_completion_and_relay_claim_never_recreate_automatic_dates(self) -> None:
        now = datetime(2026, 11, 14, 10, 0)
        one = task("one")
        manual = replace(one, auto_sync=False)
        queue = TaskQueue([one], now=now)
        queue.due(now)
        self.assertEqual(queue.take_next(now), one.id)
        queue.update_tasks([manual], now=now)
        finished = now + timedelta(minutes=2)
        queue.complete(one.id, finished)
        self.assertIsNone(queue.next_for(one.id))
        self.assertIsNone(queue.next_run_at)
        self.assertEqual(queue.due(finished + timedelta(days=1)), [])
        queue.update_tasks([manual], now=finished)
        self.assertIsNone(queue.next_for(one.id))
        queue.pause()
        self.assertTrue(queue.claim(one.id))
        queue.complete(one.id, finished + timedelta(minutes=1))
        queue.resume(finished + timedelta(minutes=1))
        self.assertIsNone(queue.next_run_at)
        self.assertEqual(queue.queued_ids, ())
        queue.update_tasks([one], now=finished + timedelta(minutes=1))
        self.assertEqual(queue.next_for(one.id), now + timedelta(minutes=8))
        queue.update_tasks([replace(manual, enabled=False)], now=finished)
        self.assertFalse(queue.claim(one.id))

    def test_independent_due_times_serial_execution_and_completion_backoff(self) -> None:
        now = datetime(2026, 11, 14, 10, 0)
        first = task("first", interval=5)
        second = task("second", interval=10, last="2026-11-14 09:58:00")
        queue = TaskQueue([first, second], now=now)
        self.assertEqual(queue.next_for("first"), now)
        self.assertEqual(queue.next_for("second"), now + timedelta(minutes=8))
        self.assertEqual(queue.next_run_at, now)
        self.assertEqual(queue.due(now), ["first"])
        self.assertEqual(queue.due(now), [])
        self.assertEqual(queue.queued_ids, ("first",))
        self.assertEqual(queue.take_next(now), "first")
        self.assertEqual(queue.active_id, "first")
        self.assertIsNone(queue.take_next(now))
        later = now + timedelta(minutes=20)
        self.assertEqual(queue.due(later), ["second"])
        self.assertEqual(queue.queued_ids, ("second",))
        self.assertFalse(queue.enqueue("first"))
        queue.complete("first", later)
        self.assertEqual(queue.next_for("first"), later + timedelta(minutes=5))
        self.assertEqual(queue.take_next(later), "second")
        queue.complete("second", later + timedelta(seconds=30))
        self.assertEqual(queue.next_for("second"), later + timedelta(minutes=10, seconds=30))
        self.assertEqual(queue.due(later + timedelta(minutes=1)), [])
        self.assertIsNone(queue.take_next(later + timedelta(minutes=1)))

    def test_manual_reserves_at_most_one_rerun_while_auto_never_piles_up(self) -> None:
        now = datetime(2026, 11, 14, 10, 0)
        queue = TaskQueue([task("one"), task("two")], now=now)
        self.assertEqual(queue.enqueue_all(), ["one", "two"])
        self.assertEqual(queue.enqueue_all(), [])
        self.assertEqual(queue.take_next(now), "one")
        self.assertFalse(queue.enqueue("one"))
        self.assertTrue(queue.enqueue("one", manual=True))
        self.assertFalse(queue.enqueue("one", manual=True))
        self.assertEqual(queue.due(now + timedelta(hours=1)), [])
        self.assertEqual(queue.queued_ids, ("two", "one"))
        queue.complete("one", now + timedelta(minutes=1))
        self.assertEqual(queue.take_next(now + timedelta(minutes=1)), "two")
        queue.complete("two", now + timedelta(minutes=1))
        self.assertEqual(queue.take_next(now + timedelta(minutes=1)), "one")
        queue.complete("one", now + timedelta(minutes=1))
        self.assertIsNone(queue.take_next(now + timedelta(minutes=1)))
        self.assertEqual(queue.next_for("one"), now + timedelta(minutes=6))

    def test_update_cleans_deleted_disabled_and_keeps_active_slot_until_completion(self) -> None:
        now = datetime(2026, 11, 14, 10, 0)
        one, two, three = task("one"), task("two"), task("three")
        queue = TaskQueue([one, two, three], now=now)
        queue.enqueue_all(manual=True)
        self.assertEqual(queue.take_next(now), "one")
        queue.enqueue("one", manual=True)
        queue.update_tasks([replace(one, enabled=False), three], now=now)
        self.assertEqual(queue.queued_ids, ("three",))
        self.assertEqual(queue.active_id, "one")
        self.assertIsNone(queue.take_next(now))
        self.assertFalse(queue.enqueue("one", manual=True))
        self.assertFalse(queue.enqueue("two", manual=True))
        self.assertIsNone(queue.next_for("one"))
        self.assertIsNone(queue.next_for("two"))
        queue.complete("wrong-id", now)
        self.assertEqual(queue.active_id, "one")
        queue.complete("one", now)
        self.assertEqual(queue.take_next(now), "three")
        queue.update_tasks([], now=now)
        self.assertEqual(queue.active_id, "three")
        self.assertEqual(queue.queued_ids, ())
        queue.complete("three", now)
        self.assertIsNone(queue.active_id)
        self.assertIsNone(queue.next_run_at)

    def test_update_preserves_completion_backoff_and_applies_interval_or_success_changes(self) -> None:
        now = datetime(2026, 11, 14, 10, 0)
        one = task("one", interval=5)
        queue = TaskQueue([one], now=now)
        queue.enqueue("one")
        queue.take_next(now)
        finished = now + timedelta(minutes=2)
        queue.complete("one", finished)
        queue.update_tasks([replace(one, name="renamed")], now=finished + timedelta(seconds=10))
        self.assertEqual(queue.next_for("one"), now + timedelta(minutes=7))
        queue.update_tasks([replace(one, interval_minutes=10)], now=finished + timedelta(seconds=20))
        self.assertEqual(queue.next_for("one"), now + timedelta(minutes=12))
        queue.update_tasks([replace(one, interval_minutes=10, last_sync_at="2026-11-14 10:04:00")], now=finished)
        self.assertEqual(queue.next_for("one"), now + timedelta(minutes=14))

    def test_explicit_relay_claim_holds_slot_and_advances_ordinary_schedule(self) -> None:
        now = datetime(2026, 11, 14, 10, 0)
        queue = TaskQueue([task("one"), task("two")], now=now)
        queue.due(now)
        self.assertTrue(queue.claim("two"))
        self.assertEqual(queue.active_id, "two")
        self.assertFalse(queue.claim("one"))
        self.assertNotIn("two", queue.due(now + timedelta(hours=1)))
        self.assertNotIn("two", queue.queued_ids)
        queue.complete("two", now + timedelta(minutes=1))
        self.assertEqual(queue.next_for("two"), now + timedelta(minutes=6))
        self.assertEqual(queue.take_next(), "one")

    def test_pause_blocks_existing_auto_queue_but_explicit_manual_can_dispatch(self) -> None:
        now = datetime(2026, 11, 14, 10, 0)
        queue = TaskQueue([task("one"), task("two"), task("three")], now=now)
        queue.due(now)
        self.assertEqual(queue.take_next(now), "one")
        queue.pause()
        self.assertTrue(queue.paused)
        self.assertIsNone(queue.next_run_at)
        self.assertIsNone(queue.next_for("two"))
        self.assertEqual(queue.due(now + timedelta(hours=2)), [])
        self.assertFalse(queue.enqueue("three"))
        queue.complete("one", now + timedelta(minutes=1))
        self.assertIsNone(queue.take_next(now + timedelta(minutes=1)))
        self.assertTrue(queue.enqueue("three", manual=True))
        self.assertFalse(queue.enqueue("three", manual=True))
        self.assertEqual(queue.take_next(now + timedelta(minutes=1)), "three")
        queue.complete("three", now + timedelta(minutes=1))
        self.assertIsNone(queue.take_next(now + timedelta(minutes=1)))
        queue.resume(now + timedelta(minutes=1))
        self.assertFalse(queue.paused)
        self.assertEqual(queue.take_next(now + timedelta(minutes=1)), "two")

    def test_skipped_manual_preview_defers_only_its_overdue_automatic_request(self) -> None:
        now = datetime(2026, 11, 14, 10, 0)
        later = task("later", last="2026-11-14 09:59:00")
        queue = TaskQueue([task("automatic"), task("preview"), later], now=now)
        queue.due(now)
        queue.enqueue("preview", manual=True)
        queue.pause()
        self.assertEqual(queue.take_next_request(), ("preview", True))
        self.assertIsNone(queue.take_next_request())
        queue.skip_preview("preview", now)
        self.assertIsNone(queue.take_next_request())
        queue.resume(now + timedelta(minutes=1))
        self.assertEqual(queue.next_for("preview"), now + timedelta(minutes=5))
        self.assertEqual(queue.next_for("later"), now + timedelta(minutes=4))
        self.assertEqual(queue.take_next_request(), ("automatic", False))
        queue.complete("automatic", now + timedelta(minutes=1))
        self.assertIsNone(queue.take_next_request())

        # A card preview does not claim the slot; its due automatic job may be queued.
        queue.skip_preview("later", now + timedelta(minutes=1))
        self.assertEqual(queue.next_for("later"), now + timedelta(minutes=4))
        self.assertEqual(queue.due(now + timedelta(minutes=4)), ["later"])
        queue.skip_preview("later", now + timedelta(minutes=4))
        self.assertEqual(queue.queued_ids, ())
        self.assertEqual(queue.due(now + timedelta(minutes=5)), ["preview"])


class SchedulerAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def test_ready_dispatch_completion_and_paused_manual_request(self) -> None:
        scheduler = Scheduler([task("one"), task("two")])
        started: list[str] = []

        def dispatch() -> None:
            next_id = scheduler.queue.take_next()
            if next_id is not None:
                started.append(next_id)

        scheduler.ready.connect(dispatch)
        try:
            scheduler.start()
            self.assertEqual(started, ["one"])
            self.assertEqual(scheduler.queue.active_id, "one")
            scheduler.pause()
            scheduler.complete("one")
            self.assertEqual(started, ["one"])
            scheduler.request("two", manual=True)
            self.assertEqual(started, ["one", "two"])
            scheduler.complete("two")
            self.assertIsNone(scheduler.queue.active_id)
            scheduler.resume()
            scheduler.update_tasks([task("new")])
            self.assertEqual(started, ["one", "two", "new"])
            scheduler.stop()
            scheduler.complete("new")
            self.assertTrue(scheduler.queue.paused)
        finally:
            scheduler.stop()
            scheduler.deleteLater()

    def test_start_and_restart_never_clear_a_pause(self) -> None:
        scheduler = Scheduler([task("due")])
        dispatched = []
        scheduler.ready.connect(lambda: dispatched.append(scheduler.queue.take_next_request()))
        try:
            scheduler.pause()
            scheduler.start()
            scheduler._on_timeout()
            self.assertTrue(scheduler.paused)
            self.assertEqual(scheduler.queue.queued_ids, ())
            self.assertEqual(dispatched, [None, None])
            scheduler.stop()
            scheduler.start()
            self.assertTrue(scheduler.paused)
            self.assertEqual(dispatched, [None, None, None])
            scheduler.resume()
            self.assertFalse(scheduler.paused)
            self.assertEqual(dispatched[-1], ("due", False))
        finally:
            scheduler.stop()
            scheduler.deleteLater()

    def test_timer_checks_due_without_manual_request(self) -> None:
        future = datetime.now() + timedelta(seconds=0.2)
        last = (future - timedelta(minutes=1)).isoformat(timespec="microseconds")
        scheduler = Scheduler([task("later", interval=1, last=last)])
        started: list[str] = []

        def dispatch() -> None:
            next_id = scheduler.queue.take_next()
            if next_id is not None:
                started.append(next_id)

        scheduler.ready.connect(dispatch)
        try:
            scheduler.start()
            self.assertEqual(started, [])
            elapsed = QElapsedTimer()
            elapsed.start()
            while not started and elapsed.elapsed() < 2500:
                QTest.qWait(20)
            self.assertEqual(started, ["later"])
            self.assertEqual(scheduler.queue.active_id, "later")
        finally:
            scheduler.stop()
            scheduler.deleteLater()


if __name__ == "__main__":
    unittest.main()
