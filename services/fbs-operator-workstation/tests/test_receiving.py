"""Приёмка, размещение, инвентаризация.

Общее правило этих экранов: баланс двигается по факту, а не по ожиданию.
Второе правило — ответы читаются по контракту, а расхождение с ним считается,
а не подстраивается молча.
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import FakeWms

from app.receiving import ReceivingRefused, ReceivingService


def run(coro):
    return asyncio.run(coro)


def test_receipt_needs_a_reference_because_it_is_the_idempotency_key(wms: FakeWms):
    service = ReceivingService(wms.client())
    with pytest.raises(ReceivingRefused) as failure:
        run(service.submit_receipt(seller_external_id="seller-1", reference="  ",
                                   lines=[{"barcode": "1", "expected_qty": 1}]))
    assert "идемпотентности" in str(failure.value)


def test_receipt_sends_the_counted_fact_including_zero(wms: FakeWms):
    """Ноль по факту — это «ничего не приехало», а не «строку не заполнили»."""
    wms.on("/receipts", lambda params: {"receipt_id": "r1", "reference": "REF-1",
                                        "owner_external_id": "seller-1", "state": "accepted"})
    service = ReceivingService(wms.client())
    run(service.submit_receipt(seller_external_id="seller-1", reference="REF-1",
                               lines=[{"barcode": "1", "expected_qty": 5, "actual_qty": 0}]))
    line = wms.route_calls("/receipts")[0]["lines"][0]
    assert line["actual_qty"] == 0


def test_new_owner_details_travel_with_the_receipt(wms: FakeWms):
    """Товар уже приехал; держать машину под разгрузкой из-за карточки нельзя."""
    wms.on("/receipts", lambda params: {"receipt_id": "r1", "reference": "REF-1",
                                        "owner_external_id": "seller-9",
                                        "owner_created": True, "state": "accepted"})
    service = ReceivingService(wms.client())
    result = run(service.submit_receipt(
        seller_external_id="seller-9", reference="REF-1",
        lines=[{"barcode": "1", "expected_qty": 1, "actual_qty": 1}],
        seller_name="ИП Новый", seller_inn="0000000000"))
    sent = wms.route_calls("/receipts")[0]
    assert sent["seller_name"] == "ИП Новый" and sent["seller_inn"] == "0000000000"
    assert result["owner_created"] is True


def test_screen_reads_the_contract_shape(wms: FakeWms):
    wms.on("/receipts/screen", lambda params: {
        "receipts": [{"receipt_id": "r1", "reference": "REF-1",
                      "owner_external_id": "seller-1", "state": "counting",
                      "lines": [{"barcode": "1", "expected_qty": 5, "actual_qty": 4}],
                      "discrepancies": [{"kind": "shortage", "barcode": "1", "qty": 1,
                                         "decision": "pending", "liable": "carrier"}]}],
        "generated_at": "2026-09-10T10:00:00+00:00"})
    service = ReceivingService(wms.client())
    screen = run(service.receipts_screen())
    assert screen["receipts"][0]["discrepancies"][0]["liable"] == "carrier"
    assert screen["discrepancy_kinds"] == ["shortage", "surplus", "mismatch", "damage"]


def test_the_mock_shape_is_understood_but_counted(wms: FakeWms):
    """Подстроиться под заглушку молча — значит выучить неправильный формат."""
    from app import metrics
    before = metrics.CONTRACT_FALLBACKS.labels(route="/receipts/screen",
                                               field="receipts")._value.get()
    wms.on("/receipts/screen", lambda params: {
        "open_receipts": [{"receipt_id": "r1", "reference": "REF-1",
                           "seller_external_id": "seller-1", "state": "counting",
                           "lines": [], "discrepancies": []}],
        "cells": []})
    service = ReceivingService(wms.client())
    screen = run(service.receipts_screen())
    assert screen["receipts"][0]["owner_external_id"] == "seller-1"
    after = metrics.CONTRACT_FALLBACKS.labels(route="/receipts/screen",
                                              field="receipts")._value.get()
    assert after > before, "чтение по запасной форме обязано попасть в счётчик"


def test_a_box_without_a_comment_is_refused(wms: FakeWms):
    """Через месяц стоят сотни одинаковых коробок, и без пометки нужную не найти."""
    service = ReceivingService(wms.client())
    with pytest.raises(ReceivingRefused):
        run(service.place_in_box(barcode="BOX-1", seller_external_id="seller-1",
                                 comment="   "))
    assert not wms.calls


def test_box_quantity_zero_is_marked_as_uncounted(wms: FakeWms):
    """quantity = 0 означает «не считали», а не «пусто»."""
    wms.on("/boxes", lambda params: {"box": {"barcode": "BOX-1"}, "created": True})
    service = ReceivingService(wms.client())
    run(service.place_in_box(barcode="BOX-1", seller_external_id="seller-1",
                             comment="верхняя полка у окна", quantity=0, counted=False))
    sent = wms.route_calls("/boxes")[0]
    assert sent["quantity"] == 0 and sent["counted"] is False


def test_inventory_sheet_keeps_the_expected_quantity_separate(wms: FakeWms):
    """Считающий не должен видеть учётную цифру до ввода факта."""
    wms.on("/inventory/sheet", lambda params: {
        "owner_external_id": "seller-1",
        "lines": [{"barcode": "1", "cell_address": "A-01", "expected_qty": 7}],
        "generated_at": "2026-09-10T10:00:00+00:00"})
    service = ReceivingService(wms.client())
    sheet = run(service.inventory_sheet(seller_external_id="seller-1"))
    assert sheet["lines"][0]["expected_qty"] == 7


def test_count_without_a_single_fact_is_refused(wms: FakeWms):
    service = ReceivingService(wms.client())
    with pytest.raises(ReceivingRefused):
        run(service.submit_count(seller_external_id="seller-1", reference="INV-1",
                                 scope="partial",
                                 lines=[{"barcode": "1", "expected_qty": 5}]))


def test_handover_requires_a_human_signature(wms: FakeWms):
    """HANDED_TO_WB ставит человек: статус WB complete приёмку не доказывает."""
    service = ReceivingService(wms.client())
    with pytest.raises(ReceivingRefused) as failure:
        run(service.shipment(seller_external_id="seller-1", action="hand_over",
                             handed_over_by="  "))
    assert "человеком" in str(failure.value)
    assert not wms.calls


def test_handover_with_a_signature_reaches_the_service(wms: FakeWms):
    wms.on("/shipments", lambda params: {"shipment_id": "s1", "owner_external_id": "seller-1",
                                         "state": "handed_to_wb", "handed_by": "Иванов",
                                         "handed_at": "2026-09-10T18:00:00+00:00",
                                         "orders": 12})
    service = ReceivingService(wms.client())
    result = run(service.shipment(seller_external_id="seller-1", action="hand_over",
                                  wb_supply_id="WB-GI-1", handed_over_by="Иванов"))
    assert result["handed_by"] == "Иванов" and result["state"] == "handed_to_wb"
    assert wms.route_calls("/shipments")[0]["handed_over_by"] == "Иванов"


def test_supply_never_carries_more_than_a_hundred_orders(wms: FakeWms):
    """Ограничение Wildberries из приложения D: до 100 заданий за вызов."""
    wms.on("/shipments", lambda params: {"shipment_id": "s1",
                                         "owner_external_id": "seller-1", "state": "open"})
    service = ReceivingService(wms.client())
    run(service.shipment(seller_external_id="seller-1", action="add_orders",
                         task_ids=[str(index) for index in range(150)]))
    assert len(wms.route_calls("/shipments")[0]["task_ids"]) == 100
