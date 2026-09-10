"""Подключение к базе billing.

Маленький собственный пул вместо psycopg_pool: раздел 2.2 мастера замораживает
набор зависимостей платформы, и тянуть новый пакет ради тридцати строк — это
менять стек ради удобства. Нагрузка биллинга — события склада, а не запросы
людей: десяток соединений с запасом.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row


class Database:
    def __init__(self, url: str, *, size: int = 8) -> None:
        self._url = url
        self._size = size
        self._free: list[psycopg.Connection] = []
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return self._url

    def _open(self) -> psycopg.Connection:
        return psycopg.connect(self._url, row_factory=dict_row, autocommit=False,
                               connect_timeout=10)

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        with self._lock:
            connection = self._free.pop() if self._free else None
        if connection is None or connection.closed:
            connection = self._open()
        try:
            yield connection
        except Exception:
            # Соединение с незакрытой транзакцией нельзя отдавать следующему:
            # он унаследует чужой ROLLBACK-состояние и упадёт на первом запросе.
            try:
                connection.rollback()
            except Exception:
                connection.close()
            raise
        finally:
            if not connection.closed:
                with self._lock:
                    if len(self._free) < self._size:
                        self._free.append(connection)
                    else:
                        connection.close()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Cursor]:
        """Одна транзакция — один курсор.

        Ни одного HTTP-вызова внутри (инвариант 2): начисление считается по
        данным, уже прочитанным из базы, и записывается одним коммитом.
        """
        with self.connection() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    yield cursor

    @contextmanager
    def cursor(self) -> Iterator[psycopg.Cursor]:
        """Чтение без записи: commit после, чтобы не держать транзакцию открытой."""
        with self.connection() as connection:
            with connection.cursor() as handle:
                yield handle
            connection.rollback()

    def ready(self) -> bool:
        try:
            with self.cursor() as handle:
                handle.execute("SELECT 1")
                return True
        except Exception:
            return False

    def close(self) -> None:
        with self._lock:
            while self._free:
                self._free.pop().close()
