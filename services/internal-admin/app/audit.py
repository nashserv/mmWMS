"""Журнал действий администратора.

Записывается всё, что меняет данные, вместе с результатом — включая неудачу.
Неудачная попытка нужна в журнале не меньше удачной: по ней и разбирают
«почему клиент не завёлся».
"""
from __future__ import annotations

import json
from typing import Any

from .db import Database

# Поля, которые не кладём в журнал целиком. secret_ref — ссылка, а не секрет,
# но начальный остаток клиента в журнале админки не нужен и раздувает строку.
_TRIMMED = ("opening_stock",)


def trim(request: dict[str, Any] | None) -> dict[str, Any]:
    if not request:
        return {}
    trimmed = dict(request)
    for key in _TRIMMED:
        if key in trimmed:
            value = trimmed[key]
            trimmed[key] = f"{len(value)} строк" if isinstance(value, list) else "…"
    return trimmed


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
                 json.dumps(trim(request), ensure_ascii=False), status,
                 json.dumps(response, ensure_ascii=False, default=str)))

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM admin_audit ORDER BY at DESC LIMIT %s", (max(1, min(limit, 500)),))
            return [dict(row) for row in cursor.fetchall()]
