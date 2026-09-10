"""Fail-closed runtime configuration for the WB FBS Gateway."""
from __future__ import annotations

import os
import ssl
import stat
from pathlib import Path


LOCAL_ENVIRONMENTS = frozenset({"test", "local", "development"})
PROTECTED_ENVIRONMENTS = frozenset({"staging", "prod", "production"})
MAX_EXTERNAL_CA_BYTES = 1024 * 1024


def external_ca_file() -> str | None:
    value = os.getenv("MMX_EXTERNAL_CA_FILE")
    if value in (None, ""):
        return None
    if value != value.strip() or len(value.encode("utf-8")) > 4096 or any(
        character in value for character in ("\r", "\n", "\x00")
    ):
        raise RuntimeError("MMX_EXTERNAL_CA_FILE is invalid")
    path = Path(value)
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_EXTERNAL_CA_BYTES:
            raise RuntimeError("MMX_EXTERNAL_CA_FILE is unavailable or too large")
        with path.open("rb") as source:
            content = source.read(MAX_EXTERNAL_CA_BYTES + 1)
        if len(content) > MAX_EXTERNAL_CA_BYTES:
            raise RuntimeError("MMX_EXTERNAL_CA_FILE is unavailable or too large")
        if b"\x00" in content or b"PRIVATE KEY" in content or b"-----BEGIN CERTIFICATE-----" not in content:
            raise RuntimeError("MMX_EXTERNAL_CA_FILE is not a PEM CA bundle")
        ssl.create_default_context(cafile=str(path))
    except (OSError, ssl.SSLError) as error:
        raise RuntimeError("MMX_EXTERNAL_CA_FILE cannot be loaded") from error
    return str(path)


def external_ssl_context() -> ssl.SSLContext:
    ca_file = external_ca_file()
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    return context


def httpx_verify() -> bool | ssl.SSLContext:
    ca_file = external_ca_file()
    if ca_file is None:
        return True
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=ca_file)
    return context


def app_environment() -> str:
    value = os.getenv("APP_ENV", "").strip().lower()
    if value not in LOCAL_ENVIRONMENTS | PROTECTED_ENVIRONMENTS:
        raise RuntimeError(
            "APP_ENV must be explicitly set to test, local, development, staging, prod, or production"
        )
    return value


def read_secret(name: str, *, required: bool = True) -> str | None:
    """Read one secret from an environment value or a mounted secret file."""
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
