"""Клиент контракта wms — единственный выход рабочего места наружу.

Контракт заморожен потоком 0 (`services/wms/contracts/openapi.yaml`) и одинаков
у mock и у настоящего сервиса. Поэтому переключение на поток A — смена
`WMS_BASE_URL`, а не правка кода: весь разговор с wms собран здесь.

Три вещи, которые этот модуль делает и о которых легко забыть:

1. **Читает по контракту, а не по тому, что удобно отдал сервер.** Там, где
   заглушка отвечает в другой форме, ответ всё же понимается — но каждое такое
   чтение считается счётчиком `contract_fallbacks_total`. Молча подстроиться
   под заглушку значит выучить неправильный формат и обнаружить это в день
   переключения на поток A.
2. **Проверяет стикер до печати.** `order_id` обязан совпасть с заданием,
   контрольная сумма — сойтись, и сверяется она `hmac.compare_digest`, а не
   оператором `==` (приложение C мастера).
3. **Проверяет владельца.** Клиент принимает результат скана, только если
   `owner_external_id` совпал с ожидаемым (инвариант 6): перепутанный владелец
   это отгрузка чужой вещи, а не строчка в отчёте.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from . import metrics
from .domain import Placement, ScanResult, Task, task_from_contract

BASE_PATH = "/api/mmx/wms/v1"

# Коды отказа резерва — закрытый список приложения C. Рабочее место их не
# придумывает и не расширяет: на эти значения завязаны и wms, и каталог.
KNOWN_ERROR_CODES = frozenset({
    "SELLER_MAPPING_MISSING", "PRODUCT_MAPPING_MISSING", "AMBIGUOUS_PRODUCT_MAPPING",
    "WAREHOUSE_UNKNOWN", "INSUFFICIENT_STOCK", "SERIALIZATION_RETRY",
})


class WmsUnavailable(RuntimeError):
    """wms не ответил: сеть, таймаут, 5xx, сломанный конверт JSON-RPC.

    Отделено от отказа операции намеренно. «Сервис молчит» и «сервис сказал
    нет» требуют от экрана разного: первое — повторить, второе — показать
    человеку причину и не повторять.
    """


# Код JSON-RPC «неверные параметры». Отказ по существу, адресованный
# человеку: у wms он означает и «товар не найден», и «команда из неправильного
# состояния», и «задания нет».
JSONRPC_INVALID_PARAMS = -32602

# По каким словам в отказе понятно, что задания просто нет.
_NOT_FOUND_HINTS = ("не найден", "не существует", "нет задания")


def _looks_like_not_found(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in _NOT_FOUND_HINTS)


class WmsRejected(RuntimeError):
    """Операция отклонена самим wms — с кодом в result (приложение C)."""

    def __init__(self, route: str, code: str | None, result: dict[str, Any],
                 message: str | None = None) -> None:
        # Текст отказа сохраняется и показывается человеку как есть: у `wms`
        # он написан словами и объясняет, что именно не так. Раньше в строке
        # оставался только код, а при `-32602` кода нет вовсе — экран получал
        # «отказ без кода» и показывал это сборщику.
        self.message = message or str(result.get("message") or "") or None
        super().__init__(f"{route}: {self.message or code or 'отказ без кода'}")
        self.route = route
        self.code = code
        self.result = result


class LabelUnusable(RuntimeError):
    """Стикер получен, но использовать его нельзя.

    Напечатанный чужой стикер отправит вещь другому покупателю — это хуже, чем
    ненапечатанный. Поэтому расхождение задания, суммы или формата не
    «предупреждение», а отказ.
    """


@dataclass(slots=True)
class Label:
    """Готовые байты для WritePrinter."""

    task_id: str
    body: bytes
    label_format: str
    content_type: str
    checksum: str
    version: int = 1
    station_id: str | None = None
    printer_transport: str = "agent"
    reprint: bool = False
    owner_external_id: str | None = None

    @property
    def is_zpl(self) -> bool:
        return self.label_format in {"zplv", "zplh", "zpl"}

    @property
    def is_raster(self) -> bool:
        return self.label_format in {"png", "svg"}


@dataclass(slots=True)
class PullBatch:
    """Ответ /tasks/pull, разобранный по схеме TasksPullResult."""

    tasks: list[Task] = field(default_factory=list)
    served_at: str | None = None
    available_total: int | None = None
    leased_until: dict[str, str | None] = field(default_factory=dict)


def decode_label_payload(payload: Any, checksum: Any) -> tuple[bytes, str]:
    """Тело стикера из поля `payload` — с проверкой контрольной суммы.

    Контракт объявляет payload закодированным в base64; заглушка потока 0
    отдаёт ZPL текстом как есть. Гадать по виду строки нельзя — валидный ZPL
    вполне может оказаться и валидным base64. Поэтому решает контрольная
    сумма: считаем обе трактовки и берём ту, что сошлась. Не сошлась ни одна —
    печатать нечего.
    """
    if not isinstance(payload, str) or not payload:
        raise LabelUnusable("в ответе нет тела стикера")
    digest = str(checksum or "").strip().lower()

    literal = payload.encode("utf-8")
    candidates: list[tuple[bytes, str]] = [(literal, "literal")]
    try:
        decoded = base64.b64decode(payload, validate=True)
        if decoded:
            # base64 идёт первым: это то, что объявляет контракт, и на
            # настоящем сервисе сойтись должна именно эта трактовка.
            candidates.insert(0, (decoded, "base64"))
    except (binascii.Error, ValueError):
        pass

    if not digest:
        # Контракт требует checksum обязательным полем. Без него проверить
        # нечем — но и отказывать нельзя: заглушка обязана оставаться пригодной.
        # Считается как расхождение контракта, а не как норма.
        metrics.CONTRACT_FALLBACKS.labels(route="label", field="checksum_missing").inc()
        return candidates[0]

    for body, how in candidates:
        if hmac.compare_digest(hashlib.sha256(body).hexdigest(), digest):
            if how == "literal":
                metrics.CONTRACT_FALLBACKS.labels(route="label", field="payload_not_base64").inc()
            return body, how
    raise LabelUnusable(
        "контрольная сумма стикера не сошлась ни как base64, ни как текст — "
        "печатать такой стикер нельзя")


def _content_type_to_format(content_type: Any, declared: Any = None) -> str:
    """Формат стикера.

    Формат берётся у сервиса, а не из локальной настройки: вопрос 2 раздела 13
    мастера ещё открыт, принтер может не понять ZPL, и рабочее место обязано
    напечатать то, что ему прислали, а не то, что оно ожидало.
    """
    if declared:
        value = str(declared).strip().lower()
        if value in {"zplv", "zplh", "zpl", "png", "svg"}:
            return value
    value = str(content_type or "").strip().lower()
    if "zpl" in value:
        return "zplv"
    if "png" in value:
        return "png"
    if "svg" in value:
        return "svg"
    return "zplv"


class WmsClient:
    """Асинхронный клиент JSON-RPC поверх POST.

    Соединение переиспользуется: на пути печати три рукопожатия TLS стоят
    больше, чем весь бюджет в 50 мс.
    """

    def __init__(self, base_url: str, *, token: str | None = None,
                 timeout: float = 5.0, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = self._normalise(base_url)
        self._token = token
        self._timeout = timeout
        headers = {"content-type": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, headers=headers,
            limits=httpx.Limits(max_keepalive_connections=16, max_connections=32))

    @staticmethod
    def _normalise(base_url: str) -> str:
        value = base_url.rstrip("/")
        return value if value.endswith(BASE_PATH) else value + BASE_PATH

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ транспорт

    async def call(self, route: str, params: dict[str, Any] | None = None, *,
                   attempts: int = 1, metric_route: str | None = None) -> dict[str, Any]:
        """Один вызов контракта. Возвращает `result`, кидает при отказе транспорта.

        `attempts > 1` допустим только там, где повтор безопасен: чтение или
        команда с ключом идемпотентности (инвариант 5). Повторить выдачу
        заданий с `claim: true` нельзя — второй вызов займёт вторую пачку.
        """
        label = metric_route or route
        body = {"jsonrpc": "2.0", "method": "call", "params": params or {}, "id": 1}
        last: Exception | None = None
        for attempt in range(max(1, attempts)):
            started = time.perf_counter()
            try:
                response = await self._client.post(route, json=body)
            except httpx.HTTPError as error:
                last = WmsUnavailable(f"{label}: {type(error).__name__}")
                metrics.WMS_CALLS.labels(route=label, outcome="transport_error").inc()
            else:
                metrics.WMS_CALL_DURATION.labels(route=label).observe(
                    time.perf_counter() - started)
                if response.status_code >= 500:
                    last = WmsUnavailable(f"{label}: HTTP {response.status_code}")
                    metrics.WMS_CALLS.labels(route=label, outcome="http_5xx").inc()
                elif response.status_code >= 400:
                    metrics.WMS_CALLS.labels(route=label, outcome="http_4xx").inc()
                    raise WmsUnavailable(f"{label}: HTTP {response.status_code}")
                else:
                    return self._unwrap(label, response)
            if attempt + 1 < attempts:
                # Пауза короткая: экран сборщика ждать не должен, а wms на
                # стенде и в проде стоит рядом.
                await _sleep(0.05 * (attempt + 1))
        metrics.WMS_CALLS.labels(route=label, outcome="failed").inc()
        raise last or WmsUnavailable(f"{label}: вызов не удался")

    def _unwrap(self, label: str, response: httpx.Response) -> dict[str, Any]:
        try:
            envelope = response.json()
        except ValueError as error:
            metrics.WMS_CALLS.labels(route=label, outcome="bad_json").inc()
            raise WmsUnavailable(f"{label}: ответ не JSON") from error
        if not isinstance(envelope, dict):
            metrics.WMS_CALLS.labels(route=label, outcome="bad_json").inc()
            raise WmsUnavailable(f"{label}: конверт JSON-RPC не объект")
        if envelope.get("error"):
            error = envelope["error"] if isinstance(envelope["error"], dict) else {}
            message = str(error.get("message") or envelope["error"])
            code = error.get("code")
            # `-32602` — «неверные параметры»: это отказ ПО СУЩЕСТВУ, а не
            # сбой сервиса. Раньше он превращался в `WmsUnavailable`, экран
            # показывал «wms недоступен», а сборщик ждал починки сервиса,
            # который работал: отказ был ему адресован, и в нём словами
            # написано, что не так.
            if code == JSONRPC_INVALID_PARAMS:
                metrics.WMS_CALLS.labels(route=label, outcome="rejected").inc()
                raise WmsRejected(label, None, {"message": message},
                                  message=message)
            metrics.WMS_CALLS.labels(route=label, outcome="jsonrpc_error").inc()
            raise WmsUnavailable(f"{label}: {message}")
        result = envelope.get("result")
        if result is None:
            metrics.WMS_CALLS.labels(route=label, outcome="empty_result").inc()
            raise WmsUnavailable(f"{label}: в ответе нет result")
        if not isinstance(result, dict):
            result = {"value": result}
        metrics.WMS_CALLS.labels(route=label, outcome="ok").inc()
        return result

    @staticmethod
    def _reject_if_error_code(route: str, result: dict[str, Any]) -> dict[str, Any]:
        """Прикладной отказ лежит в result, а не в error (так задан контракт)."""
        code = result.get("error_code")
        if code:
            raise WmsRejected(route, str(code), result)
        return result

    @staticmethod
    def _fallback(route: str, field: str, result: dict[str, Any],
                  contract_key: str, *alternatives: str) -> Any:
        """Поле по контракту, при отсутствии — по запасному имени, но со счётчиком."""
        if contract_key in result:
            return result[contract_key]
        for name in alternatives:
            if name in result:
                metrics.CONTRACT_FALLBACKS.labels(route=route, field=field).inc()
                return result[name]
        return None

    # ------------------------------------------------------------ задания

    async def tasks_pull(self, *, assignee: str | None = None, limit: int, claim: bool = False,
                         lease_seconds: int | None = None,
                         owner_external_ids: Iterable[str] | None = None,
                         states: Iterable[str] | None = None,
                         include_extended: bool = True,
                         previous: dict[str, Task] | None = None) -> PullBatch:
        """Забрать задания напрямую, без шины.

        Тот самый маршрут, ради которого переписан путь заказа (раздел 6.1):
        задание доступно здесь сразу после коммита в базу wms, даже если
        RabbitMQ выключен целиком.

        `claim=False` — чистое чтение для экрана: он обновляется чаще, чем
        человек берёт работу, и занимать при каждом обновлении нельзя.
        """
        params: dict[str, Any] = {
            "limit": int(limit), "claim": bool(claim),
            "include_extended": bool(include_extended),
        }
        # Исполнитель едет только при занятии. Чтение ничего не занимает — и
        # придумывать экрану имя пользователя не надо: `assignee` это
        # идентификатор человека из `identity` (раздел 12), а экран не человек.
        if assignee:
            params["assignee"] = assignee
        if claim and lease_seconds:
            params["lease_seconds"] = int(lease_seconds)
        if owner_external_ids:
            params["owner_external_ids"] = list(owner_external_ids)
        if states:
            params["states"] = list(states)
        # Повторять занятие заданий нельзя: второй вызов займёт вторую пачку.
        result = await self.call("/tasks/pull", params,
                                 attempts=1 if claim else 2, metric_route="/tasks/pull")
        return self._parse_pull(result, previous or {})

    def _parse_pull(self, result: dict[str, Any], previous: dict[str, Task]) -> PullBatch:
        raw = result.get("tasks")
        batch = PullBatch(
            served_at=self._fallback("/tasks/pull", "served_at", result,
                                     "served_at", "generated_at"),
            available_total=_int_or_none(result.get("available_total")),
        )
        if not isinstance(raw, list):
            return batch
        for item in raw:
            if not isinstance(item, dict):
                continue
            projection = item.get("task")
            leased_until = item.get("leased_until")
            placements = item.get("placements")
            if not isinstance(projection, dict):
                # Заглушка отдаёт плоское задание вместо {task, leased_until}.
                # Понимаем, но считаем: на настоящем сервисе так быть не должно.
                metrics.CONTRACT_FALLBACKS.labels(route="/tasks/pull", field="task").inc()
                projection = item
                leased_until = item.get("claim_expires_at")
                placements = item.get("placements")
            task = task_from_contract(
                projection,
                placements=placements if isinstance(placements, list) else None,
                previous=previous.get(str(projection.get("task_id") or "")))
            if not task.task_id:
                continue
            if not task.placements:
                task.placements = _placement_from_flat(projection)
            if task.label_fallback:
                metrics.CONTRACT_FALLBACKS.labels(route="/tasks/pull", field="label").inc()
            batch.tasks.append(task)
            batch.leased_until[task.task_id] = leased_until
        return batch

    async def task(self, task_id: str, *, previous: Task | None = None) -> Task | None:
        """Прочитать задание целиком.

        Используется для восстановления после потери ответа на скан: клиент
        перечитывает задание и принимает его, если оно уже `picked` с тем же
        штрихкодом (правило приложения C).
        """
        try:
            result = await self.call(f"/tasks/{task_id}", {},
                                     attempts=2, metric_route="/tasks/{id}")
        except WmsRejected as refused:
            # «Задания нет» — законный ответ, а не сбой: задание могли
            # отменить, пока мы про него спрашивали. Отличается от
            # `WmsUnavailable` тем, что задание после этого убирают с экрана,
            # а не оставляют ждать возвращения сервиса.
            if _looks_like_not_found(str(refused)):
                return None
            raise
        if not result or not result.get("task_id"):
            return None
        return task_from_contract(result, previous=previous)

    async def scan(self, task_id: str, barcode: str, *,
                   expected_owner: str | None = None) -> tuple[bool, ScanResult, dict[str, Any]]:
        """Скан у стойки или контрольный скан при упаковке.

        Возвращает (принят, разбор, ответ). Принят — только если сошлись оба
        условия приложения C: `status == "picked"` и владелец тот же. Владелец
        сверяется здесь, а не на экране: изоляция владельца — инвариант, а не
        предупреждение (инвариант 6).
        """
        result = await self.call(f"/tasks/{task_id}/scan", {"barcode": barcode},
                                 metric_route="/tasks/{id}/scan")
        status = str(result.get("status") or "")
        owner = result.get("owner_external_id")
        parsed = _scan_result(result.get("scan_result"), status=status, barcode_ok=True)

        if expected_owner and owner and str(owner) != str(expected_owner):
            # Ответ по чужому владельцу не принимается, каким бы ни был статус.
            return False, ScanResult.WRONG_OWNER, result
        accepted = status == "picked" and parsed is ScanResult.OK
        return accepted, parsed, result

    async def pack(self, task_id: str, *, idempotency_key: str,
                   control_scan_barcode: str | None = None,
                   box_barcode: str | None = None,
                   station_id: str | None = None,
                   actor_id: str | None = None) -> dict[str, Any]:
        """Упаковка. Контрольный штрихкод уезжает на сервер вместе с командой.

        Проверять его только на экране нельзя: экран можно обойти, а wms —
        последняя инстанция, которая решает, что задание упаковано.
        """
        params: dict[str, Any] = {"idempotency_key": idempotency_key}
        if control_scan_barcode:
            params["control_scan_barcode"] = control_scan_barcode
        if box_barcode:
            params["box_barcode"] = box_barcode
        if station_id:
            params["station_id"] = station_id
        if actor_id:
            params["actor_id"] = actor_id
        result = await self.call(f"/tasks/{task_id}/pack", params,
                                 attempts=2, metric_route="/tasks/{id}/pack")
        return self._reject_if_error_code("/tasks/{id}/pack", result)

    async def label(self, task_id: str, *, expected_order_id: Any = None) -> Label:
        """Этикетка задания из локального хранилища wms (инвариант 9)."""
        result = await self.call(f"/tasks/{task_id}/label", {},
                                 attempts=2, metric_route="/tasks/{id}/label")
        self._reject_if_error_code("/tasks/{id}/label", result)
        return self._build_label(task_id, result, expected_order_id=expected_order_id)

    async def print_label(self, task_id: str, *, station_id: str, idempotency_key: str,
                          copies: int = 1, reprint: bool = False, reason: str | None = None,
                          actor_id: str | None = None,
                          expected_order_id: Any = None,
                          expected_owner: str | None = None) -> Label:
        """Отдать локальный стикер станции и зафиксировать факт печати.

        Ни одного вызова в Wildberries на этом пути (инвариант 9) — иначе
        теряется весь смысл предзагрузки и возвращаются те самые 2–10 секунд.
        """
        params: dict[str, Any] = {
            "station_id": station_id, "idempotency_key": idempotency_key,
            "copies": int(copies), "reprint": bool(reprint),
        }
        if reprint:
            # Обязательна при перепечатке: иначе счётчик перепечаток нечем
            # разбирать, а доля перепечаток — метрика качества принтера.
            params["reason"] = reason or "повтор без указанной причины"
        elif reason:
            params["reason"] = reason
        if actor_id:
            params["actor_id"] = actor_id
        result = await self.call(f"/labels/{task_id}/print", params,
                                 attempts=2, metric_route="/labels/{id}/print")
        self._reject_if_error_code("/labels/{id}/print", result)
        label = self._build_label(task_id, result, expected_order_id=expected_order_id,
                                  expected_owner=expected_owner,
                                  route="/labels/{id}/print")
        label.station_id = str(result.get("station_id") or station_id)
        label.printer_transport = str(result.get("printer_transport") or "agent")
        label.reprint = bool(result.get("reprint", reprint))
        return label

    def _build_label(self, task_id: str, result: dict[str, Any], *,
                     expected_order_id: Any = None, expected_owner: str | None = None,
                     route: str = "/tasks/{id}/label") -> Label:
        body, _how = decode_label_payload(result.get("payload"), result.get("checksum"))

        # Три сверки, и все три об одном: напечатанный чужой стикер отправит
        # вещь другому покупателю — это хуже, чем ненапечатанный.
        returned_task = _text_or_none(result.get("task_id"))
        if returned_task is not None and returned_task != str(task_id):
            raise LabelUnusable(
                f"стикер выписан на задание {returned_task}, а печатается "
                f"{task_id}")

        owner = _text_or_none(result.get("owner_external_id"))
        if expected_owner and owner and owner != expected_owner:
            # Изоляция владельца — инвариант, а не предупреждение (инвариант 6).
            raise LabelUnusable(
                f"стикер выписан клиенту {owner}, а задание принадлежит "
                f"{expected_owner}")

        order_id = result.get("order_id")
        if expected_order_id is not None and order_id is not None:
            if str(order_id) != str(expected_order_id):
                raise LabelUnusable(
                    f"стикер выписан на заказ {order_id}, а печатается задание "
                    f"с заказом {expected_order_id}")
        if expected_order_id is not None and order_id is None:
            # Поле есть в контракте с 1.3.0. Его отсутствие — не повод
            # печатать вслепую, но и не повод остановить смену: считаем и
            # показываем, чтобы расхождение контракта было видно.
            metrics.CONTRACT_FALLBACKS.labels(route=route, field="order_id").inc()
        label_format = _content_type_to_format(result.get("content_type"), result.get("format"))
        return Label(
            task_id=str(result.get("task_id") or task_id),
            body=body,
            label_format=label_format,
            content_type=str(result.get("content_type") or _default_content_type(label_format)),
            checksum=str(result.get("checksum") or hashlib.sha256(body).hexdigest()),
            version=_int_or_none(result.get("version")) or 1,
            owner_external_id=_text_or_none(result.get("owner_external_id")),
        )

    async def return_to_shelf(self, task_id: str, *, idempotency_key: str,
                              cell_address: str | None = None,
                              box_barcode: str | None = None,
                              reason: str | None = None,
                              actor_id: str | None = None) -> dict[str, Any]:
        """Вернуть товар на полку — с адресом.

        Адрес обязателен по смыслу: без него товар «возвращается» в никуда и
        остаток снова начинает врать.
        """
        params: dict[str, Any] = {"idempotency_key": idempotency_key}
        if cell_address:
            params["cell_address"] = cell_address
        if box_barcode:
            params["box_barcode"] = box_barcode
        if reason:
            params["reason"] = reason
        if actor_id:
            params["actor_id"] = actor_id
        result = await self.call(f"/tasks/{task_id}/return-to-shelf", params,
                                 attempts=2, metric_route="/tasks/{id}/return-to-shelf")
        return self._reject_if_error_code("/tasks/{id}/return-to-shelf", result)

    async def cancel(self, task_id: str, *, cancellation_event_id: str,
                     handed_over: bool = False, reason: str | None = None) -> dict[str, Any]:
        """Отмена задания.

        `reason` — часть контракта с версии 1.3.0 и передаётся серверу: у
        отмены с рабочего места причина есть, и она разбираема («брак:
        порвана упаковка»), а выведенная сервером «отменено по событию
        ws-cancel-…» — нет. До 1.3.0 поле контрактом не принималось и
        отправлять его было нельзя.

        В боевом контуре у всех 2645 отмен `cancel_reason` был пуст, и
        разобрать их задним числом стало нечем.
        """
        params: dict[str, Any] = {
            "cancellation_event_id": cancellation_event_id,
            "handed_over": bool(handed_over),
        }
        if reason:
            params["reason"] = reason
        result = await self.call(f"/tasks/{task_id}/cancel", params,
                                 attempts=2, metric_route="/tasks/{id}/cancel")
        return self._reject_if_error_code("/tasks/{id}/cancel", result)

    # ------------------------------------------------------------ склад

    async def storage_lookup(self, *, seller_external_id: str, barcode: str,
                             states: Iterable[str] | None = None) -> list[Placement]:
        params: dict[str, Any] = {"seller_external_id": seller_external_id, "barcode": barcode}
        if states:
            params["states"] = list(states)
        result = await self.call("/storage/lookup", params, attempts=2)
        rows = self._fallback("/storage/lookup", "placements", result, "placements", "rows") or []
        placements = [Placement.from_contract(row) for row in rows if isinstance(row, dict)]
        placements.sort(key=lambda p: (p.route_order is None, p.route_order or 0))
        return placements

    async def health(self) -> dict[str, Any]:
        return await self.call("/health", {}, attempts=1)


def _default_content_type(label_format: str) -> str:
    return {"png": "image/png", "svg": "image/svg+xml"}.get(label_format, "application/x-zpl")


def _scan_result(value: Any, *, status: str, barcode_ok: bool) -> ScanResult:
    """Разбор скана. Отсутствие поля — не «всё хорошо».

    Заглушка и старые клиенты могут его не прислать; тогда разбор выводится из
    статуса, но «принято» ставится только при явном `picked`.
    """
    if value:
        try:
            return ScanResult(str(value))
        except ValueError:
            metrics.CONTRACT_FALLBACKS.labels(route="/tasks/{id}/scan",
                                              field="scan_result").inc()
    if status == "picked":
        return ScanResult.OK
    if status == "not_found":
        return ScanResult.NOT_FOUND
    metrics.CONTRACT_FALLBACKS.labels(route="/tasks/{id}/scan", field="scan_result").inc()
    return ScanResult.WRONG_BARCODE if barcode_ok else ScanResult.NOT_FOUND


def _placement_from_flat(projection: dict[str, Any]) -> list[Placement]:
    """Адрес из плоских полей задания.

    Заглушка отдаёт `cell`/`cell_id` прямо в задании, без блока `placements`.
    Пустой лист подбора хуже приблизительного: сборщику нужно, куда идти.
    """
    address = projection.get("cell_address") or projection.get("cell")
    box = projection.get("box_barcode") or projection.get("box")
    if not address and not box:
        return []
    metrics.CONTRACT_FALLBACKS.labels(route="/tasks/pull", field="placements").inc()
    return [Placement(
        barcode=str(projection.get("barcode") or ""),
        cell_address=_text_or_none(address),
        box_barcode=_text_or_none(box),
        state="good",
        quantity=_int_or_none(projection.get("quantity")) or 0,
        route_order=_int_or_none(projection.get("route_order")),
    )]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
