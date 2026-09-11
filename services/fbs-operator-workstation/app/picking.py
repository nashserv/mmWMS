"""Подбор: сессия, лист, сканы, упаковка.

Три вещи, которые здесь важнее остальных.

**Сессия наполняется настоящими сканами.** В боевом контуре
`workstation_pick_session` содержит одну запись на 6497 заданий — то есть
подбора как учтённого действия не существовало вовсе. Каждый скан у стойки
пишется сюда со временем, адресом и тем, кто сканировал.

**Контрольный скан при упаковке жёсткий.** Несовпадение — стоп, упаковка не
продолжается. Это единственное место, где реально проверяется, что в коробке
лежит то, что должно (раздел 4 мастера): ошибку у стеллажа владелец разрешил
не ловить, ошибку в коробке — нет.

**Задание не выдаётся двоим.** За это отвечает `FOR UPDATE SKIP LOCKED` на
стороне wms; рабочему месту достаточно не устраивать вторую гонку на своей
стороне — оно берёт то, что дал сервер, и не додумывает.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import metrics
from .domain import CancelReason, ScanResult, Task, uid
from .projection import Projection
from .puller import Poller
from .store import Store
from .wms_client import WmsClient, WmsRejected, WmsUnavailable

logger = logging.getLogger("workstation.picking")


class PickingRefused(RuntimeError):
    """Отказ, который показывается человеку словами, а не кодом."""


def _picklist_barcode() -> str:
    """Штрихкод листа. Дата — чтобы найденный лист было видно, за какую смену."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"PL-{stamp}-{uid()[:8].upper()}"


