"""Экран начальника склада.

Смысл экрана — показать то, что раньше молчало. В боевом контуре молчало почти
всё: 2467 расхождений статуса с Wildberries, 2645 отмен без единой причины,
сборка без остатка, срабатывавшая без следа, и четыре воркера с нулём строк
лога при зелёном healthcheck.

Поэтому здесь нет ни одного блока, который при отсутствии данных показывает
пусто и молчит. Если данных нет — экран говорит, почему их нет. Ложное «всё
хорошо» опаснее честного «проверить нечем».
"""
from __future__ import annotations

from typing import Any

from .domain import now
from .projection import Projection
from .receiving import ReceivingRefused, ReceivingService
from .store import Store

# Заявка потоку 0. Схема wms ведёт `discrepancy(kind='ledger_short')` и даже
# держит под неё индекс с комментарием «экран начальника склада» — но маршрута,
# которым этот список можно прочитать, в замороженном контракте нет.
# Полностью — docs/stream-b-requests.md, заявка 1.
LEDGER_SHORT_GAP = (
    "Клапан «собрать без остатка» (раздел 6.5) пишет discrepancy(kind='ledger_short'), "
    "но прочитать их контрактом нечем: /receipts/screen отдаёт только расхождения "
    "приёмки, у ledger_short receipt_id пуст. Нужен маршрут /discrepancies "
    "(заявка потоку 0). До него этот блок пуст не потому, что сборок без остатка "
    "нет, а потому, что их нечем прочитать."
)


class SupervisorService:
    def __init__(self, projection: Projection, store: Store,
                 receiving: ReceivingService) -> None:
        self._projection = projection
        self._store = store
        self._receiving = receiving

    async def screen(self) -> dict[str, Any]:
        return {
            "generated_at": now(),
            "full_path": self.full_path(),
            "diverged": self._diverged(),
            "discrepancies": await self._discrepancies(),
            "ledger_short": await self._ledger_short(),
            "rejected_scans": await self._store.rejected_scans(limit=25),
            "printing": await self._printing(),
            "cancellations": await self._cancellations(),
            "sessions": await self._store.open_sessions(),
        }

    # ------------------------------------------------------------ полный путь

    def full_path(self) -> dict[str, Any]:
        """Три числа подряд: сколько в wms, сколько забрано опросом, сколько на экране.

        Расхождение между соседними — это и есть «заказы иногда не падают в
        приложение», выраженное числом. Раньше его никто не считал.
        """
        snapshot = self._projection.snapshot()
        available = snapshot.get("available_in_wms")
        on_screen = snapshot.get("on_screen") or 0
        gap = None
        if isinstance(available, int):
            # Ждущие подбора — подмножество показанного: на экране лежат ещё и
            # взятые в работу. Дыра — это когда wms знает о заданиях, которых
            # на экране нет вовсе.
            gap = max(0, available - on_screen)
        return {
            "available_in_wms": available,
            "on_screen": on_screen,
            "gap": gap,
            "stale": snapshot.get("stale"),
            "last_poll_ok": snapshot.get("last_poll_ok"),
            "last_error": snapshot.get("last_error"),
            "served_at": snapshot.get("served_at"),
        }

    # ------------------------------------------------------------ блоки

    def _diverged(self) -> dict[str, Any]:
        """Расхождение с WB — состояние и алерт, а не тихая запись (инвариант 10)."""
        tasks = self._projection.diverged()
        return {
            "count": len(tasks),
            "tasks": [task.as_dict() for task in tasks[:50]],
            "note": ("наш статус разошёлся со статусом Wildberries. В боевом контуре "
                     "таких заданий 2467 из 6374, и все они молчали"),
        }

    async def _discrepancies(self) -> dict[str, Any]:
        try:
            screen = await self._receiving.receipts_screen(limit=100)
        except ReceivingRefused as error:
            return {"available": False, "reason": str(error), "items": []}
        items: list[dict[str, Any]] = []
        for receipt in screen.get("receipts", []):
            for item in receipt.get("discrepancies", []):
                row = dict(item)
                row["reference"] = receipt.get("reference")
                row["owner_external_id"] = receipt.get("owner_external_id")
                items.append(row)
        pending = [row for row in items if (row.get("decision") or "pending") == "pending"]
        return {"available": True, "items": items, "pending": len(pending),
                "total": len(items)}

    async def _ledger_short(self) -> dict[str, Any]:
        """Сборка без остатка — то, ради чего клапан 6.5 сделали видимым.

        Пока маршрута чтения нет, блок честно говорит об этом. Пустой список
        без объяснения читался бы как «таких случаев не было» — а это ровно та
        тишина, которую чиним.
        """
        return {"available": False, "items": [], "reason": LEDGER_SHORT_GAP}

    async def _printing(self) -> dict[str, Any]:
        stats = await self._store.print_stats() or {}
        total = int(stats.get("total") or 0)
        reprints = int(stats.get("reprints") or 0)
        return {
            "total_24h": total,
            "reprints_24h": reprints,
            "reprint_share": round(reprints / total, 3) if total else None,
            "failed_24h": int(stats.get("failed") or 0),
            "worst_click_to_agent_ms": _float(stats.get("worst_click_to_agent_ms")),
            "worst_agent_write_ms": _float(stats.get("worst_agent_write_ms")),
            "budget_ms": 50,
            "last_write": None,
        }

    async def _cancellations(self) -> dict[str, Any]:
        without_reason = await self._store.cancellations_without_reason()
        return {
            "without_reason": without_reason,
            "note": ("причина отмены обязательна на уровне схемы (инвариант 11). "
                     "В боевом контуре без причины были все 2645 отмен"),
        }


def _float(value: Any) -> float | None:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None
