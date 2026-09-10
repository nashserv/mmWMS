#!/usr/bin/env python3
"""Проверка шага 12 полного прогона без потоков A и B.

Шаг 12 («начисление на каждую тарифицируемую операцию; amount=45,
partner_amount=15, net_amount=30») читает начисления по событиям, которые
прогон наблюдал на шине. Пока шаги 9 и 11 красные, событий нет и шаг краснеет
не из-за биллинга — но проверить сам биллинг это не мешает.

Скрипт публикует ровно те три типа событий, которые перечислены в
`tests/full-run/scenario.py` как тарифицируемые, за того же клиента прогона, и
делает тот же запрос, что и шаг 12. Ложным зелёным он быть не может: события
идут через настоящую шину и настоящий консьюмер, а не мимо них.

    docker exec -i mmx-stand-billing-inbox-consumer-1 python - \
        < services/billing/scripts/check-step-12.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
import uuid

import pika
import psycopg
from psycopg.rows import dict_row

SELLER = "full-run-seller"          # tests/full-run/scenario.py
EXPECTED = (45, 15, 30)             # AMOUNT / PARTNER_AMOUNT / NET_AMOUNT там же

BILLABLE = [
    ("order.packed.v1", {"seller_id": SELLER, "task_id": "fr-1"}),
    ("wb.supply.shipped.v1", {"supply": "WB-GI-fr-1", "seller_id": SELLER, "orders": 1,
                              "accepted_at": None, "name": "поставка прогона"}),
    ("wb.orders.processed.v1", {"seller_id": SELLER, "orders": 1}),
]


def main() -> int:
    events = [{
        "event_id": str(uuid.uuid4()), "tenant_id": "mm-express", "type": event_type,
        "occurred_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "payload": payload, "correlation_id": "проверка шага 12",
    } for event_type, payload in BILLABLE]

    connection = pika.BlockingConnection(pika.URLParameters(os.environ["RABBITMQ_URL"]))
    channel = connection.channel()
    for body in events:
        channel.basic_publish(
            exchange=os.getenv("MMX_EVENTS_EXCHANGE", "mmx.events"),
            routing_key=body["type"],
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2))
    connection.close()
    time.sleep(float(os.getenv("CHECK_WAIT_SECONDS", "3")))

    database = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    failures = 0
    for body in events:
        row = database.execute("SELECT * FROM billing_accrual WHERE event_id = %s",
                               (body["event_id"],)).fetchone()
        if row is None:
            print(f"КРАСНЫЙ {body['type']}: начисления нет")
            failures += 1
            continue
        got = (int(row["amount"]), int(row["partner_amount"]), int(row["net_amount"]))
        status = "ЗЕЛЁНЫЙ" if got == EXPECTED else "КРАСНЫЙ"
        failures += got != EXPECTED
        print(f"{status} {body['type']}: amount={got[0]} partner_amount={got[1]} "
              f"net_amount={got[2]} (услуга {row['service']})")

    print("\nшаг 12 при наличии событий:", "ЗЕЛЁНЫЙ" if not failures else "КРАСНЫЙ")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
