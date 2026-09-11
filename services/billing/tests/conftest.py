"""Обвязка тестов биллинга.

Тесты идут против настоящего Postgres с применёнными миграциями — как в CI
платформы (reference/wb-fbs-gateway-template/README.md). Заменять базу
заглушкой здесь нельзя: половина правил биллинга живёт в схеме — история
закреплений без наложений, net_amount, раскладка комиссии, — и тест против
словаря в памяти проверял бы не их.
"""
from __future__ import annotations

import os
import pathlib
import uuid
from datetime import date
from decimal import Decimal
from typing import Any
from collections.abc import Iterator

import psycopg
import pytest

from app.db import Database


def _guard_test_database(dsn: str, variable: str) -> str:
    """Не пускать тесты в рабочую базу.

    Фолбэк на DATABASE_URL уже стоил стенду 957 чужих владельцев `utest-*`:
    переменную забывали задать, и тесты молча уходили писать туда, где живут
    настоящие данные. Забыть — это норма; уронить из-за этого стенд — нет.
    """
    from urllib.parse import urlparse

    name = (urlparse(dsn).path or "").lstrip("/").split("?")[0]
    if not name.endswith("_test"):
        pytest.exit(
            f"{variable} указывает на базу {name!r}, а имя обязано оканчиваться "
            f"на '_test'. Тесты заводят данные пачками — в рабочей базе им не место.",
            returncode=2)
    return dsn


MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"

# Дата, на которую считают все тесты. Фиксированная: «сегодня» в тесте про
# историю закреплений однажды переедет через границу периода и покраснеет ночью.
TODAY = date(2026, 9, 10)


def admin_url() -> str:
    url = os.getenv("BILLING_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "нужен BILLING_TEST_DATABASE_URL: тесты биллинга идут против настоящего "
            "Postgres, потому что половина правил держится схемой, а не кодом")
    return _guard_test_database(url, "BILLING_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    """Отдельная база на прогон тестов, снесённая по окончании."""
    base = admin_url()
    name = f"billing_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{name}"')
    target = base.rsplit("/", 1)[0] + "/" + name
    try:
        with psycopg.connect(target, autocommit=True) as connection:
            for path in sorted(MIGRATIONS.glob("*.sql")):
                connection.execute(path.read_text(encoding="utf-8"))
        handle = Database(target, size=4)
        yield handle
        handle.close()
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,))
            connection.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(autouse=True)
def clean(request: pytest.FixtureRequest) -> Iterator[None]:
    """Каждый тест начинает с пустой базы: чужие начисления — чужие суммы.

    Тесты арифметики базы не касаются вовсе, и поднимать её ради них значит
    сделать красным то, что от неё не зависит.
    """
    if "database" not in request.fixturenames:
        yield
        return
    database: Database = request.getfixturevalue("database")
    with database.transaction() as cursor:
        cursor.execute("""
            TRUNCATE billing_commission, billing_accrual, billing_unbilled, billing_inbox,
                     billing_outbox, billing_shift_output, billing_expense_allocation,
                     billing_fixed_expense, billing_invoice, billing_period,
                     billing_tariff_assignment, billing_contract, billing_tariff_tier,
                     billing_tariff_version, billing_tariff, billing_billable_event,
                     price_layer, cabinet_assignment, cabinet_wb_account, cabinet, partner
            RESTART IDENTITY CASCADE
        """)
    yield


@pytest.fixture
def stand(database: Database) -> dict[str, Any]:
    """Минимальный стенд: Зардал с наценкой 15, менеджер, кабинет, тариф 30 ₽.

    Ровно та конфигурация, которую описывает раздел 1 мастера, — на ней и
    проверяется, что клиент платит 45, партнёр получает 15, MM-Express 30.
    """
    ids = {name: str(uuid.uuid4()) for name in
           ("senior", "manager", "cabinet", "tariff", "version", "tier")}
    with database.transaction() as cursor:
        cursor.execute("INSERT INTO partner (id, name) VALUES (%s, 'Зардал')", (ids["senior"],))
        cursor.execute(
            "INSERT INTO partner (id, parent_id, name) VALUES (%s, %s, 'Менеджер кабинетов')",
            (ids["manager"], ids["senior"]))
        cursor.execute(
            "INSERT INTO cabinet (id, seller_external_id, name) VALUES (%s, 'seller-1', 'ИП Тест')",
            (ids["cabinet"],))
        cursor.execute(
            "INSERT INTO cabinet_assignment (id, cabinet_id, partner_id, role, from_date) "
            "VALUES (%s, %s, %s, 'account_manager', DATE '2026-01-01')",
            (str(uuid.uuid4()), ids["cabinet"], ids["manager"]))
        cursor.execute(
            "INSERT INTO price_layer (id, partner_id, service, markup, from_date) "
            "VALUES (%s, %s, 'packing', 15.00, DATE '2026-01-01')",
            (str(uuid.uuid4()), ids["senior"]))
        cursor.execute(
            "INSERT INTO billing_tariff (id, code, service, name, unit, is_default) "
            "VALUES (%s, 'packing-default', 'packing', 'Упаковка', 'шт', true)",
            (ids["tariff"],))
        cursor.execute(
            "INSERT INTO billing_tariff_version (id, tariff_id, effective_from, approved, "
            "approved_by, approved_at, partner_fee) "
            "VALUES (%s, %s, DATE '2026-01-01', true, 'владелец', now(), 15.00)",
            (ids["version"], ids["tariff"]))
        cursor.execute(
            "INSERT INTO billing_tariff_tier (id, version_id, up_to, unit_price) "
            "VALUES (%s, %s, NULL, 30.00)",
            (ids["tier"], ids["version"]))
        cursor.execute(
            # Те же типы, что в сиде стенда: `order.packed.v1` эмитило старое
            # рабочее место, новый wms шлёт `wms.packing.completed.v1`.
            # Обвязка, отстающая от сида, делает тесты зелёными на том, что в
            # бою не тарифицируется вовсе.
            "INSERT INTO billing_billable_event (event_type, service, quantity_path, comment) "
            "VALUES ('wms.packing.completed.v1', 'packing', NULL, 'тест'), "
            "       ('wb.supply.shipped.v1', 'shipping', 'orders', 'тест')")
    return ids


def event(event_type: str, payload: dict[str, Any], *, event_id: str | None = None,
          occurred_at: str = "2026-09-10T10:00:00+00:00") -> dict[str, Any]:
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "tenant_id": "mm-express",
        "type": event_type,
        "occurred_at": occurred_at,
        "payload": payload,
        "correlation_id": "test-corr",
    }


def rows(database: Database, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with database.cursor() as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def money(value: Any) -> Decimal:
    return Decimal(str(value))
