"""Deterministic local runtime defaults for Gateway unit tests."""
import os


os.environ["APP_ENV"] = "test"
os.environ["WB_ADAPTER"] = "fake"
os.environ["WMS_ADAPTER"] = "fake"
os.environ["SECRET_PROVIDER"] = "memory"
os.environ["ACCOUNT_STORAGE_BACKEND"] = "memory"
