"""Обвязка тестов сервиса portal."""
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
    base = os.getenv("PORTAL_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not base:
        raise RuntimeError("нужен PORTAL_TEST_DATABASE_URL")
    name = f"portal_test_{uuid.uuid4().hex[:8]}"
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
