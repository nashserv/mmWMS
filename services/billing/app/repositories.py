"""Запросы к базе billing. SQL здесь, решения — в service.py.

Разделено намеренно: правило «наценка партнёра берётся по цепочке закреплений
на дату операции» должно читаться в одном месте и проверяться тестом, а не
собираться из четырёх запросов по коду.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from collections.abc import Sequence

from psycopg import Cursor

from .domain import PartnerShare, Tier


def new_id() -> str:
    return str(uuid.uuid4())


def dig(payload: Any, path: str) -> Any:
    """Достаёт значение по точечному пути: 'task.quantity', 'orders'.

    Пути лежат в billing_billable_event, а не в коде: события пишут разные
    сервисы, и поле количества у них называется по-разному. Изменение формы
    чужого события не должно требовать релиза биллинга.
    """
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


# ------------------------------------------------------------------ кабинеты

def cabinet_by_seller(cursor: Cursor, seller_external_id: str) -> dict[str, Any] | None:
    """Кабинет по ключу владельца либо по внешнему id кабинета Wildberries.

    Оба, потому что событие приходит из разных сервисов: приложение E зовёт это
    поле `seller_id` и кладёт туда организацию, контракт wms — `seller_external_id`,
    а шлюз может прислать номер кабинета WB. Гадать по имени поля нельзя.
    """
    cursor.execute(
        """
        SELECT c.* FROM cabinet c
         WHERE c.seller_external_id = %(key)s
            OR c.wms_owner_id::text = %(key)s
            OR EXISTS (SELECT 1 FROM cabinet_wb_account w
                        WHERE w.cabinet_id = c.id AND w.external_id = %(key)s)
         LIMIT 1
        """,
        {"key": seller_external_id})
    return cursor.fetchone()


def attach_wb_account(cursor: Cursor, cabinet_id: str, external_id: str,
                      display_name: str | None, secret_ref: str | None) -> dict[str, Any]:
    """Подключает кабинет WB к клиенту. В базу едет ссылка на секрет, не токен."""
    cursor.execute(
        """
        INSERT INTO cabinet_wb_account (id, cabinet_id, external_id, display_name, secret_ref)
             VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (external_id) DO UPDATE
            SET display_name = COALESCE(EXCLUDED.display_name, cabinet_wb_account.display_name),
                secret_ref = COALESCE(EXCLUDED.secret_ref, cabinet_wb_account.secret_ref)
          RETURNING *
        """,
        (new_id(), cabinet_id, external_id, display_name, secret_ref))
    return cursor.fetchone()


def register_cabinet(cursor: Cursor, seller_external_id: str, *, name: str | None = None,
                     inn: str | None = None, wb_account_external_id: str | None = None,
                     organization_id: str | None = None, wms_owner_id: str | None = None,
                     needs_onboarding: bool = False) -> dict[str, Any]:
    """Заводит кабинет идемпотентно по seller_external_id."""
    cursor.execute(
        """
        INSERT INTO cabinet (id, seller_external_id, name, inn, organization_id,
                             needs_onboarding, wms_owner_id)
             VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (seller_external_id) DO UPDATE
            SET name = COALESCE(EXCLUDED.name, cabinet.name),
                inn = COALESCE(EXCLUDED.inn, cabinet.inn),
                organization_id =
                    COALESCE(EXCLUDED.organization_id, cabinet.organization_id),
                wms_owner_id = COALESCE(EXCLUDED.wms_owner_id, cabinet.wms_owner_id)
          RETURNING *
        """,
        (new_id(), seller_external_id, name or seller_external_id, inn, organization_id,
         needs_onboarding, wms_owner_id))
    cabinet = cursor.fetchone()
    if wb_account_external_id:
        attach_wb_account(cursor, str(cabinet["id"]), wb_account_external_id, name, None)
    return cabinet


def cabinets(cursor: Cursor, *, only_needing_onboarding: bool = False) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT * FROM cabinet WHERE (%s IS FALSE OR needs_onboarding) ORDER BY seller_external_id",
        (only_needing_onboarding,))
    return list(cursor.fetchall())


# ------------------------------------------------------------------ партнёры

def create_partner(cursor: Cursor, name: str, *, parent_id: str | None = None,
                   user_id: str | None = None, partner_id: str | None = None) -> dict[str, Any]:
    cursor.execute(
        "INSERT INTO partner (id, parent_id, name, user_id) VALUES (%s, %s, %s, %s) RETURNING *",
        (partner_id or new_id(), parent_id, name, user_id))
    return cursor.fetchone()


def partner_tree(cursor: Cursor) -> list[dict[str, Any]]:
    """Дерево целиком, с глубиной и числом закреплённых кабинетов."""
    cursor.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT p.*, 0 AS depth, p.name::text AS path
              FROM partner p WHERE p.parent_id IS NULL
            UNION ALL
            SELECT child.*, tree.depth + 1, tree.path || ' / ' || child.name
              FROM partner child JOIN tree ON child.parent_id = tree.id
             WHERE tree.depth < 32
        )
        SELECT tree.*,
               (SELECT count(*) FROM cabinet_assignment ca
                 WHERE ca.partner_id = tree.id AND ca.to_date IS NULL) AS active_cabinets
          FROM tree ORDER BY path
        """)
    return list(cursor.fetchall())


