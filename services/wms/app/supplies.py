"""Поставки Wildberries: то, что делается снаружи транзакции.

Пока здесь только освобождение заказа из поставки — оно нужно отмене задания
(раздел 6.6): отменённый заказ обязан выйти из поставки, иначе он уедет в
машине, а клиент получит отмену и отгрузку одновременно.

Вызов уходит ПОСЛЕ коммита: транзакция отмены уже закрыта, товар уже вернулся
на полку. Если Wildberries в этот момент недоступен, склад от этого не встаёт —
задание отменено, а расхождение поймает сверка.
"""
from __future__ import annotations

from datetime import datetime, timezone

import logging
from typing import Any, Callable

from . import repositories as repo
from .postgres import ConnectionPool, single
from .secrets import SecretUnavailable, provider
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


def verify_account(pool: ConnectionPool, account_id: str) -> dict[str, Any]:
    """Проверка кабинета: отвечает ли Wildberries нашим секретом.

    Отдельная роль по разделу 12 — управление кабинетами. Проверка честно
    перечисляет, что именно проверено: разрешилась ли ссылка на секрет и
    ответил ли WB на самый безобидный запрос (`GET /api/v3/orders`, один
    заказ). Записи в кабинет не делается ни в каком режиме — проверка не имеет
    права ничего создать.

    На стенде живых токенов нет вовсе, и отвечает симулятор. Рисовать зелёную
    галочку «токен верный» в такой ситуации было бы враньём, поэтому в ответе
    видно, с чем именно разговаривали.
    """
    import uuid as _uuid

    with pool.connection() as connection:
        with single(connection) as cursor:
            try:
                account = repo.find_account(cursor, account_id=_uuid.UUID(str(account_id)))
            except (ValueError, AttributeError):
                account = repo.find_account(cursor, external_id=str(account_id))
    if account is None:
        raise ValueError(f"кабинет {account_id} не заведён")

    checks: list[dict[str, Any]] = []
    secret_ok = True
    try:
        provider().resolve(account["secret_ref"])
    except SecretUnavailable as failure:
        secret_ok = False
        checks.append({"name": "secret_ref", "passed": False, "detail": str(failure)})
    if secret_ok:
        checks.append({"name": "secret_ref", "passed": True,
                       "detail": "ссылка разрешилась в провайдере"})

    reachable, detail = False, "не проверяли: секрет не разрешён"
    if secret_ok:
        try:
            with WbClient(account_external_id=account["external_id"],
                          secret_ref=account["secret_ref"]) as client:
                client.orders(cursor=0, limit=1)
            reachable, detail = True, "GET /api/v3/orders ответил"
        except (WbError, SecretUnavailable) as failure:
            detail = str(failure)
    checks.append({"name": "marketplace_scope", "passed": reachable, "detail": detail})

    verified = secret_ok and reachable
    with pool.connection() as connection:
        with single(connection) as cursor:
            repo.mark_account_verified(cursor, account["id"], verified=verified)
    # Форма — WbAccountVerifyResult. Итог проверки читается из `status` и
    # `checks`; отдельного `verified` контракт не знает, а `mode` кабинета
    # отдаётся списком в /wb/accounts.
    return {"account_id": str(account["id"]), "external_id": account["external_id"],
            "owner_external_id": account.get("seller_external_id"),
            "status": "ACTIVE" if verified else account["status"],
            "scopes": account.get("scopes") or [], "checks": checks,
            "verified_at": datetime.now(timezone.utc).isoformat()}
