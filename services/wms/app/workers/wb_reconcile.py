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
from ..domain import agrees_with_wb, TaskState

# Состояния, из которых отмена у WB означает «везти больше нечего».
# `shipped`/`handed`/`accepted` сюда не входят: товар уже уехал, и это разбор
# возврата, а не отмена (раздел 2.12).
CANCELLABLE_ON_WB_CANCEL = frozenset({
    TaskState.NEW.value,
    TaskState.RESERVED.value,
    TaskState.SHORT.value,
    TaskState.MANUAL_REVIEW.value,
    TaskState.PICKING.value,
    TaskState.PICKED.value,
})
from ..metrics import (INVENTORY_DIVERGENCE, TASKS_DIVERGED, WB_CALLS,
                       WB_ORDERS_MISSING, WB_RATE_LIMITED)
from ..postgres import ConnectionPool, pool as shared_pool, single, transaction
from ..secrets import SecretUnavailable
from ..wb import WbClient, WbError
from .loop import Worker, configure_logging

log = logging.getLogger("wms.wb_reconcile")

# Сверка не гонится: она смотрит на то, что уже случилось. Раз в минуту на
# кабинет — это 60 вызовов в час из 18 000 доступных по лимиту.
INTERVAL = float(os.getenv("WB_RECONCILE_INTERVAL_SECONDS", "60"))
ACCOUNTS_PER_TICK = int(os.getenv("WB_RECONCILE_ACCOUNTS_PER_TICK", "4"))
# Сколько наших заданий сверяется за один заход по кабинету.
PAGE = int(os.getenv("WB_RECONCILE_PAGE", "1000"))
# Пачка `POST /api/v3/orders/status`: у Wildberries предел 1000 номеров.
STATUS_BATCH = 1000
# Ежедневный отчёт расхождений по owner × sku (раздел 11, шаг 2).
REPORT_INTERVAL = float(os.getenv("WB_RECONCILE_REPORT_SECONDS", "86400"))
# Сколько держать служебные журналы лимита и публикаций остатка.
RETENTION_DAYS = int(os.getenv("WB_RETENTION_DAYS", "7"))
# Страховочная полная публикация остатка (инвариант 7).
SWEEP_INTERVAL = float(os.getenv("WB_STOCK_SWEEP_SECONDS", "600"))


