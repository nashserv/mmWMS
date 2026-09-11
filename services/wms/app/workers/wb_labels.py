"""Загрузчик стикеров: этикетка лежит локально до того, как её попросят.

Сегодня стикер запрашивается в момент упаковки, и человек ждёт до трёх
последовательных вызовов Wildberries — открыть поставку, положить в неё заказ,
запросить стикер (раздел 3.3). От нажатия «печать» до движения головки уходит
2–10 секунд при цели в 300 мс.

Здесь та же цепочка выполняется заранее, сразу после резерва, и пачкой: до 100
стикеров за вызов. При 350 заданиях в день это четыре вызова к WB, при 10 000 в
час — сто. К моменту, когда сборщик подошёл к принтеру, этикетка уже своя.

Побочная выгода, ради которой это стоило бы делать и без цели в 300 мс: WB лёг,
а печать продолжается.

Формат — ZPL (`WB_STICKER_FORMAT`, по умолчанию `zplv`): 1–3 КБ текста против
20–100 КБ картинки, и принтер печатает его нативно, без растеризации драйвером.
Откат на PNG остаётся конфигурацией, потому что вопрос 2 раздела 13 ещё открыт:
на XP-420B поддержка ZPL заявлена, но не проверена на живом принтере.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import defaultdict
from typing import Any, Sequence

from .. import rate_limit, repositories as repo
from ..metrics import LABELS_FETCHED
from ..postgres import ConnectionPool, pool as shared_pool, single, transaction
from ..secrets import SecretUnavailable
from ..wb import STICKER_BATCH, WbClient, WbError, writes_allowed
from .loop import Worker, configure_logging

log = logging.getLogger("wms.wb_labels")

STICKER_FORMAT = (os.getenv("WB_STICKER_FORMAT") or "zplv").strip().lower()
BATCH = min(int(os.getenv("WB_LABEL_BATCH", str(STICKER_BATCH))), STICKER_BATCH)

# Пауза кабинету после отказа Wildberries: 2 с, удваивается на каждом отказе
# подряд, не больше минуты. Первый отказ может быть случайностью, десятый —
# нет, и звонить в него каждые полсекунды значит выбирать лимит кабинета
# впустую и заслонять собой все остальные (раздел 6.4).
FAILURE_BACKOFF_S = 2.0
FAILURE_BACKOFF_MAX_S = 60.0
IDLE_SECONDS = float(os.getenv("WB_LABEL_IDLE_SECONDS", "0.5"))


class WbLabelWorker:
    def __init__(self, pool: ConnectionPool, *,
                 only_accounts: Sequence[str] | None = None,
                 sticker_format: str | None = None) -> None:
        self._pool = pool
        self._only = list(only_accounts) if only_accounts else _configured_accounts()
        self._format = (sticker_format or STICKER_FORMAT)
        # Кабинеты на паузе: когда можно звонить снова. Кабинет, чьих заказов
        # Wildberries не знает (заказ отменён у WB, кабинет перепутан), отвечает
        # отказом на каждый вызов. Без паузы его задания набивают собой всю
        # пачку, и стикеры перестают доставаться всем остальным — очередь
        # встаёт головой. Пауза растёт с числом отказов подряд.
        self._cooldown: dict[Any, float] = {}
        self._failures: dict[Any, int] = {}

    def tick(self) -> int:
        now = time.monotonic()
        paused = [account_id for account_id, until in self._cooldown.items() if until > now]
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                pending = repo.tasks_awaiting_labels(
                    cursor, limit=BATCH, only_accounts=self._only,
                    exclude_accounts=paused, modes=_writable_modes())
        if not pending:
            return 0

        by_account: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for task in pending:
            by_account[task["wb_account_id"]].append(task)
        return sum(self._fetch_for_account(account_id, tasks)
                   for account_id, tasks in by_account.items())

    def _fetch_for_account(self, account_id: Any, tasks: list[dict[str, Any]]) -> int:
        account_external = tasks[0]["account_external_id"]
        secret_ref = tasks[0]["secret_ref"]

        # Три вызова к WB на пачку, а не на задание: открыть поставку (один раз
        # на кабинет), положить в неё заказы, запросить стикеры.
        if not self._reserve_calls(account_id, count=3):
            log.info("кабинет %s: окно лимита выбрано, стикеры подождут", account_external)
            return 0

        try:
            with WbClient(account_external_id=account_external,
                          secret_ref=secret_ref) as client:
                supply = self._ensure_supply(client, account_id)
                self._put_into_supply(client, supply, tasks)
                stickers = client.stickers(
                    [int(task["wb_order_id"]) for task in tasks],
                    sticker_format=self._format)
        except (WbError, SecretUnavailable) as failure:
            LABELS_FETCHED.labels(format=self._format, outcome="error").inc(len(tasks))
            # Пауза с ростом: 2, 4, 8… до минуты. Повторять один и тот же
            # отказ каждые полсекунды бессмысленно и вредно — этим кабинет
            # выбирает лимит и заслоняет остальные.
            failures = self._failures.get(account_id, 0) + 1
            self._failures[account_id] = failures
            wait = min(FAILURE_BACKOFF_MAX_S, FAILURE_BACKOFF_S * (2 ** (failures - 1)))
            self._cooldown[account_id] = time.monotonic() + wait
            log.warning("кабинет %s: стикеры не получены (%s), пауза %.0f с (отказов подряд %d)",
                        account_external, failure, wait, failures)
            return 0

        by_order = {sticker.wb_order_id: sticker for sticker in stickers}
        saved = 0
        # Задания, отменённые, пока мы ходили в Wildberries. Их заказы надо
        # освободить из поставки: WB ждёт их в машине, а везти нечего.
        cancelled_meanwhile: list[int] = []
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                for task in tasks:
                    sticker = by_order.get(int(task["wb_order_id"]))
                    if sticker is None or not sticker.payload:
                        continue
                    # Перечитываем под блокировкой: между «спросили стикер» и
                    # «сохранили ответ» прошёл вызов в сеть, и задание успели
                    # отменить. Ответ, записанный вслепую, ВОСКРЕШАЛ такое
                    # задание — снимал `invalidated_at`, и отменённое снова
                    # выглядело готовым к отгрузке.
                    fresh = repo.task_view(cursor, task["id"], for_update=True)
                    if fresh is None or fresh["state"] not in repo.ALIVE_FOR_LABEL:
                        cancelled_meanwhile.append(int(task["wb_order_id"]))
                        log.info("задание %s отменено, пока брали стикер (%s): "
                                 "стикер не сохранён",
                                 task["id"], fresh["state"] if fresh else "нет задания")
                        continue
                    repo.save_label(
                        cursor, task_id=task["id"], payload=sticker.payload,
                        checksum=hashlib.sha256(sticker.payload).hexdigest(),
                        label_format=self._format)
                    saved += 1

        # Вызов в WB — после транзакции (инвариант 2).
        if cancelled_meanwhile and supply.get("wb_supply_id"):
            self._release_cancelled(account_external, secret_ref,
                                    supply["wb_supply_id"], cancelled_meanwhile)
        # Получилось — пауза снимается.
        self._failures.pop(account_id, None)
        self._cooldown.pop(account_id, None)
        LABELS_FETCHED.labels(format=self._format, outcome="ok").inc(saved)
        if saved:
            log.info("кабинет %s: стикеров получено %d из %d заданий",
                     account_external, saved, len(tasks))
        return saved

    def _release_cancelled(self, account_external: str, secret_ref: str,
                           wb_supply_id: str, order_ids: list[int]) -> None:
        """Освобождает из поставки то, что отменили, пока мы брали стикер.

        Иначе WB ждёт эти заказы в машине, а их там нет: поставка приедет
        неполной, и разбирать это будет уже клиент.
        """
        try:
            with WbClient(account_external_id=account_external,
                          secret_ref=secret_ref) as client:
                for order_id in order_ids:
                    client.release_from_supply(wb_supply_id, order_id)
        except (WbError, SecretUnavailable) as failure:
            log.warning("кабинет %s: %d отменённых заказов не освобождены "
                        "из поставки (%s)", account_external, len(order_ids), failure)
            return
        log.info("кабинет %s: из поставки освобождено %d отменённых заказов",
                 account_external, len(order_ids))

    def _ensure_supply(self, client: WbClient, account_id: Any) -> dict[str, Any]:
        """Накопительная поставка кабинета, созданная в WB при первой нужде.

        Стикер выдаётся только заданию в поставке — это требование WB, и оно
        правильное. Неправильно было выполнять его в момент упаковки.
        """
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                supply = repo.open_supply(cursor, account_id)
        if supply["wb_supply_id"]:
            return supply

        wb_supply_id = client.create_supply()
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                repo.bind_supply_to_wb(cursor, supply["id"], wb_supply_id)
        supply["wb_supply_id"] = wb_supply_id
        return supply

    def _put_into_supply(self, client: WbClient, supply: dict[str, Any],
                         tasks: list[dict[str, Any]]) -> None:
        fresh = [task for task in tasks if task["supply_id"] is None]
        if not fresh:
            return
        client.add_orders(supply["wb_supply_id"],
                          [int(task["wb_order_id"]) for task in fresh])
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                repo.attach_tasks_to_supply(cursor, [task["id"] for task in fresh],
                                            supply["id"])

    def _reserve_calls(self, account_id: Any, *, count: int) -> bool:
        """Место в минутном окне кабинета под всю пачку сразу.

        Лимит общий на кабинет и делится с опросом заданий: занять его молча,
        а потом упереться в 429 посреди пачки — значит оставить часть заданий
        в поставке без стикеров.
        """
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                return bool(rate_limit.take(cursor, account_id, cost=count))


def _writable_modes() -> tuple[str, ...]:
    """Режимы кабинетов, которым сейчас разрешено писать в Wildberries."""
    return ("live", "shadow") if writes_allowed("shadow") else ("live",)


def _configured_accounts() -> list[str]:
    raw = os.getenv("WB_SYNC_ONLY_ACCOUNTS", "")
    return [value.strip() for value in raw.split(",") if value.strip()]


def main() -> None:
    configure_logging()
    worker = WbLabelWorker(shared_pool())
    Worker("wb_labels", idle_seconds=IDLE_SECONDS).run(worker.tick)


if __name__ == "__main__":
    main()
