"""Поставки Wildberries: собрать, закрыть, передать, подтвердить.

Здесь живёт различие, из-за которого в боевом контуре подтверждённых передач
меньше одного процента (раздел 10). `complete` у Wildberries приёмку **не
доказывает** (раздел 2.12), поэтому:

  * `hand_over` ставит `handed_to_wb` и требует подписи живого человека —
    схема не даёт перевести поставку в это состояние без неё;
  * `reconcile` ставит `accepted_by_wb` по результату сверки с WB.

Это два разных факта, и мешать их нельзя: первый говорит «мы отдали», второй —
«они взяли».

Все вызовы в Wildberries уходят вне транзакции (инвариант 2): транзакция здесь
только записывает решение, а разговор с WB идёт до или после неё.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from . import rate_limit, repositories as repo
from .domain import TaskState, check_transition, now
from .postgres import ConnectionPool, single, transaction
from .secrets import SecretUnavailable
from .service import WmsService
from .wb import SUPPLY_ORDERS_BATCH, WbClient, WbError, writes_allowed

log = logging.getLogger("wms.shipments")

ACTIONS = ("open", "add_orders", "close", "deliver", "hand_over", "reconcile")


class ShipmentOperations:
    def __init__(self, pool: ConnectionPool, service: WmsService) -> None:
        self._pool = pool
        self._service = service

    def handle(self, params: dict[str, Any]) -> dict[str, Any]:
        action = str(params.get("action") or "").strip()
        if action not in ACTIONS:
            raise ValueError(f"action: одно из {', '.join(ACTIONS)}")
        seller = str(params.get("seller_external_id") or "").strip()
        if not seller:
            raise ValueError("seller_external_id обязателен")
        key = _text(params.get("idempotency_key"))
        if not key:
            raise ValueError("idempotency_key обязателен: это ключ идемпотентности")

        # Ключ занимается ДО работы. Раньше он только проверялся на непустоту:
        # повтор `deliver` находил те же задания собранными, отгружал их
        # второй раз и публиковал второе `wb.supply.shipped.v1` — то есть
        # второй счёт клиенту за ту же машину (инвариант 5).
        scope = f"shipment:{action}:{seller}:{key}"
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                seen = repo.claim_command(cursor, idempotency_key=scope,
                                          command=f"shipment.{action}")
        if seen is not None:
            return _repeat_of(seen, action)

        try:
            result = getattr(self, f"_{action}")(seller, params)
        except Exception:
            # Ключ освобождается: отказ — не выполненная команда, и повторить
            # её тем же ключом обязано быть можно.
            with self._pool.connection() as connection:
                with transaction(connection) as cursor:
                    repo.forget_command(cursor, idempotency_key=scope)
            raise
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                repo.save_command_result(
                    cursor, idempotency_key=scope,
                    aggregate_id=_uuid_or_none(result.get("shipment_id")),
                    result=result)
        return result

    # ----------------------------------------------------------- открытие

    def _open(self, seller: str, params: dict[str, Any]) -> dict[str, Any]:
        """Открывает поставку кабинета. Одна открытая на кабинет (приложение D)."""
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                owner, account = self._owner_and_account(cursor, seller)
                supply = repo.open_supply(cursor, account["id"])
                shipment = repo.ensure_shipment(cursor, owner_id=owner["id"],
                                                supply_id=supply["id"])
        if not supply["wb_supply_id"] and writes_allowed(account["mode"]):
            wb_supply_id = self._create_in_wb(account, supply["id"])
            supply["wb_supply_id"] = wb_supply_id
        return self._view(shipment, supply, seller, orders=0)

    def _create_in_wb(self, account: dict[str, Any], supply_id: uuid.UUID) -> str | None:
        """Создаёт поставку в WB — вне транзакции, как и любой вызов наружу."""
        try:
            with WbClient(account_external_id=account["external_id"],
                          secret_ref=account["secret_ref"]) as client:
                wb_supply_id = client.create_supply()
        except (WbError, SecretUnavailable) as failure:
            log.warning("кабинет %s: поставка не создана в WB (%s)",
                        account["external_id"], failure)
            return None
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                repo.bind_supply_to_wb(cursor, supply_id, wb_supply_id)
        return wb_supply_id

    # ------------------------------------------------------------ наполнение

    def _add_orders(self, seller: str, params: dict[str, Any]) -> dict[str, Any]:
        task_ids = [_uuid(value) for value in (params.get("task_ids") or [])]
        if not task_ids:
            raise ValueError("task_ids обязательны")
        if len(task_ids) > SUPPLY_ORDERS_BATCH:
            raise ValueError(f"не более {SUPPLY_ORDERS_BATCH} заданий за вызов (приложение D)")

        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                owner, account = self._owner_and_account(cursor, seller)
                supply = repo.open_supply(cursor, account["id"])
                shipment = repo.ensure_shipment(cursor, owner_id=owner["id"],
                                                supply_id=supply["id"])
                fresh = repo.tasks_not_in_supply(cursor, task_ids, supply["id"])
                repo.attach_tasks_to_supply(cursor, task_ids, supply["id"])
                orders = repo.supply_order_count(cursor, supply["id"])

        if fresh and supply["wb_supply_id"] and writes_allowed(account["mode"]):
            self._push_orders(account, supply["wb_supply_id"],
                              [int(row["wb_order_id"]) for row in fresh])
        return self._view(shipment, supply, seller, orders=orders)

    def _push_orders(self, account: dict[str, Any], wb_supply_id: str,
                     order_ids: list[int]) -> None:
        try:
            with WbClient(account_external_id=account["external_id"],
                          secret_ref=account["secret_ref"]) as client:
                for chunk in _chunks(order_ids, SUPPLY_ORDERS_BATCH):
                    client.add_orders(wb_supply_id, chunk)
        except (WbError, SecretUnavailable) as failure:
            log.warning("кабинет %s: заказы не добавлены в поставку (%s)",
                        account["external_id"], failure)

    # ------------------------------------------------ закрытие и передача

    def _close(self, seller: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                owner, account = self._owner_and_account(cursor, seller)
                supply = repo.supply_of_account(cursor, account["id"],
                                                _text(params.get("wb_supply_id")))
                shipment = repo.ensure_shipment(cursor, owner_id=owner["id"],
                                                supply_id=supply["id"])
                repo.close_supply(cursor, supply["id"])
                repo.set_shipment_state(cursor, shipment["id"], "closed")
                orders = repo.supply_order_count(cursor, supply["id"])
                shipment["state"], shipment["closed_at"] = "closed", now()
        return self._view(shipment, supply, seller, orders=orders)

    def _deliver(self, seller: str, params: dict[str, Any]) -> dict[str, Any]:
        """Передать поставку в WB.

        Уезжает то, что физически собрано. Накопительная поставка кабинета
        (раздел 6.6) держит и задания, которым стикер уже вытянут, но которые
        ещё лежат на полке: стикер выдаётся только заданию в поставке, поэтому
        они попадают туда сразу после резерва. Везти их нельзя — их никто не
        собирал, — и в момент передачи они освобождаются из поставки и ждут
        следующей. Это ровно то, для чего у WB есть `release_from_supply`.

        `orders` в событии — число уехавших заданий: на нём стоит счёт клиенту
        (приложение E), и приписать туда несобранное значит выставить счёт за
        то, чего не везли.

        С релизов 2026-03/04 перед `deliver` обязательна валидация
        `metaDetails` (приложение D). Практически она означает: у уезжающего
        задания есть штрихкод и действующий стикер.
        """
        left_behind: list[int] = []
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                owner, account = self._owner_and_account(cursor, seller)
                supply = repo.supply_of_account(cursor, account["id"],
                                                _text(params.get("wb_supply_id")))
                shipment = repo.ensure_shipment(cursor, owner_id=owner["id"],
                                                supply_id=supply["id"])
                assembled = repo.assembled_tasks_of_supply(cursor, supply["id"])
                if not assembled:
                    raise ValueError(
                        "в поставке нет ни одного собранного задания: везти нечего")

                incomplete = [task for task in assembled if not task["ready_metadata"]]
                if incomplete:
                    raise ValueError(
                        f"валидация metaDetails не пройдена: {len(incomplete)} заданий "
                        f"без стикера или штрихкода — WB откажет в передаче")

                # Несобранное возвращается в очередь: следующая поставка его
                # заберёт, а эта уедет только с тем, что лежит в коробе.
                waiting = repo.unassembled_tasks_of_supply(cursor, supply["id"])
                for task in waiting:
                    repo.set_task_state(cursor, task["id"], task["state"], clear_supply=True)
                    left_behind.append(int(task["wb_order_id"]))

                for task in assembled:
                    self._consume_reservation(cursor, task, supply)
                    repo.set_task_state(cursor, task["id"], "shipped")
                repo.close_supply(cursor, supply["id"], state="delivered")
                repo.set_shipment_state(cursor, shipment["id"], "closed")
                orders = len(assembled)

                # Тарифицируемое событие: на `orders` встаёт счёт клиенту
                # (приложение E). Пишется в ту же транзакцию, что и отгрузка.
                self._service.emit_for_aggregate(
                    cursor, aggregate_id=shipment["id"],
                    event_type="wb.supply.shipped.v1",
                    payload={"supply": supply["wb_supply_id"] or str(supply["id"]),
                             "seller_id": seller, "orders": orders,
                             "accepted_at": None,
                             "name": f"Поставка {supply['wb_supply_id'] or supply['id']}"},
                    correlation_id=str(params.get("idempotency_key")))
                shipment["state"] = "closed"

        if supply["wb_supply_id"] and writes_allowed(account["mode"]):
            self._release_left_behind(account, supply["wb_supply_id"], left_behind)
            self._deliver_in_wb(account, supply["wb_supply_id"])
        return self._view(shipment, supply, seller, orders=orders)

    def _consume_reservation(self, cursor: Any, task: dict[str, Any],
                             supply: dict[str, Any]) -> None:
        """Товар уехал: журнал обязан это увидеть (инвариант 3).

        До этой правки `deliver` не писал ни одного движения. Задание
        закрывалось в `shipped`, а резерв оставался `held` навсегда: в
        `stock_balance` вечно висел `reserved` под заказ, который уже уехал, а
        `available = good − buffer` считался по складу, половина которого
        физически отсутствовала. Состояние `consumed` стояло в схеме с первого
        дня и не использовалось нигде.

        Движение пишется со стороной `to` пустой — именно так журнал
        записывает уход товара со склада (миграция 004). Раскладка берётся из
        движений резерва: уехало ровно то и оттуда, откуда его сняли.
        """
        reservation = repo.held_reservation(cursor, task["id"], for_update=True)
        if reservation is None:
            # Резерва нет — снят отменой или собран по клапану «без остатка».
            # Отгрузка от этого не останавливается, но молчать тут нельзя.
            log.warning("задание %s уезжает без действующего резерва: "
                        "движения выхода не записаны", task["id"])
            return
        doc_ref = supply["wb_supply_id"] or str(supply["id"])
        for index, move in enumerate(repo.reservation_moves(cursor, reservation["id"])):
            repo.insert_move(
                cursor, owner_id=reservation["owner_id"], sku_id=reservation["sku_id"],
                qty=int(move["qty"]),
                cell_from=move["cell_to"], box_from=move["box_to"],
                state_from="reserved",
                # Пусто со стороны `to`: товара на складе больше нет.
                cell_to=None, box_to=None, state_to=None,
                reason="shipment", doc_type="shipment", doc_ref=doc_ref,
                idem_key=f"ship:{reservation['id']}:{index}")
        repo.consume_reservation(cursor, reservation["id"])

    def _release_left_behind(self, account: dict[str, Any], wb_supply_id: str,
                             order_ids: list[int]) -> None:
        """Освобождает из поставки то, что осталось на складе.

        Иначе WB ждёт эти заказы в машине, а их там нет: поставка приедет
        неполной, и разбирать это будет уже клиент.
        """
        if not order_ids:
            return
        try:
            with WbClient(account_external_id=account["external_id"],
                          secret_ref=account["secret_ref"]) as client:
                for order_id in order_ids:
                    client.release_from_supply(wb_supply_id, order_id)
        except (WbError, SecretUnavailable) as failure:
            log.warning("кабинет %s: %d заданий не освобождены из поставки (%s)",
                        account["external_id"], len(order_ids), failure)

    def _deliver_in_wb(self, account: dict[str, Any], wb_supply_id: str) -> None:
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                permitted = bool(rate_limit.take(cursor, account["id"]))
        if not permitted:
            log.info("кабинет %s: окно лимита выбрано, передача поставки подождёт",
                     account["external_id"])
            return
        try:
            with WbClient(account_external_id=account["external_id"],
                          secret_ref=account["secret_ref"]) as client:
                client.deliver(wb_supply_id)
        except WbError as failure:
            if failure.conflict:
                # 409 у WB значит «уже передана». Сверка разберётся, повторять
                # не надо: 409 стоит десять обычных вызовов (приложение D).
                log.info("поставка %s уже передана", wb_supply_id)
                return
            log.warning("поставка %s не передана (%s)", wb_supply_id, failure)
        except SecretUnavailable as failure:
            log.warning("поставка %s не передана (%s)", wb_supply_id, failure)

    def _hand_over(self, seller: str, params: dict[str, Any]) -> dict[str, Any]:
        """`HANDED_TO_WB` ставит только живой человек.

        Статус `complete` у Wildberries приёмку не доказывает (раздел 2.12).
        Без подписи состояние не меняется — и схема этого тоже не даст.
        """
        handed_by = _text(params.get("handed_over_by"))
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                owner, account = self._owner_and_account(cursor, seller)
                supply = repo.supply_of_account(cursor, account["id"],
                                                _text(params.get("wb_supply_id")))
                shipment = repo.ensure_shipment(cursor, owner_id=owner["id"],
                                                supply_id=supply["id"])
                orders = repo.supply_order_count(cursor, supply["id"])
                if not handed_by:
                    # Не ошибка протокола, а отказ по существу: поставка
                    # остаётся в прежнем состоянии, и клиент видит это по нему.
                    # Схема ответа закрыта (additionalProperties: false), лишнего
                    # поля с объяснением в неё не добавить.
                    return self._view(shipment, supply, seller, orders=orders)
                if shipment["state"] == "handed_to_wb":
                    result = self._view(shipment, supply, seller, orders=orders)
                    result["duplicate"] = True
                    return result
                # Подписать можно только то, что уехало. Раньше `hand_over`
                # работал по открытой поставке: человек подтверждал передачу
                # машины, которую ещё не собрали, и `handed` вставал у заданий,
                # лежащих на полке.
                if supply["state"] != "delivered":
                    raise ValueError(
                        f"поставка в состоянии {supply['state']!r}: подтвердить "
                        f"передачу можно только после deliver")
                repo.hand_over_shipment(cursor, shipment["id"], handed_by)
                for task in repo.tasks_of_supply(cursor, supply["id"]):
                    check_transition("hand_over", task["state"])
                    repo.set_task_state(cursor, task["id"], "handed")
                shipment.update({"state": "handed_to_wb", "handed_by": handed_by,
                                 "handed_at": now()})
        return self._view(shipment, supply, seller, orders=orders)

    def _reconcile(self, seller: str, params: dict[str, Any]) -> dict[str, Any]:
        """`ACCEPTED_BY_WB` — только по фактическому ответу Wildberries.

        Раньше команда просто ставила `accepted` всем заданиям поставки: то
        есть подтверждала приёмку сама себе. Именно так в боевом контуре 6072
        задания оказались в терминальном успехе, ничего не доказав.

        Теперь статусы спрашиваются у WB поимённо, и `accepted` встаёт, только
        если WB отвечает `complete` по КАЖДОМУ заказу. Не отвечает — отказ с
        перечислением того, что не сошлось; задания остаются в `handed`, а
        разбор идёт к человеку.
        """
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                owner, account = self._owner_and_account(cursor, seller)
                supply = repo.supply_of_account(cursor, account["id"],
                                                _text(params.get("wb_supply_id")))
                tasks = repo.tasks_of_supply(cursor, supply["id"])

        # Вызов в WB — вне транзакции (инвариант 2).
        confirmed = self._accepted_at_wb(account, tasks)

        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                shipment = repo.ensure_shipment(cursor, owner_id=owner["id"],
                                                supply_id=supply["id"])
                orders = repo.supply_order_count(cursor, supply["id"])
                repo.accept_shipment(cursor, shipment["id"])
                for task in tasks:
                    if task["state"] == TaskState.ACCEPTED.value:
                        continue
                    check_transition("reconcile", task["state"])
                    repo.set_task_state(cursor, task["id"], "accepted")
                shipment.update({"state": "accepted_by_wb", "accepted_at": now()})
        del confirmed
        return self._view(shipment, supply, seller, orders=orders)

    def _accepted_at_wb(self, account: dict[str, Any],
                        tasks: list[dict[str, Any]]) -> set[int]:
        """Какие заказы поставки Wildberries действительно считает уехавшими.

        Отсутствие ответа — не согласие. Если спросить не удалось или хотя бы
        один заказ не `complete`, приёмка не подтверждается: `accepted` —
        терминальный успех, и ставить его по молчанию нельзя.
        """
        order_ids = [int(task["wb_order_id"]) for task in tasks
                     if task.get("wb_order_id") is not None]
        if not order_ids:
            raise ValueError("в поставке нет заданий: подтверждать приёмку нечему")
        try:
            with WbClient(account_external_id=account["external_id"],
                          secret_ref=account["secret_ref"]) as client:
                statuses = client.orders_status(order_ids)
        except (WbError, SecretUnavailable) as failure:
            raise ValueError(
                f"Wildberries не ответил о статусах поставки ({failure}): "
                f"приёмка не подтверждается по молчанию") from None
        unconfirmed = [order_id for order_id in order_ids
                       if statuses.get(order_id) != "complete"]
        if unconfirmed:
            raise ValueError(
                f"Wildberries не подтвердил приёмку {len(unconfirmed)} заказов "
                f"(первый — {unconfirmed[0]}, у WB "
                f"{statuses.get(unconfirmed[0]) or 'нет такого заказа'!r}): "
                f"ACCEPTED_BY_WB ставится только по фактическому complete")
        return set(order_ids)

    # ------------------------------------------------------------ служебное

    def picked(self, params: dict[str, Any]) -> dict[str, Any]:
        """Собранные задания, ждущие поставки."""
        limit = min(int(params.get("limit") or 100), 500)
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                rows = repo.tasks_ready_for_supply(
                    cursor, owner_external_id=_text(params.get("seller_external_id")),
                    limit=limit)
        from .tasks import _projection
        return {"tasks": [_projection(row) for row in rows], "next_cursor": None}

    def _owner_and_account(self, cursor: Any, seller: str) -> tuple[dict, dict]:
        owner = repo.find_owner(cursor, seller)
        if owner is None:
            raise ValueError(f"продавец {seller!r} не заведён")
        account = repo.sole_account_of_owner(cursor, owner["id"])
        if account is None:
            accounts = repo.accounts_of_owner(cursor, owner["id"])
            if not accounts:
                raise ValueError(f"у продавца {seller!r} нет кабинета Wildberries")
            account = accounts[0]
        return owner, account

    @staticmethod
    def _view(shipment: dict[str, Any], supply: dict[str, Any], seller: str, *,
              orders: int) -> dict[str, Any]:
        return {
            "shipment_id": str(shipment["id"]),
            "owner_external_id": seller,
            "wb_supply_id": supply.get("wb_supply_id"),
            "state": shipment["state"],
            # Строкой, а не uuid: схема ответа описывает поле строкой, и
            # json.dumps об uuid не знает.
            "handed_by": _str_or_none(shipment.get("handed_by")),
            "handed_at": _isoformat(shipment.get("handed_at")),
            "orders": orders,
            "closed_at": _isoformat(shipment.get("closed_at")),
            "accepted_at": _isoformat(shipment.get("accepted_at")),
            "duplicate": False,
        }


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        raise ValueError(f"{value!r} не похоже на идентификатор задания") from None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _repeat_of(seen: dict[str, Any], action: str) -> dict[str, Any]:
    """Ответ на повтор команды: тот же самый, с пометкой `duplicate`.

    Ответа может не быть: первая попытка идёт прямо сейчас, в соседней
    транзакции. Отвечать «сделано» нечем и делать работу второй раз нельзя —
    это отказ, который клиент повторит позже тем же ключом.
    """
    stored = seen.get("result")
    if not stored:
        raise ValueError(
            f"команда {action} с этим idempotency_key уже выполняется: "
            f"повторите запрос позже тем же ключом")
    result = dict(stored)
    result["duplicate"] = True
    return result


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)
