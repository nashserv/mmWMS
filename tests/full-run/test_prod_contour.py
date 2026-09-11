"""Боевой контур не превращается в стенд и наоборот.

Контур для шагов 4–6 отличается от стенда десятком мелочей, и каждая из них —
это последствие, а не стиль. Разъехаться они могут молча: кто-то скопирует
блок из `compose.yaml`, и на бою окажется симулятор Wildberries или заглушка
склада.

Проверяется ТЕКСТ файла, а не поднятый контур: поднять боевой контур здесь
нельзя и не нужно. Это проверка того, что в файле написано, — единственное,
что вообще можно проверить до дня, когда его запустят.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PROD = Path(__file__).resolve().parents[2] / "infrastructure" / "prod"
COMPOSE = PROD / "compose.prod.yaml"


@pytest.fixture(scope="module")
def contour() -> dict:
    if not COMPOSE.exists():
        pytest.skip("боевого контура нет в репозитории")
    # Переменные не подставляются: проверяется, ЧТО написано, а не во что оно
    # развернётся на конкретной машине.
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _environments(contour: dict) -> dict[str, dict]:
    out = {}
    for name, service in (contour.get("services") or {}).items():
        env = service.get("environment") or {}
        if isinstance(env, dict):
            out[name] = {str(k): str(v) for k, v in env.items()}
    return out


def test_the_simulator_is_nowhere_in_the_production_contour(contour: dict) -> None:
    """Симулятора на бою нет.

    Адрес симулятора, оставшийся в переменной, — это склад, публикующий остатки
    в никуда, и оператор, который узнаёт об этом от клиента.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "wb-simulator" not in body, "в боевом контуре остался симулятор Wildberries"
    assert "WB_SIMULATOR_URL" not in body, "в боевом контуре осталась ссылка на симулятор"


def test_the_warehouse_is_never_a_mock(contour: dict) -> None:
    """`WMS_MOCK=true` на бою — это склад, отвечающий выдуманное."""
    for name, env in _environments(contour).items():
        mock = env.get("WMS_MOCK")
        if mock is None:
            continue
        assert mock.lower() == "false", (
            f"{name}: WMS_MOCK={mock}. На бою заглушка склада запрещена")


def test_every_service_says_it_is_production(contour: dict) -> None:
    """`APP_ENV` — не про логи, а про запреты.

    От него зависят `wb.writes_allowed`, отказ identity работать без ключа и
    отказ биллинга отдавать деньги неизвестно кому. `development` на бою снимает
    эти запреты разом.
    """
    wrong = [name for name, env in _environments(contour).items()
             if env.get("APP_ENV") and env["APP_ENV"] != "production"]
    assert not wrong, f"не production у сервисов: {wrong}"


def test_nothing_in_the_production_contour_has_a_default(contour: dict) -> None:
    """Умолчаний нет ни у одной переменной, кроме образа и SHA.

    `${ПЕРЕМЕННАЯ:-значение}` на бою означает «кто-то забыл, а контур встал и
    сделал что-то другое». Обязательная форма `${ПЕРЕМЕННАЯ:?...}` роняет
    подъём с внятной причиной.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    allowed = {"GIT_SHA", "POSTGRES_USER", "BACKUP_KEEP_DAYS"}
    sloppy = []
    for line in text.splitlines():
        if line.lstrip().startswith("#") or ":-" not in line:
            continue
        for chunk in line.split("${")[1:]:
            name = chunk.split(":-")[0].split("}")[0].split(":?")[0]
            if ":-" in chunk.split("}")[0] and name not in allowed:
                sloppy.append(f"{name} (в строке: {line.strip()[:60]})")
    assert not sloppy, "переменные с умолчанием в боевом контуре: " + "; ".join(sloppy)


def test_migrations_run_as_the_owner_and_services_do_not(contour: dict) -> None:
    """Миграции — владельцем, сервисы — своей ролью.

    Миграция создаёт таблицы, и роль сервиса этого делать не может: в этом и
    смысл разделения. Перепутать легко, и перепутанное молчит до первой
    миграции на бою.
    """
    for name, env in _environments(contour).items():
        dsn = env.get("DATABASE_URL")
        if not dsn:
            continue
        migrator = name.endswith("migrate")
        owned = "${POSTGRES_USER" in dsn or dsn.startswith("postgresql://wms:")
        if migrator:
            assert owned, f"{name}: миграции идут не владельцем — они не создадут таблицы"
        else:
            assert "svc_" in dsn, (
                f"{name}: сервис ходит в базу не своей ролью ({dsn.split('@')[0]}…). "
                f"Суперпользователь у сервиса — это ошибка портала, роняющая "
                f"таблицу склада")


def test_the_stand_gate_is_not_copied_into_production(contour: dict) -> None:
    """Стендовых ворот на бою быть не должно.

    `check-no-live-tokens.sh` запрещает живой токен. На стенде это защита, на
    бою — запрет подниматься вовсе, и причину искали бы в сети.
    """
    body = "\n".join(line for line in COMPOSE.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    assert "check-no-live-tokens" not in body
    assert "token-guard" not in (contour.get("services") or {})


def test_the_production_contour_keeps_its_backups_and_its_alerts(contour: dict) -> None:
    """Бэкап и алерты — часть контура, а не то, что настроят потом.

    Раздел 3.5: боевой контур сегодня болен ровно этим — «работает» и «молчит»
    в нём неразличимы.
    """
    services = set((contour.get("services") or {}).keys())
    assert "pg-backup" in services, "в боевом контуре нет бэкапа"
    assert "alertmanager" in services, "в боевом контуре некому позвонить дежурному"
    assert "prometheus" in services, "в боевом контуре нечем измерять"
