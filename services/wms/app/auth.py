"""Кто спрашивает. Общий модуль, скопированный в каждый сервис.

Копия, а не общий пакет, — потому что общего пакета у потоков нет (правило
9.5.1: один агент — один каталог), и заводить его ради двухсот строк дороже,
чем держать их одинаковыми. Файл меняется в одном месте и копируется целиком;
расхождение ловит тест `test_auth_module_is_identical`.

Три способа представиться, в порядке доверия:

  1. **JWT identity** — подпись проверяется по JWKS, ключи кэшируются на пять
     минут. Это человек.
  2. **Сервисный токен** — общий секрет для межсервисных вызовов, сравнение
     через `hmac.compare_digest`. Это не человек, и прав человека у него нет.
  3. **Ничего** — 401.

Fail-closed: вне `test`/`local`/`development` отсутствие `IDENTITY_JWKS_URL` —
ошибка старта, а не «работаем без проверки». Сервис, который молча перестал
проверять токены, выглядит совершенно здоровым.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

LOCAL_ENVIRONMENTS = frozenset({"test", "local", "development"})
ISSUER = "mmx-identity"
ALGORITHM = "EdDSA"
CLOCK_SKEW_SECONDS = 60

# Ключи перечитываются раз в пять минут. Чаще — лишний поход к identity на
# каждый запрос; реже — отозванный ключ живёт слишком долго.
JWKS_TTL_SECONDS = 300


class AuthError(Exception):
    """Не представился или представился неубедительно."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class Caller:
    """Кто пришёл. `service` — вызов не от человека, а от соседнего сервиса."""

    subject: str
    roles: tuple[str, ...] = ()
    scopes: tuple[dict[str, Any], ...] = ()
    sellers: tuple[str, ...] = ()
    partner_branches: tuple[str, ...] = ()
    service: str | None = None

    def has(self, *codes: str) -> bool:
        """Есть ли хотя бы одна из ролей — в любой области."""
        return any(code in self.roles for code in codes)

    def has_global(self, *codes: str) -> bool:
        """Есть ли роль ГЛОБАЛЬНО. Областная выдача такого права не даёт."""
        return any(scope.get("role") in codes and scope.get("kind") == "global"
                   for scope in self.scopes)


def app_environment() -> str:
    return (os.getenv("APP_ENV") or "").strip().lower()


def _local() -> bool:
    return app_environment() in LOCAL_ENVIRONMENTS


# ------------------------------------------------------------------- ключи


@dataclass
class _KeyCache:
    keys: dict[str, Any] = field(default_factory=dict)
    fetched_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


_cache = _KeyCache()


def jwks_url() -> str:
    url = (os.getenv("IDENTITY_JWKS_URL") or "").strip()
    if url:
        return url
    identity = (os.getenv("IDENTITY_URL") or "").strip()
    if identity:
        return f"{identity.rstrip('/')}/.well-known/jwks.json"
    if not _local():
        # Fail-closed. Без адреса ключей проверять подпись нечем, и «пустим
        # всех» здесь было бы ровно тем, чего требует не делать раздел 12.
        raise AuthError("IDENTITY_JWKS_URL не задан: проверять подпись нечем", 500)
    return ""


def _public_keys() -> dict[str, Any]:
    now = time.monotonic()
    with _cache.lock:
        if _cache.keys and now - _cache.fetched_at < JWKS_TTL_SECONDS:
            return _cache.keys
    url = jwks_url()
    if not url:
        return {}
    try:
        document = httpx.get(url, timeout=5.0).json()
    except Exception as failure:  # noqa: BLE001
        # Старые ключи лучше, чем никакие: identity мог просто моргнуть, а
        # выбить всю смену из системы из-за этого нельзя.
        with _cache.lock:
            if _cache.keys:
                return _cache.keys
        raise AuthError(f"ключи identity недоступны: {failure}", 503) from failure

    keys = _keys_from_jwks(document)
    with _cache.lock:
        _cache.keys, _cache.fetched_at = keys, now
    return keys


def _keys_from_jwks(document: dict[str, Any]) -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    keys: dict[str, Any] = {}
    for entry in document.get("keys") or []:
        if entry.get("kty") != "OKP" or entry.get("crv") != "Ed25519":
            continue
        kid, raw = str(entry.get("kid") or ""), entry.get("x")
        if not kid or not raw:
            continue
        try:
            keys[kid] = Ed25519PublicKey.from_public_bytes(_unb64url(str(raw)))
        except Exception:  # noqa: BLE001 — кривой ключ не роняет остальные
            continue
    return keys


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# ---------------------------------------------------------------- проверка


