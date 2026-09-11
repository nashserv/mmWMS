"""Что тарифицируется — и шлёт ли это кто-нибудь.

Список тарифицируемых событий — данные, а не код: строки в
`billing_billable_event`, которые приезжают сидом. Поэтому ошибка в нём не
ловится ни одним тестом логики: код исправен, счёт не выставляется.

Ровно так упаковка — самая частая операция склада — шла клиенту бесплатно:
включён был `order.packed.v1`, который эмитило старое рабочее место, а новый
`wms` эмитит `wms.packing.completed.v1`, и он был выключен.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

SEED = pathlib.Path(__file__).resolve().parents[1] / "seed" / "stand-billing.sql"
WMS_EVENTS = (pathlib.Path(__file__).resolve().parents[3]
              / "services" / "wms" / "contracts" / "asyncapi.yaml")

# Строка сида: ('тип', 'услуга', quantity_path, active, 'комментарий')
_ROW = re.compile(
    r"\(\s*'(?P<event>[a-z0-9._-]+)'\s*,\s*'(?P<service>[a-z_]+)'\s*,\s*"
    r"(?P<quantity>NULL|'[^']*')\s*,\s*(?P<active>true|false)\s*,", re.I)


def _seeded() -> list[dict[str, object]]:
    """Строки `billing_billable_event` из сида — то, что реально поедет."""
    if not SEED.is_file():
        pytest.skip(f"сид биллинга не найден по {SEED}")
    text = SEED.read_text(encoding="utf-8")
    start = text.index("INSERT INTO billing_billable_event")
    block = text[start:text.index("ON CONFLICT (event_type)", start)]
    rows = [{"event_type": match.group("event"),
             "service": match.group("service"),
             "quantity_path": None if match.group("quantity").upper() == "NULL"
                              else match.group("quantity").strip("'"),
             "active": match.group("active").lower() == "true"}
            for match in _ROW.finditer(block)]
    assert rows, "из сида не прочиталось ни одной строки тарифицируемых событий"
    return rows


def _emitted() -> set[str]:
    """Типы событий, которые `wms` объявляет в своём каталоге."""
    if not WMS_EVENTS.is_file():
        pytest.skip(f"каталог событий wms не найден по {WMS_EVENTS}")
    with WMS_EVENTS.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    names: set[str] = set()
    for channel in (document.get("channels") or {}).values():
        if channel.get("address"):
            names.add(str(channel["address"]))
        for message in (channel.get("messages") or {}).values():
            for key in ("name", "title"):
                if message.get(key):
                    names.add(str(message[key]))
    for message in ((document.get("components") or {}).get("messages") or {}).values():
        for key in ("name", "title"):
            if message.get(key):
                names.add(str(message[key]))
    assert names, "из каталога событий не прочитано ни одного типа"
    return names


def test_every_billed_event_type_is_one_that_someone_actually_emits() -> None:
    """Включённый тип, который никто не шлёт, — это неоплаченная работа.

    Проверка статическая, по сиду и каталогу: обе стороны — данные, и
    разойтись они могут молча.
    """
    emitted = _emitted()
    # Событий не из `wms` в списке тоже хватает: приложение E мастера числит
    # среди тарифицируемых и события Wildberries, и события склада.
    outside_wms = {"wb.supply.shipped.v1", "wb.orders.processed.v1",
                   "inventory.movement.recorded.v1"}
    known = emitted | outside_wms
    # Унаследованные имена боевого контура: новый `wms` их не эмитит, и
    # включёнными им быть нельзя — но в списке они лежат выключенными, чтобы
    # переключение было видно.
    legacy = {"order.packed.v1", "label.printed.v1"}

    unknown = [row["event_type"] for row in _seeded()
               if row["active"] and row["event_type"] not in known]
    assert not unknown, (
        f"тарифицируются события, которых никто не шлёт: {unknown}. "
        f"Работа идёт, счёт не выставляется, и по коду это не видно")

    dead = [row["event_type"] for row in _seeded()
            if row["active"] and row["event_type"] in legacy]
    assert not dead, (
        f"включены события боевого контура: {dead}. Новый wms их не эмитит — "
        f"эта услуга клиенту не выставится")


def test_packing_has_exactly_one_billed_source() -> None:
    """Две включённые строки на одну упаковку — двойной счёт клиенту.

    Парная проверка к предыдущей: включить новое событие, забыв выключить
    старое, так же плохо, как не включить вовсе.
    """
    packing = [row["event_type"] for row in _seeded()
               if row["active"] and row["service"] == "packing"]
    assert packing == ["wms.packing.completed.v1"], (
        f"источников упаковки {packing}: либо клиенту выставится дважды, "
        f"либо не выставится вовсе")


def test_receiving_is_billed_by_the_document_not_by_the_line() -> None:
    """Приёмку тарифицирует одно событие на документ.

    `inventory.movement.recorded.v1` сопровождает почти всякое движение: по
    нему счёт вышел бы на каждую строку накладной.
    """
    receiving = {row["event_type"]: row for row in _seeded()
                 if row["service"] == "receiving"}
    assert receiving["wms.receipt.completed.v1"]["active"] is True
    assert receiving["wms.receipt.completed.v1"]["quantity_path"] == "accepted_qty", (
        "количество приёмки берётся не из accepted_qty: приёмка с недостачей "
        "оплачивается тем, что реально легло на полку")
    assert receiving["inventory.movement.recorded.v1"]["active"] is False, (
        "движение товара тарифицируется: счёт выйдет на каждую строку накладной")


def test_the_shift_report_counts_receiving_too() -> None:
    """Приёмка — операция смены, и её выработку считают так же, как подбор."""
    from app.service import SHIFT_OPERATIONS

    assert "wms.receipt.completed.v1" in SHIFT_OPERATIONS, (
        "приёмщик в отчёте смены выглядит бездельником: его работа не считается")
