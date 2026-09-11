"""Симулятор Wildberries FBS для стенда.

ВАЖНО. Пункт 6 файла 01 предписывает взять симулятор из платформы — он там
уже существует и используется в проде для тестов
(/root/MM-express-final-platform-integration, раздел 2.3 мастера). Исходников
платформы в этом репозитории нет, поэтому здесь написан минимальный симулятор
строго по приложению D мастера. Когда платформенный станет доступен — заменить
этот и удалить каталог: у того за спиной боевые прогоны, у этого нет.

Что воспроизводится намеренно точно:
  * лимит 300 запросов в минуту на кабинет — без него на стенде не
    воспроизвести блокировку, ради которой в разделе 6.4 держат ограничитель;
  * стикеры пачкой до 100 штук в форматах png | svg | zplv | zplh;
  * поле sku, в котором приходит штрихкод (раздел 3.2) — главный источник
    путаницы в маппинге.

Живых токенов не принимает и не проверяет: на стенде их нет вовсе (раздел 12).
"""
from __future__ import annotations

import os
import time
from threading import RLock
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

RATE_LIMIT = int(os.getenv("WB_RATE_LIMIT_PER_MINUTE", "300"))
WINDOW_SECONDS = 60


class Simulator:
    def __init__(self) -> None:
        self._lock = RLock()
        self.reset()

    def reset(self) -> None:
        """Сброс данных. Счётчики идентификаторов НЕ откатываются.

        У Wildberries номера сквозные и не повторяются никогда. Сброс,
        возвращающий их к началу, выдаёт номера, которые уже лежат в Postgres
        от прошлых прогонов: поставка падает уникальным ключом
        `(wb_account_id, wb_supply_id)`, а заказ отсеивается опросчиком как
        уже известный — задания не заводятся, и шаг 4 полного прогона краснеет
        причиной, никак с ним не связанной.

        Посева временем мало: два сброса в одну секунду дают один и тот же
        номер. Счётчики переживают сброс.
        """
        with self._lock:
            carried_order = getattr(self, "_next_order_id", None)
            carried_supply = getattr(self, "_next_supply", None)
            self._orders: list[dict[str, Any]] = []
            self._supplies: dict[str, dict[str, Any]] = {}
            self._stocks: dict[str, dict[str, int]] = {}
            # Карточки Content API. Раздел 6.8: каталог берёт их через wms,
            # а токен держит wms (раздел 12), поэтому ходить в Content API
            # каталогу больше нечем.
            self._cards: dict[str, list[dict[str, Any]]] = {}
            self._next_nm_id = 170000001
            self._calls: dict[str, list[float]] = {}
            # Идентификатор заказа не должен повторяться между перезапусками.
            # Счётчик с фиксированного числа выдавал бы те же номера, что уже
            # лежат в Postgres от прошлых прогонов, и опросчик отсеивал бы
            # свежие заказы как уже известные (`known_orders`): задания не
            # заводятся, остаток не двигается, а шаг 4 краснеет «good не упал».
            # У настоящего Wildberries номера сквозные, поэтому берём время.
            self._next_order_id = (carried_order if carried_order is not None
                                   else 900_000_000 + int(time.time()) % 90_000_000)
            # Номер поставки, как и номер заказа, не должен повторяться между
            # сбросами симулятора: у Wildberries они сквозные. Счётчик с
            # единицы выдавал WB-GI-00000001 заново, а у нас на кабинете уже
            # лежала поставка с таким номером — `wb_supply` роняла вставку
            # уникальным ключом (wb_account_id, wb_supply_id).
            self._next_supply = (carried_supply if carried_supply is not None
                                 else int(time.time()) % 90_000_000)

    # Лимит считается по кабинету, а не глобально: у WB он именно такой.
    def take_slot(self, account: str) -> bool:
        with self._lock:
            now = time.monotonic()
            window = [t for t in self._calls.get(account, []) if now - t < WINDOW_SECONDS]
            if len(window) >= RATE_LIMIT:
                self._calls[account] = window
                return False
            window.append(now)
            self._calls[account] = window
            return True

    def cards(self, account: str, cursor: int = 0,
              limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        """Карточки кабинета страницей. Content API отдаёт их курсором."""
        with self._lock:
            rows = self._cards.get(account, [])
            page = rows[cursor:cursor + limit]
            return page, cursor + len(page)

    def seed_cards(self, account: str, barcodes: list[str]) -> list[dict[str, Any]]:
        """Ручка стенда: у настоящего Content API её нет."""
        with self._lock:
            known = {row["barcode"] for row in self._cards.get(account, [])}
            created = []
            for barcode in barcodes:
                if barcode in known:
                    continue
                card = {
                    "nmID": self._next_nm_id,
                    "vendorCode": f"art-{barcode[-6:]}",
                    "title": f"Товар {barcode[-4:]}",
                    "brand": "Тестовый бренд",
                    "subjectName": "Одежда",
                    # У Wildberries штрихкод лежит в size.skus (раздел 3.2):
                    # у одной карточки несколько размеров, и вещь на полке
                    # определяет именно штрихкод, а не артикул.
                    "sizes": [{"skus": [barcode]}],
                    "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                self._next_nm_id += 1
                self._cards.setdefault(account, []).append(card)
                created.append(card)
            return created

    def seed_orders(self, account: str, count: int, barcode: str,
                    deadline: str | None = None,
                    broken: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            created = []
            for _ in range(count):
                order = {
                    "id": self._next_order_id,
                    "rid": f"rid-{self._next_order_id}",
                    "createdAt": "2026-09-10T08:00:00Z",
                    "warehouseId": 1,
                    "supplierStatus": "new",
                    "wbStatus": "waiting",
                    # У WB поле называется sku, но лежит в нём штрихкод.
                    "skus": [barcode],
                    "account": account,
                    "ddate": deadline,
                }
                # Ручка стенда: заказ с битым полем. У настоящего WB такие
                # приезжают сами — кривая дата, отрицательное количество,
                # штрихкод не из этого мира, — и один такой роняет весь такт
                # опроса, если его не разобрать.
                if broken:
                    order.update(broken)
                self._next_order_id += 1
                self._orders.append(order)
                created.append(order)
            return created

    def orders(self, account: str, next_cursor: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = [o for o in self._orders if o["account"] == account]
            # Курсор из прошлой жизни симулятора обнуляем. Симулятор держит
            # заказы в памяти, а `wb_sync_cursor` лежит в Postgres и переживает
            # его перезапуск: после `docker compose up --build wb-simulator`
            # клиент просит «с 185-го», заказов пять, и он навсегда получает
            # пустой ответ. Настоящий Wildberries память не теряет, поэтому у
            # него такого не бывает; симулятор существует ради
            # воспроизводимости стенда, и терять её на своём же перезапуске
            # ему нельзя.
            if next_cursor > len(rows):
                next_cursor = 0
            page = rows[next_cursor:]
            return page, next_cursor + len(page)

    def statuses(self, account: str, order_ids: list[int]) -> list[dict[str, Any]]:
        """Статусы заказов поимённо. `POST /api/v3/orders/status` у WB.

        Отвечает только про заказы этого кабинета и только про те, которые
        знает: о неизвестном номере WB молчит, и симулятор молчит так же —
        иначе сверка никогда не увидит «задание пропало из кабинета».
        """
        with self._lock:
            wanted = set(order_ids)
            return [{"id": order["id"], "supplierStatus": order["supplierStatus"],
                     "wbStatus": order["wbStatus"]}
                    for order in self._orders
                    if order["account"] == account and order["id"] in wanted]

    def cancel_orders(self, account: str, order_ids: list[int]) -> int:
        """Ручка стенда: клиент отменил заказы в кабинете.

        У настоящего WB отмену делает покупатель или продавец, и наружу она
        видна только статусом `cancel`. Ручки «отмени» в API нет, поэтому она
        здесь, под префиксом `__stand__`.
        """
        with self._lock:
            changed = 0
            wanted = set(order_ids)
            for order in self._orders:
                if order["account"] == account and order["id"] in wanted:
                    order["supplierStatus"] = "cancel"
                    order["wbStatus"] = "canceled"
                    changed += 1
            return changed

    def create_supply(self, account: str) -> str:
        with self._lock:
            supply_id = f"WB-GI-{self._next_supply:08d}"
            self._next_supply += 1
            self._supplies[supply_id] = {"account": account, "orders": [],
                                         "done": False, "scanDt": None}
            return supply_id

    def add_orders(self, supply_id: str, order_ids: list[int]) -> bool:
        with self._lock:
            supply = self._supplies.get(supply_id)
            if supply is None:
                return False
            supply["orders"].extend(order_ids)
            for order in self._orders:
                if order["id"] in order_ids:
                    order["supplierStatus"] = "confirm"
            return True

    def deliver(self, supply_id: str) -> bool:
        with self._lock:
            supply = self._supplies.get(supply_id)
            if supply is None:
                return False
            supply["done"] = True
            for order in self._orders:
                if order["id"] in supply["orders"]:
                    order["supplierStatus"] = "complete"
            return True

    def put_stocks(self, warehouse_id: str, rows: list[dict[str, Any]]) -> int:
        with self._lock:
            store = self._stocks.setdefault(warehouse_id, {})
            for row in rows:
                store[str(row.get("sku"))] = int(row.get("amount", 0))
            return len(rows)

    def stocks(self, warehouse_id: str) -> dict[str, int]:
        with self._lock:
            return dict(self._stocks.get(warehouse_id, {}))


simulator = Simulator()
app = FastAPI(title="Wildberries FBS simulator (стенд)", version="0.1.0")


def _account(request: Request) -> str:
    # На стенде «кабинет» приходит заголовком: живых токенов нет,
    # различать кабинеты всё равно нужно ради лимита.
    return request.headers.get("X-Stand-Account", "default")


def _rate_limited(account: str) -> JSONResponse | None:
    if simulator.take_slot(account):
        return None
    # Форма ответа как у WB: 429 и заголовок с окном.
    return JSONResponse({"code": 429, "message": "too many requests"},
                        status_code=429, headers={"X-Ratelimit-Retry": "60"})


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v3/orders")
async def get_orders(request: Request, next: int = 0, limit: int = 1000) -> Any:
    account = _account(request)
    if (limited := _rate_limited(account)) is not None:
        return limited
    rows, cursor = simulator.orders(account, next)
    return {"next": cursor, "orders": rows[:limit]}


@app.post("/api/v3/orders/status")
async def orders_status(request: Request) -> Any:
    """Статусы заданий поимённо, пачкой до 1000 (приложение D)."""
    account = _account(request)
    if (limited := _rate_limited(account)) is not None:
        return limited
    body = await request.json()
    order_ids = [int(value) for value in (body.get("orders") or [])]
    if len(order_ids) > 1000:
        return JSONResponse({"code": 400, "message": "не более 1000 заданий за вызов"},
                            status_code=400)
    return {"orders": simulator.statuses(account, order_ids)}


@app.post("/api/v3/orders/stickers")
async def stickers(request: Request) -> Any:
    """Стикеры пачкой. До 100 за вызов (приложение D)."""
    account = _account(request)
    if (limited := _rate_limited(account)) is not None:
        return limited
    body = await request.json()
    order_ids = body.get("orders") or []
    if len(order_ids) > 100:
        return JSONResponse({"code": 400, "message": "не более 100 заданий за вызов"},
                            status_code=400)
    fmt = str(request.query_params.get("type", "zplv")).lower()
    if fmt not in {"png", "svg", "zplv", "zplh"}:
        return JSONResponse({"code": 400, "message": "неизвестный формат стикера"},
                            status_code=400)
    return {"stickers": [
        {"orderId": order_id, "partA": 100000 + index, "partB": 200000 + index,
         "barcode": f"WB-STICKER-{order_id}",
         "file": _sticker_body(fmt, order_id)}
        for index, order_id in enumerate(order_ids)
    ]}


def _sticker_body(fmt: str, order_id: int) -> str:
    if fmt.startswith("zpl"):
        return (f"^XA^PW464^LL320^FO20,20^A0N,28,28^FDWB {order_id}^FS"
                f"^FO20,70^BY2^BCN,120,Y,N,N^FD{order_id}^FS^XZ")
    if fmt == "svg":
        return f"<svg xmlns='http://www.w3.org/2000/svg'><text>WB {order_id}</text></svg>"
    # PNG отдаём как base64-заглушку: на стенде важен размер и формат ответа,
    # а не картинка.
    return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


@app.post("/api/v3/supplies")
async def create_supply(request: Request) -> Any:
    account = _account(request)
    if (limited := _rate_limited(account)) is not None:
        return limited
    return {"id": simulator.create_supply(account)}


@app.patch("/api/marketplace/v3/supplies/{supply_id}/orders")
async def add_supply_orders(supply_id: str, request: Request) -> Any:
    account = _account(request)
    if (limited := _rate_limited(account)) is not None:
        return limited
    body = await request.json()
    order_ids = body.get("orders") or []
    if len(order_ids) > 100:
        return JSONResponse({"code": 400, "message": "не более 100 заданий"}, status_code=400)
    if not simulator.add_orders(supply_id, order_ids):
        return JSONResponse({"code": 404, "message": "поставка не найдена"}, status_code=404)
    return JSONResponse({}, status_code=204)


@app.patch("/api/v3/supplies/{supply_id}/deliver")
async def deliver_supply(supply_id: str, request: Request) -> Any:
    account = _account(request)
    if (limited := _rate_limited(account)) is not None:
        return limited
    if not simulator.deliver(supply_id):
        return JSONResponse({"code": 404, "message": "поставка не найдена"}, status_code=404)
    return JSONResponse({}, status_code=204)


@app.put("/api/v3/stocks/{warehouse_id}")
async def put_stocks(warehouse_id: str, request: Request) -> Any:
    """Публикация остатков. Принимает пачку штрихкодов (раздел 6.4)."""
    account = _account(request)
    if (limited := _rate_limited(account)) is not None:
        return limited
    body = await request.json()
    simulator.put_stocks(warehouse_id, body.get("stocks") or [])
    return JSONResponse({}, status_code=204)


@app.get("/api/v3/stocks/{warehouse_id}")
async def get_stocks(warehouse_id: str) -> Any:
    return {"stocks": simulator.stocks(warehouse_id)}


# ------------------------------------------------------------------ управление
# Ручки стенда, которых у настоящего WB нет. Нужны прогону, чтобы «WB отдал
# пять заданий» было воспроизводимым шагом, а не ожиданием.

@app.post("/__stand__/seed-orders")
async def seed_orders(request: Request) -> Any:
    body = await request.json()
    created = simulator.seed_orders(
        account=str(body.get("account", "default")),
        count=int(body.get("count", 1)),
        barcode=str(body.get("barcode", "2000000000011")),
        deadline=body.get("deadline"),
        broken=body.get("broken"))
    return {"created": len(created), "orders": created}


@app.post("/__stand__/cancel-orders")
async def cancel_orders(request: Request) -> Any:
    """Ручка стенда: отменить заказы в кабинете.

    Отмена у WB — то, что боевой контур не обрабатывал вовсе: 2645 отмен
    лежали с пустой причиной. Чтобы проверять её на стенде, отмену надо уметь
    устроить.
    """
    body = await request.json()
    changed = simulator.cancel_orders(
        account=str(body.get("account", "default")),
        order_ids=[int(value) for value in (body.get("orders") or [])])
    return {"cancelled": changed}


@app.post("/content/v2/get/cards/list")
async def cards_list(request: Request) -> Any:
    """Карточки кабинета. Форма ответа — как у Content API Wildberries.

    Токен категории «Контент» (приложение D). На стенде не проверяется: живых
    токенов здесь нет вовсе (раздел 12).
    """
    account = _account(request)
    if (limited := _rate_limited(account)) is not None:
        return limited
    body = await request.json()
    settings = (body.get("settings") or {}).get("cursor") or {}
    limit = min(int(settings.get("limit") or 100), 1000)
    offset = int(settings.get("offset") or 0)
    rows, cursor = simulator.cards(account, offset, limit)
    return {"cards": rows, "cursor": {"offset": cursor, "total": len(rows)}}


# Ручка стенда: у настоящего Content API её нет.
@app.post("/__stand__/seed-cards")
async def seed_cards(request: Request) -> Any:
    body = await request.json()
    created = simulator.seed_cards(
        account=str(body.get("account", "default")),
        barcodes=[str(b) for b in (body.get("barcodes") or [])])
    return {"created": len(created), "cards": created}


@app.post("/__stand__/reset")
async def reset() -> dict[str, str]:
    simulator.reset()
    return {"status": "reset"}
