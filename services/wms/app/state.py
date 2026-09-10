"""Состояние mock-сервиса в памяти.

Не база и не претендует ею быть. Держит ровно столько, чтобы потоки B и C
видели связное поведение: задание не выдаётся двоим, стикер лежит до печати,
номер события растёт в пределах задания.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from . import fixtures
from .domain import TaskState, now, uid

# Лизинг выдачи: задание освобождается, если сборщик пропал (раздел 7).
CLAIM_TTL = timedelta(minutes=15)


def _zpl(barcode: str, title: str) -> str:
    """Минимальный настоящий ZPL для этикетки 58×40 мм.

    Формат zplv, а не png: 1–3 КБ текста против 20–100 КБ картинки, и принтер
    печатает его нативно, без растеризации драйвером (раздел 6.6).
    """
    return (
        "^XA\n^PW464\n^LL320\n"
        f"^FO20,20^A0N,28,28^FD{title[:28]}^FS\n"
        f"^FO20,70^BY2^BCN,120,Y,N,N^FD{barcode}^FS\n"
        "^XZ\n"
    )


class MockState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    # ------------------------------------------------------------ жизненный цикл

    def reset(self) -> None:
        with self._lock:
            self._tasks: dict[str, dict[str, Any]] = {}
            self._labels: dict[str, dict[str, Any]] = {}
            self._sequences: dict[str, int] = {}
            self._receipts: dict[str, dict[str, Any]] = {}
            self._boxes: list[dict[str, Any]] = [dict(box) for box in fixtures.BOXES]
            self._returns: dict[str, dict[str, Any]] = {}
            self._shipments: list[dict[str, Any]] = []
            self.documents: list[dict[str, Any]] = []

            # Реестры. Фикстуры — начальное наполнение, а не единственная
            # правда: клиента, товар и кабинет заводят на ходу, и заведённое
            # обязано быть видно резерву. Иначе happy-path недостижим ни для
            # прогона, ни для потоков B и C.
            self._owners: dict[str, dict[str, Any]] = {
                seller["seller_external_id"]: dict(seller) for seller in fixtures.SELLERS
            }
            # Остаток в карточку товара не кладём: он живёт в _stock, и две
            # копии одного числа однажды разойдутся молча.
            self._products: dict[tuple[str, str], dict[str, Any]] = {
                (product["seller_external_id"], product["barcode"]):
                    {key: value for key, value in product.items() if key != "available"}
                for product in fixtures.PRODUCTS
            }
            self._wb_accounts: dict[str, dict[str, Any]] = {
                account["external_id"]: dict(account) for account in fixtures.WB_ACCOUNTS
            }
            self._cells: dict[str, dict[str, Any]] = {
                cell["address"]: dict(cell) for cell in fixtures.CELLS
            }
            # Где лежит товар, у которого ещё нет своей коробки: заведённый
            # документом адрес нужен событию о недостаче (раздел 6.5).
            self._placement: dict[tuple[str, str], str] = {}

            self._stock: dict[tuple[str, str], int] = {
                (product["seller_external_id"], product["barcode"]): int(product["available"])
                for product in fixtures.PRODUCTS
            }
            self._reserved: dict[tuple[str, str], int] = {}

    def ready(self) -> bool:
        return True

    # ------------------------------------------------------------ реестры

    def register_owner(self, seller_external_id: str, *, name: Any = None, inn: Any = None,
                       allow_ledger_short: Any = None) -> dict[str, Any]:
        """Заводит владельца товара. Идемпотентно по внешнему идентификатору.

        Повторный вызов возвращает того же владельца, а не заводит второго
        (инвариант 5). Заново присвоенный owner_id обесценил бы все уже
        выпущенные события.
        """
        with self._lock:
            existing = self._owners.get(seller_external_id)
            if existing is not None:
                if name is not None:
                    existing["name"] = name
                if inn is not None:
                    existing["inn"] = inn
                if allow_ledger_short is not None:
                    existing["allow_ledger_short"] = bool(allow_ledger_short)
                return dict(existing)

            owner = {
                "owner_id": uid(),
                "seller_external_id": seller_external_id,
                "name": name or seller_external_id,
                "inn": inn,
                # Клапан «собрать без остатка» по умолчанию включён: в первый
                # день учёт где-то неверен, и вставшая смена гонит операторов
                # в обход — ровно та беда, которую чиним (раздел 6.5).
                "allow_ledger_short": True if allow_ledger_short is None
                                      else bool(allow_ledger_short),
            }
            self._owners[seller_external_id] = owner
            return dict(owner)

    def owners(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(owner) for owner in self._owners.values()]

    def owner_is_known(self, seller_external_id: str) -> bool:
        with self._lock:
            return seller_external_id in self._owners

    def owner_id_of(self, seller_external_id: str) -> str | None:
        with self._lock:
            owner = self._owners.get(seller_external_id)
            return owner["owner_id"] if owner else None

    def allows_ledger_short(self, seller_external_id: str) -> bool:
        with self._lock:
            owner = self._owners.get(seller_external_id)
            return bool(owner["allow_ledger_short"]) if owner else False

    def register_product(self, seller_external_id: str, barcode: str, *,
                         seller_sku: Any = None, name: Any = None) -> tuple[dict[str, Any], bool]:
        """Заводит SKU у владельца. Идемпотентно по паре владелец + штрихкод.

        Возвращает товар и признак того, что он заведён именно сейчас.
        """
        with self._lock:
            key = (seller_external_id, barcode)
            existing = self._products.get(key)
            if existing is not None:
                return dict(existing), False

            product = {
                "sku_id": uid(),
                "seller_external_id": seller_external_id,
                "barcode": barcode,
                "seller_sku": seller_sku,
                "name": name or barcode,
            }
            self._products[key] = product
            self._stock.setdefault(key, 0)
            return dict(product), True

    def product(self, seller_external_id: str, barcode: str) -> dict[str, Any] | None:
        with self._lock:
            product = self._products.get((seller_external_id, barcode))
            return dict(product) if product else None

    def products(self, seller_external_id: Any = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for key, product in self._products.items():
                seller, _ = key
                if seller_external_id and seller != seller_external_id:
                    continue
                # Наличие карточки не означает наличия вещи на полке, поэтому
                # остаток подставляется из журнала, а не хранится в карточке.
                rows.append({**product, "available": max(0, self._stock.get(key, 0))})
            return rows

    def good_qty(self, seller_external_id: str, barcode: str) -> int:
        """Годный остаток. По нему решается, сработает ли клапан 6.5."""
        with self._lock:
            return self._stock.get((seller_external_id, barcode), 0)

    def register_wb_account(self, external_id: str, **fields: Any) -> tuple[dict[str, Any], bool]:
        """Заводит кабинет WB. Значение токена сюда не попадает никогда.

        Принимается только secret_ref — ссылка на секрет (инвариант 15).
        """
        with self._lock:
            existing = self._wb_accounts.get(external_id)
            created = existing is None
            account = existing or {"id": uid(), "external_id": external_id}
            for key in ("seller_external_id", "display_name", "secret_ref",
                        "mode", "status", "token_type", "wb_warehouse_id"):
                if fields.get(key) is not None:
                    account[key] = fields[key]
            account.setdefault("mode", "shadow")
            account.setdefault("status", "ACTIVE")
            self._wb_accounts[external_id] = account
            return dict(account), created

    def wb_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(account) for account in self._wb_accounts.values()]

    def register_cell(self, address: str, *, route_order: Any = None,
                      zone: str = "STOR") -> dict[str, Any]:
        with self._lock:
            existing = self._cells.get(address)
            if existing is not None:
                return dict(existing)
            cell = {"cell_id": uid(), "address": address, "zone": zone,
                    "route_order": route_order}
            self._cells[address] = cell
            return dict(cell)

    def cells(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(cell) for cell in self._cells.values()]

    def apply_document(self, *, seller_external_id: str, reference: str, doc_type: str,
                       lines: list[dict[str, Any]], comment: Any = None) -> dict[str, Any]:
        """Складской документ: заводит адреса и двигает остаток.

        Начальный остаток при переключении клиента даёт владелец компании
        (решение владельца 11), поэтому он заезжает документом, а не появляется
        сам. Идемпотентно по reference (инвариант 5).
        """
        with self._lock:
            for document in self.documents:
                if document.get("reference") == reference:
                    return dict(document)

            applied = []
            for line in lines:
                barcode = str(line.get("barcode", ""))
                if not barcode:
                    continue
                quantity = int(line.get("quantity", 0))
                self.register_product(seller_external_id, barcode)
                key = (seller_external_id, barcode)
                self._stock[key] = self._stock.get(key, 0) + quantity

                address = line.get("cell_address")
                if address:
                    cell = self.register_cell(str(address))
                    self._placement[key] = cell["cell_id"]
                applied.append({"barcode": barcode, "quantity": quantity,
                                "cell_address": address})

            document = {
                "document_id": uid(), "reference": reference, "doc_type": doc_type,
                "seller_external_id": seller_external_id, "comment": comment,
                "state": "applied", "lines": applied, "created_at": now(),
            }
            self.documents.append(document)
            return dict(document)

    def next_sequence(self, task_id: str) -> int:
        """Номер события в пределах задания.

        В настоящем сервисе инкремент идёт под блокировкой строки задания в той
        же транзакции, что и движение (приложение E). Здесь — под тем же
        замком, чтобы потребители видели ту же монотонность.
        """
        with self._lock:
            self._sequences[task_id] = self._sequences.get(task_id, 0) + 1
            return self._sequences[task_id]

    # ------------------------------------------------------------ резерв и задания

    def reserve(self, *, seller_external_id: str, barcode: str, quantity: int,
                wb_order_id: Any = None, deadline: Any = None) -> dict[str, Any]:
        with self._lock:
            task_id = uid()
            key = (seller_external_id, barcode)
            self._reserved[key] = self._reserved.get(key, 0) + quantity
            self._stock[key] = self._stock.get(key, 0) - quantity

            product = self._products.get(key) or {}
            task = {
                "task_id": task_id,
                "reservation_id": uid(),
                "wb_order_id": wb_order_id,
                "owner_external_id": seller_external_id,
                # Настоящие UUID: контракт событий требует формат uuid, и
                # заглушка обязана отдавать то, подо что пишутся консьюмеры.
                "owner_id": self.owner_id_of(seller_external_id),
                "sku_id": product.get("sku_id"),
                "barcode": barcode,
                "quantity": quantity,
                "state": TaskState.RESERVED.value,
                "wb_status": "new",
                "deadline": deadline,
                "cell_id": self._cell_for(seller_external_id, barcode),
                "assignee": None,
                "claim_expires_at": None,
                "created_at": now(),
                "sequence": 0,
            }
            task["sequence"] = self.next_sequence(task_id)
            self._tasks[task_id] = task

            # Стикер тянется заранее, не при упаковке (инвариант 9). К моменту
            # нажатия «печать» он уже здесь.
            payload = _zpl(barcode, str(product.get("name") or seller_external_id))
            label_id = uid()
            task["label_id"] = label_id
            self._labels[task_id] = {
                "id": label_id,
                "order_id": wb_order_id,
                "version": 1,
                "payload": payload,
                "content_type": "zplv",
                "checksum": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                "sticker": {"barcode": barcode},
                "invalidated": False,
            }
            return task

    def pull(self, *, assignee: Any, limit: int) -> list[dict[str, Any]]:
        """Выдача заданий сборщику.

        Задание не должно уйти двоим (шаг 8 прогона). В настоящем сервисе это
        FOR UPDATE SKIP LOCKED; здесь — замок и отметка assignee.
        """
        with self._lock:
            deadline_at = datetime.now(timezone.utc) + CLAIM_TTL
            taken: list[dict[str, Any]] = []
            candidates = [
                task for task in self._tasks.values()
                if task["state"] == TaskState.RESERVED.value and task["assignee"] is None
            ]
            # Сортировка по сроку WB, а не по времени создания (раздел 7).
            candidates.sort(key=lambda task: (task["deadline"] is None, task["deadline"] or ""))
            for task in candidates[:max(0, limit)]:
                task["assignee"] = assignee
                task["claim_expires_at"] = deadline_at.isoformat()
                task["state"] = TaskState.PICKING.value
                taken.append(dict(task))
            return taken

    def available_for_pull(self) -> int:
        with self._lock:
            return sum(1 for task in self._tasks.values()
                       if task["state"] == TaskState.RESERVED.value and task["assignee"] is None)

    def task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def set_state(self, task_id: str, state: TaskState) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["state"] = state.value

    def release(self, task_id: str, *, reason: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            key = (task["owner_external_id"], task["barcode"])
            self._reserved[key] = max(0, self._reserved.get(key, 0) - int(task["quantity"]))
            self._stock[key] = self._stock.get(key, 0) + int(task["quantity"])
            task["state"] = TaskState.RESERVED.value
            task["assignee"] = None
            task["claim_expires_at"] = None
            task["release_reason"] = reason

    def cancel(self, task_id: str, *, reason: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            key = (task["owner_external_id"], task["barcode"])
            self._reserved[key] = max(0, self._reserved.get(key, 0) - int(task["quantity"]))
            self._stock[key] = self._stock.get(key, 0) + int(task["quantity"])
            task["state"] = TaskState.CANCELLED.value
            task["cancel_reason"] = reason
            # Стикер помечается недействительным, заказ освобождается из
            # поставки (раздел 6.6).
            if task_id in self._labels:
                self._labels[task_id]["invalidated"] = True

    def label(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            label = self._labels.get(task_id)
            if not label or label["invalidated"]:
                return None
            return {key: value for key, value in label.items() if key != "invalidated"}

    # ------------------------------------------------------------ остатки

    def stocks_for(self, seller_external_id: Any) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for (seller, barcode), good in self._stock.items():
                if seller_external_id and seller != seller_external_id:
                    continue
                reserved = self._reserved.get((seller, barcode), 0)
                # available = good - reserved - buffer, всегда занижать
                # (инвариант 7). Буфер у фикстур нулевой.
                rows.append({
                    "barcode": barcode,
                    "seller_external_id": seller,
                    "good": good,
                    "reserved": reserved,
                    "available": max(0, good - reserved),
                })
            return rows

    def lookup(self, barcode: Any) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"box_barcode": box["barcode"], "cell": box["cell"],
                 "quantity": box["quantity"], "comment": box["comment"]}
                for box in self._boxes
                if not barcode or box.get("barcode_product") == barcode
            ]

    def _cell_for(self, seller_external_id: str, barcode: str) -> str | None:
        """Идентификатор ячейки, а не её адрес: в событиях ездят UUID.

        Сначала коробка — фактическая единица адресации (раздел 2.9), затем
        адрес, заданный документом. Без ячейки расхождение неадресно, и
        инвентаризация не знает, куда идти.
        """
        for box in self._boxes:
            if (box.get("barcode_product") == barcode
                    and box.get("seller_external_id") == seller_external_id):
                return box.get("cell_id")
        return self._placement.get((seller_external_id, barcode))

    # ------------------------------------------------------------ приёмка

    def receive(self, *, seller_external_id: str, reference: str,
                lines: list[dict[str, Any]], seller_name: Any = None,
                seller_inn: Any = None) -> dict[str, Any]:
        with self._lock:
            # Владельца, которого склад видит впервые, заводим по имени и ИНН
            # (приложение C). Товар, пришедший впервые, — тоже: приёмка это
            # первый момент, когда вещь вообще появляется на складе.
            self.register_owner(seller_external_id, name=seller_name, inn=seller_inn)
            # Идемпотентность по reference (инвариант 5): повтор — тот же ответ,
            # а не вторая приёмка.
            if reference in self._receipts:
                return dict(self._receipts[reference])

            discrepancies = []
            for line in lines:
                expected = line.get("expected_qty")
                actual = line.get("actual_qty", expected)
                barcode = str(line.get("barcode", ""))
                if actual is None:
                    continue
                # Баланс двигается по факту, а не по ожиданию (шаг 3 прогона).
                self.register_product(seller_external_id, barcode)
                key = (seller_external_id, barcode)
                self._stock[key] = self._stock.get(key, 0) + int(actual)
                address = line.get("cell_address")
                if address:
                    self._placement[key] = self.register_cell(str(address))["cell_id"]
                if expected is not None and int(actual) != int(expected):
                    discrepancies.append({
                        "barcode": barcode,
                        "kind": "shortage" if int(actual) < int(expected) else "surplus",
                        "qty": abs(int(actual) - int(expected)),
                        "decision": "pending",
                    })

            receipt = {
                "receipt_id": uid(), "reference": reference,
                "seller_external_id": seller_external_id,
                "state": "accepted", "lines": lines, "discrepancies": discrepancies,
            }
            self._receipts[reference] = receipt
            self.documents.append({"doc_type": "receipt", "reference": reference,
                                   "created_at": now()})
            return dict(receipt)

    def open_receipts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._receipts.values() if r["state"] != "accepted"]

    def pending_putaway(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"reference": receipt["reference"], "lines": receipt["lines"]}
                for receipt in self._receipts.values()
            ]

    def inventory_sheet(self, seller_external_id: Any) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"box_barcode": box["barcode"], "cell": box["cell"],
                 "barcode": box.get("barcode_product"), "expected_qty": box["quantity"]}
                for box in self._boxes
                if not seller_external_id or box["seller_external_id"] == seller_external_id
            ]

    def inventory_count(self, *, seller_external_id: str, reference: str, scope: str,
                        lines: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            self.documents.append({"doc_type": "inventory", "reference": reference,
                                   "scope": scope, "created_at": now()})
            return {"count_id": uid(), "reference": reference, "scope": scope,
                    "state": "applied", "lines": lines}

    # ------------------------------------------------------------ коробки

    def create_box(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            box = {
                "barcode": str(params.get("barcode", uid())),
                "seller_external_id": str(params.get("seller_external_id", "")),
                "barcode_product": params.get("product_barcode"),
                "cell": params.get("cell"),
                "quantity": int(params.get("quantity", 0)),
                "counted": bool(params.get("counted", False)),
                "comment": str(params.get("comment", "")),
                "state": "stored",
            }
            self._boxes.append(box)
            return dict(box)

    def boxes(self, seller_external_id: Any) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(box) for box in self._boxes
                    if box["state"] == "stored"
                    and (not seller_external_id
                         or box["seller_external_id"] == seller_external_id)]

    def remove_box(self, barcode: str) -> dict[str, Any]:
        with self._lock:
            for box in self._boxes:
                if box["barcode"] == barcode:
                    box["state"] = "removed"
                    return {"barcode": barcode, "state": "removed"}
            return {"barcode": barcode, "state": "not_found"}

    # ------------------------------------------------------------ отгрузка

    def open_shipment(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            shipment = {
                "shipment_id": uid(),
                "seller_external_id": params.get("seller_external_id"),
                "wb_supply_id": params.get("wb_supply_id"),
                "state": "open",
                "orders": params.get("orders") or [],
                # HANDED_TO_WB требует подтверждения человеком (раздел 2.12).
                "handed_by": None,
                "handed_at": None,
            }
            self._shipments.append(shipment)
            return dict(shipment)

    def shipments(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(shipment) for shipment in self._shipments]

    # ------------------------------------------------------------ возвраты

    def open_return(self, task_id: str, return_event_id: str,
                    seller_external_id: str, reason: Any) -> str:
        with self._lock:
            # Идемпотентность по return_event_id (инвариант 5).
            for existing in self._returns.values():
                if existing["return_event_id"] == return_event_id:
                    return existing["return_id"]
            return_id = uid()
            task = self._tasks.get(task_id) or {}
            self._returns[return_id] = {
                "return_id": return_id, "return_event_id": return_event_id,
                "task_id": task_id, "seller_external_id": seller_external_id,
                "owner_id": task.get("owner_id") or fixtures.owner_id_of(seller_external_id),
                "sku_id": task.get("sku_id"),
                "qty": int(task.get("quantity", 1)),
                "state": "expected", "decision": None, "reason": reason,
            }
            return return_id

    def receive_return(self, return_id: str) -> None:
        with self._lock:
            if return_id in self._returns:
                self._returns[return_id]["state"] = "received"

    def get_return(self, return_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._returns.get(return_id)
            return dict(item) if item else None

    def decide_return(self, return_id: str, decision: str) -> None:
        with self._lock:
            if return_id in self._returns:
                self._returns[return_id]["state"] = "decided"
                self._returns[return_id]["decision"] = decision

    def returns(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._returns.values()]

    # ------------------------------------------------------------ метрики

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            by_state: dict[str, int] = {}
            for task in self._tasks.values():
                by_state[task["state"]] = by_state.get(task["state"], 0) + 1
            return {
                "tasks_total": len(self._tasks),
                "tasks_by_state": by_state,
                "labels_ready": sum(1 for label in self._labels.values()
                                    if not label["invalidated"]),
                "returns_total": len(self._returns),
            }
