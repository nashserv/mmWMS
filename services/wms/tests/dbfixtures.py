"""Подключение тестов к настоящему Postgres.

Транзакцию раздела 6.2 нельзя проверить на заглушке: её смысл — блокировки,
триггеры проекции и `ON CONFLICT`, то есть ровно то, чего в памяти нет.
Поэтому тесты ядра идут против живой базы с применёнными миграциями.

Адрес берётся из `WMS_TEST_DATABASE_URL` (или `DATABASE_URL`). Без него тесты
ядра не выполняются — и говорят об этом вслух, а не притворяются зелёными.
"""
from __future__ import annotations

import os
import uuid

import pytest

REQUIRED = (
    "нужен Postgres с применёнными миграциями: задайте WMS_TEST_DATABASE_URL. "
    "Транзакция 6.2 проверяется блокировками и триггерами, на заглушке её нет."
)


def database_url() -> str | None:
    return (os.getenv("WMS_TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip() or None


def require_database() -> str:
    url = database_url()
    if not url:
        pytest.skip(REQUIRED)
    return url


def unique(prefix: str) -> str:
    """Идентификатор, не сталкивающийся ни с сидом стенда, ни с прошлым прогоном.

    База стенда не пустая — в ней 22 продавца и 255 SKU из реальной выгрузки.
    Тест обязан работать рядом с ними, а не требовать чистой базы: именно так
    сервис и будет жить.
    """
    return f"utest-{prefix}-{uuid.uuid4().hex[:12]}"
