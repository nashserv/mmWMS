"""Полный прогон — 16 проверок раздела 9.6 мастер-контекста.

Порядок тестов и текст утверждений — из мастера, дословно. Это один сценарий
склада, разложенный на шестнадцать шагов, а не набор независимых проверок:
задания, заведённые шагом 4, подбираются шагом 8 и отгружаются шагом 11.

Владелец шага помечен маркером потока (`stream_a`, `stream_b`, `stream_c`) —
тем самым, что стоит в скобках у мастера. Срез одного потока:

    pytest -m stream_a

На старте потока 0 большинство шагов красные: `wms` — это mock (пункт 9 файла
`01-stream-0-contracts-and-stand.md`). Ни одного `skip` и ни одного `xfail`
здесь нет и появляться не должно — прогон существует, чтобы показывать, что
именно ещё не работает.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

import scenario as data
from runner import (Db, LoadResult, TxnWatch, Wms, env, lock_hold_histogram,
                    not_ready, percentile_from_buckets, port_is_open,
                    wait_until)

# Формат стикера берётся ИЗ ОКРУЖЕНИЯ, как и у сервиса. Жёстко вписанный
# `zplv` делал прогон красным ровно в тот день, когда владелец ответит на
# вопрос 2 раздела 13 и стенд переведут на png: прогон сказал бы «сломано»
# про исправную настройку.
STICKER_FORMAT = (os.getenv("WB_STICKER_FORMAT") or "zplv").strip()

# Задержка «WB → доступность в /tasks/pull» — менее 2 с, p99 (раздел 10).
TASK_VISIBLE_S = 2.0

# Раздел 6.2 отводит транзакции резерва 2–5 мс; вызов в Wildberries — около
# 500 мс. Порог в 100 мс отделяет одно от другого с большим запасом и совпадает
# с инвариантом 4 («удержание блокировки под 100 мс»).
TXN_LIMIT_MS = 100.0

# Шаг 10: от вызова до записи в устройство менее 50 мс.
PRINT_LIMIT_MS = 50.0

# Шаг 7: сколько ждать стикеры. Они тянутся фоново, пачкой до 100 штук, сразу
# после резерва (раздел 6.6) — то есть асинхронно по построению. Проверяется,
# что стикер лежит ДО начала подбора, а не что он появляется мгновенно:
# критерий раздела 10 — «стикер готов до начала упаковки > 99 %».
LABEL_READY_S = 10.0

# Шаг 16: запас пропускной способности — решение владельца 4.
TARGET_PER_HOUR = 10_000


def need(ctx: dict[str, Any], key: str, step: int) -> Any:
    """Достаёт то, что должен был подготовить предыдущий шаг.

    Пустое значение считается отсутствующим: ноль заданий не должен молча
    проходить проверку «ни одно задание не выдано двоим». Ложный зелёный в
    прогоне опаснее красного — он снимает блокировку мержа (правило 9.5.5).
    """
    if not ctx.get(key):
        not_ready(f"шаг {step} не дал «{key}»: чинить сначала его, этот шаг без него не выполним")
    return ctx[key]


def seed_wb_orders(account: str, count: int, barcode: str, deadline: str) -> list[int]:
    """Просит симулятор WB отдать задания и возвращает их номера у WB.

    Через ручку стенда `/__stand__/seed-orders`, а не через `/reservations`:
    шаг 4 проверяет путь «WB → задание в базе» целиком, включая опрос. Обход
    опроса сделал бы шаг зелёным ровно там, где в боевом контуре и теряются
    задания.
    """
    base = os.getenv("WB_SIMULATOR_URL", "http://127.0.0.1:8090").rstrip("/")
    try:
        response = httpx.post(f"{base}/__stand__/seed-orders", timeout=10.0, json={
            "account": account, "count": count, "barcode": barcode, "deadline": deadline})
    except httpx.HTTPError as failure:
        not_ready(f"симулятор WB не отвечает по {base} ({failure}); "
                  f"его поднимает стенд потока 0, задания брать неоткуда")
    if response.status_code >= 400:
        not_ready(f"симулятор WB ответил {response.status_code} на POST /__stand__/seed-orders")
    created = response.json().get("orders") or []
    if len(created) != count:
        not_ready(f"симулятор завёл {len(created)} заданий из {count}")
    return [int(order["id"]) for order in created]


# ============================================================ шаг 1 (C, A)

@pytest.mark.stream_c
@pytest.mark.stream_a
def test_step_01_opening_stock_lands_in_the_ledger_not_only_in_the_balance(
        wms: Wms, db: Db, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 1: завести клиента, ячейки, загрузить начальный остаток.

    ASSERT stock_balance == загруженному, stock_move содержит doc_type='opening'.

    Начальный остаток при переключении клиента даёт владелец компании (решение
    владельца 11) — инвентаризацию склада не проектируем, значит первый остаток
    обязан заезжать документом и оставлять след в журнале. Сравнение идёт по
    приросту, а не по абсолюту: прогон гоняется ежедневно, и на второй день
    равенство с абсолютом означало бы, что вчерашний остаток пропал.
    """
    wms.result("/sellers", {
        "seller_external_id": data.SELLER, "name": data.SELLER_NAME, "inn": data.SELLER_INN,
        "active": True, "allow_ledger_short": True})

    # Товары прогона, включая тот, что останется без остатка (шаг 6).
    for barcode in (*data.BARCODES, data.ZERO_STOCK_BARCODE):
        wms.result("/catalog/products/ensure", {
            "seller_external_id": data.SELLER, "barcode": barcode,
            "seller_sku": f"FR-{barcode[-4:]}", "name": f"Товар прогона {barcode[-4:]}"})

    # Кабинет WB: без него симулятору некуда отдавать задания (шаг 4).
    wms.result("/wb/accounts", {
        "op": "upsert", "external_id": data.WB_ACCOUNT, "seller_external_id": data.SELLER,
        "display_name": "Кабинет полного прогона", "secret_ref": data.WB_SECRET_REF,
        "mode": "shadow", "status": "ACTIVE"})

    # Клиент, товары и кабинет есть — дальше шаги могут идти своей дорогой,
    # даже если сам документ начального остатка не применится. Иначе один
    # красный в начале превратил бы весь прогон в пятнадцать одинаковых
    # «шаг 1 не дал», и день ушёл бы на диагностику вслепую.
    ctx["owner_ready"] = True

    before = {barcode: db.balance(data.SELLER, barcode) for barcode in data.BARCODES}

    reference = scenario.reference("opening")
    result = wms.result("/warehouse/documents", {
        "seller_external_id": data.SELLER, "warehouse_code": data.WAREHOUSE_CODE,
        "reference": reference, "doc_type": "opening",
        "comment": "начальный остаток от владельца компании",
        "lines": [{"barcode": barcode, "quantity": data.OPENING_QTY,
                   "cell_address": address, "state": "good"}
                  for barcode, (address, _) in zip(data.BARCODES, data.CELLS, strict=False)]})
    assert result.get("state") == "applied", f"документ начального остатка не применён: {result}"

    # Ячейки заведены: сборщику нужен адрес, а не «где-то на складе».
    for address, _ in data.CELLS:
        assert db.value("SELECT id FROM cell WHERE address = %s", (address,)), \
            f"ячейка {address} не заведена"

    for barcode in data.BARCODES:
        after = db.balance(data.SELLER, barcode)
        assert after - before[barcode] == data.OPENING_QTY, (
            f"{barcode}: stock_balance вырос на {after - before[barcode]}, "
            f"а загружено {data.OPENING_QTY}")

        opening = [move for move in db.moves(data.SELLER, barcode, doc_type="opening")
                   if move.get("doc_ref") == reference]
        assert opening, (f"{barcode}: в stock_move нет движения с doc_type='opening' "
                         f"и doc_ref={reference}")
        assert sum(int(move["qty"]) for move in opening) == data.OPENING_QTY

    ctx["opening_reference"] = reference


