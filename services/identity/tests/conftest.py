"""Обвязка тестов роле́вой части identity."""
from __future__ import annotations

import os
import pathlib
import uuid
from typing import Iterator

import psycopg
import pytest

from app.db import Database

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    base = os.getenv("IDENTITY_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not base:
        raise RuntimeError("нужен IDENTITY_TEST_DATABASE_URL: роли и области живут в базе")
    name = f"identity_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{name}"')
    target = base.rsplit("/", 1)[0] + "/" + name
    try:
        with psycopg.connect(target, autocommit=True) as connection:
            for path in sorted(MIGRATIONS.glob("*.sql")):
                connection.execute(path.read_text(encoding="utf-8"))
        handle = Database(target, size=2)
        yield handle
        handle.close()
    finally:
        with psycopg.connect(base, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,))
            connection.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(autouse=True)
def clean(database: Database) -> Iterator[None]:
    with database.transaction() as cursor:
        cursor.execute("DELETE FROM identity_role_grant")
    yield
