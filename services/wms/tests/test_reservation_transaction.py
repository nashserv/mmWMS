"""Ядро потока A: транзакция раздела 6.2 против настоящего Postgres.

Каждый тест назван по инварианту раздела 8, который он держит. Проверяется не
«функция вернула словарь», а то, ради чего эта функция написана: заказ и резерв
рождаются вместе, журнал нельзя переписать, а сборка без остатка оставляет след.
"""
from __future__ import annotations

import threading
import uuid

import pytest

from app.postgres import ConnectionPool, transaction
from app.service import CatalogOperations, StockOperations, WmsService

from dbfixtures import require_database, unique


@pytest.fixture(scope="module")
def pool() -> ConnectionPool:
    connections = ConnectionPool(require_database(), max_size=16)
    yield connections
    connections.close()


@pytest.fixture()
def wms(pool: ConnectionPool) -> WmsService:
    return WmsService(pool)


@pytest.fixture()
def catalog(pool: ConnectionPool) -> CatalogOperations:
    return CatalogOperations(pool)


@pytest.fixture()
def stock(pool: ConnectionPool) -> StockOperations:
    return StockOperations(pool)


class Client:
    """Один заведённый владелец со своим кабинетом, товаром и остатком."""

    def __init__(self, catalog: CatalogOperations, stock: StockOperations,
                 *, allow_ledger_short: bool = True) -> None:
        self.seller = unique("seller")
        self.account = unique("wb")
        self.barcode = f"46{uuid.uuid4().int % 10**11:011d}"
        self.cell = unique("cell").upper()
        self._catalog = catalog
        self._stock = stock
        catalog.upsert_owner({"seller_external_id": self.seller, "name": "Тест ядра",
                              "allow_ledger_short": allow_ledger_short})
        catalog.upsert_wb_account({
            "external_id": self.account, "seller_external_id": self.seller,
            "display_name": "Кабинет теста", "secret_ref": f"vault://test/{self.account}",
            "mode": "shadow", "status": "ACTIVE"})
        self.product = catalog.ensure_product({
            "seller_external_id": self.seller, "barcode": self.barcode,
            "seller_sku": "FR-TEST", "name": "Товар теста"})

    def load(self, quantity: int, *, cell: str | None = None) -> str:
        reference = unique("opening")
        self._stock.apply_document({
            "seller_external_id": self.seller, "reference": reference, "doc_type": "opening",
            "lines": [{"barcode": self.barcode, "quantity": quantity,
                       "cell_address": cell or self.cell, "state": "good"}]})
        return reference

    def order(self, **overrides) -> dict:
        params = {"idempotency_key": unique("idem"), "seller_external_id": self.seller,
                  "wb_account_external_id": self.account,
                  "wb_order_id": uuid.uuid4().int % 10**12, "sku": self.barcode,
                  "barcode": self.barcode, "quantity": 1,
                  "correlation_id": unique("corr")}
        params.update(overrides)
        return params


@pytest.fixture()
def client(catalog: CatalogOperations, stock: StockOperations) -> Client:
    return Client(catalog, stock)


def balance(pool: ConnectionPool, seller: str, barcode: str, state: str = "good") -> int:
    with pool.connection() as connection:
        with transaction(connection) as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(b.qty), 0) AS qty FROM stock_balance b "
                "  JOIN owner o ON o.id = b.owner_id JOIN sku s ON s.id = b.sku_id "
                " WHERE o.seller_external_id = %s AND s.barcode = %s AND b.state = %s",
                (seller, barcode, state))
            row = cursor.fetchone()
            return int(row["qty"]) if row else 0


def rows(pool: ConnectionPool, sql: str, params: tuple) -> list[dict]:
    with pool.connection() as connection:
        with transaction(connection) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


# ------------------------------------------- инвариант 1: одна транзакция

def test_order_and_reservation_are_born_in_one_commit(
        wms: WmsService, pool: ConnectionPool, client: Client) -> None:
    """Инвариант 1: заказ и резерв создаются одной транзакцией.

    Расхождение «заказ есть, резерва нет» — корень 2467 разошедшихся заданий
    боевого контура (раздел 3). Здесь оно невозможно по построению.
    """
    client.load(10)
    good_before = balance(pool, client.seller, client.barcode, "good")

    outcome = wms.reserve(client.order(quantity=3))

    assert outcome.status == "reserved", outcome.error_code
    assert outcome.task_id and outcome.reservation_id
    assert outcome.ledger_short is False

    held = rows(pool, "SELECT id, qty, state FROM reservation WHERE task_id = %s",
                (outcome.task_id,))
    assert len(held) == 1 and held[0]["state"] == "held" and held[0]["qty"] == 3

    assert balance(pool, client.seller, client.barcode, "good") == good_before - 3
    assert balance(pool, client.seller, client.barcode, "reserved") == 3