# ============================================================ шаг 2 (A, B)

@pytest.mark.stream_a
@pytest.mark.stream_b
def test_step_02_receipt_raises_the_balance_by_exactly_what_arrived(
        wms: Wms, db: Db, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 2: приёмка 3 SKU в коробки и ячейки.

    ASSERT баланс вырос ровно на принятое; каждая коробка имеет непустой comment.

    Комментарий у коробки — не украшение: через месяц на складе стоят сотни
    одинаковых коробок, и без пометки нужную не найти (раздел 2.9).
    """
    need(ctx, "owner_ready", 1)
    before = {barcode: db.balance(data.SELLER, barcode) for barcode in data.BARCODES}

    reference = scenario.reference("receipt")
    result = wms.result("/receipts", {
        "seller_external_id": data.SELLER, "warehouse_code": data.WAREHOUSE_CODE,
        "reference": reference, "seller_name": data.SELLER_NAME, "seller_inn": data.SELLER_INN,
        "lines": [{"barcode": barcode, "expected_qty": data.RECEIPT_QTY,
                   "actual_qty": data.RECEIPT_QTY, "box_barcode": box, "cell_address": cell,
                   "comment": comment}
                  for barcode, (box, cell, comment) in zip(data.BARCODES, data.BOXES, strict=False)]})
    assert result.get("state") in ("accepted", "counting"), f"приёмка не принята: {result}"

    for barcode in data.BARCODES:
        after = db.balance(data.SELLER, barcode)
        assert after - before[barcode] == data.RECEIPT_QTY, (
            f"{barcode}: баланс вырос на {after - before[barcode]}, принято {data.RECEIPT_QTY}")

    for box_barcode, cell_address, _ in data.BOXES:
        box = db.row("SELECT comment, cell_id FROM box WHERE barcode = %s", (box_barcode,))
        assert box, f"коробка {box_barcode} не заведена приёмкой"
        assert (box["comment"] or "").strip(), f"коробка {box_barcode} без комментария"
        assert box["cell_id"], f"коробка {box_barcode} не поставлена в ячейку {cell_address}"

    ctx["receipt_reference"] = reference


# ============================================================ шаг 3 (A, B)

@pytest.mark.stream_a
@pytest.mark.stream_b
def test_step_03_shortage_is_recorded_and_the_balance_follows_the_count(
        wms: Wms, db: Db, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 3: приёмка с недостачей.

    ASSERT discrepancy(kind='shortage') создан, баланс по факту, не по ожиданию.

    Подгонка факта под ожидание — это и есть та самая молчаливая ложь учёта,
    из-за которой на 21 продавца в боевом контуре осталось 92 единицы.
    """
    need(ctx, "owner_ready", 1)
    barcode = data.BARCODES[0]
    before = db.balance(data.SELLER, barcode)

    reference = scenario.reference("shortage")
    wms.result("/receipts", {
        "seller_external_id": data.SELLER, "warehouse_code": data.WAREHOUSE_CODE,
        "reference": reference,
        "lines": [{"barcode": barcode, "expected_qty": data.SHORTAGE_EXPECTED,
                   "actual_qty": data.SHORTAGE_ACTUAL,
                   "box_barcode": data.BOXES[0][0], "cell_address": data.BOXES[0][1],
                   "comment": "прогон: приёмка с недостачей"}]})

    after = db.balance(data.SELLER, barcode)
    assert after - before == data.SHORTAGE_ACTUAL, (
        f"баланс вырос на {after - before}: должен идти по факту "
        f"({data.SHORTAGE_ACTUAL}), а не по ожиданию ({data.SHORTAGE_EXPECTED})")

    discrepancy = db.row(
        "SELECT d.kind, d.qty FROM discrepancy d JOIN receipt r ON r.id = d.receipt_id "
        "WHERE r.reference = %s AND d.kind = 'shortage'", (reference,))
    assert discrepancy, f"по приёмке {reference} не создан discrepancy(kind='shortage')"
    assert int(discrepancy["qty"]) == data.SHORTAGE_EXPECTED - data.SHORTAGE_ACTUAL


# ============================================================== шаг 4 (A)

@pytest.mark.stream_a
def test_step_04_five_wb_orders_become_tasks_and_reservations_in_one_transaction(
        db: Db, ctx: dict[str, Any]) -> None:
    """Шаг 4: симулятор WB отдаёт 5 заданий.

    ASSERT 5 wms_task за < 2 с; 5 reservation; баланс good упал, reserved вырос.
    ASSERT ни одного HTTP-вызова внутри транзакции (проверка по трейсу).

    Трейс на стенде — pg_stat_activity: HTTP-вызов внутри транзакции оставляет
    однозначный отпечаток, бэкенд сидит `idle in transaction` и ждёт клиента,
    пока тот разговаривает с Wildberries. Наблюдение обязано что-то увидеть:
    пустой трейс — это не «нарушений нет», это «резерв в базу не пишется».
    """
    need(ctx, "owner_ready", 1)
    barcode = data.BARCODES[0]
    good_before = db.balance(data.SELLER, barcode, "good")
    reserved_before = db.balance(data.SELLER, barcode, "reserved")

    deadline = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))

    # Резерв здесь делает опросчик: задание приходит от WB, а не от клиента.
    # Смотреть на маршруты бессмысленно — они в этом шаге ничего не резервируют.
    with TxnWatch(db, application="wms-wb-sync") as watch:
        started = time.monotonic()
        order_ids = seed_wb_orders(data.WB_ACCOUNT, data.WB_ORDERS, barcode, deadline)
        # Ждать полный набор, а не первое появившееся задание. Пятёрка
        # заводится за ~27 мс по 4 мс на задание, наблюдатель опрашивает раз в
        # 50 мс — список из одной строки уже истинен, и `or None` завершал бы
        # ожидание внутри пачки. Шаг мерил бы удачу попадания между опросами,
        # а не «5 заданий за < 2 с». Атомарной пятёрка быть не может:
        # одна транзакция на пять заказов держала бы блокировки строк остатка
        # 200 мс при пределе 100 мс (инвариант 4).
        def all_arrived() -> list[dict[str, Any]] | None:
            found = db.rows(
                "SELECT id, state FROM wms_task WHERE wb_order_id = ANY(%s)", (order_ids,))
            return found if len(found) == data.WB_ORDERS else None

        tasks = wait_until(all_arrived, timeout_s=TASK_VISIBLE_S, interval_s=0.05)
        elapsed = time.monotonic() - started
        if not tasks:
            # Досталось меньше пяти — покажем, сколько именно, а не пустоту.
            tasks = db.rows(
                "SELECT id, state FROM wms_task WHERE wb_order_id = ANY(%s)", (order_ids,))

    # Что доехало — то доехало: следующим шагам нужны эти задания, даже если
    # их меньше пяти. Неполный набор они увидят сами.
    ctx["wb_order_ids"] = order_ids
    ctx["task_ids"] = [task["id"] for task in (tasks or [])]

    # Если заданий нет — сразу говорим, в каком состоянии кабинет. Чаще всего
    # причина не в опросе, а в том, что кабинет придержан лимитом Wildberries
    # после предыдущей работы (раздел 6.4), и это надо видеть, а не угадывать.
    cabinet = db.row(
        "SELECT status, sync_error_code, sync_attempts, "
        "       round(EXTRACT(EPOCH FROM (next_sync_at - now()))::numeric, 1) AS wait_s "
        "  FROM wb_account WHERE external_id = %s", (data.WB_ACCOUNT,)) or {}
    assert tasks and len(tasks) == data.WB_ORDERS, (
        f"за {elapsed:.2f} с в wms_task появилось {len(tasks or [])} заданий из "
        f"{data.WB_ORDERS}; кабинет {data.WB_ACCOUNT}: статус {cabinet.get('status')}, "
        f"код {cabinet.get('sync_error_code')}, следующий опрос через "
        f"{cabinet.get('wait_s')} с")
    assert elapsed < TASK_VISIBLE_S, (
        f"задания дошли за {elapsed:.2f} с при бюджете {TASK_VISIBLE_S} с")

    task_ids = ctx["task_ids"]
    reservations = db.rows(
        "SELECT id FROM reservation WHERE task_id = ANY(%s) AND state = 'held'", (task_ids,))
    assert len(reservations) == data.WB_ORDERS, (
        f"резервов {len(reservations)} на {data.WB_ORDERS} заданий: заказ и резерв "
        f"рождаются одной транзакцией (инвариант 1)")

    good_after = db.balance(data.SELLER, barcode, "good")
    reserved_after = db.balance(data.SELLER, barcode, "reserved")
    assert good_after == good_before - data.WB_ORDERS, (
        f"good {good_before} → {good_after}, ожидалось падение на {data.WB_ORDERS}")
    assert reserved_after == reserved_before + data.WB_ORDERS, (
        f"reserved {reserved_before} → {reserved_after}, ожидался рост на {data.WB_ORDERS}")

    assert watch.error is None, f"наблюдение за транзакциями оборвалось: {watch.error}"
    assert watch.seen > 0, (
        "трейс пуст: за время резерва в базе не было ни одной транзакции — "
        "проверять нечего, резерв идёт мимо Postgres")
    assert not watch.idle_in_transaction, (
        f"{len(watch.idle_in_transaction)} раз бэкенд ждал клиента дольше "
        f"{TxnWatch.IDLE_LIMIT_MS:.0f} мс с открытой транзакцией — "
        f"это HTTP-вызов внутри неё (инвариант 2)")
    assert watch.max_age_ms < TXN_LIMIT_MS, (
        f"самая долгая транзакция {watch.max_age_ms:.0f} мс при пределе {TXN_LIMIT_MS:.0f} мс: "
        f"внутри неё что-то ждёт сети")


