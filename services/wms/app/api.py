"""Mock сервиса wms: те же маршруты, что в контракте, фиксированные ответы.

Существует ради одного правила: никто никого не ждёт (правило 9.5.4). Потоки B
и C пишут клиентов и консьюмеров против этого mock, пока поток A строит
настоящий сервис. Когда поток A готов — mock снимается, контракт остаётся.

Здесь НЕТ бизнес-логики. Всё, что похоже на решение (резерв прошёл, маппинг не
найден, клапан сработал), выбирается по фикстуре, а не вычисляется.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import fixtures
from .config import app_environment, trusted_hosts
from .domain import ErrorCode, EventEnvelope, TaskState, now, uid
from .events import EventPublisher
from .state import MockState

BASE_PATH = "/api/mmx/wms/v1"
TENANT_ID = os.getenv("MMX_TENANT_ID", "mm-express")
# Оператор стенда. В настоящем сервисе сюда приедет пользователь из identity;
# контракт требует actor_id у событий физического действия, и заглушка не
# имеет права отдавать null там, где поток B ждёт человека.
STAND_ACTOR_ID = "0a000000-0000-4000-8000-00000000000a"

publisher = EventPublisher()
state = MockState()
router = APIRouter(prefix=BASE_PATH)


def _params(body: Any) -> dict[str, Any]:
    """Достаёт params из конверта JSON-RPC.

    Конверт принимается и в полном виде, и без него: клиенты потока B ещё
    пишутся, и спотыкаться о форму конверта на этом этапе — потеря времени.
    """
    if not isinstance(body, dict):
        return {}
    if "params" in body and isinstance(body["params"], dict):
        return body["params"]
    return body


def _result(request_body: Any, payload: Any) -> JSONResponse:
    request_id = request_body.get("id", 1) if isinstance(request_body, dict) else 1
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": payload})


async def _body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return {}


def _emit(event_type: str, payload: dict[str, Any], correlation_id: str) -> None:
    publisher.publish(EventEnvelope(
        type=event_type, tenant_id=TENANT_ID, payload=payload, correlation_id=correlation_id))


# ---------------------------------------------------------------- служебное

@router.post("/health")
async def health(request: Request) -> JSONResponse:
    return _result(await _body(request), {"status": "ok", "mock": True})


# ---------------------------------------------------------------- справочники

@router.post("/sellers")
async def sellers(request: Request) -> JSONResponse:
    """Список владельцев, а с параметрами — ещё и заведение нового.

    Клиента заводят на ходу (шаг 1 прогона), и заведённый обязан быть виден
    резерву. Пока маршрут только читал, любой новый продавец получал
    SELLER_MAPPING_MISSING — и весь прогон вставал на первом же шаге.
    """
    body = await _body(request)
    params = _params(body)
    seller = params.get("seller_external_id")
    created = None
    if seller:
        created = state.register_owner(
            str(seller), name=params.get("name"), inn=params.get("inn"),
            allow_ledger_short=params.get("allow_ledger_short"))
    return _result(body, {"sellers": state.owners(), "seller": created})


@router.post("/catalog/products")
async def catalog_products(request: Request) -> JSONResponse:
    body = await _body(request)
    seller = _params(body).get("seller_external_id")
    return _result(body, {"products": state.products(seller)})


@router.post("/catalog/products/ensure")
async def catalog_products_ensure(request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    seller = str(params.get("seller_external_id", ""))
    barcode = str(params.get("barcode", ""))
    if not seller or not barcode:
        return _result(body, {"status": "rejected", "error_code": "SELLER_MAPPING_MISSING"})

    # Товар заводится у владельца: изоляция владельца доходит до каталога
    # (инвариант 6), одинаковый штрихкод у двух клиентов — разные вещи.
    state.register_owner(seller)
    product, created = state.register_product(
        seller, barcode, seller_sku=params.get("seller_sku"), name=params.get("name"))
    return _result(body, {
        "product_id": product["sku_id"], "sku_id": product["sku_id"],
        "barcode": barcode, "seller_external_id": seller, "created": created,
    })


@router.post("/catalog/stocks")
async def catalog_stocks(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {"stocks": state.stocks_for(_params(body).get("seller_external_id"))})


@router.post("/catalog/stocks/bulk")
async def catalog_stocks_bulk(request: Request) -> JSONResponse:
    body = await _body(request)
    rows = state.stocks_for(_params(body).get("seller_external_id"))
    # Строки без штрихкода или с нецелым/отрицательным available отбрасываются
    # (приложение C). Заниженный остаток — норма, отрицательный — нет.
    clean = [r for r in rows
             if r.get("barcode") and isinstance(r.get("available"), int) and r["available"] >= 0]
    return _result(body, {"stocks": clean})


# ---------------------------------------------------------------- склад

@router.post("/warehouse/documents")
async def warehouse_documents(request: Request) -> JSONResponse:
    """Складской документ: со строками — применяет, без них — перечисляет.

    Начальный остаток при переключении клиента даёт владелец компании
    (решение владельца 11), поэтому он заезжает документом с doc_type='opening'
    и оставляет след, а не появляется на складе сам.
    """
    body = await _body(request)
    params = _params(body)
    lines = params.get("lines")
    reference = params.get("reference")
    if not lines or not reference:
        return _result(body, {"documents": state.documents})

    seller = str(params.get("seller_external_id", ""))
    state.register_owner(seller)
    document = state.apply_document(
        seller_external_id=seller, reference=str(reference),
        doc_type=str(params.get("doc_type", "adjustment")),
        lines=lines, comment=params.get("comment"))
    return _result(body, document)


@router.post("/warehouse/stock")
async def warehouse_stock(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {"stock": state.stocks_for(_params(body).get("seller_external_id"))})


@router.post("/storage/lookup")
async def storage_lookup(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {"placements": state.lookup(_params(body).get("barcode"))})


@router.post("/storage/count")
async def storage_count(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {"counted": True, "reference": _params(body).get("reference")})


# ---------------------------------------------------------------- резерв

@router.post("/reservations")
async def reservations(request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    seller = str(params.get("seller_external_id", ""))
    barcode = str(params.get("barcode") or params.get("sku") or "")
    correlation_id = str(params.get("correlation_id") or uid())

    quantity = int(params.get("quantity", 1))
    wb_order_id = params.get("wb_order_id")
    failure = (correlation_id, wb_order_id, barcode, quantity)

    if not state.owner_is_known(seller):
        return _result(body, _reservation_error(ErrorCode.SELLER_MAPPING_MISSING, *failure))

    if barcode == fixtures.AMBIGUOUS_BARCODE:
        # Два совпадения — это AMBIGUOUS, а не «берём первое» (раздел 3.2).
        return _result(body, _reservation_error(ErrorCode.AMBIGUOUS_PRODUCT_MAPPING, *failure))

    product = state.product(seller, barcode)
    if product is None:
        return _result(body, _reservation_error(ErrorCode.PRODUCT_MAPPING_MISSING, *failure))

    good = state.good_qty(seller, barcode)
    ledger_short = good < quantity

    if ledger_short and not state.allows_ledger_short(seller):
        return _result(body, _reservation_error(ErrorCode.INSUFFICIENT_STOCK, *failure))

    task = state.reserve(
        seller_external_id=seller, barcode=barcode, quantity=quantity,
        wb_order_id=params.get("wb_order_id"), deadline=params.get("deadline"))

    # Каждому событию — свой номер. Номер берётся у задания в момент эмиссии,
    # а не переиспользуется: потребители полагаются на строгую монотонность
    # sequence в пределах задания (приложение E).
    if ledger_short:
        # Клапан «собрать без остатка»: смена не встаёт, но след остаётся
        # (раздел 6.5). Молчаливого _force_reservation больше нет.
        _emit("wms.stock.shortfall.v1", {
            "owner_id": task["owner_id"], "sku_id": task["sku_id"],
            "cell_id": task["cell_id"], "qty_short": quantity - max(0, good),
            "task_id": task["task_id"],
            # sequence сюда не кладём: контракт задаёт этому payload ровно пять
            # полей и additionalProperties: false (пункт 3 файла 01).
        }, correlation_id)

    _emit("wms.reservation.succeeded.v1", {
        "task_id": task["task_id"], "reservation_id": task["reservation_id"],
        "owner_id": task["owner_id"], "sku_id": task["sku_id"], "qty": quantity,
        "wb_order_id": task["wb_order_id"],
        "sequence": state.next_sequence(task["task_id"]),
    }, correlation_id)

    return _result(body, {
        "status": "reserved",
        "task_id": task["task_id"],
        "owner_external_id": seller,
        "error_code": None,
        "ledger_short": ledger_short,
    })


def _reservation_error(code: ErrorCode, correlation_id: str, wb_order_id: Any,
                       barcode: str, quantity: int) -> dict[str, Any]:
    """Отказ резерва — тоже событие.

    Задания ещё нет, поэтому номер события ведётся по заказу WB: у отказа всё
    равно должен быть свой порядок, иначе повторный опрос неотличим от нового
    отказа.
    """
    # Дублирующего поля reason здесь нет: схема стоит с additionalProperties:
    # false, а код отказа сам себя объясняет.
    _emit("wms.reservation.failed.v1", {
        "wb_order_id": int(wb_order_id), "barcode": barcode, "quantity": quantity,
        "error_code": code.value,
        "sequence": state.next_sequence(f"wb-order-{wb_order_id}"),
    }, correlation_id)
    return {"status": "rejected", "task_id": None,
            "owner_external_id": None, "error_code": code.value}


# ---------------------------------------------------------------- задания
# /tasks/pull регистрируется ДО /tasks/{task_id}: иначе параметр пути
# проглотит слово pull и рабочее место получит «задание с id pull».

@router.post("/tasks/pull")
async def tasks_pull(request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    limit = int(params.get("limit", 10))
    available_before = state.available_for_pull()
    tasks = state.pull(assignee=params.get("assignee"), limit=limit)
    # Форма ответа — по схеме TasksPullResult: каждое задание приходит вместе
    # со сроком своего лизинга, чтобы рабочее место знало, когда задание
    # вернётся в очередь, если сборщик пропал.
    return _result(body, {
        "tasks": [{"task": task, "leased_until": task["claim_expires_at"]} for task in tasks],
        "served_at": now(),
        "available_total": max(0, available_before - len(tasks)),
    })


@router.post("/tasks/{task_id}")
async def task_read(task_id: str, request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, state.task(task_id))


@router.post("/tasks/{task_id}/scan")
async def task_scan(task_id: str, request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    barcode = str(params.get("barcode", ""))
    task = state.task(task_id)

    if not task:
        return _result(body, {"status": "not_found", "owner_external_id": None})

    # Контрольный скан — главный рубеж качества (раздел 4). Чужой штрихкод
    # отклоняется, и отказ виден, а не тонет.
    if barcode != task.get("barcode"):
        _emit("wms.item.scanned.v1", {
            "task_id": task_id, "owner_id": task["owner_id"], "sku_id": task["sku_id"],
            "barcode": barcode, "qty": int(task["quantity"]),
            "scan_result": "wrong_barcode", "actor_id": STAND_ACTOR_ID,
            "sequence": state.next_sequence(task_id),
        }, str(params.get("correlation_id") or uid()))
        return _result(body, {"status": "rejected", "scan_result": "wrong_barcode",
                              "owner_external_id": task["owner_external_id"]})

    state.set_state(task_id, TaskState.PICKED)
    _emit("wms.item.scanned.v1", {
        "task_id": task_id, "owner_id": task["owner_id"], "sku_id": task["sku_id"],
        "barcode": barcode, "qty": int(task["quantity"]),
        "scan_result": "ok", "actor_id": STAND_ACTOR_ID,
        "sequence": state.next_sequence(task_id),
    }, str(params.get("correlation_id") or uid()))
    return _result(body, {"status": "picked", "scan_result": "ok",
                          "owner_external_id": task["owner_external_id"]})


@router.post("/tasks/{task_id}/pack")
async def task_pack(task_id: str, request: Request) -> JSONResponse:
    body = await _body(request)
    state.set_state(task_id, TaskState.PACKED)
    task = state.task(task_id) or {}
    _emit("wms.packing.completed.v1", {
        "task_id": task_id, "owner_id": task.get("owner_id"), "sku_id": task.get("sku_id"),
        "qty": int(task.get("quantity", 1)), "actor_id": STAND_ACTOR_ID,
        "sequence": state.next_sequence(task_id),
    }, str(_params(body).get("correlation_id") or uid()))
    return _result(body, {"status": "packed", "task_id": task_id})


@router.post("/tasks/{task_id}/label")
async def task_label(task_id: str, request: Request) -> JSONResponse:
    """Стикер отдаётся из локального хранилища.

    Инвариант 9: к моменту нажатия этикетка уже лежит у нас. Запроса к WB
    здесь нет и быть не может — именно он давал 2–10 секунд ожидания.
    """
    body = await _body(request)
    label = state.label(task_id)
    if not label:
        return _result(body, {"error_code": "LABEL_NOT_READY", "id": None})
    return _result(body, label)


@router.post("/tasks/{task_id}/return-to-shelf")
async def task_return_to_shelf(task_id: str, request: Request) -> JSONResponse:
    body = await _body(request)
    state.release(task_id, reason="returned_to_shelf")
    task = state.task(task_id) or {}
    _emit("wms.returned.to.shelf.v1", {
        "task_id": task_id, "owner_id": task.get("owner_id"), "sku_id": task.get("sku_id"),
        "qty": int(task.get("quantity", 1)), "reason": "returned_to_shelf",
        "sequence": state.next_sequence(task_id),
    }, str(_params(body).get("correlation_id") or uid()))
    return _result(body, {"status": "returned_to_shelf", "task_id": task_id})


@router.post("/tasks/{task_id}/cancel")
async def task_cancel(task_id: str, request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    # Причина отмены обязательна (инвариант 11). В боевом контуре у всех 2645
    # отмен она была NULL — здесь пустая причина не принимается.
    reason = str(params.get("reason") or params.get("cancellation_event_id") or "")
    if not reason:
        return _result(body, {"status": "rejected", "error_code": "CANCEL_REASON_REQUIRED"})

    state.cancel(task_id, reason=reason)
    task = state.task(task_id) or {}
    _emit("wms.order.cancelled.v1", {
        "task_id": task_id, "wb_order_id": task.get("wb_order_id"),
        "owner_id": task.get("owner_id"), "cancel_reason": reason,
        "handed_over": bool(params.get("handed_over", False)),
        "sequence": state.next_sequence(task_id),
    }, str(params.get("correlation_id") or uid()))
    return _result(body, {"status": "cancelled", "task_id": task_id,
                          "label_invalidated": True, "released_from_supply": True})


@router.post("/tasks/{task_id}/return")
async def task_return(task_id: str, request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    return_id = state.open_return(
        task_id, str(params.get("return_event_id", uid())),
        str(params.get("seller_external_id", "")), params.get("reason"))
    item = state.get_return(return_id) or {}
    _emit("wms.return.expected.v1", {
        "task_id": task_id, "return_id": return_id,
        "return_event_id": item.get("return_event_id"), "owner_id": item.get("owner_id"),
    }, str(params.get("correlation_id") or uid()))
    return _result(body, {"return_id": return_id})


# ---------------------------------------------------------------- приёмка

@router.post("/receipts")
async def receipts(request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    receipt = state.receive(
        seller_external_id=str(params.get("seller_external_id", "")),
        reference=str(params.get("reference", uid())),
        lines=params.get("lines") or [],
        seller_name=params.get("seller_name"), seller_inn=params.get("seller_inn"))
    return _result(body, receipt)


@router.post("/receipts/screen")
async def receipts_screen(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {
        "open_receipts": state.open_receipts(),
        "cells": state.cells(),
        "discrepancy_kinds": ["shortage", "surplus", "mismatch", "damage"],
    })


@router.post("/putaway/screen")
async def putaway_screen(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {
        "pending": state.pending_putaway(),
        # Порядок обхода — то, по чему сортируется лист подбора (раздел 4).
        "cells": sorted(state.cells(), key=lambda c: (c["route_order"] is None,
                                                     c["route_order"] or 0)),
    })


@router.post("/inventory/sheet")
async def inventory_sheet(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {"lines": state.inventory_sheet(
        _params(body).get("seller_external_id"))})


@router.post("/inventory/count")
async def inventory_count(request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    return _result(body, state.inventory_count(
        seller_external_id=str(params.get("seller_external_id", "")),
        reference=str(params.get("reference", uid())),
        scope=str(params.get("scope", "partial")),
        lines=params.get("lines") or []))


# ---------------------------------------------------------------- коробки

@router.post("/boxes")
async def boxes_create(request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    # Комментарий обязателен: через месяц на складе стоят сотни одинаковых
    # коробок, и без него коробку не найти (раздел 2.9).
    if not str(params.get("comment") or "").strip():
        return _result(body, {"status": "rejected", "error_code": "BOX_COMMENT_REQUIRED"})
    return _result(body, state.create_box(params))


@router.post("/boxes/list")
async def boxes_list(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {"boxes": state.boxes(_params(body).get("seller_external_id"))})


@router.post("/boxes/remove")
async def boxes_remove(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, state.remove_box(str(_params(body).get("barcode", ""))))


# ---------------------------------------------------------------- отгрузка

@router.post("/shipments")
async def shipments(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, state.open_shipment(_params(body)))


@router.post("/shipments/picked")
async def shipments_picked(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {"shipments": state.shipments()})


# ---------------------------------------------------------------- возвраты

@router.post("/returns/{return_id}/receive")
async def returns_receive(return_id: str, request: Request) -> JSONResponse:
    body = await _body(request)
    state.receive_return(return_id)
    item = state.get_return(return_id) or {}
    _emit("wms.return.received.v1", {
        "return_id": return_id, "owner_id": item.get("owner_id"),
        "qty": int(item.get("qty", 1)),
    }, str(_params(body).get("correlation_id") or uid()))
    return _result(body, {"return_id": return_id, "state": "received"})


@router.post("/returns/{return_id}/decision")
async def returns_decision(return_id: str, request: Request) -> JSONResponse:
    body = await _body(request)
    decision = str(_params(body).get("decision", "resellable"))
    state.decide_return(return_id, decision)
    item = state.get_return(return_id) or {}
    resellable = decision == "resellable"
    event = "wms.return.resellable.v1" if resellable else "wms.return.defective.v1"
    _emit(event, {
        "return_id": return_id, "owner_id": item.get("owner_id"),
        "qty": int(item.get("qty", 1)), "decision": decision,
        # Годный возврат встаёт в good, брак — в defect. Состояние товара
        # закреплено за решением контрактом, а не выбирается на месте.
        "state_to": "good" if resellable else "defect",
    }, str(_params(body).get("correlation_id") or uid()))
    return _result(body, {"return_id": return_id, "state": "decided", "decision": decision})


@router.post("/returns/receipt")
async def returns_receipt(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {"returns": state.returns()})


# ---------------------------------------------------------------- печать и WB

@router.post("/labels/{task_id}/print")
async def label_print(task_id: str, request: Request) -> JSONResponse:
    """Отдаёт локальный ZPL агенту печати.

    Цель — от нажатия до движения головки меньше 300 мс (решение владельца 6),
    поэтому здесь только чтение из хранилища и передача агенту. Ни одного
    вызова наружу.
    """
    body = await _body(request)
    label = state.label(task_id)
    if not label:
        return _result(body, {"status": "rejected", "error_code": "LABEL_NOT_READY"})
    _emit("wms.label.attached.v1", {
        "task_id": task_id, "label_id": label["id"], "format": label["content_type"],
        "checksum": label["checksum"], "version": label["version"],
        "sequence": state.next_sequence(task_id),
    }, str(_params(body).get("correlation_id") or uid()))
    return _result(body, {"status": "sent_to_agent", "task_id": task_id,
                          "format": label["content_type"], "payload": label["payload"],
                          "checksum": label["checksum"]})


@router.post("/wb/accounts")
async def wb_accounts(request: Request) -> JSONResponse:
    """Кабинеты WB: перечисление и заведение.

    Значение токена не принимается и не отдаётся никогда — только ссылка
    secret_ref (инвариант 15, раздел 12).
    """
    body = await _body(request)
    params = _params(body)
    created = None
    if str(params.get("op", "list")) == "upsert" and params.get("external_id"):
        account, was_created = state.register_wb_account(
            str(params["external_id"]),
            seller_external_id=params.get("seller_external_id"),
            display_name=params.get("display_name"),
            secret_ref=params.get("secret_ref"),
            mode=params.get("mode"), status=params.get("status"),
            token_type=params.get("token_type"),
            wb_warehouse_id=params.get("wb_warehouse_id"))
        created = was_created
        if params.get("seller_external_id"):
            state.register_owner(str(params["seller_external_id"]))
    return _result(body, {"accounts": state.wb_accounts(), "created": created})


@router.post("/wb/accounts/{account_id}/verify")
async def wb_account_verify(account_id: str, request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {"account_id": account_id, "verified": True,
                          "mode": "shadow", "status": "ACTIVE",
                          "scopes": ["marketplace"]})


def create_app() -> FastAPI:
    # Падаем на старте, а не на первом запросе: сервис без объявленного
    # окружения не должен считаться поднявшимся.
    environment = app_environment()

    app = FastAPI(
        title="MM-Express WMS (mock)",
        version="0.1.0",
        description="Заглушка потока 0. Контракт настоящий, логика фиктивная.",
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts(environment))
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        from .metrics import prometheus_payload
        payload, content_type = prometheus_payload(state)
        return PlainTextResponse(payload.decode("utf-8"), media_type=content_type)

    @app.get("/__mock__/events")
    async def mock_events() -> dict[str, Any]:
        """Служебное окно в опубликованные события — только для тестов стенда."""
        return {"events": publisher.published}

    @app.post("/__mock__/reset")
    async def mock_reset() -> dict[str, str]:
        state.reset()
        publisher.clear()
        return {"status": "reset"}

    return app


app = create_app()
