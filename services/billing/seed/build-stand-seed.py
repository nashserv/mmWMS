#!/usr/bin/env python3
"""Генератор сида биллинга для стенда.

Читает уже посеянных продавцов и кабинеты прямо из `reference/stand-seed/seed-stand.sql`
потока 0 и раскладывает их по дереву партнёров. Новых идентификаторов не
выдумывает: `seller_external_id`, `external_id` кабинета WB и uuid владельца
берутся оттуда как есть, иначе данные потоков разойдутся и «кабинет 07» в
биллинге окажется другим кабинетом, чем «кабинет 07» на складе.

Запуск:
    python3 services/billing/seed/build-stand-seed.py
"""
from __future__ import annotations

import pathlib
import re
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[3]
SOURCE = ROOT / "reference" / "stand-seed" / "seed-stand.sql"
TARGET = pathlib.Path(__file__).resolve().parent / "stand-billing.sql"
# Роли стенда генерируются здесь же: у партнёра и его пользователя обязан быть
# один и тот же uuid в двух базах, иначе ветка прав не сойдётся с деревом.
IDENTITY_TARGET = ROOT / "services" / "identity" / "seed" / "stand-roles.sql"

# Пространство имён для устойчивых uuid: повторный запуск генератора обязан
# дать те же идентификаторы, иначе сид перестанет быть идемпотентным.
NAMESPACE = uuid.UUID("6d6d7800-0000-4000-8000-000000626c6e")


# Услуги, на которые старший менеджер ставит наценку. Хранение не входит —
# см. комментарий в сгенерированном файле.
MARKUP_SERVICES = ("receiving", "labeling", "packing", "picking", "shipping",
                   "returns", "order_processing")

# (код, услуга, название, единица, цена MM-Express, цена-заглушка?)
#
# 30 ₽ стоят операции, о которых мастер говорит прямо (раздел 1): сборка,
# упаковка, отгрузка, обработка заказов. С наценкой партнёра клиент платит 45 —
# ровно то, что проверяет шаг 12 полного прогона.
TARIFFS = (
    ("receiving-default",        "receiving",        "Приёмка",             "шт",                    15.00, True),
    ("storage-default",          "storage",          "Хранение",            "коробко-место × сутки",  5.00, True),
    ("labeling-default",         "labeling",         "Стикеровка",          "шт",                    10.00, True),
    ("packing-default",          "packing",          "Упаковка",            "шт",                    30.00, False),
    ("picking-default",          "picking",          "Сборка",              "заказ",                 30.00, False),
    ("shipping-default",         "shipping",         "Отгрузка",            "заказ в поставке",      30.00, False),
    ("returns-default",          "returns",          "Возврат",             "шт",                    25.00, True),
    ("order-processing-default", "order_processing", "Обработка заказов",   "заказ",                 30.00, False),
)

# (тип события, услуга, путь к количеству, включено, зачем/почему нет)
BILLABLE = (
    ("wb.supply.shipped.v1", "shipping", "orders", True,
     "Тарифицируется на проде сегодня. orders — заказов в поставке, тарифицируемое "
     "количество (приложение E мастера)."),
    ("order.packed.v1", "packing", None, True,
     "Тарифицируется на проде сегодня. Одно событие — одна упаковка."),
    ("wb.orders.processed.v1", "order_processing", "orders", True,
     "Тарифицируется на проде сегодня. Количество — orders, форма payload "
     "зафиксирована в приложении E мастера (версия 1.3). До неё считали "
     "единицей, и при пачке заказов клиент был бы недосчитан."),
    ("wms.label.attached.v1", "labeling", None, True,
     "Стикеровка сегодня бесплатна (раздел 3.4). Владелец в payload обязателен "
     "с версии 1.3 мастера — до неё строки уходили в billing_unbilled с "
     "SELLER_UNKNOWN."),
    ("wms.return.received.v1", "returns", None, True,
     "Возвраты сегодня не тарифицируются (раздел 3.4)."),
    ("wms.packing.completed.v1", "packing", None, False,
     "Выключено: то же физическое действие, что order.packed.v1 рабочего места. "
     "Включить только вместе с выключением того — иначе двойной счёт клиенту."),
    ("wms.picking.completed.v1", "picking", None, False,
     "Выключено: подбор оплачивается сборкой заказа, а не отдельно. Включить, "
     "когда владелец подтвердит сборку отдельной услугой."),
    ("wms.item.scanned.v1", "picking", None, False,
     "Выключено: скан у стойки — шаг внутри подбора, а не услуга. В выработку "
     "смены он идёт, в счёт клиенту — нет."),
    ("wms.receipt.completed.v1", "receiving", "accepted_qty", True,
     "Приёмка. Событие заведено в версии 1.3 мастера: до него тарифицировать "
     "её было нечем. Количество — accepted_qty, принятое ПО ФАКТУ: приёмка с "
     "недостачей оплачивается тем, что реально легло на полку."),
    ("inventory.movement.recorded.v1", "receiving", None, False,
     "Выключено: движение товара сопровождает почти всё и выставится дважды. "
     "Приёмку тарифицирует wms.receipt.completed.v1 — одно событие на документ, "
     "а не на каждую строку."),
)


