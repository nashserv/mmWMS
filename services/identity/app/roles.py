"""Роли и выдачи. Логика прав, но не механика токенов.

Поток C владеет ролями сервиса identity и не трогает JWT/JWKS (файл 04,
«Владеет»). Поэтому здесь есть «кому что можно» и нет ни одной строки про
подпись, ключи и срок жизни токена.
"""
from __future__ import annotations

import uuid
from typing import Any

from .db import Database

# Роли, которые видят деньги и справочники. Список закрытый: право, добавленное
# молча, обнаруживается тогда, когда менеджер увидел чужую комиссию.
BILLING_READERS = frozenset({"admin", "accountant", "senior_manager", "account_manager"})
BILLING_WRITERS = frozenset({"admin", "accountant"})


class RoleDirectory:
    def __init__(self, database: Database) -> None:
        self.db = database

    def roles(self) -> list[dict[str, Any]]:
        with self.db.cursor() as cursor:
            cursor.execute("SELECT * FROM identity_role ORDER BY kind, code")
            return [dict(row) for row in cursor.fetchall()]

    def grant(self, user_id: str, role_code: str, granted_by: str, *,
              scope_kind: str = "global", scope_id: str | None = None) -> dict[str, Any]:
        if not (granted_by or "").strip():
            raise ValueError("выдача роли без автора неразбираема при разборе инцидента")
        with self.db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO identity_role_grant (id, user_id, role_code, scope_kind, scope_id,
                                                 granted_by)
                     VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, role_code, scope_kind, COALESCE(scope_id, ''))
                  WHERE revoked_at IS NULL DO NOTHING
                  RETURNING *
                """,
                (str(uuid.uuid4()), user_id, role_code, scope_kind, scope_id, granted_by))
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT * FROM identity_role_grant
                     WHERE user_id = %s AND role_code = %s AND scope_kind = %s
                       AND COALESCE(scope_id, '') = COALESCE(%s, '') AND revoked_at IS NULL
                    """,
                    (user_id, role_code, scope_kind, scope_id))
                row = cursor.fetchone()
            return dict(row)

    def revoke(self, grant_id: str, revoked_by: str) -> dict[str, Any] | None:
        """Отзыв — дата, а не удаление строки: кто и когда имел право, спросят."""
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE identity_role_grant SET revoked_at = now(), revoked_by = %s "
                " WHERE id = %s AND revoked_at IS NULL RETURNING *",
                (revoked_by, grant_id))
            row = cursor.fetchone()
            return dict(row) if row else None

    def principal(self, user_id: str) -> dict[str, Any]:
        """Кто этот человек и что ему можно.

        Отдаётся ровно то, чем биллинг и портал ограничивают выдачу: роли и
        области. Какие именно кабинеты стоят за веткой партнёра, знает биллинг —
        дерево менеджеров живёт там.
        """
        with self.db.cursor() as cursor:
            cursor.execute(
                """
                SELECT g.role_code, g.scope_kind, g.scope_id, r.kind, r.name
                  FROM identity_role_grant g
                  JOIN identity_role r ON r.code = g.role_code
                 WHERE g.user_id = %s AND g.revoked_at IS NULL
                 ORDER BY g.role_code
                """,
                (user_id,))
            grants = [dict(row) for row in cursor.fetchall()]
        return {
            "user_id": user_id,
            "roles": sorted({row["role_code"] for row in grants}),
            "scopes": [{"role": row["role_code"], "kind": row["scope_kind"],
                        "id": row["scope_id"]} for row in grants],
            "partner_branches": sorted({row["scope_id"] for row in grants
                                        if row["scope_kind"] == "partner_branch"
                                        and row["scope_id"]}),
            "sellers": sorted({row["scope_id"] for row in grants
                               if row["scope_kind"] == "seller" and row["scope_id"]}),
        }
