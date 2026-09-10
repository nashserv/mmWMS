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
from typing import Callable

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


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