def stable(kind: str, key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{kind}:{key}"))


def user_of(partner_id: str) -> str:
    """Пользователь identity, под которым партнёр входит в кабинет."""
    return stable("user", partner_id)


def owners_from_seed(text: str) -> list[tuple[str, str, str, str]]:
    """(owner_id, seller_external_id, name, inn) — в порядке файла."""
    block = re.search(r"INSERT INTO owner \([^)]*\) VALUES\s*(.*?);", text, re.S)
    if not block:
        raise SystemExit("в сиде потока 0 не найден блок owner")
    rows = re.findall(
        r"\('([0-9a-f-]{36})',\s*'([^']*)',\s*'([^']*)',\s*'([^']*)'", block.group(1))
    return rows


def accounts_from_seed(text: str) -> list[tuple[str, str, str]]:
    """(owner_id, external_id, display_name)."""
    block = re.search(r"INSERT INTO wb_account \([^)]*\) VALUES\s*(.*?);", text, re.S)
    if not block:
        raise SystemExit("в сиде потока 0 не найден блок wb_account")
    rows = re.findall(
        r"\('[0-9a-f-]{36}',\s*'([0-9a-f-]{36})',\s*'([^']*)',\s*'([^']*)'", block.group(1))
    return rows


def quote(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def main() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    owners = owners_from_seed(text)
    accounts = accounts_from_seed(text)

    # Дерево: один корень (раздел 13, вопрос 7 — сегодня старший менеджер один)
    # и три менеджера кабинетов под ним. Три, а не один, чтобы права «менеджер
    # видит только своих» было на чём проверить.
    root = ("Зардал", stable("partner", "zardal"), None)
    managers = [(f"Менеджер кабинетов {index}", stable("partner", f"manager-{index}"), root[1])
                for index in (1, 2, 3)]

    out: list[str] = []
    add = out.append
    add("-- Сид биллинга для стенда. Сгенерирован services/billing/seed/build-stand-seed.py.")
    add("-- Продавцы и кабинеты взяты из reference/stand-seed/seed-stand.sql потока 0:")
    add(f"--   {len(owners)} продавцов, {len(accounts)} кабинетов WB, те же идентификаторы.")
    add("-- Секретов нет: secret_ref — заведомые заглушки стенда, токенов биллинг не хранит.")
    add("")
    add("BEGIN;")
    add("")
    add("-- === Дерево партнёров ===")
    add("-- user_id — тот же пользователь, которому identity выдаёт роль на ветку.")
    add("INSERT INTO partner (id, parent_id, name, user_id) VALUES")
    rows = [f"    ({quote(root[1])}, NULL, {quote(root[0])}, {quote(user_of(root[1]))})"]
    rows += [f"    ({quote(pid)}, {quote(parent)}, {quote(name)}, {quote(user_of(pid))})"
             for name, pid, parent in managers]
    add(",\n".join(rows))
    # Заполняем пустой user_id и не трогаем проставленный: повторный сид — это
    # обновление стенда, а не откат того, что администратор поправил руками.
    add("ON CONFLICT (id) DO UPDATE SET user_id = COALESCE(partner.user_id, EXCLUDED.user_id);")
    add("")

    add("-- === Кабинеты ===")
    add("-- cabinet.wms_owner_id — тот же uuid владельца, что в базе wms: события")
    add("-- физического действия несут его, а не внешний ключ продавца.")
    add("INSERT INTO cabinet (id, seller_external_id, wms_owner_id, name, inn, active) VALUES")
    cabinet_rows = []
    for owner_id, seller, name, inn in owners:
        cabinet_rows.append(
            f"    ({quote(stable('cabinet', seller))}, {quote(seller)}, {quote(owner_id)}, "
            f"{quote(name)}, {quote(inn)}, true)")
    add(",\n".join(cabinet_rows))
    add("ON CONFLICT (seller_external_id) DO NOTHING;")
    add("")

    by_owner = {owner_id: seller for owner_id, seller, _, _ in owners}
    add("-- === Кабинеты Wildberries ===")
    add("-- У двух владельцев их по два — поэтому отдельная таблица, а не поле.")
    add("INSERT INTO cabinet_wb_account (id, cabinet_id, external_id, display_name, secret_ref) VALUES")
    account_rows = []
    for owner_id, external_id, display_name in accounts:
        seller = by_owner.get(owner_id)
        if seller is None:
            continue
        account_rows.append(
            f"    ({quote(stable('wb', external_id))}, {quote(stable('cabinet', seller))}, "
            f"{quote(external_id)}, {quote(display_name)}, "
            f"{quote('stand-fake-secret-' + external_id.split('-')[-1])})")
    add(",\n".join(account_rows))
    add("ON CONFLICT (external_id) DO NOTHING;")
    add("")

    add("-- === Закрепление кабинетов за менеджерами ===")
    add("-- Раскладка по трём менеджерам синтетическая: кто из реальных менеджеров")
    add("-- ведёт какой кабинет, знает владелец, и на стенде этих данных нет и быть")
    add("-- не должно. История закреплений от этого не страдает — важна её форма.")
    add("INSERT INTO cabinet_assignment (id, cabinet_id, partner_id, role, from_date, comment) VALUES")
    assignment_rows = []
    for index, (_, seller, _, _) in enumerate(owners):
        manager = managers[index % len(managers)]
        assignment_rows.append(
            f"    ({quote(stable('assign', seller))}, {quote(stable('cabinet', seller))}, "
            f"{quote(manager[1])}, 'account_manager', DATE '2026-01-01', "
            f"{quote('сид стенда: раскладка по менеджерам синтетическая')})")
    add(",\n".join(assignment_rows))
    add("ON CONFLICT (id) DO NOTHING;")
    add("")

    add("-- === Наценка партнёра ===")
    add("-- 15 ₽ с операции у старшего менеджера (раздел 1 мастера).")
    add("-- У менеджеров кабинетов своей наценки нет — допущение раздела 13, вопрос 6.")
    add("-- Выражено отсутствием строки, а не нулём: ноль читался бы как решение.")
    add("--")
    add("-- Хранения в списке нет намеренно: мастер говорит «наценка 15 ₽ с операции»,")
    add("-- а коробко-место × сутки — не операция. Ставит ли партнёр наценку на")
    add("-- хранение, знает владелец (продолжение раздела 13, вопрос 5). До ответа")
    add("-- хранение тарифицируется без наценки, и это видно в расшифровке счёта.")
    add("INSERT INTO price_layer (id, partner_id, cabinet_id, service, markup, from_date) VALUES")
    layer_rows = [
        f"    ({quote(stable('layer', f'zardal-{service}'))}, {quote(root[1])}, NULL, "
        f"'{service}', 15.00, DATE '2026-01-01')"
        for service in MARKUP_SERVICES]
    add(",\n".join(layer_rows))
    add("ON CONFLICT (id) DO NOTHING;")
    add("")

    add("-- === Тарифы на все услуги ===")
    add("-- Цены — заглушки: раздел 13, вопрос 3 («список услуг с ценами») владельцем")
    add("-- не закрыт, и допущение того же вопроса разрешает строить биллинг на них")
    add("-- с правкой в админке. Заглушкой не является только 30 ₽ за операцию —")
    add("-- это тариф MM-Express из раздела 1, и вместе с наценкой партнёра он даёт")
    add("-- те самые 45 ₽, которые платит клиент.")
    add("INSERT INTO billing_tariff (id, code, service, name, unit, is_default) VALUES")
    add(",\n".join(
        f"    ({quote(stable('tariff', code))}, {quote(code)}, '{service}', {quote(name)}, "
        f"{quote(unit)}, true)"
        for code, service, name, unit, _price, _stub in TARIFFS))
    add("ON CONFLICT (code) DO NOTHING;")
    add("")
    add("-- partner_fee — ставка последней надежды для кабинета, которому слой")
    add("-- наценки ещё не завели (так наценка жила в биллинге as-is, раздел 2.10).")
    add("-- У хранения она ноль, и это решение, а не забывчивость: мастер говорит")
    add("-- «15 ₽ с операции», а коробко-место × сутки — не операция. Оставить здесь")
    add("-- 15 значило бы протащить наценку на хранение через запасной путь, ровно")
    add("-- мимо решения не ставить её до ответа владельца.")
    add("INSERT INTO billing_tariff_version (id, tariff_id, effective_from, approved,")
    add("                                    approved_by, approved_at, partner_fee) VALUES")
    add(",\n".join(
        f"    ({quote(stable('version', code))}, {quote(stable('tariff', code))}, "
        f"DATE '2026-01-01', true, 'стенд: заглушка вместо подписи владельца', now(), "
        f"{'15.00' if service in MARKUP_SERVICES else '0.00'})"
        for code, service, *_ in TARIFFS))
    add("-- Ставку обновляем: повторный сид — обновление стенда, а не откат решения.")
    add("ON CONFLICT (id) DO UPDATE SET partner_fee = EXCLUDED.partner_fee;")
    add("")
    add("-- Одна ступень «и далее»: объёмных скидок владелец не называл.")
    add("INSERT INTO billing_tariff_tier (id, version_id, up_to, unit_price, minimum) VALUES")
    add(",\n".join(
        f"    ({quote(stable('tier', code))}, {quote(stable('version', code))}, NULL, "
        f"{price:.2f}, 0.00)"
        for code, _service, _name, _unit, price, _stub in TARIFFS))
    add("ON CONFLICT (id) DO NOTHING;")
    add("")

    add("-- === Что тарифицируется ===")
    add("-- Список данными, а не цепочкой if'ов: сегодня на проде тарифицируются")
    add("-- четыре источника из десятков, и увидеть это можно только чтением кода.")
    add("INSERT INTO billing_billable_event (event_type, service, quantity_path, active, comment) VALUES")
    add(",\n".join(
        f"    ({quote(event)}, '{service}', {quote(path)}, {'true' if active else 'false'}, "
        f"{quote(comment)})"
        for event, service, path, active, comment in BILLABLE))
    add("ON CONFLICT (event_type) DO NOTHING;")
    add("")
    add("COMMIT;")
    add("")

    TARGET.write_text("\n".join(out), encoding="utf-8")
    print(f"{TARGET.name}: {len(owners)} кабинетов, {len(account_rows)} кабинетов WB, "
          f"{len(managers) + 1} партнёров")

    write_identity_seed(root, managers, [seller for _, seller, _, _ in owners])


def write_identity_seed(root: tuple[str, str, None],
                        managers: list[tuple[str, str, str]],
                        sellers: list[str]) -> None:
    """Сид ролей стенда.

    Партнёру выдаётся роль на ветку — на самого себя как корень. Отсюда и
    работает правило «менеджер видит своих, старший — свою ветку целиком»:
    ветка раскрывается рекурсивно, и отдельной роли «старший» для этого не
    нужно, нужно лишь, чтобы под ним кто-то был.

    Люди склада выдуманы: настоящих сотрудников на стенде нет и быть не должно.
    """
    out: list[str] = []
    add = out.append
    add("-- Роли стенда. Сгенерирован services/billing/seed/build-stand-seed.py вместе")
    add("-- с сидом биллинга: пользователь партнёра обязан совпадать в двух базах.")
    add("-- Люди склада выдуманы, настоящих сотрудников на стенде нет.")
    add("")
    add("BEGIN;")
    add("")
    add("INSERT INTO identity_role_grant (id, user_id, role_code, scope_kind, scope_id, granted_by) VALUES")

    rows = []
    for name, partner_id, role in [(root[0], root[1], "senior_manager")] + [
            (name, pid, "account_manager") for name, pid, _ in managers]:
        rows.append(
            f"    ({quote(stable('grant', partner_id))}, {quote(user_of(partner_id))}, "
            f"'{role}', 'partner_branch', {quote(partner_id)}, 'сид стенда')")
    for role, key in (("admin", "admin"), ("accountant", "accountant"),
                      ("warehouse_head", "warehouse-head"), ("picker", "picker-1"),
                      ("picker", "picker-2"), ("receiver", "receiver-1"),
                      ("logist", "logist-1")):
        rows.append(
            f"    ({quote(stable('grant', key))}, {quote(stable('user', key))}, "
            f"'{role}', 'global', NULL, 'сид стенда')")
    # Владелец на каждый кабинет: без него ЛК клиента показывать некому, а
    # область роли — единственное, откуда портал узнаёт, чей это кабинет.
    for seller in sellers:
        rows.append(
            f"    ({quote(stable('grant', 'owner-' + seller))}, "
            f"{quote(stable('user', 'owner-' + seller))}, "
            f"'owner', 'seller', {quote(seller)}, 'сид стенда')")
    add(",\n".join(rows))
    add("ON CONFLICT (id) DO NOTHING;")
    add("")
    add("COMMIT;")
    add("")

    IDENTITY_TARGET.parent.mkdir(parents=True, exist_ok=True)
    IDENTITY_TARGET.write_text("\n".join(out), encoding="utf-8")
    print(f"{IDENTITY_TARGET.name}: {len(rows)} выдач ролей")


if __name__ == "__main__":
    main()
