"""Инструменты полного прогона: HTTP-клиент, база, шина, наблюдение за транзакциями.

Отдельный модуль, а не conftest, по одной причине: сами проверки раздела 9.6
должны читаться как сценарий склада, а не как возня с сокетами. Всё, что не
является утверждением о складе, живёт здесь.

Прогон идёт против поднятого стенда (приложение F мастера), а не против кода в
процессе: между потоками A, B и C проверяется именно стык, а он существует
только по HTTP, в базе и на шине.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, NoReturn
from urllib.parse import urlparse

import httpx

BASE_PATH = "/api/mmx/wms/v1"


class NotReady(AssertionError):
    """Проверка не выполнена, потому что нужного ещё нет на стенде.

    Наследуется от AssertionError сознательно: красное обязано остаться
    красным (пункт 9 файла 01). Отдельный тип нужен только затем, чтобы отчёт
    мог показать одной строкой, чего именно не хватает, вместо простыни
    трассировки.
    """


def not_ready(what: str) -> NoReturn:
    raise NotReady(what)


def env(name: str, default: str | None = None) -> str:
    """Переменная окружения или честное падение.

    Молча пропустить проверку нельзя: пропуск выглядит как успех, а полный
    прогон существует ровно для того, чтобы показывать, что не работает.
    """
    value = os.getenv(name) or default
    if not value:
        not_ready(
            f"не задана переменная окружения {name}; прогон идёт против стенда, "
            f"адрес брать неоткуда"
        )
    return value


# --------------------------------------------------------------------- HTTP


@dataclass
class Call:
    """Один вызов контракта: и результат, и то, сколько он занял."""

    path: str
    status_code: int
    body: dict[str, Any]
    elapsed_ms: float

    @property
    def result(self) -> dict[str, Any]:
        return self.body.get("result") or {}

    @property
    def error(self) -> dict[str, Any] | None:
        """Ошибка транспорта JSON-RPC (не путать с error_code внутри result)."""
        return self.body.get("error")

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and self.error is None


class Wms:
    """Клиент контракта `/api/mmx/wms/v1` — все вызовы POST, конверт JSON-RPC."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)
        self._id = 0

    def close(self) -> None:
        self._client.close()

    def call(self, path: str, params: dict[str, Any] | None = None) -> Call:
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": "call", "params": params or {}, "id": self._id}
        started = time.perf_counter()
        try:
            response = self._client.post(f"{self.base_url}{BASE_PATH}{path}", json=payload)
        except httpx.HTTPError as failure:
            not_ready(f"сервис wms не отвечает по {self.base_url}: {failure}")
        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = {"error": {"message": response.text[:200]}}
        return Call(path=path, status_code=response.status_code, body=body,
                    elapsed_ms=elapsed_ms)

    def result(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Вызов, от которого ждём успеха: несостоявшийся — сразу красный."""
        call = self.call(path, params)
        if not call.ok:
            not_ready(f"{path} ответил {call.status_code} / {call.error}: маршрут не реализован")
        return call.result


# --------------------------------------------------------------------- база


class Db:
    """Прямое чтение базы `wms`.

    Часть утверждений раздела 9.6 сформулирована про таблицы (`stock_balance`,
    `stock_move`, `discrepancy`), а не про ответы API. Проверять их через API
    значило бы верить сервису на слово ровно там, где он и ошибается.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._connection: Any = None

    def _connect(self) -> Any:
        import psycopg
        from psycopg.rows import dict_row

        if self._connection is None or self._connection.closed:
            try:
                self._connection = psycopg.connect(self.url, autocommit=True,
                                                   row_factory=dict_row, connect_timeout=10)
            except Exception as failure:  # noqa: BLE001 — сообщение важнее типа
                not_ready(f"база по DATABASE_URL недоступна: {failure}")
        return self._connection

    def close(self) -> None:
        if self._connection is not None and not self._connection.closed:
            self._connection.close()

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                return list(cursor.fetchall())
        except Exception as failure:  # noqa: BLE001
            not_ready(f"запрос к базе не выполнился ({failure}); схема потока 0 применена?")

    def row(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        found = self.rows(sql, params)
        return found[0] if found else None

    def value(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.row(sql, params)
        if row is None:
            return None
        return next(iter(row.values()))

    def spawn(self) -> "Db":
        """Отдельное соединение — наблюдателю нужно своё, чужое он заблокирует."""
        return Db(self.url)

    # --- запросы, которые повторяются в нескольких шагах ------------------

    def owner_id(self, seller_external_id: str) -> str | None:
        return self.value("SELECT id FROM owner WHERE seller_external_id = %s",
                          (seller_external_id,))

    def sku_id(self, seller_external_id: str, barcode: str) -> str | None:
        return self.value(
            "SELECT s.id FROM sku s JOIN owner o ON o.id = s.owner_id "
            "WHERE o.seller_external_id = %s AND s.barcode = %s",
            (seller_external_id, barcode))

    def balance(self, seller_external_id: str, barcode: str, state: str = "good") -> int:
        """Остаток по проекции `stock_balance` — единственному месту, где он живёт."""
        return int(self.value(
            "SELECT COALESCE(SUM(b.qty), 0) AS qty FROM stock_balance b "
            "JOIN owner o ON o.id = b.owner_id JOIN sku s ON s.id = b.sku_id "
            "WHERE o.seller_external_id = %s AND s.barcode = %s AND b.state = %s",
            (seller_external_id, barcode, state)) or 0)

    def moves(self, seller_external_id: str, barcode: str,
              doc_type: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT m.* FROM stock_move m "
               "JOIN owner o ON o.id = m.owner_id JOIN sku s ON s.id = m.sku_id "
               "WHERE o.seller_external_id = %s AND s.barcode = %s")
        params: list[Any] = [seller_external_id, barcode]
        if doc_type:
            sql += " AND m.doc_type = %s"
            params.append(doc_type)
        return self.rows(sql + " ORDER BY m.id", params)

    def task_by_order(self, wb_order_id: int) -> dict[str, Any] | None:
        return self.row("SELECT * FROM wms_task WHERE wb_order_id = %s", (wb_order_id,))


# ------------------------------------------------ наблюдение за транзакциями


@dataclass
class TxnSample:
    state: str
    age_ms: float
    wait_event_type: str | None
    query: str


class TxnWatch:
    """Наблюдатель за транзакциями сервиса — «трейс», доступный со стенда.

    Шаг 4 требует доказать, что внутри транзакции нет HTTP-вызовов. Отпечаток
    такого вызова в Postgres однозначен: бэкенд сидит `idle in transaction` и
    ждёт клиента, пока тот разговаривает с Wildberries. Плюс возраст самой
    транзакции: раздел 6.2 отводит ей 2–5 мс, вызов в WB — 500 мс.

    Это не замена трассировке OpenTelemetry: когда поток A её добавит,
    проверку можно будет ужесточить до «в спане транзакции нет дочерних
    HTTP-спанов». До тех пор наблюдение по pg_stat_activity — настоящая
    проверка того же самого факта, а не имитация.
    """

    SQL = """
        SELECT state,
               EXTRACT(EPOCH FROM (now() - xact_start)) * 1000 AS age_ms,
               wait_event_type,
               left(coalesce(query, ''), 200) AS query
          FROM pg_stat_activity
         WHERE datname = current_database()
           AND pid <> pg_backend_pid()
           AND xact_start IS NOT NULL
    """

    def __init__(self, db: Db, interval_s: float = 0.005) -> None:
        self._db = db.spawn()
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples: list[TxnSample] = []
        self.error: str | None = None

    def __enter__(self) -> "TxnWatch":
        self._thread = threading.Thread(target=self._loop, name="txn-watch", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._db.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for row in self._db.rows(self.SQL):
                    self.samples.append(TxnSample(
                        state=str(row.get("state") or ""),
                        age_ms=float(row.get("age_ms") or 0.0),
                        wait_event_type=row.get("wait_event_type"),
                        query=str(row.get("query") or "")))
            except Exception as failure:  # noqa: BLE001 — наблюдатель не роняет прогон
                self.error = str(failure)
                return
            self._stop.wait(self._interval)

    # --- то, ради чего наблюдатель заводится ------------------------------

    @property
    def seen(self) -> int:
        return len(self.samples)

    @property
    def idle_in_transaction(self) -> list[TxnSample]:
        """Транзакция открыта, а бэкенд ждёт клиента — отпечаток HTTP внутри неё."""
        return [s for s in self.samples if s.state == "idle in transaction"]

    @property
    def max_age_ms(self) -> float:
        return max((s.age_ms for s in self.samples), default=0.0)

    def percentile_age_ms(self, percent: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(s.age_ms for s in self.samples)
        index = min(len(ordered) - 1, int(round(percent / 100 * (len(ordered) - 1))))
        return ordered[index]


# --------------------------------------------------------------------- шина


class Bus:
    """Слушатель `mmx.events`.

    События читаются с шины, а не из служебного окна mock: потоки B и C
    подписаны именно на обменник, и проверять надо то, что увидят они.
    Отсутствие брокера прогон не роняет — раздел 6.1 прямо требует, чтобы
    склад работал без шины, и шаг 14 это проверяет.
    """

    def __init__(self, url: str, exchange: str = "mmx.events") -> None:
        self.url = url
        self.exchange = exchange
        self.events: list[dict[str, Any]] = []
        self.error: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "Bus":
        self._thread = threading.Thread(target=self._loop, name="bus-tap", daemon=True)
        self._thread.start()
        # Даём подписке встать до первого действия склада: событие, вылетевшее
        # раньше подписки, потеряно навсегда и превратится в ложный красный.
        time.sleep(1.0)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._consume()
            except Exception as failure:  # noqa: BLE001
                self.error = str(failure)
                # Брокер могли выключить намеренно (шаг 14) — ждём и пробуем снова.
                self._stop.wait(2.0)

    def _consume(self) -> None:
        import pika

        connection = pika.BlockingConnection(pika.URLParameters(self.url))
        try:
            channel = connection.channel()
            channel.exchange_declare(exchange=self.exchange, exchange_type="topic", durable=True)
            queue = channel.queue_declare(queue="", exclusive=True, auto_delete=True).method.queue
            channel.queue_bind(exchange=self.exchange, queue=queue, routing_key="#")
            self.error = None
            while not self._stop.is_set():
                _, _, body = channel.basic_get(queue=queue, auto_ack=True)
                if body is None:
                    connection.sleep(0.05)
                    continue
                try:
                    event = json.loads(body)
                except json.JSONDecodeError:
                    continue
                with self._lock:
                    self.events.append(event)
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    def collected(self, event_type: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self.events)
        if event_type is None:
            return events
        return [event for event in events if event.get("type") == event_type]

    def wait_for(self, predicate: Callable[[dict[str, Any]], bool],
                 timeout_s: float = 10.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for event in self.collected():
                if predicate(event):
                    return event
            time.sleep(0.1)
        return None

    def require_connected(self) -> None:
        if self.error:
            not_ready(f"шина {self.exchange} недоступна ({self.error}); "
                      f"события проверить нечем")


# ------------------------------------------------------------- вспомогательное


def wait_until(check: Callable[[], Any], timeout_s: float, interval_s: float = 0.05) -> Any:
    """Ждёт истинного значения и возвращает его; иначе — None.

    Возвращает именно значение, а не флаг: почти всем шагам нужен не факт
    появления, а само появившееся (задание, событие, строка).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval_s)


def port_is_open(url: str, timeout_s: float = 1.0) -> bool:
    """Жив ли порт по URL. Нужен шагу 14: брокер обязан быть действительно выключен."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (5671 if parsed.scheme == "amqps" else 5672)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


@dataclass
class LoadResult:
    """Итог нагрузочного шага: не только скорость, но и чем за неё заплатили."""

    accepted: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def per_hour(self) -> float:
        return self.accepted / self.seconds * 3600 if self.seconds else 0.0