def subtree_ids(cursor: Cursor, root_partner_id: str) -> list[str]:
    """Партнёр и вся его ветка — основа прав (файл 04, «Роли и права»)."""
    cursor.execute("SELECT partner_id FROM partner_subtree(%s)", (root_partner_id,))
    return [str(row["partner_id"]) for row in cursor.fetchall()]


class AssignmentConflict(RuntimeError):
    """Закрепление позади уже заведённого будущего — это не история, а путаница."""


def assign_cabinet(cursor: Cursor, cabinet_id: str, partner_id: str, role: str,
                   from_date: date, *, comment: str | None = None) -> dict[str, Any]:
    """Закрепляет кабинет за партнёром, закрывая предыдущее закрепление.

    Закрытие — не UPDATE поля, а конец периода: прошлые месяцы обязаны
    считаться по тому партнёру, который вёл кабинет тогда.

    Три случая, и все три встречаются в жизни:

    1. **Тот же партнёр с той же или более ранней даты** — делать нечего.
       Повторный онбординг (человек нажал дважды, сеть моргнула) обязан быть
       безвредным, а не падать на ограничении базы.
    2. **Другой партнёр с более поздней даты** — обычная передача: старое
       закрепление закрывается этой датой, новое начинается с неё.
    3. **Другой партнёр с той же даты** — у старого закрепления нет ни одного
       прошедшего дня, тарифицировать по нему было нечего. Заменяем, а не
       плодим второе: два партнёра на один день — два счёта на одну операцию.

    Закрепление задним числом позади уже заведённого будущего — отказ: молча
    подвинуть будущего партнёра значит переписать деньги, которых ещё нет.
    """
    # `FOR UPDATE` на текущем закреплении.
    #
    # Два онбординга одного кабинета одновременно — обычное дело: человек
    # нажал дважды, сеть моргнула, консьюмер переигрывает. Без блокировки оба
    # читали «закрепления нет» и заводили по одному: два партнёра на один
    # день — два счёта на одну операцию. Ограничение исключения ловило это
    # уже как ошибку базы, а ошибка базы на онбординге выглядит как
    # «не работает».
    cursor.execute(
        """
        SELECT * FROM cabinet_assignment
         WHERE cabinet_id = %s AND role = %s AND to_date IS NULL
         ORDER BY from_date DESC LIMIT 1 FOR UPDATE
        """,
        (cabinet_id, role))
    current = cursor.fetchone()

    if current is not None:
        if str(current["partner_id"]) == str(partner_id) and current["from_date"] <= from_date:
            return current
        if current["from_date"] > from_date:
            raise AssignmentConflict(
                f"кабинет уже закреплён с {current['from_date']}, а закрепление просят "
                f"с {from_date}: задним числом позади будущего закрепления не ставим")
        if current["from_date"] == from_date:
            cursor.execute("DELETE FROM cabinet_assignment WHERE id = %s", (current["id"],))
        else:
            cursor.execute("UPDATE cabinet_assignment SET to_date = %s WHERE id = %s",
                           (from_date, current["id"]))

    cursor.execute(
        """
        INSERT INTO cabinet_assignment (id, cabinet_id, partner_id, role, from_date, comment)
             VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (new_id(), cabinet_id, partner_id, role, from_date, comment))
    return cursor.fetchone()


def set_price_layer(cursor: Cursor, partner_id: str, service: str, markup: Decimal,
                    from_date: date, *, cabinet_id: str | None = None) -> dict[str, Any]:
    """Ставит наценку партнёра с этой даты, закрывая предыдущую."""
    cursor.execute(
        """
        UPDATE price_layer SET to_date = %s
         WHERE partner_id = %s AND service = %s AND to_date IS NULL AND from_date < %s
           AND cabinet_id IS NOT DISTINCT FROM %s
        """,
        (from_date, partner_id, service, from_date, cabinet_id))
    cursor.execute(
        """
        INSERT INTO price_layer (id, partner_id, cabinet_id, service, markup, from_date)
             VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (new_id(), partner_id, cabinet_id, service, markup, from_date))
    return cursor.fetchone()