class PickingService:
    def __init__(self, client: WmsClient, projection: Projection, store: Store,
                 poller: Poller) -> None:
        self._client = client
        self._projection = projection
        self._store = store
        self._poller = poller

    # ------------------------------------------------------------ сессия

    async def start_session(self, *, actor_id: str, station_id: str | None,
                            limit: int, lease_seconds: int,
                            owner_external_ids: list[str] | None = None) -> dict[str, Any]:
        """Начать обход: занять задания и собрать лист.

        Сессия многопродавцовая по умолчанию: один обход собирает задания
        нескольких клиентов (раздел 4 мастера), поэтому фильтра по владельцу
        нет, пока его не попросили явно.
        """
        # Доступность базы проверяется ДО занятия заданий.
        #
        # Раньше задания занимались, а сессия не заводилась: лист подбора
        # оказывался пустым, сборщик уходил ни с чем, а задания оставались за
        # ним на срок лизинга — пятнадцать минут очередь была пуста для всех,
        # включая его самого.
        if not await self._store.ping():
            raise PickingRefused(
                "база рабочего места недоступна: задания не выданы, чтобы не "
                f"занять их без листа подбора. {self._store.last_error or ''}".strip())

        try:
            tasks: list[Task] = await self._poller.claim(
                assignee=actor_id, limit=limit, lease_seconds=lease_seconds,
                owner_external_ids=owner_external_ids)
        except WmsUnavailable as error:
            raise PickingRefused(f"wms недоступен, задания не выданы: {error}") from error

        note = None
        if not tasks and self._poller.claim_ignored:
            # wms занял задания при чтении экрана (нарушение контракта, заявка 2).
            # Раздаём их сборщику из проекции, иначе смена встанет на пустом
            # экране при полной очереди. Путь включается только по факту
            # нарушения и исчезнет сам, когда поток A сдаст настоящий сервис.
            tasks = await self._poller.adopt_screen_claimed(assignee=actor_id, limit=limit)
            if tasks:
                note = ("задания взяты в обход: wms занимает их при `claim: false`. "
                        "Чинится в потоке 0, заявка 2")

        if not tasks:
            # Пустая очередь — не ошибка. Но и сессию заводить не на что:
            # пустой лист подбора это мусор на столе.
            return {"session_id": None, "tasks": [], "picklist_barcode": None,
                    "note": "свободных заданий нет"}

        await self._enrich_placements(tasks)
        barcode = _picklist_barcode()
        session_id = await self._store.open_session(
            actor_id=actor_id, station_id=station_id, picklist_barcode=barcode)
        ordered = sorted(tasks, key=lambda task: task.route_order)
        if session_id is None:
            # База отказала между проверкой и записью. Задания уже заняты —
            # отдаём человеку список, чтобы он собрал по нему: лист на бумаге
            # лучше, чем пятнадцать минут потерянной очереди.
            logger.warning("сессия не заведена: база рабочего места отказала. "
                           "Заданий на руках: %d", len(ordered))
            return {"session_id": None, "picklist_barcode": None,
                    "actor_id": actor_id, "station_id": station_id,
                    "tasks": [task.as_dict() for task in ordered],
                    "store_degraded": True,
                    "note": "база рабочего места недоступна: лист не сохранён, "
                            "собирайте по списку на экране"}
        await self._store.add_lines(session_id, ordered)
        for task in ordered:
            await self._store.note_task(task, session_id=session_id, station_id=station_id,
                                        actor_id=actor_id)
        metrics.PICK_SESSIONS.set(len(await self._store.open_sessions()))
        logger.info("сессия %s: выдано заданий %d, лист %s", session_id, len(tasks), barcode)
        return {
            "session_id": session_id,
            "picklist_barcode": barcode,
            "actor_id": actor_id,
            "station_id": station_id,
            "tasks": [task.as_dict() for task in ordered],
            "note": note,
        }

    async def _enrich_placements(self, tasks: list[Task]) -> None:
        """Дописать адреса в строки листа, если их не было в ответе выдачи.

        Контракт кладёт их в `PullTask.placements` (`include_extended`), но
        если сервис их не прислал, лист без адресов бесполезен: сборщик
        пойдёт искать вещь глазами по всему складу. Спрашиваем
        `/storage/lookup` — тот же контракт, тот же порядок обхода.
        Один вызов на уникальную пару «владелец + штрихкод», и только при
        старте сессии, а не на каждый опрос экрана.
        """
        missing = [task for task in tasks if not task.placements]
        if not missing:
            return
        cache: dict[tuple[str, str], list[Any]] = {}
        for task in missing:
            key = (task.owner_external_id, task.barcode)
            if key not in cache:
                try:
                    cache[key] = await self._client.storage_lookup(
                        seller_external_id=task.owner_external_id, barcode=task.barcode)
                except WmsUnavailable:
                    cache[key] = []
            task.placements = list(cache[key])

    async def session_view(self, session_id: str) -> dict[str, Any]:
        session = await self._store.session(session_id)
        if not session:
            raise PickingRefused(f"сессия {session_id} не найдена")
        lines = await self._store.session_lines(session_id)
        # Строка листа дополняется живым состоянием задания: лист бумажный,
        # экран — нет, и на экране сборщик должен видеть, что уже отсканировано.
        enriched: list[dict[str, Any]] = []
        for line in lines:
            task = self._projection.get(str(line["task_id"]))
            row = dict(line)
            row["state"] = task.raw_state if task else None
            row["screen_status"] = task.screen.value if task else None
            row["label_ready"] = task.label_ready if task else False
            row["lease_expired"] = _lease_expired(task)
            enriched.append(row)
        return {
            "session": dict(session),
            "lines": enriched,
            "picked": sum(1 for row in enriched if row.get("scan_result") == "ok"),
            "total": len(enriched),
        }

    async def session_by_barcode(self, picklist_barcode: str) -> dict[str, Any]:
        """Найти сессию по штрихкоду бумажного листа."""
        session = await self._store.session_by_barcode(picklist_barcode)
        if not session:
            raise PickingRefused(f"лист {picklist_barcode} не найден")
        return await self.session_view(str(session["id"]))

    async def finish_session(self, session_id: str, *, state: str = "completed") -> dict[str, Any]:
        await self._store.finish_session(session_id, state=state)
        metrics.PICK_SESSIONS.set(len(await self._store.open_sessions()))
        return {"ok": True, "session_id": session_id, "state": state}

    # ------------------------------------------------------------ сканы

    async def scan_at_rack(self, *, task_id: str, barcode: str, actor_id: str,
                           session_id: str | None = None,
                           station_id: str | None = None) -> dict[str, Any]:
        """Скан у стойки.

        Ошибка здесь по решению владельца допустима — её ловит контрольный
        скан при упаковке. Но допустима не значит невидима: отклонённый скан
        записывается, иначе непонятно, где сборщик регулярно берёт не то.
        """
        task = self._projection.get(task_id)
        expected_owner = task.owner_external_id if task else None
        try:
            accepted, result, response = await self._client.scan(
                task_id, barcode, expected_owner=expected_owner)
        except WmsUnavailable as error:
            # Ответ мог потеряться уже после того, как wms применил скан.
            # Правило приложения C: перечитать задание и принять, если оно
            # уже picked с тем же штрихкодом.
            recovered = await self._recover_scan(task_id, barcode)
            if recovered is None:
                raise PickingRefused(f"скан не прошёл, wms недоступен: {error}") from error
            accepted, result, response = True, ScanResult.OK, recovered

        metrics.SCANS.labels(stage="rack", scan_result=result.value).inc()
        await self._store.record_scan(
            task_id=task_id, stage="rack", barcode=barcode, scan_result=result.value,
            accepted=accepted, actor_id=actor_id, station_id=station_id,
            session_id=session_id)

        if accepted:
            refreshed = await self._refresh(task_id)
            if refreshed is not None:
                await self._store.note_task(refreshed, session_id=session_id,
                                            station_id=station_id, actor_id=actor_id,
                                            picked=True)
        return {
            "accepted": accepted,
            "scan_result": result.value,
            "task_id": task_id,
            "barcode": barcode,
            "owner_external_id": response.get("owner_external_id"),
            "message": _scan_message(result, accepted),
        }

    async def _recover_scan(self, task_id: str, barcode: str) -> dict[str, Any] | None:
        """Перечитать задание после потери ответа (правило приложения C)."""
        try:
            task = await self._client.task(task_id)
        except WmsUnavailable:
            return None
        if task is None:
            return None
        if task.raw_state in {"picked", "packed", "labeled"} and task.barcode == barcode:
            self._projection.upsert(task)
            metrics.CONTRACT_FALLBACKS.labels(route="/tasks/{id}/scan",
                                              field="recovered_after_lost_reply").inc()
            return {"status": "picked", "owner_external_id": task.owner_external_id,
                    "recovered": True}
        return None

    # ------------------------------------------------------------ упаковка

    async def pack(self, *, task_id: str, control_barcode: str, actor_id: str,
                   station_id: str | None = None, box_barcode: str | None = None,
                   session_id: str | None = None) -> dict[str, Any]:
        """Упаковка с контрольным сканом. Несовпадение — стоп.

        Проверка стоит и здесь, и на сервере: экран отвечает за красный экран
        и остановку, wms — за то, что задание нельзя перевести в `packed`
        чужим штрихкодом. Одна проверка без другой ничего не стоит.
        """
        task = self._projection.get(task_id) or await self._refresh(task_id)
        if task is None:
            raise PickingRefused(f"задание {task_id} рабочему месту неизвестно")

        control = (control_barcode or "").strip()
        if not control:
            raise PickingRefused("упаковка без контрольного скана не выполняется")

        if control != task.barcode:
            # Красный экран и стоп. Продолжать нельзя, пока не решено:
            # переподбор или расхождение.
            metrics.SCANS.labels(stage="control",
                                 scan_result=ScanResult.WRONG_BARCODE.value).inc()
            await self._store.record_scan(
                task_id=task_id, stage="control", barcode=control,
                scan_result=ScanResult.WRONG_BARCODE.value, accepted=False,
                actor_id=actor_id, station_id=station_id, session_id=session_id)
            return {
                "accepted": False,
                "stop": True,
                "scan_result": ScanResult.WRONG_BARCODE.value,
                "task_id": task_id,
                "expected_barcode": task.barcode,
                "scanned_barcode": control,
                "message": ("в коробке не тот товар: ожидался штрихкод "
                            f"{task.barcode}, отсканирован {control}. "
                            "Упаковка остановлена"),
            }

        try:
            result = await self._client.pack(
                task_id, idempotency_key=f"ws-pack-{task_id}",
                control_scan_barcode=control, box_barcode=box_barcode,
                station_id=station_id, actor_id=actor_id)
        except WmsRejected as error:
            metrics.SCANS.labels(stage="control", scan_result="short").inc()
            raise PickingRefused(
                f"wms не принял упаковку: {error.code or 'без кода'}") from error
        except WmsUnavailable as error:
            raise PickingRefused(f"wms недоступен: {error}") from error

        owner = result.get("owner_external_id")
        if owner and str(owner) != task.owner_external_id:
            # Перепутанный владелец — это отгрузка чужой вещи (инвариант 6).
            raise PickingRefused(
                f"wms вернул владельца {owner}, а задание принадлежит "
                f"{task.owner_external_id}. Упаковка остановлена")

        metrics.SCANS.labels(stage="control", scan_result=ScanResult.OK.value).inc()
        await self._store.record_scan(
            task_id=task_id, stage="control", barcode=control,
            scan_result=ScanResult.OK.value, accepted=True, actor_id=actor_id,
            station_id=station_id, session_id=session_id)

        refreshed = await self._refresh(task_id)
        await self._store.note_task(refreshed or task, session_id=session_id,
                                    station_id=station_id, actor_id=actor_id, packed=True)
        return {
            "accepted": True,
            "stop": False,
            "task_id": task_id,
            "state": (refreshed or task).raw_state,
            "box_barcode": box_barcode,
            "label_ready": (refreshed or task).label_ready,
            "message": "упаковано, можно печатать этикетку",
        }

    # ------------------------------------------------------------ отмена и возврат

    async def cancel(self, *, task_id: str, reason_code: str, comment: str,
                     actor_id: str, handed_over: bool = False) -> dict[str, Any]:
        """Отмена задания. Без причины — не отменяется.

        Инвариант 11 и прямое требование файла 03: в боевом контуре 2645 отмен
        не имеют ни одной причины. Причина здесь состоит из кода (для счёта) и
        текста (для разбора), и оба обязательны.
        """
        try:
            code = CancelReason(reason_code)
        except ValueError as error:
            raise PickingRefused(
                f"причина отмены {reason_code!r} не из списка: "
                f"{', '.join(item.value for item in CancelReason)}") from error
        text = (comment or "").strip()
        if not text:
            raise PickingRefused("отмена требует текстовой причины, а не только кода")

        task = self._projection.get(task_id) or await self._refresh(task_id)
        if task is None:
            raise PickingRefused(f"задание {task_id} рабочему месту неизвестно")

        reason = f"{code.value}: {text}"
        try:
            await self._client.cancel(
                task_id,
                # Детерминированный ключ: повтор отмены не создаёт вторую.
                cancellation_event_id=f"ws-cancel-{task_id}",
                handed_over=handed_over, reason=reason)
        except WmsRejected as error:
            raise PickingRefused(f"wms не принял отмену: {error.code}") from error
        except WmsUnavailable as error:
            raise PickingRefused(f"wms недоступен: {error}") from error

        await self._store.record_cancel(
            task_id=task_id, owner_external_id=task.owner_external_id,
            barcode=task.barcode, reason=reason, reason_code=code.value, actor_id=actor_id)
        self._projection.forget(task_id)
        logger.info("задание %s отменено: %s", task_id, reason)
        return {"ok": True, "task_id": task_id, "cancel_reason": reason,
                "cancel_reason_code": code.value}

    async def return_to_shelf(self, *, task_id: str, cell_address: str, actor_id: str,
                              box_barcode: str | None = None,
                              reason: str | None = None) -> dict[str, Any]:
        """Вернуть товар на полку — обязательно с адресом и причиной.

        Без адреса товар «возвращается» в никуда, и остаток снова начинает
        врать — та самая беда, из-за которой учёта на складе не было.

        Без причины возврат неразбираем: `wms` его отвергнет (инвариант 11), и
        отказ дойдёт до сборщика уже после того, как он отошёл от стойки.
        Спрашиваем здесь, пока он ещё стоит.
        """
        if not (cell_address or "").strip():
            raise PickingRefused("возврат на полку без адреса ячейки не выполняется")
        if not (reason or "").strip():
            raise PickingRefused("возврат на полку без причины не выполняется")
        try:
            await self._client.return_to_shelf(
                task_id, idempotency_key=f"ws-shelf-{task_id}-{uid()[:8]}",
                cell_address=cell_address, box_barcode=box_barcode,
                reason=reason, actor_id=actor_id)
        except WmsRejected as error:
            # Текст отказа — человеку как есть: в нём написано, что не так.
            raise PickingRefused(
                f"wms не принял возврат: {error.message or error.code}") from error
        except WmsUnavailable as error:
            raise PickingRefused(f"wms недоступен: {error}") from error
        await self._refresh(task_id)
        return {"ok": True, "task_id": task_id, "cell_address": cell_address}

    # ------------------------------------------------------------ вспомогательное

    async def _refresh(self, task_id: str) -> Task | None:
        """Перечитать задание в проекцию после своей же команды."""
        try:
            task = await self._client.task(task_id, previous=self._projection.get(task_id))
        except WmsUnavailable:
            return self._projection.get(task_id)
        if task is not None:
            self._projection.upsert(task)
        return task


def _lease_expired(task: Task | None) -> bool:
    if task is None or not task.claim_expires_at:
        return False
    try:
        expires = datetime.fromisoformat(task.claim_expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires < datetime.now(timezone.utc)


def _scan_message(result: ScanResult, accepted: bool) -> str:
    """Экран обязан показать, ЧТО именно не так, а не «ошибка»."""
    if accepted:
        return "штрихкод принят"
    return {
        ScanResult.WRONG_BARCODE: "не тот штрихкод: на полке взяли другую вещь",
        ScanResult.WRONG_OWNER: "товар другого владельца — отгружать нельзя",
        ScanResult.NOT_FOUND: "задание не найдено в wms",
        ScanResult.SHORT: "товара не хватает — нужен переподбор или расхождение",
        ScanResult.OK: "штрихкод принят, но задание не перешло в picked",
    }.get(result, "скан отклонён")
