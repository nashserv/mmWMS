"""HTTP-интерфейс сервиса billing.

Форма как у остальных сервисов платформы (раздел 2.2): FastAPI, TrustedHost,
/healthz — процесс жив, /readyz — база доступна, /metrics — Prometheus.

Деньги отдаются строками, а не числами: float в JSON превращает 45.00 в
45.000000000000004, и объяснять это придётся клиенту, а не компьютеру.
"""
from __future__ import annotations

import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import metrics
from . import repositories as repo
from .admin import Admin, InvoiceConflict, OnboardingError
from .config import app_environment, database_url, trusted_hosts
from .db import Database
from .domain import Service, today
from . import auth
from .principal import (Forbidden, Principal, Principals, Unauthorized, require_partner,
                        require_write, summary, visible_cabinet_ids)
from .service import BillingService
from .wms_client import WmsClient

BASE_PATH = "/api/billing/v1"

database = Database(database_url() or "postgresql:///billing")
billing = BillingService(database)
admin = Admin(database, WmsClient())
principals = Principals()
router = APIRouter(prefix=BASE_PATH)


def caller(request: Request) -> Principal:
    """Кто спрашивает. Отказ — исключение, а не пустая выдача.

    Пустой список вместо отказа читается как «у вас ничего нет» и прячет
    настоящую причину: человеку не выдали роль либо identity лежит.
    """
    return principals.of(request.headers.get("authorization"))


