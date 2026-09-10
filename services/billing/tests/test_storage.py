"""Хранение — суточное начисление кроном, а не событием.

«Хранение, самый предсказуемый доход фулфилмента, не тарифицируется вообще»
(файл 04). Единица — коробко-место × сутки, значит источник количества — склад,
а не шина.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from app.db import Database
from app.service import BillingService

from conftest import rows

DAY = date(2026, 9, 9)


def _storage_tariff(database: Database, stand: dict[str, Any], *, price: str = "5.00",
                    approved: bool = True) -> None:
    with database.transaction() as cursor:
        cursor.execute(
            "INSERT INTO billing_tariff (id, code, service, name, unit, is_default) "
            "VALUES (gen_random_uuid(), 'storage-default', 'storage', 'Хранение', "
            "'коробко-место × сутки', true) RETURNING id")
        tariff = cursor.fetchone()["id"]
        cursor.execute(
            "INSERT INTO billing_tariff_version (id, tariff_id, effective_from, approved, "
            "approved_by, approved_at) VALUES (gen_random_uuid(), %s, DATE '2026-01-01', %s, "
            "%s, now()) RETURNING id",
            (tariff, approved, "владелец" if approved else None))
        version = cursor.fetchone()["id"]
        cursor.execute(
            "INSERT INTO billing_tariff_tier (id, version_id, up_to, unit_price) "
            "VALUES (gen_random_uuid(), %s, NULL, %s)", (version, Decimal(price)))
        cursor.execute(
            "INSERT INTO price_layer (id, partner_id, service, markup, from_date) "
            "VALUES (gen_random_uuid(), %s, 'storage', 0, DATE '2026-01-01')",
            (stand["senior"],))


def test_a_day_of_storage_is_charged_per_box_place(
        database: Database, stand: dict[str, Any]) -> None:
    """Место занимает коробка, а не то, сколько в неё положили."""
    _storage_tariff(database, stand)

    BillingService(database).accrue_storage(DAY, lambda seller: 12)

    accrual = rows(database, "SELECT * FROM billing_accrual WHERE service = 'storage'")[0]
    assert accrual["quantity"] == Decimal("12.000")
    assert accrual["amount"] == Decimal("60.00")
    assert accrual["occurred_on"] == DAY
    assert accrual["event_type"] == "billing.storage.day.v1"


def test_running_the_cron_twice_does_not_charge_twice(
        database: Database, stand: dict[str, Any]) -> None:
    """event_id вычислим из кабинета и дня — поэтому воркер можно гонять хоть каждый час."""
    _storage_tariff(database, stand)
    service = BillingService(database)

    first = service.accrue_storage(DAY, lambda seller: 4)
    second = service.accrue_storage(DAY, lambda seller: 4)

    assert first[0]["outcome"] == "accrued"
    assert second[0]["outcome"] == "duplicate"
    assert len(rows(database, "SELECT * FROM billing_accrual")) == 1


def test_a_cabinet_with_nothing_stored_pays_nothing(
        database: Database, stand: dict[str, Any]) -> None:
    """Ноль коробок — не ошибка и не строка в unbilled, а просто нет услуги."""
    _storage_tariff(database, stand)

    results = BillingService(database).accrue_storage(DAY, lambda seller: 0)

    assert results == []
    assert rows(database, "SELECT * FROM billing_accrual") == []
    assert rows(database, "SELECT * FROM billing_unbilled") == []


def test_a_warehouse_that_will_not_answer_leaves_a_trace(
        database: Database, stand: dict[str, Any]) -> None:
    """Кабинет, по которому склад молчит, не должен уронить остальные."""
    _storage_tariff(database, stand)
    with database.transaction() as cursor:
        cursor.execute("INSERT INTO cabinet (id, seller_external_id, name) "
                       "VALUES (%s, 'seller-2', 'ИП Второй')", (str(uuid.uuid4()),))

    def places(seller: str) -> int:
        if seller == "seller-1":
            raise RuntimeError("склад недоступен")
        return 3

    results = BillingService(database).accrue_storage(DAY, places)

    outcomes = {row["seller"]: row["outcome"] for row in results}
    assert outcomes["seller-1"] == "unbilled"
    assert outcomes["seller-2"] == "accrued"


def test_storage_without_an_approved_price_is_visible_not_silent(
        database: Database, stand: dict[str, Any]) -> None:
    _storage_tariff(database, stand, approved=False)

    results = BillingService(database).accrue_storage(DAY, lambda seller: 5)

    assert results[0]["reason"] == "TARIFF_NOT_APPROVED"
    unbilled = rows(database, "SELECT * FROM billing_unbilled")
    assert len(unbilled) == 1
    assert unbilled[0]["payload"]["places"] == "5"


def test_storage_carries_the_partner_markup_when_the_owner_sets_one(
        database: Database, stand: dict[str, Any]) -> None:
    """Ставит ли партнёр наценку на хранение — вопрос к владельцу (раздел 13, 5).

    Модель обязана уметь и то и другое: ноль в сиде — решение, а не ограничение.
    """
    _storage_tariff(database, stand)
    with database.transaction() as cursor:
        cursor.execute("UPDATE price_layer SET markup = 2 WHERE service = 'storage'")

    BillingService(database).accrue_storage(DAY, lambda seller: 10)

    accrual = rows(database, "SELECT * FROM billing_accrual")[0]
    assert accrual["amount"] == Decimal("70.00")
    assert accrual["partner_amount"] == Decimal("20.00")
    assert accrual["net_amount"] == Decimal("50.00")


def test_a_zero_partner_fee_is_a_decision_and_holds(
        database: Database, stand: dict[str, Any]) -> None:
    """Ноль в partner_fee обязан означать «наценки нет», а не «возьми откуда-нибудь».

    Ставка последней надежды из версии тарифа нужна для переноса данных прода,
    где наценка жила именно там. Но если у услуги её сознательно поставили в
    ноль — как у хранения до ответа владельца, — запасной путь не имеет права
    протащить наценку мимо решения.
    """
    _storage_tariff(database, stand)
    with database.transaction() as cursor:
        # Ни одного слоя наценки на хранение и ноль в версии тарифа.
        cursor.execute("DELETE FROM price_layer WHERE service = 'storage'")
        cursor.execute("UPDATE billing_tariff_version SET partner_fee = 0 "
                       "  WHERE tariff_id = (SELECT id FROM billing_tariff "
                       "                      WHERE service = 'storage')")

    BillingService(database).accrue_storage(DAY, lambda seller: 7)

    accrual = rows(database, "SELECT * FROM billing_accrual WHERE service = 'storage'")[0]
    assert accrual["partner_amount"] == Decimal("0.00")
    assert accrual["net_amount"] == accrual["amount"] == Decimal("35.00")


def test_the_stand_seed_leaves_storage_without_a_partner_markup() -> None:
    """Решение из README закреплено в сиде, а не только в тексте.

    Иначе следующая правка генератора вернёт 15 ₽ на хранение, и заметит это
    клиент в акте, а не мы.
    """
    import pathlib
    import re

    seed = (pathlib.Path(__file__).resolve().parents[1] / "seed" / "stand-billing.sql"
            ).read_text(encoding="utf-8")
    storage_tariff = re.search(r"\('([0-9a-f-]{36})', 'storage-default'", seed)
    assert storage_tariff, "в сиде нет тарифа на хранение"
    version_line = next(line for line in seed.splitlines()
                        if storage_tariff.group(1) in line and "DATE '2026-01-01'" in line
                        and "billing_tariff" not in line)
    assert version_line.rstrip(",").endswith("0.00)"), (
        f"версия тарифа на хранение несёт наценку: {version_line.strip()}")
