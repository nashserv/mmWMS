"""База рабочего места — то, что знает только оно само.

Проекция заданий сюда не попадает (см. комментарий в `migrations/001`). Здесь
лежат сессии подбора, журнал сканов, задания печати, причины отмен и то, какой
формат этикетки реально понял принтер конкретной станции.

Отдельно про отказ базы. Недоступный Postgres не имеет права остановить смену:
экран продолжает показывать задания из проекции, сборщик продолжает подбор.
Но потерянная запись не проглатывается молча — она считается счётчиком и
опускает готовность сервиса. Молчаливая потеря записи это ровно та практика,
из-за которой 2645 отмен остались без причины.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable, Sequence

from prometheus_client import Counter

from .domain import Task, uid

logger = logging.getLogger("workstation.store")

STORE_ERRORS = Counter(
    "mmx_workstation_store_errors_total",
    "Writes to the workstation database that did not land.",
    ("operation",),
)


class StoreUnavailable(RuntimeError):
    """База рабочего места недоступна, и пробовать сейчас не надо."""


def _is_connection_broken(error: Exception) -> bool:
    """Сломано соединение или плохи данные.

    `OperationalError` — сеть, сервер, таймаут: соединение выбрасывается.
    `DataError`, `IntegrityError`, `ProgrammingError` — наш запрос или наши
    значения: соединение цело и остаётся в пуле.
    """
    try:
        import psycopg
    except ImportError:
        return True
    if isinstance(error, (psycopg.DataError, psycopg.IntegrityError,
                          psycopg.ProgrammingError)):
        return False
    return True


class Store:
    """Минимальный асинхронный доступ к базе `workstation`.

    Пул написан здесь, а не взят библиотекой, ровно по одной причине: набор
    зависимостей платформы заморожен (раздел 2.2 мастера), `psycopg_pool` в нём
    нет, а вводить новую зависимость ради тридцати строк дороже, чем написать
    их.
    """

    def __init__(self, dsn: str, *, max_connections: int = 8,
                 retry_after_seconds: float = 10.0) -> None:
        self._dsn = dsn
        self._max = max_connections
        self._free: list[Any] = []
        self._lock = asyncio.Lock()
        self._opened = 0
        self.available = False
        self.last_error: str | None = None
        # Предохранитель. База лежит — каждый запрос вставал на `connect_timeout`
        # и держал в этом ожидании экран: опрос раз в секунду, пять экранов,
        # и рабочее место превращалось в очередь из ждущих корутин. После
        # отказа соединение не пробуется чаще раза в десять секунд.
        self._retry_after = retry_after_seconds
        self._closed_until = 0.0
        # Семафор на число соединений: без него `_acquire` открывал их
        # столько, сколько пришло запросов — и упирался в `max_connections`
        # Postgres, а не в свой.
        self._slots = asyncio.Semaphore(max_connections)

    # ------------------------------------------------------------ соединения

    async def _connect(self) -> Any:
        import psycopg
        from psycopg.rows import dict_row

        return await psycopg.AsyncConnection.connect(
            # Две секунды, а не пять: экран опрашивает очередь раз в
            # секунду, и ждать соединения дольше самого цикла нельзя.
            self._dsn, row_factory=dict_row, autocommit=True, connect_timeout=2)

    async def _connect_soon(self) -> None:
        """Пропускает попытку соединения, пока предохранитель не остыл."""
        if time.monotonic() < self._closed_until:
            raise StoreUnavailable(
                f"база рабочего места недоступна; следующая попытка через "
                f"{self._closed_until - time.monotonic():.0f} с")

    async def _acquire(self) -> Any:
        async with self._lock:
            while self._free:
                connection = self._free.pop()
                if not connection.closed:
                    return connection
                self._opened -= 1
        await self._connect_soon()
        await self._slots.acquire()
        async with self._lock:
            self._opened += 1
        try:
            return await self._connect()
        except Exception:
            async with self._lock:
                self._opened -= 1
            self._slots.release()
            self._closed_until = time.monotonic() + self._retry_after
            raise

    async def _release(self, connection: Any, *, broken: bool = False) -> None:
        self._slots.release()
        async with self._lock:
            if broken or connection.closed or len(self._free) >= self._max:
                self._opened -= 1
                try:
                    await connection.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            self._free.append(connection)

    async def close(self) -> None:
        async with self._lock:
            connections, self._free = self._free, []
            self._opened = 0
        for connection in connections:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001
                pass

    async def execute(self, sql: str, params: Sequence[Any] = (), *,
                      fetch: str = "none", operation: str = "execute") -> Any:
        """Один запрос. Ошибка считается и логируется, но не валит операцию склада."""
        connection = None
        try:
            connection = await self._acquire()
            async with connection.cursor() as cursor:
                await cursor.execute(sql, tuple(params))
                if fetch == "one":
                    result = await cursor.fetchone()
                elif fetch == "all":
                    result = await cursor.fetchall()
                else:
                    result = None
            self.available = True
            self.last_error = None
            self._closed_until = 0.0
            await self._release(connection)
            return result
        except StoreUnavailable as error:
            # Предохранитель: соединение даже не пробовалось. Ждать
            # `connect_timeout` на каждом запросе значит держать экран в
            # очереди из ждущих корутин.
            self.available = False
            self.last_error = str(error)
            STORE_ERRORS.labels(operation=operation).inc()
            return None
        except Exception as error:  # noqa: BLE001 — база упала, смена продолжается
            # Разница между «соединение сломано» и «данные плохие»
            # принципиальна: в первом случае соединение выбрасывается, во
            # втором остаётся в пуле. Выбрасывать его на каждой опечатке в
            # параметрах значит пересоздавать пул на ровном месте.
            broken = _is_connection_broken(error)
            if broken:
                self.available = False
                self._closed_until = time.monotonic() + self._retry_after
            self.last_error = f"{type(error).__name__}: {error}"
            STORE_ERRORS.labels(operation=operation).inc()
            logger.warning("запись в базу рабочего места не удалась (%s): %s",
                           operation, self.last_error)
            if connection is not None:
                await self._release(connection, broken=broken)
            return None

    async def ping(self) -> bool:
        row = await self.execute("SELECT 1 AS ok", fetch="one", operation="ping")
        return bool(row)

    # ------------------------------------------------------------ сессии подбора

    async def open_session(self, *, actor_id: str, station_id: str | None,
                           picklist_barcode: str) -> str | None:
        """Заводит сессию подбора. `None` — база не приняла запись.

        Раньше возвращался идентификатор в любом случае: сессии в базе нет, а
        сборщик работает с её номером — и все последующие сканы уходят в
        никуда, не сказав об этом ни слова.
        """
        session_id = uid()
        await self.execute(
            "INSERT INTO workstation_pick_session (id, actor_id, station_id, state, "
            "picklist_barcode) VALUES (%s, %s, %s, 'picking', %s) "
            "RETURNING id",
            (session_id, actor_id, station_id, picklist_barcode),
            fetch="one", operation="open_session")
        return session_id if self.available else None

    async def add_lines(self, session_id: str, tasks: Iterable[Task]) -> int:
        """Строки листа. Повтор по (сессия, задание) ничего не добавляет.

        Сборщик может обновить лист, не потеряв уже проставленные сканы.
        """
        added = 0
        for task in tasks:
            placement = task.placements[0] if task.placements else None
            await self.execute(
                "INSERT INTO workstation_pick_line (id, session_id, task_id, "
                "owner_external_id, barcode, name, quantity, cell_address, box_barcode, "
                "route_order) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (session_id, task_id) DO NOTHING",
                (uid(), session_id, task.task_id, task.owner_external_id, task.barcode,
                 task.name, max(1, task.quantity), task.cell_address, task.box_barcode,
                 placement.route_order if placement else None),
                operation="add_lines")
            added += 1
        return added

    async def session_lines(self, session_id: str) -> list[dict[str, Any]]:
        rows = await self.execute(
            "SELECT task_id, owner_external_id, barcode, name, quantity, cell_address, "
            "box_barcode, route_order, scanned_at, scan_result "
            "FROM workstation_pick_line WHERE session_id = %s "
            "ORDER BY route_order NULLS LAST, cell_address NULLS LAST, task_id",
            (session_id,), fetch="all", operation="session_lines")
        return list(rows or [])

    async def session(self, session_id: str) -> dict[str, Any] | None:
        return await self.execute(
            "SELECT id, actor_id, station_id, state, picklist_barcode, started_at, finished_at "
            "FROM workstation_pick_session WHERE id = %s",
            (session_id,), fetch="one", operation="session")

    async def session_by_barcode(self, picklist_barcode: str) -> dict[str, Any] | None:
        """Лист, найденный на складе, возвращается к своей сессии по штрихкоду."""
        return await self.execute(
            "SELECT id, actor_id, station_id, state, picklist_barcode, started_at, finished_at "
            "FROM workstation_pick_session WHERE picklist_barcode = %s",
            (picklist_barcode,), fetch="one", operation="session_by_barcode")

    async def open_sessions(self) -> list[dict[str, Any]]:
        rows = await self.execute(
            "SELECT s.id, s.actor_id, s.station_id, s.state, s.picklist_barcode, s.started_at, "
            "count(l.id) AS lines_total, "
            "count(l.id) FILTER (WHERE l.scan_result = 'ok') AS lines_picked "
            "FROM workstation_pick_session s "
            "LEFT JOIN workstation_pick_line l ON l.session_id = s.id "
            "WHERE s.state IN ('open', 'picking') "
            "GROUP BY s.id ORDER BY s.started_at",
            fetch="all", operation="open_sessions")
        return list(rows or [])

    async def finish_session(self, session_id: str, *, state: str = "completed") -> None:
        await self.execute(
            "UPDATE workstation_pick_session SET state = %s, finished_at = now() "
            "WHERE id = %s AND state IN ('open', 'picking')",
            (state, session_id), operation="finish_session")

    # ------------------------------------------------------------ сканы

    async def record_scan(self, *, task_id: str, stage: str, barcode: str,
                          scan_result: str, accepted: bool, actor_id: str | None,
                          station_id: str | None = None,
                          session_id: str | None = None) -> None:
        """Скан в журнал — принятый и отклонённый одинаково.

        Отклонённые нужны больше принятых: по ним видно, где сборщик берёт не
        то, и работает ли контрольный скан вообще.
        """
        await self.execute(
            "INSERT INTO workstation_scan (id, task_id, stage, barcode, scan_result, "
            "accepted, actor_id, station_id, session_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (uid(), task_id, stage, barcode, scan_result, accepted, actor_id,
             station_id, session_id),
            operation="record_scan")
        if session_id:
            await self.execute(
                "UPDATE workstation_pick_line SET scanned_at = now(), scan_result = %s "
                "WHERE session_id = %s AND task_id = %s",
                (scan_result, session_id, task_id), operation="record_scan_line")

    async def rejected_scans(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.execute(
            "SELECT task_id, stage, barcode, scan_result, actor_id, scanned_at "
            "FROM workstation_scan WHERE NOT accepted ORDER BY scanned_at DESC LIMIT %s",
            (limit,), fetch="all", operation="rejected_scans")
        return list(rows or [])

    # ------------------------------------------------------------ печать

    async def start_print(self, *, idempotency_key: str, task_id: str, station_id: str,
                          label_format: str, checksum: str, payload_bytes: int,
                          copies: int, reprint: bool, reason: str | None,
                          actor_id: str | None) -> str:
        job_id = uid()
        await self.execute(
            "INSERT INTO workstation_print_job (id, idempotency_key, task_id, station_id, "
            "label_format, checksum, payload_bytes, copies, reprint, reason, actor_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (job_id, idempotency_key, task_id, station_id, label_format, checksum,
             payload_bytes, copies, reprint, reason, actor_id),
            operation="start_print")
        return job_id

    async def finish_print(self, *, idempotency_key: str, outcome: str,
                           click_to_agent_ms: float | None = None,
                           agent_write_ms: float | None = None,
                           error: str | None = None) -> None:
        await self.execute(
            "UPDATE workstation_print_job SET outcome = %s, "
            "sent_at = COALESCE(sent_at, CASE WHEN %s IN ('sent','written') THEN now() END), "
            "written_at = COALESCE(written_at, CASE WHEN %s = 'written' THEN now() END), "
            "click_to_agent_ms = COALESCE(%s, click_to_agent_ms), "
            "agent_write_ms = COALESCE(%s, agent_write_ms), "
            "error = %s WHERE idempotency_key = %s",
            (outcome, outcome, outcome, click_to_agent_ms, agent_write_ms, error,
             idempotency_key),
            operation="finish_print")

    async def print_job(self, idempotency_key: str) -> dict[str, Any] | None:
        return await self.execute(
            "SELECT id, task_id, station_id, label_format, outcome, reprint, reason, "
            "click_to_agent_ms, agent_write_ms, requested_at, written_at "
            "FROM workstation_print_job WHERE idempotency_key = %s",
            (idempotency_key,), fetch="one", operation="print_job")

    async def last_print(self) -> dict[str, Any] | None:
        return await self.execute(
            "SELECT task_id, station_id, label_format, outcome, click_to_agent_ms, "
            "agent_write_ms, written_at FROM workstation_print_job "
            "WHERE written_at IS NOT NULL ORDER BY written_at DESC LIMIT 1",
            fetch="one", operation="last_print")

    async def print_stats(self) -> dict[str, Any] | None:
        """Доля перепечаток и худшие времена — на экран начальника смены."""
        return await self.execute(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE reprint) AS reprints, "
            "count(*) FILTER (WHERE outcome = 'failed') AS failed, "
            "max(click_to_agent_ms) AS worst_click_to_agent_ms, "
            "max(agent_write_ms) AS worst_agent_write_ms "
            "FROM workstation_print_job WHERE requested_at > now() - interval '24 hours'",
            fetch="one", operation="print_stats")

    # ------------------------------------------------------------ журнал заданий

    async def note_task(self, task: Task, *, screen_status: str | None = None,
                        session_id: str | None = None, station_id: str | None = None,
                        actor_id: str | None = None, picked: bool = False,
                        packed: bool = False, printed: bool = False,
                        handed: bool = False) -> None:
        await self.execute(
            "INSERT INTO workstation_task (task_id, owner_external_id, barcode, screen_status, "
            "session_id, station_id, actor_id, picked_at, packed_at, printed_at, handed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, "
            "CASE WHEN %s THEN now() END, CASE WHEN %s THEN now() END, "
            "CASE WHEN %s THEN now() END, CASE WHEN %s THEN now() END) "
            "ON CONFLICT (task_id) DO UPDATE SET "
            "screen_status = EXCLUDED.screen_status, "
            "session_id = COALESCE(EXCLUDED.session_id, workstation_task.session_id), "
            "station_id = COALESCE(EXCLUDED.station_id, workstation_task.station_id), "
            "actor_id = COALESCE(EXCLUDED.actor_id, workstation_task.actor_id), "
            "picked_at = COALESCE(workstation_task.picked_at, EXCLUDED.picked_at), "
            "packed_at = COALESCE(workstation_task.packed_at, EXCLUDED.packed_at), "
            "printed_at = COALESCE(workstation_task.printed_at, EXCLUDED.printed_at), "
            "handed_at = COALESCE(workstation_task.handed_at, EXCLUDED.handed_at), "
            "updated_at = now()",
            (task.task_id, task.owner_external_id, task.barcode,
             screen_status or task.screen.value, session_id, station_id, actor_id,
             picked, packed, printed, handed),
            operation="note_task")

    async def record_cancel(self, *, task_id: str, owner_external_id: str, barcode: str,
                            reason: str, reason_code: str, actor_id: str | None) -> None:
        """Отмена с причиной. База не примет её без причины — и это главное.

        Проверка стоит в схеме, а не в этом методе: код обходят, схему нет.
        """
        await self.execute(
            "INSERT INTO workstation_task (task_id, owner_external_id, barcode, screen_status, "
            "actor_id, cancelled_at, cancel_reason, cancel_reason_code) "
            "VALUES (%s, %s, %s, 'cancelled', %s, now(), %s, %s) "
            "ON CONFLICT (task_id) DO UPDATE SET screen_status = 'cancelled', "
            "cancelled_at = now(), cancel_reason = EXCLUDED.cancel_reason, "
            "cancel_reason_code = EXCLUDED.cancel_reason_code, "
            "actor_id = COALESCE(EXCLUDED.actor_id, workstation_task.actor_id), "
            "updated_at = now()",
            (task_id, owner_external_id, barcode, actor_id, reason, reason_code),
            operation="record_cancel")

    async def cancellations_without_reason(self) -> int:
        """Сколько отмен осталось без причины. Целевое значение — ноль, всегда.

        Схема такого не допустит; счётчик существует, чтобы это было видно на
        экране, а не только в надежде на constraint.
        """
        row = await self.execute(
            "SELECT count(*) AS n FROM workstation_task "
            "WHERE cancelled_at IS NOT NULL AND (cancel_reason IS NULL OR btrim(cancel_reason) = '')",
            fetch="one", operation="cancellations_without_reason")
        return int((row or {}).get("n") or 0)

    # ------------------------------------------------------------ принтеры станций

    async def upsert_printer(self, *, station_id: str, station_name: str,
                             printer_name: str | None = None,
                             transport: str = "agent") -> None:
        await self.execute(
            "INSERT INTO workstation_printer (station_id, station_name, printer_name, "
            "transport, last_seen_at) VALUES (%s, %s, %s, %s, now()) "
            "ON CONFLICT (station_id) DO UPDATE SET station_name = EXCLUDED.station_name, "
            "printer_name = COALESCE(EXCLUDED.printer_name, workstation_printer.printer_name), "
            "transport = EXCLUDED.transport, last_seen_at = now()",
            (station_id, station_name, printer_name, transport),
            operation="upsert_printer")

    async def record_probe(self, *, station_id: str, confirmed_format: str,
                           note: str | None = None) -> None:
        """Результат теста принтера — вопрос 2 раздела 13 мастера.

        Формат этикетки не решается в конфиге: он проверяется на живом
        принтере и записывается вместе с датой проверки.
        """
        await self.execute(
            "UPDATE workstation_printer SET confirmed_format = %s, probed_at = now(), "
            "probe_note = %s WHERE station_id = %s",
            (confirmed_format, note, station_id), operation="record_probe")

    async def printers(self) -> list[dict[str, Any]]:
        rows = await self.execute(
            "SELECT station_id, station_name, printer_name, transport, active, "
            "confirmed_format, probed_at, probe_note, last_seen_at "
            "FROM workstation_printer ORDER BY station_name",
            fetch="all", operation="printers")
        return list(rows or [])

    async def printer(self, station_id: str) -> dict[str, Any] | None:
        return await self.execute(
            "SELECT station_id, station_name, printer_name, transport, active, "
            "confirmed_format, probed_at, probe_note, last_seen_at "
            "FROM workstation_printer WHERE station_id = %s",
            (station_id,), fetch="one", operation="printer")