def plain(value: Any) -> Any:
    """Приводит ответ к JSON без потери копеек."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [plain(item) for item in value]
    return value


def ok(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(plain(payload), status_code=status_code)


async def body_of(request: Request) -> dict[str, Any]:
    try:
        parsed = await request.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def as_date(value: Any, fallback: date | None = None) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value.strip()[:10])
    if fallback is not None:
        return fallback
    raise OnboardingError("нужна дата в формате ГГГГ-ММ-ДД")


def as_money(value: Any) -> Decimal:
    return Decimal(str(value))


# ----------------------------------------------------------------- служебное

@router.get("/health")
def health() -> JSONResponse:
    return ok({"status": "ok", "service": "billing"})


# ------------------------------------------------------------------ партнёры

@router.get("/partners")
def list_partners(request: Request) -> JSONResponse:
    """Дерево менеджеров. Администратору — целиком, партнёру — своя ветка."""
    principal = caller(request)
    tree = admin.partner_tree()
    if principal.unrestricted:
        return ok({"partners": tree})
    with database.cursor() as cursor:
        from .principal import visible_partner_ids

        visible = set(visible_partner_ids(cursor, principal) or ())
    return ok({"partners": [row for row in tree if str(row["id"]) in visible]})


@router.post("/partners")
async def create_partner(request: Request) -> JSONResponse:
    require_write(caller(request))
    body = await body_of(request)
    name = str(body.get("name") or "").strip()
    if not name:
        return ok({"error": "имя партнёра обязательно"}, 400)
    return ok({"partner": admin.create_partner(
        name, parent_id=body.get("parent_id"), user_id=body.get("user_id"))}, 201)


@router.get("/partners/{partner_id}/cabinets")
def partner_cabinets(partner_id: str, request: Request,
                           on: str | None = None) -> JSONResponse:
    with database.cursor() as cursor:
        require_partner(cursor, caller(request), partner_id)
    return ok({"cabinets": admin.partner_cabinets(partner_id, on=as_date(on, today()))})


@router.post("/partners/{partner_id}/markups")
async def set_markup(partner_id: str, request: Request) -> JSONResponse:
    """Наценка партнёра по услуге, с даты. Прошлые периоды не пересчитываются."""
    require_write(caller(request))
    body = await body_of(request)
    try:
        layer = admin.set_markup(
            partner_id, str(body.get("service")), as_money(body.get("markup", 0)),
            as_date(body.get("from_date"), today()), cabinet_id=body.get("cabinet_id"))
    except ValueError as error:
        return ok({"error": str(error)}, 400)
    return ok({"price_layer": layer}, 201)


@router.get("/partners/{partner_id}/commission")
def partner_commission(partner_id: str, period: str, request: Request) -> JSONResponse:
    """Своя комиссия и комиссия ветки. Чужую не показываем даже по прямой ссылке."""
    with database.cursor() as cursor:
        require_partner(cursor, caller(request), partner_id)
    return ok(billing.commission(partner_id, period))


# ------------------------------------------------------------------ кабинеты

@router.get("/cabinets")
def list_cabinets(request: Request, needs_onboarding: bool = False) -> JSONResponse:
    principal = caller(request)
    cabinets = admin.cabinets(only_needing_onboarding=needs_onboarding)
    if principal.unrestricted:
        return ok({"cabinets": cabinets})
    with database.cursor() as cursor:
        visible = set(visible_cabinet_ids(cursor, principal) or ())
    return ok({"cabinets": [row for row in cabinets if str(row["id"]) in visible]})


@router.post("/cabinets/{cabinet_id}/assign")
async def assign_cabinet(cabinet_id: str, request: Request) -> JSONResponse:
    require_write(caller(request))
    body = await body_of(request)
    try:
        assignment = admin.assign_cabinet(
            cabinet_id, str(body.get("partner_id")), str(body.get("role", "account_manager")),
            as_date(body.get("from_date"), today()), comment=body.get("comment"))
    except repo.AssignmentConflict as error:
        return ok({"error": str(error)}, 409)
    except Exception as error:  # noqa: BLE001 — конфликт схемы это 409, не 500
        conflict = _conflict_or_raise(error)
        if conflict is not None:
            return conflict
        if isinstance(error, ValueError):
            return ok({"error": str(error)}, 400)
        raise
    return ok({"assignment": assignment}, 201)


@router.post("/onboarding")
async def onboard(request: Request) -> JSONResponse:
    """Онбординг клиента одним потоком. Ни одного ручного запроса в базу."""
    require_write(caller(request))
    body = await body_of(request)
    try:
        result = admin.onboard(
            seller_external_id=str(body["seller_external_id"]),
            name=str(body.get("name") or body["seller_external_id"]),
            inn=body.get("inn"),
            contract_reference=str(body.get("contract_reference")
                                   or f"CONTRACT-{body['seller_external_id']}"),
            partner_id=body.get("partner_id"),
            wb_account_external_id=body.get("wb_account_external_id"),
            secret_ref=body.get("secret_ref"),
            tariffs=body.get("tariffs") or {},
            opening_stock=body.get("opening_stock") or [],
            from_date=as_date(body.get("from_date"), today()))
    except KeyError as error:
        return ok({"error": f"не хватает поля {error}"}, 400)
    except (repo.AssignmentConflict, InvoiceConflict) as error:
        return ok({"error": str(error)}, 409)
    except Exception as error:  # noqa: BLE001 — конфликт схемы это 409, не 500
        conflict = _conflict_or_raise(error)
        if conflict is not None:
            return conflict
        if isinstance(error, OnboardingError | ValueError):
            return ok({"error": str(error)}, 400)
        raise
    return ok(result, 201 if result["state"] == "ok" else 202)


# -------------------------------------------------------------------- тарифы

@router.get("/tariffs")
def list_tariffs(request: Request) -> JSONResponse:
    """Прайс видит любой, кто представился. Цены — не секрет, но и не улица:
    по ним видно, сколько платят клиенты и какая у склада маржа."""
    caller(request)
    return ok({"tariffs": admin.tariffs()})


@router.post("/tariffs")
async def create_tariff(request: Request) -> JSONResponse:
    require_write(caller(request))
    body = await body_of(request)
    try:
        tariff = admin.create_tariff(
            str(body["code"]), str(body["service"]), str(body.get("name") or body["code"]),
            str(body.get("unit") or "шт"), is_default=bool(body.get("is_default")))
    except (KeyError, ValueError) as error:
        return ok({"error": str(error)}, 400)
    return ok({"tariff": tariff}, 201)


@router.post("/tariffs/{tariff_id}/versions")
async def add_version(tariff_id: str, request: Request) -> JSONResponse:
    require_write(caller(request))
    body = await body_of(request)
    try:
        version = admin.add_version(
            tariff_id, as_date(body.get("effective_from"), today()),
            body.get("tiers") or [], partner_fee=as_money(body.get("partner_fee", 0)),
            accumulation=str(body.get("accumulation") or "per_event"))
    except (OnboardingError, ValueError, KeyError) as error:
        return ok({"error": str(error)}, 400)
    return ok({"version": version}, 201)


@router.post("/tariff-versions/{version_id}/approve")
async def approve_version(version_id: str, request: Request) -> JSONResponse:
    require_write(caller(request))
    body = await body_of(request)
    try:
        return ok({"version": admin.approve_version(version_id,
                                                    str(body.get("approved_by") or ""))})
    except OnboardingError as error:
        return ok({"error": str(error)}, 400)


# -------------------------------------------------------------------- события

@router.post("/events")
async def ingest(request: Request) -> JSONResponse:
    """Принять событие шины напрямую.

    Тот же путь, что у billing-inbox-consumer, буква в букву: маршрут нужен,
    чтобы тарификацию можно было проверить без брокера — и чтобы админка могла
    переиграть событие из billing_unbilled после исправления справочника.

    Маршрут ПИШЕТ ДЕНЬГИ: событие превращается в начисление клиенту. Поэтому
    либо человек с правом записи, либо сервисный токен консьюмера — тот не
    человек, но и не улица.
    """
    token = auth.bearer(request.headers)
    if not auth.service_token_matches(token):
        require_write(caller(request))
    return ok(billing.ingest(await body_of(request)))


@router.post("/unbilled/{event_id}/replay")
def replay_unbilled(event_id: str, request: Request) -> JSONResponse:
    """Переиграть событие, которое не дошло до счёта.

    Тариф не был утверждён, клиент не был заведён, справочник поправили — и
    событие надо провести заново. Раньше для этого слали его тело в
    `POST /events` руками: тело брали из отчёта, правили на глаз, и в счёт
    клиенту уходило то, что напечатал человек, а не то, что произошло на
    складе.

    Здесь тело берётся ИЗ INBOX — ровно то, что пришло по шине. Переигрывается
    только неначисленное: `accrued` этот маршрут не тронет.
    """
    require_write(caller(request))
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT event_id, event_type, tenant_id, correlation_id, payload, "
            "       occurred_at, outcome, attempts "
            "  FROM billing_inbox WHERE event_id = %s", (event_id,))
        stored = cursor.fetchone()
    if stored is None:
        return ok({"error": f"события {event_id} нет в inbox: переигрывать нечего"}, 404)
    if stored["outcome"] == "accrued":
        # Начисленное не переигрывается никогда: это второй счёт клиенту.
        return ok({"error": f"событие {event_id} уже начислено, повтор запрещён",
                   "outcome": stored["outcome"]}, 409)

    envelope = {
        "event_id": str(stored["event_id"]),
        "type": stored["event_type"],
        "tenant_id": stored["tenant_id"],
        "correlation_id": stored["correlation_id"],
        "occurred_at": stored["occurred_at"].isoformat() if stored["occurred_at"] else None,
        "payload": stored["payload"],
    }
    outcome = billing.ingest(envelope)
    return ok({"event_id": event_id, "attempts": int(stored["attempts"]) + 1,
               "previous_outcome": stored["outcome"], "result": outcome})


@router.get("/accruals/summary")
def accruals_summary(request: Request, period: str | None = None,
                           seller: str | None = None,
                           cabinet_id: str | None = None) -> JSONResponse:
    """Итог периода одним числом — посчитанным базой.

    Портал и админка складывали начисления сами, каждая по своей странице: на
    полутора тысячах строк с `limit=1000` они показывали РАЗНЫЕ суммы, и обе
    расходились со счётом. Спор «сколько я должен» решается не тем, кто
    аккуратнее сложил, а тем, что складывают в одном месте.

    Если счёт за период выставлен, его итог приезжает рядом: это та самая
    бумага, с которой сравнивают.
    """
    with database.cursor() as cursor:
        visible = visible_cabinet_ids(cursor, caller(request))
        cursor.execute(
            """
            SELECT COALESCE(sum(a.amount), 0)         AS amount,
                   COALESCE(sum(a.partner_amount), 0) AS partner_amount,
                   COALESCE(sum(a.net_amount), 0)     AS net_amount,
                   count(*)                           AS operations
              FROM billing_accrual a
              JOIN cabinet c ON c.id = a.cabinet_id
             WHERE (%(period)s::text IS NULL OR a.period = %(period)s::text)
               AND (%(seller)s::text IS NULL OR c.seller_external_id = %(seller)s::text)
               AND (%(cabinet)s::uuid IS NULL OR a.cabinet_id = %(cabinet)s::uuid)
               AND (%(visible)s::uuid[] IS NULL OR a.cabinet_id = ANY(%(visible)s::uuid[]))
            """,
            {"period": period, "seller": seller, "cabinet": cabinet_id,
             "visible": visible})
        totals = dict(cursor.fetchone())

        cursor.execute(
            """
            SELECT i.number, i.state, i.total_amount, i.partner_total, i.net_total,
                   i.issued_at, i.paid_at
              FROM billing_invoice i
              JOIN cabinet c ON c.id = i.cabinet_id
             WHERE (%(period)s::text IS NULL OR i.period = %(period)s::text)
               AND (%(seller)s::text IS NULL OR c.seller_external_id = %(seller)s::text)
               AND (%(cabinet)s::uuid IS NULL OR i.cabinet_id = %(cabinet)s::uuid)
               AND (%(visible)s::uuid[] IS NULL OR i.cabinet_id = ANY(%(visible)s::uuid[]))
             ORDER BY i.issued_at DESC LIMIT 1
            """,
            {"period": period, "seller": seller, "cabinet": cabinet_id,
             "visible": visible})
        invoice = cursor.fetchone()

    return ok({
        "period": period,
        "seller_external_id": seller,
        "totals": {
            "amount": str(totals["amount"]),
            "partner_amount": str(totals["partner_amount"]),
            "net_amount": str(totals["net_amount"]),
            "operations": int(totals["operations"]),
        },
        "invoice": dict(invoice) if invoice else None,
    })


@router.get("/accruals")
def accruals(request: Request, cabinet_id: str | None = None, period: str | None = None,
                   limit: int = 200, cursor_after: str | None = None) -> JSONResponse:
    """Расшифровка начислений с видимой наценкой партнёра (файл 04, «ЛК клиента»).

    Клиент обязан видеть 45 и понимать, что 30 идёт MM-Express, 15 партнёру, —
    а не считать нас источником завышенной цены.

    Пагинация курсорная, а не «сколько влезло в limit». Клиент с полутора
    тысячами операций видел первую тысячу и не знал об этом: страница
    заканчивалась молча, а итог, сложенный по ней, расходился со счётом.
    Итог за период спрашивают у `/accruals/summary` — его считает база.

    `cursor_after` — значение `next_cursor` предыдущей страницы.
    """
    page = max(1, min(limit, 1000))
    after = _decode_cursor(cursor_after)
    with database.cursor() as handle:
        visible = visible_cabinet_ids(handle, caller(request))
        handle.execute(
            """
            SELECT a.*, c.seller_external_id AS cabinet_seller, p.name AS partner_name
              FROM billing_accrual a
              JOIN cabinet c ON c.id = a.cabinet_id
         LEFT JOIN partner p ON p.id = a.partner_id
             WHERE (%(cabinet)s::uuid IS NULL OR a.cabinet_id = %(cabinet)s::uuid)
               AND (%(period)s::text IS NULL OR a.period = %(period)s::text)
               AND (%(visible)s::uuid[] IS NULL OR a.cabinet_id = ANY(%(visible)s::uuid[]))
               AND (%(after_on)s::date IS NULL
                    OR (a.occurred_on, a.created_at, a.id)
                        < (%(after_on)s::date, %(after_at)s::timestamptz, %(after_id)s::uuid))
             ORDER BY a.occurred_on DESC, a.created_at DESC, a.id DESC
             LIMIT %(limit)s
            """,
            {"cabinet": cabinet_id, "period": period, "visible": visible,
             "limit": page + 1,
             "after_on": after[0] if after else None,
             "after_at": after[1] if after else None,
             "after_id": after[2] if after else None})
        rows = [dict(row) for row in handle.fetchall()]

    # Лишняя строка запрошена намеренно: по ней видно, что страница не
    # последняя. «Пришло ровно limit» об этом не говорит ничего.
    has_more = len(rows) > page
    rows = rows[:page]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last["occurred_on"], last["created_at"], last["id"])
    return ok({"accruals": rows, "count": len(rows), "next_cursor": next_cursor})


def _encode_cursor(occurred_on: Any, created_at: Any, row_id: Any) -> str:
    import base64

    raw = f"{occurred_on.isoformat()}|{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(value: str | None) -> tuple[str, str, str] | None:
    """Курсор предыдущей страницы. Мусор — как будто курсора нет.

    Отказывать на испорченном курсоре незачем: он приходит из нашего же
    ответа, и единственная причина испортиться — кто-то правил ссылку руками.
    Отдать первую страницу честнее, чем 400 на пустом месте.
    """
    if not value:
        return None
    import base64

    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        occurred_on, created_at, row_id = raw.split("|", 2)
        return occurred_on, created_at, row_id
    except Exception:  # noqa: BLE001
        return None


# -------------------------------------------------------------------- отчёты

@router.get("/reports/margin")
def margin(period: str, request: Request) -> JSONResponse:
    """выручка − наценка партнёра − себестоимость = маржа по кабинету.

    Себестоимость — внутреннее число MM-Express, поэтому отчёт целиком виден
    только администратору и бухгалтеру. Партнёру видна его комиссия, но не то,
    сколько мы на его клиенте заработали.
    """
    require_write(caller(request))
    return ok({"period": period, "cabinets": billing.margin(period)})


@router.get("/reports/unbilled")
def unbilled(request: Request) -> JSONResponse:
    """Что склад сделал, а клиенту не выставлено, — по причинам."""
    require_write(caller(request))
    return ok({"reasons": billing.unbilled()})


@router.get("/reports/shift")
def shift(request: Request, day: str | None = None) -> JSONResponse:
    """Выработка смены: кто сколько сделал (витрина начальника склада).

    Кто сколько сделал — это про людей, а не про деньги клиента. Смотрит
    начальник склада или администратор, а не любой представившийся.
    """
    who = caller(request)
    if not (who.unrestricted or "warehouse_head" in who.roles):
        raise Forbidden("выработку смены видит начальник склада или администратор")
    return ok({"day": as_date(day, today()).isoformat(),
               "rows": billing.shift_output(as_date(day, today()))})


# ------------------------------------------------------ периоды, счета, расходы

@router.post("/periods/{period}/close")
async def close_period(period: str, request: Request) -> JSONResponse:
    require_write(caller(request))
    body = await body_of(request)
    closed_by = str(body.get("closed_by") or "").strip()
    if not closed_by:
        return ok({"error": "закрытие периода требует имени: закрытый период не пересчитывается"},
                  400)
    return ok({"period": admin.close_period(period, closed_by)})


@router.post("/periods/{period}/allocate")
def allocate(period: str, request: Request) -> JSONResponse:
    require_write(caller(request))
    return ok(admin.allocate_period(period))


@router.post("/expenses")
async def add_expense(request: Request) -> JSONResponse:
    require_write(caller(request))
    body = await body_of(request)
    try:
        expense = admin.add_expense(
            str(body["period"]), str(body["category"]), as_money(body.get("amount", 0)),
            str(body.get("comment") or ""),
            allocation_rule=str(body.get("allocation_rule") or "by_operations"),
            cabinet_id=body.get("cabinet_id"))
    except (KeyError, OnboardingError, ValueError) as error:
        return ok({"error": str(error)}, 400)
    return ok({"expense": expense}, 201)


def _conflict_or_raise(error: Exception) -> JSONResponse | None:
    """Конфликт схемы — это 409, а не 500.

    `ExclusionViolation` на закреплении означает «этот кабинет уже закреплён
    на эти даты», `UniqueViolation` — «такая запись уже есть». Оба — ответ
    человеку, а не сбой сервиса: 500 отправляет его чинить то, что не
    сломано.
    """
    import psycopg

    if isinstance(error, psycopg.errors.ExclusionViolation | psycopg.errors.UniqueViolation):
        return ok({"error": _conflict_text(error)}, 409)
    return None


def _conflict_text(error: Exception) -> str:
    detail = getattr(getattr(error, "diag", None), "constraint_name", None)
    if detail:
        return f"запись конфликтует с уже существующей ({detail})"
    return "запись конфликтует с уже существующей"


@router.post("/invoices")
async def issue_invoice(request: Request) -> JSONResponse:
    require_write(caller(request))
    body = await body_of(request)
    try:
        invoice = admin.issue_invoice(str(body["cabinet_id"]), str(body["period"]),
                                      str(body.get("number") or
                                          f"{body['period']}-{str(body['cabinet_id'])[:8]}"))
    except InvoiceConflict as error:
        # 409, а не 400: данные верные, а состояние счёта не то.
        return ok({"error": str(error)}, 409)
    except (KeyError, OnboardingError) as error:
        return ok({"error": str(error)}, 400)
    return ok({"invoice": invoice}, 201)


@router.get("/invoices/{invoice_id}")
def invoice(invoice_id: str, request: Request) -> JSONResponse:
    """Акт за период: клиент выгружает сам, без участия бухгалтера.

    Свой — да. Чужой — нет: в акте видно, сколько платит другой клиент и какая
    у него наценка. Видимость считается деревом закреплений, а не ролью.
    """
    who = caller(request)
    try:
        found = admin.invoice(invoice_id)
    except OnboardingError as error:
        return ok({"error": str(error)}, 404)

    if not who.unrestricted:
        cabinet_id = str((found.get("invoice") or found).get("cabinet_id") or "")
        with database.cursor() as cursor:
            allowed = visible_cabinet_ids(cursor, who)
        if allowed is not None and cabinet_id not in {str(item) for item in allowed}:
            # 404, а не 403: существование чужого счёта — тоже сведение.
            return ok({"error": "счёт не найден"}, 404)
    return ok(found)


@router.post("/invoices/{invoice_id}/pay")
def pay_invoice(invoice_id: str, request: Request) -> JSONResponse:
    """Оплата счёта переводит вознаграждение партнёра в payable, не раньше."""
    require_write(caller(request))
    try:
        return ok({"invoice": admin.pay_invoice(invoice_id)})
    except InvoiceConflict as error:
        return ok({"error": str(error)}, 409)
    except OnboardingError as error:
        return ok({"error": str(error)}, 404)


# --------------------------------------------------------------- приложение

def create_app() -> FastAPI:
    environment = app_environment()
    application = FastAPI(title="MM-Express billing", version="1.0.0")
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts(environment))

    @application.middleware("http")
    async def observe(request: Request, call_next: Any) -> Any:
        started = time.monotonic()
        response = await call_next(request)
        metrics.observe_http(response.status_code, time.monotonic() - started)
        return response

    @application.exception_handler(Unauthorized)
    async def unauthorized(_: Request, error: Unauthorized) -> JSONResponse:
        return ok({"error": str(error)}, 401)

    @application.exception_handler(Forbidden)
    async def forbidden(_: Request, error: Forbidden) -> JSONResponse:
        # 403, а не пустая выдача: человек должен понять, что видит не всё.
        return ok({"error": str(error)}, 403)

    @application.get("/whoami")
    async def whoami(request: Request) -> JSONResponse:
        return ok(summary(caller(request)))

    @application.get("/healthz")
    async def healthz() -> JSONResponse:
        # Процесс жив. О базе здесь не спрашиваем: healthz, зависящий от базы,
        # перезапускает контейнер вместо того, чтобы чинить базу.
        return ok({"status": "ok"})

    @application.get("/readyz")
    async def readyz() -> JSONResponse:
        ready = database.ready()
        return ok({"status": "ready" if ready else "degraded", "database": ready},
                  200 if ready else 503)

    @application.get("/metrics")
    async def prometheus() -> PlainTextResponse:
        payload, content_type = metrics.prometheus_payload(database)
        return PlainTextResponse(payload, media_type=content_type)

    @application.get("/services")
    async def services() -> JSONResponse:
        return ok({"services": [item.value for item in Service]})

    application.include_router(router)
    return application


app = create_app()
