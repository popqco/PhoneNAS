from __future__ import annotations

import unittest
from pathlib import Path
from subprocess import TimeoutExpired
from types import SimpleNamespace
from unittest.mock import patch

from app.core import mount


STARTUP_BUDGET_MS = 30_000


class FakeTimer:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.interval = 0
        self.single_shot = False
        self.timeout = SimpleNamespace(connect=self._connect)

    def _connect(self, callback) -> None:
        self.fire = callback

    def setInterval(self, milliseconds: int) -> None:
        self.interval = milliseconds

    def setSingleShot(self, single_shot: bool) -> None:
        self.single_shot = single_shot

    def start(self, milliseconds: int | None = None) -> None:
        if milliseconds is not None:
            self.interval = milliseconds
        self.clock.timers[self] = self.clock.now_ms + self.interval

    def stop(self) -> None:
        self.clock.timers.pop(self, None)


class FakeClock:
    def __init__(self):
        self.now_ms = 100_000
        self.timers: dict[FakeTimer, int] = {}

    def monotonic(self) -> float:
        return self.now_ms / 1000

    def advance(self, milliseconds: int) -> None:
        until = self.now_ms + milliseconds
        while self.timers:
            timer = min(self.timers, key=self.timers.__getitem__)
            due = self.timers[timer]
            if due > until:
                break
            self.now_ms = due
            if timer.single_shot:
                timer.stop()
            else:
                timer.start()
            timer.fire()
        self.now_ms = until


class FakeProcess:
    def __init__(self):
        self.returncode: int | None = None
        self.ignore_terminate = False
        self.wait_timeouts: list[float] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        if not self.ignore_terminate:
            self.returncode = 1

    def kill(self) -> None:
        self.returncode = 1

    def wait(self, *, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        if self.returncode is None:
            raise TimeoutExpired("fake rclone", timeout)
        return self.returncode


class MountSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.drive_visible = False
        self.processes: list[FakeProcess] = []
        self.rclone = self.enterContext(patch.object(mount, "Rclone", autospec=True)).return_value
        self.rclone.popen.side_effect = self._launch
        self.enterContext(patch.object(mount, "data_dir", return_value=Path("unused-mount-test-data")))
        self.enterContext(patch.object(mount, "os", SimpleNamespace(path=SimpleNamespace(isdir=self._drive_isdir))))
        self.enterContext(patch.object(mount, "QTimer", side_effect=lambda _parent: FakeTimer(self.clock)))
        self.enterContext(patch.object(mount, "monotonic", self.clock.monotonic, create=True))
        config = SimpleNamespace(
            get={"mount.drive": "Q:"}.get, url="http://offline.invalid", username="", password="",
        )
        self.supervisor = mount.MountSupervisor(config)
        self.states: list[str] = []
        self.messages: list[str] = []
        self.supervisor.state_changed.connect(self.states.append)
        self.supervisor.log_line.connect(self.messages.append)
        self.addCleanup(self.supervisor.stop)

    def _drive_isdir(self, path: str) -> bool:
        self.assertEqual(path, "Q:\\")
        return self.drive_visible

    def _launch(self, *_args, **_kwargs) -> FakeProcess:
        process = FakeProcess()
        self.processes.append(process)
        return process

    def test_live_process_without_a_ready_drive_times_out_and_is_reaped(self) -> None:
        self.supervisor.start()
        process, = self.processes
        process.ignore_terminate = True
        self.clock.advance(STARTUP_BUDGET_MS - 1)
        self.assertEqual(self.supervisor.state, "starting")
        self.assertIsNone(process.poll())

        self.clock.advance(1)
        self.assertEqual(self.states, ["starting", "retrying"])
        self.assertIsNotNone(process.poll())
        self.assertEqual(process.wait_timeouts, [5, 5])
        self.rclone.release.assert_called_once_with(process)
        self.assertTrue(any("超时" in message and "Q:" in message for message in self.messages))

    def test_drive_ready_before_deadline_stays_mounted_while_process_is_alive(self) -> None:
        self.supervisor.start()
        self.clock.advance(10_000)
        self.drive_visible = True
        self.clock.advance(5_000)
        self.assertEqual(self.supervisor.state, "mounted")

        self.drive_visible = False
        self.clock.advance(60_000)
        self.assertEqual(self.states, ["starting", "mounted"])
        process, = self.processes
        self.assertIsNone(process.poll())
        self.rclone.release.assert_not_called()

    def test_unconfigured_mount_does_not_start_or_retry(self) -> None:
        self.supervisor.config.url = ""
        self.supervisor.start()
        self.clock.advance(300_000)
        self.assertEqual(self.states, ["error"])
        self.assertEqual(self.processes, [])
        self.rclone.write_conf.assert_not_called()

    def test_occupied_drive_is_rejected_without_launching_or_retrying(self) -> None:
        self.drive_visible = True
        self.supervisor.start()
        self.clock.advance(300_000)

        self.assertEqual(self.states, ["error"])
        self.assertTrue(any("占用" in message and "Q:" in message for message in self.messages))
        self.rclone.write_conf.assert_not_called()
        self.assertEqual(self.processes, [])

    def test_timeouts_use_the_existing_backoff_and_eventually_stop_retrying(self) -> None:
        self.supervisor.start()
        for attempts, delay in enumerate((5_000, 15_000, 30_000, 60_000, 120_000), start=1):
            self.clock.advance(STARTUP_BUDGET_MS - 1)
            self.assertEqual(self.supervisor.state, "starting")
            self.clock.advance(1)
            self.assertEqual(self.supervisor.state, "retrying")
            self.clock.advance(delay - 1)
            self.assertEqual(len(self.processes), attempts)
            self.clock.advance(1)
            self.assertEqual(len(self.processes), attempts + 1)
            self.assertEqual(self.supervisor.state, "starting")

        self.clock.advance(STARTUP_BUDGET_MS)
        self.assertEqual(self.supervisor.state, "error")
        self.clock.advance(300_000)
        self.assertEqual(len(self.processes), 6)
        self.assertTrue(all(process.poll() is not None for process in self.processes))

    def test_early_process_exit_uses_backoff_without_reporting_a_startup_timeout(self) -> None:
        self.supervisor.start()
        process, = self.processes
        process.returncode = 7
        self.clock.advance(1_500)

        self.assertEqual(self.states, ["starting", "retrying"])
        self.rclone.release.assert_called_once_with(process)
        self.assertTrue(any("code=7" in message for message in self.messages))
        self.assertFalse(any("超时" in message for message in self.messages))

    def test_stop_during_startup_reaps_process_and_prevents_timeout_retry(self) -> None:
        self.supervisor.start()
        self.clock.advance(10_000)
        self.supervisor.stop()
        self.clock.advance(300_000)

        self.assertEqual(self.states, ["starting", "stopped"])
        process, = self.processes
        self.assertIsNotNone(process.poll())
        self.rclone.release.assert_called_once_with(process)

    def test_stop_during_backoff_cancels_the_retry(self) -> None:
        self.supervisor.start()
        self.clock.advance(STARTUP_BUDGET_MS)
        self.assertEqual(self.supervisor.state, "retrying")

        self.supervisor.stop()
        self.clock.advance(300_000)
        self.assertEqual(self.states, ["starting", "retrying", "stopped"])
        self.assertEqual(len(self.processes), 1)


if __name__ == "__main__":
    unittest.main()