class WbReconcileWorker:
    def __init__(self, pool: ConnectionPool, *,
                 only_accounts: Sequence[str] | None = None,
                 tasks: Any = None,
                 publish_stock: Any = None) -> None:
        self._pool = pool
        # Страховка инварианта 7: раз в десять минут остаток публикуется
        # целиком, что бы ни случилось с отдельными вызовами.
        self._publish = publish_stock if publish_stock is not None else _default_publish(pool)
        # Операции над заданиями: сверка отменяет то, что отменил клиент у WB.
        # Передаётся снаружи, чтобы воркер не собирал половину сервиса сам.
        self._tasks = tasks if tasks is not None else _default_tasks(pool)
        self._only = list(only_accounts) if only_accounts else _configured_accounts()
        # Когда кабинет можно сверять снова. Метки в базе для этого мало:
        # `last_reconciled_at` проставляется только заданиям, которые пришли в
        # ответе WB, а те, которых там нет, оставляют кабинет «просроченным»
        # навсегда — и сверка уходит в бесконечный цикл, выбирая общий лимит
        # кабинета и лишая вызовов опрос заданий.
        self._next_allowed: dict[Any, float] = {}
        # Первый отчёт — сразу после старта: если расхождения уже накопились,
        # узнать об этом надо не через сутки.
        self._next_report = 0.0
        # А вот страховочная публикация не нужна сразу: на старте остаток
        # только что опубликован тем, кто его двигал.
        self._next_sweep = time.monotonic() + SWEEP_INTERVAL

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
        if time.monotonic() >= self._next_sweep:
            self._republish_everything()
            self._next_sweep = time.monotonic() + SWEEP_INTERVAL
        if time.monotonic() >= self._next_report:
            self._report()
            self._retention()
            self._next_report = time.monotonic() + REPORT_INTERVAL
        return checked

    def _republish_everything(self) -> None:
        """Полная публикация остатка всех владельцев — страховка инварианта 7.

        Публикация идёт сразу после движения, без таймеров, и это правильно.
        Но потерянный вызов — упавший контейнер, сеть, 429 — оставляет остаток
        в Wildberries расходящимся до следующего движения по тому же товару. У
        редкого товара это недели.

        Развёртка редкая (раз в десять минут) и намеренно тупая: взять всех
        владельцев и сказать публикатору «эти изменились».
        """
        if self._publish is None:
            return
        try:
            with self._pool.connection() as connection:
                with single(connection) as cursor:
                    owners = repo.owners_with_stock(cursor)
            for owner_id, sku_ids in owners.items():
                self._publish(owner_id, sku_ids)
        except Exception:  # noqa: BLE001 — страховка не роняет сверку
            log.exception("полная публикация остатка не удалась")
            return
        if owners:
            log.info("страховочная публикация остатка: владельцев %d", len(owners))

    def _retention(self) -> None:
        """Чистит служебные журналы. На проде outbox дорос до 136 210 записей.

        `wb_rate_limit` растёт по строке на кабинет в минуту — это полмиллиона
        строк в год на один кабинет, и нужны они ровно минуту.
        """
        try:
            with self._pool.connection() as connection:
                with transaction(connection) as cursor:
                    removed = rate_limit.retention(cursor, days=RETENTION_DAYS)
        except Exception:  # noqa: BLE001 — уборка не роняет сверку
            log.exception("уборка служебных журналов не удалась")
            return
        if removed:
            log.info("убрано строк служебных журналов: %d", removed)

    def _reconcile(self, account: dict[str, Any]) -> int:
        # Спрашиваем адресно про свои незакрытые задания, а не читаем первую
        # страницу истории кабинета. Первая страница — самые старые заказы за
        # всё время; открытое задание месячной давности в неё не попадает
        # никогда, и его расхождения не видит никто.
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                wanted = repo.open_tasks_of_account(cursor, account["id"], limit=PAGE)
        if not wanted:
            return 0

        calls = max(1, (len(wanted) + STATUS_BATCH - 1) // STATUS_BATCH)
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                if not all(rate_limit.take(cursor, account["id"]) for _ in range(calls)):
                    return 0
        try:
            with WbClient(account_external_id=account["external_id"],
                          secret_ref=account["secret_ref"]) as client:
                statuses = client.orders_status(wanted)
        except (WbError, SecretUnavailable) as failure:
            WB_CALLS.labels(operation="reconcile", outcome="error").inc()
            log.warning("кабинет %s: сверка не состоялась (%s)",
                        account["external_id"], failure)
            return 0
        WB_CALLS.labels(operation="reconcile", outcome="ok").inc()

        # Задание, о котором WB промолчал, — это находка, а не тишина. Оно
        # исчезло из кабинета: подменили токен, заказ удалён, кабинет не тот.
        # Раньше такое задание просто не попадало в выборку и жило вечно.
        missing = [order_id for order_id in wanted if order_id not in statuses]
        if missing:
            log.error("кабинет %s: Wildberries не знает %d наших заданий, первое — заказ %s",
                      account["external_id"], len(missing), missing[0])
            WB_ORDERS_MISSING.labels(account=str(account["external_id"])).set(len(missing))
        else:
            WB_ORDERS_MISSING.labels(account=str(account["external_id"])).set(0)
        if not statuses:
            return 0

        diverged, checked = [], 0
        # Отмены у WB — не расхождение, а команда: клиент отменил заказ, и
        # везти больше нечего. Обрабатываются ПОСЛЕ транзакции сверки: отмена
        # снимает резерв, пишет движения и публикует остаток, а держать это
        # внутри сверки значит держать блокировку на время вызова в WB
        # (инвариант 2).
        to_cancel: list[tuple[Any, int]] = []
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                for task in repo.tasks_for_reconcile(cursor, list(statuses)):
                    wb_status = statuses.get(int(task["wb_order_id"]))
                    checked += 1
                    if wb_status == "cancel" and task["state"] in CANCELLABLE_ON_WB_CANCEL:
                        to_cancel.append((task["id"], int(task["wb_order_id"])))
                        continue
                    if agrees_with_wb(task["state"], wb_status,
                                      in_supply=task.get("supply_id") is not None):
                        repo.mark_reconciled(cursor, task["id"], wb_status=wb_status)
                        continue
                    # Не перезаписываем: кто прав — вопрос к человеку.
                    repo.mark_diverged(cursor, task["id"], wb_status=wb_status)
                    diverged.append((task["wb_order_id"], task["state"], wb_status))

        for task_id, wb_order_id in to_cancel:
            self._cancel_after_wb(task_id, wb_order_id, account["external_id"])

        if diverged:
            # Алерт, а не тихая запись (инвариант 10). Полный список — в лог,
            # счётчик — в Prometheus: молчащая сверка ничем не лучше молчащего
            # воркера.
            log.error("кабинет %s: расхождений с WB %d, первое — заказ %s: у нас %s, у WB %s",
                      account["external_id"], len(diverged), diverged[0][0],
                      diverged[0][1], diverged[0][2])
        return checked

    def _cancel_after_wb(self, task_id: Any, wb_order_id: int, account: str) -> None:
        """Отменить задание, которое отменил клиент у Wildberries.

        Раньше такое задание уходило в `diverged` и оставалось лежать: товар
        числился в резерве под заказ, которого больше нет, и склад собирал бы
        его вручную. В боевом контуре так и накопились 2645 отмен, у всех с
        пустой причиной.

        Причина обязательна (инвариант 11), и она здесь разбираема: «отменено
        у Wildberries», с номером заказа.
        """
        if self._tasks is None:
            log.warning("заказ %s отменён у WB, но отменить задание нечем: "
                        "операции над заданиями не подключены", wb_order_id)
            return
        try:
            self._tasks.cancel(str(task_id), {
                "cancellation_event_id": f"wb-cancel-{wb_order_id}",
                "reason": f"отменено у Wildberries (заказ {wb_order_id})",
                "handed_over": False})
        except Exception as failure:  # noqa: BLE001 — сверка не падает из-за одного задания
            log.exception("кабинет %s: не удалось отменить задание %s по отмене WB (%s)",
                          account, task_id, failure)
            return
        log.info("кабинет %s: заказ %s отменён у WB — задание отменено и резерв снят",
                 account, wb_order_id)

    def _report(self) -> None:
        """Отчёт расхождений по владельцу и товару — то, что читают в shadow.

        Раздел 11, шаг 2: `wms` строит свою таблицу заданий рядом с боевым
        контуром, и ежедневный отчёт показывает, сходятся ли они. Отчёт идёт в
        лог, в метрику и в таблицу `shadow_divergence_report` — копить
        расхождения молча ровно то, что делал боевой контур.

        Таблица нужна именно для shadow: наблюдают неделю, а вопрос недели —
        «убывает ли разница». По логу его не задать.
        """
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                lines = repo.divergence_report(cursor)
                # Снимок ложится в таблицу, а не только в лог: неделю
                # наблюдения (раздел 11, шаг 2) сравнивают посуточно, а лог
                # ротируется и теряется вместе с контейнером.
                repo.save_divergence_report(cursor, lines)
        if not lines:
            log.info("сверка: расхождений с Wildberries нет")
            return
        total = sum(int(line["tasks"]) for line in lines)
        log.error("сверка: расхождений с WB %d по %d парам владелец × товар",
                  total, len(lines))
        for line in lines[:20]:
            log.error("  %s / %s: %s у нас против %s у WB — заданий %d, старшему с %s",
                      line["seller_external_id"], line["barcode"] or "—",
                      line["state"], line["wb_status"] or "—", int(line["tasks"]),
                      line["oldest"])

    def _refresh_gauge(self) -> None:
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                cursor.execute("SELECT count(*) AS n FROM wms_task WHERE state = 'diverged'")
                diverged = cursor.fetchone()
                # Кабинеты под паузой Wildberries: такой кабинет не получает
                # новые заказы вовсе, и склад не знает, что не получает.
                cursor.execute(
                    "SELECT count(*) AS n FROM wb_account WHERE blocked_until > now()")
                paused = cursor.fetchone()
                # Инвариант 1: `stock_balance` — проекция журнала. Расхождение
                # с последним пересчётом значит, что где-то пишут мимо него.
                cursor.execute(
                    "SELECT count(*) AS n FROM ("
                    "  SELECT l.sku_id, l.cell_id, l.box_id, l.fact_qty, "
                    "         COALESCE(b.qty, 0) AS balance, "
                    "         row_number() OVER (PARTITION BY l.sku_id, l.cell_id, l.box_id "
                    "                            ORDER BY c.applied_at DESC) AS recency "
                    "    FROM inventory_count_line l "
                    "    JOIN inventory_count c ON c.id = l.count_id AND c.state = 'applied' "
                    "    LEFT JOIN stock_balance b "
                    "           ON b.sku_id = l.sku_id AND b.cell_id = l.cell_id "
                    "          AND b.box_id IS NOT DISTINCT FROM l.box_id "
                    "          AND b.state = 'good' "
                    "   WHERE c.applied_at > now() - interval '1 day') fresh "
                    " WHERE recency = 1 AND fact_qty IS DISTINCT FROM balance")
                divergence = cursor.fetchone()
        TASKS_DIVERGED.set(int(diverged["n"]) if diverged else 0)
        WB_RATE_LIMITED.set(int(paused["n"]) if paused else 0)
        INVENTORY_DIVERGENCE.set(int(divergence["n"]) if divergence else 0)


def _default_tasks(pool: ConnectionPool) -> Any:
    """Операции над заданиями для отмены по сигналу WB.

    Собирается лениво и не роняет воркер: сверка полезна и без возможности
    отменять — она всё равно покажет расхождение.
    """
    try:
        from ..service import WmsService
        from ..stock_push import publisher as stock_publisher_for
        from ..supplies import release_from_supply
        from ..tasks import TaskOperations

        publisher = stock_publisher_for(pool)
        return TaskOperations(pool, WmsService(pool, on_stock_changed=publisher.notify),
                              publisher.notify,
                              on_supply_release=release_from_supply(pool))
    except Exception:  # noqa: BLE001
        log.exception("не удалось собрать операции над заданиями: "
                      "сверка будет только отмечать расхождения")
        return None


def _default_publish(pool: ConnectionPool) -> Any:
    """Публикатор остатка. Не роняет воркер, если собрать его не вышло."""
    try:
        from ..stock_push import publisher as stock_publisher_for
        return stock_publisher_for(pool).notify
    except Exception:  # noqa: BLE001
        log.exception("не удалось собрать публикатор остатка: "
                      "страховка инварианта 7 отключена")
        return None


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
