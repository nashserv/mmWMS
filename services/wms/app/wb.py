"""Клиент Wildberries FBS. Единственное место, где `wms` ходит наружу.

Все вызовы — приложение D мастера. Граница вынесена в отдельный модуль ровно
затем, чтобы «ни одного HTTP-вызова внутри транзакции» (инвариант 2) можно было
проверить взглядом: транзакция живёт в `service.py`, сеть — здесь, и эти два
файла не пересекаются.

Токен берётся у секрет-провайдера по `secret_ref` и не покидает этот модуль:
в события, логи и ответы API он не попадает никогда (инвариант 15). В строку
ошибки — тоже: сообщения WB пересказываются кодом и статусом, тело ответа
целиком наружу не идёт.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import LOCAL_ENVIRONMENTS, app_environment
from .rate_limit import Pace
from .secrets import SecretProvider, provider

log = logging.getLogger("wms.wb")

def default_base_url() -> str:
    """Адрес Wildberries. Читается на каждый вызов, а не на импорт.

    На стенде отвечает симулятор; в бою — api.wildberries.ru. Живого
    Wildberries на стенде быть не должно (раздел 12), поэтому адрес приходит
    снаружи и умолчания на боевой хост здесь нет.
    """
    return (os.getenv("WB_API_URL") or os.getenv("WB_SIMULATOR_URL")
            or "http://wb-simulator:8090")

# Форматы стикера, которые поддерживает WB. Целевой — zplv (раздел 6.6);
# png оставлен откатом на случай, если реальный принтер не распознает ZPL
# (раздел 13, вопрос 2 ещё открыт).
STICKER_FORMATS = ("zplv", "zplh", "svg", "png")
STICKER_BATCH = 100          # приложение D: до 100 стикеров за вызов
SUPPLY_ORDERS_BATCH = 100    # приложение D: до 100 заданий в поставку за вызов


class WbError(RuntimeError):
    """Отказ Wildberries. Текст безопасен для лога: без тела и без токена."""

    def __init__(self, status: int, code: str, *, retry_after: float = 0.0) -> None:
        super().__init__(f"Wildberries ответил {status} ({code})")
        self.status = status
        self.code = code
        self.retry_after = retry_after

    @property
    def rate_limited(self) -> bool:
        return self.status == 429

    @property
    def conflict(self) -> bool:
        return self.status == 409

    @property
    def auth_rejected(self) -> bool:
        return self.status in (401, 403)


@dataclass
class WbOrder:
    """Сборочное задание в форме Wildberries.

    Поле `skus` названо так у самого WB, но лежит в нём штрихкод (раздел 3.2) —
    отсюда `barcode` рядом: дальше по коду догадываться об этом уже не нужно.
    """

    wb_order_id: int
    uid: str | None
    barcode: str | None
    quantity: int
    deadline: str | None
    supplier_status: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_wb(cls, row: dict[str, Any]) -> "WbOrder":
        skus = row.get("skus") or []
        return cls(
            wb_order_id=int(row["id"]),
            uid=str(row.get("rid")) if row.get("rid") is not None else None,
            barcode=str(skus[0]) if skus else None,
            # У FBS-задания WB всегда одна единица; поле оставлено на случай,
            # если это когда-нибудь перестанет быть правдой.
            quantity=int(row.get("quantity") or 1),
            deadline=row.get("ddate") or row.get("dueDate"),
            supplier_status=row.get("supplierStatus"),
            raw=row)


@dataclass
class WbSticker:
    wb_order_id: int
    payload: bytes
    part_a: int | None = None
    part_b: int | None = None
    barcode: str | None = None


class WbClient:
    """Один кабинет Wildberries. Экземпляр держит темп вызовов этого кабинета."""

    def __init__(self, *, account_external_id: str, secret_ref: str,
                 base_url: str | None = None, timeout: float = 15.0,
                 secrets: SecretProvider | None = None,
                 client: httpx.Client | None = None) -> None:
        self._account = account_external_id
        self._secret_ref = secret_ref
        self._base_url = (base_url or default_base_url()).rstrip("/")
        self._secrets = secrets or provider()
        self._pace = Pace()
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "WbClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------ транспорт

    def _headers(self) -> dict[str, str]:
        token = self._secrets.resolve(self._secret_ref)
        return {
            "Authorization": token,
            # Симулятору стенда нужно различать кабинеты: живых токенов на нём
            # нет, а лимит считается по кабинету (services/wb-simulator).
            "X-Stand-Account": self._account,
            "Accept": "application/json",
        }

    def _call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
              json: Any = None) -> Any:
        self._pace.wait()
        try:
            response = self._client.request(
                method, f"{self._base_url}{path}", params=params, json=json,
                headers=self._headers())
        except httpx.HTTPError as failure:
            # Тип ошибки, но не её текст: в текст httpx кладёт URL, а в URL
            # у некоторых интеграций уезжает ключ.
            raise WbError(0, type(failure).__name__) from None

        if response.status_code >= 400:
            raise WbError(response.status_code, _error_code(response),
                          retry_after=_retry_after(response))
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise WbError(response.status_code, "MALFORMED_JSON") from None

    # -------------------------------------------------------------- задания

    def orders(self, *, cursor: int = 0, limit: int = 1000) -> tuple[list[WbOrder], int]:
        """`GET /api/v3/orders` — новые и исторические задания с курсором.

        Курсор возвращается вместе со страницей: перекрытие делает опросчик,
        а не клиент, — здесь только то, что отдал Wildberries.
        """
        body = self._call("GET", "/api/v3/orders", params={"next": cursor, "limit": limit})
        rows = body.get("orders") or []
        return [WbOrder.from_wb(row) for row in rows], int(body.get("next", cursor))

    # ------------------------------------------------------------- стикеры

    def stickers(self, order_ids: list[int], *, sticker_format: str = "zplv") -> list[WbSticker]:
        """`POST /api/v3/orders/stickers` — пачкой до 100 (приложение D).

        Тянется заранее, сразу после резерва, а не в момент упаковки: к
        нажатию «печать» этикетка обязана уже лежать локально (инвариант 9).
        """
        if not order_ids:
            return []
        if len(order_ids) > STICKER_BATCH:
            raise ValueError(f"не более {STICKER_BATCH} стикеров за вызов (приложение D)")
        if sticker_format not in STICKER_FORMATS:
            raise ValueError(f"неизвестный формат стикера: {sticker_format!r}")
        body = self._call("POST", "/api/v3/orders/stickers",
                          params={"type": sticker_format, "width": 58, "height": 40},
                          json={"orders": order_ids})
        stickers = []
        for row in body.get("stickers") or []:
            payload = row.get("file") or ""
            stickers.append(WbSticker(
                wb_order_id=int(row["orderId"]),
                payload=_sticker_bytes(payload, sticker_format),
                part_a=row.get("partA"), part_b=row.get("partB"),
                barcode=row.get("barcode")))
        return stickers

    # ------------------------------------------------------------- поставки

    def create_supply(self, name: str | None = None) -> str:
        """`POST /api/v3/supplies`. Одна поставка — один кабинет (приложение D)."""
        body = self._call("POST", "/api/v3/supplies", json={"name": name or "MM-Express"})
        return str(body.get("id") or body.get("supplyId") or "")

    def add_orders(self, supply_id: str, order_ids: list[int]) -> None:
        """`PATCH /api/marketplace/v3/supplies/{id}/orders` — до 100 за вызов."""
        if not order_ids:
            return
        if len(order_ids) > SUPPLY_ORDERS_BATCH:
            raise ValueError(f"не более {SUPPLY_ORDERS_BATCH} заданий за вызов (приложение D)")
        self._call("PATCH", f"/api/marketplace/v3/supplies/{supply_id}/orders",
                   json={"orders": order_ids})

    def release_from_supply(self, supply_id: str, order_id: int) -> None:
        """Освобождает заказ из поставки при отмене задания (раздел 6.6).

        Перенесено из адаптера боевого шлюза как есть: у WB это удаление
        задания из поставки, и без него отменённый заказ уедет в машину.
        """
        self._call("DELETE", f"/api/marketplace/v3/supplies/{supply_id}/orders/{order_id}")

    def deliver(self, supply_id: str) -> None:
        """`PATCH /api/v3/supplies/{id}/deliver` — передача поставки.

        С релизов 2026-03/04 перед вызовом обязательна валидация `metaDetails`;
        её делает вызывающий, потому что данные для неё лежат в базе, а не здесь.
        """
        self._call("PATCH", f"/api/v3/supplies/{supply_id}/deliver")

    # -------------------------------------------------------------- остатки

    def put_stocks(self, warehouse_id: int | str, rows: list[dict[str, Any]]) -> int:
        """`PUT /api/v3/stocks/{warehouseId}` — публикация остатков пачкой.

        Вызов уходит сразу после движения, без таймеров и накопления
        (раздел 6.4). Пачка возникает не от ожидания, а от того, что за время
        предыдущего вызова успело измениться несколько SKU.
        """
        if not rows:
            return 0
        self._call("PUT", f"/api/v3/stocks/{warehouse_id}", json={"stocks": rows})
        return len(rows)


def _error_code(response: httpx.Response) -> str:
    """Код отказа из тела WB. Само тело наружу не идёт."""
    try:
        body = response.json()
    except ValueError:
        return "UNPARSEABLE"
    code = body.get("code") if isinstance(body, dict) else None
    return str(code) if code is not None else f"HTTP_{response.status_code}"


def _retry_after(response: httpx.Response) -> float:
    for header in ("X-Ratelimit-Retry", "Retry-After"):
        value = response.headers.get(header)
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                continue
    return 60.0 if response.status_code == 429 else 0.0


def _sticker_bytes(payload: str, sticker_format: str) -> bytes:
    """Тело стикера в байтах.

    ZPL и SVG приходят текстом и хранятся текстом: 1–3 КБ против 20–100 КБ у
    картинки, и принтер печатает ZPL нативно, без растеризации драйвером
    (раздел 6.6). PNG приходит в base64 — раскодируем, чтобы в базе лежало
    ровно то, что уйдёт в устройство.
    """
    if sticker_format == "png":
        import base64
        try:
            return base64.b64decode(payload, validate=True)
        except Exception:
            return payload.encode("utf-8")
    return payload.encode("utf-8")


def writes_allowed(mode: str) -> bool:
    """Можно ли писать в Wildberries для кабинета в этом режиме.

    `shadow` означает «только `GET /api/v3/orders`, ни одной записи»: ни
    стикеров, ни поставок, ни публикации остатков (раздел 11, шаг 2). Смысл
    запрета — живой токен отправит настоящие команды в кабинет клиента:
    создаст поставку, переведёт задание в собранное, перезапишет остатки.
    Необратимо, и узнает об этом клиент, а не мы.

    Исключение ровно одно и требует двух независимых условий сразу:
    окружение локальное (test/local/development) И адрес Wildberries не задан,
    то есть отвечает симулятор стенда. На стенде живых кабинетов нет по
    построению — это проверяется воротами token-guard при каждом подъёме, —
    и запрещать там запись значит запрещать проверять цикл целиком.

    В staging и prod `shadow` абсолютен: одного условия для записи мало,
    а обоих там не бывает.
    """
    if (mode or "").strip().lower() == "live":
        return True
    return app_environment() in LOCAL_ENVIRONMENTS and not os.getenv("WB_API_URL", "").strip()
