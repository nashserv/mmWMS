"""Поставки Wildberries: то, что делается снаружи транзакции.

Пока здесь только освобождение заказа из поставки — оно нужно отмене задания
(раздел 6.6): отменённый заказ обязан выйти из поставки, иначе он уедет в
машине, а клиент получит отмену и отгрузку одновременно.

Вызов уходит ПОСЛЕ коммита: транзакция отмены уже закрыта, товар уже вернулся
на полку. Если Wildberries в этот момент недоступен, склад от этого не встаёт —
задание отменено, а расхождение поймает сверка.
"""
from __future__ import annotations

import logging
from typing import Callable

from . import repositories as repo
from .postgres import ConnectionPool, single
from .secrets import SecretUnavailable
from .wb import WbClient, WbError, writes_allowed

log = logging.getLogger("wms.supplies")


def release_from_supply(pool: ConnectionPool) -> Callable[[str, int], None]:
    """Возвращает функцию «освободить заказ из поставки WB»."""

    def release(wb_supply_id: str, wb_order_id: int) -> None:
        with pool.connection() as connection:
            with single(connection) as cursor:
                account = repo.account_of_supply(cursor, wb_supply_id)
        if account is None or not writes_allowed(account["mode"]):
            return
        try:
            with WbClient(account_external_id=account["external_id"],
                          secret_ref=account["secret_ref"]) as client:
                client.release_from_supply(wb_supply_id, wb_order_id)
        except (WbError, SecretUnavailable) as failure:
            # Не роняем отмену: задание уже отменено, товар уже на полке.
            # Расхождение с WB поймает сверка и разбудит человека (инвариант 10).
            log.warning("заказ %s не освобождён из поставки %s (%s)",
                        wb_order_id, wb_supply_id, failure)

    return release
