"""Проба живости воркера — по работе, а не по тому, что процесс запущен.

Инвариант 14: молчащий воркер считается сломанным. В боевом контуре четыре
воркера стоят `Up` с зелёным healthcheck и нулём строк лога (раздел 3.5) —
ровно потому, что проба спрашивала «процесс есть?», а не «работа идёт?».

Признак работы берётся из базы, а не из метрик процесса: у каждого процесса
свой реестр метрик, и воркеры своего HTTP не поднимают. База переживает и
перезапуск, и ротацию логов.

    python -m app.workers.healthcheck wb_sync
"""
from __future__ import annotations

import os
import sys

from ..postgres import pool as shared_pool, single

# Сколько воркер имеет право молчать, прежде чем считаться сломанным.
# Опрос ходит раз в секунду, сверка — раз в минуту, поэтому пороги разные.
LIMITS: dict[str, tuple[str, int]] = {
    # воркер: (запрос, предел молчания в секундах)
    "wb_sync": (
        "SELECT EXTRACT(EPOCH FROM (now() - max(last_sync_at))) "
        "  FROM wb_account WHERE status = 'ACTIVE'", 120),
    "wb_labels": (
        # Стикеры тянутся по надобности: отсутствие работы — норма. Проверяем,
        # что воркер способен читать базу, и что очередь не растёт вечно.
        "SELECT CASE WHEN count(*) > 500 THEN 999999 ELSE 0 END "
        "  FROM wms_task t LEFT JOIN wb_label l ON l.task_id = t.id "
        " WHERE t.state = 'reserved' AND l.id IS NULL", 60),
    "wb_reconcile": (
        "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - max(last_reconciled_at))), 0) "
        "  FROM wms_task WHERE state IN ('shipped', 'handed')", 900),
    "outbox": (
        # Неопубликованный аутбокс старше пяти минут — публикатор не работает.
        "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(occurred_at))), 0) "
        "  FROM outbox WHERE published_at IS NULL", 300),
}


def main() -> int:
    worker = sys.argv[1] if len(sys.argv) > 1 else os.getenv("WMS_ROLE", "")
    probe = LIMITS.get(worker.replace("-", "_"))
    if probe is None:
        print(f"неизвестный воркер {worker!r}: проверять нечего", file=sys.stderr)
        return 1

    sql, limit = probe
    try:
        with shared_pool().connection() as connection:
            with single(connection) as cursor:
                cursor.execute(sql)
                row = cursor.fetchone()
    except Exception as failure:  # noqa: BLE001 — проба обязана отвечать, а не падать
        print(f"{worker}: база недоступна ({failure})", file=sys.stderr)
        return 1

    value = next(iter(row.values())) if row else None
    age = float(value) if value is not None else 0.0
    if age > limit:
        print(f"{worker}: молчит {age:.0f} с при пределе {limit} с", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
