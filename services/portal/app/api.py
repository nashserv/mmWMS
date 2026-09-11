"""ЛК клиента.

Три обещания файла 04, и все три — про одно и то же: клиент видит то же, что
видим мы, теми же числами.

1. **Остаток — из склада, а не из зеркала.** Сегодня портал показывает 3528
   единиц при реальном остатке 92, потому что зеркалит кабинет Wildberries.
   Здесь остаток спрашивается у `wms` при каждом показе и нигде не хранится.
2. **Начисления с видимой наценкой партнёра.** Клиент платит 45 ₽ и должен
   понимать, что 30 идёт MM-Express, а 15 — партнёру, который его привёл, а
   не считать нас источником завышенной цены.
3. **Акт за период клиент выгружает сам**, без участия бухгалтера.
"""
from __future__ import annotations

import csv
import os
import io
import pathlib
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

# Та же зона, что в биллинге: период в ЛК и период в счёте обязаны быть одним
# и тем же месяцем. По UTC ночная смена первого числа попадала в разные.
WAREHOUSE_ZONE = ZoneInfo(os.getenv("BILLING_TIMEZONE", "Europe/Moscow"))


def today() -> date:
    return datetime.now(timezone.utc).astimezone(WAREHOUSE_ZONE).date()

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import app_environment, database_url, trusted_hosts
from .db import Database
from .metrics import EXPORTS, HTTP_DURATION, HTTP_REQUESTS, SERVICE, SERVICE_READY
from .upstream import BillingClient, IdentityClient, Upstream, WmsClient

BASE_PATH = "/api/portal/v1"
STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"

database = Database(database_url() or "postgresql:///portal")
wms = WmsClient()
billing = BillingClient()
identity = IdentityClient()
router = APIRouter(prefix=BASE_PATH)


class NoCabinet(RuntimeError):
    """Токен есть, а кабинета за ним нет."""


def plain(value: Any) -> Any:
    import datetime
    import uuid as uuid_module

    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, uuid_module.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def ok(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(plain(payload), status_code=status_code)


def token_of(request: Request) -> str | None:
    return request.headers.get("authorization")


def client_of(request: Request) -> dict[str, Any]:
    """Клиент за токеном и его кабинет.

    Кабинет берётся из области роли (`seller` в identity), а не из параметра
    запроса: иначе достаточно подставить чужой ключ продавца, чтобы увидеть
    чужой остаток.
    """
    principal = identity.whoami(token_of(request))
    if not principal.get("active"):
        raise NoCabinet(str(principal.get("reason") or "не представлен"))
    sellers = list(principal.get("sellers") or [])
    if not sellers:
        raise NoCabinet(
            "у пользователя нет кабинета: роль владельца или менеджера выдаётся "
            "с областью seller, иначе показывать нечего")
    return {"user_id": principal.get("user_id"), "seller": sellers[0], "sellers": sellers,
            "roles": principal.get("roles") or []}


def record_export(actor_id: str, seller: str, kind: str, period: str | None,
                  rows_count: int) -> None:
    with database.transaction() as cursor:
        cursor.execute(
            "INSERT INTO portal_export (actor_id, seller_external_id, kind, period, rows_count) "
            "VALUES (%s, %s, %s, %s, %s)",
            (actor_id, seller, kind, period, rows_count))
    EXPORTS.labels(kind=kind).inc()


# ------------------------------------------------------------------- остаток

@router.get("/me")
async def me(request: Request) -> JSONResponse:
    return ok(client_of(request))


@router.get("/stock")
async def stock(request: Request, barcode: str | None = None) -> JSONResponse:
    """Остаток по товарам, коробкам и ячейкам — из склада.

    Одно число, потому что источник один. Никакого зеркала кабинета WB здесь
    нет и не будет: именно оно даёт 3528 против 92.
    """
    client = client_of(request)
    rows = wms.stock(client["seller"])
    if barcode:
        rows = [row for row in rows if str(row.get("barcode")) == barcode]
    placements = wms.placements(client["seller"], barcode)
    return ok({
        "seller_external_id": client["seller"],
        "source": "wms",
        "stock": rows,
        "placements": placements,
        "total_good": sum(int(row.get("good") or 0) for row in rows),
        "total_reserved": sum(int(row.get("reserved") or 0) for row in rows),
        "total_available": sum(int(row.get("available") or 0) for row in rows),
    })


# --------------------------------------------------------------- начисления

@router.get("/accruals")
async def accruals(request: Request, period: str | None = None) -> JSONResponse:
    """Расшифровка с видимой наценкой партнёра.

    Итог без разбивки читается как «MM-Express берёт 45». Разбивка объясняет,
    что 30 — наши, 15 — того, кто привёл клиента.
    """
    client = client_of(request)
    month = period or today().strftime("%Y-%m")
    status, body = billing.get("/api/billing/v1/accruals", token_of(request),
                               {"period": month, "limit": 1000,
                                "seller": client["seller"]})
    if status >= 400:
        return ok(body, status)
    rows = body.get("accruals", [])

    # Итог НЕ складывается здесь. На полутора тысячах операций страница
    # заканчивалась на тысяче, и сумма по ней расходилась и со счётом, и с
    # админкой — каждая показывала своё число, и спор «сколько я должен»
    # решался тем, кто аккуратнее сложил.
    status, summary = billing.get("/api/billing/v1/accruals/summary", token_of(request),
                                  {"period": month, "seller": client["seller"]})
    if status >= 400:
        return ok(summary, status)
    return ok({
        "period": month,
        "accruals": rows,
        "next_cursor": body.get("next_cursor"),
        "totals": summary.get("totals", {}),
        "invoice": summary.get("invoice"),
        "explanation": "Клиент платит тариф MM-Express плюс наценку партнёра, "
                       "который ведёт кабинет. Обе части показаны отдельно.",
    })


@router.get("/accruals.csv")
async def accruals_csv(request: Request, period: str | None = None) -> Response:
    """Акт за период. Клиент выгружает сам, без участия бухгалтера (файл 04)."""
    client = client_of(request)
    month = period or today().strftime("%Y-%m")
    status, body = billing.get("/api/billing/v1/accruals", token_of(request),
                               {"period": month, "limit": 1000})
    if status >= 400:
        return ok(body, status)
    rows = body.get("accruals", [])

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["дата", "услуга", "количество", "цена MM-Express", "наценка партнёра",
                     "к оплате", "из них MM-Express", "из них партнёру", "партнёр"])
    for row in rows:
        writer.writerow(_safe([
            row["occurred_on"], row["service"], row["quantity"], row["unit_price"],
            row["markup"], row["amount"], row["net_amount"], row["partner_amount"],
            row.get("partner_name") or ""]))
    # Итог в акте — тот же, что в ЛК и в счёте: считает его база.
    status, summary = billing.get("/api/billing/v1/accruals/summary", token_of(request),
                                  {"period": month, "seller": client["seller"]})
    totals = summary.get("totals", {}) if status < 400 else {}
    writer.writerow([])
    writer.writerow(["итого", "", totals.get("operations", len(rows)), "", "",
                     totals.get("amount", ""), totals.get("net_amount", ""),
                     totals.get("partner_amount", ""), ""])

    record_export(str(client["user_id"]), client["seller"], "act", month, len(rows))
    # Windows-1251 не используем: файл читают и на Linux, и в браузере. BOM —
    # чтобы Excel не открыл кириллицу кракозябрами.
    payload = ("﻿" + buffer.getvalue()).encode("utf-8")
    return Response(payload, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="act-{client["seller"]}-{month}.csv"'})


@router.get("/exports")
async def exports(request: Request) -> JSONResponse:
    client = client_of(request)
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM portal_export WHERE seller_external_id = %s ORDER BY at DESC LIMIT 50",
            (client["seller"],))
        return ok({"exports": [dict(row) for row in cursor.fetchall()]})


