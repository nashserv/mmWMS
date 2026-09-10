"""Ограничитель вызовов Wildberries — защита от бана, а не механизм задержки.

Лимит WB: 300 запросов в минуту на кабинет, интервал 200 мс, burst 20,
ответ 409 стоит 10 (приложение D).

Раздел 6.4 задаёт смысл прямо: в нормальной работе очередь пуста и ограничитель
не срабатывает никогда — при 350 заданиях в день это около двух вызовов в
минуту на все кабинеты. Он существует ради одного случая: массовая приёмка на
500 SKU выпустила бы 500 вызовов подряд и получила блокировку кабинета.

Окно ведётся в таблице `wb_rate_limit`, а не в памяти процесса: опросчик,
публикатор остатков и загрузчик стикеров — разные процессы, а лимит у
Wildberries общий на кабинет. Счётчик в памяти позволил бы трём процессам
втроём выбрать тройной лимит.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from psycopg import Cursor

# Приложение D. Стоимость 409 выше обычного вызова: WB считает отказ дороже,
# и продолжать долбить после него — прямой путь к блокировке кабинета.
LIMIT_PER_MINUTE = 300
WINDOW_SECONDS = 60
MIN_INTERVAL_SECONDS = 0.2
BURST = 20
CONFLICT_COST = 10


@dataclass(frozen=True)
class Permit:
    """Разрешение на вызов. `wait_seconds` > 0 — сколько ждать до следующего."""

    allowed: bool
    wait_seconds: float = 0.0
    used: int = 0

    def __bool__(self) -> bool:
        return self.allowed


def take(cursor: Cursor, account_id: uuid.UUID, *, cost: int = 1) -> Permit:
    """Занимает место в минутном окне кабинета.

    Окно выравнивается по минуте, а не скользит: у WB оно именно такое, и
    считать честнее так же, как считает он.
    """
    cursor.execute(
        "INSERT INTO wb_rate_limit (account_id, window_start, used) "
        "VALUES (%(account)s, date_trunc('minute', now()), %(cost)s) "
        "ON CONFLICT (account_id, window_start) DO UPDATE SET "
        "    used = wb_rate_limit.used + %(cost)s "
        "  WHERE wb_rate_limit.used + %(cost)s <= %(limit)s "
        "    AND (wb_rate_limit.blocked_until IS NULL OR wb_rate_limit.blocked_until <= now()) "
        "RETURNING used, "
        "          EXTRACT(EPOCH FROM (window_start + interval '1 minute' - now())) AS wait",
        {"account": account_id, "cost": cost, "limit": LIMIT_PER_MINUTE})
    row = cursor.fetchone()
    if row is None:
        # Либо окно выбрано, либо кабинет под явной блокировкой. И то и другое
        # значит одно: следующий вызов — после окна.
        cursor.execute(
            "SELECT used, GREATEST("
            "    EXTRACT(EPOCH FROM (window_start + interval '1 minute' - now())), "
            "    COALESCE(EXTRACT(EPOCH FROM (blocked_until - now())), 0)) AS wait "
            "  FROM wb_rate_limit "
            " WHERE account_id = %s AND window_start = date_trunc('minute', now())",
            (account_id,))
        current = cursor.fetchone() or {"used": LIMIT_PER_MINUTE, "wait": WINDOW_SECONDS}
        return Permit(False, max(0.0, float(current["wait"] or 0)), int(current["used"] or 0))
    return Permit(True, 0.0, int(row["used"]))


def block(cursor: Cursor, account_id: uuid.UUID, seconds: float) -> None:
    """Кабинет получил 429 или 409: придерживаем очередь до конца паузы."""
    cursor.execute(
        "INSERT INTO wb_rate_limit (account_id, window_start, used, blocked_until) "
        "VALUES (%(account)s, date_trunc('minute', now()), %(cost)s, "
        "        now() + make_interval(secs => %(seconds)s)) "
        "ON CONFLICT (account_id, window_start) DO UPDATE SET "
        "    used = wb_rate_limit.used + %(cost)s, "
        "    blocked_until = GREATEST(COALESCE(wb_rate_limit.blocked_until, now()), "
        "                             now() + make_interval(secs => %(seconds)s))",
        {"account": account_id, "cost": CONFLICT_COST, "seconds": float(seconds)})


class Pace:
    """Интервал между вызовами внутри процесса: 200 мс, burst 20.

    Минутное окно живёт в базе, а темп — здесь: спрашивать базу перед каждым
    вызовом ради паузы в 200 мс дороже самой паузы.
    """

    def __init__(self, *, interval: float = MIN_INTERVAL_SECONDS, burst: int = BURST) -> None:
        self._interval = interval
        self._burst = burst
        self._allowance = float(burst)
        self._last = time.monotonic()

    def wait(self) -> None:
        now = time.monotonic()
        self._allowance = min(float(self._burst),
                              self._allowance + (now - self._last) / self._interval)
        self._last = now
        if self._allowance >= 1.0:
            self._allowance -= 1.0
            return
        delay = (1.0 - self._allowance) * self._interval
        time.sleep(delay)
        self._last = time.monotonic()
        self._allowance = 0.0
