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
        with self._lock:
            self._orders: list[dict[str, Any]] = []
            self._supplies: dict[str, dict[str, Any]] = {}
            self._stocks: dict[str, dict[str, int]] = {}
            self._calls: dict[str, list[float]] = {}
            self._next_order_id = 900001
            self._next_supply = 1

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

    def seed_orders(self, account: str, count: int, barcode: str,
                    deadline: str | None = None) -> list[dict[str, Any]]:
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
                self._next_order_id += 1
                self._orders.append(order)
                created.append(order)
            return created

    def orders(self, account: str, next_cursor: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = [o for o in self._orders if o["account"] == account][next_cursor:]
            return rows, next_cursor + len(rows)

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
        deadline=body.get("deadline"))
    return {"created": len(created), "orders": created}


@app.post("/__stand__/reset")
async def reset() -> dict[str, str]:
    simulator.reset()
    return {"status": "reset"}
