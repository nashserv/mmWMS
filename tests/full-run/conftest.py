"""Обвязка полного прогона: подключения к стенду и читаемый красный.

Пункт 9 файла `01-stream-0-contracts-and-stand.md`: на старте потока 0
большинство проверок падает, потому что `wms` — это mock. Это и есть смысл
скрипта. Поэтому здесь нет ни одного `skip` и ни одного `xfail`: красное
обязано остаться красным.

Но красное обязано быть ещё и понятным. Отсюда итоговая таблица «шаг /
владелец / статус / чего не хватает» — по ней видно, чей поток чинит, не
разбирая трассировки.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any
from collections.abc import Iterator

import pytest

from runner import Bus, Db, Wms
from scenario import Scenario

# Шаги раздела 9.6 мастера — названия и владельцы дословно, включая порядок.
# Владелец в скобках у мастера — это поток, который чинит красный.
STEPS: dict[int, tuple[str, str]] = {
    1: ("Завести клиента, ячейки, загрузить начальный остаток", "C, A"),
    2: ("Приёмка 3 SKU в коробки и ячейки", "A, B"),
    3: ("Приёмка с недостачей", "A, B"),
    4: ("Симулятор WB отдаёт 5 заданий", "A"),
    5: ("Задание на немаппленный товар", "A"),
    6: ("Задание на товар с нулевым остатком, allow_ledger_short=true", "A"),
    7: ("Стикеры", "A"),
    8: ("Пять параллельных сессий подбора тянут задания", "A, B"),
    9: ("Подбор, скан у стойки, упаковка с контрольным сканом", "B"),
    10: ("Печать", "B"),
    11: ("Поставка и передача", "A, B"),
    12: ("Начисления", "C"),
    13: ("Отмена задания после выдачи стикера", "A, B"),
    14: ("RabbitMQ выключен", "A, B"),
    15: ("Публикация остатка", "A"),
    16: ("Нагрузка: 10 000 заданий в час", "A"),
}

_STEP_IN_NAME = re.compile(r"test_step_(\d{2})_")

# Итог каждого шага: статус и одна строка «чего не хватает».
_RESULTS: dict[int, dict[str, str]] = {}


# --------------------------------------------------------------- подключения


@pytest.fixture(scope="session")
def scenario() -> Scenario:
    return Scenario()


@pytest.fixture(scope="session")
def wms() -> Iterator[Wms]:
    """Сервис `wms` на стенде. По умолчанию — проброшенный порт compose."""
    client = Wms(os.getenv("WMS_BASE_URL", "http://127.0.0.1:8080"))
    yield client
    client.close()


@pytest.fixture(scope="session")
def db() -> Iterator[Db]:
    """База `wms`.

    Адрес обязателен: шаги 1, 2, 4 и далее утверждают о `stock_balance` и
    `stock_move`, и подменить это чтением API нельзя — как раз в расхождении
    между ответом сервиса и журналом и живут все беды раздела 3.
    """
    from runner import env

    connection = Db(env("DATABASE_URL"))
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def bus() -> Iterator[Bus]:
    """Подписка на `mmx.events` на весь прогон.

    Сессионная, а не по тесту: событие, вылетевшее до подписки, потеряно, а
    шаг 12 разбирает начисления по событиям более ранних шагов.
    """
    from runner import env

    tap = Bus(env("RABBITMQ_URL"), os.getenv("MMX_EVENTS_EXCHANGE", "mmx.events")).start()
    yield tap
    tap.stop()


@pytest.fixture(scope="session", autouse=True)
def tidy_stand(wms: Wms, db: Db) -> Iterator[None]:
    """Прогон убирает за собой — и до, и после.

    Клиент прогона один и тот же между запусками (так проверяется
    идемпотентность, инвариант 5), а вот задания копятся. Больше всех оставляет
    нагрузочный шаг 16: около 160 штук в `reserved`, и никто их не разбирает —
    шаги 9–11 принадлежат потоку B и на срезе `-m stream_a` не выполняются
    вовсе.

    Выдача идёт по сроку WB (`ORDER BY deadline`), поэтому на следующем запуске
    первыми уходят вчерашние задания, а не сегодняшние: шаг 8 краснеет с
    «задания не выданы никому», хотя выдача работает. Замер потока A: после
    нескольких прогонов 283 задания в `reserved`, из них свободных 36.

    Отмена — законная операция с причиной (инвариант 11): товар возвращается на
    полку, и стенд остаётся в том состоянии, в каком прогон его застал. Уборка
    до запуска нужна отдельно: она чинит уже накопленное, не дожидаясь, пока
    все прогоны станут аккуратными.
    """
    _purge_run_data(db, "до прогона")
    _reset_simulator("до прогона")
    _wait_for_the_cabinet_limit(db)
    yield
    _purge_run_data(db, "после прогона")
    _reset_simulator("после прогона")


def _wait_for_the_cabinet_limit(db: Db) -> None:
    """Ждёт, пока освободится окно ограничителя кабинета прогона.

    Нагрузочный шаг 16 заводит около 160 заданий за двадцать секунд, и каждое
    движение публикует остаток — это сотни вызовов в один кабинет при лимите
    Wildberries 300 в минуту (приложение D). Ограничитель честно придерживает
    очередь, ровно как описано в разделе 6.4, и кабинет остаётся закрытым до
    конца минуты.

    Для самого прогона это значит, что два запуска подряд не проходят: шаг 4
    ждёт задания две секунды, а опрос кабинета в это время отбит лимитом.
    Поэтому прогон дожидается свободного окна ДО начала — так же, как
    разбирает за собой незакрытые задания. Окно не сбрасывается: сброс
    ограничителя спрятал бы ровно то поведение, ради которого он есть.
    """
    from scenario import WB_ACCOUNT

    # Сколько кабинет должен оставаться свободным, прежде чем считать окно
    # устоявшимся.
    settle_s = 3.0

    # Публикация остатка уходит фоново, вне транзакции (инвариант 2), поэтому
    # отмены, сделанные уборкой, догорают уже после неё. Ждём не «сейчас
    # свободно», а «свободно и остаётся свободным»: иначе прогон стартует в
    # промежутке, а блокировка приезжает через полсекунды — и краснеет шаг 4.
    deadline = time.monotonic() + 120
    clear_since = None
    warned = False
    while time.monotonic() < deadline:
        try:
            # Придержать кабинет могут с двух сторон: наш ограничитель
            # (`wb_rate_limit.blocked_until`) и сам Wildberries, ответивший 429
            # — тогда опросчик отодвигает `next_sync_at` и ставит кабинету
            # статус RATE_LIMITED. Ждать надо обе.
            blocked = db.value(
                "SELECT GREATEST( "
                "         COALESCE(MAX(EXTRACT(EPOCH FROM (rl.blocked_until - now()))), 0), "
                "         COALESCE(MAX(EXTRACT(EPOCH FROM (a.next_sync_at - now()))), 0)) AS s "
                "  FROM wb_account a "
                "  LEFT JOIN wb_rate_limit rl ON rl.account_id = a.id "
                " WHERE a.external_id = %s "
                "   AND (rl.blocked_until > now() "
                "        OR (a.sync_error_code IS NOT NULL AND a.next_sync_at > now()))",
                (WB_ACCOUNT,))
        except Exception:  # noqa: BLE001 — ожидание не имеет права ронять прогон
            return
        if not blocked or float(blocked) <= 0:
            if clear_since is None:
                clear_since = time.monotonic()
            if time.monotonic() - clear_since >= settle_s:
                return
            time.sleep(0.5)
            continue
        clear_since = None
        if not warned:
            print(f"\nкабинет {WB_ACCOUNT} придержан ограничителем — жду окно "
                  f"({float(blocked):.0f} с); это след прошлого прогона, не поломка")
            warned = True
        time.sleep(2)


def _purge_run_data(db: Db, when: str) -> None:
    """Удалить данные прогона целиком. Не отменить — удалить.

    Отмена через API была неверна дважды. Она складская операция: возвращает
    товар на полку, пишет движения, публикует остаток — сто шестьдесят отмен
    подряд выбирали лимит кабинета, и следующий прогон не мог опросить WB
    вовремя. И симулятор об отмене не знает: у него заказ остаётся `new`, у нас
    становится `cancelled`, сверка честно называет это расхождением — так
    накопился 401 `diverged` из заданий, которых давно нет.

    Данные прогона синтетические и пересоздаются следующим запуском, поэтому их
    правильно удалять. Подробности и обоснование по инварианту 3 — в самом
    purge.sql.
    """
    from scenario import LOAD_SELLER, SELLER

    script = (Path(__file__).resolve().parent / "purge.sql").read_text(encoding="utf-8")
    sellers = [SELLER, LOAD_SELLER]
    try:
        # Список клиентов — отдельным параметризованным запросом: psycopg не
        # выполняет многооператорный скрипт с параметрами, а подставлять имена
        # в текст руками нельзя.
        db.execute_script(
            "DROP TABLE IF EXISTS run_owner; DROP TABLE IF EXISTS run_owner_name;")
        db.execute("CREATE TEMP TABLE run_owner_name AS "
                   "SELECT unnest(%s::text[]) AS seller_external_id", (sellers,))
        db.execute("CREATE TEMP TABLE run_owner AS SELECT id FROM owner "
                   " WHERE seller_external_id = ANY(%s)", (sellers,))
        db.execute_script(script)
        db.execute_script("DROP TABLE IF EXISTS run_owner; "
                          "DROP TABLE IF EXISTS run_owner_name;")
    except Exception as failure:  # noqa: BLE001 — уборка не имеет права ронять прогон
        print(f"\nуборка {when}: не удалось убрать данные прогона ({failure})")
        return
    print(f"\nуборка {when}: данные прогона удалены")


def _reset_simulator(when: str) -> None:
    """Сбросить симулятор WB — вторую сторону той же картины.

    Без этого у него остаются заказы прогона, которых в нашей базе уже нет:
    сверка видит несовпадение и плодит `diverged` на пустом месте.
    """
    import httpx

    base = (os.getenv("WB_SIMULATOR_URL") or "http://127.0.0.1:8090").rstrip("/")
    try:
        httpx.post(f"{base}/__stand__/reset", timeout=10.0).raise_for_status()
    except Exception as failure:  # noqa: BLE001
        print(f"\nуборка {when}: симулятор не сбросился ({failure})")


@pytest.fixture(scope="session")
def ctx() -> dict[str, Any]:
    """То, что шаги передают друг другу.

    Прогон — один сценарий склада, а не шестнадцать независимых тестов:
    задания из шага 4 подбираются в шаге 8 и отгружаются в шаге 11. Шаг, до
    которого сценарий не дошёл, обязан покраснеть с указанием на первый
    сломавшийся, а не молча пропуститься.
    """
    return {}


# ------------------------------------------------------------------ маркеры


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "stream_a: красный чинит поток A")
    config.addinivalue_line("markers", "stream_b: красный чинит поток B")
    config.addinivalue_line("markers", "stream_c: красный чинит поток C")


# ------------------------------------------------------------------- отчёт


def _step_of(nodeid: str) -> int | None:
    match = _STEP_IN_NAME.search(nodeid)
    return int(match.group(1)) if match else None


_EXC_PREFIX = re.compile(r"^[\w.]*(NotReady|AssertionError|Failed|Error|Exception):\s*")


def _reason(report: pytest.TestReport) -> str:
    """Одна строка «чего не хватает» вместо простыни трассировки."""
    crash = getattr(report.longrepr, "reprcrash", None)
    message = crash.message if crash else str(report.longrepr)
    message = message.splitlines()[0] if message else ""
    return _EXC_PREFIX.sub("", message).strip()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    step = _step_of(report.nodeid)
    if step is None:
        return
    entry = _RESULTS.setdefault(step, {"status": "ЗЕЛЁНЫЙ", "reason": ""})
    if report.failed:
        # Падение в setup — это не «тест сломался», это «стенда нет».
        entry["status"] = "КРАСНЫЙ" if report.when == "call" else "СТЕНД"
        entry["reason"] = _reason(report)
    elif report.skipped:
        # Пропусков в полном прогоне быть не должно: пропуск читается как
        # успех, а скрипт существует ровно ради обратного.
        entry["status"] = "ПРОПУЩЕН"
        entry["reason"] = "пропуск запрещён пунктом 9 файла 01"


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Итоговая таблица: шаг, владелец, статус, чего не хватает.

    Причина печатается отдельной строкой под шагом, а не в колонке: она —
    самое важное в этом отчёте, и обрезать её ради ровных столбцов значит
    потерять то, ради чего отчёт заведён.
    """
    write = terminalreporter.write_line
    write("")
    write("=" * 100)
    write("ПОЛНЫЙ ПРОГОН — раздел 9.6 мастер-контекста, 16 проверок")
    write("Владелец — поток, который чинит красный (правило 9.5.5).")

    # Из какого коммита собран стенд. Прогон без этой строки говорит «зелёно»,
    # но не говорит «зелёно У ЧЕГО»: образ мог быть собран вчера из другого
    # клона, и выглядел бы он точно так же.
    from runner import repository_revision, stand_revision

    stand, repo = stand_revision(), repository_revision()
    if stand == repo:
        write(f"Стенд собран из {stand[:12]} — тот же коммит, что в рабочем каталоге.")
    else:
        write(f"ВНИМАНИЕ: стенд собран из {stand[:12]}, а в рабочем каталоге {repo[:12]}.")
        write("Прогон проверяет ОБРАЗ СТЕНДА, а не то, что лежит в файлах.")
    write("=" * 100)
    write(f"{'шаг':>3}  {'владелец':<9} {'статус':<10} проверка")
    write("-" * 100)

    green = 0
    for number, (title, owner) in STEPS.items():
        entry = _RESULTS.get(number)
        if entry is None:
            status, reason = "не гонялся", "срез маркера или прогон прерван"
        else:
            status, reason = entry["status"], entry["reason"]
        if status == "ЗЕЛЁНЫЙ":
            green += 1
        write(f"{number:>3}  {owner:<9} {status:<10} {title}")
        if reason:
            write(f"{'':>3}  {'':<9} {'':<10} └ {_cut(reason, 150)}")

    write("-" * 100)
    write(f"Зелёных {green} из {len(STEPS)}.")
    if green < len(STEPS):
        write("Красное — не поломка прогона, а список того, что ещё не работает.")
        write("Срез одного потока: pytest -m stream_a (stream_b, stream_c).")
    write("=" * 100)
