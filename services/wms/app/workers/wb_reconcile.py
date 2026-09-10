"""Сверка статусов с Wildberries.

Инвариант 10: расхождение с WB — состояние `diverged` и алерт, а не тихая
запись. В боевом контуре 2467 заданий из 6374 расходились с Wildberries —
`CANCELLED` у нас против `complete` у них, — и ни одно из этих расхождений
никого не разбудило (раздел 3).

Сверка читает только `GET /api/v3/orders`, ничего не пишет и потому работает в
любом режиме кабинета, включая `shadow` (раздел 11, шаг 2). В shadow она и есть
главный инструмент: `wms` строит свою таблицу заданий рядом с боевым контуром,
а ежедневный отчёт расхождений показывает, сходятся ли они.

Разошедшееся задание не перезаписывается. Мы не знаем, кто прав: у WB может
быть отмена, которую мы не видели, а у нас — отгрузка, о которой WB ещё не
знает. Догадка здесь дороже разбора, поэтому задание останавливается и ждёт
человека.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Sequence

from .. import rate_limit, repositories as repo
from ..domain import agrees_with_wb
from ..metrics import TASKS_DIVERGED, WB_CALLS
from ..postgres import ConnectionPool, pool as shared_pool, single, transaction
from ..secrets import SecretUnavailable
from ..wb import WbClient, WbError
from .loop import Worker, configure_logging

log = logging.getLogger("wms.wb_reconcile")

# Сверка не гонится: она смотрит на то, что уже случилось. Раз в минуту на
# кабинет — это 60 вызовов в час из 18 000 доступных по лимиту.
INTERVAL = float(os.getenv("WB_RECONCILE_INTERVAL_SECONDS", "60"))
ACCOUNTS_PER_TICK = int(os.getenv("WB_RECONCILE_ACCOUNTS_PER_TICK", "4"))
PAGE = int(os.getenv("WB_RECONCILE_PAGE", "1000"))


class WbReconcileWorker:
    def __init__(self, pool: ConnectionPool, *,
                 only_accounts: Sequence[str] | None = None) -> None:
        self._pool = pool
        self._only = list(only_accounts) if only_accounts else _configured_accounts()
        # Когда кабинет можно сверять снова. Метки в базе для этого мало:
        # `last_reconciled_at` проставляется только заданиям, которые пришли в
        # ответе WB, а те, которых там нет, оставляют кабинет «просроченным»
        # навсегда — и сверка уходит в бесконечный цикл, выбирая общий лимит
        # кабинета и лишая вызовов опрос заданий.
        self._next_allowed: dict[Any, float] = {}

    def tick(self) -> int:
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                accounts = repo.accounts_due_for_reconcile(
                    cursor, limit=ACCOUNTS_PER_TICK, older_than_seconds=INTERVAL,
                    only=self._only)
        now = time.monotonic()
        due = [account for account in accounts
               if self._next_allowed.get(account["id"], 0.0) <= now]
        checked = sum(self._reconcile(account) for account in due)
        for account in due:
            self._next_allowed[account["id"]] = time.monotonic() + INTERVAL
        self._refresh_gauge()
        return checked

    def _reconcile(self, account: dict[str, Any]) -> int:
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                if not rate_limit.take(cursor, account["id"]):
                    return 0
        try:
            with WbClient(account_external_id=account["external_id"],
                          secret_ref=account["secret_ref"]) as client:
                orders, _cursor = client.orders(cursor=0, limit=PAGE)
        except (WbError, SecretUnavailable) as failure:
            WB_CALLS.labels(operation="reconcile", outcome="error").inc()
            log.warning("кабинет %s: сверка не состоялась (%s)",
                        account["external_id"], failure)
            return 0
        WB_CALLS.labels(operation="reconcile", outcome="ok").inc()

        statuses = {order.wb_order_id: order.supplier_status for order in orders}
        if not statuses:
            return 0

        diverged, checked = [], 0
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                for task in repo.tasks_for_reconcile(cursor, list(statuses)):
                    wb_status = statuses.get(int(task["wb_order_id"]))
                    checked += 1
                    if agrees_with_wb(task["state"], wb_status):
                        repo.mark_reconciled(cursor, task["id"], wb_status=wb_status)
                        continue
                    # Не перезаписываем: кто прав — вопрос к человеку.
                    repo.mark_diverged(cursor, task["id"], wb_status=wb_status)
                    diverged.append((task["wb_order_id"], task["state"], wb_status))

        if diverged:
            # Алерт, а не тихая запись (инвариант 10). Полный список — в лог,
            # счётчик — в Prometheus: молчащая сверка ничем не лучше молчащего
            # воркера.
            log.error("кабинет %s: расхождений с WB %d, первое — заказ %s: у нас %s, у WB %s",
                      account["external_id"], len(diverged), diverged[0][0],
                      diverged[0][1], diverged[0][2])
        return checked

    def _refresh_gauge(self) -> None:
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                cursor.execute("SELECT count(*) AS n FROM wms_task WHERE state = 'diverged'")
                row = cursor.fetchone()
        TASKS_DIVERGED.set(int(row["n"]) if row else 0)


def _configured_accounts() -> list[str]:
    raw = os.getenv("WB_SYNC_ONLY_ACCOUNTS", "")
    return [value.strip() for value in raw.split(",") if value.strip()]


def main() -> None:
    configure_logging()
    worker = WbReconcileWorker(shared_pool())
    # min_interval — страховка от того же цикла на уровне каркаса: сверка
    # никогда не должна стартовать чаще раза в секунду, сколько бы работы она
    # себе ни нашла.
    Worker("wb_reconcile", idle_seconds=INTERVAL, min_interval=1.0).run(worker.tick)


if __name__ == "__main__":
    main()
