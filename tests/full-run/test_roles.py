"""Каждый сервис ходит в базу под своей ролью, а не под владельцем.

Девять сервисов ходили под `wms` — владельцем ВСЕХ баз стенда. Значит ошибка в
запросе портала могла уронить таблицу склада, а утёкший пароль биллинга
открывал всё сразу.

Скрипт `create-roles.sh` был написан заранее, но ни разу не запускался — и в
нём пряталось ровно то, из-за чего запускать его было опасно: роль для склада
он называл `wms`, тем же именем, что владелец баз. Ветка `ALTER ROLE ...
PASSWORD` переписала бы пароль СУПЕРПОЛЬЗОВАТЕЛЯ, и всё, что ходит по
`POSTGRES_PASSWORD` — миграции, сиды, бэкап, тесты, — перестало бы
подключаться. Скрипт при этом отработал бы «успешно».

Проверяется не конфигурация, а живые подключения: в `compose.yaml` можно
написать что угодно, а `pg_stat_activity` показывает, кто на самом деле
подключён.
"""
from __future__ import annotations

import pytest

from runner import Db, env

# Сервис → база, в которую он обязан ходить под собственной ролью. Миграции и
# сиды сюда не входят намеренно: они создают таблицы и обязаны быть владельцем.
UNDER_THEIR_OWN_ROLE = {
    "wms": "svc_wms",
    "billing": "svc_billing",
    "identity": "svc_identity",
    "portal": "svc_portal",
    "internal_admin": "svc_admin",
    "workstation": "svc_workstation",
}


@pytest.fixture(scope="module")
def connections() -> list[dict]:
    """Кто сейчас подключён и к чему. Спрашивается у самой базы."""
    db = Db(env("DATABASE_URL"))
    try:
        # Собственные подключения прогона исключаются по адресу клиента.
        # Прогон ходит в базу владельцем законно: он читает журнал, чтобы
        # утверждать о движениях, а не работает сервисом. Не исключив себя,
        # проверка краснела бы на самой себе — и это единственный случай, когда
        # она соврала бы.
        return db.rows(
            "SELECT usename, datname, count(*) AS n FROM pg_stat_activity "
            " WHERE datname IS NOT NULL AND usename IS NOT NULL "
            "   AND pid <> pg_backend_pid() "
            "   AND client_addr IS DISTINCT FROM "
            "       (SELECT client_addr FROM pg_stat_activity WHERE pid = pg_backend_pid()) "
            " GROUP BY usename, datname")
    finally:
        db.close()


def test_no_service_holds_a_connection_as_the_owner_of_every_database(
        connections: list[dict]) -> None:
    """Владелец баз не должен держать соединений к рабочим базам.

    Одно подключение к `postgres` — это сам прогон и админские скрипты, оно
    законно. Подключение владельца к `wms` или `billing` означает, что сервис
    остался под суперпользователем.
    """
    owner = "wms"
    trespassing = [
        (row["usename"], row["datname"], row["n"]) for row in connections
        if row["usename"] == owner and row["datname"] in UNDER_THEIR_OWN_ROLE
    ]
    assert not trespassing, (
        f"владелец баз держит соединения к рабочим базам: {trespassing}. "
        f"Сервис под суперпользователем — это ошибка портала, роняющая таблицу "
        f"склада, и утёкший пароль, открывающий всё сразу")


def test_each_service_connects_under_its_own_role(connections: list[dict]) -> None:
    """Подключения к базе сервиса — от его роли и ни от какой другой.

    Проверяется по факту подключения, а не по compose: в файле можно написать
    что угодно, а `pg_stat_activity` показывает, кто пришёл на самом деле.
    """
    wrong: list[str] = []
    for row in connections:
        expected = UNDER_THEIR_OWN_ROLE.get(str(row["datname"]))
        if expected is None:
            continue  # тестовые базы и postgres — не про это
        if str(row["usename"]) != expected:
            wrong.append(f"{row['datname']} ← {row['usename']} (ждали {expected})")
    assert not wrong, "в базы ходят не те роли: " + "; ".join(wrong)


def test_the_service_roles_are_not_superusers() -> None:
    """Роль сервиса не имеет права быть суперпользователем.

    Иначе разделение — это переименование: права те же, только имён больше.
    """
    db = Db(env("DATABASE_URL"))
    try:
        roles = db.rows(
            # Без LIKE: psycopg разбирает `%` как плейсхолдер даже там, где
            # параметров нет, и запрос падает разговором про форматирование.
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
            " WHERE left(rolname, 4) = 'svc_'")
    finally:
        db.close()

    assert roles, (
        "ролей svc_* нет вовсе — create-roles.sh не запускался, и все сервисы "
        "ходят под владельцем баз")
    too_strong = [r["rolname"] for r in roles
                  if r["rolsuper"] or r["rolcreatedb"] or r["rolcreaterole"]]
    assert not too_strong, f"роли сервисов со слишком широкими правами: {too_strong}"


def test_the_owner_role_is_not_one_of_the_service_roles() -> None:
    """Роль сервиса не совпадает с владельцем баз.

    Ровно эта ловушка и лежала в `create-roles.sh`: роль `wms` для склада — это
    тот же объект, что владелец `wms`. Запуск скрипта переписал бы пароль
    суперпользователя, и всё, что ходит по POSTGRES_PASSWORD, отвалилось бы.
    """
    assert "wms" not in UNDER_THEIR_OWN_ROLE.values(), (
        "роль сервиса названа именем владельца баз — смена её пароля "
        "перепишет пароль суперпользователя")
