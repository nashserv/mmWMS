"""Копии общего модуля auth обязаны совпадать байт в байт.

Общего пакета у потоков нет (правило 9.5.1: один агент — один каталог), и
заводить его ради двухсот строк дороже, чем держать копии одинаковыми. Но
«держать одинаковыми» на памяти не работает: правку вносят в один сервис, а
через месяц оказывается, что в другом проверка подписи мягче.

Тест живёт в wms, потому что он тут один на все пять — дублировать ещё и его
значило бы повторить ту же ошибку.
"""
from __future__ import annotations

import hashlib
import pathlib

import pytest

SERVICES = ("wms", "billing", "internal-admin", "portal", "fbs-operator-workstation")
ROOT = pathlib.Path(__file__).resolve().parents[3] / "services"


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_copy_of_the_auth_module_is_the_same() -> None:
    paths = {name: ROOT / name / "app" / "auth.py" for name in SERVICES}
    missing = [name for name, path in paths.items() if not path.is_file()]
    assert not missing, f"модуль auth отсутствует в сервисах: {missing}"

    digests = {name: digest(path) for name, path in paths.items()}
    unique = set(digests.values())
    assert len(unique) == 1, (
        "копии модуля auth разошлись:\n  " + "\n  ".join(
            f"{name}: {value[:12]}" for name, value in sorted(digests.items())) +
        "\nПравка вносится в один файл и копируется целиком — иначе в одном "
        "сервисе проверка подписи однажды окажется мягче, чем в остальных.")


@pytest.mark.parametrize("name", SERVICES)
def test_no_copy_quietly_allows_everyone(name: str) -> None:
    """Грубая, но полезная проверка: в модуле не должно быть строк, которые
    выключают проверку. Их туда добавляют «на время отладки»."""
    text = (ROOT / name / "app" / "auth.py").read_text(encoding="utf-8")
    for forbidden in ("return Caller(subject=\"anonymous\"", "verify=False",
                      "if True:", "# noqa: S105"):
        assert forbidden not in text, f"{name}: в модуле auth найдено {forbidden!r}"
