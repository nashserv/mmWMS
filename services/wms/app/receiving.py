"""Приёмка, размещение и инвентаризация.

Пять приёмщиков работают параллельно (раздел 4), поэтому приёмка идёт
документом с идемпотентным `reference`, а не «просто добавить на склад».

Главное правило здесь одно: **баланс двигается по факту пересчёта, а не по
ожиданию**. Разница оформляется типизированным расхождением, а не подгонкой
числа. Подгонка — это и есть та молчаливая ложь учёта, из-за которой на 21
продавца в боевом контуре осталось 92 единицы (раздел 3.1).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from . import repositories as repo
from .postgres import ConnectionPool, single, transaction

log = logging.getLogger("wms.receiving")

# Виды расхождений приёмки (раздел 7). `ledger_short` сюда не относится —
# это клапан отгрузки, а не приёмки.
SHORTAGE = "shortage"
SURPLUS = "surplus"


class ReceivingOperations:
    def __init__(self, pool: ConnectionPool,
                 on_stock_changed: Callable[[uuid.UUID, set[uuid.UUID]], None] | None = None
                 ) -> None:
        self._pool = pool
        self._on_stock_changed = on_stock_changed

    # ------------------------------------------------------------- приёмка

    def receive(self, params: dict[str, Any]) -> dict[str, Any]:
        """Принять товар документом.

        Владельца, которого склад видит впервые, заводит по `seller_name` и
        `seller_inn` (приложение C): товар уже приехал, и держать машину под
        разгрузкой из-за незаведённой карточки нельзя.
        """
        seller = str(params.get("seller_external_id") or "").strip()
        reference = str(params.get("reference") or "").strip()
        warehouse_code = str(params.get("warehouse_code") or "RUM").strip() or "RUM"
        lines = params.get("lines") or []
        if not seller or not reference or not lines:
            raise ValueError("seller_external_id, reference и lines обязательны")

        actor = _uuid_or_none(params.get("actor_id"))
        touched: set[uuid.UUID] = set()
        owner_id: uuid.UUID | None = None

        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                existing = repo.receipt_by_reference(cursor, reference)
                if existing is not None:
                    # Повтор приёмки ничего не добавляет: тот же ответ, а не
                    # второй приход товара (инвариант 5).
                    return self._existing_receipt(cursor, existing, seller)

                owner, owner_created = repo.upsert_owner(
                    cursor, seller, name=_text(params.get("seller_name")),
                    inn=_text(params.get("seller_inn")))
                owner_id = owner["id"]
                warehouse = repo.ensure_warehouse(cursor, warehouse_code)
                receipt = repo.insert_receipt(
                    cursor, owner_id=owner["id"], reference=reference,
                    warehouse_id=warehouse["id"], actor_id=actor)

                accepted, counted_all, discrepancies = 0, True, []
                for index, line in enumerate(lines):
                    barcode = str(line.get("barcode") or "").strip()
                    if not barcode:
                        continue
                    sku, _ = repo.upsert_sku(
                        cursor, owner["id"], barcode,
                        seller_sku=_text(line.get("seller_sku")),
                        name=_text(line.get("name")))
                    cell = repo.ensure_cell(
                        cursor, _text(line.get("cell_address")) or f"{warehouse_code}-INBOUND",
                        warehouse_code=warehouse_code)
                    box = None
                    if _text(line.get("box_barcode")):
                        box = repo.ensure_box(
                            cursor, str(line["box_barcode"]).strip(), owner_id=owner["id"],
                            sku_id=sku["id"], cell_id=cell["id"],
                            comment=_text(line.get("comment"))
                            or f"приёмка {reference}: {barcode}",
                            created_by=actor)

                    expected = _int_or_none(line.get("expected_qty"))
                    actual = _int_or_none(line.get("actual_qty"))
                    repo.insert_receipt_line(
                        cursor, receipt_id=receipt["id"], sku_id=sku["id"],
                        expected_qty=expected, actual_qty=actual,
                        box_id=box["id"] if box else None, cell_id=cell["id"])

                    if actual is None:
                        # Строка объявлена, но не пересчитана. Двигать баланс
                        # по ожиданию нельзя — это и есть подгонка (раздел 3.1).
                        counted_all = False
                        continue

                    if actual > 0:
                        repo.insert_move(
                            cursor, owner_id=owner["id"], sku_id=sku["id"], qty=actual,
                            cell_to=cell["id"], box_to=box["id"] if box else None,
                            state_to="good", reason="receipt", doc_type="receipt",
                            doc_ref=reference, actor_id=actor,
                            idem_key=f"receipt:{reference}:{index}")
                        touched.add(sku["id"])
                    accepted += 1

                    if expected is not None and actual != expected:
                        kind = SHORTAGE if actual < expected else SURPLUS
                        created = repo.insert_discrepancy(
                            cursor, owner_id=owner["id"], sku_id=sku["id"], kind=kind,
                            qty=abs(expected - actual), receipt_id=receipt["id"],
                            cell_id=cell["id"], actor_id=actor,
                            comment=f"ожидали {expected}, пересчитали {actual}")
                        discrepancies.append({
                            "discrepancy_id": str(created["id"]), "kind": kind,
                            "barcode": barcode, "qty": int(created["qty"]),
                            "decision": created["decision"],
                            "cell_address": cell["address"],
                            "created_at": _isoformat(created["created_at"])})

                state = "accepted" if counted_all else "counting"
                repo.set_receipt_state(cursor, receipt["id"], state)
                result = {
                    "receipt_id": str(receipt["id"]), "reference": reference,
                    "owner_external_id": seller, "owner_created": owner_created,
                    "state": state, "lines_accepted": accepted,
                    "discrepancies": discrepancies, "duplicate": False}

        # Транзакция закрыта — остаток можно публиковать (раздел 6.4).
        if owner_id is not None:
            self._announce(owner_id, touched)
        return result

    def _existing_receipt(self, cursor: Any, receipt: dict[str, Any],
                          seller: str) -> dict[str, Any]:
        rows = repo.discrepancies_of_receipt(cursor, receipt["id"])
        return {
            "receipt_id": str(receipt["id"]), "reference": receipt["reference"],
            "owner_external_id": seller, "owner_created": False,
            "state": receipt["state"],
            "lines_accepted": repo.receipt_line_count(cursor, receipt["id"]),
            "discrepancies": [{
                "discrepancy_id": str(row["id"]), "kind": row["kind"],
                "barcode": row["barcode"], "qty": int(row["qty"]),
                "decision": row["decision"], "cell_address": row["cell_address"],
                "created_at": _isoformat(row["created_at"])} for row in rows],
            "duplicate": True}

    def screen(self, params: dict[str, Any]) -> dict[str, Any]:
        """Данные экрана приёмки: работа, а не архив."""
        states = params.get("states") or ["draft", "counting"]
        limit = min(int(params.get("limit") or 50), 500)
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                rows = repo.receipts_for_screen(
                    cursor, states=states, owner_external_id=params.get("owner_external_id"),
                    reference=_text(params.get("reference")), limit=limit)
        return {"receipts": rows}

    # ---------------------------------------------------------- размещение

    def putaway_screen(self, params: dict[str, Any]) -> dict[str, Any]:
        """Что принято, но ещё не разложено по местам хранения."""
        limit = min(int(params.get("limit") or 100), 500)
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                rows = repo.putaway_queue(
                    cursor, owner_external_id=params.get("owner_external_id"), limit=limit)
        return {"placements": rows}

    def move_box(self, params: dict[str, Any]) -> dict[str, Any]:
        """Переставить коробку в другую ячейку.

        Товар едет вместе с коробкой: движение пишется на каждую позицию, а не
        «коробка переехала, а остаток остался числиться в старой ячейке».
        """
        barcode = str(params.get("box_barcode") or "").strip()
        address = str(params.get("cell_address") or "").strip()
        if not barcode or not address:
            raise ValueError("box_barcode и cell_address обязательны")
        actor = _uuid_or_none(params.get("actor_id"))
        reference = _text(params.get("reference")) or f"putaway:{barcode}:{address}"

        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                box = repo.find_box(cursor, barcode)
                if box is None:
                    raise ValueError(f"коробка {barcode!r} не заведена")
                target = repo.ensure_cell(cursor, address)
                moved = repo.move_box_contents(
                    cursor, box=box, target_cell=target["id"], reference=reference,
                    actor_id=actor)
                repo.place_box(cursor, box["id"], target["id"])
        return {"box_barcode": barcode, "cell_address": address, "moves": moved}

    # ------------------------------------------------------ инвентаризация

    def sheet(self, params: dict[str, Any]) -> dict[str, Any]:
        """Лист инвентаризации: что учёт думает про эти места."""
        seller = str(params.get("seller_external_id") or "").strip()
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                rows = repo.inventory_sheet(
                    cursor, seller_external_id=seller,
                    cell_address=_text(params.get("cell_address")),
                    barcodes=params.get("barcodes"))
        return {"owner_external_id": seller, "lines": rows}

    def count(self, params: dict[str, Any]) -> dict[str, Any]:
        """Применить пересчёт: излишки приходуются, недостачи списываются.

        Каждое расхождение — движение с `doc_type='count'`… точнее, с
        `'inventory'`: список типов документов закрыт миграцией 004, и
        выдумывать новый значит разойтись со схемой.
        """
        seller = str(params.get("seller_external_id") or "").strip()
        reference = str(params.get("reference") or "").strip()
        scope = str(params.get("scope") or "partial").strip()
        lines = params.get("lines") or []
        if not seller or not reference or not lines:
            raise ValueError("seller_external_id, reference и lines обязательны")
        if scope not in ("partial", "full"):
            raise ValueError("scope: partial или full")

        actor = _uuid_or_none(params.get("actor_id"))
        touched: set[uuid.UUID] = set()
        owner_id: uuid.UUID | None = None
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                existing = repo.inventory_by_reference(cursor, reference)
                if existing is not None:
                    return {"count_id": str(existing["id"]), "reference": reference,
                            "owner_external_id": seller, "state": existing["state"],
                            "adjustments": [], "duplicate": True}

                owner = repo.find_owner(cursor, seller)
                if owner is None:
                    raise ValueError(f"продавец {seller!r} не заведён")
                owner_id = owner["id"]
                count = repo.insert_inventory_count(
                    cursor, owner_id=owner["id"], reference=reference, scope=scope,
                    actor_id=actor)

                adjustments = []
                for index, line in enumerate(lines):
                    barcode = str(line.get("barcode") or "").strip()
                    fact = _int_or_none(line.get("fact_qty"))
                    if not barcode or fact is None:
                        continue
                    sku, error = repo.find_sku(cursor, owner["id"], barcode=barcode,
                                               sku_field=None)
                    if sku is None:
                        raise ValueError(f"товар {barcode!r}: {error.value if error else ''}")
                    cell = repo.ensure_cell(
                        cursor, _text(line.get("cell_address")) or "RUM-INBOUND")
                    box = repo.find_box(cursor, str(line["box_barcode"]).strip()) \
                        if _text(line.get("box_barcode")) else None

                    expected = repo.balance_at(
                        cursor, owner_id=owner["id"], sku_id=sku["id"], cell_id=cell["id"],
                        box_id=box["id"] if box else None, state="good")
                    repo.insert_inventory_line(
                        cursor, count_id=count["id"], sku_id=sku["id"], cell_id=cell["id"],
                        box_id=box["id"] if box else None, expected_qty=expected,
                        fact_qty=fact)
                    if fact == expected:
                        continue

                    delta = fact - expected
                    # Излишек приходуется, недостача списывается — и то и другое
                    # движением, а не правкой числа (инвариант 3).
                    repo.insert_move(
                        cursor, owner_id=owner["id"], sku_id=sku["id"], qty=abs(delta),
                        cell_to=cell["id"] if delta > 0 else None,
                        box_to=box["id"] if (box and delta > 0) else None,
                        state_to="good" if delta > 0 else None,
                        cell_from=cell["id"] if delta < 0 else None,
                        box_from=box["id"] if (box and delta < 0) else None,
                        state_from="good" if delta < 0 else None,
                        reason="inventory", doc_type="inventory", doc_ref=reference,
                        actor_id=actor, idem_key=f"inventory:{reference}:{index}")
                    touched.add(sku["id"])
                    adjustments.append({
                        "barcode": barcode, "cell_address": cell["address"],
                        "expected_qty": expected, "fact_qty": fact, "delta": delta})

                repo.apply_inventory_count(cursor, count["id"])
                result = {"count_id": str(count["id"]), "reference": reference,
                          "owner_external_id": seller, "state": "applied",
                          "adjustments": adjustments, "duplicate": False}

        if owner_id is not None:
            self._announce(owner_id, touched)
        return result

    # ------------------------------------------------------------- коробки

    def create_box(self, params: dict[str, Any]) -> dict[str, Any]:
        """Завести коробку под товар владельца.

        Комментарий обязателен: через месяц на складе стоят сотни одинаковых
        коробок, и без пометки нужную не найти (раздел 2.9). Пустой отбивается
        и здесь, и ограничением схемы — дисциплине это оставлять нельзя.
        """
        barcode = str(params.get("box_barcode") or params.get("barcode") or "").strip()
        seller = str(params.get("seller_external_id") or "").strip()
        comment = _text(params.get("comment"))
        if not barcode or not seller:
            raise ValueError("box_barcode и seller_external_id обязательны")
        if not comment:
            raise ValueError(
                "comment обязателен: через месяц на складе сотни одинаковых коробок")

        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                owner = repo.find_owner(cursor, seller)
                if owner is None:
                    raise ValueError(f"продавец {seller!r} не заведён")
                sku = None
                if _text(params.get("barcode")) and _text(params.get("barcode")) != barcode:
                    sku, _error = repo.find_sku(cursor, owner["id"],
                                                barcode=_text(params.get("barcode")),
                                                sku_field=None)
                cell = None
                if _text(params.get("cell_address")):
                    cell = repo.ensure_cell(cursor, str(params["cell_address"]).strip())
                box = repo.ensure_box(
                    cursor, barcode, owner_id=owner["id"],
                    sku_id=sku["id"] if sku else None,
                    cell_id=cell["id"] if cell else None, comment=comment,
                    created_by=_uuid_or_none(params.get("actor_id")))
        return {"box_barcode": box["barcode"], "owner_external_id": seller,
                "cell_address": params.get("cell_address"), "comment": box["comment"],
                "state": box["state"]}

    def list_boxes(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                rows = repo.boxes_of(
                    cursor, owner_external_id=_text(params.get("seller_external_id")),
                    cell_address=_text(params.get("cell_address")),
                    barcode=_text(params.get("box_barcode")),
                    limit=min(int(params.get("limit") or 200), 500))
        return {"boxes": [{
            "box_barcode": row["barcode"], "owner_external_id": row["seller_external_id"],
            "cell_address": row["cell_address"], "barcode": row["sku_barcode"],
            "quantity": int(row["quantity"]), "counted": row["counted"],
            "comment": row["comment"], "sequence": row["sequence"],
            "total_boxes": row["total_boxes"], "state": row["state"],
            "created_at": _isoformat(row["created_at"])} for row in rows]}

    def remove_box(self, params: dict[str, Any]) -> dict[str, Any]:
        """Убрать коробку со склада. Остаток в ней должен быть нулевым.

        Коробка с товаром, помеченная убранной, — это потерянный остаток:
        по учёту он лежит там, где коробки уже нет.
        """
        barcode = str(params.get("box_barcode") or "").strip()
        if not barcode:
            raise ValueError("box_barcode обязателен")
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                box = repo.find_box(cursor, barcode)
                if box is None:
                    raise ValueError(f"коробка {barcode!r} не заведена")
                left = repo.box_contents(cursor, box["id"])
                if left:
                    raise ValueError(
                        f"в коробке {barcode!r} ещё лежит товар ({len(left)} позиций): "
                        f"сначала переставьте или спишите его")
                removed = repo.remove_box(cursor, barcode)
        return {"box_barcode": barcode, "state": "removed", "duplicate": not removed}

    # ------------------------------------------------------------- хранение

    def lookup(self, params: dict[str, Any]) -> dict[str, Any]:
        """Найти, где лежит товар или что лежит в ячейке."""
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                rows = repo.storage_lookup(
                    cursor, owner_external_id=_text(params.get("seller_external_id")),
                    barcode=_text(params.get("barcode")),
                    cell_address=_text(params.get("cell_address")),
                    box_barcode=_text(params.get("box_barcode")),
                    limit=min(int(params.get("limit") or 200), 500))
        return {"rows": rows}

    def count_cell(self, params: dict[str, Any]) -> dict[str, Any]:
        """Пересчёт одной ячейки — частный случай инвентаризации.

        Отдельный маршрут потому, что у стойки пересчитывают одну ячейку, а не
        затевают документ: результат всё равно уезжает движениями с
        `doc_type='inventory'`.
        """
        address = str(params.get("cell_address") or "").strip()
        if not address:
            raise ValueError("cell_address обязателен")
        lines = [dict(line, cell_address=address) for line in (params.get("lines") or [])]
        if not lines:
            raise ValueError("lines обязательны: пересчёт без факта — это не пересчёт")
        return self.count({
            "seller_external_id": params.get("seller_external_id"),
            "reference": params.get("reference") or f"cell-count:{address}",
            "scope": "partial", "lines": lines,
            "actor_id": params.get("actor_id")})

    # ------------------------------------------------------------ служебное

    def _announce(self, owner_id: uuid.UUID, sku_ids: set[uuid.UUID]) -> None:
        if self._on_stock_changed is None or not sku_ids:
            return
        try:
            self._on_stock_changed(owner_id, sku_ids)
        except Exception:
            # Товар уже принят и записан в журнал. Непрошедшая публикация —
            # повод для метрики, а не для отката приёмки.
            log.warning("остаток принят, но не опубликован", exc_info=False)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, AttributeError):
        return None


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
