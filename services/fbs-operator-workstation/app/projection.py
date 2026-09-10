"""Проекция заданий на экране.

Проекция не хранится в базе намеренно. Это копия того, что лежит в wms, и
вторая копия однажды разойдётся с первой — ровно так рабочее место и оказалось
в положении, когда на экране одно, в шлюзе другое, а в Odoo третье. Перезапуск
восстанавливает её одним опросом, за время меньше секунды.

Переживают перезапуск только собственные записи рабочего места — сессии
подбора, задания печати, журнал сканов. Они лежат в базе `workstation`.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from . import metrics
from .domain import PICKABLE_STATES, ScreenStatus, Task, TaskState

# Состояния, которые рабочее место держит на экране. Терминальные
# (`cancelled`, `handed`, `accepted`) не держит: экран показывает работу, а не
# архив.
SCREEN_STATES: tuple[str, ...] = (
    TaskState.NEW.value,
    TaskState.RESERVED.value,
    TaskState.PICKING.value,
    TaskState.PICKED.value,
    TaskState.PACKED.value,
    TaskState.LABELED.value,
    TaskState.IN_SUPPLY.value,
    TaskState.SHIPPED.value,
    TaskState.MANUAL_REVIEW.value,
    TaskState.SHORT.value,
    TaskState.DIVERGED.value,
)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Projection:
    """Задания, которые сейчас видит рабочее место.

    Единственный писатель — опросчик. Пока писателей двое, они однажды
    разойдутся, и экран покажет то, чего в wms нет.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._first_seen: dict[str, float] = {}
        # Когда задание перестало приходить в ответе и когда его последний раз
        # перепроверяли поимённо.
        self._missing_since: dict[str, float] = {}
        self._verified_at: dict[str, float] = {}
        self.available_total: int | None = None
        self.served_at: str | None = None
        self.last_poll_at: float | None = None
        self.last_poll_ok: bool = False
        self.last_error: str | None = None

    # ------------------------------------------------------------ запись

    def apply(self, tasks: Iterable[Task], *, available_total: int | None = None,
              served_at: str | None = None) -> list[Task]:
        """Принять результат опроса. Возвращает задания, увиденные впервые.

        Опрос только добавляет и обновляет — он никогда не стирает. Задание,
        не пришедшее в ответе, могло не поместиться в лимит, не подойти под
        фильтр сервера или уехать в терминальное состояние; отличить эти
        случаи по факту отсутствия нельзя. Стереть с экрана задание, которое
        сборщик держит в руках, — ровно та потеря, ради устранения которой всё
        переписано. Поэтому пропавшие сначала перепроверяются поимённо
        (`forget_if_gone` в опросчике), и только потом уходят.
        """
        seen_now = datetime.now(timezone.utc).timestamp()
        fresh: list[Task] = []
        with self._lock:
            arrived: set[str] = set()
            for task in tasks:
                arrived.add(task.task_id)
                if task.task_id not in self._tasks:
                    fresh.append(task)
                    self._first_seen[task.task_id] = seen_now
                    self._observe_delay(task, seen_now)
                self._tasks[task.task_id] = task
                self._missing_since.pop(task.task_id, None)
            for task_id in set(self._tasks) - arrived:
                self._missing_since.setdefault(task_id, seen_now)
            if available_total is not None:
                self.available_total = available_total
            if served_at:
                self.served_at = served_at
            self.last_poll_at = seen_now
            self.last_poll_ok = True
            self.last_error = None
        self._refresh_gauges()
        return fresh

    def stale_candidates(self, *, grace_seconds: float, recheck_seconds: float,
                         limit: int) -> list[str]:
        """Кого пора перепроверить поимённо.

        Не всех пропавших сразу: сервис может отдавать очередь порциями, и
        перечитывать по заданию на каждый опрос — это трафик на ровном месте.
        Берём тех, кто не приходит дольше `grace_seconds`, и не чаще
        `recheck_seconds` на задание.
        """
        now_ts = datetime.now(timezone.utc).timestamp()
        with self._lock:
            candidates = [
                (missing_at, task_id)
                for task_id, missing_at in self._missing_since.items()
                if now_ts - missing_at >= grace_seconds
                and now_ts - self._verified_at.get(task_id, 0.0) >= recheck_seconds
            ]
        candidates.sort()
        return [task_id for _missing_at, task_id in candidates[:max(0, limit)]]

    def mark_verified(self, task_id: str) -> None:
        with self._lock:
            self._verified_at[task_id] = datetime.now(timezone.utc).timestamp()

    def note_failure(self, error: str) -> None:
        """Опрос не удался.

        Проекция не очищается: устаревший экран лучше пустого, а факт
        устаревания виден отдельным полем и метрикой.
        """
        with self._lock:
            self.last_poll_ok = False
            self.last_error = error
            self.last_poll_at = datetime.now(timezone.utc).timestamp()

    def upsert(self, task: Task) -> None:
        """Точечное обновление после команды по конкретному заданию.

        Команда рабочего места — тоже ответ wms, а не догадка: обновляется тем,
        что сервис вернул, и следующий опрос всё равно перепроверит.
        """
        with self._lock:
            self._tasks[task.task_id] = task
            self._missing_since.pop(task.task_id, None)
        self._refresh_gauges()

    def forget(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)
            self._first_seen.pop(task_id, None)
            self._missing_since.pop(task_id, None)
            self._verified_at.pop(task_id, None)
        self._refresh_gauges()

    def _observe_delay(self, task: Task, seen_now: float) -> None:
        """Задержка «задание создано в wms → задание на экране».

        Критерий раздела 10: меньше 5 секунд p99. Мерить нужно от создания
        задания, а не от ответа сервера, иначе меряется скорость сети, а не
        то, сколько сборщик реально ждал работу.
        """
        created = _parse_ts(task.updated_at)
        if created is None:
            return
        delay = seen_now - created.timestamp()
        if 0 <= delay <= 3600:
            metrics.TASK_VISIBLE_DELAY.observe(delay)

    def _refresh_gauges(self) -> None:
        with self._lock:
            counts: dict[str, int] = {status.value: 0 for status in ScreenStatus}
            diverged = 0
            for task in self._tasks.values():
                counts[task.screen.value] = counts.get(task.screen.value, 0) + 1
                if task.diverged:
                    diverged += 1
        for status, count in counts.items():
            metrics.TASKS_ON_SCREEN.labels(screen_status=status).set(count)
        metrics.TASKS_DIVERGED.set(diverged)

    # ------------------------------------------------------------ чтение

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    def for_screen(self, *, assignee: str | None = None,
                   owner_external_id: str | None = None,
                   screen_status: str | None = None) -> list[Task]:
        """Задания для экрана, отсортированные по сроку Wildberries.

        По сроку, а не по времени создания: горит то, что WB ждёт раньше
        (раздел 7 мастера).
        """
        with self._lock:
            rows = list(self._tasks.values())
        if assignee:
            rows = [task for task in rows if task.assignee == assignee]
        if owner_external_id:
            rows = [task for task in rows if task.owner_external_id == owner_external_id]
        if screen_status:
            rows = [task for task in rows if task.screen.value == screen_status]
        rows.sort(key=lambda task: (task.deadline is None, task.deadline or "", task.task_id))
        return rows

    def picklist(self, assignee: str) -> list[Task]:
        """Лист подбора: то, что взял этот сборщик, в порядке обхода склада.

        Сортировка змейкой по стеллажам (`route_order`), а не по порядку
        заказов — иначе обход превращается в беготню (раздел «Лист подбора»).
        """
        with self._lock:
            rows = [task for task in self._tasks.values() if task.assignee == assignee]
        rows = [task for task in rows
                if task.state in {TaskState.PICKING, TaskState.RESERVED, TaskState.PICKED}]
        rows.sort(key=lambda task: task.route_order)
        return rows

    def pickable(self) -> list[Task]:
        with self._lock:
            rows = [task for task in self._tasks.values()
                    if task.state in PICKABLE_STATES and not task.assignee]
        rows.sort(key=lambda task: (task.deadline is None, task.deadline or ""))
        return rows

    def diverged(self) -> list[Task]:
        with self._lock:
            return [task for task in self._tasks.values() if task.diverged]

    def snapshot(self) -> dict[str, Any]:
        """Три числа полного пути — на них смотрит экран начальника смены."""
        with self._lock:
            on_screen = len(self._tasks)
            stale = (self.last_poll_at is None
                     or not self.last_poll_ok)
            return {
                "available_in_wms": self.available_total,
                "on_screen": on_screen,
                "served_at": self.served_at,
                "last_poll_at": self.last_poll_at,
                "last_poll_ok": self.last_poll_ok,
                "stale": stale,
                "last_error": self.last_error,
            }