def partner_chain(cursor: Cursor, cabinet_id: str, service: str,
                  on: date) -> tuple[str | None, list[PartnerShare]]:
    """Кто ведёт кабинет на эту дату и чьи наценки на нём лежат.

    Возвращает (партнёр кабинета, доли по цепочке вверх). Партнёр кабинета —
    самое специфичное закрепление: менеджер кабинетов, если он есть, иначе
    старший. По нему считается «мои клиенты», а через partner_subtree — «моя
    ветка» (файл 04).

    Доли берутся со всей цепочки вверх: наценку добавляет тот, у кого есть
    слой цены. Допущение раздела 13, вопрос 6 — у менеджера кабинетов своей
    наценки нет, — выражено отсутствием строки в price_layer, а не отдельной
    веткой в коде.
    """
    cursor.execute(
        """
        WITH RECURSIVE assigned AS (
            SELECT ca.partner_id,
                   CASE ca.role WHEN 'account_manager' THEN 0 ELSE 1 END AS specificity
              FROM cabinet_assignment ca
             WHERE ca.cabinet_id = %(cabinet)s
               AND ca.from_date <= %(on)s
               AND (ca.to_date IS NULL OR ca.to_date > %(on)s)
        ), chain AS (
            SELECT p.id, p.parent_id, 0 AS depth
              FROM partner p JOIN assigned a ON a.partner_id = p.id
             WHERE p.active
            UNION
            SELECT parent.id, parent.parent_id, chain.depth + 1
              FROM partner parent JOIN chain ON chain.parent_id = parent.id
             WHERE parent.active AND chain.depth < 32
        )
        -- КАЖДЫЙ партнёр в цепочке ровно один раз.
        --
        -- `UNION` в рекурсивной части отбрасывает одинаковые строки, но не
        -- одного партнёра на разной глубине. Старший, закреплённый на кабинете
        -- НАПРЯМУЮ и одновременно родитель менеджера кабинетов, приходил
        -- дважды: depth 0 и depth 1 — и его наценка складывалась сама с собой.
        -- Клиент платил её в двойном размере, а сумма комиссий не сходилась с
        -- долей партнёра в начислении.
        , unique_chain AS (
            SELECT id, min(depth) AS depth FROM chain GROUP BY id
        )
        SELECT chain.id AS partner_id,
               chain.depth,
               COALESCE(override.markup, base.markup, 0) AS markup,
               COALESCE(override.id, base.id) AS price_layer_id
          FROM unique_chain AS chain
          LEFT JOIN LATERAL (
              SELECT pl.id, pl.markup FROM price_layer pl
               WHERE pl.partner_id = chain.id AND pl.service = %(service)s
                 AND pl.cabinet_id = %(cabinet)s
                 AND pl.from_date <= %(on)s AND (pl.to_date IS NULL OR pl.to_date > %(on)s)
               LIMIT 1) override ON true
          LEFT JOIN LATERAL (
              SELECT pl.id, pl.markup FROM price_layer pl
               WHERE pl.partner_id = chain.id AND pl.service = %(service)s
                 AND pl.cabinet_id IS NULL
                 AND pl.from_date <= %(on)s AND (pl.to_date IS NULL OR pl.to_date > %(on)s)
               LIMIT 1) base ON true
         ORDER BY chain.depth
        """,
        {"cabinet": cabinet_id, "service": service, "on": on})
    rows = list(cursor.fetchall())

    cursor.execute(
        """
        SELECT ca.partner_id FROM cabinet_assignment ca
         WHERE ca.cabinet_id = %s AND ca.from_date <= %s
           AND (ca.to_date IS NULL OR ca.to_date > %s)
         ORDER BY CASE ca.role WHEN 'account_manager' THEN 0 ELSE 1 END, ca.from_date DESC
         LIMIT 1
        """,
        (cabinet_id, on, on))
    owner_row = cursor.fetchone()
    owner = str(owner_row["partner_id"]) if owner_row else None

    shares = [PartnerShare(partner_id=str(row["partner_id"]),
                           markup=Decimal(row["markup"]),
                           price_layer_id=(str(row["price_layer_id"])
                                           if row["price_layer_id"] else None))
              for row in rows if Decimal(row["markup"]) > 0]
    return owner, shares


