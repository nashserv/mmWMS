"""Справочник партнёров, дерево менеджеров и история закреплений.

История, а не поле: партнёра передают другому, а прошлые периоды
пересчитываться не должны (файл 04). Здесь это и проверяется — на деньгах.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.admin import Admin
from app.db import Database
from app.service import BillingService

from conftest import event, rows


def test_the_branch_of_a_senior_manager_includes_every_manager_under_them(
        database: Database, stand: dict[str, Any]) -> None:
    """Старший видит ветку целиком, менеджер — только себя (файл 04)."""
    with database.cursor() as cursor:
        cursor.execute("SELECT partner_id FROM partner_subtree(%s)", (stand["senior"],))
        branch = {str(row["partner_id"]) for row in cursor.fetchall()}
        cursor.execute("SELECT partner_id FROM partner_subtree(%s)", (stand["manager"],))
        own = {str(row["partner_id"]) for row in cursor.fetchall()}

    assert branch == {stand["senior"], stand["manager"]}
    assert own == {stand["manager"]}


def test_handing_a_cabinet_over_does_not_rewrite_the_past(
        database: Database, stand: dict[str, Any]) -> None:
    """Смена партнёра закрывает период, а не переписывает его.

    Ровно то, ради чего закрепление — история, а не поле partner_id на
    кабинете: начисление августа обязано остаться за августовским менеджером.
    """
    admin = Admin(database, wms=None)  # type: ignore[arg-type]
    service = BillingService(database)

    august = event("wms.packing.completed.v1", {"seller_id": "seller-1"},
                   occurred_at="2026-08-15T10:00:00+00:00")
    service.ingest(august)

    successor = str(uuid.uuid4())
    with database.transaction() as cursor:
        cursor.execute("INSERT INTO partner (id, parent_id, name) VALUES (%s, %s, 'Преемник')",
                       (successor, stand["senior"]))
    admin.assign_cabinet(stand["cabinet"], successor, "account_manager", date(2026, 9, 1))

    september = event("wms.packing.completed.v1", {"seller_id": "seller-1"},
                      occurred_at="2026-09-15T10:00:00+00:00")
    service.ingest(september)

    accruals = {row["period"]: str(row["partner_id"])
                for row in rows(database, "SELECT period, partner_id FROM billing_accrual")}
    assert accruals["2026-08"] == stand["manager"], "август переписан на нового партнёра"
    assert accruals["2026-09"] == successor

    # Старое закрепление закрыто датой, а не удалено: без периода не объяснить,
    # почему августовская комиссия ушла другому человеку.
    history = rows(database, "SELECT partner_id, from_date, to_date FROM cabinet_assignment "
                             "WHERE cabinet_id = %s ORDER BY from_date", (stand["cabinet"],))
    assert len(history) == 2
    assert history[0]["to_date"] == date(2026, 9, 1)
    assert history[1]["to_date"] is None


def test_two_partners_cannot_hold_the_same_cabinet_on_the_same_day(
        database: Database, stand: dict[str, Any]) -> None:
    """Два партнёра в одной роли на один день — это два счёта на одну операцию."""
    import psycopg

    other = str(uuid.uuid4())
    with database.transaction() as cursor:
        cursor.execute("INSERT INTO partner (id, parent_id, name) VALUES (%s, %s, 'Второй')",
                       (other, stand["senior"]))
    with pytest.raises(psycopg.errors.ExclusionViolation):
        with database.transaction() as cursor:
            cursor.execute(
                "INSERT INTO cabinet_assignment (id, cabinet_id, partner_id, role, from_date) "
                "VALUES (%s, %s, %s, 'account_manager', DATE '2026-06-01')",
                (str(uuid.uuid4()), stand["cabinet"], other))


def test_a_markup_can_be_overridden_for_one_client(
        database: Database, stand: dict[str, Any]) -> None:
    """Раздел 13, вопрос 5: одна ставка на партнёра, переопределяется на клиента."""
    Admin(database, wms=None).set_markup(  # type: ignore[arg-type]
        stand["senior"], "packing", Decimal("25"), date(2026, 1, 1),
        cabinet_id=stand["cabinet"])

    BillingService(database).ingest(event("wms.packing.completed.v1", {"seller_id": "seller-1"}))

    accrual = rows(database, "SELECT * FROM billing_accrual")[0]
    assert accrual["markup"] == Decimal("25.00")
    assert accrual["amount"] == Decimal("55.00")
    assert accrual["net_amount"] == Decimal("30.00"), "наценка партнёра съела нашу выручку"


def test_a_markup_change_applies_from_its_date_and_not_backwards(
        database: Database, stand: dict[str, Any]) -> None:
    """Прошлые месяцы считаются по ставке, действовавшей тогда."""
    Admin(database, wms=None).set_markup(  # type: ignore[arg-type]
        stand["senior"], "packing", Decimal("20"), date(2026, 9, 1))
    service = BillingService(database)

    service.ingest(event("wms.packing.completed.v1", {"seller_id": "seller-1"},
                         occurred_at="2026-08-15T10:00:00+00:00"))
    service.ingest(event("wms.packing.completed.v1", {"seller_id": "seller-1"},
                         occurred_at="2026-09-15T10:00:00+00:00"))

    by_period = {row["period"]: row["partner_amount"]
                 for row in rows(database, "SELECT period, partner_amount FROM billing_accrual")}
    assert by_period == {"2026-08": Decimal("15.00"), "2026-09": Decimal("20.00")}


def test_the_partner_is_not_paid_before_the_client_pays(
        database: Database, stand: dict[str, Any]) -> None:
    """Признание по факту оплаты (файл 04): pending до денег, payable после."""
    admin = Admin(database, wms=None)  # type: ignore[arg-type]
    BillingService(database).ingest(event("wms.packing.completed.v1", {"seller_id": "seller-1"}))

    assert rows(database, "SELECT partner_payout_state FROM billing_accrual")[0][
        "partner_payout_state"] == "pending"

    invoice = admin.issue_invoice(stand["cabinet"], "2026-09", "INV-2026-09-1")
    assert invoice["total_amount"] == Decimal("45.00")
    assert invoice["partner_total"] == Decimal("15.00")
    assert invoice["net_total"] == Decimal("30.00")
    assert rows(database, "SELECT partner_payout_state FROM billing_accrual")[0][
        "partner_payout_state"] == "pending", "выставленный счёт — ещё не оплаченный"

    admin.pay_invoice(str(invoice["id"]))
    assert rows(database, "SELECT partner_payout_state FROM billing_accrual")[0][
        "partner_payout_state"] == "payable"


def test_commission_of_a_manager_shows_own_and_branch_separately(
        database: Database, stand: dict[str, Any]) -> None:
    """Менеджер видит свою комиссию, старший — ветку целиком."""
    BillingService(database).ingest(event("wms.packing.completed.v1", {"seller_id": "seller-1"}))
    service = BillingService(database)

    senior = service.commission(stand["senior"], "2026-09")
    manager = service.commission(stand["manager"], "2026-09")

    assert senior["branch_total"] == Decimal("15.00")
    assert Decimal(senior["own"]["amount"]) == Decimal("15.00")
    assert manager["branch_total"] == Decimal("0"), "менеджеру видна чужая комиссия"


def test_a_cycle_in_the_manager_tree_is_refused_by_the_database(
        database: Database, stand: dict[str, Any]) -> None:
    """Цикл — это зависший отчёт о комиссии. Ловим на записи, не на чтении."""
    import psycopg

    with pytest.raises(psycopg.errors.RaiseException):
        with database.transaction() as cursor:
            cursor.execute("UPDATE partner SET parent_id = %s WHERE id = %s",
                           (stand["manager"], stand["senior"]))


def test_reassigning_the_same_partner_the_same_day_is_harmless(
        database: Database, stand: dict[str, Any]) -> None:
    """Человек нажал дважды, сеть моргнула — повтор обязан быть безвредным.

    Ограничение «два партнёра на один кабинет в один день» правильное, но
    падать на нём при повторе онбординга — значит наказывать за надёжность.
    """
    admin = Admin(database, wms=None)  # type: ignore[arg-type]
    first = admin.assign_cabinet(stand["cabinet"], stand["manager"], "account_manager",
                                 date(2026, 9, 10))
    again = admin.assign_cabinet(stand["cabinet"], stand["manager"], "account_manager",
                                 date(2026, 9, 10))

    assert first["id"] == again["id"]
    assert len(rows(database, "SELECT * FROM cabinet_assignment WHERE cabinet_id = %s "
                              "AND to_date IS NULL", (stand["cabinet"],))) == 1


def test_replacing_a_partner_on_the_same_day_leaves_one_assignment(
        database: Database, stand: dict[str, Any]) -> None:
    """У закрепления, начатого сегодня, вчерашних денег нет — заменяем, не плодим."""
    successor = str(uuid.uuid4())
    with database.transaction() as cursor:
        cursor.execute("INSERT INTO partner (id, parent_id, name) VALUES (%s, %s, 'Преемник')",
                       (successor, stand["senior"]))
    admin = Admin(database, wms=None)  # type: ignore[arg-type]
    admin.assign_cabinet(stand["cabinet"], stand["manager"], "account_manager", date(2026, 9, 10))
    admin.assign_cabinet(stand["cabinet"], successor, "account_manager", date(2026, 9, 10))

    open_now = rows(database, "SELECT partner_id, from_date FROM cabinet_assignment "
                              "WHERE cabinet_id = %s AND to_date IS NULL", (stand["cabinet"],))
    assert len(open_now) == 1
    assert str(open_now[0]["partner_id"]) == successor


def test_assigning_behind_a_future_assignment_is_refused(
        database: Database, stand: dict[str, Any]) -> None:
    """Молча подвинуть будущего партнёра — значит переписать деньги, которых ещё нет."""
    from app.repositories import AssignmentConflict

    successor = str(uuid.uuid4())
    with database.transaction() as cursor:
        cursor.execute("INSERT INTO partner (id, parent_id, name) VALUES (%s, %s, 'Преемник')",
                       (successor, stand["senior"]))
    admin = Admin(database, wms=None)  # type: ignore[arg-type]
    admin.assign_cabinet(stand["cabinet"], successor, "account_manager", date(2026, 12, 1))

    with pytest.raises(AssignmentConflict, match="задним числом"):
        admin.assign_cabinet(stand["cabinet"], stand["manager"], "account_manager",
                             date(2026, 10, 1))


def test_a_senior_on_the_cabinet_and_above_the_manager_is_paid_once(
        database: Database, stand: dict[str, Any]) -> None:
    """Один партнёр в цепочке — одна наценка, сколько бы раз он в ней ни был.

    Старший, закреплённый на кабинете НАПРЯМУЮ и одновременно родитель
    менеджера кабинетов, приходил в цепочку дважды — depth 0 и depth 1, — и
    его наценка складывалась сама с собой. Клиент платил её в двойном
    размере, а сумма комиссий не сходилась с долей партнёра в начислении.
    """
    with database.transaction() as cursor:
        # Старший закрепляется на том же кабинете вторым закреплением —
        # обычное дело: он ведёт клиента вместе со своим менеджером.
        cursor.execute(
            "INSERT INTO cabinet_assignment (id, cabinet_id, partner_id, role, from_date) "
            "VALUES (%s, %s, %s, 'senior_manager', DATE '2026-01-01')",
            (str(uuid.uuid4()), stand["cabinet"], stand["senior"]))

    BillingService(database).ingest(
        event("wms.packing.completed.v1", {"seller_id": "seller-1"}))

    accruals = rows(database,
                    "SELECT amount, partner_amount, net_amount FROM billing_accrual")
    assert len(accruals) == 1
    assert Decimal(accruals[0]["amount"]) == Decimal("45.00"), (
        f"клиенту выставлено {accruals[0]['amount']} вместо 45.00: наценка "
        f"старшего посчитана дважды")
    assert Decimal(accruals[0]["partner_amount"]) == Decimal("15.00")

    commissions = rows(database,
                       "SELECT partner_id, amount FROM billing_commission ORDER BY amount")
    assert sum(Decimal(row["amount"]) for row in commissions) == Decimal("15.00"), (
        "сумма комиссий не сошлась с наценкой начисления")
    assert len({str(row["partner_id"]) for row in commissions}) == len(commissions), (
        "один партнёр получил две строки комиссии")
