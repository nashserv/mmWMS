"""Фикстуры mock-сервиса.

Данные подобраны так, чтобы поток B и поток C могли пройти по своим сценариям
целиком, не дожидаясь потока A, и чтобы каждый нетривиальный случай раздела 3.2
имел свой штрихкод.

Идентификаторы — настоящие UUID, а не читаемые строки вида "owner-seller-a".
Контракт событий требует формат uuid, и mock обязан отдавать то, подо что
потоки B и C пишут консьюмеры: заглушка, нарушающая контракт, хуже отсутствия
заглушки — она учит клиентов неправильному формату.
"""
from __future__ import annotations

from typing import Any

WAREHOUSE_CODE = "RUM"
WAREHOUSE_ID = "d0000000-0000-4000-8000-000000000001"

SELLERS: list[dict[str, Any]] = [
    {"owner_id": "a0000000-0000-4000-8000-000000000001",
     "seller_external_id": "seller-a", "name": "ОсОО «Акей»", "inn": "00000000000001",
     "allow_ledger_short": True},
    {"owner_id": "a0000000-0000-4000-8000-000000000002",
     "seller_external_id": "seller-b", "name": "ИП Эминов", "inn": "00000000000002",
     "allow_ledger_short": False},
]

# Штрихкод определяет вещь на полке, артикул — только модель (раздел 3.2).
PRODUCTS: list[dict[str, Any]] = [
    {"sku_id": "b0000000-0000-4000-8000-000000000001",
     "seller_external_id": "seller-a", "barcode": "2000000000011",
     "seller_sku": "AKEY-JACKET-M", "name": "Куртка утеплённая, M", "available": 42},
    {"sku_id": "b0000000-0000-4000-8000-000000000002",
     "seller_external_id": "seller-a", "barcode": "2000000000028",
     "seller_sku": "AKEY-JACKET-L", "name": "Куртка утеплённая, L", "available": 17},
    {"sku_id": "b0000000-0000-4000-8000-000000000003",
     "seller_external_id": "seller-a", "barcode": "2000000000035",
     "seller_sku": "AKEY-BOOTS-39", "name": "Ботинки, 39", "available": 0},
    {"sku_id": "b0000000-0000-4000-8000-000000000004",
     "seller_external_id": "seller-b", "barcode": "2000000000042",
     "seller_sku": "EMIN-DRESS-S", "name": "Платье, S", "available": 8},
]

# Один штрихкод на две карточки: у одной карточки несколько размеров, и WB
# присылает его в поле sku. Это AMBIGUOUS, а не «берём первое» (раздел 3.2).
AMBIGUOUS_BARCODE = "2000000000099"

# Товар с нулевым остатком: у seller-a клапан включён — резерв пройдёт с
# ledger_short; у seller-b выключен — придёт INSUFFICIENT_STOCK (раздел 6.5).
ZERO_STOCK_BARCODE = "2000000000035"

CELLS: list[dict[str, Any]] = [
    {"cell_id": "c0000000-0000-4000-8000-000000000001",
     "address": "01-02-03", "zone": "STOR", "route_order": 10},
    {"cell_id": "c0000000-0000-4000-8000-000000000002",
     "address": "01-02-04", "zone": "STOR", "route_order": 20},
    {"cell_id": "c0000000-0000-4000-8000-000000000003",
     "address": "01-03-01", "zone": "STOR", "route_order": 30},
    {"cell_id": "c0000000-0000-4000-8000-000000000004",
     "address": "01-03-02", "zone": "STOR", "route_order": 40},
    {"cell_id": "c0000000-0000-4000-8000-000000000005",
     "address": "RCV-01", "zone": "RECV", "route_order": None},
]