# ------------------------------------------------------------------- тарифы

def period_is_closed(cursor: Cursor, period: str) -> bool:
    """Закрыт ли расчётный период.

    Закрытый период значит выставленный счёт: дописать в него начисление —
    это разойтись с бумагой, которая уже лежит у клиента.
    """
    cursor.execute("SELECT state FROM billing_period WHERE period = %s", (period,))
    row = cursor.fetchone()
    return bool(row) and row["state"] == "closed"


def billable_event(cursor: Cursor, event_type: str) -> dict[str, Any] | None:
    cursor.execute("SELECT * FROM billing_billable_event WHERE event_type = %s", (event_type,))
    return cursor.fetchone()


def tariff_version(cursor: Cursor, cabinet_id: str, service: str,
                   on: date) -> dict[str, Any] | None:
    """Версия тарифа, действующая для кабинета на эту дату.

    Сначала тариф по договору кабинета, затем тариф услуги по умолчанию.
    Порядок явный: «какой тариф применился» не должен зависеть от того, в
    каком порядке база вернула строки.
    """
    cursor.execute(
        """
        SELECT v.*, t.service, t.code, t.unit, t.id AS tariff_id,
               (ta.id IS NOT NULL) AS from_contract
          FROM billing_tariff t
          JOIN billing_tariff_version v ON v.tariff_id = t.id
           AND v.effective_from <= %(on)s
           AND (v.effective_to IS NULL OR v.effective_to > %(on)s)
          LEFT JOIN billing_tariff_assignment ta
                 ON ta.tariff_id = t.id AND ta.cabinet_id = %(cabinet)s
                AND ta.service = %(service)s AND ta.from_date <= %(on)s
                AND (ta.to_date IS NULL OR ta.to_date > %(on)s)
         WHERE t.service = %(service)s AND t.active
           AND (ta.id IS NOT NULL OR t.is_default)
         ORDER BY from_contract DESC, v.effective_from DESC
         LIMIT 1
        """,
        {"cabinet": cabinet_id, "service": service, "on": on})
    return cursor.fetchone()


def tiers(cursor: Cursor, version_id: str) -> list[Tier]:
    cursor.execute(
        "SELECT up_to, unit_price, minimum FROM billing_tariff_tier WHERE version_id = %s",
        (version_id,))
    return [Tier(unit_price=Decimal(row["unit_price"]),
                 up_to=(Decimal(row["up_to"]) if row["up_to"] is not None else None),
                 minimum=Decimal(row["minimum"]))
            for row in cursor.fetchall()]


# ------------------------------------------------------------- приём событий

def claim_event(cursor: Cursor, event_id: str, event_type: str, tenant_id: str,
                correlation_id: str | None, payload: dict[str, Any],
                occurred_at: datetime) -> bool:
    """Кладёт событие в inbox. False — событие уже НАЧИСЛЕНО (инвариант 5).

    Повтор начисленного не переигрывается: at-least-once доставка шины иначе
    удвоит счёт клиенту, а это разговор, которого не должно быть.

    Но событие, которое НЕ начислено — `unbilled` (тариф не утверждён, клиент
    не заведён) или `failed`, — переигрывается и обязано переигрываться.
    Иначе причину устраняют, а событие остаётся невыставленным навсегда:
    ровно так неоплаченная работа копилась и оставалась неоплаченной.
    """
    # Начисление живёт дольше строки inbox: ретеншен убирает разобранное через
    # 90 дней, а деньги остаются. Повтор доставки после уборки нашёл бы inbox
    # пустым и начислил бы второй раз — по событию, за которое клиент уже
    # заплатил. `billing_accrual.event_id` уникален и служит вторым рубежом.
    cursor.execute("SELECT 1 FROM billing_accrual WHERE event_id = %s", (event_id,))
    if cursor.fetchone() is not None:
        return False

    cursor.execute(
        """
        INSERT INTO billing_inbox (event_id, event_type, tenant_id, correlation_id,
                                   payload, occurred_at, attempts)
             VALUES (%s, %s, %s, %s, %s, %s, 1)
        ON CONFLICT (event_id) DO UPDATE
           SET attempts = billing_inbox.attempts + 1,
               processed_at = NULL,
               last_error = NULL,
               payload = EXCLUDED.payload,
               correlation_id = COALESCE(EXCLUDED.correlation_id,
                                         billing_inbox.correlation_id)
         WHERE billing_inbox.outcome IN ('unbilled', 'failed')
          RETURNING event_id
        """,
        (event_id, event_type, tenant_id, correlation_id, json.dumps(payload, ensure_ascii=False),
         occurred_at))
    return cursor.fetchone() is not None


