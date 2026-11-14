from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QTimer, Qt, Signal


class Scheduler(QObject):
    tick = Signal()

    def __init__(self, interval_minutes: int, parent=None):
        super().__init__(parent)
        self._interval = max(1, int(interval_minutes))
        self._paused = False
        self._next_run: datetime | None = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self._on_timeout)
        self._apply_interval()

    # -- public -----------------------------------------------------------
    @property
    def interval_minutes(self) -> int:
        return self._interval

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def next_run_at(self) -> datetime | None:
        return None if self._paused else self._next_run

    def start(self) -> None:
        self._paused = False
        self._arm()

    def stop(self) -> None:
        self._timer.stop()
        self._next_run = None

    def pause(self) -> None:
        self._paused = True
        self._timer.stop()
        self._next_run = None

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            self._arm()

    def set_interval(self, minutes: int) -> None:
        self._interval = max(1, int(minutes))
        self._apply_interval()
        if not self._paused:
            self._arm()

    def trigger_now(self) -> None:
        self.tick.emit()
        if not self._paused:
            self._arm()

    # -- internals --------------------------------------------------------
    def _apply_interval(self) -> None:
        self._timer.setInterval(self._interval * 60 * 1000)

    def _arm(self) -> None:
        self._timer.start()
        self._next_run = datetime.now() + timedelta(minutes=self._interval)

    def _on_timeout(self) -> None:
        self.tick.emit()
        if not self._paused:
            self._next_run = datetime.now() + timedelta(minutes=self._interval)
