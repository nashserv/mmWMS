"""billing-inbox-consumer: события шины → начисления.

Инвариант 14 мастера: молчащий воркер считается сломанным. На проде четыре
воркера стоят с нулём строк лога при зелёном healthcheck — поэтому здесь есть
и счётчик обработанного, и собственный /metrics, и явный отказ вместо тихого
проглатывания.

Событие — уведомление, а не способ доставки (раздел 6.1): деньги считаются по
событиям, но склад от этого консьюмера не зависит. Его падение не
останавливает ни приёмку, ни подбор.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any

import pika
from prometheus_client import start_http_server

from .config import app_environment, database_url, events_exchange
from .db import Database
from .metrics import WORKER_PROCESSED
from .service import BillingService

QUEUE = os.getenv("BILLING_QUEUE", "billing.events")
DEAD_LETTER_QUEUE = os.getenv("BILLING_DEAD_LETTER_QUEUE", "stand.dead-letters")
# Слушаем всё: что тарифицировать, решает таблица billing_billable_event, а не
# привязка очереди. Иначе включение новой услуги в админке требовало бы
# перезапуска воркера — и услуга молча оставалась бы бесплатной.
ROUTING_KEY = os.getenv("BILLING_ROUTING_KEY", "#")

# Ретеншен inbox: разобранные и нетарифицируемые события не хранятся вечно.
# На проде integration_outbox — 136 210 записей без ретеншена (раздел 3.6).
RETENTION_DAYS = int(os.getenv("BILLING_INBOX_RETENTION_DAYS", "30"))

log = logging.getLogger("billing.consumer")


class Consumer:
    def __init__(self, url: str, service: BillingService) -> None:
        self._url = url
        self._service = service
        self._stopping = threading.Event()

    def stop(self, *_: Any) -> None:
        self._stopping.set()

    def run(self) -> None:
        while not self._stopping.is_set():
            try:
                self._consume()
            except Exception as failure:  # noqa: BLE001 — воркер обязан пережить брокер
                log.warning("шина недоступна (%s), повтор через 5 с", failure)
                self._stopping.wait(5.0)

    def _consume(self) -> None:
        connection = pika.BlockingConnection(pika.URLParameters(self._url))
        channel = connection.channel()
        channel.exchange_declare(exchange=events_exchange(), exchange_type="topic", durable=True)
        channel.queue_declare(queue=QUEUE, durable=True, arguments={
            # Событие, которое не удалось обработать даже после повтора, уходит
            # в мёртвые письма, а не пропадает. На проде dead_letters пуст при
            # регулярных потерях заданий (раздел 3.5) — так выглядит потеря.
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": DEAD_LETTER_QUEUE,
        })
        channel.queue_bind(exchange=events_exchange(), queue=QUEUE, routing_key=ROUTING_KEY)
        channel.basic_qos(prefetch_count=32)
        log.info("слушаю %s ← %s (%s)", QUEUE, events_exchange(), ROUTING_KEY)

        for method, _properties, body in channel.consume(QUEUE, inactivity_timeout=1.0):
            if self._stopping.is_set():
                break
            if method is None:
                continue
            try:
                event = json.loads(body.decode("utf-8"))
            except Exception:
                # Не JSON — переигрывать нечего, разбирать человеку.
                channel.basic_nack(method.delivery_tag, requeue=False)
                continue
            try:
                result = self._service.ingest(event)
                channel.basic_ack(method.delivery_tag)
                if result.get("outcome") == "accrued":
                    log.info("начислено %s по %s", result.get("amount"), result.get("service"))
                elif result.get("outcome") == "unbilled":
                    log.warning("не начислено: %s (%s)", result.get("reason"),
                                result.get("detail"))
            except Exception as failure:  # noqa: BLE001
                # Ошибка обработчика: событие уходит в мёртвые письма с
                # сохранённым следом в billing_inbox. Тихого ack здесь быть не
                # может — это ровно та потеря выручки, которую чиним.
                log.exception("обработка события упала: %s", failure)
                channel.basic_nack(method.delivery_tag, requeue=False)

        try:
            channel.cancel()
            connection.close()
        except Exception:
            pass


def sweep(database: Database, stopping: threading.Event) -> None:
    """Ретеншен inbox. Разобранное не хранится вечно."""
    while not stopping.wait(3600.0):
        try:
            with database.transaction() as cursor:
                cursor.execute(
                    "DELETE FROM billing_inbox WHERE processed_at < now() - %s::interval "
                    "AND outcome IN ('accrued', 'skipped')",
                    (f"{RETENTION_DAYS} days",))
        except Exception as failure:  # noqa: BLE001
            log.warning("ретеншен inbox не сработал: %s", failure)


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app_environment()
    url = os.getenv("RABBITMQ_URL", "")
    if not url:
        log.error("RABBITMQ_URL не задан: консьюмеру нечего слушать")
        return 2

    database = Database(database_url())
    service = BillingService(database)
    consumer = Consumer(url, service)

    # Собственный /metrics: воркер обязан уметь доказать, что он работает.
    start_http_server(int(os.getenv("METRICS_PORT", "8081")))
    WORKER_PROCESSED.labels(worker="inbox").inc(0)

    stopping = threading.Event()
    threading.Thread(target=sweep, args=(database, stopping), daemon=True).start()
    signal.signal(signal.SIGTERM, consumer.stop)
    signal.signal(signal.SIGINT, consumer.stop)

    started = time.monotonic()
    consumer.run()
    stopping.set()
    log.info("остановлен после %.0f с работы", time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