# Имена полей — из BoxProjection контракта, не из головы: клиенты потоков B и C
# пишутся против заглушки, и её форма становится их формой. `state` обязателен
# по контракту, и без него `MockState.boxes()` роняла весь список в 500 —
# заявка 3 потока B и пункт 5 потока C, найдено дважды независимо.
BOXES: list[dict[str, Any]] = [
    {"box_id": "e0000000-0000-4000-8000-000000000001", "barcode": "BOX-0001",
     "owner_external_id": "seller-a", "product_barcode": "2000000000011",
     "cell_address": "01-02-03", "cell_id": "c0000000-0000-4000-8000-000000000001",
     "quantity": 42, "counted": True, "state": "stored",
     "comment": "куртки M, верхняя полка у окна"},
    {"box_id": "e0000000-0000-4000-8000-000000000002", "barcode": "BOX-0002",
     "owner_external_id": "seller-a", "product_barcode": "2000000000028",
     "cell_address": "01-02-04", "cell_id": "c0000000-0000-4000-8000-000000000002",
     "quantity": 17, "counted": True, "state": "stored",
     "comment": "куртки L, нижняя полка, рядом со стойкой 4"},
    {"box_id": "e0000000-0000-4000-8000-000000000003", "barcode": "BOX-0003",
     "owner_external_id": "seller-b", "product_barcode": "2000000000042",
     "cell_address": "01-03-01", "cell_id": "c0000000-0000-4000-8000-000000000003",
     "quantity": 8, "counted": True, "state": "stored",
     "comment": "платья S, синяя коробка с наклейкой"},
    # Коробка стоит на полке, а по учёту в ней ноль — ровно тот случай, ради
    # которого держат клапан раздела 6.5. Расхождение обязано быть адресным,
    # поэтому у сборки без остатка есть ячейка, а не NULL.
    {"box_id": "e0000000-0000-4000-8000-000000000004", "barcode": "BOX-0004",
     "owner_external_id": "seller-a", "product_barcode": "2000000000035",
     "cell_address": "01-03-02", "cell_id": "c0000000-0000-4000-8000-000000000004",
     "quantity": 0, "counted": False, "state": "stored",
     "comment": "ботинки 39, дальний стеллаж — учёт расходится с полкой"},
]

WB_ACCOUNTS: list[dict[str, Any]] = [
    {"id": "f0000000-0000-4000-8000-000000000001", "external_id": "wb-acc-1",
     "seller_external_id": "seller-a", "display_name": "Кабинет Акей",
     # Ссылка на секрет, не секрет. Живых токенов на стенде нет (раздел 12).
     "secret_ref": "vault://mmx/stand/wb/seller-a", "mode": "shadow", "status": "ACTIVE",
     "token_type": "SERVICE"},
    {"id": "f0000000-0000-4000-8000-000000000002", "external_id": "wb-acc-2",
     "seller_external_id": "seller-b", "display_name": "Кабинет Эминова",
     "secret_ref": "vault://mmx/stand/wb/seller-b", "mode": "shadow", "status": "ACTIVE",
     "token_type": "SERVICE"},
]

STATIONS: list[dict[str, Any]] = [
    {"name": f"station-{n}", "transport": "agent", "printer_model": "Xprinter XP-420B",
     "active": True}
    for n in range(1, 6)
]


def barcode_is_known(barcode: str) -> bool:
    return any(product["barcode"] == barcode for product in PRODUCTS)


def product_by_barcode(barcode: str) -> dict[str, Any] | None:
    for product in PRODUCTS:
        if product["barcode"] == barcode:
            return product
    return None


def seller_is_known(seller_external_id: str) -> bool:
    return any(seller["seller_external_id"] == seller_external_id for seller in SELLERS)


def owner_id_of(seller_external_id: str) -> str | None:
    for seller in SELLERS:
        if seller["seller_external_id"] == seller_external_id:
            return seller["owner_id"]
    return None


def allows_ledger_short(seller_external_id: str) -> bool:
    for seller in SELLERS:
        if seller["seller_external_id"] == seller_external_id:
            return bool(seller["allow_ledger_short"])
    return False
