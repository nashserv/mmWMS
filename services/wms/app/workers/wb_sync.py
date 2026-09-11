"""Опросчик Wildberries — внутренний воркер `wms`.

Заменяет `gateway-sync-worker` вместе с очередью, которая за ним стояла
(раздел 6.3). Между Wildberries и заданием больше нет ни одной очереди: воркер
опросил WB и тут же, той же транзакцией раздела 6.2, завёл задание и резерв.
Рабочее место увидит задание, даже если RabbitMQ лежит (инвариант 8).

Цикл на кабинет:
    занять кабинет лизингом          (два опросчика не бьют в один лимит)
    взять место в минутном окне      (300 запросов, приложение D)
    GET /api/v3/orders с перекрытием (дыра на границе окна закрывается)
    на каждое задание — транзакция 6.2
    сохранить курсор, назначить следующий опрос

HTTP-вызов здесь и транзакция там разнесены во времени намеренно: вызов в WB
занимает около 500 мс, транзакция — 3 мс, и держать блокировку строки остатка
всё это время значит уронить пропускную способность на три порядка.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Sequence

from .. import rate_limit, repositories as repo
from ..metrics import WB_SYNC_LAST_SUCCESS, WB_SYNC_LAG
from ..postgres import ConnectionPool, pool as shared_pool, single
from ..secrets import SecretUnavailable
from ..service import WmsService
from ..stock_push import publisher as stock_publisher
from ..wb import WbClient, WbError, WbOrder
from .loop import Worker, configure_logging

log = logging.getLogger("wms.wb_sync")

# Как часто опрашивать кабинет в спокойном состоянии. Задержка «WB → задание в
# базе» должна укладываться в 2 с по p99 (раздел 10), поэтому интервал заметно
# меньше секунды: цена вопроса — один вызов из трёхсот в минуту.
SYNC_INTERVAL = float(os.getenv("WB_SYNC_INTERVAL_SECONDS", "0.5"))
# Перекрытие курсора: WB отдаёт задания страницами, и на границе окна страница
# может сдвинуться. Перечитать десяток заданий дешевле, чем потерять одно —
# повторный опрос идемпотентен по wb_order_id (инвариант 5).
SYNC_OVERLAP = int(os.getenv("WB_SYNC_OVERLAP", "10"))
ACCOUNTS_PER_TICK = int(os.getenv("WB_SYNC_ACCOUNTS_PER_TICK", "8"))

# Пауза, когда опрашивать было нечего. Это НЕ то же самое, что SYNC_INTERVAL, и
# путать их дорого.
#
# SYNC_INTERVAL — как часто опрашивать ОДИН кабинет, и он про бюджет лимита
# Wildberries: 300 запросов в минуту на кабинет (приложение D).
#
# IDLE_SECONDS — сколько спать, когда ни один кабинет не подошёл по сроку.
# Холостой такт не делает в WB НИ ОДНОГО вызова: он только спрашивает Postgres,
# кому пора. Бюджету лимита он не стоит ничего, а вот задержку «WB →
# доступность» (критерий раздела 10, меньше 2 с) добавляет целиком.
#
# Когда они были одним числом, кабинетов 24 и восемь за такт, полный круг
# складывался из трёх тактов плюс холостая пауза — и упирался в те самые 2 с,
# из-за чего шаг 4 полного прогона краснел через раз.
IDLE_SECONDS = float(os.getenv("WB_SYNC_IDLE_SECONDS", "0.2"))
LEASE_SECONDS = int(os.getenv("WB_SYNC_LEASE_SECONDS", "120"))


def _configured_accounts() -> list[str]:
    """Кабинеты из `WB_SYNC_ONLY_ACCOUNTS`, через запятую. Пусто — все."""
    raw = os.getenv("WB_SYNC_ONLY_ACCOUNTS", "")
    return [value.strip() for value in raw.split(",") if value.strip()]


class WbSyncWorker:
    def __init__(self, pool: ConnectionPool, *, service: WmsService | None = None,
                 only_accounts: Sequence[str] | None = None) -> None:
        self._pool = pool
        # Резерв уводит товар из good — значит, доступный остаток изменился и
        # его надо опубликовать. Немедленно, без таймеров (раздел 6.4).
        self._service = service or WmsService(
            pool, on_stock_changed=stock_publisher(pool).notify)
        # Список кабинетов, которые опрашивает этот воркер. Пустой — все.
        # Переключение на новую WMS идёт по одному кабинету (раздел 11, шаг 6):
        # первый клиент едет отдельным воркером, остальные остаются на боевом
        # шлюзе, и откат — это убрать кабинет из списка.
        self._only = list(only_accounts) if only_accounts else _configured_accounts()

    def tick(self) -> int:
        """Один проход по кабинетам, которым пора. Возвращает число заданий."""
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                accounts = repo.lease_accounts(
                    cursor, limit=ACCOUNTS_PER_TICK, lease_seconds=LEASE_SECONDS,
                    only=self._only)
        return sum(self._sync_account(account) for account in accounts)

    def _sync_account(self, account: dict[str, Any]) -> int:
        started = time.monotonic()
        try:
            orders, next_cursor = self._fetch(account)
        except WbError as failure:
            self._after_failure(account, failure)
            return 0
        except SecretUnavailable as failure:
            # Кабинет без разрешимого секрета опрашивать нечем. Это не
            # временный сбой: до вмешательства человека он так и останется.
            log.error("кабинет %s: секрет не разрешён (%s)", account["external_id"], failure)
            self._finish(account, next_in=60.0, error="SECRET_UNAVAILABLE", status="AUTH_ERROR")
            return 0

        # Перекрытие курсора возвращает уже заведённые задания — отсеиваем их
        # одним запросом, до всякой транзакции. Идемпотентность от этого не
        # зависит: ON CONFLICT в транзакции остаётся последним рубежом, а этот
        # фильтр просто не даёт открывать транзакцию ради известного ответа.
        known: set[int] = set()
        if orders:
            with self._pool.connection() as connection:
                with single(connection) as cursor:
                    known = repo.known_orders(cursor, [order.wb_order_id for order in orders])

        created = 0
        for order in orders:
            if order.wb_order_id in known:
                continue
            if self._reserve(account, order):
                created += 1

        with self._pool.connection() as connection:
            with single(connection) as cursor:
                repo.finish_sync_with_cursor(
                    cursor, account["id"], cursor_value=next_cursor,
                    next_in_seconds=SYNC_INTERVAL, status="ACTIVE")
        WB_SYNC_LAST_SUCCESS.set(time.time())
        WB_SYNC_LAG.observe(time.monotonic() - started)
        if created:
            log.info("кабинет %s: заведено заданий %d, перечитано по перекрытию %d",
                     account["external_id"], created, len(known))
        return created

    def _fetch(self, account: dict[str, Any]) -> tuple[list[WbOrder], int]:
        """Забирает страницу заданий. Место в лимите занимается до вызова."""
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                allowed, wait, cursor_value = repo.take_slot_and_cursor(
                    cursor, account["id"], limit=rate_limit.LIMIT_PER_MINUTE)
        if not allowed:
            # Ограничитель существует ради защиты от бана и в нормальной работе
            # не срабатывает (раздел 6.4). Сработал — ждём окно, а не долбим.
            raise WbError(429, "LOCAL_RATE_LIMIT", retry_after=wait)

        from_cursor = max(0, cursor_value - SYNC_OVERLAP)
        with WbClient(account_external_id=account["external_id"],
                      secret_ref=account["secret_ref"]) as client:
            orders, next_cursor = client.orders(cursor=from_cursor)
        # Курсор двигаем не дальше, чем отдал WB, и не назад: перекрытие —
        # это способ перечитать, а не откатиться.
        return orders, max(cursor_value, next_cursor)

    def _reserve(self, account: dict[str, Any], order: WbOrder) -> bool:
        """Задание и резерв — одной транзакцией (инвариант 1).

        Отменённое у WB задание заводить незачем: у нас его ещё нет, а `cancel`
        означает, что везти уже нечего.
        """
        if order.supplier_status == "cancel":
            return False
        outcome = self._service.reserve({
            "idempotency_key": f"wb-order-{order.wb_order_id}",
            "seller_external_id": account["seller_external_id"],
            "wb_account_external_id": account["external_id"],
            "wb_order_id": order.wb_order_id,
            "order_uid": order.uid,
            "sku": order.barcode,
            "barcode": order.barcode,
            "quantity": order.quantity,
            "deadline": order.deadline,
            "correlation_id": f"wb-sync-{order.wb_order_id}",
        })
        return outcome.status == "reserved" and not outcome.duplicate

    def _after_failure(self, account: dict[str, Any], failure: WbError) -> None:
        status = None
        wait = SYNC_INTERVAL
        if failure.rate_limited:
            status, wait = "RATE_LIMITED", max(failure.retry_after, 1.0)
            with self._pool.connection() as connection:
                with single(connection) as cursor:
                    rate_limit.block(cursor, account["id"], wait)
        elif failure.auth_rejected:
            # Токен отозван или сменился. Дальше долбить бессмысленно и вредно:
            # WB считает повторные 401 поводом для блокировки.
            status, wait = "AUTH_ERROR", 300.0
        else:
            wait = min(SYNC_INTERVAL * (2 ** min(account["sync_attempts"], 6)), 60.0)
        log.warning("кабинет %s: опрос не удался (%s), следующий через %.0f с",
                    account["external_id"], failure.code, wait)
        self._finish(account, next_in=wait, error=failure.code, status=status)

    def _finish(self, account: dict[str, Any], *, next_in: float, error: str | None,
                status: str | None) -> None:
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                repo.finish_sync(cursor, account["id"], next_in_seconds=next_in,
                                 error_code=error, status=status)


def main() -> None:
    configure_logging()
    worker = WbSyncWorker(shared_pool())
    Worker("wb_sync", idle_seconds=IDLE_SECONDS).run(worker.tick)


if __name__ == "__main__":
    main()
