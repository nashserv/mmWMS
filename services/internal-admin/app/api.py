"""Админка MM-Express: онбординг, партнёры, тарифы, отчёты.

Тонкий слой над биллингом плюс журнал того, кто что сделал. Ни одного правила
о деньгах здесь нет: вторая копия правил однажды разойдётся с первой, и
разойдётся она в счёте клиента.

Экран — ванильный JS (раздел 2.2 мастера), отдаётся из `static/`.
"""
from __future__ import annotations

import pathlib
import time
from datetime import date
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .audit import Audit
from .config import app_environment, database_url, trusted_hosts
from .db import Database
from .metrics import (ACTIONS, HTTP_DURATION, HTTP_REQUESTS, SERVICE, SERVICE_READY)
from .upstream import BillingClient, IdentityClient, Upstream

BASE_PATH = "/api/admin/v1"
STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"

database = Database(database_url() or "postgresql:///internal_admin")
billing = BillingClient()
identity = IdentityClient()
audit = Audit(database)
router = APIRouter(prefix=BASE_PATH)


def plain(value: Any) -> Any:
    """Приводит ответ к JSON.

    Ответы биллинга уже разобраны из JSON и безопасны, а собственный журнал
    админки читается прямо из базы — там даты и uuid как есть.
    """
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


async def body_of(request: Request) -> dict[str, Any]:
    try:
        parsed = await request.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def who(request: Request) -> dict[str, Any]:
    try:
        return identity.whoami(token_of(request))
    except Upstream:
        return {"active": False, "reason": "identity недоступен"}


def write_through(request: Request, action: str, subject: str | None, path: str,
                  body: dict[str, Any]) -> JSONResponse:
    """Отправить в биллинг и записать, кто это сделал и чем кончилось."""
    principal = who(request)
    status, response = billing.post(path, token_of(request), body)
    audit.record(actor_id=str(principal.get("user_id") or "неизвестен"),
                 actor_roles=list(principal.get("roles") or []),
                 action=action, subject=subject, request=body,
                 status=status, response=response)
    ACTIONS.labels(action=action, outcome="ok" if status < 400 else "отказ").inc()
    return ok(response, status)


# ------------------------------------------------------------------- экраны

@router.get("/whoami")
async def whoami(request: Request) -> JSONResponse:
    return ok(who(request))


@router.get("/overview")
async def overview(request: Request, period: str | None = None) -> JSONResponse:
    """Первый экран: что требует внимания сегодня.

    Три числа, которых сегодня нет ни у кого: сколько кабинетов заведено мимо
    процесса, сколько работы склада не доехало до счёта и какие кабинеты
    убыточны. Именно их отсутствие и позволяет 6374 заданиям превращаться в
    145 начислений незамеченно.
    """
    token = token_of(request)
    month = period or date.today().strftime("%Y-%m")
    _, cabinets = billing.get("/api/billing/v1/cabinets", token, {"needs_onboarding": True})
    unbilled_status, unbilled = billing.get("/api/billing/v1/reports/unbilled", token)
    margin_status, margin = billing.get("/api/billing/v1/reports/margin", token,
                                        {"period": month})
    rows = margin.get("cabinets", []) if margin_status < 400 else []
    return ok({
        "period": month,
        "needs_onboarding": (cabinets or {}).get("cabinets", []),
        "unbilled": (unbilled or {}).get("reasons", []) if unbilled_status < 400 else [],
        # Первым идёт худший: отчёт нужен, чтобы увидеть, кто съедает смену.
        "margin": sorted(rows, key=lambda row: float(row.get("margin") or 0))[:20],
        "restricted": {"unbilled": unbilled_status >= 400, "margin": margin_status >= 400},
    })


@router.get("/partners")
async def partners(request: Request) -> JSONResponse:
    status, body = billing.get("/api/billing/v1/partners", token_of(request))
    return ok(body, status)


@router.get("/partners/{partner_id}/cabinets")
async def partner_cabinets(partner_id: str, request: Request) -> JSONResponse:
    status, body = billing.get(f"/api/billing/v1/partners/{partner_id}/cabinets",
                               token_of(request))
    return ok(body, status)


@router.get("/partners/{partner_id}/commission")
async def commission(partner_id: str, period: str, request: Request) -> JSONResponse:
    status, body = billing.get(f"/api/billing/v1/partners/{partner_id}/commission",
                               token_of(request), {"period": period})
    return ok(body, status)


@router.post("/partners")
async def create_partner(request: Request) -> JSONResponse:
    body = await body_of(request)
    return write_through(request, "партнёр заведён", str(body.get("name") or ""),
                         "/api/billing/v1/partners", body)


@router.post("/partners/{partner_id}/markups")
async def set_markup(partner_id: str, request: Request) -> JSONResponse:
    body = await body_of(request)
    return write_through(request, "наценка партнёра изменена", partner_id,
                         f"/api/billing/v1/partners/{partner_id}/markups", body)


@router.get("/cabinets")
async def cabinets(request: Request, needs_onboarding: bool = False) -> JSONResponse:
    status, body = billing.get("/api/billing/v1/cabinets", token_of(request),
                               {"needs_onboarding": needs_onboarding})
    return ok(body, status)


@router.post("/cabinets/{cabinet_id}/assign")
async def assign(cabinet_id: str, request: Request) -> JSONResponse:
    body = await body_of(request)
    return write_through(request, "кабинет закреплён за партнёром", cabinet_id,
                         f"/api/billing/v1/cabinets/{cabinet_id}/assign", body)


