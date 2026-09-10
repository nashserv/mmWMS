"""Fail-closed конфигурация ЛК клиента.

Форма повторяет шаблон платформы (services/wms/app/config.py и
reference/wb-fbs-gateway-template): секрет читается либо из переменной, либо из
смонтированного файла, но никогда из обоих сразу и никогда «по умолчанию».
Умолчание у секрета — это работающий стенд с чужим ключом.
"""
from __future__ import annotations

import os
from pathlib import Path

LOCAL_ENVIRONMENTS = frozenset({"test", "local", "development"})
PROTECTED_ENVIRONMENTS = frozenset({"staging", "prod", "production"})

SERVICE = "portal"


def app_environment() -> str:
    value = os.getenv("APP_ENV", "").strip().lower()
    if value not in LOCAL_ENVIRONMENTS | PROTECTED_ENVIRONMENTS:
        raise RuntimeError(
            "APP_ENV must be explicitly set to test, local, development, staging, prod, or production"
        )
    return value


def read_secret(name: str, *, required: bool = True) -> str | None:
    direct = os.getenv(name)
    file_name = os.getenv(f"{name}_FILE")
    if direct and file_name:
        raise RuntimeError(f"configure only one of {name} and {name}_FILE")
    value = direct
    if file_name:
        path = Path(file_name)
        try:
            if not path.is_file() or path.stat().st_size > 65_536:
                raise RuntimeError(f"{name}_FILE is unavailable or too large")
            value = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError(f"{name}_FILE cannot be read") from error
    if value and ("\r" in value or "\n" in value or "\0" in value or len(value) > 65_536):
        raise RuntimeError(f"{name} is invalid")
    if required and not value:
        raise RuntimeError(f"{name} or {name}_FILE is required")
    return value


def trusted_hosts(environment: str) -> list[str]:
    configured = os.getenv("TRUSTED_HOSTS")
    if environment not in LOCAL_ENVIRONMENTS and not configured:
        raise RuntimeError("TRUSTED_HOSTS is required outside test/local environments")
    values = [
        value.strip()
        for value in (configured or "testserver,localhost,127.0.0.1").split(",")
        if value.strip()
    ]
    if not values or (environment not in LOCAL_ENVIRONMENTS and "*" in values):
        raise RuntimeError("TRUSTED_HOSTS must contain explicit hosts")
    return values


def database_url() -> str:
    return read_secret("DATABASE_URL") or ""


def tenant_id() -> str:
    return os.getenv("MMX_TENANT_ID", "mm-express")


def events_exchange() -> str:
    return os.getenv("MMX_EVENTS_EXCHANGE", "mmx.events")


def billing_url() -> str:
    return os.getenv("BILLING_URL", "http://billing:8080").rstrip("/")


def identity_url() -> str:
    return os.getenv("IDENTITY_URL", "http://identity:8080").rstrip("/")


def wms_url() -> str:
    """Склад — единственный источник остатка.

    Портал не держит зеркала: сегодня он показывает 3528 единиц при реальном
    остатке 92, потому что зеркалит кабинет Wildberries, а не склад. Одно
    число получается только у того, у кого один источник.
    """
    return os.getenv("WMS_BASE_URL", "http://wms:8080").rstrip("/")