# --------------------------------------------------------------- приложение

def create_app() -> FastAPI:
    environment = app_environment()
    application = FastAPI(title="MM-Express — личный кабинет", version="1.0.0")
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts(environment))

    @application.middleware("http")
    async def observe(request: Request, call_next: Any) -> Any:
        started = time.monotonic()
        response = await call_next(request)
        HTTP_REQUESTS.labels(service=SERVICE, status=str(response.status_code)).inc()
        HTTP_DURATION.labels(service=SERVICE).observe(max(0.0, time.monotonic() - started))
        return response

    @application.exception_handler(NoCabinet)
    async def no_cabinet(_: Request, error: NoCabinet) -> JSONResponse:
        return ok({"error": str(error)}, 403)

    @application.exception_handler(Upstream)
    async def upstream_down(_: Request, error: Upstream) -> JSONResponse:
        # 502: сломался не портал. Показать вчерашнее число вместо отказа —
        # это и есть 3528 против 92, только другим путём.
        return ok({"error": str(error)}, 502)

    @application.get("/healthz")
    async def healthz() -> JSONResponse:
        return ok({"status": "ok"})

    @application.get("/readyz")
    async def readyz() -> JSONResponse:
        ready = database.ready()
        SERVICE_READY.labels(service=SERVICE).set(1 if ready else 0)
        return ok({"status": "ready" if ready else "degraded", "database": ready},
                  200 if ready else 503)

    @application.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    application.include_router(router)

    @application.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    if STATIC.is_dir():
        application.mount("/static", StaticFiles(directory=STATIC), name="static")
    return application


app = create_app()


# Значение, начинающееся с `=`, `+`, `-`, `@`, Excel считает ФОРМУЛОЙ и
# выполняет при открытии файла. Название услуги и имя партнёра приходят из
# базы, а туда — из онбординга: достаточно назвать партнёра
# `=HYPERLINK(...)`, чтобы акт клиента стал исполняемым.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _safe(values: list[Any]) -> list[Any]:
    """Обезвреживает значения, которые Excel принял бы за формулу."""
    guarded: list[Any] = []
    for value in values:
        text = "" if value is None else str(value)
        guarded.append("'" + text if text.startswith(_FORMULA_STARTS) else value)
    return guarded
