"""Агент печати: кто он и на что имеет право отвечать.

Две дыры, закрытые здесь.

Первая: в сокет приходил кто угодно и объявлял себя станцией — печатал чужие
стикеры и подтверждал чужие задания. Теперь агент предъявляет общий секрет
станции, и вне локальных сред он обязателен.

Вторая: `resolve` принимал результат от ЛЮБОГО подключённого агента. На складе
пять станций и пять принтеров (раздел 4); чужое подтверждение означает, что
«напечатано» — это «кто-то сказал, что напечатал», а стикер при этом уехал на
чужую коробку.
"""
from __future__ import annotations

import asyncio

from app.agent_hub import AgentHub, AgentSession


class FakeSocket:
    """Сокет, который только запоминает отправленное."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


def run(coro):
    """В этом наборе async гоняется так же, как у соседей: отдельного плагина
    для pytest у сервиса нет, и вводить его ради трёх тестов незачем."""
    return asyncio.run(coro)


def session(station_id: str) -> AgentSession:
    return AgentSession(station_id=station_id, station_name=station_id,
                        websocket=FakeSocket(), printer_name=None,
                        transport="agent", capabilities={}, confirmed_format=None)


def test_a_foreign_station_cannot_close_someone_elses_job() -> None:
    run(_test_a_foreign_station_cannot_close_someone_elses_job())


async def _test_a_foreign_station_cannot_close_someone_elses_job() -> None:
    """Подтверждение принимается только от той станции, которой печать и слали."""
    hub = AgentHub()
    ours, theirs = session("station-1"), session("station-2")
    await hub.register(ours)
    await hub.register(theirs)

    printing = asyncio.create_task(hub.send_print(
        station_id="station-1", job_id="job-1", task_id="t-1", payload=b"^XA^XZ",
        label_format="zplv", content_type="application/x-zpl", wait_ack=True))
    await asyncio.sleep(0.05)

    job_id = ours.websocket.sent[-1]["job_id"]

    # Чужая станция пытается закрыть наше задание.
    assert hub.resolve(job_id, {"ok": True, "write_ms": 1.0},
                       station_id="station-2") is False, \
        "чужая станция закрыла задание печати"
    assert not printing.done(), "печать считается подтверждённой чужим ответом"

    # Своя — закрывает.
    assert hub.resolve(job_id, {"ok": True, "write_ms": 1.0},
                       station_id="station-1") is True
    result = await asyncio.wait_for(printing, timeout=2)
    assert result["ok"] is True


def test_an_unknown_job_is_not_resolved() -> None:
    run(_test_an_unknown_job_is_not_resolved())


async def _test_an_unknown_job_is_not_resolved() -> None:
    """Ответ на задание, которого не было, — это не успех, а сигнал."""
    hub = AgentHub()
    assert hub.resolve("никому-не-отправляли", {"ok": True}) is False


def test_the_job_owner_is_forgotten_after_the_job_is_done() -> None:
    run(_test_the_job_owner_is_forgotten_after_the_job_is_done())


async def _test_the_job_owner_is_forgotten_after_the_job_is_done() -> None:
    """Память о заданиях не растёт вечно: иначе за смену это десятки тысяч
    строк, которые никто не читает."""
    hub = AgentHub()
    ours = session("station-1")
    await hub.register(ours)

    printing = asyncio.create_task(hub.send_print(
        station_id="station-1", job_id="job-2", task_id="t-2", payload=b"^XA^XZ",
        label_format="zplv", content_type="application/x-zpl", wait_ack=True))
    await asyncio.sleep(0.05)
    job_id = ours.websocket.sent[-1]["job_id"]
    hub.resolve(job_id, {"ok": True, "write_ms": 1.0}, station_id="station-1")
    await asyncio.wait_for(printing, timeout=2)

    assert hub.resolve(job_id, {"ok": True}, station_id="station-1") is False, \
        "задание осталось в памяти после завершения"

