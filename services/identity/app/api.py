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
from .keys import load_signing_key, public_keys
from .metrics import (HTTP_DURATION, HTTP_REQUESTS, INTROSPECT_REFUSED, SERVICE, SERVICE_READY)
from .roles import RoleDirectory
from . import tokens as jwt

BASE_PATH = "/api/identity/v1"
STAND_TOKEN_PREFIX = "stand:"

database = Database(database_url() or "postgresql:///identity")
directory = RoleDirectory(database)
router = APIRouter(prefix=BASE_PATH)

# Ключ подписи берётся лениво: при импорте модуля базы может ещё не быть.
_signing: dict[str, Any] = {}


def signing_key() -> Any:
    if "key" not in _signing:
        _signing["key"] = load_signing_key(database)
    return _signing["key"]


def verifying_keys() -> dict[str, Any]:
    """Чем проверять предъявленный токен: действующий ключ и недавно отозванные."""
    return {key.kid: key.public for key in public_keys(database, signing_key())}


def _uuid_or_none(value: str) -> str | None:
    """Идентификатор пользователя обязан быть uuid: `identity` — единственный
    их источник (раздел 12). Кривой идентификатор это 400, а не 500."""
    import uuid as uuid_module

    try:
        return str(uuid_module.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


# ------------------------------------------------------------ кто спрашивает


def caller(request: Request) -> dict[str, Any] | None:
    """Разобрать предъявленный токен. None — не предъявлен или не принят.

    Роли берутся из БАЗЫ по субъекту, а не из полезной нагрузки токена: роль
    могли отозвать минуту назад, а токен живёт до конца смены. Токен отвечает
    на вопрос «кто это», справочник — на вопрос «что ему сейчас можно».
    """
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    if not token:
        return None

    if token.startswith(STAND_TOKEN_PREFIX):
        # Непрозрачный стендовый токен — только в локальных средах.
        if app_environment() not in LOCAL_ENVIRONMENTS:
            return None
        subject = token[len(STAND_TOKEN_PREFIX):]
        if _uuid_or_none(subject) is None:
            return None
        return directory.principal(subject)

    try:
        payload = jwt.decode(token, verifying_keys())
    except jwt.TokenError:
        return None
    return directory.principal(str(payload["sub"]))


def has_role(principal: dict[str, Any] | None, code: str) -> bool:
    """Есть ли у человека эта роль ГЛОБАЛЬНО.

    Смотрим в `scopes`, а не в `roles`: первое несёт область выдачи, второе —
    только имена. Областные выдачи сюда не считаются намеренно: администратор
    ролей это право на весь справочник, и «admin в пределах одного кабинета»
    такого права не даёт.
    """
    if not principal:
        return False
    return any(scope.get("role") == code and scope.get("kind") == "global"
               for scope in principal.get("scopes") or [])


def require_admin(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Кто спрашивает и можно ли ему. Возвращает (principal, отказ)."""
    who = caller(request)
    if who is None:
        return None, ok({"error": "нужен Bearer-токен identity"}, 401)
    if not has_role(who, "admin"):
        return who, ok({"error": "нужна глобальная роль admin: выдача и отзыв ролей "
                                 "меняют права на складе"}, 403)
    return who, None


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
def roles() -> JSONResponse:
    return ok({"roles": directory.roles()})


@router.post("/grants")
async def grant(request: Request) -> JSONResponse:
    who, refusal = require_admin(request)
    if refusal is not None:
        return refusal

    body = await body_of(request)
    if "user_id" in body and _uuid_or_none(str(body["user_id"])) is None:
        return ok({"error": "user_id должен быть uuid: identity — единственный "
                            "источник идентификаторов пользователей (раздел 12)"}, 400)
    try:
        created = directory.grant(
            str(body["user_id"]), str(body["role_code"]),
            # Автор выдачи берётся ИЗ ТОКЕНА, а не из тела. Поле в теле —
            # это подпись за того, кого назовут: при разборе инцидента она
            # ничего не стоит.
            str(who.get("user_id") or "") if who else "",
            scope_kind=str(body.get("scope_kind") or "global"), scope_id=body.get("scope_id"))
    except KeyError as error:
        return ok({"error": f"не хватает поля {error}"}, 400)
    except ValueError as error:
        return ok({"error": str(error)}, 400)
    return ok({"grant": created}, 201)


@router.post("/grants/{grant_id}/revoke")
def revoke(grant_id: str, request: Request) -> JSONResponse:
    who, refusal = require_admin(request)
    if refusal is not None:
        return refusal
    if _uuid_or_none(grant_id) is None:
        return ok({"error": "идентификатор выдачи должен быть uuid"}, 400)

    # Автор отзыва — из токена. Отзыв роли без разбираемого автора бесполезен
    # ровно в тот момент, когда разбирают инцидент.
    revoked_by = str((who or {}).get("user_id") or "").strip()
    result = directory.revoke(grant_id, revoked_by)
    return ok({"grant": result}) if result else ok({"error": "выдача не найдена"}, 404)


@router.get("/users/{user_id}/roles")
def user_roles(user_id: str, request: Request) -> JSONResponse:
    """Чьи роли смотрим. Свои — можно всегда, чужие — только администратору."""
    if _uuid_or_none(user_id) is None:
        return ok({"error": "user_id должен быть uuid"}, 400)
    who = caller(request)
    if who is None:
        return ok({"error": "нужен Bearer-токен identity"}, 401)
    if str(who.get("user_id") or "") != user_id and not has_role(who, "admin"):
        return ok({"error": "чужие роли видит только admin"}, 403)
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
        if not principal.get("roles"):
            # Боевой identity подтвердил, кто это, но складских ролей у него
            # нет — значит на складе ему нельзя ничего. `active: true` без
            # ролей читается вызывающим как «пускать», и это открытая дверь.
            INTROSPECT_REFUSED.labels(reason="no_roles").inc()
            return ok({"active": False,
                       "reason": f"у {user_id or 'предъявителя'} нет складских ролей"}, 403)
        return ok({"active": True, **upstream_body, **principal})

    # Подписанный нами токен проверяется здесь же: ключ наш, JWKS наш.
    if not token.startswith(STAND_TOKEN_PREFIX):
        try:
            payload = jwt.decode(token, verifying_keys())
        except jwt.TokenError as failure:
            INTROSPECT_REFUSED.labels(reason="bad_signature").inc()
            return ok({"active": False, "reason": str(failure)}, 401)
        principal = directory.principal(str(payload["sub"]))
        if not principal["roles"]:
            # Токен подлинный, но человек больше ничего не может: роль отозвали.
            INTROSPECT_REFUSED.labels(reason="no_roles").inc()
            return ok({"active": False,
                       "reason": f"у {payload['sub']} нет ни одной действующей роли"}, 403)
        return ok({"active": True, "token_type": "jwt", **principal})

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


@router.post("/tokens")
async def issue_token(request: Request) -> JSONResponse:
    """Выдать подписанный токен.

    На стенде — единственный способ получить рабочий токен: боевого identity
    рядом нет, а `stand:<uuid>` больше не годится нигде, кроме локальных сред.

    Вне локальных сред выпуск закрыт совсем. Не «требует роли» — закрыт:
    сервис, умеющий выдать токен любому пользователю, в бою и есть обход
    аутентификации, сколько ролей ни навешивай. Там токены выдаёт боевой
    identity, у которого есть пароли и вторые факторы.
    """
    environment = app_environment()
    if environment not in LOCAL_ENVIRONMENTS or upstream_url():
        return ok({"error": "выпуск токенов здесь доступен только на стенде; "
                            "в бою их выдаёт identity платформы"}, 404)

    body = await body_of(request)
    user_id = _uuid_or_none(str(body.get("user_id") or ""))
    if user_id is None:
        return ok({"error": "user_id должен быть uuid"}, 400)

    principal = directory.principal(user_id)
    if not principal["roles"]:
        return ok({"error": f"у {user_id} нет ни одной действующей роли — "
                            f"токен без прав не нужен никому"}, 403)

    ttl = int(body.get("ttl_seconds") or jwt.DEFAULT_TTL_SECONDS)
    token = jwt.issue(signing_key(), subject=user_id,
                      roles=principal["roles"], ttl_seconds=ttl)
    return ok({"access_token": token, "token_type": "Bearer",
               "expires_in": ttl, "user_id": user_id})


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

    @application.get("/.well-known/jwks.json")
    async def jwks() -> JSONResponse:
        """Открытые ключи. По ним `wms` и остальные проверяют подпись сами,
        не спрашивая identity на каждый запрос (раздел 12).

        Приватной части здесь нет и быть не может: отдаётся только `x`,
        открытая точка кривой.
        """
        try:
            keys = [key.jwk() for key in public_keys(database, signing_key())]
        except Exception as failure:  # noqa: BLE001
            return ok({"error": f"ключи недоступны: {failure}"}, 503)
        return ok({"keys": keys})

    @application.get("/metrics")
    async def metrics() -> PlainTextResponse:
        SERVICE_READY.labels(service=SERVICE).set(1 if database.ready() else 0)
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    application.include_router(router)
    return application


app = create_app()
