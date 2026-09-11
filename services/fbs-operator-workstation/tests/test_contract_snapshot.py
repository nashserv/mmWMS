"""Снимок собственного API рабочего места.

Контракт `wms` заморожен потоком 0 и живёт в его каталоге. Здесь — другой
контракт: между экраном и сервисом. Он не заморожен и меняется свободно, но
меняться должен заметно: снимок в `contracts/` лежит рядом с кодом и обязан
совпадать с тем, что сервис отдаёт на самом деле.

Разошлось — либо обновить снимок осознанно, либо вернуть маршрут: молча
уехавший API ломает экраны на станциях, а узнаётся об этом от оператора.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).resolve().parents[1] / "contracts" / "workstation-openapi.json"


@pytest.fixture()
def application(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WMS_BASE_URL", "http://wms.test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/none")
    from app import api
    return api.create_app()


def test_snapshot_matches_the_running_service(application):
    live = application.openapi()
    assert SNAPSHOT.exists(), (
        f"снимок {SNAPSHOT.name} отсутствует; создать: "
        "python -m tests.snapshot > contracts/workstation-openapi.json")
    stored = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert sorted(stored["paths"]) == sorted(live["paths"]), (
        "набор маршрутов разошёлся со снимком — обновить осознанно или вернуть маршрут")
    for path, methods in live["paths"].items():
        assert sorted(stored["paths"][path]) == sorted(methods), f"методы {path} разошлись"


def test_the_screens_have_their_own_api_not_the_wms_contract(application):
    """Экраны не ходят в wms напрямую: весь разговор с ним — через сервис."""
    paths = application.openapi()["paths"]
    assert any(path.startswith("/api/workstation/v1/") for path in paths)
    assert not any(path.startswith("/api/mmx/wms/") for path in paths), (
        "контракт wms принадлежит потоку 0 и здесь не переопределяется")
