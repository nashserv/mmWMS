"""Подпись и проверка JWT. Раздел 12: `wms` доверяет JWT по JWKS.

Своя реализация, а не библиотека, по двум причинам. Первая — раздел 2.2:
новых зависимостей не вводим, а `cryptography` в наборе платформы уже есть.
Вторая — объём: нужно ровно подписать, проверить и отдать JWKS, и всё это
здесь помещается в двести строк, которые можно прочитать целиком.

Алгоритм — EdDSA на Ed25519. У него нет параметров, которые можно выбрать
неправильно: ни длины ключа, ни схемы дополнения, ни кривой с сюрпризами.
`alg: none` не поддерживается вовсе — не «отклоняется проверкой», а просто
не существует в коде.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (Ed25519PrivateKey,
                                                               Ed25519PublicKey)

ISSUER = "mmx-identity"
ALGORITHM = "EdDSA"

# Сколько живёт токен. Смена — двенадцать часов (раздел 4), и токен обязан
# пережить её целиком: перелогиниться у стойки посреди подбора значит бросить
# лист и уйти к администратору.
DEFAULT_TTL_SECONDS = 13 * 3600

# Запас на расхождение часов между сервисами. Без него токен, выданный секунду
# назад, может оказаться «из будущего» для соседа.
CLOCK_SKEW_SECONDS = 60


class TokenError(ValueError):
    """Токен не принят. Текст говорит почему — но наружу он не уходит."""


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# --------------------------------------------------------------------- ключи


@dataclass(frozen=True)
class SigningKey:
    kid: str
    private_pem: str
    public_pem: str

    @property
    def private(self) -> Ed25519PrivateKey:
        key = serialization.load_pem_private_key(self.private_pem.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise TokenError("ключ подписи не Ed25519")
        return key

    @property
    def public(self) -> Ed25519PublicKey:
        key = serialization.load_pem_public_key(self.public_pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise TokenError("открытый ключ не Ed25519")
        return key

    def jwk(self) -> dict[str, Any]:
        """Публичная часть в форме JWKS. Приватной здесь нет и быть не может."""
        raw = self.public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)
        return {"kty": "OKP", "crv": "Ed25519", "use": "sig",
                "alg": ALGORITHM, "kid": self.kid, "x": b64url(raw)}


def generate_key() -> SigningKey:
    """Новая пара. `kid` — отпечаток открытой части, а не случайное число:
    так по токену видно, каким ключом он подписан, даже если журнал потерян."""
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    public_pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    raw = public.public_bytes(encoding=serialization.Encoding.Raw,
                             format=serialization.PublicFormat.Raw)
    return SigningKey(kid=hashlib.sha256(raw).hexdigest()[:16],
                      private_pem=private_pem, public_pem=public_pem)


def key_from_pem(private_pem: str) -> SigningKey:
    """Ключ из PEM — так он приходит из секрет-провайдера в защищённых средах."""
    private = serialization.load_pem_private_key(private_pem.encode(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise TokenError("IDENTITY_SIGNING_KEY должен быть ключом Ed25519 в PEM")
    public = private.public_key()
    public_pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    raw = public.public_bytes(encoding=serialization.Encoding.Raw,
                             format=serialization.PublicFormat.Raw)
    return SigningKey(kid=hashlib.sha256(raw).hexdigest()[:16],
                      private_pem=private_pem, public_pem=public_pem)


# -------------------------------------------------------------------- выпуск


def issue(key: SigningKey, *, subject: str, roles: list[dict[str, Any]] | None = None,
          ttl_seconds: int = DEFAULT_TTL_SECONDS,
          audience: str | None = None) -> str:
    """Подписать токен. Роли кладутся в него, но доверять им нельзя вслепую:
    сервис всё равно спрашивает identity, если решение дорогое."""
    now = int(time.time())
    header = {"alg": ALGORITHM, "typ": "JWT", "kid": key.kid}
    payload: dict[str, Any] = {
        "iss": ISSUER, "sub": subject, "iat": now, "nbf": now,
        "exp": now + int(ttl_seconds), "jti": str(uuid.uuid4()),
        "roles": roles or [],
    }
    if audience:
        payload["aud"] = audience
    signing_input = f"{b64url(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    signature = key.private.sign(signing_input.encode())
    return f"{signing_input}.{b64url(signature)}"


# ------------------------------------------------------------------ проверка


def decode(token: str, keys: dict[str, Ed25519PublicKey], *,
           issuer: str = ISSUER, audience: str | None = None) -> dict[str, Any]:
    """Разобрать и проверить токен. Возвращает payload или бросает TokenError.

    Порядок важен: сначала подпись, потом всё остальное. Читать полезную
    нагрузку неподписанного токена — значит принимать решения по тому, что
    прислал кто угодно.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("токен не в форме JWT")
    raw_header, raw_payload, raw_signature = parts

    try:
        header = json.loads(unb64url(raw_header))
    except Exception as failure:  # noqa: BLE001
        raise TokenError("заголовок токена не разбирается") from failure

    if header.get("alg") != ALGORITHM:
        # `alg: none` и подмена на HMAC — классика. Алгоритм здесь ровно один.
        raise TokenError(f"алгоритм {header.get('alg')!r} не поддерживается")

    kid = str(header.get("kid") or "")
    public = keys.get(kid)
    if public is None:
        raise TokenError("ключ, которым подписан токен, неизвестен")

    try:
        public.verify(unb64url(raw_signature), f"{raw_header}.{raw_payload}".encode())
    except Exception as failure:  # noqa: BLE001
        raise TokenError("подпись не сходится") from failure

    try:
        payload = json.loads(unb64url(raw_payload))
    except Exception as failure:  # noqa: BLE001
        raise TokenError("полезная нагрузка не разбирается") from failure

    now = int(time.time())
    if payload.get("iss") != issuer:
        raise TokenError(f"чужой издатель: {payload.get('iss')!r}")
    if int(payload.get("exp", 0)) + CLOCK_SKEW_SECONDS < now:
        raise TokenError("срок действия истёк")
    if int(payload.get("nbf", 0)) - CLOCK_SKEW_SECONDS > now:
        raise TokenError("токен ещё не действует")
    if audience and payload.get("aud") != audience:
        raise TokenError("токен выдан для другого получателя")
    if not str(payload.get("sub") or "").strip():
        raise TokenError("в токене нет субъекта")
    return payload


def keys_from_jwks(document: dict[str, Any]) -> dict[str, Ed25519PublicKey]:
    """Открытые ключи из JWKS, по `kid`."""
    keys: dict[str, Ed25519PublicKey] = {}
    for entry in document.get("keys") or []:
        if entry.get("kty") != "OKP" or entry.get("crv") != "Ed25519":
            continue
        kid = str(entry.get("kid") or "")
        raw = entry.get("x")
        if not kid or not raw:
            continue
        try:
            keys[kid] = Ed25519PublicKey.from_public_bytes(unb64url(str(raw)))
        except Exception:  # noqa: BLE001 — кривой ключ в JWKS не роняет проверку
            continue
    return keys
