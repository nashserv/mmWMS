"""Постоянные расходы, их разнесение и маржа по кабинету.

`billing_fixed_expense` на проде пуст, поэтому маржа не считается вовсе: видна
выручка и не видно, какой из 21 кабинета съедает смену (файл 04).
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app.admin import Admin
from app.db import Database
from app.service import BillingService

from conftest import event, rows


def _second_cabinet(database: Database) -> str:
    cabinet_id = str(uuid.uuid4())
    with database.transaction() as cursor:
        cursor.execute(
            "INSERT INTO cabinet (id, seller_external_id, name) "
            "VALUES (%s, 'seller-2', 'ИП Второй')", (cabinet_id,))
    return cabinet_id


def test_expenses_are_split_by_the_rule_written_in_the_row(
        database: Database, stand: dict[str, Any]) -> None:
    """Правило разнесения — колонка, а не соглашение в голове (файл 04)."""
    _second_cabinet(database)
    service = BillingService(database)
    for _ in range(3):
        service.ingest(event("order.packed.v1", {"seller_id": "seller-1"}))
    service.ingest(event("order.packed.v1", {"seller_id": "seller-2"}))

    admin = Admin(database, wms=None)  # type: ignore[arg-type]
    admin.add_expense("2026-09", "payroll", Decimal("40000"), "ФОТ смены за сентябрь")
    result = admin.allocate_period("2026-09")

    assert result["allocations"] == 2
    allocated = {row["cabinet_id"]: row["amount"] for row in
                 rows(database, "SELECT cabinet_id, amount FROM billing_expense_allocation")}
    # Три операции против одной: 30 000 и 10 000, без потерянной копейки.
    assert sorted(allocated.values()) == [Decimal("10000.00"), Decimal("30000.00")]
    assert sum(allocated.values()) == Decimal("40000.00")


def test_margin_is_revenue_minus_partner_fee_minus_cost(
        database: Database, stand: dict[str, Any]) -> None:
    """выручка − наценка партнёра − себестоимость = маржа по кабинету."""
    service = BillingService(database)
    for _ in range(4):
        service.ingest(event("order.packed.v1", {"seller_id": "seller-1"}))

    admin = Admin(database, wms=None)  # type: ignore[arg-type]
    admin.add_expense("2026-09", "rent", Decimal("50"), "аренда, доля сентября")
    admin.allocate_period("2026-09")

    margin = service.margin("2026-09")
    assert len(margin) == 1
    row = margin[0]
    assert row["gross_revenue"] == Decimal("180.00"), "клиент заплатил 4 × 45"
    assert row["partner_fee"] == Decimal("60.00"), "партнёру 4 × 15"
    assert row["net_revenue"] == Decimal("120.00"), "MM-Express 4 × 30"
    assert row["allocated_cost"] == Decimal("50.00")
    assert row["margin"] == Decimal("70.00")


def test_an_expense_with_nothing_to_split_across_stays_visible(
        database: Database, stand: dict[str, Any]) -> None:
    """За период не было операций: делить не на что, и молча делить нельзя."""
    admin = Admin(database, wms=None)  # type: ignore[arg-type]
    admin.add_expense("2026-09", "rent", Decimal("50000"), "аренда за пустой месяц")

    result = admin.allocate_period("2026-09")

    assert result == {"period": "2026-09", "expenses": 1, "allocations": 0}
    assert rows(database, "SELECT * FROM billing_expense_allocation") == []


def test_a_direct_expense_lands_on_one_cabinet(
        database: Database, stand: dict[str, Any]) -> None:
    other = _second_cabinet(database)
    admin = Admin(database, wms=None)  # type: ignore[arg-type]
    admin.add_expense("2026-09", "consumables", Decimal("1200"),
                      "упаковка под негабарит этого клиента",
                      allocation_rule="direct", cabinet_id=other)

    admin.allocate_period("2026-09")

    allocation = rows(database, "SELECT * FROM billing_expense_allocation")[0]
    assert str(allocation["cabinet_id"]) == other
    assert allocation["amount"] == Decimal("1200.00")
    assert allocation["basis"] == "direct"
