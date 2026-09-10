"""Соединения с Postgres и повтор при конкуренции за строку остатка.

Пул написан здесь, а не взят из psycopg_pool: набор зависимостей заморожен
разделом 2.2 мастера, и новая библиотека ради двадцати строк — это лишний
пакет в образе и лишний повод разойтись с остальными девятью сервисами.

Транзакция резерва (раздел 6.2) обязана быть короткой: только Postgres,
2–5 мс, ни одного HTTP-вызова внутри (инвариант 2). Всё, что связано с
Wildberries, уезжает за COMMIT.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

# Конкуренция за строку остатка — не ошибка данных, а нормальная жизнь склада:
# десять писателей (пять сборщиков и пять приёмщиков, раздел 4) бьют в один
# owner × sku. Postgres отвечает на это своими кодами, и различать их надо
# по коду, а не по тексту сообщения.
RETRYABLE_SQLSTATES = frozenset({
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
})


class PoolClosed(RuntimeError):
    """Пул остановлен, соединение выдать нельзя."""


class ConnectionPool:
    """Ограниченный пул синхронных соединений.

    Ограничение размера — не экономия, а защита базы: восемь клиентов
    нагрузочного шага (шаг 16 прогона) не должны превратиться в восемьдесят
    backend-процессов, каждый со своей блокировкой.
    """

    def __init__(self, dsn: str, *, max_size: int = 10, connect_timeout: int = 5) -> None:
        self._dsn = dsn
        self._max_size = max(1, max_size)
        self._connect_timeout = connect_timeout
        self._idle: list[psycopg.Connection] = []
        self._leased = 0
        self._closed = False
        # Condition, а не Semaphore: ждущий обязан просыпаться и при возврате
        # соединения, и при закрытии пула, иначе остановка сервиса повиснет.
        self._cond = threading.Condition(threading.Lock())

    def _new_connection(self) -> psycopg.Connection:
        connection = psycopg.connect(
            self._dsn, autocommit=False, row_factory=dict_row,
            connect_timeout=self._connect_timeout)
        # Идентификатор приложения виден в pg_stat_activity — по нему прогон
        # отличает наши транзакции от чужих (шаги 4 и 16).
        with connection.cursor() as cursor:
            cursor.execute("SET application_name = 'wms'")
        connection.commit()
        return connection

    @contextmanager
    def connection(self, *, timeout: float = 30.0) -> Iterator[psycopg.Connection]:
        connection = self._acquire(timeout)
        try:
            yield connection
        except BaseException:
            self._discard_or_rollback(connection)
            raise
        else:
            self._release(connection)

    def _acquire(self, timeout: float) -> psycopg.Connection:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if self._closed:
                    raise PoolClosed("пул соединений остановлен")
                if self._idle:
                    connection = self._idle.pop()
                    self._leased += 1
                    break
                if self._leased < self._max_size:
                    self._leased += 1
                    connection = None  # type: ignore[assignment]
                    break
                if not self._cond.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("нет свободных соединений с базой")
        if connection is None:
            try:
                return self._new_connection()
            except BaseException:
                # Не удалось подключиться — место в пуле обязано освободиться,
                # иначе после недоступности базы пул останется «занятым» навсегда.
                with self._cond:
                    self._leased -= 1
                    self._cond.notify()
                raise
        if connection.closed:
            with self._cond:
                self._leased -= 1
                self._cond.notify()
            return self._acquire(max(0.0, deadline - time.monotonic()))
        return connection

    def _release(self, connection: psycopg.Connection) -> None:
        try:
            # Соединение возвращается чистым: незакрытая транзакция оставила бы
            # следующего арендатора внутри чужой, а бэкенд — в idle in transaction,
            # том самом состоянии, которое прогон считает HTTP-вызовом внутри
            # транзакции (шаг 4).
            if connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                connection.rollback()
        except Exception:
            self._forget(connection)
            return
        with self._cond:
            self._leased -= 1
            if self._closed or connection.closed:
                self._close_quietly(connection)
            else:
                self._idle.append(connection)
            self._cond.notify()

    def _discard_or_rollback(self, connection: psycopg.Connection) -> None:
        try:
            connection.rollback()
        except Exception:
            self._forget(connection)
            return
        self._release(connection)

    def _forget(self, connection: psycopg.Connection) -> None:
        self._close_quietly(connection)
        with self._cond:
            self._leased -= 1
            self._cond.notify()

    @staticmethod
    def _close_quietly(connection: psycopg.Connection) -> None:
        try:
            connection.close()
        except Exception:
            pass

    def close(self) -> None:
        with self._cond:
            self._closed = True
            idle, self._idle = self._idle, []
            self._cond.notify_all()
        for connection in idle:
            self._close_quietly(connection)

    def healthy(self) -> bool:
        """Живая проверка для /readyz: база отвечает на запрос, а не «порт открыт»."""
        try:
            with self.connection(timeout=5.0) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                connection.rollback()
            return True
        except Exception:
            return False


def database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is required for the real wms service")
    return value


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def pool() -> ConnectionPool:
    """Единый пул процесса. Воркеры создают свой — у них свой жизненный цикл."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                database_url(), max_size=int(os.getenv("WMS_DB_POOL_SIZE", "10")))
        return _pool


def reset_pool() -> None:
    """Закрывает пул. Нужен тестам и корректной остановке сервиса."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = None


def is_retryable(error: BaseException) -> bool:
    sqlstate = getattr(error, "sqlstate", None)
    return sqlstate in RETRYABLE_SQLSTATES


@contextmanager
def single(connection: psycopg.Connection) -> Iterator[psycopg.Cursor]:
    """Один запрос — без транзакции вовсе.

    Postgres и так выполняет одиночный запрос атомарно, а явные BEGIN/COMMIT
    вокруг него дают лишний круг до сервера и промежуток, в котором бэкенд
    сидит `idle in transaction`, ожидая COMMIT. Для фоновых воркеров, которые
    делают такие запросы десятками в секунду, это ровно тот шум, по которому
    потом невозможно отличить настоящую долгую транзакцию от рабочей.

    Многошаговые операции сюда не относятся: у резерва (раздел 6.2) транзакция
    обязана быть явной и общей на все пять шагов.
    """
    previous = connection.autocommit
    connection.autocommit = True
    cursor = connection.cursor()
    try:
        yield cursor
    finally:
        cursor.close()
        connection.autocommit = previous


@contextmanager
def transaction(connection: psycopg.Connection) -> Iterator[psycopg.Cursor]:
    """Одна транзакция — один курсор.

    Явный BEGIN/COMMIT, а не autocommit: раздел 6.2 требует, чтобы задание,
    резерв, движение и событие ложились одним коммитом либо не ложились вовсе.
    """
    cursor = connection.cursor()
    try:
        yield cursor
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        cursor.close()