def test_balance_moves_only_through_the_ledger(
        wms: WmsService, pool: ConnectionPool, client: Client) -> None:
    """Инвариант 3: баланс — проекция журнала, а не самостоятельная запись."""
    client.load(5)
    outcome = wms.reserve(client.order(quantity=2))

    moves = rows(pool,
                 "SELECT qty, reason, doc_type, state_from, state_to FROM stock_move "
                 " WHERE doc_type = 'reservation' AND doc_ref = %s", (outcome.reservation_id,))
    assert moves, "резерв не оставил движения в журнале"
    assert sum(int(move["qty"]) for move in moves) == 2
    for move in moves:
        assert (move["state_from"], move["state_to"]) == ("good", "reserved")


def test_the_ledger_cannot_be_rewritten(pool: ConnectionPool, client: Client) -> None:
    """Инвариант 3: stock_move append-only, и это держит база, а не дисциплина."""
    client.load(1)
    with pytest.raises(Exception) as failure:
        with pool.connection() as connection:
            with transaction(connection) as cursor:
                cursor.execute("UPDATE stock_move SET qty = qty + 1 WHERE idem_key LIKE %s",
                               ("opening:%",))
    assert "append-only" in str(failure.value)


# --------------------------------- инварианты 4 и 5: блокировки и повторы