def finish_event(cursor: Cursor, event_id: str, outcome: str,
                 error: str | None = None) -> None:
    cursor.execute(
        """
        UPDATE billing_inbox
           SET processed_at = now(), outcome = %s, last_error = %s
         WHERE event_id = %s
        """,
        (outcome, error, event_id))


def record_unbilled(cursor: Cursor, event_id: str, event_type: str, reason: str,
                    detail: str, payload: dict[str, Any], tenant_id: str | None,
                    occurred_at: datetime | None) -> None:
    """Событие, не ставшее начислением, обязано остаться видимым."""
    cursor.execute(
        """
        INSERT INTO billing_unbilled (id, event_id, event_type, tenant_id, reason, detail,
                                      payload, occurred_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO UPDATE SET reason = EXCLUDED.reason, detail = EXCLUDED.detail
        """,
        (new_id(), event_id, event_type, tenant_id, reason, detail,
         json.dumps(payload, ensure_ascii=False), occurred_at))


def resolve_unbilled(cursor: Cursor, event_id: str) -> None:
    """Событие дошло до счёта: строка в отчёте закрывается.

    Без этого переигранное событие оставалось в отчёте «не дошло до счёта»
    навсегда — и отчёт переставал значить что-либо: в нём вперемешку лежало
    разобранное и неразобранное.
    """
    cursor.execute(
        "UPDATE billing_unbilled SET resolved_at = now() "
        " WHERE event_id = %s AND resolved_at IS NULL", (event_id,))


# ----------------------------------------------------------------- начисления

def insert_accrual(cursor: Cursor, *, event_id: str, event_type: str, tenant_id: str,
                   correlation_id: str | None, cabinet: dict[str, Any], service: str,
                   tariff_version_id: str | None, charge: Any, partner_id: str | None,
                   occurred_on: date, allocation_key: str | None) -> dict[str, Any]:
    cursor.execute(
        """
        INSERT INTO billing_accrual (
            id, event_id, event_type, tenant_id, correlation_id,
            cabinet_id, seller_external_id, organization_id,
            service, tariff_version_id, quantity, unit_price, markup,
            amount, partner_id, partner_amount, occurred_on, allocation_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING *
        """,
        (new_id(), event_id, event_type, tenant_id, correlation_id,
         cabinet["id"], cabinet["seller_external_id"], cabinet.get("organization_id"),
         service, tariff_version_id, charge.quantity, charge.unit_price, charge.markup,
         charge.amount, partner_id, charge.partner_amount, occurred_on, allocation_key))
    row = cursor.fetchone()
    if row is None:
        cursor.execute("SELECT * FROM billing_accrual WHERE event_id = %s", (event_id,))
        row = cursor.fetchone()
    return row


def insert_commissions(cursor: Cursor, accrual_id: str,
                       split: Sequence[tuple[PartnerShare, Decimal]]) -> None:
    for share, amount in split:
        cursor.execute(
            """
            INSERT INTO billing_commission (id, accrual_id, partner_id, markup, amount,
                                            price_layer_id)
                 VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (accrual_id, partner_id) DO NOTHING
            """,
            (new_id(), accrual_id, share.partner_id, share.markup, amount,
             share.price_layer_id))


def emit(cursor: Cursor, event_type: str, tenant_id: str, payload: dict[str, Any],
         correlation_id: str | None) -> str:
    """Кладёт событие биллинга в собственный outbox — той же транзакцией."""
    event_id = new_id()
    cursor.execute(
        """
        INSERT INTO billing_outbox (event_id, type, tenant_id, payload, correlation_id)
             VALUES (%s, %s, %s, %s, %s)
        """,
        (event_id, event_type, tenant_id, json.dumps(payload, ensure_ascii=False),
         correlation_id))
    return event_id


def record_shift_output(cursor: Cursor, *, event_id: str, event_type: str, actor_id: str,
                        occurred_at: datetime, operation: str, cabinet_id: str | None,
                        quantity: Decimal) -> None:
    """Выработка смены: кто сколько сделал (файл 04)."""
    cursor.execute(
        """
        INSERT INTO billing_shift_output (id, event_id, event_type, actor_id, occurred_on,
                                          occurred_at, operation, cabinet_id, quantity)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (new_id(), event_id, event_type, actor_id, occurred_at.date(), occurred_at,
         operation, cabinet_id, quantity))
