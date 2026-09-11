"""Кто спрашивает и что ему видно.

«Менеджер видит только своих клиентов и свою комиссию, старший — свою ветку
целиком» (файл 04, «Роли и права»). Правило живёт здесь одним куском, а не
фильтром в каждом обработчике: забытый фильтр в одном месте показывает
менеджеру чужие деньги, и заметить это некому.

Токены биллинг не разбирает. Кто предъявил токен, отвечает `identity` — он
владеет ключами (раздел 2.2, JWKS, issuer mmx-identity). Поток C механику
JWT не трогает (файл 04), поэтому здесь только вызов `introspect`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable

import threading
import time

import httpx

from .config import LOCAL_ENVIRONMENTS, app_environment

# Роли, которым видно всё: администратор ведёт справочники, бухгалтер — счета.
UNRESTRICTED = frozenset({"admin", "accountant"})
# Роли, которым можно менять справочники и деньги.
WRITERS = frozenset({"admin", "accountant"})


class Unauthorized(RuntimeError):
    """Кто спрашивает — неизвестно."""


class Forbidden(RuntimeError):
    """Известно кто, и ему этого не видно."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    roles: frozenset[str]
    partner_branches: tuple[str, ...] = ()
    sellers: tuple[str, ...] = ()
    # Стенд без identity: доступ открыт, и это обязано быть видно в ответе,
    # а не подразумеваться. В защищённых средах такой принципал не выдаётся.
    open_stand: bool = False

    @property
    def unrestricted(self) -> bool:
        return self.open_stand or bool(self.roles & UNRESTRICTED)

    @property
    def may_write(self) -> bool:
        return self.open_stand or bool(self.roles & WRITERS)


OPEN_STAND = Principal(user_id="стенд", roles=frozenset({"admin"}), open_stand=True)


class Principals:
    """Резолвер принципала через `identity`."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 5.0) -> None:
        self.base_url = (base_url if base_url is not None
                         else os.getenv("IDENTITY_URL", "")).strip().rstrip("/")
        self._timeout = timeout

    def of(self, authorization: str | None) -> Principal:
        if not self.base_url:
            # identity не настроен. На стенде это нормально — поток 0 его не
            # поднимал. В защищённой среде это открытая дверь, поэтому отказ.
            if app_environment() in LOCAL_ENVIRONMENTS:
                return OPEN_STAND
            raise Unauthorized(
                "IDENTITY_URL не задан: биллинг не может узнать, кто спрашивает, "
                "а отдавать деньги неизвестно кому нельзя")

        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if not token:
            raise Unauthorized("нужен заголовок Authorization: Bearer <токен>")

        cached = _introspection_cache.get(token)
        if cached is not None:
            return cached

        try:
            response = httpx.post(f"{self.base_url}/api/identity/v1/introspect",
                                  json={"token": token}, timeout=self._timeout)
        except httpx.HTTPError as failure:
            raise Unauthorized(f"identity недоступен: {failure}") from failure
        body = response.json() if response.content else {}
        if response.status_code >= 400 or not body.get("active"):
            raise Unauthorized(str(body.get("reason") or "identity не признал токен"))

        principal = Principal(
            user_id=str(body.get("user_id") or ""),
            roles=frozenset(body.get("roles") or ()),
            partner_branches=tuple(body.get("partner_branches") or ()),
            sellers=tuple(body.get("sellers") or ()),
        )
        _introspection_cache.put(token, principal)
        return principal


class _IntrospectionCache:
    """Ответ identity на тридцать секунд.

    Экран админки и ЛК опрашивают по несколько маршрутов подряд, и каждый
    ходил в identity: на одно открытие страницы — десяток вызовов туда и
    обратно, каждый со своим круговым временем. Тридцать секунд — меньше
    любого разумного срока отзыва прав и больше любой серии запросов одной
    страницы.

    Ключ — сам токен, и хранится он только в памяти процесса (инвариант 15:
    ни в лог, ни в базу).
    """

    TTL_SECONDS = 30.0
    LIMIT = 512

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, Principal]] = {}
        self._lock = threading.Lock()

    def get(self, token: str) -> Principal | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return None
            expires_at, principal = entry
            if expires_at <= now:
                self._entries.pop(token, None)
                return None
            return principal

    def put(self, token: str, principal: Principal) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._entries) >= self.LIMIT:
                # Чистим просроченное, а не выбираем «наименее нужное»: кэш
                # живёт тридцать секунд, и просроченного в нём всегда больше.
                self._entries = {key: value for key, value in self._entries.items()
                                 if value[0] > now}
                if len(self._entries) >= self.LIMIT:
                    self._entries.clear()
            self._entries[token] = (now + self.TTL_SECONDS, principal)


_introspection_cache = _IntrospectionCache()


def visible_partner_ids(cursor: Any, principal: Principal) -> list[str] | None:
    """Партнёры, которых этому человеку видно. None — видно всех.

    Ветка раскрывается рекурсивно через partner_subtree: старший менеджер видит
    свою ветку целиком, менеджер кабинетов — только себя, потому что под ним
    никого нет. Одно правило на обоих, без отдельной ветки в коде.
    """
    if principal.unrestricted:
        return None
    visible: set[str] = set()
    for root in principal.partner_branches:
        cursor.execute("SELECT partner_id FROM partner_subtree(%s)", (root,))
        visible.update(str(row["partner_id"]) for row in cursor.fetchall())
    return sorted(visible)


def visible_cabinet_ids(cursor: Any, principal: Principal) -> list[str] | None:
    """Кабинеты, которые видно. None — видно все.

    Клиенту видны его собственные (область seller), партнёру — закреплённые за
    его веткой. Закрепление берётся действующее: переданный другому кабинет
    перестаёт быть виден прежнему менеджеру, а его прошлые начисления — нет,
    и это правильно: комиссия за август остаётся его.
    """
    if principal.unrestricted:
        return None
    cabinets: set[str] = set()
    if principal.sellers:
        cursor.execute("SELECT id FROM cabinet WHERE seller_external_id = ANY(%s)",
                       (list(principal.sellers),))
        cabinets.update(str(row["id"]) for row in cursor.fetchall())
    partners = visible_partner_ids(cursor, principal)
    if partners:
        cursor.execute(
            "SELECT DISTINCT cabinet_id FROM cabinet_assignment "
            " WHERE partner_id = ANY(%s) AND (to_date IS NULL OR to_date > current_date)",
            (partners,))
        cabinets.update(str(row["cabinet_id"]) for row in cursor.fetchall())
    return sorted(cabinets)


def require_partner(cursor: Any, principal: Principal, partner_id: str) -> None:
    visible = visible_partner_ids(cursor, principal)
    if visible is None or partner_id in visible:
        return
    raise Forbidden(f"партнёр {partner_id} не в вашей ветке")


def require_write(principal: Principal) -> None:
    if not principal.may_write:
        raise Forbidden("менять справочники и деньги может администратор или бухгалтер")


def summary(principal: Principal) -> dict[str, Any]:
    return {"user_id": principal.user_id, "roles": sorted(principal.roles),
            "partner_branches": list(principal.partner_branches),
            "sellers": list(principal.sellers), "open_stand": principal.open_stand}


def any_of(values: Iterable[str]) -> list[str]:
    return list(values)
