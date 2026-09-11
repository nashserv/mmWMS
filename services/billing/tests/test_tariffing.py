"""Событие шины → начисление. Или строка в billing_unbilled с причиной.

Третьего исхода нет — именно он и даёт на проде 145 начислений на 6374 задания.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.db import Database
from app.service import BillingService

from conftest import event, rows


def test_a_packed_order_becomes_an_accrual_of_45_15_30(
        database: Database, stand: dict[str, Any]) -> None:
    """Шаг 12 полного прогона, дословно: amount=45, partner_amount=15, net=30."""
    service = BillingService(database)
    result = service.ingest(event("wms.packing.completed.v1", {"seller_id": "seller-1", "task_id": "t-1"}))

    assert result["outcome"] == "accrued", result
    accrual = rows(database, "SELECT * FROM billing_accrual")[0]
    assert (accrual["amount"], accrual["partner_amount"], accrual["net_amount"]) == (
        Decimal("45.00"), Decimal("15.00"), Decimal("30.00"))
    assert accrual["service"] == "packing"
    assert accrual["period"] == "2026-09"
    # Наценку получает старший менеджер, а числится кабинет за своим менеджером:
    # по нему считается «мои клиенты», по ветке — «моя комиссия».
    assert str(accrual["partner_id"]) == stand["manager"]
    commission = rows(database, "SELECT * FROM billing_commission")
    assert len(commission) == 1
    assert str(commission[0]["partner_id"]) == stand["senior"]
    assert commission[0]["amount"] == Decimal("15.00")


def test_the_same_event_twice_does_not_bill_the_client_twice(
        database: Database, stand: dict[str, Any]) -> None:
    """Шина доставляет at-least-once. Повтор обязан быть безвредным (инвариант 5)."""
    service = BillingService(database)
    message = event("wms.packing.completed.v1", {"seller_id": "seller-1"})

    first = service.ingest(message)
    second = service.ingest(message)

    assert first["outcome"] == "accrued"
    assert second["outcome"] == "duplicate"
    assert len(rows(database, "SELECT * FROM billing_accrual")) == 1


def test_quantity_comes_from_the_payload_where_the_mapping_says(
        database: Database, stand: dict[str, Any]) -> None:
    """`orders` в поставке — тарифицируемое количество (приложение E)."""
    with database.transaction() as cursor:
        cursor.execute(
            "INSERT INTO billing_tariff (id, code, service, name, unit, is_default) "
            "VALUES (gen_random_uuid(), 'shipping-default', 'shipping', 'Отгрузка', "
            "'заказ в поставке', true) RETURNING id")
        tariff = cursor.fetchone()["id"]
        cursor.execute(
            "INSERT INTO billing_tariff_version (id, tariff_id, effective_from, approved, "
            "approved_by, approved_at) VALUES (gen_random_uuid(), %s, DATE '2026-01-01', true, "
            "'владелец', now()) RETURNING id", (tariff,))
        version = cursor.fetchone()["id"]
        cursor.execute(
            "INSERT INTO billing_tariff_tier (id, version_id, up_to, unit_price) "
            "VALUES (gen_random_uuid(), %s, NULL, 30.00)", (version,))
        cursor.execute(
            "INSERT INTO price_layer (id, partner_id, service, markup, from_date) "
            "VALUES (gen_random_uuid(), %s, 'shipping', 15.00, DATE '2026-01-01')",
            (stand["senior"],))

    BillingService(database).ingest(event("wb.supply.shipped.v1", {
        "supply": "WB-GI-1", "seller_id": "seller-1", "orders": 5,
        "accepted_at": None, "name": "поставка"}))

    accrual = rows(database, "SELECT * FROM billing_accrual")[0]
    assert accrual["quantity"] == Decimal("5.000")
    assert accrual["amount"] == Decimal("225.00")
    assert accrual["partner_amount"] == Decimal("75.00")


def test_an_event_nobody_tariffs_is_skipped_with_the_reason_from_the_table(
        database: Database, stand: dict[str, Any]) -> None:
    """Решение «не тарифицируем» лежит в таблице и объяснено, а не подразумевается."""
    with database.transaction() as cursor:
        cursor.execute(
            "INSERT INTO billing_billable_event (event_type, service, active, comment) "
            "VALUES ('wms.item.scanned.v1', 'picking', false, "
            "'скан — шаг подбора, а не услуга')")

    result = BillingService(database).ingest(
        event("wms.item.scanned.v1", {"seller_id": "seller-1"}))

    assert result["outcome"] == "skipped"
    assert "не услуга" in (result["detail"] or "")
    assert rows(database, "SELECT * FROM billing_accrual") == []


def test_a_billable_event_without_a_tariff_lands_in_unbilled_not_in_silence(
        database: Database, stand: dict[str, Any]) -> None:
    """Разница между «склад отработал» и «клиенту выставлено» обязана быть видна."""
    with database.transaction() as cursor:
        cursor.execute("UPDATE billing_tariff SET active = false")

    result = BillingService(database).ingest(
        event("wms.packing.completed.v1", {"seller_id": "seller-1"}))

    assert result["outcome"] == "unbilled"
    assert result["reason"] == "NO_TARIFF"
    unbilled = rows(database, "SELECT * FROM billing_unbilled")
    assert len(unbilled) == 1
    assert unbilled[0]["payload"]["seller_id"] == "seller-1"


def test_an_unapproved_price_never_reaches_the_client(
        database: Database, stand: dict[str, Any]) -> None:
    """Цена без подписи — это спор с клиентом, а не строка в счёте."""
    with database.transaction() as cursor:
        cursor.execute("UPDATE billing_tariff_version SET approved = false, "
                       "approved_by = NULL, approved_at = NULL")

    result = BillingService(database).ingest(event("wms.packing.completed.v1", {"seller_id": "seller-1"}))

    assert result["reason"] == "TARIFF_NOT_APPROVED"
    assert rows(database, "SELECT * FROM billing_accrual") == []


def test_a_cabinet_that_arrived_by_event_is_billed_but_marked_for_onboarding(
        database: Database, stand: dict[str, Any]) -> None:
    """Клапан по образцу раздела 6.5: деньги не теряем, ошибку делаем видимой.

    21 кабинет WB при 4 записях в sellers (раздел 3.6) — так выглядит кабинет,
    заведённый мимо процесса. Отказать в начислении значило бы потерять выручку
    молча; поэтому счёт выставляется, а кабинет помечен.
    """
    result = BillingService(database).ingest(
        event("wms.packing.completed.v1", {"seller_id": "seller-неизвестный"}))

    assert result["outcome"] == "accrued"
    cabinet = rows(database, "SELECT * FROM cabinet WHERE seller_external_id = %s",
                   ("seller-неизвестный",))[0]
    assert cabinet["needs_onboarding"] is True


def test_a_broken_envelope_is_recorded_rather_than_dropped(database: Database) -> None:
    """Событие, которого биллинг не понял, обязано остаться видимым."""
    result = BillingService(database).ingest({"type": "wms.packing.completed.v1", "payload": {}})

    assert result["reason"] == "BAD_ENVELOPE"
    assert len(rows(database, "SELECT * FROM billing_unbilled")) == 1


def test_physical_actions_feed_the_shift_report_even_when_not_billed(
        database: Database, stand: dict[str, Any]) -> None:
    """Скан клиенту не выставляется, но рабочее время стоит (файл 04)."""
    with database.transaction() as cursor:
        cursor.execute(
            "INSERT INTO billing_billable_event (event_type, service, active, comment) "
            "VALUES ('wms.item.scanned.v1', 'picking', false, 'шаг подбора')")

    BillingService(database).ingest(event("wms.item.scanned.v1", {
        "seller_id": "seller-1", "actor_id": "0a000000-0000-4000-8000-00000000000a", "qty": 2}))

    output = rows(database, "SELECT * FROM billing_shift_summary")
    assert output and output[0]["operation"] == "скан у стойки"
    assert output[0]["units"] == Decimal("2.000")


def test_an_accrual_announces_itself_on_the_bus(
        database: Database, stand: dict[str, Any]) -> None:
    """Начисление и его событие пишутся одной транзакцией — outbox, не вызов."""
    BillingService(database).ingest(event("wms.packing.completed.v1", {"seller_id": "seller-1"}))

    outbox = rows(database, "SELECT * FROM billing_outbox")
    assert len(outbox) == 1
    assert outbox[0]["type"] == "billing.accrual.created.v1"
    assert outbox[0]["payload"]["net_amount"] == "30.00"
    assert outbox[0]["published_at"] is None


def test_an_unbilled_event_is_replayed_once_the_tariff_is_approved(
        database: Database, stand: dict[str, Any]) -> None:
    """Причину устранили — событие обязано дойти до счёта.

    Раньше повтор любого события считался дублем доставки и отбрасывался:
    тариф утверждали, справочник правили, а событие оставалось невыставленным
    навсегда. Неоплаченная работа копилась и оставалась неоплаченной.
    """
    with database.transaction() as cursor:
        cursor.execute("UPDATE billing_tariff_version SET approved = false, "
                       "       approved_by = NULL, approved_at = NULL "
                       " WHERE id = %s", (stand["version"],))

    service = BillingService(database)
    packed = event("wms.packing.completed.v1", {"seller_id": "seller-1"})
    first = service.ingest(packed)

    assert first["outcome"] == "unbilled", f"ожидали unbilled, получили {first}"
    assert not rows(database, "SELECT id FROM billing_accrual")
    open_rows = rows(database,
                     "SELECT event_id, reason, resolved_at FROM billing_unbilled")
    assert len(open_rows) == 1 and open_rows[0]["resolved_at"] is None

    # Тариф утверждён — то, ради чего событие и оставляли видимым.
    with database.transaction() as cursor:
        cursor.execute("UPDATE billing_tariff_version SET approved = true, "
                       "       approved_by = 'владелец', approved_at = now() "
                       " WHERE id = %s", (stand["version"],))

    again = service.ingest(packed)

    assert again["outcome"] == "accrued", (
        f"переигранное событие не дошло до счёта: {again}. "
        f"Причину устранили, а работа осталась неоплаченной")
    accruals = rows(database, "SELECT amount FROM billing_accrual")
    assert len(accruals) == 1, f"начислений {len(accruals)}: счёт выставлен дважды"
    assert Decimal(accruals[0]["amount"]) == Decimal("45.00")

    closed = rows(database, "SELECT resolved_at FROM billing_unbilled")
    assert closed[0]["resolved_at"] is not None, (
        "строка осталась в отчёте «не дошло до счёта» после того, как дошла — "
        "в отчёте вперемешку разобранное и неразобранное")

    attempts = rows(database, "SELECT attempts, outcome FROM billing_inbox")
    assert int(attempts[0]["attempts"]) == 2, "попытки не считаются"
    assert attempts[0]["outcome"] == "accrued"


def test_an_accrued_event_is_never_replayed(
        database: Database, stand: dict[str, Any]) -> None:
    """Начисленное не переигрывается: это второй счёт клиенту.

    Парная проверка: открыть переигрывание неначисленного легко так, что
    вместе с ним откроется и переигрывание начисленного.
    """
    service = BillingService(database)
    packed = event("wms.packing.completed.v1", {"seller_id": "seller-1"})
    assert service.ingest(packed)["outcome"] == "accrued"

    again = service.ingest(packed)

    assert again["outcome"] == "duplicate", (
        f"начисленное событие переиграно ({again}): клиенту выставится дважды")
    assert len(rows(database, "SELECT id FROM billing_accrual")) == 1
