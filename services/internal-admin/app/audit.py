"""Журнал действий администратора.

Записывается всё, что меняет данные, вместе с результатом — включая неудачу.
Неудачная попытка нужна в журнале не меньше удачной: по ней и разбирают
«почему клиент не завёлся».
"""
from __future__ import annotations

import json
from typing import Any

from .db import Database

import re

# Поля, которые не кладём в журнал целиком. Начальный остаток клиента в журнале
# админки не нужен и раздувает строку.
_TRIMMED = ("opening_stock",)

# Форма JWT. Журнал админки читают при разборе инцидентов, и живой токен,
# попавший в него, живёт там дольше, чем сам инцидент (инвариант 15).
# Сегменты намеренно короткие: настоящий признак — префикс `eyJ` (это `{"` в
# base64) и три части через точку. Требовать длины значит однажды пропустить
# токен, у которого подпись короче ожидаемого. Лишнее срабатывание при
# редактировании не стоит ничего, пропущенный токен стоит всего.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}")

# Ссылка на секрет — не секрет, но и не то, что нужно хранить у неудачных
# попыток: по журналу отказов видно, какие `secret_ref` перебирали.
_SECRETISH = ("secret_ref", "token", "access_token", "api_key", "password")


def redact(value: Any) -> Any:
    """Вырезать из значения всё, похожее на живой токен, на любой глубине."""
    if isinstance(value, str):
        return _JWT_RE.sub("<токен вырезан>", value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def trim(request: dict[str, Any] | None, *, status: int = 200) -> dict[str, Any]:
    if not request:
        return {}
    trimmed = dict(request)
    for key in _TRIMMED:
        if key in trimmed:
            value = trimmed[key]
            trimmed[key] = f"{len(value)} строк" if isinstance(value, list) else "…"
    if status >= 400:
        # Действие не прошло — хранить его секретоподобные поля незачем.
        for key in _SECRETISH:
            if key in trimmed:
                trimmed[key] = "<не сохранено: действие отклонено>"
    return redact(trimmed)


class Audit:
    def __init__(self, database: Database) -> None:
        self.db = database

    def record(self, *, actor_id: str, actor_roles: list[str], action: str,
               subject: str | None, request: dict[str, Any] | None,
               status: int, response: Any) -> None:
        with self.db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO admin_audit (actor_id, actor_roles, action, subject, request,
                                         status, response)
                     VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (actor_id, actor_roles, action, subject,
                 json.dumps(trim(request, status=status), ensure_ascii=False), status,
                 json.dumps(redact(response), ensure_ascii=False, default=str)))

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM admin_audit ORDER BY at DESC LIMIT %s", (max(1, min(limit, 500)),))
            return [dict(row) for row in cursor.fetchall()]