def _decode(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("токен не в форме JWT")
    raw_header, raw_payload, raw_signature = parts
    try:
        header = json.loads(_unb64url(raw_header))
    except Exception as failure:  # noqa: BLE001
        raise AuthError("заголовок токена не разбирается") from failure

    if header.get("alg") != ALGORITHM:
        raise AuthError(f"алгоритм {header.get('alg')!r} не поддерживается")

    public = _public_keys().get(str(header.get("kid") or ""))
    if public is None:
        raise AuthError("ключ, которым подписан токен, неизвестен")
    try:
        public.verify(_unb64url(raw_signature), f"{raw_header}.{raw_payload}".encode())
    except Exception as failure:  # noqa: BLE001
        raise AuthError("подпись не сходится") from failure

    payload = json.loads(_unb64url(raw_payload))
    now = int(time.time())
    if payload.get("iss") != ISSUER:
        raise AuthError("чужой издатель токена")
    if int(payload.get("exp", 0)) + CLOCK_SKEW_SECONDS < now:
        raise AuthError("срок действия токена истёк")
    if int(payload.get("nbf", 0)) - CLOCK_SKEW_SECONDS > now:
        raise AuthError("токен ещё не действует")
    if not str(payload.get("sub") or "").strip():
        raise AuthError("в токене нет субъекта")
    return payload


def _introspect(token: str) -> dict[str, Any] | None:
    """Спросить identity. Нужно там, где решение дороже подписи: роли могли
    отозвать, а токен живёт до конца смены."""
    identity = (os.getenv("IDENTITY_URL") or "").strip()
    if not identity:
        return None
    try:
        response = httpx.post(f"{identity.rstrip('/')}/api/identity/v1/introspect",
                              json={"token": token}, timeout=5.0)
    except Exception:  # noqa: BLE001
        return None
    if response.status_code >= 400:
        return None
    body = response.json()
    return body if body.get("active") else None


def bearer(headers: Any) -> str:
    raw = ""
    try:
        raw = headers.get("authorization") or headers.get("Authorization") or ""
    except AttributeError:
        raw = ""
    if not raw.lower().startswith("bearer "):
        return ""
    return raw[7:].strip()


def service_token_matches(token: str) -> bool:
    """Сервисный токен. Сравнение постоянного времени: обычное `==` на секретах
    сравнивает до первого несовпадения и рассказывает длину общего префикса."""
    expected = (os.getenv("SERVICE_TOKEN") or "").strip()
    if not expected or not token:
        return False
    return hmac.compare_digest(expected, token)


def caller_from(headers: Any, *, allow_service: bool = False) -> Caller:
    """Кто пришёл. Бросает `AuthError`, если никто.

    `allow_service=True` — маршрут, который зовут соседние сервисы (консьюмер
    событий, например). У сервисного вызова нет ролей человека, и раздать их
    ему нельзя: он не человек и подписывать за человека не может.
    """
    token = bearer(headers)
    if not token:
        raise AuthError("нужен Bearer-токен identity")

    if allow_service and service_token_matches(token):
        return Caller(subject="service", service=os.getenv("SERVICE_NAME") or "service")

    payload = _decode(token)
    fresh = _introspect(token)
    if fresh is None:
        # identity не ответил — верим подписи. Она уже проверена, а отказ из-за
        # недоступности identity выбивает смену на ровном месте.
        roles = tuple(str(role) for role in (payload.get("roles") or []))
        return Caller(subject=str(payload["sub"]), roles=roles)

    return Caller(
        subject=str(fresh.get("user_id") or payload["sub"]),
        roles=tuple(str(role) for role in (fresh.get("roles") or [])),
        scopes=tuple(fresh.get("scopes") or []),
        sellers=tuple(str(item) for item in (fresh.get("sellers") or [])),
        partner_branches=tuple(str(item) for item in (fresh.get("partner_branches") or [])),
    )


def require(headers: Any, *roles: str, allow_service: bool = False) -> Caller:
    """Кто пришёл и можно ли ему. Без ролей — просто «представился»."""
    who = caller_from(headers, allow_service=allow_service)
    if who.service is not None:
        return who
    if roles and not who.has(*roles):
        raise AuthError(f"нужна одна из ролей: {', '.join(roles)}", 403)
    return who


def assert_configured() -> None:
    """Позвать при старте. Вне локальных сред без адреса ключей — падаем."""
    if _local():
        return
    if not (os.getenv("IDENTITY_JWKS_URL") or os.getenv("IDENTITY_URL")):
        raise RuntimeError(
            "не задан ни IDENTITY_JWKS_URL, ни IDENTITY_URL: проверять подпись "
            "токенов нечем. Сервис без проверки выглядит здоровым, и это хуже, "
            "чем не подняться (раздел 12).")
