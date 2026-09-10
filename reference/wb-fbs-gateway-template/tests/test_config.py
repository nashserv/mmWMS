import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import app_environment, read_secret, trusted_hosts
from app.service import Gateway, GatewayError


def test_environment_must_be_explicit():
    with patch.dict(os.environ, {}, clear=True), pytest.raises(RuntimeError, match="APP_ENV must be explicitly set"):
        app_environment()


def test_secret_file_and_direct_value_are_mutually_exclusive(tmp_path: Path):
    value_file = tmp_path / "value"
    value_file.write_text("from-file\n", encoding="utf-8")
    with patch.dict(os.environ, {"APP_ENV": "test", "EXAMPLE_FILE": str(value_file)}, clear=True):
        assert read_secret("EXAMPLE") == "from-file"
    with patch.dict(os.environ, {"APP_ENV": "test", "EXAMPLE": "direct", "EXAMPLE_FILE": str(value_file)}, clear=True), pytest.raises(RuntimeError, match="only one"):
        read_secret("EXAMPLE")


def test_protected_runtime_rejects_fake_wb_even_with_an_odoo_adapter(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("WB_ADAPTER", "fake")
    monkeypatch.setenv("WMS_ADAPTER", "odoo")
    monkeypatch.setenv("ODOO_WMS_URL", "https://odoo.internal")
    monkeypatch.setenv("ODOO_WMS_DB", "mmx")
    monkeypatch.setenv("ODOO_WMS_LOGIN", "gateway")
    monkeypatch.setenv("ODOO_WMS_API_KEY", "x" * 40)
    with pytest.raises(GatewayError, match="WB_ADAPTER_NOT_CONFIGURED"):
        Gateway.from_env()


def test_protected_hosts_are_explicit():
    with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True), pytest.raises(RuntimeError, match="TRUSTED_HOSTS"):
        trusted_hosts(app_environment())
