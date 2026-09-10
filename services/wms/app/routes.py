"""Настоящие маршруты сервиса wms поверх Postgres.

Контракт тот же, что у заглушки потока 0, — `/api/mmx/wms/v1/*`, конверт
JSON-RPC (приложение B). Разница только в том, что за ответом теперь стоит
база: резерв, остаток и владелец читаются и пишутся по-настоящему, а не
выбираются по фикстуре.

Маршрут, которого этот поток ещё не написал, отвечает явной ошибкой с именем
недостающего куска. Молчаливая заглушка под настоящим именем — это то, как
заглушки доезжают до прода; лучше внятный отказ, чем правдоподобный ответ.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .metrics import observe_http
from .postgres import ConnectionPool
from .stock_push import publisher as stock_publisher_for
from .supplies import release_from_supply, verify_account
from .labels import LabelOperations
from .receiving import ReceivingOperations
from .returns import ReturnOperations
from .service import CatalogOperations, StockOperations, WmsService
from .shipments import ShipmentOperations
from .tasks import TaskOperations

BASE_PATH = "/api/mmx/wms/v1"

log = logging.getLogger("wms.routes")

# Числовые коды JSON-RPC. Прикладной отказ сюда не попадает — у него свой
# error_code внутри result (приложение C).
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL = -32603
JSONRPC_NOT_IMPLEMENTED = -32601


def _params(body: Any) -> dict[str, Any]:
    """Достаёт params из конверта. Конверт принимается и без обёртки.

    Ровно та же снисходительность, что у заглушки: клиенты потока B уже
    написаны против неё, и ужесточать форму на переходе с mock на настоящий
    сервис — значит ломать их на ровном месте.
    """
    if not isinstance(body, dict):
        return {}
    inner = body.get("params")
    return inner if isinstance(inner, dict) else body


def _request_id(body: Any) -> Any:
    return body.get("id", 1) if isinstance(body, dict) else 1


def _result(body: Any, payload: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": _request_id(body), "result": payload})


def _error(body: Any, code: int, message: str, **data: Any) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": _request_id(body), "error": error})


async def _body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return {}


def create_router(pool: ConnectionPool) -> APIRouter:
    """Собирает маршруты вокруг одного пула соединений.

    Публикатора событий здесь нет намеренно: события пишутся в outbox той же
    транзакцией, что и движение товара, а в шину их несёт отдельный воркер
    (app/workers/outbox_publisher.py). Маршрут о шине не знает вовсе — склад
    от неё не зависит (раздел 6.1).
    """
    router = APIRouter(prefix=BASE_PATH)
    # Остаток публикуется в Wildberries сразу после движения, без таймеров
    # (раздел 6.4). Публикатор получает уведомление уже после коммита —
    # внутри транзакции вызовов наружу нет и быть не может (инвариант 2).
    stock_publisher = stock_publisher_for(pool)
    wms = WmsService(pool, on_stock_changed=stock_publisher.notify)
    catalog = CatalogOperations(pool)
    stock = StockOperations(pool, stock_publisher.notify)
    receiving = ReceivingOperations(pool, stock_publisher.notify)
    shipments = ShipmentOperations(pool, wms)
    labels = LabelOperations(pool, wms)
    returns = ReturnOperations(pool, wms, stock_publisher.notify)
    tasks = TaskOperations(pool, wms, stock_publisher.notify,
                           on_supply_release=release_from_supply(pool))

    # Событий отсюда никто не отправляет намеренно. Они уже записаны в outbox
    # той же транзакцией, что и движение товара, и в шину их несёт единственный
    # публикатор (app/workers/outbox_publisher.py).
    #
    # Раньше маршрут отправлял их ещё и сам «чтобы побыстрее». Это давало две
    # беды сразу: каждое событие уезжало в шину дважды, и десять миллисекунд
    # разговора с брокером ложились на горячий путь резерва — тот самый, для
    # которого раздел 10 требует «WB → задание» под 2 с. Склад от шины не
    # зависит (раздел 6.1), торопиться с уведомлением незачем.

    def guarded(handler: Callable[[dict[str, Any]], Any]):
        """Обёртка вокруг обработчика: валидация — 400 по протоколу, сбой — 500.

        Разделение принципиальное: прикладной отказ («товар не найден») уезжает
        в result с кодом, а сюда попадает только то, что клиент не может
        разобрать по контракту.
        """
        async def endpoint(request: Request) -> JSONResponse:
            started = time.monotonic()
            body = await _body(request)
            try:
                payload = handler(_params(body))
            except ValueError as invalid:
                observe_http(400, time.monotonic() - started)
                return _error(body, JSONRPC_INVALID_PARAMS, str(invalid))
            except NotImplementedError as missing:
                observe_http(501, time.monotonic() - started)
                return _error(body, JSONRPC_NOT_IMPLEMENTED, str(missing))
            except Exception as failure:                    # noqa: BLE001
                log.exception("маршрут упал")
                observe_http(500, time.monotonic() - started)
                return _error(body, JSONRPC_INTERNAL, f"внутренняя ошибка: {failure}")
            observe_http(200, time.monotonic() - started)
            return _result(body, payload)

        return endpoint

    def missing(what: str) -> Callable[[dict[str, Any]], Any]:
        """Маршрут контракта, до которого поток A ещё не дошёл."""
        def handler(_params: dict[str, Any]) -> Any:
            raise NotImplementedError(
                f"маршрут ещё не реализован потоком A: {what}. "
                f"Пока пользуйтесь заглушкой (WMS_MOCK=true)")
        return handler

    def post(path: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        router.add_api_route(path, guarded(handler), methods=["POST"])

    def post_id(path: str, handler: Callable[..., Any]) -> None:
        """Маршрут с идентификатором в пути: `/tasks/{task_id}/...`.

        Как называется параметр — не важно: у возвратов это `return_id`, у
        кабинетов `account_id`, а обработчик один и тот же.
        """
        async def endpoint(request: Request) -> JSONResponse:
            started = time.monotonic()
            body = await _body(request)
            # Идентификатор берём из пути сами: у маршрутов возвратов и
            # кабинетов он называется иначе, а обработчик один.
            identifier = next(iter(request.path_params.values()), "")
            try:
                payload = handler(identifier, _params(body))
            except ValueError as invalid:
                observe_http(400, time.monotonic() - started)
                return _error(body, JSONRPC_INVALID_PARAMS, str(invalid))
            except Exception as failure:                    # noqa: BLE001
                log.exception("маршрут %s упал", path)
                observe_http(500, time.monotonic() - started)
                return _error(body, JSONRPC_INTERNAL, f"внутренняя ошибка: {failure}")
            observe_http(200, time.monotonic() - started)
            return _result(body, payload)

        router.add_api_route(path, endpoint, methods=["POST"])

    # ------------------------------------------------------------ служебное

    post("/health", lambda _: {"status": "ok", "mock": False, "database": pool.healthy()})

    # ---------------------------------------------------------- справочники

    def sellers(params: dict[str, Any]) -> dict[str, Any]:
        seller = params.get("seller_external_id")
        created = catalog.upsert_owner(params) if seller else None
        return {"sellers": catalog.owners(), "seller": created}

    post("/sellers", sellers)
    post("/catalog/products", lambda p: {"products": catalog.products(p.get("seller_external_id"))})
    post("/catalog/products/ensure", catalog.ensure_product)

    def catalog_stocks(params: dict[str, Any]) -> dict[str, Any]:
        seller = str(params.get("seller_external_id") or "")
        barcode = str(params.get("barcode") or "")
        rows, dropped = stock.available_rows(seller)
        return {"stocks": [row for row in rows if row["barcode"] == barcode],
                "owner_external_id": seller, "dropped_rows": dropped}

    def catalog_stocks_bulk(params: dict[str, Any]) -> dict[str, Any]:
        seller = str(params.get("seller_external_id") or "")
        rows, dropped = stock.available_rows(seller)
        return {"stocks": rows, "owner_external_id": seller, "dropped_rows": dropped}

    post("/catalog/stocks", catalog_stocks)
    post("/catalog/stocks/bulk", catalog_stocks_bulk)

    # ------------------------------------------------- склад и начальный остаток

    post("/warehouse/documents", stock.apply_document)
    post("/warehouse/stock", stock.placements)

    # ---------------------------------------------------------------- резерв

    def reservations(params: dict[str, Any]) -> dict[str, Any]:
        if not str(params.get("sku") or params.get("barcode") or "").strip():
            raise ValueError("sku обязателен: у Wildberries в нём приходит штрихкод")
        if params.get("wb_order_id") in (None, ""):
            raise ValueError("wb_order_id обязателен: это ключ идемпотентности задания")
        return wms.reserve(params).as_result()

    post("/reservations", reservations)

    # ------------------------------------------------------- кабинеты WB

    def wb_accounts(params: dict[str, Any]) -> dict[str, Any]:
        if str(params.get("op") or "").strip() == "upsert":
            return catalog.upsert_wb_account(params)
        return {"accounts": catalog.accounts(params.get("owner_external_id"))}

    post("/wb/accounts", wb_accounts)

    # ---------------------------------------------------------------- задания
    # /tasks/pull регистрируется ДО /tasks/{task_id}: иначе параметр пути
    # проглотит слово pull и рабочее место получит «задание с id pull».

    post("/tasks/pull", tasks.pull)
    post_id("/tasks/{task_id}", lambda task_id, _params: tasks.read(task_id))
    post_id("/tasks/{task_id}/scan", tasks.scan)
    post_id("/tasks/{task_id}/pack", tasks.pack)
    post_id("/tasks/{task_id}/return-to-shelf", tasks.return_to_shelf)
    post_id("/tasks/{task_id}/cancel", tasks.cancel)
    post_id("/tasks/{task_id}/label", labels.read)
    post_id("/tasks/{task_id}/return", returns.expect)
    post_id("/labels/{task_id}/print", labels.print)

    # ---------------------------------------------------- приёмка и хранение

    post("/receipts", receiving.receive)
    post("/receipts/screen", receiving.screen)
    post("/putaway/screen", receiving.putaway_screen)
    post("/inventory/sheet", receiving.sheet)
    post("/inventory/count", receiving.count)

    def box_create(params: dict[str, Any]) -> dict[str, Any]:
        """Завести коробку. Комментарий обязателен — так требует склад.

        «Через месяц стоят сотни одинаковых коробок» (раздел 2.9): без пометки
        нужную не найти, поэтому пустой комментарий отбивается здесь и в схеме.
        """
        return receiving.create_box(params)

    post("/boxes", box_create)
    post("/boxes/list", receiving.list_boxes)
    post("/boxes/remove", receiving.remove_box)
    post("/storage/lookup", receiving.lookup)
    post("/storage/count", receiving.count_cell)

    # ------------------------------------- ещё не написано потоком A

    post("/shipments", shipments.handle)
    post("/shipments/picked", shipments.picked)

    # ------------------------------------------------------------- возвраты

    post("/returns/receipt", returns.receipt)
    post_id("/returns/{return_id}/receive", returns.receive)
    post_id("/returns/{return_id}/decision", returns.decide)

    def wb_account_verify(account_id: str, _params: dict[str, Any]) -> dict[str, Any]:
        """Проверка кабинета: отвечает ли Wildberries нашим секретом.

        Живого токена на стенде нет и быть не может (раздел 12), поэтому
        проверка честно говорит, что именно она проверила, а не рисует
        зелёную галочку.
        """
        return verify_account(pool, account_id)

    post_id("/wb/accounts/{account_id}/verify", wb_account_verify)

    return router
