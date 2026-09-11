"""Наблюдатель за транзакциями отличает сеть от дрожания планировщика.

Проверка шага 4 доказывает инвариант 2 — внутри транзакции нет HTTP-вызовов —
по отпечатку в `pg_stat_activity`: бэкенд сидит `idle in transaction` и ждёт
клиента. Отпечаток настоящий, но у него есть второе, безобидное население:
промежуток между двумя операторами одной транзакции.

Порог между этими населениями стоял на 50 мс и на загруженном стенде ловил
второе вместо первого — прогон покраснел на образце 51.1 мс, где транзакцию
держал `INSERT INTO outbox`, последний оператор перед коммитом. HTTP-вызова
после него нет: `wb_sync._fetch` закрывает соединение ДО разговора с WB, и
резерв идёт отдельной транзакцией.

Тест здесь не про стенд, а про саму линейку: он ничего не поднимает и не
ходит в базу.
"""

from __future__ import annotations

from runner import TxnSample, TxnWatch


def _sample(age_ms: float, query: str = "INSERT INTO outbox ...") -> TxnSample:
    return TxnSample(state="idle in transaction", age_ms=age_ms,
                     wait_event_type="Client", query=query)


def _watch(*samples: TxnSample) -> TxnWatch:
    watch = TxnWatch.__new__(TxnWatch)
    watch.samples = list(samples)
    return watch


def test_a_gap_between_two_statements_is_not_a_network_call() -> None:
    """Полтора процента сверх порога — это планировщик, а не Wildberries.

    Ровно этот образец — 51.1 мс на `INSERT INTO outbox` — покрасил шаг 4 на
    прогоне 19:14. До калибровки порога тест падал здесь.
    """
    assert _watch(_sample(51.143)).idle_in_transaction == []


def test_a_call_to_wildberries_is_still_caught() -> None:
    """Вызов в настоящий шлюз — около 500 мс (раздел 6.4) — виден с запасом.

    Если этот тест покраснеет, значит порог подняли до бессмысленного.
    """
    caught = _watch(_sample(497.0, query="SELECT ... FROM wms_task FOR UPDATE"))
    assert len(caught.idle_in_transaction) == 1


def test_the_threshold_sits_between_the_two_populations() -> None:
    """Порог обязан разделять два населения, а не задевать край одного.

    Дрожание планировщика на загруженном стенде доходит до полусотни
    миллисекунд, вызов в WB — около пятисот. Порог, прижатый к любому из
    краёв, перестаёт что-либо доказывать: снизу он краснеет на ровном месте,
    сверху пропускает то, ради чего заведён.
    """
    assert TxnWatch.IDLE_LIMIT_MS > 100.0, "порог задевает дрожание планировщика"
    assert TxnWatch.IDLE_LIMIT_MS < 400.0, "порог пропустит вызов в Wildberries"


def test_a_backend_that_is_not_idle_is_not_counted() -> None:
    """Работающий бэкенд ждёт не клиента, а свою же работу."""
    busy = TxnSample(state="active", age_ms=900.0,
                     wait_event_type=None, query="UPDATE stock_balance ...")
    assert _watch(busy).idle_in_transaction == []
