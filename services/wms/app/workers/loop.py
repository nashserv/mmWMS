"""Общий каркас воркера: цикл, остановка по сигналу, метрика живости.

Вынесен отдельно, чтобы у трёх воркеров не разошлись три разных представления
о том, как останавливаться и как отчитываться. В боевом контуре четыре воркера
не пишут ни строки лога при `Up` и зелёном healthcheck (раздел 3.5) — здесь
каждый цикл виден и в логе, и в метрике.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable

from ..metrics import WORKER_PROCESSED

log = logging.getLogger("wms.worker")


class Worker:
    """Цикл воркера с двумя видами пауз.

    `idle_seconds` — пауза, когда делать было нечего. `min_interval` —
    гарантированный промежуток между началами циклов, даже когда работа есть.

    Второй нужен не для экономии: воркер, крутящийся без пауз, выбирает общий
    на кабинет лимит Wildberries (300 запросов в минуту) и лишает вызовов
    соседей — опрос заданий и публикацию остатка. Один раз это уже случилось:
    сверка нашла себе работу на каждом цикле и за минуту сожгла окно, после
    чего задания перестали доезжать вовсе.
    """

    def __init__(self, name: str, *, idle_seconds: float = 1.0,
                 min_interval: float = 0.0) -> None:
        self.name = name
        self._idle = idle_seconds
        self._min_interval = min_interval
        self._stop = threading.Event()

    def request_stop(self, *_: object) -> None:
        log.info("воркер %s: остановка по сигналу", self.name)
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def sleep(self, seconds: float) -> None:
        """Пауза, прерываемая остановкой: SIGTERM не должен ждать полный цикл."""
        self._stop.wait(max(0.0, seconds))

    def run(self, tick: Callable[[], int]) -> None:
        """Гоняет `tick` до остановки. Возврат — сколько единиц обработано.

        Исключение внутри цикла не убивает воркера: упавший цикл — это один
        неудачный проход, а не повод оставить склад без опроса. Но и молчать
        о нём нельзя, поэтому он попадает в лог целиком.
        """
        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, self.request_stop)
        serve_metrics(self.name)
        log.info("воркер %s запущен", self.name)

        backoff = self._idle
        while not self.stopping:
            started = time.monotonic()
            try:
                processed = tick()
            except Exception:
                log.exception("воркер %s: цикл упал", self.name)
                # Растущая пауза после падения: если упала база, долбить её
                # в полную силу — значит мешать ей подняться.
                backoff = min(backoff * 2, 30.0)
                self.sleep(backoff)
                continue

            backoff = self._idle
            elapsed = time.monotonic() - started
            WORKER_PROCESSED.labels(worker=self.name).inc(max(0, processed))
            if processed:
                log.info("воркер %s: обработано %d за %.0f мс",
                         self.name, processed, elapsed * 1000)
                self.sleep(self._min_interval - elapsed)
            else:
                self.sleep(max(self._idle, self._min_interval - elapsed))
        log.info("воркер %s остановлен", self.name)


def serve_metrics(worker: str) -> int | None:
    """Поднимает /metrics воркера. `None` — порт не задан.

    Без него метрики воркера не видит НИКТО: процесс их считает, Prometheus
    их не забирает, и «воркер молчит» остаётся незамеченным ровно так же, как
    в боевом контуре — четыре воркера стояли Up с зелёным healthcheck и нулём
    строк лога (инвариант 14).

    Порт задаётся переменной `WMS_METRICS_PORT`; в compose он свой у каждого
    воркера.
    """
    raw = (os.getenv("WMS_METRICS_PORT") or "").strip()
    if not raw:
        log.warning("воркер %s: WMS_METRICS_PORT не задан — метрики никто не заберёт, "
                    "и молчание воркера останется незамеченным", worker)
        return None
    try:
        port = int(raw)
    except ValueError:
        log.error("воркер %s: WMS_METRICS_PORT=%r не число", worker, raw)
        return None
    try:
        from prometheus_client import start_http_server

        start_http_server(port)
    except Exception:  # noqa: BLE001 — без метрик воркер работает, но громко жалуется
        log.exception("воркер %s: не удалось поднять /metrics на порту %d", worker, port)
        return None
    log.info("воркер %s: метрики на :%d", worker, port)
    return port


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
