"""Конфигурация. Умолчание у секрета — это работающий стенд с чужим ключом."""
from __future__ import annotations


import pytest

from app import config


def test_environment_must_be_declared(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "")
    with pytest.raises(RuntimeError):
        config.app_environment()
    monkeypatch.setenv("APP_ENV", "производство")
    with pytest.raises(RuntimeError):
        config.app_environment()


def test_wms_url_is_required(monkeypatch: pytest.MonkeyPatch):
    """Без адреса wms рабочему месту некуда спрашивать задания."""
    monkeypatch.delenv("WMS_BASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        config.wms_base_url()


def test_bus_url_is_optional(monkeypatch: pytest.MonkeyPatch):
    """Отсутствие брокера — законное состояние: склад от шины не зависит."""
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    assert config.rabbitmq_url() is None


def test_poll_interval_has_hard_limits(monkeypatch: pytest.MonkeyPatch):
    """Ноль кладёт wms, час возвращает ту самую беду, ради которой всё переписано."""
    monkeypatch.setenv("WORKSTATION_POLL_INTERVAL_MS", "0")
    with pytest.raises(RuntimeError):
        config.poll_interval_seconds()
    monkeypatch.setenv("WORKSTATION_POLL_INTERVAL_MS", "600000")
    with pytest.raises(RuntimeError):
        config.poll_interval_seconds()
    monkeypatch.setenv("WORKSTATION_POLL_INTERVAL_MS", "750")
    assert config.poll_interval_seconds() == 0.75


def test_service_token_is_required_outside_local(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TRUSTED_HOSTS", "workstation")
    # Убираются ОБА имени. `SERVICE_TOKEN` — общее для всех сервисов,
    # `WMS_SERVICE_TOKEN` — прежнее: тест, чистящий только прежнее, проходил
    # на стенде, где задано общее, и ничего не проверял.
    for name in ("SERVICE_TOKEN", "SERVICE_TOKEN_FILE",
                 "WMS_SERVICE_TOKEN", "WMS_SERVICE_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError):
        config.wms_service_token()
    monkeypatch.setenv("APP_ENV", "test")
    assert config.wms_service_token() is None


def test_secret_is_never_taken_from_two_places_at_once(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WMS_SERVICE_TOKEN", "a")
    monkeypatch.setenv("WMS_SERVICE_TOKEN_FILE", "/tmp/x")
    with pytest.raises(RuntimeError):
        config.read_secret("WMS_SERVICE_TOKEN")


def test_wildcard_host_is_refused_in_production(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRUSTED_HOSTS", "*")
    with pytest.raises(RuntimeError):
        config.trusted_hosts("production")


def test_token_never_reaches_the_url(monkeypatch: pytest.MonkeyPatch):
    """Инвариант 15: секреты не попадают ни в логи, ни в ответы, ни в URL."""
    from app.wms_client import WmsClient
    token = "not-a-real-token-0000"
    client = WmsClient("http://wms:8080", token=token)
    assert token not in client.base_url
