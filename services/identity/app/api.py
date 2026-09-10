"""HTTP роле́вой части сервиса identity.

**Границы этого сервиса.** Поток C владеет ролями и не трогает механику
JWT/JWKS (файл 04). Боевой identity живёт на платформе, его исходников в этом
репозитории нет. Поэтому здесь:

* есть справочник ролей, выдача и отзыв с областью действия;
* нет ни одной строки про подпись токена, ключи и их ротацию;
* `introspect` либо делегирует боевому identity (IDENTITY_UPSTREAM_URL), либо
  на стенде принимает непрозрачный токен вида `stand:<uuid>` — и **отказывает
  вне локальных сред**. Заглушка, молча доехавшая до прода, — это открытая
  дверь, поэтому она закрыта проверкой окружения, а не памятью.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import (LOCAL_ENVIRONMENTS, app_environment, database_url, trusted_hosts,
                     upstream_url)
from .db import Database
from .metrics import (HTTP_DURATION, HTTP_REQUESTS, INTROSPECT_REFUSED, SERVICE, SERVICE_READY)
from .roles import RoleDirectory

BASE_PATH = "/api/identity/v1"
STAND_TOKEN_PREFIX = "stand:"

database = Database(database_url() or "postgresql:///identity")
directory = RoleDirectory(database)
router = APIRouter(prefix=BASE_PATH)


def ok(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(jsonable(payload), status_code=status_code)


def jsonable(value: Any) -> Any:
    import datetime
    import uuid as uuid_module

    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, uuid_module.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


async def body_of(request: Request) -> dict[str, Any]:
    try:
        parsed = await request.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@router.get("/roles")
async def roles() -> JSONResponse:
    return ok({"roles": directory.roles()})


@router.post("/grants")
async def grant(request: Request) -> JSONResponse:
    body = await body_of(request)
    try:
        created = directory.grant(
            str(body["user_id"]), str(body["role_code"]), str(body.get("granted_by") or ""),
            scope_kind=str(body.get("scope_kind") or "global"), scope_id=body.get("scope_id"))
    except KeyError as error:
        return ok({"error": f"не хватает поля {error}"}, 400)
    except ValueError as error:
        return ok({"error": str(error)}, 400)
    return ok({"grant": created}, 201)


@router.post("/grants/{grant_id}/revoke")
async def revoke(grant_id: str, request: Request) -> JSONResponse:
    body = await body_of(request)
    revoked_by = str(body.get("revoked_by") or "").strip()
    if not revoked_by:
        return ok({"error": "отзыв роли без автора неразбираем при разборе инцидента"}, 400)
    result = directory.revoke(grant_id, revoked_by)
    return ok({"grant": result}) if result else ok({"error": "выдача не найдена"}, 404)


@router.get("/users/{user_id}/roles")
async def user_roles(user_id: str) -> JSONResponse:
    return ok(directory.principal(user_id))


@router.post("/introspect")
async def introspect(request: Request) -> JSONResponse:
    """Кто предъявил этот токен.

    Проверку подписи выполняет боевой identity — он владеет ключами. Здесь
    либо делегирование ему, либо стендовый непрозрачный токен, и никакого
    третьего пути: разбирать JWT самостоятельно значит завести вторую
    реализацию проверки подписи, которая однажды разойдётся с первой.
    """
    body = await body_of(request)
    token = str(body.get("token") or "").strip()
    if not token:
        INTROSPECT_REFUSED.labels(reason="no_token").inc()
        return ok({"active": False, "reason": "токен не предъявлен"}, 401)

    upstream = upstream_url()
    if upstream:
        try:
            response = httpx.post(f"{upstream.rstrip('/')}{BASE_PATH}/introspect",
                                  json={"token": token}, timeout=5.0)
        except httpx.HTTPError as failure:
            INTROSPECT_REFUSED.labels(reason="upstream_down").inc()
            return ok({"active": False, "reason": f"боевой identity недоступен: {failure}"}, 503)
        if response.status_code >= 400:
            INTROSPECT_REFUSED.labels(reason="upstream_refused").inc()
            return ok({"active": False, "reason": "боевой identity отказал"}, 401)
        upstream_body = response.json()
        user_id = str(upstream_body.get("user_id") or "")
        # Роли берём свои: четыре складские заведены здесь, боевой identity о
        # них ещё не знает (файл 04, «Identity получает роли …»).
        principal = directory.principal(user_id) if user_id else {}
        return ok({"active": True, **upstream_body, **principal})

    if app_environment() not in LOCAL_ENVIRONMENTS:
        # Стендовая заглушка вне стенда — это вход без пароля.
        INTROSPECT_REFUSED.labels(reason="stand_token_outside_stand").inc()
        return ok({"active": False,
                   "reason": "непрозрачные стендовые токены разрешены только в локальных "
                             "средах; задайте IDENTITY_UPSTREAM_URL"}, 401)

    if not token.startswith(STAND_TOKEN_PREFIX):
        INTROSPECT_REFUSED.labels(reason="not_a_stand_token").inc()
        return ok({"active": False, "reason": "на стенде ожидается токен вида stand:<uuid>"}, 401)

    user_id = token[len(STAND_TOKEN_PREFIX):]
    principal = directory.principal(user_id)
    if not principal["roles"]:
        INTROSPECT_REFUSED.labels(reason="no_roles").inc()
        return ok({"active": False, "reason": f"у {user_id} нет ни одной действующей роли"}, 403)
    return ok({"active": True, "stand": True, **principal})


def create_app() -> FastAPI:
    environment = app_environment()
    application = FastAPI(title="MM-Express identity — роли", version="1.0.0")
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts(environment))

    @application.middleware("http")
    async def observe(request: Request, call_next: Any) -> Any:
        started = time.monotonic()
        response = await call_next(request)
        HTTP_REQUESTS.labels(service=SERVICE, status=str(response.status_code)).inc()
        HTTP_DURATION.labels(service=SERVICE).observe(max(0.0, time.monotonic() - started))
        return response

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
        SERVICE_READY.labels(service=SERVICE).set(1 if database.ready() else 0)
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    application.include_router(router)
    return application


app = create_app()