def test_ten_pickers_never_double_spend_one_sku(
        pool: ConnectionPool, catalog: CatalogOperations, stock: StockOperations) -> None:
    """Инвариант 4: конкурентный резерв не даёт двойного списания.

    Десять параллельных писателей на один owner × sku — это пять сборщиков и
    пять приёмщиков раздела 4. Товара ровно на половину: половина обязана
    получить резерв, половина — клапан 6.5, и ни одна единица не может быть
    выдана дважды.
    """
    client = Client(catalog, stock)
    client.load(5)
    wms = WmsService(pool)

    outcomes: list = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(10)
    lock = threading.Lock()

    def grab() -> None:
        try:
            params = client.order(quantity=1)
            barrier.wait(timeout=30)
            outcome = wms.reserve(params)
            with lock:
                outcomes.append(outcome)
        except BaseException as failure:      # noqa: BLE001 — падение потока обязано быть видно
            with lock:
                errors.append(failure)

    threads = [threading.Thread(target=grab, name=f"picker-{n}") for n in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"резерв упал под конкуренцией: {errors[0]!r}"
    assert len(outcomes) == 10
    assert all(outcome.status == "reserved" for outcome in outcomes), \
        [outcome.error_code for outcome in outcomes if outcome.status != "reserved"]

    # Ни одна единица не ушла дважды: пять честных резервов и пять по клапану.
    honest = [outcome for outcome in outcomes if not outcome.ledger_short]
    assert len(honest) == 5, f"честных резервов {len(honest)}, а товара было ровно 5"
    assert balance(pool, client.seller, client.barcode, "reserved") == 10
    assert balance(pool, client.seller, client.barcode, "good") == -5

    reservations = rows(pool, "SELECT DISTINCT task_id FROM reservation WHERE task_id = ANY(%s)",
                        ([outcome.task_id for outcome in outcomes],))
    assert len(reservations) == 10, "резерв потерялся или задание получило два резерва"


def test_repeating_the_same_order_changes_nothing(
        wms: WmsService, pool: ConnectionPool, client: Client) -> None:
    """Инвариант 5: повтор команды — тот же ответ, а не второе движение."""
    client.load(10)
    params = client.order(quantity=2)

    first = wms.reserve(params)
    second = wms.reserve(params)

    assert first.status == second.status == "reserved"
    assert first.task_id == second.task_id
    assert first.reservation_id == second.reservation_id
    assert second.duplicate is True
    assert balance(pool, client.seller, client.barcode, "reserved") == 2
    assert len(rows(pool, "SELECT id FROM wms_task WHERE wb_order_id = %s",
                    (params["wb_order_id"],))) == 1


def test_repeating_an_opening_document_adds_nothing(
        stock: StockOperations, pool: ConnectionPool, client: Client) -> None:
    """Инвариант 5: документ идемпотентен по reference."""
    reference = client.load(10)
    after_first = balance(pool, client.seller, client.barcode, "good")

    again = stock.apply_document({
        "seller_external_id": client.seller, "reference": reference, "doc_type": "opening",
        "lines": [{"barcode": client.barcode, "quantity": 10,
                   "cell_address": client.cell, "state": "good"}]})

    assert again["duplicate"] is True
    assert balance(pool, client.seller, client.barcode, "good") == after_first


# ------------------------------ инвариант 6: изоляция владельца и маппинг

def test_unmapped_product_becomes_a_task_in_manual_review(
        wms: WmsService, pool: ConnectionPool, client: Client) -> None:
    """Раздел 3.2: немаппленный товар обязан попасть человеку на глаза с кодом.

    В боевом контуре он уходил в отказ и дальше в отмену без причины — часть
    тех 2645 (раздел 3). Резерв при этом не создаётся: товар без маппинга не
    превращается в остаток (инвариант 6).
    """
    stranger = f"46{uuid.uuid4().int % 10**11:011d}"
    outcome = wms.reserve(client.order(sku=stranger, barcode=stranger))

    assert outcome.status == "rejected"
    assert outcome.error_code == "PRODUCT_MAPPING_MISSING"
    assert outcome.task_id, "задание обязано быть заведено даже без маппинга"

    task = rows(pool, "SELECT state, manual_review_code, manual_review_reason, sku_id "
                      "  FROM wms_task WHERE id = %s", (outcome.task_id,))[0]
    assert task["state"] == "manual_review"
    assert task["manual_review_code"] == "PRODUCT_MAPPING_MISSING"
    assert task["manual_review_reason"], "код без объяснения неразбираем"
    assert task["sku_id"] is None
    assert not rows(pool, "SELECT id FROM reservation WHERE task_id = %s", (outcome.task_id,))


def test_two_matches_are_ambiguous_not_the_first_one(
        wms: WmsService, catalog: CatalogOperations, stock: StockOperations,
        pool: ConnectionPool) -> None:
    """Раздел 3.2: два совпадения — AMBIGUOUS, а не «берём первое».

    У одной карточки Wildberries несколько размеров: артикул определяет модель,
    штрихкод — вещь на полке. Взять первый попавшийся размер значит отгрузить
    не ту вещь.
    """
    client = Client(catalog, stock)
    article = unique("article")
    for _ in range(2):
        catalog.ensure_product({
            "seller_external_id": client.seller,
            "barcode": f"46{uuid.uuid4().int % 10**11:011d}",
            "seller_sku": article, "name": "Один артикул, два размера"})

    outcome = wms.reserve(client.order(sku=article, barcode=None))

    assert outcome.error_code == "AMBIGUOUS_PRODUCT_MAPPING"
    assert outcome.status == "rejected"
    task = rows(pool, "SELECT state, manual_review_code FROM wms_task WHERE id = %s",
                (outcome.task_id,))[0]
    assert task["state"] == "manual_review"
    assert task["manual_review_code"] == "AMBIGUOUS_PRODUCT_MAPPING"


def test_wb_puts_the_barcode_into_the_sku_field(
        wms: WmsService, client: Client) -> None:
    """Раздел 3.2, правило 1: у WB поле называется sku, а лежит в нём штрихкод.

    Третий шаг поиска (`barcode == sku`) существует ровно ради этого случая:
    поле `barcode` в запросе не заполнено, штрихкод пришёл в `sku`.
    """
    client.load(3)
    outcome = wms.reserve(client.order(sku=client.barcode, barcode=None))
    assert outcome.status == "reserved", outcome.error_code


def test_reservation_never_crosses_the_owner(
        wms: WmsService, catalog: CatalogOperations, stock: StockOperations) -> None:
    """Инвариант 6: одинаковый штрихкод у двух клиентов — разные вещи."""
    first = Client(catalog, stock)
    second = Client(catalog, stock)
    catalog.ensure_product({"seller_external_id": second.seller, "barcode": first.barcode,
                            "name": "Тот же штрихкод, другой владелец"})
    first.load(5)

    outcome = wms.reserve(second.order(sku=first.barcode, barcode=first.barcode, quantity=1))

    # Товар второго владельца существует, но остатка у него нет: резерв обязан
    # опереться на его собственный остаток, а не на чужие пять единиц.
    assert outcome.status == "reserved" and outcome.ledger_short is True, \
        "резерв дотянулся до чужого остатка"


# --------------------------------- инвариант 12: клапан со следом

def test_ledger_short_reserves_but_leaves_three_traces(
        wms: WmsService, pool: ConnectionPool, client: Client) -> None:
    """Инвариант 12: сборка без остатка оставляет движение, расхождение и событие.

    Клапан сохраняем — иначе в первый день новой WMS подбор встанет и операторы
    снова пойдут в обход (раздел 6.5). Но молчаливого `_force_reservation`
    больше нет.
    """
    outcome = wms.reserve(client.order(quantity=2))

    assert outcome.status == "reserved" and outcome.ledger_short is True

    move = rows(pool, "SELECT qty, reason FROM stock_move WHERE doc_ref = %s AND reason = %s",
                (outcome.reservation_id, "ledger_short"))
    assert move and int(move[0]["qty"]) == 2

    discrepancy = rows(pool, "SELECT kind, qty, cell_id, decision FROM discrepancy "
                             " WHERE task_id = %s", (outcome.task_id,))
    assert discrepancy and discrepancy[0]["kind"] == "ledger_short"
    assert int(discrepancy[0]["qty"]) == 2
    assert discrepancy[0]["cell_id"], "расхождение без адреса некому разбирать"
    assert discrepancy[0]["decision"] == "pending"

    types = [emitted.envelope.type for emitted in outcome.events]
    assert "wms.stock.shortfall.v1" in types
    assert types[-1] == "wms.reservation.succeeded.v1", \
        "событие успеха обязано идти последним: до него расхождение уже описано"


def test_the_valve_is_switched_off_per_owner_not_globally(
        wms: WmsService, pool: ConnectionPool, catalog: CatalogOperations,
        stock: StockOperations) -> None:
    """Раздел 6.5: клапан отключается по владельцу товара.

    Владелец с выключенным клапаном получает отказ, но задание остаётся видимым
    в состоянии `short` — тихой отмены без причины больше нет (раздел 3).
    """
    strict = Client(catalog, stock, allow_ledger_short=False)
    outcome = wms.reserve(strict.order(quantity=1))

    assert outcome.status == "rejected"
    assert outcome.error_code == "INSUFFICIENT_STOCK"
    task = rows(pool, "SELECT state FROM wms_task WHERE id = %s", (outcome.task_id,))[0]
    assert task["state"] == "short"
    assert not rows(pool, "SELECT id FROM reservation WHERE task_id = %s", (outcome.task_id,))


# ------------------------------------- приложение E: порядок событий

def test_events_keep_a_monotonic_sequence_within_the_task(
        wms: WmsService, pool: ConnectionPool, client: Client) -> None:
    """Приложение E: sequence монотонен в пределах задания и растёт в той же транзакции."""
    outcome = wms.reserve(client.order(quantity=1))

    events = rows(pool, "SELECT type, sequence FROM outbox WHERE aggregate_id = %s "
                        " ORDER BY sequence", (outcome.task_id,))
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert events[-1]["type"] == "wms.reservation.succeeded.v1"


def test_unknown_seller_is_refused_without_inventing_an_owner(
        wms: WmsService, pool: ConnectionPool, client: Client) -> None:
    """Инвариант 6: чужой товар не приписывается первому попавшемуся клиенту."""
    outcome = wms.reserve(client.order(seller_external_id=unique("nobody")))

    assert outcome.status == "rejected"
    assert outcome.error_code == "SELLER_MAPPING_MISSING"
    assert outcome.task_id is None
    # Отказ тоже событие: у него свой порядок по заказу WB, иначе повторный
    # опрос неотличим от нового отказа.
    assert [emitted.envelope.type for emitted in outcome.events] == ["wms.reservation.failed.v1"]


def test_no_reservation_touches_wildberries(
        wms: WmsService, client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Инвариант 2: ни одного HTTP-вызова внутри транзакции.

    Проверяется буквально: любой исходящий вызов на время резерва запрещён.
    Вызов в Wildberries занимает около 500 мс — внутри транзакции он уронил бы
    пропускную способность на три порядка (раздел 6.2).
    """
    import httpx

    def forbidden(*_args, **_kwargs):
        raise AssertionError("HTTP-вызов внутри транзакции резерва (инвариант 2)")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)

    client.load(2)
    assert wms.reserve(client.order(quantity=1)).status == "reserved"
