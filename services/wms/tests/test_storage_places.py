"""Хранение считается от объёма товара, а не от числа занятых коробов.

Решение владельца 12.09.2026 (раздел 13). Единица тарифа прежняя — коробо-место
× сутки, — но количество вычисляется:

    коробо-места = Σ по штрихкодам ( остаток_единиц / норма_единиц_в_коробе )

Норма снимается с приёмки: кладовщик разложил тысячу футболок по коробам —
склад записал, сколько легло в каждый. В этом числе уже сидит и объём вещи, и
плотность укладки, и форма короба, и оно ИЗМЕРЕНО, а не выведено из габаритов,
которых у нас нет ни одного: `dims_mm` пуст у всех записей, в карточке
Wildberries габаритов тоже нет.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app import repositories as repo
from app.postgres import ConnectionPool, single, transaction
from app.receiving import ReceivingOperations
from app.service import CatalogOperations

from dbfixtures import require_database, unique


@pytest.fixture(scope="module")
def pool() -> ConnectionPool:
    connections = ConnectionPool(require_database(), max_size=8)
    yield connections
    connections.close()


@pytest.fixture()
def client(pool: ConnectionPool) -> dict:
    catalog = CatalogOperations(pool)
    seller = unique("seller")
    catalog.upsert_owner({"seller_external_id": seller, "name": "Клиент хранения"})
    return {"seller": seller, "cell": unique("cell").upper()}


def _barcode() -> str:
    return f"46{uuid.uuid4().int % 10**11:011d}"


def _receive(pool: ConnectionPool, client: dict, *, barcode: str,
             box_barcode: str | None, qty: int) -> None:
    """Приёмка одной строки через настоящий маршрут приёмки.

    Именно она снимает норму — проверять надо тот путь, которым товар приезжает
    на склад, а не прямой вызов репозитория.
    """
    ReceivingOperations(pool).receive({
        "seller_external_id": client["seller"],
        "reference": unique("receipt"),
        "warehouse_code": "ST",
        "lines": [{"barcode": barcode, "box_barcode": box_barcode,
                   "cell_address": client["cell"],
                   "expected_qty": qty, "actual_qty": qty}],
    })


def _places(pool: ConnectionPool, client: dict, day: date | None = None) -> dict:
    return ReceivingOperations(pool).places({
        "seller_external_id": client["seller"],
        "day": (day or date.today()).isoformat()})


def _norm(pool: ConnectionPool, client: dict, barcode: str) -> int | None:
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(
                "SELECT s.units_per_box FROM sku s JOIN owner o ON o.id = s.owner_id "
                " WHERE o.seller_external_id = %s AND s.barcode = %s",
                (client["seller"], barcode))
            row = cursor.fetchone()
    return row["units_per_box"] if row else None


def test_the_norm_is_taken_from_how_the_goods_actually_landed(
        pool: ConnectionPool, client: dict) -> None:
    """Норма берётся с приёмки, а не из габаритов, которых у нас нет."""
    barcode = _barcode()
    _receive(pool, client, barcode=barcode, box_barcode=unique("BOX"), qty=44)

    assert _norm(pool, client, barcode) == 44


def test_a_half_empty_box_does_not_lower_a_norm_taken_from_a_full_one(
        pool: ConnectionPool, client: dict) -> None:
    """Норма — верхняя планка наблюдённого, а не последнее значение.

    Иначе короб, принятый наполовину, опустит норму вдвое, и хранение станет
    вдвое дороже — на ровном месте, без единого изменения в товаре.
    """
    barcode = _barcode()
    _receive(pool, client, barcode=barcode, box_barcode=unique("FULL"), qty=44)
    _receive(pool, client, barcode=barcode, box_barcode=unique("HALF"), qty=12)

    assert _norm(pool, client, barcode) == 44, (
        "полупустой короб опустил норму, снятую с полного")


def test_places_do_not_depend_on_how_the_goods_are_spread_across_boxes(
        pool: ConnectionPool, client: dict) -> None:
    """То, ради чего всё затевалось.

    Двести пятьдесят футболок при норме 44 — это 5,68 коробо-места, лежат они в
    шести коробах или в двадцати трёх. Отбор понемногу из каждого короба счёт не
    меняет: считается объём товара, а не число занятых мест.
    """
    barcode = _barcode()
    # Плотная приёмка задаёт норму и кладёт 250 штук в шесть коробов.
    for _ in range(5):
        _receive(pool, client, barcode=barcode, box_barcode=unique("TIGHT"), qty=44)
    _receive(pool, client, barcode=barcode, box_barcode=unique("TIGHT"), qty=30)
    tight = _places(pool, client)

    # Тот же товар, то же количество, размазанное по двадцати трём коробам.
    other = {"seller": client["seller"], "cell": unique("cell").upper()}
    spread = _barcode()
    for index in range(23):
        _receive(pool, other, barcode=spread, box_barcode=unique("THIN"),
                 qty=11 if index else 8)
    with pool.connection() as connection:
        with transaction(connection) as cursor:
            cursor.execute(
                "UPDATE sku SET units_per_box = 44 "
                "  FROM owner o WHERE o.id = sku.owner_id "
                "   AND o.seller_external_id = %s AND sku.barcode = %s",
                (client["seller"], spread))

    both = _places(pool, client)
    # Оба товара по 250 штук при норме 44 — вместе ровно вдвое больше одного.
    assert tight["places"] == pytest.approx(5.68, abs=0.02)
    assert both["places"] == pytest.approx(11.36, abs=0.04), (
        f"размазанный по коробам товар посчитан иначе, чем плотный: "
        f"вместе {both['places']}, плотный один {tight['places']}")


def test_goods_without_a_norm_are_not_counted_but_are_reported(
        pool: ConnectionPool, client: dict) -> None:
    """Придумать количество и поставить его в счёт хуже, чем сказать «не знаю».

    Товар без нормы не попадает в число мест и возвращается отдельно: биллинг
    заведёт строку «не дошло до счёта», человек поправит норму, начисление
    переиграется (находка 4.3).
    """
    _receive(pool, client, barcode=_barcode(), box_barcode=unique("KNOWN"), qty=20)
    # Товар лёг россыпью в ячейку, короба не было — нормы взять неоткуда.
    _receive(pool, client, barcode=_barcode(), box_barcode=None, qty=500)

    counted = _places(pool, client)

    assert counted["places"] == pytest.approx(1.0, abs=0.02), (
        "товар без нормы попал в счёт по выдуманному количеству")
    assert counted["skus_without_norm"] == 1
    assert counted["units_without_norm"] == 500


def test_yesterday_is_counted_by_yesterdays_stock(
        pool: ConnectionPool, client: dict) -> None:
    """Досчёт за пропущенный день берёт остаток ТОГО дня.

    До фикса склад спрашивали без дня вовсе, и досчёт за три дня трижды брал
    сегодняшний остаток: клиенту начислялось хранение за дни, когда товара ещё
    не было.
    """
    _receive(pool, client, barcode=_barcode(), box_barcode=unique("TODAY"), qty=44)

    assert _places(pool, client)["places"] == pytest.approx(1.0, abs=0.02)
    assert _places(pool, client, date.today() - timedelta(days=2))["places"] == 0.0, (
        "хранение начислено за день, когда товара на складе ещё не было")


def test_a_day_is_required(pool: ConnectionPool, client: dict) -> None:
    """Спросить «сколько мест» без дня нельзя.

    Умолчание «сегодня» — это ровно тот молчаливый ответ, из-за которого
    досчёт за прошлые сутки считался по сегодняшнему остатку.
    """
    with pytest.raises(ValueError, match="day"):
        ReceivingOperations(pool).places({"seller_external_id": client["seller"]})


def test_every_state_on_the_shelf_is_counted(
        pool: ConnectionPool, client: dict) -> None:
    """Брак и карантин занимают полку так же, как годный товар.

    Не считать их — значит держать чужой неликвид бесплатно (допущение
    интегратора 12.09.2026, раздел 13).
    """
    barcode = _barcode()
    _receive(pool, client, barcode=barcode, box_barcode=unique("DEF"), qty=44)

    with pool.connection() as connection:
        with transaction(connection) as cursor:
            cursor.execute(
                "SELECT o.id AS owner_id, s.id AS sku_id, b.cell_id "
                "  FROM sku s JOIN owner o ON o.id = s.owner_id "
                "  JOIN stock_balance b ON b.sku_id = s.id "
                " WHERE o.seller_external_id = %s AND s.barcode = %s LIMIT 1",
                (client["seller"], barcode))
            row = cursor.fetchone()
            # Годный переводится в брак: с полки он никуда не делся.
            repo.insert_move(
                cursor, owner_id=row["owner_id"], sku_id=row["sku_id"], qty=44,
                cell_from=row["cell_id"], cell_to=row["cell_id"],
                state_from="good", state_to="defect", reason="разбор",
                doc_type="adjustment", doc_ref=unique("adj"), actor_id=None,
                idem_key=f"storage-test:{uuid.uuid4()}")

    assert _places(pool, client)["places"] == pytest.approx(1.0, abs=0.02), (
        "товар в браке перестал считаться — клиент хранит неликвид бесплатно")


def test_a_norm_can_be_corrected_by_hand(
        pool: ConnectionPool, client: dict) -> None:
    """Норма, снятая с пробной партии, завышает счёт вчетверо — её правят руками.

    Товар, приходящий по десять штук, получит норму 10 и будет стоить клиенту
    целое место за десять футболок, хотя их влезает сорок четыре.
    """
    barcode = _barcode()
    _receive(pool, client, barcode=barcode, box_barcode=unique("TRY"), qty=10)
    before = _places(pool, client)["places"]

    ReceivingOperations(pool).set_units_per_box({
        "seller_external_id": client["seller"], "barcode": barcode,
        "units_per_box": 44})
    after = _places(pool, client)["places"]

    assert before == pytest.approx(1.0, abs=0.02)
    assert after == pytest.approx(0.23, abs=0.02), (
        "правка нормы не изменила счёт — маршрут правки бесполезен")


def test_a_norm_of_zero_is_refused(pool: ConnectionPool, client: dict) -> None:
    """На норму делят. Ноль превратил бы счёт в отказ базы посреди начисления."""
    barcode = _barcode()
    _receive(pool, client, barcode=barcode, box_barcode=unique("Z"), qty=5)

    with pytest.raises(ValueError, match="больше нуля"):
        ReceivingOperations(pool).set_units_per_box({
            "seller_external_id": client["seller"], "barcode": barcode,
            "units_per_box": 0})
