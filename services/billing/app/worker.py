"""billing-worker: хранение по суткам и публикация собственных событий.

Две работы, которых у биллинга не было и без которых он неполон.

**Хранение.** Единица — коробко-место × сутки (файл 04), значит начисление
приходит не событием, а кроном. Сегодня хранение не тарифицируется вовсе, хотя
это самый предсказуемый доход фулфилмента: товар лежит независимо от того,
заказали его или нет.

**Outbox.** Начисление и событие о нём пишутся одной транзакцией, но событие
кто-то должен вынести на шину. Без этого `billing_outbox` растёт молча — ровно
как `integration_outbox` на проде: 136 210 записей без ретеншена (раздел 3.6).

Инвариант 14: молчащий воркер считается сломанным, поэтому у обеих работ свой
счётчик и свой /metrics.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
from prometheus_client import start_http_server

from .config import app_environment, database_url, events_exchange, tenant_id
from .domain import today
from .db import Database
from .metrics import WORKER_PROCESSED
from .service import BillingService

log = logging.getLogger("billing.worker")

# Догоняем несколько суток назад, а не только вчера: воркер, простоявший
# выходные, обязан досчитать хранение сам, а не оставить дыру в счёте.
STORAGE_BACKFILL_DAYS = int(os.getenv("BILLING_STORAGE_BACKFILL_DAYS", "3"))
STORAGE_INTERVAL = float(os.getenv("BILLING_STORAGE_INTERVAL_SECONDS", "3600"))
OUTBOX_INTERVAL = float(os.getenv("BILLING_OUTBOX_INTERVAL_SECONDS", "5"))
OUTBOX_BATCH = int(os.getenv("BILLING_OUTBOX_BATCH", "100"))


def box_places(seller_external_id: str, day: date) -> dict[str, Any]:
    """Сколько коробо-мест занимал клиент НА КОНЕЦ ЭТИХ СУТОК — по данным склада.

    Вызов в `wms` идёт снаружи транзакции (инвариант 2).

    День здесь не для красоты. Хранение начисляется за прошедшие сутки и
    досчитывается за пропущенные дни (`STORAGE_BACKFILL_DAYS`). Раньше склад
    спрашивали без дня — и досчёт за три дня брал СЕГОДНЯШНИЙ остаток трижды,
    то есть начислял хранение за дни, когда товара ещё не было.

    Считает склад, а не мы: «сколько места занимает товар» — складской факт.
    Возвращается и число мест, и сколько товара посчитать не удалось за
    отсутствием нормы: придумывать количество и ставить его в счёт нельзя.
    """
    base = os.getenv("WMS_BASE_URL", "http://wms:8080").rstrip("/")
    path = os.getenv("WMS_API_PATH", "/api/mmx/wms/v1")
    # Склад спрашивает, кто пришёл (находка 1.3), и соседний сервис
    # представляется сервисным токеном. Без него запрос возвращает 401 — а
    # выглядит это как «у клиента ноль коробко-мест», потому что отказ
    # считался отсутствием количества.
    headers = {}
    token = (os.getenv("SERVICE_TOKEN") or "").strip()
    if token:
        headers["authorization"] = f"Bearer {token}"
    response = httpx.post(f"{base}{path}/storage/places", timeout=15.0,
                          headers=headers, json={
        "jsonrpc": "2.0", "method": "call", "id": 1,
        "params": {"seller_external_id": seller_external_id, "day": day.isoformat()}})
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise RuntimeError(f"склад отказал: {body['error'].get('message')}")
    result = body.get("result") or {}
    return {"places": Decimal(str(result.get("places") or 0)),
            "skus_without_norm": int(result.get("skus_without_norm") or 0),
            "units_without_norm": int(result.get("units_without_norm") or 0)}


class StorageLoop:
    def __init__(self, service: BillingService, stopping: threading.Event) -> None:
        self._service = service
        self._stopping = stopping

    def run(self) -> None:
        while True:
            try:
                self.once()
            except Exception as failure:  # noqa: BLE001 — воркер обязан пережить склад
                log.warning("начисление хранения не прошло: %s", failure)
            if self._stopping.wait(STORAGE_INTERVAL):
                return

    def once(self) -> None:
        # Имя не `today`: локальная переменная с именем импортированной
        # функции затеняет её, и следующий такт падает `UnboundLocalError` —
        # начисление хранения не проходит вовсе, а в логе видно только
        # «не прошло».
        current = today()
        for offset in range(1, STORAGE_BACKFILL_DAYS + 1):
            day = current - timedelta(days=offset)
            results = self._service.accrue_storage(day, box_places, tenant=tenant_id())
            accrued = [row for row in results if row["outcome"] == "accrued"]
            if accrued:
                log.info("хранение за %s: начислено кабинетам %d", day, len(accrued))
            # Отказы — тоже в лог, и с причиной.
            #
            # Логировались только успехи. Пока воркер ходил в закрытый склад
            # без токена, в логе было пусто: ни одной строки о том, что
            # хранение не начислено НИ ОДНОМУ кабинету. Видно это было только
            # в счётчике, а счётчик смотрят, когда уже загорелось.
            refused = [row for row in results if row["outcome"] == "unbilled"]
            if refused:
                reasons = Counter(str(row.get("reason")) for row in refused)
                log.warning(
                    "хранение за %s: не начислено кабинетам %d (%s); первый: %s — %s",
                    day, len(refused),
                    ", ".join(f"{name} × {count}" for name, count in reasons.most_common()),
                    refused[0].get("seller"), str(refused[0].get("detail"))[:200])


class OutboxLoop:
    """Выносит события биллинга на шину. Опубликованное помечается, не удаляется."""

    def __init__(self, database: Database, url: str, stopping: threading.Event) -> None:
        self._db = database
        self._url = url
        self._stopping = stopping
        self._connection: Any = None
        self._channel: Any = None

    def run(self) -> None:
        while not self._stopping.is_set():
            try:
                published = self.once()
            except Exception as failure:  # noqa: BLE001
                log.warning("публикация outbox не прошла: %s", failure)
                self._connection = None
                self._channel = None
                published = 0
            if published == 0:
                self._stopping.wait(OUTBOX_INTERVAL)

    def once(self) -> int:
        import pika

        with self._db.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM billing_outbox WHERE published_at IS NULL "
                " ORDER BY id LIMIT %s", (OUTBOX_BATCH,))
            rows = [dict(row) for row in cursor.fetchall()]
        if not rows:
            return 0

        if self._connection is None or self._connection.is_closed:
            self._connection = pika.BlockingConnection(pika.URLParameters(self._url))
            self._channel = None
        if self._channel is None or self._channel.is_closed:
            # Канал переиспользуется, а не создаётся на каждую пачку: лимит
            # каналов на соединение у RabbitMQ конечен, а пачки идут каждую
            # секунду.
            self._channel = self._connection.channel()
            self._channel.exchange_declare(
                exchange=events_exchange(), exchange_type="topic", durable=True)
            # Подтверждения брокера. Без них `basic_publish` возвращается,
            # как только байты ушли в сокет: событие помечается
            # опубликованным, из outbox уходит, а RabbitMQ мог его не принять.
            # Ровно то, что outbox и должен исключать.
            self._channel.confirm_delivery()
        channel = self._channel

        published = 0
        for row in rows:
            body = {
                "event_id": str(row["event_id"]), "tenant_id": row["tenant_id"],
                "type": row["type"], "occurred_at": row["occurred_at"].isoformat(),
                "payload": row["payload"], "correlation_id": row["correlation_id"],
            }
            try:
                channel.basic_publish(
                    exchange=events_exchange(), routing_key=row["type"],
                    body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    properties=pika.BasicProperties(content_type="application/json",
                                                    delivery_mode=2),
                    # Некуда положить — отказ, а не тишина: событие без очереди
                    # обмен принимает и выбрасывает.
                    mandatory=True)
            except Exception as failure:  # noqa: BLE001
                with self._db.transaction() as cursor:
                    cursor.execute(
                        "UPDATE billing_outbox SET attempts = attempts + 1, last_error = %s "
                        " WHERE id = %s", (str(failure)[:500], row["id"]))
                raise
            with self._db.transaction() as cursor:
                cursor.execute("UPDATE billing_outbox SET published_at = now() WHERE id = %s",
                               (row["id"],))
            published += 1
        WORKER_PROCESSED.labels(worker="outbox").inc(published)
        return published


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app_environment()
    database = Database(database_url())
    service = BillingService(database)
    stopping = threading.Event()

    start_http_server(int(os.getenv("METRICS_PORT", "8081")))
    WORKER_PROCESSED.labels(worker="storage").inc(0)
    WORKER_PROCESSED.labels(worker="outbox").inc(0)

    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())

    threads = [threading.Thread(target=StorageLoop(service, stopping).run, daemon=True)]
    rabbit = os.getenv("RABBITMQ_URL", "")
    if rabbit:
        threads.append(threading.Thread(target=OutboxLoop(database, rabbit, stopping).run,
                                        daemon=True))
    else:
        log.warning("RABBITMQ_URL не задан: события биллинга останутся в outbox")
    for thread in threads:
        thread.start()

    started = time.monotonic()
    while not stopping.wait(1.0):
        pass
    log.info("остановлен после %.0f с работы", time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
