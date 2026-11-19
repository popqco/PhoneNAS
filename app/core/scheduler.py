from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from PySide6.QtCore import QObject, QTimer, Qt, Signal

from app.core.tasks import MirrorTask


class TaskQueue:
    """An in-memory serial scheduler; no Qt event loop or disk access required."""

    def __init__(self, tasks: Iterable[MirrorTask] = (), now: datetime | None = None):
        self._tasks: dict[str, MirrorTask] = {}
        self._next: dict[str, datetime] = {}
        self._queued: dict[str, bool] = {}
        self._completed: dict[str, datetime] = {}
        self._active: str | None = None
        self._paused = False
        self.update_tasks(tasks, now)

    def update_tasks(self, tasks: Iterable[MirrorTask], now: datetime | None = None) -> None:
        current = now or datetime.now()
        candidates = tuple(tasks)
        updated: dict[str, MirrorTask] = {}
        for task in candidates:
            if not isinstance(task, MirrorTask):
                raise ValueError("任务必须是 MirrorTask")
            MirrorTask.from_dict(task.to_dict())
            if task.id in updated:
                raise ValueError("任务 ID 不能重复")
            updated[task.id] = task
        schedules: dict[str, datetime] = {}
        for task_id, task in updated.items():
            if not task.enabled or not task.auto_sync:
                continue
            previous_task = self._tasks.get(task_id)
            if task_id in self._next and previous_task is not None:
                if (previous_task.interval_minutes == task.interval_minutes
                        and previous_task.last_sync_at == task.last_sync_at):
                    schedules[task_id] = self._next[task_id]
                    continue
            completed = self._completed.get(task_id)
            try:
                previous = datetime.fromisoformat(task.last_sync_at)
                if previous.tzinfo is not None and current.tzinfo is None:
                    previous = previous.astimezone().replace(tzinfo=None)
                elif previous.tzinfo is None and current.tzinfo is not None:
                    previous = previous.replace(tzinfo=current.tzinfo)
                baseline = max(previous, completed) if completed is not None else previous
                schedules[task_id] = baseline + timedelta(minutes=task.interval_minutes)
            except ValueError:
                schedules[task_id] = (completed + timedelta(minutes=task.interval_minutes)
                                      if completed is not None else current)
        self._tasks = updated
        self._completed = {task_id: when for task_id, when in self._completed.items() if task_id in updated}
        self._next = schedules
        self._queued = {task_id: manual for task_id, manual in self._queued.items()
                        if task_id in updated and updated[task_id].enabled
                        and (manual or updated[task_id].auto_sync)}

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def active_id(self) -> str | None:
        return self._active

    @property
    def queued_ids(self) -> tuple[str, ...]:
        return tuple(self._queued)

    @property
    def next_run_at(self) -> datetime | None:
        if self.paused:
            return None
        return min((when for task_id, when in self._next.items() if task_id != self._active), default=None)

    def next_for(self, task_id: str) -> datetime | None:
        if self.paused or task_id == self._active:
            return None
        return self._next.get(task_id)

    def enqueue(self, task_id: str, manual: bool = False) -> bool:
        task = self._tasks.get(task_id)
        if task is None or not task.enabled or (not manual and (self.paused or not task.auto_sync)):
            return False
        if task_id in self._queued:
            if manual and not self._queued[task_id]:
                self._queued[task_id] = True
                return True
            return False
        if task_id == self._active and not manual:
            return False
        self._queued[task_id] = manual
        return True

    def enqueue_all(self, manual: bool = False) -> list[str]:
        return [task_id for task_id in self._tasks if self.enqueue(task_id, manual)]

    def pause(self) -> None:
        self._paused = True

    def resume(self, now: datetime | None = None) -> None:
        self._paused = False
        self.due(now)

    def claim(self, task_id: str) -> bool:
        """Reserve the serial slot for an explicit, already-confirmed job."""
        task = self._tasks.get(task_id)
        if self._active is not None or task is None or not task.enabled:
            return False
        self._queued.pop(task_id, None)
        self._active = task_id
        return True

    def take_next(self, now: datetime | None = None) -> str | None:
        request = self.take_next_request()
        return request[0] if request is not None else None

    def take_next_request(self) -> tuple[str, bool] | None:
        """Reserve the slot and return (task ID, needs manual preview)."""
        if self._active is not None:
            return None
        for task_id, manual in self._queued.items():
            if self.paused and not manual:
                continue
            del self._queued[task_id]
            self._active = task_id
            return task_id, manual
        return None

    def skip_preview(self, task_id: str, now: datetime | None = None) -> None:
        """Skip this attempt, including any automatic request already due for it."""
        if self._active == task_id:
            self._active = None
        if self._queued.get(task_id) is False:
            del self._queued[task_id]
        when = self._next.get(task_id)
        current = now or datetime.now()
        if when is not None and when <= current:
            self._next[task_id] = current + timedelta(minutes=self._tasks[task_id].interval_minutes)

    def complete(self, task_id: str, now: datetime | None = None) -> None:
        if self._active != task_id:
            return
        self._active = None
        task = self._tasks.get(task_id)
        if task is not None:
            current = now or datetime.now()
            self._completed[task_id] = current
            if task.enabled and task.auto_sync:
                self._next[task_id] = current + timedelta(minutes=task.interval_minutes)

    def due(self, now: datetime | None = None) -> list[str]:
        if self.paused:
            return []
        current = now or datetime.now()
        result = []
        for task_id, when in self._next.items():
            if when <= current and self.enqueue(task_id):
                result.append(task_id)
        return result


class Scheduler(QObject):
    """Qt wake-up adapter. The main window owns dispatch via queue.take_next_request()."""

    ready = Signal()

    def __init__(self, tasks: Iterable[MirrorTask] = (), parent=None):
        super().__init__(parent)
        self.queue = TaskQueue(tasks)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_timeout)
        self._started = False

    @property
    def paused(self) -> bool:
        return self.queue.paused

    @property
    def next_run_at(self) -> datetime | None:
        return self.queue.next_run_at

    def start(self) -> None:
        self._started = True
        self._timer.start()
        self.queue.due()
        self.ready.emit()

    def stop(self) -> None:
        self._started = False
        self._timer.stop()
        self.queue.pause()

    def pause(self) -> None:
        self.queue.pause()
        self.ready.emit()

    def resume(self) -> None:
        self.queue.resume()
        if self._started:
            self._timer.start()
        self.ready.emit()

    def update_tasks(self, tasks: Iterable[MirrorTask]) -> None:
        self.queue.update_tasks(tasks)
        if self._started:
            self.queue.due()
        self.ready.emit()

    def request(self, task_id: str | None = None, manual: bool = True) -> None:
        if task_id is None:
            self.queue.enqueue_all(manual=manual)
        else:
            self.queue.enqueue(task_id, manual=manual)
        self.ready.emit()

    def complete(self, task_id: str) -> None:
        self.queue.complete(task_id)
        self.ready.emit()

    def _on_timeout(self) -> None:
        if self.queue.due():
            self.ready.emit()
