"""Уборка после прогона убирает за собой на самом деле.

Оба случая ниже прошли мимо зелёного прогона и накопили мусор на стенде за
сутки с лишним: 267 начислений клиента прогона в биллинге и 195 сессий
подбора в `wms`. Ни один шаг раздела 9.6 их не проверял — прогон смотрит на
то, что создал, и не смотрит на то, что после себя оставил.

Обе жалобы уборка печатала. Печатала в `print`, который при зелёном прогоне
`pytest -q` не показывает, — то есть молчала.

Подготовка здесь везде «взять или завести»: клиент прогона в базе либо есть,
либо нет, и тест обязан работать в обоих случаях. Настаивать на `INSERT`
значило бы падать ровно там, где мусор от прошлой уборки ещё лежит, — то
есть именно в той ситуации, ради которой тест написан.
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import _purge_billing, _purge_run_data
from runner import Db
from scenario import SELLER


def _billing() -> Db:
    dsn = os.getenv("BILLING_DATABASE_URL")
    if not dsn:
        pytest.skip("BILLING_DATABASE_URL не задан")
    return Db(dsn)


def _cabinet_of_the_run(billing: Db) -> uuid.UUID:
    """Кабинет клиента прогона: взять существующий или завести.

    Имя НЕ переписывается: кабинет может быть настоящим кабинетом прогона, и
    переименовывать чужую строку справочника тест права не имеет. `DO UPDATE`
    здесь только ради `RETURNING id` — `DO NOTHING` его не отдаёт.
    """
    row = billing.row(
        "INSERT INTO cabinet (id, seller_external_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (seller_external_id) DO UPDATE SET seller_external_id = EXCLUDED.seller_external_id "
        "RETURNING id",
        (uuid.uuid4(), SELLER, "кабинет проверки уборки"))
    assert row is not None
    return row["id"]


def _partner_under_the_root(billing: Db, partner: uuid.UUID) -> None:
    """Партнёр теста — ВСЕГДА под корнем дерева, никогда не корень сам.

    Партнёр без родителя — корень. Биллинг заводит кабинет, приехавший
    событием, на единственный корень и отказывается гадать, когда корней
    несколько (`_default_partner`): кабинет остаётся без партнёра, наценка
    обнуляется. Шесть таких партнёров, оставшихся от упавших посреди прогонов
    тестов, покрасили шаг 12 — упаковка пришла 30/0/30 вместо 45/15/30.

    Защита биллинга сработала как задумано. Ошибка была в тесте, который мог
    незаметно менять форму дерева партнёров на стенде.
    """
    root = billing.row("SELECT id FROM partner WHERE parent_id IS NULL AND active LIMIT 1")
    billing.execute(
        "INSERT INTO partner (id, name, parent_id) VALUES (%s, %s, %s) "
        "ON CONFLICT DO NOTHING",
        (partner, "партнёр проверки уборки", root["id"] if root else None))


def _an_accrual_with_its_split(billing: Db, cabinet: uuid.UUID,
                               partner: uuid.UUID) -> uuid.UUID:
    """Начисление 45.00 с наценкой 15.00, разложенной на одного партнёра.

    Раскладка сходится с наценкой до копейки — иначе отложенный триггер не
    даст записать и саму подготовку.
    """
    accrual = uuid.uuid4()
    _partner_under_the_root(billing, partner)
    billing.execute(
        "INSERT INTO billing_accrual "
        "  (id, event_id, event_type, tenant_id, cabinet_id, seller_external_id, "
        "   service, quantity, unit_price, markup, amount, partner_id, partner_amount, "
        "   occurred_on) "
        "VALUES (%s, %s, 'wms.packing.completed.v1', 'stand', %s, %s, "
        "        'packing', 1, 30.00, 15.00, 45.00, %s, 15.00, CURRENT_DATE)",
        (accrual, uuid.uuid4(), cabinet, SELLER, partner))
    billing.execute(
        "INSERT INTO billing_commission (id, accrual_id, partner_id, markup, amount) "
        "VALUES (%s, %s, %s, 15.00, 15.00)",
        (uuid.uuid4(), accrual, partner))
    return accrual


def _forget(billing: Db, accrual: uuid.UUID, partner: uuid.UUID) -> None:
    """Убрать за тестом. Начисление уносит свою раскладку каскадом, поэтому
    отложенному триггеру на коммите проверять уже нечего."""
    billing.execute("DELETE FROM billing_accrual WHERE id = %s", (accrual,))
    billing.execute("DELETE FROM partner WHERE id = %s", (partner,))


def test_the_purge_takes_the_money_of_the_run_seller_with_it() -> None:
    """Начисление с разложенной комиссией убирается целиком.

    До фикса уборка шла пятью автокоммитами, и второй — удаление комиссий —
    не мог закоммититься: отложенный `billing_commission_matches_accrual`
    на коммите видел начисление с наценкой 15.00 и раскладкой 0.00 и
    отказывал. Исключение гасил `except`, до удаления начислений дело не
    доходило, и деньги синтетического клиента оставались в базе навсегда.
    """
    billing = _billing()
    partner = uuid.uuid4()
    accrual = _an_accrual_with_its_split(billing, _cabinet_of_the_run(billing), partner)
    try:
        _purge_billing([SELLER], "проверка")

        left = billing.row(
            "SELECT count(*) AS n FROM billing_accrual WHERE id = %s", (accrual,))
        assert left is not None and left["n"] == 0, (
            "начисление клиента прогона осталось в биллинге после уборки")
        split = billing.row(
            "SELECT count(*) AS n FROM billing_commission WHERE accrual_id = %s",
            (accrual,))
        assert split is not None and split["n"] == 0, "комиссия осталась без начисления"
    finally:
        _forget(billing, accrual, partner)
        billing.close()


def test_deleting_a_split_without_its_accrual_is_still_refused() -> None:
    """Причина, по которой уборка обязана быть одной транзакцией.

    Это не обходной путь, а требование схемы: начисление без раскладки —
    партнёр без части своих денег (находка 4.2). Убрать комиссии, оставив
    начисление, нельзя, и не должно стать можно: если этот тест однажды
    покраснеет, значит защиту 4.2 сняли.
    """
    billing = _billing()
    partner = uuid.uuid4()
    accrual = _an_accrual_with_its_split(billing, _cabinet_of_the_run(billing), partner)
    try:
        with pytest.raises(Exception, match="раскладка комиссии"):
            billing.execute(
                "DELETE FROM billing_commission WHERE accrual_id = %s", (accrual,))
    finally:
        _forget(billing, accrual, partner)
        billing.close()


def test_the_purge_takes_the_picking_sessions_it_opened(db: Db) -> None:
    """Сессия подбора прогона не остаётся сиротой.

    У `pick_session` нет владельца: связь с прогоном живёт только в строках
    листа. До фикса уборка удаляла строки и на этом останавливалась — сессия
    оставалась, и отличить её от настоящей было уже нечем. Шаг 8 открывает
    пять сессий за прогон; к концу аудита их накопилось 195.
    """
    account, sku, task, session = (uuid.uuid4(), uuid.uuid4(),
                                   uuid.uuid4(), uuid.uuid4())
    owner_row = db.row(
        "INSERT INTO owner (id, seller_external_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (seller_external_id) DO UPDATE SET name = EXCLUDED.name "
        "RETURNING id",
        (uuid.uuid4(), SELLER, "клиент проверки уборки"))
    assert owner_row is not None
    owner = owner_row["id"]

    db.execute(
        "INSERT INTO wb_account (id, owner_id, external_id, display_name, secret_ref) "
        "VALUES (%s, %s, %s, %s, 'development-only-purge-check')",
        (account, owner, f"purge-{account}", "кабинет проверки уборки"))
    db.execute(
        "INSERT INTO sku (id, owner_id, barcode) VALUES (%s, %s, %s)",
        (sku, owner, f"purge-{sku}"))
    db.execute(
        "INSERT INTO wms_task (id, wb_order_id, wb_account_id, owner_id, quantity) "
        "VALUES (%s, %s, %s, %s, 1)",
        (task, uuid.uuid4().int % 10_000_000, account, owner))
    db.execute("INSERT INTO pick_session (id) VALUES (%s)", (session,))
    db.execute(
        "INSERT INTO pick_line (id, session_id, task_id, owner_id, sku_id, qty) "
        "VALUES (%s, %s, %s, %s, %s, 1)",
        (uuid.uuid4(), session, task, owner, sku))

    _purge_run_data(db, "проверка")

    left = db.row("SELECT count(*) AS n FROM pick_session WHERE id = %s", (session,))
    assert left is not None and left["n"] == 0, (
        "сессия подбора прогона осталась в базе после уборки")


def test_the_purge_finds_the_money_by_the_owner_id_not_only_by_the_name(db: Db) -> None:
    """Начисление, заведённое на `owner_id`, тоже убирается.

    В биллинге кабинет прогона заведён не на `full-run-seller`, а на
    идентификатор владельца из события — событие несёт `owner_id`, а внешнего
    имени клиента в нём нет. Уборка искала по имени и этих начислений не
    видела: каждый запуск оставлял по кабинету и по пачке начислений, и к
    концу аудита их набралось 975.
    """
    billing = _billing()
    owner_row = db.row(
        "INSERT INTO owner (id, seller_external_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (seller_external_id) DO UPDATE SET name = EXCLUDED.name "
        "RETURNING id",
        (uuid.uuid4(), SELLER, "клиент проверки уборки"))
    assert owner_row is not None
    owner = str(owner_row["id"])

    cabinet_row = billing.row(
        "INSERT INTO cabinet (id, seller_external_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (seller_external_id) DO UPDATE "
        "   SET seller_external_id = EXCLUDED.seller_external_id RETURNING id",
        (uuid.uuid4(), owner, f"кабинет {owner}"))
    assert cabinet_row is not None
    partner = uuid.uuid4()
    accrual = uuid.uuid4()
    # Кабинет справочника должен существовать: ниже проверяется, что уборка
    # его НЕ трогает, а «нет строки» и «строку снесли» — разные вещи.
    _cabinet_of_the_run(billing)
    _partner_under_the_root(billing, partner)
    billing.execute(
        "INSERT INTO billing_accrual "
        "  (id, event_id, event_type, tenant_id, cabinet_id, seller_external_id, "
        "   service, quantity, unit_price, markup, amount, partner_id, partner_amount, "
        "   occurred_on) "
        "VALUES (%s, %s, 'wms.packing.completed.v1', 'stand', %s, %s, "
        "        'packing', 1, 30.00, 15.00, 45.00, %s, 15.00, CURRENT_DATE)",
        (accrual, uuid.uuid4(), cabinet_row["id"], owner, partner))
    try:
        _purge_run_data(db, "проверка")

        left = billing.row(
            "SELECT count(*) AS n FROM billing_accrual WHERE id = %s", (accrual,))
        assert left is not None and left["n"] == 0, (
            "начисление, заведённое на идентификатор владельца, осталось в биллинге")

        # И сам кабинет: он заведён на владельца этого запуска, следующий
        # заведёт свой. Не удалять — значит копить по кабинету за прогон, а с
        # ним закрепление менеджера; к концу аудита их набралось 155.
        cabinet_left = billing.row(
            "SELECT count(*) AS n FROM cabinet WHERE seller_external_id = %s", (owner,))
        assert cabinet_left is not None and cabinet_left["n"] == 0, (
            "кабинет, заведённый на владельца прогона, остался в биллинге")

        # А кабинет по внешнему имени клиента — строка справочника, одна на
        # все запуски, и трогать её уборка не должна.
        named = billing.row(
            "SELECT count(*) AS n FROM cabinet WHERE seller_external_id = %s", (SELLER,))
        assert named is not None and named["n"] == 1, (
            "уборка снесла кабинет справочника, общий для всех запусков")
    finally:
        billing.execute("DELETE FROM billing_accrual WHERE id = %s", (accrual,))
        billing.execute("DELETE FROM partner WHERE id = %s", (partner,))
        billing.close()


def test_the_stand_keeps_exactly_one_root_partner(db: Db) -> None:
    """У дерева партнёров стенда ровно один корень.

    Не про уборку, а про то, что тест не имеет права оставить стенд сломанным.
    Партнёр без родителя — корень; при двух корнях биллинг перестаёт гадать,
    на кого записать кабинет, приехавший событием, и наценка молча становится
    нулевой. Видно это только на шаге 12, сообщением «упаковка 30/0/30», по
    которому причину не найти.
    """
    billing = _billing()
    try:
        roots = billing.rows(
            "SELECT id, name FROM partner WHERE parent_id IS NULL AND active ORDER BY name")
        assert len(roots) == 1, (
            "корней в дереве партнёров "
            f"{len(roots)}: {', '.join(str(r['name']) for r in roots)}. "
            "Биллинг заводит кабинет, пришедший событием, на единственный "
            "корень и при нескольких отказывается гадать — наценка обнулится")
    finally:
        billing.close()


def test_the_purge_takes_a_session_that_never_got_a_line(db: Db) -> None:
    """Сессия, которой не досталось задания, тоже убирается.

    Шаг 8 открывает пять сессий, а задание достаётся не каждой. Такая сессия
    не оставляет следа в `pick_line` — по владельцу её не найти вовсе, и
    поиск по строкам из теста выше её не видит. Отсюда и брались 195 сирот
    в состоянии `picking`.
    """
    stale, fresh = uuid.uuid4(), uuid.uuid4()
    db.execute(
        "INSERT INTO pick_session (id, state, started_at) "
        "VALUES (%s, 'picking', now() - interval '2 hours')", (stale,))
    # Свежая пустая сессия — это сборщик, который только что встал к стойке.
    # Её уборка трогать не имеет права.
    db.execute("INSERT INTO pick_session (id, state) VALUES (%s, 'open')", (fresh,))
    try:
        _purge_run_data(db, "проверка")

        gone = db.row("SELECT count(*) AS n FROM pick_session WHERE id = %s", (stale,))
        assert gone is not None and gone["n"] == 0, (
            "пустая сессия двухчасовой давности осталась в базе")
        alive = db.row("SELECT count(*) AS n FROM pick_session WHERE id = %s", (fresh,))
        assert alive is not None and alive["n"] == 1, (
            "уборка снесла сессию, которую сборщик только что открыл")
    finally:
        db.execute("DELETE FROM pick_session WHERE id IN (%s, %s)", (stale, fresh))