# ============================================================== шаг 5 (A)

@pytest.mark.stream_a
def test_step_05_unmapped_product_goes_to_manual_review_without_a_reservation(
        wms: Wms, db: Db, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 5: задание на немаппленный товар.

    ASSERT state='manual_review', manual_review_code='PRODUCT_MAPPING_MISSING',
    резерв не создан.

    В боевом контуре немаппленный товар уходил в отказ и дальше в тихую отмену
    без причины — часть тех 2645 (раздел 3.2). Он обязан попадать на глаза
    человеку с кодом, а не исчезать.
    """
    need(ctx, "owner_ready", 1)
    order_id = scenario.next_order_id()
    result = wms.result("/reservations", {
        "idempotency_key": scenario.idem(f"unmapped-{order_id}"),
        "seller_external_id": data.SELLER, "wb_account_external_id": data.WB_ACCOUNT,
        "wb_order_id": order_id, "sku": data.UNMAPPED_BARCODE,
        "barcode": data.UNMAPPED_BARCODE, "quantity": 1,
        "correlation_id": scenario.reference("unmapped")})

    assert result.get("status") != "reserved", f"резерв на немаппленный товар прошёл: {result}"
    assert result.get("error_code") == "PRODUCT_MAPPING_MISSING", (
        f"код ошибки {result.get('error_code')}, ожидался PRODUCT_MAPPING_MISSING")

    task = db.task_by_order(order_id)
    assert task, (f"задание {order_id} не заведено: немаппленный товар обязан "
                  f"стать заданием в manual_review")
    assert task["state"] == "manual_review", f"состояние {task['state']}, ожидалось manual_review"
    assert task["manual_review_code"] == "PRODUCT_MAPPING_MISSING"
    assert not db.rows("SELECT id FROM reservation WHERE task_id = %s", (task["id"],)), \
        "на немаппленный товар создан резерв"


# ============================================================== шаг 6 (A)

@pytest.mark.stream_a
def test_step_06_zero_stock_reserves_but_leaves_a_visible_trace(
        wms: Wms, db: Db, bus: Any, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 6: задание на товар с нулевым остатком, allow_ledger_short=true.

    ASSERT резерв создан, discrepancy(kind='ledger_short'), событие
    wms.stock.shortfall.v1.

    Клапан сохраняем — иначе в первый день новой WMS подбор встанет и операторы
    снова пойдут в обход (раздел 6.5). Но молчаливого `_force_reservation`
    больше нет: смена не встаёт, а ошибка становится счётной и адресной.
    """
    need(ctx, "owner_ready", 1)
    order_id = scenario.next_order_id()
    result = wms.result("/reservations", {
        "idempotency_key": scenario.idem(f"short-{order_id}"),
        "seller_external_id": data.SELLER, "wb_account_external_id": data.WB_ACCOUNT,
        "wb_order_id": order_id, "sku": data.ZERO_STOCK_BARCODE,
        "barcode": data.ZERO_STOCK_BARCODE, "quantity": 1,
        "correlation_id": scenario.reference("ledger-short")})
    assert result.get("status") == "reserved", (
        f"резерв не создан: {result}. Клапан включён по владельцу, смена вставать не должна")

    task = db.task_by_order(order_id)
    assert task, f"задание {order_id} не заведено"
    assert db.rows("SELECT id FROM reservation WHERE task_id = %s AND state = 'held'",
                   (task["id"],)), "резерв не записан"

    discrepancy = db.row(
        "SELECT qty, cell_id FROM discrepancy WHERE task_id = %s AND kind = 'ledger_short'",
        (task["id"],))
    assert discrepancy, "нет discrepancy(kind='ledger_short'): сборка без остатка снова молчит"

    bus.require_connected()
    event = bus.wait_for(
        lambda e: e.get("type") == "wms.stock.shortfall.v1"
        and str(e.get("payload", {}).get("task_id")) == str(task["id"]),
        timeout_s=10.0)
    assert event, "событие wms.stock.shortfall.v1 не дошло до mmx.events"
    for field in ("owner_id", "sku_id", "cell_id", "qty_short", "task_id"):
        assert field in event["payload"], f"в payload события нет {field}"

    ctx["ledger_short_task"] = task["id"]


# ============================================================== шаг 7 (A)

@pytest.mark.stream_a
def test_step_07_labels_are_ready_in_zplv_before_picking_starts(
        db: Db, ctx: dict[str, Any]) -> None:
    """Шаг 7: стикеры.

    ASSERT wb_label заполнен для всех 5 заданий ДО начала подбора, format='zplv'.

    «До начала подбора» проверяется буквально: у заданий ещё нет ни строки
    подбора, ни сборщика. Сегодня стикер запрашивается в момент упаковки, и
    человек ждёт до трёх вызовов Wildberries (раздел 3.3).
    """
    task_ids = need(ctx, "task_ids", 4)

    not_started = db.value(
        "SELECT count(*) AS n FROM wms_task WHERE id = ANY(%s) "
        "AND (state <> 'reserved' OR assignee IS NOT NULL)", (task_ids,))
    assert int(not_started or 0) == 0, (
        "подбор уже начался — проверка «стикер готов ДО подбора» потеряла смысл; "
        "шаг 7 обязан идти до шага 8")
    assert int(db.value("SELECT count(*) AS n FROM pick_line WHERE task_id = ANY(%s)",
                        (task_ids,)) or 0) == 0

    # Стикер тянется фоново, вне транзакции резерва (раздел 6.6), поэтому его
    # ждут, а не читают сразу. Смысл шага от этого не меняется: проверяется,
    # что стикер лежит ДО начала подбора, — а подбор ещё не начинался, это
    # утверждение выше. Критерий раздела 10 — «стикер готов до начала
    # упаковки», а не «мгновенно».
    def all_labels() -> list[dict[str, Any]] | None:
        found = db.rows(
            "SELECT task_id, format, invalidated_at FROM wb_label WHERE task_id = ANY(%s)",
            (task_ids,))
        return found if len(found) == len(task_ids) else None

    labels = wait_until(all_labels, timeout_s=LABEL_READY_S, interval_s=0.1)
    if not labels:
        labels = db.rows(
            "SELECT task_id, format, invalidated_at FROM wb_label WHERE task_id = ANY(%s)",
            (task_ids,))
    assert len(labels) == len(task_ids), (
        f"за {LABEL_READY_S:.0f} с стикеров {len(labels)} на {len(task_ids)} заданий: "
        f"они тянутся заранее, пачкой, сразу после резерва (раздел 6.6)")
    for label in labels:
        assert label["format"] == "zplv", (
            f"формат {label['format']}, целевой zplv: 1–3 КБ текста против 20–100 КБ картинки")
        assert label["invalidated_at"] is None


# ============================================================ шаг 8 (A, B)

@pytest.mark.stream_a
@pytest.mark.stream_b
def test_step_08_five_parallel_sessions_never_hand_one_task_to_two_pickers(
        wms: Wms, ctx: dict[str, Any]) -> None:
    """Шаг 8: пять параллельных сессий подбора тянут задания.

    ASSERT ни одно задание не выдано двоим, ни одно не потеряно.

    Пять сборщиков работают одновременно (раздел 4). Дубль означает, что двое
    пойдут за одной вещью; потеря — что задание не пойдёт никто.
    """
    task_ids = {str(task_id) for task_id in need(ctx, "task_ids", 4)}

    pulled: dict[str, list[str]] = {}
    errors: list[str] = []

    def pull(picker: str) -> None:
        # Каждой сессии — своё соединение: пять сборщиков это пять клиентов,
        # и гонку за задание надо воспроизвести, а не обойти общим замком.
        client = Wms(wms.base_url)
        try:
            result = client.result("/tasks/pull", {"assignee": picker, "limit": len(task_ids),
                                                   "claim": True})
            pulled[picker] = [str(item.get("task", {}).get("task_id"))
                              for item in result.get("tasks", [])]
        except AssertionError as failure:
            errors.append(f"{picker}: {failure}")
        finally:
            client.close()

    threads = [threading.Thread(target=pull, args=(picker,)) for picker in data.PICKERS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    ctx["pulled"] = {picker: ids for picker, ids in pulled.items() if ids}

    assert not errors, f"выдача заданий не работает: {errors[0]}"

    taken: list[str] = [task_id for ids in pulled.values() for task_id in ids]
    duplicates = {task_id for task_id in taken if taken.count(task_id) > 1}
    assert not duplicates, f"задания выданы двоим: {sorted(duplicates)}"

    lost = task_ids - set(taken)
    assert not lost, f"задания не выданы никому: {sorted(lost)}"


# ============================================================== шаг 9 (B)

@pytest.mark.stream_b
def test_step_09_control_scan_rejects_a_foreign_barcode_and_the_session_keeps_place_and_time(
        wms: Wms, db: Db, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 9: подбор, скан у стойки, упаковка с контрольным сканом.

    ASSERT скан чужого штрихкода отклонён; сессия содержит адрес и время.

    Контрольный скан при упаковке — главный рубеж качества процесса (раздел 4):
    единственное место, где реально проверяется, что в коробке лежит то, что
    должно. Сессия без адреса и времени — это те самые 1 запись
    `workstation_pick_session` на 6497 заданий из раздела 3.
    """
    pulled = need(ctx, "pulled", 8)
    # Задание берём из пятёрки шага 4, а не первое попавшееся из произвольной
    # сессии. В очереди рядом лежит задание шага 6 — на ZERO_STOCK_BARCODE, — и
    # если досталось оно, «свой» штрихкод BARCODES[0] не совпадёт, шаг
    # покраснеет не по вине рабочего места, а за ним и шаг 11, которому нужен
    # packed_task_ids. Наблюдалось: два прогона подряд при одном и том же коде,
    # в одном шаг 9 зелёный, в другом красный.
    step_04 = {str(task_id) for task_id in need(ctx, "task_ids", 4)}
    taken = [task_id for ids in pulled.values() for task_id in ids if str(task_id) in step_04]
    assert taken, (
        "среди выданных нет ни одного задания шага 4 — сканировать их штрихкодом нечего")
    task_id = taken[0]

    rejected = wms.result(f"/tasks/{task_id}/scan", {"barcode": data.BARCODES[2]})
    assert rejected.get("status") != "picked", (
        f"скан чужого штрихкода принят: {rejected}. Ошибка обязана останавливать упаковку")
    assert rejected.get("scan_result") in ("wrong_barcode", "wrong_owner", "not_found"), (
        f"разбор скана {rejected.get('scan_result')}: экран должен показать, что именно не так")

    picked = wms.result(f"/tasks/{task_id}/scan", {"barcode": data.BARCODES[0]})
    assert picked.get("status") == "picked", f"свой штрихкод не принят: {picked}"
    assert picked.get("owner_external_id") == data.SELLER

    packed = wms.result(f"/tasks/{task_id}/pack", {
        "idempotency_key": scenario.idem(f"pack-{task_id}"),
        "control_scan_barcode": data.BARCODES[0], "box_barcode": data.BOXES[0][0]})
    ctx["packed_task_ids"] = [task_id]
    assert packed.get("state") in ("packed", "labeled"), f"упаковка не прошла: {packed}"

    line = db.row("SELECT cell_id, scanned_at, scan_result FROM pick_line "
                  "WHERE task_id = %s ORDER BY scanned_at DESC NULLS LAST LIMIT 1", (task_id,))
    assert line, f"по заданию {task_id} нет строки подбора: сессия не наполняется сканами"
    assert line["cell_id"], "в строке подбора нет адреса — сборщику некуда идти"
    assert line["scanned_at"], "в строке подбора нет времени скана"


# ============================================================= шаг 10 (B)

@pytest.mark.stream_b
def test_step_10_print_reaches_the_device_in_under_50ms(
        wms: Wms, db: Db, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 10: печать.

    ASSERT от вызова до записи в устройство < 50 мс.

    Путь состоит из двух половин: `wms` отдаёт локальный ZPL, агент печати
    пишет его RAW-байтами в USB-принтер. Первую половину прогон измеряет сам.
    Вторую измерить может только агент (поток B) — поэтому он обязан отдавать
    время своей записи; без этого утверждение проверить нечем, и шаг красный,
    а не «условно зелёный».
    """
    task_id = need(ctx, "packed_task_ids", 9)[0]

    # Станцию берём ту, на которой стоит агент: он один знает, к какому
    # принтеру подключён. Первая активная по имени — не то же самое, и
    # случайная строка в таблице увела бы печать на станцию без агента.
    stats_url = os.getenv("PRINT_AGENT_STATS_URL")
    station_id = None
    if stats_url:
        try:
            station_id = httpx.get(stats_url, timeout=5.0).json().get("station_id")
        except Exception:  # noqa: BLE001 — упадём ниже, с внятной причиной
            station_id = None
    if not station_id:
        station_id = db.value("SELECT id FROM station WHERE active ORDER BY name LIMIT 1")
    assert station_id, "на стенде не заведено ни одной станции — печатать некуда"

    worst = 0.0
    for attempt in range(10):
        call = wms.call(f"/labels/{task_id}/print", {
            "station_id": str(station_id),
            "idempotency_key": scenario.idem(f"print-{task_id}-{attempt}"),
            "reprint": attempt > 0, "reason": "прогон: замер задержки печати"})
        assert call.ok, f"печать не отдана: {call.status_code} / {call.error}"
        assert call.result.get("payload"), "в ответе нет байтов для WritePrinter"
        worst = max(worst, call.elapsed_ms)

    assert worst < PRINT_LIMIT_MS, (
        f"wms отдаёт стикер за {worst:.0f} мс при пределе {PRINT_LIMIT_MS:.0f} мс — "
        f"на агента и принтер не остаётся ничего. Это время, измеренное СНАРУЖИ: "
        f"в него входит и HTTP. Прежде чем чинить маршрут, посмотрите на "
        f"гистограмму самого сервиса — `mmx_wms_label_print_duration_seconds` в "
        f"/metrics меряет только его половину. Если по ней всё внутри бюджета, "
        f"а снаружи нет, узкое место не в складе, а в том, что стенд делит "
        f"восемь ядер между Postgres, четырьмя воркерами и самим прогоном")

    # Вторая половина пути. «Клик печать» на складе делается на экране рабочего
    # места, оно и толкает байты агенту по открытому соединению (раздел 6.6).
    # Дёргать один только wms недостаточно: он отдаёт байты, но никому их не
    # push'ит, и агент честно отчитывается о нуле записей. Проверять надо путь
    # целиком, иначе половина, ради которой переписана вся этикетка, не
    # проверяется вовсе.
    workstation_url = os.getenv("WORKSTATION_BASE_URL")
    if not workstation_url:
        not_ready(
            "не задана переменная окружения WORKSTATION_BASE_URL; «клик печать» идёт "
            "через рабочее место (раздел 6.6), и без его адреса агенту никто "
            "не толкнёт байты")
    try:
        pushed = httpx.post(
            f"{workstation_url.rstrip('/')}/api/workstation/v1/print",
            json={"task_id": str(task_id), "station_id": str(station_id),
                  "actor_id": data.PICKERS[0], "reprint": True,
                  "reason": "прогон: замер записи в устройство"},
            timeout=10.0)
    except Exception as failure:  # noqa: BLE001
        not_ready(f"рабочее место не отвечает по WORKSTATION_BASE_URL ({failure})")
    assert pushed.status_code == 200, (
        f"рабочее место не приняло печать: {pushed.status_code} / {pushed.text[:200]}")

    stats_url = os.getenv("PRINT_AGENT_STATS_URL")
    if not stats_url:
        not_ready(
            "поток B: агент печати не отдаёт телеметрию записи в устройство. "
            "Нужен PRINT_AGENT_STATS_URL с JSON {\"last_write_ms\": …, \"task_id\": …}; "
            "без него «до записи в устройство» измерить нечем")
    try:
        # Push агенту и ответ рабочему месту — разные стороны сокета, поэтому
        # телеметрию ждём, а не читаем сразу: гонка здесь дала бы ложный красный.
        stats = wait_until(
            lambda: (lambda s: s if s.get("last_write_ms") is not None else None)(
                httpx.get(stats_url, timeout=5.0).json()),
            timeout_s=10.0, interval_s=0.1) or httpx.get(stats_url, timeout=5.0).json()
    except Exception as failure:  # noqa: BLE001
        not_ready(f"агент печати не отвечает по PRINT_AGENT_STATS_URL ({failure})")
    write_ms = stats.get("last_write_ms")
    assert write_ms is not None, f"агент не сообщил last_write_ms: {stats}"
    assert str(stats.get("task_id")) == str(task_id), (
        f"агент отчитался о задании {stats.get('task_id')}, а печатали {task_id}")
    assert float(write_ms) < PRINT_LIMIT_MS, (
        f"запись в устройство {float(write_ms):.0f} мс при пределе {PRINT_LIMIT_MS:.0f} мс")


# =========================================================== шаг 11 (A, B)

@pytest.mark.stream_a
@pytest.mark.stream_b
def test_step_11_supply_is_shipped_with_correct_orders_and_handover_needs_a_human(
        wms: Wms, bus: Any, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 11: поставка и передача.

    ASSERT wb.supply.shipped.v1 с корректным orders; HANDED_TO_WB требует
    подтверждения человеком.

    `orders` — тарифицируемое количество (приложение E), на нём стоит счёт
    клиенту. `complete` у Wildberries приёмку не доказывает, поэтому передачу
    подтверждает живой человек (раздел 2.12); сегодня таких подтверждений
    меньше 1 % отгруженных.
    """
    task_ids = need(ctx, "packed_task_ids", 9)

    opened = wms.result("/shipments", {
        "seller_external_id": data.SELLER, "idempotency_key": scenario.idem("supply-open"),
        "action": "open"})
    ctx["shipment"] = opened
    assert opened.get("state") == "open", f"поставка не открыта: {opened}"

    wms.result("/shipments", {
        "seller_external_id": data.SELLER, "idempotency_key": scenario.idem("supply-add"),
        "action": "add_orders", "wb_supply_id": opened.get("wb_supply_id"),
        "task_ids": [str(task_id) for task_id in task_ids]})
    wms.result("/shipments", {
        "seller_external_id": data.SELLER, "idempotency_key": scenario.idem("supply-close"),
        "action": "close", "wb_supply_id": opened.get("wb_supply_id")})
    delivered = wms.result("/shipments", {
        "seller_external_id": data.SELLER, "idempotency_key": scenario.idem("supply-deliver"),
        "action": "deliver", "wb_supply_id": opened.get("wb_supply_id")})
    assert delivered.get("state") in ("closed", "handed_to_wb", "accepted_by_wb"), (
        f"поставка не передана: {delivered}")

    bus.require_connected()
    shipped = bus.wait_for(lambda e: e.get("type") == "wb.supply.shipped.v1", timeout_s=15.0)
    assert shipped, "событие wb.supply.shipped.v1 не дошло до mmx.events"
    payload = shipped["payload"]
    assert int(payload.get("orders", -1)) == len(task_ids), (
        f"orders={payload.get('orders')} при {len(task_ids)} заданиях в поставке — "
        f"счёт клиенту выставится по этому числу")

    # Передача без подписи человека принята быть не должна.
    unsigned = wms.call("/shipments", {
        "seller_external_id": data.SELLER, "idempotency_key": scenario.idem("supply-hand-nobody"),
        "action": "hand_over", "wb_supply_id": opened.get("wb_supply_id")})
    assert unsigned.result.get("state") != "handed_to_wb", (
        "поставка переведена в handed_to_wb без подтверждения человеком")

    handed = wms.result("/shipments", {
        "seller_external_id": data.SELLER, "idempotency_key": scenario.idem("supply-hand"),
        "action": "hand_over", "wb_supply_id": opened.get("wb_supply_id"),
        "handed_over_by": "полный прогон, кладовщик"})
    assert handed.get("state") == "handed_to_wb", f"передача не подтверждена: {handed}"
    assert handed.get("handed_by"), "в поставке не сохранён тот, кто подтвердил передачу"
    ctx["shipment"] = handed


# ============================================================= шаг 12 (C)

@pytest.mark.stream_c
def test_step_12_every_billable_operation_is_accrued_45_15_30(
        bus: Any, ctx: dict[str, Any]) -> None:
    """Шаг 12: начисления.

    ASSERT начисление на каждую тарифицируемую операцию; amount=45,
    partner_amount=15, net_amount=30.

    Модель денег трёхуровневая наценочная (раздел 1): клиент платит 45, из них
    15 — наценка партнёра, 30 — MM-Express. Сегодня тарифицируется меньше двух
    процентов того, что делает склад.
    """
    bus.require_connected()

    billable = [event for event in bus.collected()
                if event.get("type") in data.BILLABLE_EVENT_TYPES]
    assert billable, (
        "за прогон не наблюдалось ни одной тарифицируемой операции "
        f"({', '.join(data.BILLABLE_EVENT_TYPES)}) — начислять не с чего; "
        "сначала должны позеленеть шаги 9 и 11")

    billing = Db(env("BILLING_DATABASE_URL"))
    table = os.getenv("BILLING_ACCRUAL_TABLE", "billing_accrual")
    try:
        # КАЖДЫЙ тип проверяется отдельно, а не «хоть что-то начислилось».
        #
        # Общая проверка зеленела на одной услуге из трёх: упаковка не
        # тарифицировалась вовсе (включён был `order.packed.v1`, которого
        # никто не шлёт), а шаг всё равно был зелёным — по отгрузке.
        seen_types = {event["type"] for event in billable}
        missing = [name for name in data.BILLABLE_EVENT_TYPES if name not in seen_types]
        assert not missing, (
            f"за прогон не пришло ни одного события типов {missing}: услуга "
            f"делается, а тарифицировать её нечем. Именно так упаковка — самая "
            f"частая операция склада — шла клиенту бесплатно")

        # Неизвестный тип в списке включённых — тоже находка: он не
        # тарифицируется, и узнать об этом можно только здесь.
        enabled = {row["event_type"] for row in billing.rows(
            "SELECT event_type FROM billing_billable_event WHERE active")}
        unknown = sorted(enabled - set(data.BILLABLE_EVENT_TYPES))
        assert not unknown, (
            f"включены типы, которых прогон не видел ни разу: {unknown}. "
            f"Либо их никто не издаёт, либо прогон их не покрывает — и то и "
            f"другое значит невыставленный счёт")

        for event in billable:
            accrual = billing.row(
                f"SELECT * FROM {table} WHERE event_id = %s", (event["event_id"],))  # noqa: S608
            assert accrual, (
                f"на операцию {event['type']} (event_id={event['event_id']}) "
                f"нет начисления: физическое действие = движение + событие + начисление "
                f"(инвариант 13)")
            amount = int(accrual["amount"])
            partner = int(accrual["partner_amount"])
            net = int(accrual.get("net_amount") if accrual.get("net_amount") is not None
                      else amount - partner)
            assert (amount, partner, net) == (data.AMOUNT, data.PARTNER_AMOUNT, data.NET_AMOUNT), (
                f"{event['type']}: amount={amount}, partner_amount={partner}, net_amount={net}; "
                f"ожидалось {data.AMOUNT}/{data.PARTNER_AMOUNT}/{data.NET_AMOUNT}")
    finally:
        billing.close()


# =========================================================== шаг 13 (A, B)

@pytest.mark.stream_a
@pytest.mark.stream_b
def test_step_13_cancel_after_the_label_unwinds_reservation_label_and_supply(
        wms: Wms, db: Db, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 13: отмена задания после выдачи стикера.

    ASSERT резерв снят, товар вернулся в good, стикер помечен недействительным,
    заказ освобождён из поставки, cancel_reason не пуст.

    В боевом контуре у всех 2645 отмен `manual_review_reason` был NULL — отмена
    без причины неразбираема (раздел 3, инвариант 11).
    """
    task_ids = need(ctx, "task_ids", 4)
    packed = set(ctx.get("packed_task_ids", []))
    candidates = [task_id for task_id in task_ids if str(task_id) not in {str(p) for p in packed}]
    assert candidates, "нет задания со стикером, которое можно отменить"
    task_id = candidates[-1]

    barcode = data.BARCODES[0]
    good_before = db.balance(data.SELLER, barcode, "good")

    result = wms.result(f"/tasks/{task_id}/cancel", {
        "cancellation_event_id": scenario.idem(f"cancel-{task_id}"), "handed_over": False})
    assert result.get("state") == "cancelled", f"задание не отменено: {result}"
    assert (result.get("cancel_reason") or "").strip(), "отмена без причины (инвариант 11)"

    task = db.row("SELECT state, cancel_reason, supply_id FROM wms_task WHERE id = %s", (task_id,))
    assert task and (task["cancel_reason"] or "").strip(), "в базе отмена без причины"
    assert task["supply_id"] is None, "заказ не освобождён из поставки"

    reservation = db.row("SELECT state, released_at, release_reason FROM reservation "
                         "WHERE task_id = %s ORDER BY created_at DESC LIMIT 1", (task_id,))
    assert reservation, f"резерв задания {task_id} не найден"
    assert reservation["state"] == "released", f"резерв в состоянии {reservation['state']}"
    assert reservation["release_reason"], "снятый резерв не объяснил, почему снят"

    good_after = db.balance(data.SELLER, barcode, "good")
    assert good_after == good_before + 1, (
        f"good {good_before} → {good_after}: товар не вернулся на полку")

    label = db.row("SELECT invalidated_at FROM wb_label WHERE task_id = %s", (task_id,))
    assert label and label["invalidated_at"], "стикер отменённого задания остался действительным"


# =========================================================== шаг 14 (A, B)

@pytest.mark.stream_a
@pytest.mark.stream_b
def test_step_14_tasks_keep_arriving_through_tasks_pull_with_the_broker_down(
        wms: Wms, db: Db, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 14: RabbitMQ выключен.

    ASSERT задания продолжают появляться через /tasks/pull.

    Событие — уведомление, а не способ доставки (раздел 6.1). Это тот самый
    шаг, который архитектурно закрывает «новые заказы иногда не падают в
    приложение»: между Wildberries и заданием больше нет очереди.
    """
    need(ctx, "owner_ready", 1)
    rabbit_url = env("RABBITMQ_URL")
    compose = Path(__file__).resolve().parents[2] / "infrastructure" / "stand" / "compose.yaml"
    stop_cmd = os.getenv("FULL_RUN_RABBIT_STOP", f"docker compose -f {compose} stop rabbitmq")
    start_cmd = os.getenv("FULL_RUN_RABBIT_START", f"docker compose -f {compose} start rabbitmq")

    stopped = subprocess.run(stop_cmd, shell=True, capture_output=True, text=True, timeout=120)
    try:
        if stopped.returncode != 0:
            not_ready(f"не удалось выключить RabbitMQ командой «{stop_cmd}»: "
                      f"{stopped.stderr.strip()[:120]}")
        gone = wait_until(lambda: not port_is_open(rabbit_url), timeout_s=30, interval_s=0.5)
        if not gone:
            not_ready("RabbitMQ остался доступен — проверка «шина выключена» ничего не значит")

        order_id = scenario.next_order_id()
        result = wms.result("/reservations", {
            "idempotency_key": scenario.idem(f"nobus-{order_id}"),
            "seller_external_id": data.SELLER, "wb_account_external_id": data.WB_ACCOUNT,
            "wb_order_id": order_id, "sku": data.BARCODES[1], "barcode": data.BARCODES[1],
            "quantity": 1, "correlation_id": scenario.reference("no-bus")})
        assert result.get("status") == "reserved", (
            f"без шины резерв не прошёл: {result}. Склад от шины не зависит")

        task = wait_until(lambda: db.task_by_order(order_id), timeout_s=TASK_VISIBLE_S)
        assert task, "задание не появилось в базе при выключенной шине"

        pulled = wait_until(
            lambda: [item for item in wms.result(
                "/tasks/pull", {"assignee": data.PICKER_NO_BUS, "limit": 50, "claim": True}
            ).get("tasks", [])
                if str(item.get("task", {}).get("task_id")) == str(task["id"])] or None,
            timeout_s=TASK_VISIBLE_S, interval_s=0.2)
        assert pulled, "задание не пришло в /tasks/pull при выключенном RabbitMQ"
    finally:
        subprocess.run(start_cmd, shell=True, capture_output=True, text=True, timeout=120)
        wait_until(lambda: port_is_open(rabbit_url), timeout_s=60, interval_s=1.0)

    # Шина вернулась — консьюмер обязан вернуться вместе с ней.
    #
    # Проверка «склад работает без шины» без этой половины неполна: она
    # оставляла консьюмер лежащим, а неоплаченная работа копилась бы дальше
    # молча. Именно так это и выглядит в боевом контуре — всё «работает», а
    # счёт не выставляется.
    billing = Db(env("BILLING_DATABASE_URL"))
    try:
        before = int(billing.value(
            "SELECT count(*) AS n FROM billing_inbox") or 0)
        order_id = scenario.next_order_id()
        wms.result("/reservations", {
            "idempotency_key": scenario.idem(f"after-bus-{order_id}"),
            "seller_external_id": data.SELLER, "wb_account_external_id": data.WB_ACCOUNT,
            "wb_order_id": order_id, "sku": data.BARCODES[1], "barcode": data.BARCODES[1],
            "quantity": 1, "correlation_id": scenario.reference("after-bus")})

        recovered = wait_until(
            lambda: (int(billing.value("SELECT count(*) AS n FROM billing_inbox") or 0)
                     > before) or None,
            timeout_s=90, interval_s=1.0)
        assert recovered, (
            "после включения шины консьюмер биллинга не разобрал ни одного "
            "нового события за полторы минуты: он не переподключился, и "
            "неоплаченная работа будет копиться молча")
    finally:
        billing.close()


# ============================================================= шаг 15 (A)

@pytest.mark.stream_a
def test_step_15_stock_push_leaves_immediately_and_publishes_a_lowered_available(
        wms: Wms, db: Db, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 15: публикация остатка.

    ASSERT вызов ушёл немедленно после движения, available = good − buffer.

    Никаких таймеров и накопления (раздел 6.4). Ограничитель существует только
    как защита от бана Wildberries и в нормальной работе не срабатывает.
    """
    need(ctx, "owner_ready", 1)
    barcode = data.BARCODES[2]

    pushes_before = int(db.value("SELECT count(*) AS n FROM wb_stock_push") or 0)

    reference = scenario.reference("push")
    wms.result("/receipts", {
        "seller_external_id": data.SELLER, "warehouse_code": data.WAREHOUSE_CODE,
        "reference": reference,
        "lines": [{"barcode": barcode, "expected_qty": 1, "actual_qty": 1,
                   "box_barcode": data.BOXES[2][0], "cell_address": data.BOXES[2][1],
                   "comment": "прогон: движение ради публикации остатка"}]})

    def new_push() -> dict[str, Any] | None:
        """Появилась ли новая строка журнала публикаций после нашего движения."""
        if int(db.value("SELECT count(*) AS n FROM wb_stock_push") or 0) <= pushes_before:
            return None
        return db.row("SELECT pushed_at, rows FROM wb_stock_push ORDER BY pushed_at DESC LIMIT 1")

    # «Немедленно» — это без таймера, а не мгновенно: остаётся время на сам
    # HTTP-вызов в Wildberries. Секунды хватает с запасом, а накопление за ней
    # уже не спрячешь.
    push = wait_until(new_push, timeout_s=1.0, interval_s=0.05)
    assert push, ("после движения не ушла публикация остатка в WB: "
                  "остаток публикуется сразу, без таймеров (раздел 6.4)")

    # Резерв в формуле не участвует: движение `good → reserved` уже вывело его
    # из `good` (раздел 6.2). Вычесть его второй раз — занизить вдвое.
    expected = db.row(
        "SELECT COALESCE(SUM(b.qty) FILTER (WHERE b.state = 'good'), 0) AS good, "
        "       MAX(s.buffer) AS buffer "
        "  FROM stock_balance b JOIN owner o ON o.id = b.owner_id JOIN sku s ON s.id = b.sku_id "
        " WHERE o.seller_external_id = %s AND s.barcode = %s", (data.SELLER, barcode))
    assert expected, f"по {barcode} нет остатка в проекции"
    available = max(0, int(expected["good"]) - int(expected["buffer"] or 0))

    published = wms.result("/catalog/stocks/bulk", {"seller_external_id": data.SELLER})
    rows = {row["barcode"]: row["available"] for row in published.get("stocks", [])}
    assert barcode in rows, f"{barcode} не попал в публикацию — непроданный товар"
    assert rows[barcode] == available, (
        f"публикуем {rows[barcode]}, а good − buffer = {available}: "
        f"остаток в WB всегда занижаем на страховой запас (инвариант 7)")


# ============================================================= шаг 16 (A)

@pytest.mark.stream_a
def test_step_16_ten_thousand_tasks_per_hour_without_errors_and_locks_under_100ms(
        wms: Wms, db: Db, ctx: dict[str, Any], scenario: data.Scenario) -> None:
    """Шаг 16: нагрузка — 10 000 заданий в час.

    ASSERT ошибок нет, удержание блокировки p99 < 100 мс.

    Это требование к системе, а не план физической сборки (решение владельца 4):
    пять сборщиков дают 300–750 заданий в час. Длительность окна задаётся
    FULL_RUN_LOAD_SECONDS; целевая скорость от неё не зависит.
    """
    need(ctx, "owner_ready", 1)
    seconds = float(os.getenv("FULL_RUN_LOAD_SECONDS", "60"))
    workers = int(os.getenv("FULL_RUN_LOAD_WORKERS", "8"))
    interval = 3600.0 / TARGET_PER_HOUR          # пауза между заданиями на всём потоке
    barcode = data.LOAD_BARCODE

    # Нагрузка идёт на СВОЕГО клиента и свой кабинет: 160 заданий за двадцать
    # секунд выбирают лимит Wildberries (300 в минуту на кабинет), и если бы
    # это был кабинет шага 4, следующий прогон не смог бы его опросить вовремя.
    # Разный владелец заодно разводит очереди выдачи: задания нагрузки не
    # попадут шагу 8 вместо его собственных.
    wms.result("/sellers", {
        "seller_external_id": data.LOAD_SELLER, "name": "Полный прогон — нагрузка",
        "inn": data.SELLER_INN, "active": True, "allow_ledger_short": True})
    wms.result("/catalog/products/ensure", {
        "seller_external_id": data.LOAD_SELLER, "barcode": barcode,
        "seller_sku": f"FR-LOAD-{barcode[-4:]}", "name": "Товар нагрузки"})
    wms.result("/wb/accounts", {
        "op": "upsert", "external_id": data.LOAD_WB_ACCOUNT,
        "seller_external_id": data.LOAD_SELLER,
        "display_name": "Кабинет нагрузки", "secret_ref": data.WB_SECRET_REF,
        "mode": "shadow", "status": "ACTIVE"})
    # Остатка должно хватить на всё окно: клапан ledger_short здесь не
    # проверяется, а недостача исказила бы измерение удержания блокировки.
    wms.result("/warehouse/documents", {
        "seller_external_id": data.LOAD_SELLER, "warehouse_code": data.WAREHOUSE_CODE,
        "reference": scenario.reference("load-opening"), "doc_type": "opening",
        "comment": "нагрузочный шаг: остаток под окно",
        "lines": [{"barcode": barcode, "quantity": 100000,
                   "cell_address": "FR-LOAD-01", "state": "good"}]})

    outcome = LoadResult()
    lock = threading.Lock()
    stop_at = time.monotonic() + seconds
    next_start = [time.monotonic()]

    def fire() -> None:
        client = Wms(wms.base_url)
        try:
            while True:
                with lock:
                    if time.monotonic() >= stop_at:
                        return
                    slot = next_start[0]
                    next_start[0] = slot + interval
                delay = slot - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                order_id = scenario.next_order_id()
                call = client.call("/reservations", {
                    "idempotency_key": scenario.idem(f"load-{order_id}"),
                    "seller_external_id": data.LOAD_SELLER,
                    "wb_account_external_id": data.LOAD_WB_ACCOUNT,
                    "wb_order_id": order_id, "sku": barcode, "barcode": barcode,
                    "quantity": 1, "correlation_id": f"load-{order_id}"})
                status = call.result.get("status")
                with lock:
                    if not call.ok:
                        outcome.errors.append(f"{call.status_code} {call.error}")
                    elif status != "reserved":
                        outcome.errors.append(f"{status}/{call.result.get('error_code')}")
                    else:
                        outcome.accepted += 1
        finally:
            client.close()

    # Считаем задания СВОЕГО клиента, а не все подряд: рядом идёт опрос
    # Wildberries по другим кабинетам, и общий счётчик мерил бы заодно и его.
    def load_tasks() -> int:
        return int(db.value(
            "SELECT count(*) AS n FROM wms_task t JOIN owner o ON o.id = t.owner_id "
            " WHERE o.seller_external_id = %s", (data.LOAD_SELLER,)) or 0)

    tasks_before = load_tasks()
    metrics_url = os.getenv("WMS_METRICS_URL") or f"{wms.base_url.rstrip('/')}/metrics"
    locks_before = lock_hold_histogram(metrics_url, "reserve")
    with TxnWatch(db) as watch:
        started = time.monotonic()
        threads = [threading.Thread(target=fire, name=f"load-{n}") for n in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=seconds + 60)
        outcome.seconds = time.monotonic() - started

    assert not outcome.errors, (
        f"{len(outcome.errors)} отказов из {len(outcome.errors) + outcome.accepted}, "
        f"первый: {outcome.errors[0]}")
    assert outcome.per_hour >= TARGET_PER_HOUR, (
        f"держим {outcome.per_hour:.0f} заданий в час при требуемых {TARGET_PER_HOUR}")

    tasks_after = load_tasks()
    assert tasks_after - tasks_before == outcome.accepted, (
        f"принято {outcome.accepted} заданий, а в wms_task прибавилось "
        f"{tasks_after - tasks_before}: часть нагрузки не доехала до базы")

    assert watch.error is None, f"наблюдение за транзакциями оборвалось: {watch.error}"
    assert watch.seen > 0, (
        "за всю нагрузку в базе не наблюдалось ни одной транзакции — "
        "удержание блокировки измерять не на чем")
    # Инвариант 4 — про удержание блокировки ПОД РЕЗЕРВ, и сам сервис его
    # меряет. Берём его гистограмму: наблюдение за pg_stat_activity видит все
    # транзакции маршрутов сразу — вместе с опросом очереди рабочим местом,
    # который идёт раз в секунду и держит транзакцию десятки миллисекунд, — и
    # приписывает их возраст резерву. На холостом стенде это уже около 48 мс
    # при пределе 100: половину бюджета выбирала чужая работа.
    reserve_p99 = percentile_from_buckets(locks_before, lock_hold_histogram(metrics_url, "reserve"), 99)
    assert reserve_p99 is not None, (
        "за всю нагрузку сервис не отметил ни одного резерва в "
        "mmx_wms_lock_hold_seconds{operation=\"reserve\"} — удержание блокировки "
        "измерять не на чем")
    assert reserve_p99 < TXN_LIMIT_MS, (
        f"удержание блокировки под резерв p99 = {reserve_p99:.0f} мс при пределе "
        f"{TXN_LIMIT_MS:.0f} мс (инвариант 4)")

    # Наблюдение за pg_stat_activity здесь оставлено ради одного: доказать,
    # что нагрузка действительно шла через Postgres. Мерить им удержание
    # блокировки нельзя — оно видит все транзакции маршрутов вперемешку.
    slowest = watch.max_age_ms
    log_line = (f"резерв p99 {reserve_p99:.0f} мс (метрика сервиса), "
                f"самая долгая транзакция маршрутов за окно {slowest:.0f} мс")
    print(f"\n{log_line}")


# ==================================== мёртвые письма (этап 5.2 аудита)

@pytest.mark.stream_c
def test_a_rejected_message_lands_in_dead_letters_not_in_nowhere(
        bus: Any) -> None:
    """Отвергнутое сообщение обязано найтись в `stand.dead-letters`.

    На проде очередь мёртвых писем пуста при регулярных потерях заданий
    (раздел 3.5). Пустая она была не потому, что потерь нет: аргумент
    `x-dead-letter-exchange` задаётся при СОЗДАНИИ очереди, потребитель,
    объявивший её без него, терял отвергнутое молча — и «dead_letters пуст»
    значило «терялось в никуда».

    Политика `dead-letters` действует на очередь снаружи и не зависит от
    того, кто её объявил. Проверяется это единственным способом: отвергнуть
    сообщение и посмотреть, где оно.
    """
    bus.require_connected()
    pika = pytest.importorskip("pika", reason="нужен pika для проверки мёртвых писем")

    url = env("RABBITMQ_URL")
    probe = f"stand.dlq-probe-{uuid.uuid4().hex[:8]}"
    marker = uuid.uuid4().hex

    connection = pika.BlockingConnection(pika.URLParameters(url))
    try:
        channel = connection.channel()
        # Очередь объявляется БЕЗ аргумента dead-letter — именно так её
        # объявил бы потребитель, который о нём забыл.
        channel.queue_declare(queue=probe, durable=True, auto_delete=False)
        channel.basic_publish(exchange="", routing_key=probe, body=marker.encode("utf-8"))

        got = None
        for _ in range(50):
            method, _properties, body = channel.basic_get(queue=probe, auto_ack=False)
            if method is not None:
                got = (method, body)
                break
            time.sleep(0.1)
        assert got is not None, "сообщение не доехало до собственной очереди"
        method, body = got
        assert body.decode("utf-8") == marker
        # Отвергаем без возврата в очередь: именно это делает консьюмер с
        # событием, которое не смог разобрать.
        channel.basic_nack(method.delivery_tag, requeue=False)

        found = None
        for _ in range(50):
            dead_method, _dead_props, dead_body = channel.basic_get(
                queue="stand.dead-letters", auto_ack=True)
            if dead_method is None:
                time.sleep(0.1)
                continue
            if dead_body.decode("utf-8", "replace") == marker:
                found = dead_body
                break
        assert found is not None, (
            "отвергнутое сообщение не нашлось в stand.dead-letters: политика "
            "dead-letter-exchange не действует, и потери уходят в никуда")
    finally:
        try:
            channel.queue_delete(queue=probe)
        except Exception:  # noqa: BLE001 — уборка не важнее проверки
            pass
        connection.close()
