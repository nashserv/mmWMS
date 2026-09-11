"""Публикация остатков в Wildberries — немедленно, без таймеров.

Раздел 6.4 не оставляет места для толкования: остаток изменился — вызов уходит
сразу. Никакого накопления по расписанию, никаких «раз в минуту». Ограничитель
существует только как защита от бана и в нормальной работе не срабатывает
никогда: при 350 заданиях в день это около двух вызовов в минуту на все
кабинеты при лимите 300.

Единственное следствие того, что HTTP занимает время: если для кабинета вызов
уже в полёте, изменение уезжает следующим, который стартует в момент возврата
текущего. Это конвейер, а не задержка. Если за время полёта изменилось
несколько SKU — они уедут одним запросом, что не медленнее и экономнее по
лимиту.

Публикуется заниженный остаток: `available = good − reserved − buffer`
(инвариант 7). Занижать всегда — непроданный товар дешевле проданного
несуществующего.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from . import rate_limit, repositories as repo
from .metrics import STOCK_PUSH, STOCK_PUSH_DELAY
from .postgres import ConnectionPool, single
from .secrets import SecretUnavailable
from .wb import WbClient, WbError, writes_allowed

log = logging.getLogger("wms.stock_push")


class StockPublisher:
    """Конвейер публикаций: один вызов в полёте на кабинет.

    Не очередь с задержкой, а именно конвейер. Пока вызов летит, изменения
    копятся; как только он вернулся, накопленное уезжает следующим вызовом.
    В спокойной работе накапливать нечего, и изменение уходит сразу.
    """

    def __init__(self, pool: ConnectionPool, *, max_workers: int = 4) -> None:
        self._pool = pool
        self._lock = threading.Lock()
        # Кабинет → накопленные с момента старта текущего вызова SKU.
        self._pending: dict[uuid.UUID, set[uuid.UUID]] = {}
        self._in_flight: set[uuid.UUID] = set()
        self._marked_at: dict[uuid.UUID, float] = {}
        self._threads: set[threading.Thread] = set()
        self._max_workers = max_workers
        self._stopped = False

    # ------------------------------------------------------------ уведомление

    def notify(self, owner_id: uuid.UUID | None, sku_ids: set[uuid.UUID]) -> None:
        """Остаток по этим SKU изменился. Вызывается ПОСЛЕ коммита.

        Никогда изнутри транзакции: вызов в Wildberries занимает около 500 мс,
        и внутри блокировки это уронило бы пропускную способность на три
        порядка (инвариант 2).

        Ключ — владелец товара, а не кабинет: тот, кто двигает остаток, знает
        чей это товар, а в какие кабинеты его публиковать — дело публикатора.
        """
        account_id = owner_id
        if account_id is None or not sku_ids or self._stopped:
            return
        with self._lock:
            self._pending.setdefault(account_id, set()).update(sku_ids)
            self._marked_at.setdefault(account_id, time.monotonic())
            if account_id in self._in_flight:
                # Вызов уже летит — накопленное уедет следующим, который
                # стартует в момент его возврата.
                return
            if len(self._threads) >= self._max_workers:
                return
            self._in_flight.add(account_id)
            thread = threading.Thread(
                target=self._drain_account, args=(account_id,),
                name=f"stock-push-{str(account_id)[:8]}", daemon=True)
            self._threads.add(thread)
        thread.start()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Ждёт, пока конвейер опустеет. Нужен тестам и остановке сервиса."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._in_flight and not any(self._pending.values()):
                    return True
            time.sleep(0.01)
        return False

    def stop(self) -> None:
        self._stopped = True
        for thread in list(self._threads):
            thread.join(timeout=2.0)

    # ------------------------------------------------------------- отправка

    def _drain_account(self, account_id: uuid.UUID) -> None:
        """Выгребает накопленное по владельцу, пока оно не кончится."""
        try:
            current: uuid.UUID = account_id
            while True:
                with self._lock:
                    skus = self._pending.pop(current, set())
                    marked = self._marked_at.pop(current, None)
                    if not skus:
                        self._in_flight.discard(current)
                        # Очередь своего владельца пуста — забираем чужую,
                        # если её некому взять. Иначе накопленное у владельца,
                        # чей поток уже завершился, лежит до следующего
                        # движения по нему: остаток в Wildberries отстаёт
                        # неизвестно насколько (инвариант 7).
                        orphan = self._orphaned()
                        if orphan is None:
                            return
                        current = orphan
                        continue
                self._push(current, skus, marked)
        except Exception:
            log.exception("публикация остатка кабинета %s оборвалась", account_id)
            with self._lock:
                self._in_flight.discard(account_id)
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    def _orphaned(self) -> uuid.UUID | None:
        """Владелец с накопленным остатком, за которым никто не пришёл.

        Вызывается под `self._lock`.
        """
        for owner_id, skus in self._pending.items():
            if skus and owner_id not in self._in_flight:
                self._in_flight.add(owner_id)
                return owner_id
        return None

    def _push(self, owner_id: uuid.UUID, sku_ids: set[uuid.UUID],
              marked_at: float | None) -> None:
        """Публикует остаток владельца во все его кабинеты, которым можно писать."""
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                accounts = repo.accounts_of_owner(cursor, owner_id)
                rows = repo.available_for_push(cursor, owner_id, sku_ids)
        if not rows:
            return

        payload = [{"sku": row["barcode"], "amount": int(row["available"])} for row in rows]
        for account in accounts:
            if not writes_allowed(account["mode"]):
                # В shadow в Wildberries не пишут вообще ничего (раздел 11).
                continue
            if not account.get("wb_warehouse_id"):
                # Раньше подставлялся склад №1 — чужой. Остаток клиента
                # уезжал на склад, которого у него нет, и WB его либо
                # отвергал, либо принимал не туда. Молчать об этом нельзя:
                # инвариант 7 обещает опубликованный остаток, а он не
                # публикуется вовсе.
                STOCK_PUSH.labels(outcome="no_warehouse").inc()
                log.error("кабинет %s: не задан wb_warehouse_id — остаток "
                          "не публикуется. Инвариант 7 не выполняется для "
                          "этого кабинета, пока склад не задан",
                          account["external_id"])
                continue
            self._push_one(account, payload, sku_ids, marked_at)

    def _push_one(self, account: dict[str, Any], payload: list[dict[str, Any]],
                  sku_ids: set[uuid.UUID], marked_at: float | None) -> None:
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                permitted = bool(rate_limit.take(cursor, account["id"]))
        if not permitted:
            # Массовая приёмка на 500 SKU выпустила бы 500 вызовов и получила
            # блокировку кабинета — единственный случай, ради которого
            # ограничитель существует. Накопленное возвращается в очередь.
            with self._lock:
                self._pending.setdefault(account["owner_id"], set()).update(sku_ids)
            log.info("кабинет %s: окно лимита выбрано, публикация подождёт",
                     account["external_id"])
            time.sleep(1.0)
            return

        outcome = "ok"
        try:
            with WbClient(account_external_id=account["external_id"],
                          secret_ref=account["secret_ref"]) as client:
                client.put_stocks(account["wb_warehouse_id"], payload)
        except (WbError, SecretUnavailable) as failure:
            outcome = "error"
            log.warning("кабинет %s: остаток не опубликован (%s)",
                        account["external_id"], failure)
        finally:
            with self._pool.connection() as connection:
                with single(connection) as cursor:
                    repo.record_stock_push(cursor, account_id=account["id"],
                                           rows=len(payload), result={"status": outcome})
            STOCK_PUSH.labels(outcome=outcome).inc()
            if marked_at is not None:
                STOCK_PUSH_DELAY.observe(max(0.0, time.monotonic() - marked_at))


_publisher: StockPublisher | None = None
_publisher_lock = threading.Lock()


def publisher(pool: ConnectionPool) -> StockPublisher:
    global _publisher
    with _publisher_lock:
        if _publisher is None:
            _publisher = StockPublisher(pool)
        return _publisher


def reset_publisher() -> None:
    global _publisher
    with _publisher_lock:
        if _publisher is not None:
            _publisher.stop()
        _publisher = None
