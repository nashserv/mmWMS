"""Обновление снимка собственного API: `python -m tests.snapshot > contracts/...`."""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("WMS_BASE_URL", "http://wms.test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/none")

from app.api import create_app  # noqa: E402

if __name__ == "__main__":
    json.dump(create_app().openapi(), sys.stdout, ensure_ascii=False, indent=2,
              sort_keys=True)
    sys.stdout.write("\n")