@router.post("/onboarding")
async def onboard(request: Request) -> JSONResponse:
    """Онбординг клиента одним нажатием.

    Договор → тариф → партнёр → кабинет WB → владелец в `wms` → начальный
    остаток. Ни одного ручного запроса в базу — ровно то, чего не хватает
    сегодня: 21 кабинет WB заведён при 4 записях в `sellers`.
    """
    body = await body_of(request)
    return write_through(request, "клиент заведён", str(body.get("seller_external_id") or ""),
                         "/api/billing/v1/onboarding", body)


@router.get("/tariffs")
async def tariffs(request: Request) -> JSONResponse:
    status, body = billing.get("/api/billing/v1/tariffs", token_of(request))
    return ok(body, status)


@router.post("/tariffs")
async def create_tariff(request: Request) -> JSONResponse:
    body = await body_of(request)
    return write_through(request, "тариф заведён", str(body.get("code") or ""),
                         "/api/billing/v1/tariffs", body)


@router.post("/tariffs/{tariff_id}/versions")
async def add_version(tariff_id: str, request: Request) -> JSONResponse:
    body = await body_of(request)
    return write_through(request, "версия тарифа заведена", tariff_id,
                         f"/api/billing/v1/tariffs/{tariff_id}/versions", body)


@router.post("/tariff-versions/{version_id}/approve")
async def approve(version_id: str, request: Request) -> JSONResponse:
    """Утверждение цены. Кто утвердил — в журнале, а не в памяти."""
    body = await body_of(request)
    principal = who(request)
    body.setdefault("approved_by", str(principal.get("user_id") or ""))
    return write_through(request, "версия тарифа утверждена", version_id,
                         f"/api/billing/v1/tariff-versions/{version_id}/approve", body)


@router.get("/accruals")
async def accruals(request: Request, cabinet_id: str | None = None,
                   period: str | None = None) -> JSONResponse:
    params = {key: value for key, value in
              (("cabinet_id", cabinet_id), ("period", period)) if value}
    status, body = billing.get("/api/billing/v1/accruals", token_of(request), params)
    return ok(body, status)


@router.get("/reports/unbilled")
async def unbilled(request: Request) -> JSONResponse:
    status, body = billing.get("/api/billing/v1/reports/unbilled", token_of(request))
    return ok(body, status)


@router.get("/reports/margin")
async def margin(request: Request, period: str) -> JSONResponse:
    status, body = billing.get("/api/billing/v1/reports/margin", token_of(request),
                               {"period": period})
    return ok(body, status)


@router.get("/reports/shift")
async def shift(request: Request, day: str | None = None) -> JSONResponse:
    """Витрина начальника склада: кто сколько сделал за смену."""
    status, body = billing.get("/api/billing/v1/reports/shift", token_of(request),
                               {"day": day} if day else None)
    return ok(body, status)


@router.post("/periods/{period}/close")
async def close_period(period: str, request: Request) -> JSONResponse:
    body = await body_of(request)
    principal = who(request)
    body.setdefault("closed_by", str(principal.get("user_id") or ""))
    return write_through(request, "период закрыт", period,
                         f"/api/billing/v1/periods/{period}/close", body)


@router.post("/periods/{period}/allocate")
async def allocate(period: str, request: Request) -> JSONResponse:
    return write_through(request, "расходы разнесены", period,
                         f"/api/billing/v1/periods/{period}/allocate", {})


@router.post("/expenses")
async def expense(request: Request) -> JSONResponse:
    body = await body_of(request)
    return write_through(request, "расход добавлен", str(body.get("period") or ""),
                         "/api/billing/v1/expenses", body)


@router.post("/invoices")
async def invoice(request: Request) -> JSONResponse:
    body = await body_of(request)
    return write_through(request, "счёт выставлен", str(body.get("cabinet_id") or ""),
                         "/api/billing/v1/invoices", body)


@router.post("/invoices/{invoice_id}/pay")
async def pay(invoice_id: str, request: Request) -> JSONResponse:
    """Отметка об оплате. С неё начинается вознаграждение партнёра, не раньше."""
    return write_through(request, "счёт оплачен", invoice_id,
                         f"/api/billing/v1/invoices/{invoice_id}/pay", {})


@router.get("/roles")
async def roles() -> JSONResponse:
    return ok({"roles": identity.roles()})


@router.post("/grants")
async def grant(request: Request) -> JSONResponse:
    body = await body_of(request)
    principal = who(request)
    body.setdefault("granted_by", str(principal.get("user_id") or ""))
    status, response = identity.grant(token_of(request), body)
    audit.record(actor_id=str(principal.get("user_id") or "неизвестен"),
                 actor_roles=list(principal.get("roles") or []),
                 action="роль выдана", subject=str(body.get("user_id") or ""),
                 request=body, status=status, response=response)
    return ok(response, status)


@router.get("/audit")
async def audit_log(limit: int = 100) -> JSONResponse:
    """Кто, когда и что сделал. Половина правила «ни одного ручного SQL»."""
    return ok(audit.recent(limit))


# --------------------------------------------------------------- приложение

def create_app() -> FastAPI:
    environment = app_environment()
    application = FastAPI(title="MM-Express — админка", version="1.0.0")
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts(environment))

    @application.middleware("http")
    async def observe(request: Request, call_next: Any) -> Any:
        started = time.monotonic()
        response = await call_next(request)
        HTTP_REQUESTS.labels(service=SERVICE, status=str(response.status_code)).inc()
        HTTP_DURATION.labels(service=SERVICE).observe(max(0.0, time.monotonic() - started))
        return response

    @application.exception_handler(Upstream)
    async def upstream_down(_: Request, error: Upstream) -> JSONResponse:
        # 502, а не 500: сломалась не админка, и чинить надо не её.
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
