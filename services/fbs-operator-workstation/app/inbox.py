"""Событийный консьюмер — ускоритель экрана, а не источник истины.

Он остаётся в системе (`workstation-inbox-consumer` жил здесь и раньше), но у
него отобрана единственная опасная обязанность: приносить данные. Теперь
событие приносит только повод — «в wms что-то изменилось, сходи посмотри
раньше срока».

Из этого следует всё остальное:

- потерянное событие стоит одного интервала опроса, а не потерянного заказа;
- задвоенное событие стоит одного лишнего опроса;
- событие с неправильным payload не может испортить экран, потому что payload
  никто не читает;
- выключенный RabbitMQ не останавливает смену (пункт 14 полного прогона).

Консьюмер работает в отдельном потоке: pika блокирующая, а событийный цикл
рабочего места держит опрос и push агентам печати, и блокировать его нельзя.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from . import metrics

logger = logging.getLogger("workstation.inbox")

WORKER = "inbox_consumer"

# Что считается поводом сходить за заданиями. Список нарочно широкий: цена
# ошибки — один лишний опрос, цена пропуска — задание, которое сборщик увидит
# на секунду позже.
INTERESTING_PREFIXES = ("wms.", "wb.fbs", "wb.supply", "inventory.", "task.", "order.")

QUEUE = "workstation.inbox"


class InboxConsumer:
    """Подписка на `mmx.events`. Живёт своей жизнью и никого не держит."""

    def __init__(self, url: str | None, exchange: str, *,
                 on_event: Callable[[str], None],
                 loop: asyncio.AbstractEventLoop | None = None,
                 reconnect_seconds: float = 5.0) -> None:
        self._url = url
        self._exchange = exchange
        self._on_event = on_event
        self._loop = loop
        self._reconnect = reconnect_seconds
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._connection: Any = None
        self.events_seen = 0
        self.connected = False
        self.last_error: str | None = None

    # ------------------------------------------------------------ жизненный цикл

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or self._loop or asyncio.get_event_loop()
        if not self._url:
            # Законное состояние, а не ошибка: склад от шины не зависит.
            logger.info("RABBITMQ_URL не задан — рабочее место работает только опросом")
            metrics.BUS_CONNECTED.set(0)
            metrics.worker_beat(WORKER, units=0)
            return
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="workstation-inbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 — закрываем на выходе, ошибка тут ничего не меняет
                pass
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5.0)
        self.connected = False
        metrics.BUS_CONNECTED.set(0)
        metrics.worker_stopped(WORKER)

    # ------------------------------------------------------------ цикл

    def _run(self) -> None:
        metrics.worker_beat(WORKER, units=0)
        while not self._stopping.is_set():
            try:
                self._consume()
            except Exception as error:  # noqa: BLE001 — брокер падает, смена продолжается
                self.connected = False
                self.last_error = f"{type(error).__name__}: {error}"
                metrics.BUS_CONNECTED.set(0)
                metrics.WORKER_ERRORS.labels(worker=WORKER).inc()
                logger.warning("шина недоступна (%s) — работаем опросом", self.last_error)
            # Пауза перед переподключением. Без неё падающий брокер превращается
            # в горячий цикл, который сожжёт процессор ровно тогда, когда всё
            # остальное и так плохо.
            self._stopping.wait(self._reconnect)
        metrics.worker_stopped(WORKER)

    def _consume(self) -> None:
        import pika  # локальный импорт: без брокера сервис обязан работать

        parameters = pika.URLParameters(str(self._url))
        parameters.heartbeat = 30
        parameters.blocked_connection_timeout = 30
        self._connection = pika.BlockingConnection(parameters)
        try:
            channel = self._connection.channel()
            channel.exchange_declare(exchange=self._exchange, exchange_type="topic", durable=True)
            # Границы очереди обязательны. Событие для рабочего места — это
            # повод сходить за данными, и живёт оно секунды: опрос всё равно
            # идёт раз в секунду. Без границ очередь копится, пока рабочее
            # место лежит, и после подъёма оно разбирает вчерашние поводы,
            # а брокер к тому времени упирается в диск.
            channel.queue_declare(queue=QUEUE, durable=True, arguments={
                "x-message-ttl": 60_000,      # минута: дольше повод не нужен
                "x-max-length": 1_000,        # тысячи поводов хватит на любой всплеск
                "x-overflow": "drop-head",    # выбрасываем старые, а не отказываем издателю
            })
            channel.queue_bind(queue=QUEUE, exchange=self._exchange, routing_key="#")
            # Небольшой prefetch: сообщения тут ничего не стоят, но и копить их
            # незачем — важен факт «что-то произошло», а не глубина очереди.
            channel.basic_qos(prefetch_count=32)
            self.connected = True
            self.last_error = None
            metrics.BUS_CONNECTED.set(1)
            logger.info("шина подключена, очередь %s", QUEUE)

            for method, _properties, _body in channel.consume(QUEUE, inactivity_timeout=1.0):
                if self._stopping.is_set():
                    break
                if method is None:
                    # Тишина в очереди — не повод считать воркер мёртвым, но и
                    # не повод считать, что он работал. units=0 (инвариант 14).
                    metrics.worker_beat(WORKER, units=0)
                    continue
                self._handle(str(method.routing_key or ""))
                channel.basic_ack(method.delivery_tag)
        finally:
            self.connected = False
            metrics.BUS_CONNECTED.set(0)
            try:
                if self._connection and self._connection.is_open:
                    self._connection.close()
            except Exception:  # noqa: BLE001
                pass
            self._connection = None

    def _handle(self, routing_key: str) -> None:
        """Единственное действие на событие: разбудить опрос.

        Payload не читается вообще. Это не экономия — это отказ от второго
        писателя в проекцию: пока данные приходят двумя путями, они однажды
        разойдутся.
        """
        self.events_seen += 1
        metrics.worker_beat(WORKER, units=1)
        if not routing_key.startswith(INTERESTING_PREFIXES):
            metrics.BUS_NUDGES.labels(outcome="ignored").inc()
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            metrics.BUS_NUDGES.labels(outcome="no_loop").inc()
            return
        try:
            loop.call_soon_threadsafe(self._on_event, "bus")
        except RuntimeError:
            metrics.BUS_NUDGES.labels(outcome="no_loop").inc()

    def status(self) -> dict[str, Any]:
        return {
            "configured": bool(self._url),
            "connected": self.connected,
            "events_seen": self.events_seen,
            "last_error": self.last_error,
            # Главное, что здесь стоит прочитать глазами: шина не обязательна.
            "note": "шина ускоряет экран; источник истины — опрос /tasks/pull",
        }


class NullConsumer:
    """Заглушка для окружений без брокера — чтобы не проверять None на каждом шагу."""

    connected = False
    events_seen = 0
    last_error = None

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        metrics.BUS_CONNECTED.set(0)

    def stop(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {"configured": False, "connected": False, "events_seen": 0,
                "note": "шина не настроена; рабочее место работает опросом"}


def uptime_seconds(started_at: float) -> float:
    return max(0.0, time.time() - started_at)
