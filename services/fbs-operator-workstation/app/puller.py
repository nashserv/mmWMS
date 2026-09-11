"""Опросчик заданий — новый источник истины рабочего места.

Это и есть переход, ради которого переписан путь заказа (раздел 6.1 мастера).
Раньше задание доезжало до экрана по цепочке из четырёх очередей и трёх
воркеров: терялось сообщение — терялся заказ, и никто об этом не узнавал.
Теперь рабочее место само спрашивает `/tasks/pull` и видит задание, даже если
RabbitMQ выключен целиком (пункт 14 полного прогона).

Событийный консьюмер остаётся, но только как ускоритель: он не пишет проекцию
и не может её испортить, он лишь говорит «сходи посмотри раньше срока».
Поэтому потерянное, задвоенное или кривое событие стоит ровно одного лишнего
опроса, а не потерянного заказа.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from datetime import datetime, timezone

from . import metrics
from .domain import TERMINAL_STATES, assignee_id
from .projection import SCREEN_STATES, Projection
from .wms_client import PullBatch, WmsClient, WmsUnavailable

logger = logging.getLogger("workstation.puller")

WORKER = "task_poller"

# Имя, под которым рабочее место когда-то представлялось wms при чтении
# очереди. Больше не отправляется: контракт требует `assignee` только при
# `claim: true`, а чтение ничего не занимает. Значение осталось затем, чтобы
# отличать задания, занятые прежними версиями экрана, — на стенде такие ещё
# лежат в базе.
SCREEN_ASSIGNEE = "workstation-screen"

# Сколько задание должно не приходить в ответе, прежде чем его перепроверят
# поимённо, и как часто перепроверять одно и то же. Числа подобраны так, чтобы
# закрытая пачка заданий уходила с экрана за секунды, а не за минуты, и при
# этом опрос не превращался в поштучное чтение всей очереди.
MISSING_GRACE_SECONDS = 3.0
MISSING_RECHECK_SECONDS = 20.0
VERIFY_PER_POLL = 25

# Сколько уехавшее задание висит на экране, прежде чем уйти.
#
# `shipped` и `in_supply` — работа, которая уже сделана: смотреть на неё
# полезно час-другой, а к следующей смене она превращается в стену из чужих
# заказов, в которой не найти своё. Терминальные состояния уходят раньше, по
# `_verify_missing`; эти двое терминальными не являются и висели вечно.
STALE_SHIPPED_SECONDS = 6 * 3600.0


class Poller:
    """Цикл опроса. Один на сервис, а не один на экран.

    Пять сборщиков смотрят в один и тот же склад; пять независимых опросов
    дали бы пятикратный трафик и пять разных представлений о том, что лежит
    в очереди.
    """

    def __init__(self, client: WmsClient, projection: Projection, *,
                 interval_seconds: float = 1.0, limit: int = 200,
                 store: Any = None) -> None:
        self._client = client
        self._projection = projection
        self._interval = interval_seconds
        self._limit = limit
        # База рабочего места: по ней видно, у кого сейчас открыта сессия
        # подбора. Нужна затем, чтобы спросить wms про задания В РУКАХ — общий
        # опрос отдаёт только свободные.
        self._store = store
        self._restored = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._adopt_lock = asyncio.Lock()
        # Кому отдано задание в обход (см. claim_ignored). Держится здесь,
        # потому что следующий ответ wms придёт с прежним assignee и затрёт
        # local-выдачу — а задание, потерявшее хозяина, уйдёт второму
        # сборщику. Двое за одной вещью — то, чего быть не должно никогда.
        self._adopted: dict[str, str] = {}
        self.polls_ok = 0
        self.polls_failed = 0
        # wms обязан не занимать ничего при `claim: false` (контракт
        # TasksPullParams). Заглушка потока 0 занимает — см.
        # docs/stream-b-requests.md, заявка 2. Флаг ставится по факту, а не
        # по настройке: против исправного сервиса он никогда не включится.
        self.claim_ignored = False
        self.claim_ignored_since: str | None = None

    # ------------------------------------------------------------ жизненный цикл

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._loop(), name="workstation-task-poller")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        metrics.worker_stopped(WORKER)

    def nudge(self, source: str = "bus") -> None:
        """Опросить раньше срока.

        Единственное, что событию позволено сделать с проекцией. Оно не
        приносит данные — оно приносит повод сходить за ними.
        """
        metrics.BUS_NUDGES.labels(outcome=source).inc()
        self._wake.set()

    # ------------------------------------------------------------ цикл

    async def _loop(self) -> None:
        metrics.worker_beat(WORKER, units=0)
        while not self._stopping:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — цикл опроса не имеет права умирать
                self.polls_failed += 1
                metrics.WORKER_ERRORS.labels(worker=WORKER).inc()
                self._projection.note_failure(str(error))
                logger.warning("опрос заданий не удался: %s", error)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            finally:
                self._wake.clear()

    async def poll_once(self) -> PullBatch:
        """Один опрос очереди. Ничего не занимает — только смотрит.

        `claim: false` здесь принципиально: экран обновляется чаще, чем человек
        берёт работу, и занимать задание при каждом обновлении значит
        разложить всю очередь по пустым сессиям.
        """
        started = time.perf_counter()
        previous = {task.task_id: task for task in self._projection.all()}
        try:
            batch = await self._client.tasks_pull(
                limit=self._limit, claim=False,
                states=SCREEN_STATES, previous=previous)
        except WmsUnavailable as error:
            metrics.POLLS.labels(outcome="unavailable").inc()
            self.polls_failed += 1
            self._projection.note_failure(str(error))
            # Воркер жив и работу делал — просто не смог. Разница между
            # «молчит» и «ошибается» видна по errors_total (инвариант 14).
            metrics.worker_beat(WORKER, units=0)
            metrics.WORKER_ERRORS.labels(worker=WORKER).inc()
            raise
        finally:
            metrics.POLL_DURATION.observe(time.perf_counter() - started)

        # Второй опрос — по каждому, у кого открыта сессия подбора. Общий
        # опрос отдаёт только свободные задания: всё, что сборщик взял,
        # исчезало с экрана ровно в момент, когда он его взял.
        in_hands = await self._tasks_in_hands()
        if in_hands:
            batch.tasks.extend(in_hands)

        self._restamp(batch.tasks)
        fresh = self._projection.apply(
            batch.tasks, available_total=batch.available_total, served_at=batch.served_at)
        self._projection.drop_stale_shipped(STALE_SHIPPED_SECONDS)
        await self._verify_missing()

        self._detect_claim_violation(batch)
        metrics.POLLS.labels(outcome="ok").inc()
        metrics.TASKS_PULLED.set(len(batch.tasks))
        if batch.available_total is not None:
            metrics.TASKS_AVAILABLE_IN_WMS.set(batch.available_total)
        metrics.worker_beat(WORKER, units=len(fresh))
        self.polls_ok += 1
        if fresh:
            logger.info("новых заданий на экране: %d", len(fresh))
        return batch

    async def _tasks_in_hands(self) -> list[Any]:
        """Задания, которые держат сборщики с открытыми сессиями.

        Спрашиваются поимённо по каждому актору: `claim: false` с заполненным
        `assignee` отдаёт И свободные, И задания этого исполнителя (контракт
        1.3.0). Без второго опроса проекция теряла всё, что в руках.

        База рабочего места может лежать (этап 3.3): тогда список сессий
        пуст, и экран показывает хотя бы свободную очередь.
        """
        if self._store is None:
            return []
        try:
            sessions = await self._store.open_sessions()
        except Exception:  # noqa: BLE001 — опрос не падает из-за базы экрана
            logger.warning("список открытых сессий недоступен: задания в руках "
                           "не попадут на экран этим опросом", exc_info=False)
            return []
        actors = {str(row["actor_id"]) for row in sessions if row.get("actor_id")}
        if not actors:
            return []
        collected: list[Any] = []
        seen: set[str] = set()
        for actor in sorted(actors):
            try:
                batch = await self._client.tasks_pull(
                    limit=self._limit, claim=False, assignee=assignee_id(actor),
                    states=SCREEN_STATES,
                    previous={task.task_id: task for task in self._projection.all()})
            except WmsUnavailable:
                # Свободную очередь мы уже получили; ради одного актора опрос
                # не роняем.
                continue
            for task in batch.tasks:
                if task.assignee and task.task_id not in seen:
                    seen.add(task.task_id)
                    collected.append(task)
        return collected

    async def restore_open_sessions(self) -> int:
        """Перечитать задания открытых сессий при старте.

        Рабочее место перезапустили посреди смены: проекция пуста, а у пяти
        сборщиков на руках по обходу. Без этого экран показывает «работы нет»
        человеку, который стоит с коробкой.
        """
        if self._store is None or self._restored:
            return 0
        self._restored = True
        try:
            sessions = await self._store.open_sessions()
        except Exception:  # noqa: BLE001
            logger.warning("сессии подбора при старте не перечитаны", exc_info=False)
            self._restored = False
            return 0
        restored = 0
        for row in sessions:
            try:
                lines = await self._store.session_lines(str(row["id"]))
            except Exception:  # noqa: BLE001
                continue
            for line in lines:
                task_id = str(line["task_id"])
                if self._projection.get(task_id) is not None:
                    continue
                try:
                    task = await self._client.task(task_id)
                except WmsUnavailable:
                    continue
                if task is None or task.state in TERMINAL_STATES:
                    continue
                self._projection.upsert(task)
                restored += 1
        if restored:
            logger.info("при старте возвращено на экран заданий из открытых сессий: %d",
                        restored)
        return restored

    async def _verify_missing(self) -> None:
        """Перепроверить задания, переставшие приходить в ответе.

        Убрать задание с экрана можно только одним способом — спросив о нём
        поимённо и получив терминальное состояние. Всё остальное — догадка, а
        догадка здесь означает потерянный заказ: именно так они и терялись,
        только раньше догадку делала очередь.
        """
        candidates = self._projection.stale_candidates(
            grace_seconds=MISSING_GRACE_SECONDS,
            recheck_seconds=MISSING_RECHECK_SECONDS,
            limit=VERIFY_PER_POLL)
        for task_id in candidates:
            self._projection.mark_verified(task_id)
            try:
                task = await self._client.task(
                    task_id, previous=self._projection.get(task_id))
            except WmsUnavailable:
                # Сервис молчит — задание остаётся на экране. Пропавший
                # сервис не повод стирать работу.
                return
            if task is None or task.state in TERMINAL_STATES:
                self._projection.forget(task_id)
                self._adopted.pop(task_id, None)
                metrics.TASKS_RETIRED.inc()
            else:
                self._restamp([task])
                self._projection.upsert(task)

    def _restamp(self, tasks: list[Any]) -> None:
        """Вернуть заданию сборщика, которому оно уже отдано в обход."""
        if not self._adopted:
            return
        for task in tasks:
            owner = self._adopted.get(task.task_id)
            if owner:
                task.assignee = owner

    def _detect_claim_violation(self, batch: PullBatch) -> None:
        """Заметить, что сервис занял задания при чтении экрана.

        Экран обновляется каждую секунду. Сервис, занимающий задание на
        чтении, за минуту разложит всю очередь по несуществующей сессии, и
        сборщики не получат ничего. Молчать об этом нельзя: без флага
        симптом выглядит как «заданий нет», то есть ровно как та беда,
        которую чиним.
        """
        stolen = [task for task in batch.tasks
                  if task.assignee == SCREEN_ASSIGNEE
                  or batch.leased_until.get(task.task_id)]
        if not stolen:
            return
        metrics.CONTRACT_FALLBACKS.labels(route="/tasks/pull", field="claim_ignored").inc()
        if not self.claim_ignored:
            self.claim_ignored = True
            self.claim_ignored_since = _now_iso()
            logger.error(
                "wms занял %d заданий при `claim: false` — это нарушение контракта. "
                "Рабочее место раздаёт их сборщикам из проекции, чтобы смена не встала; "
                "чинится в потоке 0 (docs/stream-b-requests.md, заявка 2)", len(stolen))

    async def adopt_screen_claimed(self, *, assignee: str, limit: int) -> list[Any]:
        """Раздать сборщику задания, которые wms занял на чтении экрана.

        Обходной путь, включающийся **только** после того, как нарушение
        замечено, и отключающийся сам, как только wms начнёт уважать
        `claim: false`. Замок нужен, чтобы два сборщика не забрали одно и то
        же задание: пять параллельных сессий — обычная работа склада.
        """
        if not self.claim_ignored:
            return []
        async with self._adopt_lock:
            free = [task for task in self._projection.all()
                    if task.assignee == SCREEN_ASSIGNEE
                    and task.task_id not in self._adopted][:max(0, limit)]
            for task in free:
                task.assignee = assignee
                self._adopted[task.task_id] = assignee
                self._projection.upsert(task)
        if free:
            logger.warning("сборщику %s отдано %d заданий из занятых экраном "
                           "(обход нарушения контракта)", assignee, len(free))
        return free

    # ------------------------------------------------------------ выдача

    async def claim(self, *, assignee: str, limit: int, lease_seconds: int,
                    owner_external_ids: list[str] | None = None) -> list[Any]:
        """Занять задания за конкретным сборщиком.

        Единственное место, где рабочее место просит `claim: true`. Гонку пяти
        сессий разруливает wms через `FOR UPDATE SKIP LOCKED`; здесь достаточно
        не устраивать вторую гонку на своей стороне — повтора этого вызова нет
        (второй занял бы вторую пачку), а результат кладётся в проекцию как
        есть.
        """
        previous = {task.task_id: task for task in self._projection.all()}
        # Контракт `wms` описывает `assignee` как uuid (версия 1.3.0). На
        # экране сборщик набирает себя руками, поэтому имя разворачивается в
        # тот же uuid, что и на стороне `wms`: «за кем задание» остаётся
        # воспроизводимым, а по значению `picker-1` в базе человека было не
        # найти.
        batch = await self._client.tasks_pull(
            assignee=assignee_id(assignee), limit=limit, claim=True,
            lease_seconds=lease_seconds,
            owner_external_ids=owner_external_ids, previous=previous)
        for task in batch.tasks:
            self._projection.upsert(task)
        if batch.available_total is not None:
            metrics.TASKS_AVAILABLE_IN_WMS.set(batch.available_total)
        metrics.worker_beat(WORKER, units=len(batch.tasks))
        return batch.tasks

    def status(self) -> dict[str, Any]:
        return {
            "interval_seconds": self._interval,
            "limit": self._limit,
            "polls_ok": self.polls_ok,
            "polls_failed": self.polls_failed,
            "running": bool(self._task and not self._task.done()),
            "claim_ignored": self.claim_ignored,
            "claim_ignored_since": self.claim_ignored_since,
            "claim_ignored_note": (
                "wms занимает задания при `claim: false` — нарушение контракта, "
                "заявка 2 в docs/stream-b-requests.md"
            ) if self.claim_ignored else None,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
