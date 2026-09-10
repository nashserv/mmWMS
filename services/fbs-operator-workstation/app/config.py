"""Fail-closed конфигурация рабочего места.

Форма секретов и хостов взята из шаблона платформы без изменений по существу:
секрет читается либо из переменной, либо из смонтированного файла, но никогда
из обоих сразу и никогда с умолчанием. Умолчание у секрета — это работающий
стенд с чужим ключом.

Настройки самого рабочего места собраны здесь же, чтобы «переключиться с mock
на настоящий wms» было сменой одной переменной, а не правкой кода (раздел
«Работа против mock» файла 03).
"""
from __future__ import annotations

import os
from pathlib import Path

LOCAL_ENVIRONMENTS = frozenset({"test", "local", "development"})
PROTECTED_ENVIRONMENTS = frozenset({"staging", "prod", "production"})

SERVICE = "fbs_operator_workstation"


def app_environment() -> str:
    value = os.getenv("APP_ENV", "").strip().lower()
    if value not in LOCAL_ENVIRONMENTS | PROTECTED_ENVIRONMENTS:
        raise RuntimeError(
            "APP_ENV must be explicitly set to test, local, development, staging, prod, or production"
        )
    return value


def read_secret(name: str, *, required: bool = True) -> str | None:
    """Читает секрет из переменной окружения либо из смонтированного файла."""
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


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Число из окружения с жёсткими границами.

    Границы, а не «что дали»: интервал опроса в 0 мс кладёт wms, интервал в час
    возвращает ту самую беду, ради которой всё переписано.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def wms_base_url() -> str:
    """Базовый URL сервиса wms — с префиксом контракта или без него.

    Один и тот же контракт у mock и у настоящего сервиса (приложение B
    мастера), поэтому переключение потока A — смена этой переменной, и
    больше ничего.
    """
    value = (os.getenv("WMS_BASE_URL") or "").strip()
    if not value:
        raise RuntimeError("WMS_BASE_URL is required: рабочему месту некуда спрашивать задания")
    return value.rstrip("/")


def wms_service_token() -> str | None:
    """Сервисный токен к wms.

    В локальных окружениях его может не быть — на стенде авторизация не
    поднята. Вне их отсутствие токена это ошибка старта, а не тихий
    неавторизованный клиент.
    """
    environment = app_environment()
    required = environment not in LOCAL_ENVIRONMENTS
    return read_secret("WMS_SERVICE_TOKEN", required=required)


def wms_timeout_seconds() -> float:
    return _int_env("WMS_TIMEOUT_MS", 5_000, minimum=200, maximum=60_000) / 1000.0


def poll_interval_seconds() -> float:
    """Период опроса /tasks/pull.

    Опрос — источник истины (раздел 6.1). Период задаёт худшую задержку
    «задание в базе wms → задание на экране», когда шина молчит: критерий
    раздела 10 — меньше 5 секунд p99.
    """
    return _int_env("WORKSTATION_POLL_INTERVAL_MS", 1_000, minimum=100, maximum=30_000) / 1000.0


def poll_limit() -> int:
    return _int_env("WORKSTATION_POLL_LIMIT", 200, minimum=1, maximum=500)


def lease_seconds() -> int:
    """Срок лизинга выданного задания.

    Сборщик ушёл со смены посреди обхода — задание обязано вернуться в общую
    очередь, а не пропасть вместе с ним (раздел 7 мастера).
    """
    return _int_env("WORKSTATION_LEASE_SECONDS", 900, minimum=30, maximum=3_600)


def database_url() -> str:
    value = (os.getenv("DATABASE_URL") or "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is required: база workstation")
    return value


def rabbitmq_url() -> str | None:
    """Шина. Пустая переменная — законное состояние, а не ошибка.

    Рабочее место обязано работать при выключенном RabbitMQ (пункт 14 прогона),
    поэтому отсутствие брокера настраивается, а не обходится."""
    value = (os.getenv("RABBITMQ_URL") or "").strip()
    return value or None


def events_exchange() -> str:
    return (os.getenv("MMX_EVENTS_EXCHANGE") or "mmx.events").strip()


def tenant_id() -> str:
    return (os.getenv("MMX_TENANT_ID") or "mm-express").strip()


def worker_silence_seconds() -> int:
    """Через сколько секунд без обработанной единицы воркер считается сломанным.

    Инвариант 14 мастера: молчащий воркер сломан. В боевом контуре четыре
    воркера стояли Up с зелёным healthcheck и нулём строк лога.
    """
    return _int_env("WORKSTATION_WORKER_SILENCE_SECONDS", 900, minimum=60, maximum=86_400)
