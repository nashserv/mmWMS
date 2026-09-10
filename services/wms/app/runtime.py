"""Снимок состояния склада для метрик Prometheus.

Метрики читаются из базы, а не из памяти процесса: воркеров у сервиса
несколько, и «сколько заданий в подборе» — это факт склада, а не факт одного
процесса. Молчащий воркер считается сломанным (инвариант 14), поэтому здесь же
считается возраст самого старого неопубликованного события.

Опрос идёт с кэшем: /metrics дёргает Prometheus раз в несколько секунд, а
семь агрегатов по базе на каждый скрейп — это лишняя нагрузка ради цифры,
которая всё равно меняется медленнее интервала опроса.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from .postgres import ConnectionPool, transaction

CACHE_SECONDS = 5.0


class RuntimeMetrics:
    def __init__(self, pool: ConnectionPool, *, cache_seconds: float = CACHE_SECONDS) -> None:
        self._pool = pool
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = {}
        self._taken_at = 0.0

    def ready(self) -> bool:
        return self._pool.healthy()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._snapshot and time.monotonic() - self._taken_at < self._cache_seconds:
                return self._snapshot
        fresh = self._collect()
        with self._lock:
            self._snapshot = fresh
            self._taken_at = time.monotonic()
        return fresh

    def _collect(self) -> dict[str, Any]:
        with self._pool.connection(timeout=5.0) as connection:
            with transaction(connection) as cursor:
                cursor.execute("SELECT state, count(*) AS n FROM wms_task GROUP BY state")
                by_state = {row["state"]: int(row["n"]) for row in cursor.fetchall()}

                cursor.execute(
                    "SELECT count(*) AS n FROM wb_label WHERE invalidated_at IS NULL")
                labels_ready = int((cursor.fetchone() or {"n": 0})["n"])

                cursor.execute(
                    "SELECT count(*) AS pending, "
                    "       COALESCE(EXTRACT(EPOCH FROM now() - MIN(occurred_at)), 0) AS oldest "
                    "  FROM outbox WHERE published_at IS NULL")
                outbox = cursor.fetchone() or {"pending": 0, "oldest": 0}

                cursor.execute(
                    "SELECT count(*) AS n FROM discrepancy "
                    " WHERE kind = 'ledger_short' AND decision = 'pending'")
                shortfalls = int((cursor.fetchone() or {"n": 0})["n"])

        return {
            "tasks_by_state": by_state,
            "labels_ready": labels_ready,
            "outbox_pending": int(outbox["pending"]),
            "outbox_oldest_seconds": float(outbox["oldest"] or 0),
            "ledger_short_open": shortfalls,
        }
