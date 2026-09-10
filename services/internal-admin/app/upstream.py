"""Клиент биллинга и identity.

Админка — тонкая: она не считает деньги и не хранит справочники, она их
показывает и записывает, кто их трогал. Всё, что похоже на решение, живёт в
биллинге, и второй копии правил здесь нет намеренно — разошедшиеся копии
обнаруживаются на разнице в счёте клиента.

Токен пользователя прокидывается насквозь: права считает биллинг по ответу
identity, админка их не переизобретает.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .config import billing_url, identity_url


class Upstream(RuntimeError):
    """Сервис за админкой не ответил."""


class BillingClient:
    def __init__(self, base_url: str | None = None, *, timeout: float = 15.0) -> None:
        self.base_url = (base_url or billing_url()).rstrip("/")
        self._timeout = timeout

    def call(self, method: str, path: str, authorization: str | None, *,
             params: dict[str, Any] | None = None,
             json_body: dict[str, Any] | None = None) -> tuple[int, Any]:
        headers = {"Authorization": authorization} if authorization else {}
        try:
            response = httpx.request(method, f"{self.base_url}{path}", headers=headers,
                                     params=params, json=json_body, timeout=self._timeout)
        except httpx.HTTPError as failure:
            raise Upstream(f"биллинг недоступен: {failure}") from failure
        try:
            body = response.json() if response.content else {}
        except json.JSONDecodeError:
            body = {"error": response.text[:500]}
        return response.status_code, body

    def get(self, path: str, authorization: str | None,
            params: dict[str, Any] | None = None) -> tuple[int, Any]:
        return self.call("GET", path, authorization, params=params)

    def post(self, path: str, authorization: str | None,
             json_body: dict[str, Any] | None = None) -> tuple[int, Any]:
        return self.call("POST", path, authorization, json_body=json_body)


class IdentityClient:
    def __init__(self, base_url: str | None = None, *, timeout: float = 5.0) -> None:
        self.base_url = (base_url or identity_url()).rstrip("/")
        self._timeout = timeout

    def whoami(self, authorization: str | None) -> dict[str, Any]:
        """Кто вошёл. Нужен админке для одного — что показывать на экране.

        Право на данные проверяет биллинг: тот, кто отдаёт, тот и решает.
        Проверка в двух местах однажды разойдётся, и разойдётся в пользу
        показать лишнее.
        """
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if not token:
            return {"active": False, "reason": "нужен заголовок Authorization: Bearer <токен>"}
        try:
            response = httpx.post(f"{self.base_url}/api/identity/v1/introspect",
                                  json={"token": token}, timeout=self._timeout)
        except httpx.HTTPError as failure:
            raise Upstream(f"identity недоступен: {failure}") from failure
        return response.json() if response.content else {"active": False}

    def roles(self) -> list[dict[str, Any]]:
        try:
            response = httpx.get(f"{self.base_url}/api/identity/v1/roles", timeout=self._timeout)
        except httpx.HTTPError as failure:
            raise Upstream(f"identity недоступен: {failure}") from failure
        return (response.json() or {}).get("roles", [])

    def grant(self, authorization: str | None, body: dict[str, Any]) -> tuple[int, Any]:
        try:
            response = httpx.post(f"{self.base_url}/api/identity/v1/grants", json=body,
                                  timeout=self._timeout)
        except httpx.HTTPError as failure:
            raise Upstream(f"identity недоступен: {failure}") from failure
        return response.status_code, (response.json() if response.content else {})
