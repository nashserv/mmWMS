"""Mock сервиса wms: те же маршруты, что в контракте, фиксированные ответы.

Существует ради одного правила: никто никого не ждёт (правило 9.5.4). Потоки B
и C пишут клиентов и консьюмеров против этого mock, пока поток A строит
настоящий сервис. Когда поток A готов — mock снимается, контракт остаётся.

Здесь НЕТ бизнес-логики. Всё, что похоже на решение (резерв прошёл, маппинг не
найден, клапан сработал), выбирается по фикстуре, а не вычисляется.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import fixtures
from .config import app_environment, mock_mode, trusted_hosts
from .domain import ErrorCode, EventEnvelope, TaskState, now, uid
from .events import EventPublisher
from . import state as state_module
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

def _product_view(product: dict[str, Any], seller: Any) -> dict[str, Any]:
    """Карточка товара в форме контракта (`Product`)."""
    row: dict[str, Any] = {
        "owner_external_id": str(seller or product.get("seller_external_id") or ""),
        "barcode": product["barcode"],
        "sku_id": product.get("sku_id"),
    }
    for source, target in (("seller_sku", "seller_sku"), ("name", "name")):
        if product.get(source) is not None:
            row[target] = product[source]
    row["buffer"] = int(product.get("buffer") or 0)
    return row


def _task_command(task: dict[str, Any] | None, task_id: str) -> dict[str, Any]:
    """Ответ команды над заданием (`TaskCommandResult`).

    `state` обязателен по контракту, и без него рабочее место не может
    показать, чем кончилась команда: шаг 9 полного прогона краснел не потому,
    что упаковка не работает, а потому, что заглушка не возвращала состояние.
    `status` контрактом не предусмотрен вовсе.
    """
    task = task or {}
    row = {
        "task_id": task_id,
        "owner_external_id": task.get("seller_external_id") or task.get("owner_external_id") or "",
        "state": task.get("state") or "unknown",
    }
    if task.get("cancel_reason"):
        row["cancel_reason"] = task["cancel_reason"]
    return row


def _box_view(box: dict[str, Any]) -> dict[str, Any]:
    """Коробка в форме контракта (`BoxProjection`).

    Внутренний `cell_id` наружу не отдаётся: у клиента наших uuid нет, он
    адресует ячейку адресом (`cell_address`).
    """
    row = {key: value for key, value in box.items() if key != "cell_id"}
    row.setdefault("state", "stored")
    row.setdefault("counted", False)
    row.setdefault("comment", "—")
    row.setdefault("quantity", 0)
    return row


def _owner_of_label(task_id: str) -> str:
    """Владелец задания. Пустым он быть не может: печать чужого стикера
    отправит вещь другому покупателю (приложение C)."""
    task = state.task(task_id) or {}
    return str(task.get("seller_external_id") or "unknown-owner")


def _wb_account_view(account: dict[str, Any]) -> dict[str, Any]:
    """Кабинет WB в форме контракта (`WbAccount`).

    Значение токена здесь не появляется никогда — только `secret_ref`, ссылка
    на секрет (инвариант 15, раздел 12).
    """
    row = {key: value for key, value in account.items() if key != "seller_external_id"}
    row["owner_external_id"] = account.get("seller_external_id") or ""
    return row


def _return_view(item: dict[str, Any]) -> dict[str, Any]:
    """Возврат в форме контракта (`ReturnResult`)."""
    return {
        "return_id": item["return_id"],
        "owner_external_id": item.get("seller_external_id") or "",
        "task_id": item.get("task_id"),
        "state": item.get("state") or "expected",
        "decision": item.get("decision"),
    }


def _count_view(params: dict[str, Any], count: dict[str, Any]) -> dict[str, Any]:
    """Инвентаризация в форме контракта (`InventoryCountResult`)."""
    return {
        "count_id": count["count_id"],
        "reference": count["reference"],
        "owner_external_id": str(params.get("seller_external_id") or ""),
        "state": count["state"],
        "applied_at": now(),
        "moves": len(count.get("lines") or []),
        "discrepancies": [],
        "duplicate": False,
    }


def _stock_row(row: dict[str, Any]) -> dict[str, Any]:
    """Строка публикуемого остатка (`StockRow`): только штрихкод и available.

    `good` и `reserved` наружу не отдаются: контракт их не знает, а клиент,
    выучивший их у заглушки, не найдёт у настоящего сервиса.
    """
    return {"barcode": row["barcode"], "available": int(row["available"])}


@router.post("/health")
async def health(request: Request) -> JSONResponse:
    # Форма — HealthResult. Признак заглушки едет в `checks`, а не отдельным
    # полем: у схемы `additionalProperties: false`.
    return _result(await _body(request), {"status": "ok", "checks": {"mock": "true"}})


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
    if not seller:
        raise ValueError("seller_external_id обязателен")
    # `created` — про заведение владельца, а не про то, что параметр передали:
    # повторный вызов обязан вернуть того же владельца и `created: false`
    # (инвариант 5). Заново присвоенный owner_id обесценил бы все уже
    # выпущенные события.
    existed = state.owner(str(seller)) is not None
    created = state.register_owner(
        str(seller), name=params.get("name"), inn=params.get("inn"),
        allow_ledger_short=params.get("allow_ledger_short"))
    # Форма — SellerResult: один владелец, а не список. Список владельцев
    # контрактом не предусмотрен вовсе, и синониму места нет
    # (`additionalProperties: false`).
    owner = created
    return _result(body, {
        "owner_external_id": owner["seller_external_id"],
        "owner_id": owner.get("owner_id"),
        "name": owner.get("name") or owner["seller_external_id"],
        "inn": owner.get("inn"),
        "active": bool(owner.get("active", True)),
        "allow_ledger_short": bool(owner.get("allow_ledger_short", True)),
        "created": not existed,
    })


@router.post("/catalog/products")
async def catalog_products(request: Request) -> JSONResponse:
    body = await _body(request)
    seller = _params(body).get("seller_external_id")
    return _result(body, {
        "products": [_product_view(row, row.get("seller_external_id"))
                     for row in state.products(seller)]})


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
    # Форма — CatalogProductEnsureResult: карточка внутри `product`.
    return _result(body, {"product": _product_view(product, seller), "created": created})


@router.post("/catalog/stocks")
async def catalog_stocks(request: Request) -> JSONResponse:
    body = await _body(request)
    seller = _params(body).get("seller_external_id")
    return _result(body, {
        "owner_external_id": seller,
        "stocks": [_stock_row(row) for row in state.stocks_for(seller)]})


@router.post("/catalog/stocks/bulk")
async def catalog_stocks_bulk(request: Request) -> JSONResponse:
    body = await _body(request)
    rows = state.stocks_for(_params(body).get("seller_external_id"))
    # Строки без штрихкода или с нецелым/отрицательным available отбрасываются
    # (приложение C). Заниженный остаток — норма, отрицательный — нет.
    clean = [_stock_row(r) for r in rows
             if r.get("barcode") and isinstance(r.get("available"), int) and r["available"] >= 0]
    return _result(body, {"stocks": clean,
                          "owner_external_id": _params(body).get("seller_external_id")})


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
    # Форма — WarehouseDocumentResult. Внутренние поля документа наружу не
    # едут: клиенту важно, что документ применён и сколько движений он дал.
    return _result(body, {
        "reference": document["reference"],
        "owner_external_id": seller,
        "state": document["state"],
        "moves": len(document.get("lines") or []),
        "duplicate": False})


@router.post("/warehouse/stock")
async def warehouse_stock(request: Request) -> JSONResponse:
    body = await _body(request)
    # Форма — WarehouseStockResult: `rows` из StockPlacement, а не `stock`.
    seller = _params(body).get("seller_external_id")
    return _result(body, {
        "owner_external_id": seller,
        "rows": state.placements_for(seller)})


@router.post("/warehouse/movements")
async def warehouse_movements(request: Request) -> JSONResponse:
    """История движений по товару за период — файл 04 требует её от ЛК клиента.

    Заглушка ведёт журнал в памяти теми же движениями, что и настоящий сервис
    пишет в `stock_move`: клиент, написанный против неё, не переучивается.
    """
    body = await _body(request)
    params = _params(body)
    seller = str(params.get("seller_external_id", ""))
    limit = min(int(params.get("limit") or 100), 500)
    rows = state.movements(seller_external_id=seller,
                           barcode=params.get("barcode"), limit=limit + 1)
    return _result(body, {
        "seller_external_id": seller,
        "movements": rows[:limit],
        "next_cursor": rows[limit - 1]["movement_id"] if len(rows) > limit else None,
        "generated_at": now(),
    })


@router.post("/catalog/wb-cards")
async def catalog_wb_cards(request: Request) -> JSONResponse:
    """Карточки Wildberries через `wms` (раздел 6.8).

    Токен категории «Контент» держит `wms` (раздел 12), поэтому ходить в
    Content API каталогу больше нечем.
    """
    body = await _body(request)
    params = _params(body)
    seller = str(params.get("seller_external_id", ""))
    wanted = [str(b) for b in (params.get("barcodes") or [])]
    return _result(body, {
        "seller_external_id": seller,
        "cards": state.wb_cards(seller_external_id=seller, barcodes=wanted),
        "next_cursor": None,
        "generated_at": now(),
    })


@router.post("/storage/lookup")
async def storage_lookup(request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    seller = params.get("seller_external_id")
    return _result(body, {
        "owner_external_id": seller,
        "placements": [row for row in state.placements_for(seller)
                       if not params.get("barcode") or row["barcode"] == params["barcode"]]})


@router.post("/storage/count")
async def storage_count(request: Request) -> JSONResponse:
    body = await _body(request)
    # Форма — StorageCountResult: сколько и в каких состояниях лежит товар.
    # `{"counted": true}` контракт не знает вовсе.
    params = _params(body)
    seller = str(params.get("seller_external_id") or "")
    barcode = str(params.get("barcode") or "")
    rows = [r for r in state.placements_for(seller)
            if not barcode or r["barcode"] == barcode]
    by_state: dict[str, int] = {}
    for row in rows:
        by_state[row["state"]] = by_state.get(row["state"], 0) + int(row["quantity"])
    return _result(body, {
        "owner_external_id": seller,
        "barcode": barcode or (rows[0]["barcode"] if rows else ""),
        "total": sum(by_state.values()),
        "by_state": by_state,
        "available": by_state.get("good", 0)})


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
    # claim и states читаются, а не игнорируются: заявка 2 потока B.
    claim = params.get("claim")
    tasks = state.pull(
        assignee=params.get("assignee"), limit=limit,
        claim=True if claim is None else bool(claim),
        states=params.get("states"),
        owner_external_ids=params.get("owner_external_ids"))
    # Форма ответа — по схеме TasksPullResult: каждое задание приходит вместе
    # со сроком своего лизинга, чтобы рабочее место знало, когда задание
    # вернётся в очередь, если сборщик пропал.
    return _result(body, {
        "tasks": [{"task": state_module._task_projection(task),
                   "leased_until": task.get("claim_expires_at"),
                   "placements": state_module.task_placements(task)}
                  for task in tasks],
        "served_at": now(),
        "available_total": max(0, available_before - len(tasks)),
    })


@router.post("/tasks/{task_id}")
async def task_read(task_id: str, request: Request) -> JSONResponse:
    body = await _body(request)
    task = state.task(task_id)
    if task is None:
        raise ValueError(f"задание {task_id} не найдено")
    return _result(body, state_module._task_projection(task))


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
    return _result(body, _task_command(state.task(task_id), task_id))


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
    return _result(body, _task_command(state.task(task_id), task_id))


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
    return _result(body, {
        **_task_command(state.task(task_id), task_id),
        # Причина обязательна и в схеме, и в контракте (инвариант 11): в бою у
        # всех 2645 отмен она была NULL, и разобрать их стало нечем.
        "cancel_reason": reason,
        "label_invalidated": True, "released_from_supply": True, "stock_released": True})


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
    # Форма — ReceiptResult: строки наружу не едут, их читают экраном.
    return _result(body, {
        "receipt_id": receipt["receipt_id"],
        "reference": receipt["reference"],
        "owner_external_id": receipt["owner_external_id"],
        "state": receipt["state"],
        "lines_accepted": len(receipt.get("lines") or []),
        "discrepancies": receipt.get("discrepancies") or [],
        "duplicate": False})


@router.post("/receipts/screen")
async def receipts_screen(request: Request) -> JSONResponse:
    """Форма — ReceiptsScreenResult: `receipts` и `generated_at`.

    Не `open_receipts`/`cells`/`discrepancy_kinds`: у схемы
    `additionalProperties: false`, и своих имён клиент просто не увидит.
    """
    body = await _body(request)
    return _result(body, {"receipts": state.open_receipts(), "generated_at": now()})


@router.post("/putaway/screen")
async def putaway_screen(request: Request) -> JSONResponse:
    """Форма — PutawayScreenResult: `items` и `generated_at`."""
    body = await _body(request)
    return _result(body, {"items": state.pending_putaway(), "generated_at": now()})


@router.post("/discrepancies")
async def discrepancies(request: Request) -> JSONResponse:
    """Расхождения вне контекста приёмки — экран начальника склада.

    Пустой список означает «таких случаев не было», и это обязано быть
    правдой, а не следствием отсутствия маршрута (инвариант 12).
    """
    body = await _body(request)
    params = _params(body)
    return _result(body, {
        "discrepancies": state.discrepancies(
            owner_external_id=params.get("owner_external_id"),
            kinds=params.get("kinds"),
            decisions=params.get("decisions")),
        "generated_at": now(),
    })


@router.post("/inventory/sheet")
async def inventory_sheet(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {"owner_external_id": _params(body).get("seller_external_id"),
                          "generated_at": now(),
                          "lines": state.inventory_sheet(
        _params(body).get("seller_external_id"))})


@router.post("/inventory/count")
async def inventory_count(request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    return _result(body, _count_view(params, state.inventory_count(
        seller_external_id=str(params.get("seller_external_id", "")),
        reference=str(params.get("reference", uid())),
        scope=str(params.get("scope", "partial")),
        lines=params.get("lines") or [])))


# ---------------------------------------------------------------- коробки

@router.post("/boxes")
async def boxes_create(request: Request) -> JSONResponse:
    body = await _body(request)
    params = _params(body)
    # Комментарий обязателен: через месяц на складе стоят сотни одинаковых
    # коробок, и без него коробку не найти (раздел 2.9).
    if not str(params.get("comment") or "").strip():
        return _result(body, {"status": "rejected", "error_code": "BOX_COMMENT_REQUIRED"})
    # Форма — BoxResult: коробка внутри `box`.
    return _result(body, {"box": _box_view(state.create_box(params)), "created": True})


@router.post("/boxes/list")
async def boxes_list(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {
        "boxes": [_box_view(box)
                  for box in state.boxes(_params(body).get("seller_external_id"))]})


@router.post("/boxes/remove")
async def boxes_remove(request: Request) -> JSONResponse:
    body = await _body(request)
    barcode = str(_params(body).get("barcode", ""))
    state.remove_box(barcode)
    removed = state.box(barcode)
    if removed is None:
        raise ValueError(f"коробка {barcode!r} не заведена")
    return _result(body, {"box": _box_view(removed), "created": False})


# ---------------------------------------------------------------- отгрузка

@router.post("/shipments")
async def shipments(request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, state.open_shipment(_params(body)))


@router.post("/shipments/picked")
async def shipments_picked(request: Request) -> JSONResponse:
    body = await _body(request)
    # Форма — ShipmentsPickedResult: собранные ЗАДАНИЯ, готовые к отгрузке,
    # а не список поставок. Клиент ждёт TaskProjection.
    return _result(body, {"tasks": state.picked_tasks(
        _params(body).get("seller_external_id"))})


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
    return _result(body, {
        "return_id": return_id,
        "owner_external_id": item.get("seller_external_id") or "",
        "task_id": item.get("task_id"),
        "state": "received"})


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
    return _result(body, {
        "return_id": return_id,
        "owner_external_id": item.get("seller_external_id") or "",
        "task_id": item.get("task_id"),
        "state": "decided", "decision": decision})


@router.post("/returns/receipt")
async def returns_receipt(request: Request) -> JSONResponse:
    body = await _body(request)
    # Форма — ReturnsReceiptResult: приёмка возвратов документом.
    params = _params(body)
    return _result(body, {
        "reference": str(params.get("reference") or uid()),
        "returns": [_return_view(item) for item in state.returns()],
        # Сколько строк не удалось привязать к заданию. Ноль не гарантирован:
        # возврат может приехать раньше, чем WB отдаст связь.
        "unmatched": 0, "duplicate": False})


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
    params = _params(body)
    task = state.task(task_id) or {}
    _emit("wms.label.attached.v1", {
        "task_id": task_id, "label_id": label["id"],
        # Владелец обязателен: стикеровка тарифицируется (раздел 3.4), и без
        # него биллингу некому её выставить — строка повисает в отчёте с
        # причиной SELLER_UNKNOWN.
        "owner_id": task.get("owner_id") or state.owner_id_of(
            str(task.get("seller_external_id") or "")),
        "format": label["format"],
        "checksum": label["checksum"], "version": label["version"],
        "sequence": state.next_sequence(task_id),
    }, str(params.get("correlation_id") or uid()))
    # Форма — LabelPrintResult. `station_id` обязателен: принтер на каждом
    # рабочем месте свой, и перепутать станцию значит напечатать чужой стикер.
    return _result(body, {
        "task_id": task_id,
        # Владелец обязан быть непустым: напечатанный чужой стикер отправит
        # вещь другому покупателю (приложение C).
        "owner_external_id": task.get("seller_external_id") or _owner_of_label(task_id),
        "format": label["format"],
        "content_type": label["content_type"],
        "payload": label["payload"],
        "checksum": label["checksum"],
        "version": label["version"],
        "station_id": str(params.get("station_id") or ""),
        "printer_transport": "agent",
        "reprint": bool(params.get("reprint", False)),
        "duplicate": False,
    })


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
    return _result(body, {"accounts": [_wb_account_view(a) for a in state.wb_accounts()],
                          "created": bool(created)})


@router.post("/wb/accounts/{account_id}/verify")
async def wb_account_verify(account_id: str, request: Request) -> JSONResponse:
    body = await _body(request)
    return _result(body, {"account_id": account_id, "verified_at": now(),
                          # Что именно проверено. Живого токена на стенде нет
                          # и быть не может (раздел 12), поэтому проверяется
                          # ссылка на секрет, а не сам секрет.
                          "checks": [
                              {"name": "token_valid", "passed": True,
                               "detail": "ссылка на секрет на месте"},
                              {"name": "scope_marketplace", "passed": True},
                              {"name": "rate_limit", "passed": True,
                               "detail": "300 запросов в минуту, окно свободно"},
                          ],
                          "status": "ACTIVE",
                          "scopes": ["marketplace"]})


def _pool_lifespan(*closers: Any) -> Any:
    """Lifespan приложения: отпустить пул соединений и остановить конвейер публикаций."""
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            for close in closers:
                close()

    return lifespan


def create_app() -> FastAPI:
    # Падаем на старте, а не на первом запросе: сервис без объявленного
    # окружения не должен считаться поднявшимся.
    environment = app_environment()
    # Заглушка обязана заявлять о себе явно (WMS_MOCK). Настоящий сервис
    # поднимается только при явно выключенном флаге: молчаливое превращение
    # mock в «почти настоящий сервис» — это то, как заглушки доезжают до прода.
    mocked = mock_mode()

    app = FastAPI(
        title="MM-Express WMS (mock)" if mocked else "MM-Express WMS",
        version="0.1.0",
        description=("Заглушка потока 0. Контракт настоящий, логика фиктивная."
                     if mocked else
                     "Сервис склада: своя база, своя транзакция, свой Wildberries."),
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts(environment))

    if mocked:
        app.include_router(router)
        readiness: Any = lambda: True
        metrics_source: Any = state
    else:
        # Импорт здесь, а не наверху: заглушке база не нужна вовсе, и требовать
        # DATABASE_URL ради её запуска — значит держать потоки B и C заложниками
        # чужой инфраструктуры (правило 9.5.4).
        from .postgres import pool, reset_pool
        from .routes import create_router
        from .runtime import RuntimeMetrics
        from .stock_push import reset_publisher

        connections = pool()
        app.include_router(create_router(connections))
        readiness = connections.healthy
        metrics_source = RuntimeMetrics(connections)

        # Закрытие пула вешается на lifespan приложения: соединения обязаны
        # отпуститься при остановке, иначе Postgres какое-то время держит
        # backend'ы уже мёртвого контейнера.
        app.router.lifespan_context = _pool_lifespan(reset_pool, reset_publisher)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        # Готовность — это доступность зависимостей, а не «процесс жив».
        # Мониторинг, следящий за портом, а не за работой, — раздел 3.5.
        ready = bool(readiness())
        return JSONResponse({"status": "ready" if ready else "not-ready"},
                            status_code=200 if ready else 503)

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        from .metrics import prometheus_payload
        payload, content_type = prometheus_payload(metrics_source)
        return PlainTextResponse(payload.decode("utf-8"), media_type=content_type)

    if mocked:
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
