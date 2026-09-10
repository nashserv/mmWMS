# -*- coding: utf-8 -*-
"""Строит анонимизированный SQL-сид для стенда из read-only CSV-выгрузки прода.

Свежие UUID везде — никакой связи со старыми id. Имена/номера продавцов —
синтетика. Секреты кабинетов WB — заведомые заглушки вида stand-fake-*,
никогда не настоящие. Товарные данные (артикул, штрихкод, название) —
общие розничные описания, не персональные и не секретные, оставлены как есть.
"""
import csv
import uuid

RAW = r"C:\Users\user\AppData\Local\Temp\claude\C--Users-user-Desktop-ZMS\e381544d-a7ca-4c29-8666-310dcfa0ce13\scratchpad\dump-raw"
OUT = r"C:\Users\user\AppData\Local\Temp\claude\C--Users-user-Desktop-ZMS\e381544d-a7ca-4c29-8666-310dcfa0ce13\scratchpad\seed-stand.sql"


def esc(s):
    if s is None or s == "":
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"


def new_id():
    return str(uuid.uuid4())


with open(RAW + r"\sellers.csv", encoding="utf-8", newline="") as f:
    sellers = list(csv.DictReader(f))

owner_map = {}  # старый sellers.id -> новый owner id
owner_rows = []
for i, row in enumerate(sellers, start=1):
    new_owner_id = new_id()
    owner_map[row["id"]] = new_owner_id
    name = f"Тестовый продавец {i:02d}"
    seller_ext = f"stand-seller-{i:03d}"
    inn = f"{9990000000 + i}"
    active = "true" if row["status"] == "ACTIVE" else "false"
    owner_rows.append(
        f"({esc(new_owner_id)}, {esc(seller_ext)}, {esc(name)}, {esc(inn)}, {active}, now())"
    )

with open(RAW + r"\wb_accounts.csv", encoding="utf-8", newline="") as f:
    wb_accounts = list(csv.DictReader(f))

wb_rows = []
for i, row in enumerate(wb_accounts, start=1):
    owner_id = owner_map.get(row["seller_id"])
    if not owner_id:
        continue
    wb_id = new_id()
    ext = f"stand-wb-{i:03d}"
    display = f"Тестовый кабинет {i:02d}"
    secret_ref = f"stand-fake-secret-{i:03d}"
    # Схема стенда допускает только shadow|live (раздел 11) — не исходные
    # PRODUCTION/TEST/BASIC/PERSONAL с прода.
    mode = "live" if row["mode"] == "PRODUCTION" else "shadow"
    status = row["status"] if row["status"] in ("ACTIVE", "AUTH_ERROR", "RATE_LIMITED", "DISABLED") else "ACTIVE"
    wb_rows.append(
        f"({esc(wb_id)}, {esc(owner_id)}, {esc(ext)}, {esc(display)}, {esc(secret_ref)}, "
        f"{esc(mode)}, {esc(status)}, 0)"
    )

with open(RAW + r"\catalog_product.csv", encoding="utf-8", newline="") as f:
    products = list(csv.DictReader(f))

sku_rows = []
seen = set()
for row in products:
    owner_id = owner_map.get(row["owner_id"])
    barcode = (row["barcode"] or "").strip()
    if not owner_id or not barcode:
        continue
    key = (owner_id, barcode)
    if key in seen:
        continue
    seen.add(key)
    sku_id = new_id()
    seller_sku = row["seller_sku"] or ""
    name = row["name"] or ""
    sku_rows.append(
        f"({esc(sku_id)}, {esc(owner_id)}, {esc(seller_sku)}, {esc(barcode)}, {esc(name)}, 0)"
    )

with open(OUT, "w", encoding="utf-8") as f:
    f.write("-- Анонимизированный сид стенда. Снято read-only с 91.108.239.175 10.09.2026.\n")
    f.write("-- Секретов нет: имена/номера продавцов синтетика, secret_ref заведомая заглушка.\n")
    f.write("-- Задания намеренно не сидируются — их даёт wb-simulator, не история.\n\n")

    f.write("BEGIN;\n\n")

    f.write(f"-- owner: {len(owner_rows)} строк\n")
    f.write("INSERT INTO owner (id, seller_external_id, name, inn, active, created_at) VALUES\n")
    f.write(",\n".join(owner_rows) + ";\n\n")

    f.write(f"-- wb_account: {len(wb_rows)} строк\n")
    f.write(
        "INSERT INTO wb_account (id, owner_id, external_id, display_name, secret_ref, "
        "mode, status, sync_attempts) VALUES\n"
    )
    f.write(",\n".join(wb_rows) + ";\n\n")

    f.write(f"-- sku: {len(sku_rows)} строк\n")
    f.write("INSERT INTO sku (id, owner_id, seller_sku, barcode, name, buffer) VALUES\n")
    f.write(",\n".join(sku_rows) + ";\n\n")

    f.write("COMMIT;\n")

print(f"owner={len(owner_rows)} wb_account={len(wb_rows)} sku={len(sku_rows)}")
print(f"написано в {OUT}")
