"""Бизнес-логика биллинга.

Одно правило держит весь файл: **тарифицируемое событие обязано закончиться
либо начислением, либо строкой в billing_unbilled с причиной**. Молчаливого
третьего исхода нет. На проде он есть — и именно поэтому 6374 задания дают 145
начислений (раздел 3.4 мастера).
"""
from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from psycopg import Cursor

from . import repositories as repo
from .config import auto_onboard_unknown_cabinet, tenant_id
from .db import Database
from .domain import (Envelope, EnvelopeError, PartnerRole, PartnerShare, Service,
                     UnbilledReason)
from .metrics import (ACCRUALS, ACCRUED_AMOUNT, CABINETS_NEEDING_ONBOARDING,
                      SHIFT_OUTPUT_REJECTED, UNBILLED, WORKER_PROCESSED)
from .money import charge as compute_charge
from .money import pick_tier, split_markup

# События физического действия, из которых считается выработка смены (файл 04).
# Операция названа так, как её понимает начальник склада, а не как называется
# событие: в витрине стоит «упаковка», а не «wms.packing.completed.v1».
# Пространство имён для вычислимых event_id суточного хранения: одно и то же
# для кабинета и дня, поэтому повторный прогон не начисляет второй раз.
STORAGE_NAMESPACE = uuid.UUID("6d6d7800-0000-4000-8000-000073746f72")

SHIFT_OPERATIONS = {
    "wms.item.scanned.v1": "скан у стойки",
    "wms.packing.completed.v1": "упаковка",
    "wms.picking.completed.v1": "подбор",
    "wms.label.attached.v1": "стикеровка",
    "wms.return.received.v1": "приём возврата",
    # Приёмка — операция смены, и её выработку считают так же, как подбор.
    # Событие заведено в версии 1.3 мастера; без строки здесь приёмщик в
    # отчёте смены выглядел бездельником.
    "wms.receipt.completed.v1": "приёмка",
    "inventory.movement.recorded.v1": "движение товара",
    "order.packed.v1": "упаковка",
    "label.printed.v1": "печать этикетки",
}


class BillingService:
    def __init__(self, database: Database) -> None:
        self.db = database

    # ------------------------------------------------------------ приём события

    def ingest(self, body: Any) -> dict[str, Any]:
        """Принимает событие шины и доводит его до начисления.

        Одна транзакция: inbox, начисление, комиссия и outbox либо все вместе,
        либо ничего (инвариант 1 по духу — деньги и след о них неразделимы).
        Ни одного HTTP-вызова внутри (инвариант 2).
        """
        try:
            envelope = Envelope.parse(body)
        except EnvelopeError as error:
            # Конверт неразбираем — записать в inbox нечего (нет ключа), но
            # потерять событие нельзя. Причина видна в billing_unbilled.
            # Ключ обязан быть uuid: `billing_unbilled.event_id` — колонка
            # uuid, и строка «не-uuid» роняла саму запись об отказе. Событие с
            # мусорным ключом исчезало вместе с объяснением, почему исчезло.
            given = str((body or {}).get("event_id") or "").strip()
            event_id, detail = _uuid_or_new(given, str(error))
            with self.db.transaction() as cursor:
                repo.record_unbilled(
                    cursor, event_id, str((body or {}).get("type") or "?"),
                    UnbilledReason.BAD_ENVELOPE.value, detail,
                    body if isinstance(body, dict) else {"raw": str(body)}, None, None)
            UNBILLED.labels(reason=UnbilledReason.BAD_ENVELOPE.value).inc()
            return {"outcome": "unbilled", "reason": UnbilledReason.BAD_ENVELOPE.value,
                    "detail": str(error)}

        with self.db.transaction() as cursor:
            fresh = repo.claim_event(
                cursor, envelope.event_id, envelope.type, envelope.tenant_id,
                envelope.correlation_id, envelope.payload, envelope.occurred_at)
            if not fresh:
                # Повтор доставки. Тот же ответ, не второе начисление.
                return {"outcome": "duplicate", "event_id": envelope.event_id}

            self._record_shift(cursor, envelope)
            result = self._tariff(cursor, envelope)
            repo.finish_event(cursor, envelope.event_id, result["outcome"],
                              result.get("detail"))
            if result["outcome"] == "accrued":
                # Дошло до счёта — закрываем строку в отчёте «не дошло».
                # Иначе переигранное событие остаётся в нём навсегда, и отчёт
                # перестаёт значить что-либо.
                repo.resolve_unbilled(cursor, envelope.event_id)

        WORKER_PROCESSED.labels(worker="inbox").inc()
        return result

    # ------------------------------------------------------------ тарификация

    def _tariff(self, cursor: Cursor, envelope: Envelope) -> dict[str, Any]:
        mapping = repo.billable_event(cursor, envelope.type)
        if mapping is None or not mapping["active"]:
            # Не тарифицируется — и это решение, записанное в таблице, а не
            # умолчание кода. Строка с комментарием объясняет почему.
            return {"outcome": "skipped", "event_id": envelope.event_id,
                    "detail": None if mapping is None else mapping["comment"]}

        service = str(mapping["service"])
        seller = self._resolve_seller(envelope, mapping["seller_paths"])
        if not seller:
            return self._unbilled(cursor, envelope, UnbilledReason.SELLER_UNKNOWN,
                                  f"ни один из путей {list(mapping['seller_paths'])} "
                                  f"не дал кабинет")

        cabinet = repo.cabinet_by_seller(cursor, seller)
        if cabinet is None:
            if not auto_onboard_unknown_cabinet():
                return self._unbilled(cursor, envelope, UnbilledReason.CABINET_UNKNOWN,
                                      f"кабинет {seller} не заведён онбордингом")
            cabinet = self._auto_onboard(cursor, seller)

        quantity = self._resolve_quantity(envelope, mapping["quantity_path"])
        if quantity is None or quantity <= 0:
            return self._unbilled(cursor, envelope, UnbilledReason.NO_QUANTITY,
                                  f"путь {mapping['quantity_path']!r} не дал количества")

        version = repo.tariff_version(cursor, str(cabinet["id"]), service, envelope.occurred_on)
        if version is None:
            return self._unbilled(cursor, envelope, UnbilledReason.NO_TARIFF,
                                  f"на услугу {service} нет действующего тарифа "
                                  f"на {envelope.occurred_on}")
        if not version["approved"]:
            # Цена без подписи в счёт не идёт. Выручка не теряется: строка
            # ждёт утверждения версии и переигрывается.
            return self._unbilled(cursor, envelope, UnbilledReason.TARIFF_NOT_APPROVED,
                                  f"версия тарифа {version['id']} не утверждена")

        return self._charge(
            cursor, cabinet=cabinet, service=service, quantity=quantity,
            occurred_on=envelope.occurred_on, version=version, event_id=envelope.event_id,
            event_type=envelope.type, tenant=envelope.tenant_id or tenant_id(),
            correlation_id=envelope.correlation_id,
            allocation_key=self._allocation_key(envelope))

    def _charge(self, cursor: Cursor, *, cabinet: dict[str, Any], service: str,
                quantity: Decimal, occurred_on: date, version: dict[str, Any],
                event_id: str, event_type: str, tenant: str,
                correlation_id: str | None, allocation_key: str | None) -> dict[str, Any]:
        """Считает и записывает начисление. Одно место на все источники.

        Событие шины и суточное начисление за хранение приходят разными путями,
        но цена, наценка и раскладка комиссии у них обязаны считаться одним
        кодом: две реализации однажды разойдутся, и разойдутся в счёте.
        """
        tier = pick_tier(repo.tiers(cursor, str(version["id"])), quantity)
        owner_partner, shares = repo.partner_chain(
            cursor, str(cabinet["id"]), service, occurred_on)
        shares = self._fallback_to_tariff_fee(shares, owner_partner, version)

        charge = compute_charge(quantity, tier, shares)
        accrual = repo.insert_accrual(
            cursor, event_id=event_id, event_type=event_type, tenant_id=tenant,
            correlation_id=correlation_id, cabinet=cabinet, service=service,
            tariff_version_id=str(version["id"]), charge=charge, partner_id=owner_partner,
            occurred_on=occurred_on, allocation_key=allocation_key)
        commissions = split_markup(quantity, shares, charge.partner_amount)
        # Проверка ЗДЕСЬ, а не только триггером. Триггер отложен до коммита и
        # скажет «раскладка не сходится» посреди пачки событий — с именем
        # начисления, но без того, что его породило. Здесь же видно и событие,
        # и цепочку партнёров, по которой доли считались.
        laid_out = sum(amount for _, amount in commissions)
        if laid_out != charge.partner_amount:
            raise ValueError(
                f"раскладка комиссии {laid_out} не сходится с наценкой "
                f"{charge.partner_amount} по событию {event_id} ({event_type}): "
                f"долей {len(commissions)} на цепочку из {len(shares)} партнёров")
        repo.insert_commissions(cursor, str(accrual["id"]), commissions)

        repo.emit(cursor, "billing.accrual.created.v1", tenant, {
            "accrual_id": str(accrual["id"]),
            "source_event_id": event_id,
            "source_event_type": event_type,
            "seller_external_id": cabinet["seller_external_id"],
            "service": service,
            "quantity": str(charge.quantity),
            "amount": str(charge.amount),
            "partner_amount": str(charge.partner_amount),
            "net_amount": str(charge.net_amount),
            "period": accrual["period"],
        }, correlation_id)

        ACCRUALS.labels(service=service).inc()
        ACCRUED_AMOUNT.labels(service=service).inc(float(charge.amount))
        return {"outcome": "accrued", "event_id": event_id,
                "accrual_id": str(accrual["id"]), "service": service,
                "amount": str(charge.amount), "partner_amount": str(charge.partner_amount),
                "net_amount": str(charge.net_amount)}

    def _fallback_to_tariff_fee(self, shares: list[PartnerShare], owner_partner: str | None,
                                version: dict[str, Any]) -> list[PartnerShare]:
        """Ставка последней надежды — partner_fee версии тарифа.

        Слой наценки в биллинге as-is жил именно там (раздел 2.10), и файл 04
        требует не строить его заново, а довести до конца. Доводим так:
        настоящая ставка — в price_layer партнёра, а partner_fee остаётся
        значением по умолчанию для кабинета, которому слой ещё не завели.
        Без этого перенос данных прода обнулил бы наценку Зардала.
        """
        if shares or owner_partner is None:
            return shares
        fee = Decimal(version.get("partner_fee") or 0)
        if fee <= 0:
            return shares
        return [PartnerShare(partner_id=owner_partner, markup=fee, price_layer_id=None)]

    def _auto_onboard(self, cursor: Cursor, seller: str) -> dict[str, Any]:
        """Кабинет приехал событием, а не онбордингом.

        Отказать — значит потерять выручку молча, ровно то, чем болен прод.
        Поэтому поступаем как клапан «собрать без остатка» (раздел 6.5): счёт
        выставляется, но кабинет помечен needs_onboarding и виден в счётчике,
        в отчёте и в админке. Ошибка становится видимой и адресной, а не
        останавливает деньги.
        """
        cabinet = repo.register_cabinet(cursor, seller, needs_onboarding=True)
        partner_id = self._default_partner(cursor)
        if partner_id:
            cursor.execute(
                "SELECT 1 FROM cabinet_assignment WHERE cabinet_id = %s AND to_date IS NULL",
                (cabinet["id"],))
            if cursor.fetchone() is None:
                repo.assign_cabinet(
                    cursor, str(cabinet["id"]), partner_id,
                    PartnerRole.ACCOUNT_MANAGER.value, date.today(),
                    comment="закреплён автоматически: кабинет пришёл событием, "
                            "не онбордингом — подтвердить в админке")
        CABINETS_NEEDING_ONBOARDING.inc()
        return cabinet

    def _default_partner(self, cursor: Cursor) -> str | None:
        """Партнёр по умолчанию: явно заданный либо единственный корень дерева.

        Сегодня корень один — Зардал, он приводит клиентов (раздел 1). Как
        только корней станет больше, догадка запрещена: без BILLING_DEFAULT_PARTNER
        кабинет останется без партнёра, и это будет видно.
        """
        configured = os.getenv("BILLING_DEFAULT_PARTNER", "").strip()
        if configured:
            cursor.execute(
                "SELECT id FROM partner WHERE (id::text = %s OR name = %s) AND active",
                (configured, configured))
            row = cursor.fetchone()
            return str(row["id"]) if row else None
        cursor.execute("SELECT id FROM partner WHERE parent_id IS NULL AND active")
        roots = cursor.fetchall()
        return str(roots[0]["id"]) if len(roots) == 1 else None

    def _unbilled(self, cursor: Cursor, envelope: Envelope, reason: UnbilledReason,
                  detail: str) -> dict[str, Any]:
        repo.record_unbilled(cursor, envelope.event_id, envelope.type, reason.value, detail,
                             envelope.payload, envelope.tenant_id, envelope.occurred_at)
        UNBILLED.labels(reason=reason.value).inc()
        return {"outcome": "unbilled", "event_id": envelope.event_id,
                "reason": reason.value, "detail": detail}

    @staticmethod
    def _resolve_seller(envelope: Envelope, paths: Any) -> str | None:
        for path in list(paths or []):
            value = repo.dig(envelope.payload, str(path))
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _resolve_quantity(envelope: Envelope, path: str | None) -> Decimal | None:
        if not path:
            # Одно физическое действие — одна единица. Так тарифицируется
            # упаковка: событие есть, количества в нём нет и не нужно.
            return Decimal("1")
        value = repo.dig(envelope.payload, path)
        if value is None:
            return None
        try:
            quantity = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        # `Decimal("NaN")` и `Decimal("Infinity")` разбираются успешно, а
        # дальше превращаются в сумму счёта: `NaN` проходит все сравнения
        # ложью, и начисление уходит клиенту числом, которого не бывает.
        if not quantity.is_finite():
            return None
        return quantity

    @staticmethod
    def _allocation_key(envelope: Envelope) -> str | None:
        for path in ("task_id", "supply", "order_id", "receipt_id", "return_id"):
            value = repo.dig(envelope.payload, path)
            if value not in (None, ""):
                return str(value)
        return None

    def _record_shift(self, cursor: Cursor, envelope: Envelope) -> None:
        """Выработка смены пишется независимо от того, тарифицируется ли событие.

        Скан у стойки клиенту не выставляется, но рабочее время он стоит —
        без этого не посчитать себестоимость операции, а значит и маржу.
        """
        operation = SHIFT_OPERATIONS.get(envelope.type)
        actor = repo.dig(envelope.payload, "actor_id")
        if not operation or not actor:
            return
        quantity = self._resolve_quantity(envelope, "qty") or Decimal("1")
        try:
            # Точка сохранения, а не общая транзакция: выработка — витрина, а
            # не деньги, и кривой actor_id в чужом событии не имеет права
            # отменить начисление. Без savepoint отказ здесь откатил бы всё:
            # в Postgres упавший запрос ломает транзакцию целиком.
            with cursor.connection.transaction():
                repo.record_shift_output(
                    cursor, event_id=envelope.event_id, event_type=envelope.type,
                    actor_id=str(actor), occurred_at=envelope.occurred_at,
                    operation=operation, cabinet_id=None, quantity=quantity)
        except Exception:
            SHIFT_OUTPUT_REJECTED.labels(event_type=envelope.type).inc()


    # ------------------------------------------------------- хранение (крон)

    STORAGE_SERVICE = Service.STORAGE.value

    def accrue_storage(self, day: date, places: Callable[[str], int],
                       *, tenant: str | None = None) -> list[dict[str, Any]]:
        """Начисляет хранение за сутки по всем кабинетам.

        Хранение — самый предсказуемый доход фулфилмента, и сегодня оно не
        тарифицируется вовсе (раздел 3.4, файл 04). Единица — коробко-место ×
        сутки (файл 04), поэтому количество берётся у склада: сколько коробок
        клиента стояло в этот день.

        `places` — функция «кабинет → коробко-мест». Вызов в склад делает
        воркер, снаружи транзакции (инвариант 2); сюда приходит уже число.

        Идемпотентность — по вычислимому `event_id`: uuid5 от кабинета и даты.
        Повторный прогон за те же сутки не начисляет второй раз, а значит,
        воркер можно гонять хоть каждый час и догонять пропущенные дни.
        """
        results: list[dict[str, Any]] = []
        with self.db.cursor() as cursor:
            cursor.execute("SELECT * FROM cabinet WHERE active ORDER BY seller_external_id")
            cabinets = [dict(row) for row in cursor.fetchall()]

        for cabinet in cabinets:
            try:
                quantity = Decimal(places(str(cabinet["seller_external_id"])))
            except Exception as failure:  # noqa: BLE001 — один кабинет не валит остальные
                results.append({"outcome": "unbilled", "reason": "NO_QUANTITY",
                                "seller": cabinet["seller_external_id"], "detail": str(failure)})
                UNBILLED.labels(reason=UnbilledReason.NO_QUANTITY.value).inc()
                continue
            if quantity <= 0:
                # Клиент, у которого в этот день не стояло ни одной коробки,
                # за хранение не платит. Это не ошибка и в unbilled не идёт.
                continue
            # Кабинет в исходе обязателен: воркер пишет в лог «начислено кому»,
            # а не «начислено N штук» — по второму разбирать нечего.
            results.append({**self._accrue_storage_day(cabinet, quantity, day,
                                                       tenant or tenant_id()),
                            "seller": cabinet["seller_external_id"]})
        WORKER_PROCESSED.labels(worker="storage").inc(len(results))
        return results

    def _accrue_storage_day(self, cabinet: dict[str, Any], quantity: Decimal, day: date,
                            tenant: str) -> dict[str, Any]:
        event_id = str(uuid.uuid5(STORAGE_NAMESPACE, f"{cabinet['id']}:{day.isoformat()}"))
        with self.db.transaction() as cursor:
            cursor.execute("SELECT 1 FROM billing_accrual WHERE event_id = %s", (event_id,))
            if cursor.fetchone():
                return {"outcome": "duplicate", "event_id": event_id,
                        "seller": cabinet["seller_external_id"]}
            version = repo.tariff_version(cursor, str(cabinet["id"]), self.STORAGE_SERVICE, day)
            if version is None or not version["approved"]:
                reason = (UnbilledReason.NO_TARIFF if version is None
                          else UnbilledReason.TARIFF_NOT_APPROVED)
                repo.record_unbilled(
                    cursor, event_id, "billing.storage.day.v1", reason.value,
                    f"хранение за {day} по кабинету {cabinet['seller_external_id']}",
                    {"seller_external_id": cabinet["seller_external_id"],
                     "places": str(quantity), "day": day.isoformat()}, tenant, None)
                UNBILLED.labels(reason=reason.value).inc()
                return {"outcome": "unbilled", "reason": reason.value,
                        "seller": cabinet["seller_external_id"]}
            return self._charge(
                cursor, cabinet=cabinet, service=self.STORAGE_SERVICE, quantity=quantity,
                occurred_on=day, version=version, event_id=event_id,
                event_type="billing.storage.day.v1", tenant=tenant, correlation_id=None,
                allocation_key=f"storage:{day.isoformat()}")

    # ---------------------------------------------------------------- отчёты

    def margin(self, period: str) -> list[dict[str, Any]]:
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM billing_margin_by_cabinet WHERE period = %s "
                "ORDER BY margin ASC", (period,))
            return [dict(row) for row in cursor.fetchall()]

    def unbilled(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT reason, count(*) AS events, min(created_at) AS since "
                "FROM billing_unbilled WHERE resolved_at IS NULL "
                "GROUP BY reason ORDER BY events DESC LIMIT %s", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def shift_output(self, day: date) -> list[dict[str, Any]]:
        with self.db.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM billing_shift_summary WHERE occurred_on = %s "
                "ORDER BY actor_id, operation", (day,))
            return [dict(row) for row in cursor.fetchall()]

    def commission(self, partner_id: str, period: str) -> dict[str, Any]:
        """Комиссия партнёра за период: своя и по всей ветке.

        Менеджер видит свою, старший — ветку целиком (файл 04). Разделение
        считается здесь, а не фильтром в обработчике: забытый фильтр показывает
        менеджеру чужие деньги.
        """
        with self.db.cursor() as cursor:
            branch = repo.subtree_ids(cursor, partner_id)
            cursor.execute(
                """
                SELECT c.partner_id,
                       sum(c.amount)                                     AS amount,
                       count(*)                                          AS operations,
                       sum(CASE WHEN a.partner_payout_state = 'payable'
                                THEN c.amount ELSE 0 END)                AS payable,
                       sum(CASE WHEN a.partner_payout_state = 'pending'
                                THEN c.amount ELSE 0 END)                AS pending
                  FROM billing_commission c
                  JOIN billing_accrual a ON a.id = c.accrual_id
                 WHERE a.period = %s AND c.partner_id = ANY(%s)
                 GROUP BY c.partner_id
                """,
                (period, branch))
            rows = [dict(row) for row in cursor.fetchall()]
        own = next((row for row in rows if str(row["partner_id"]) == str(partner_id)), None)
        return {
            "partner_id": partner_id, "period": period,
            "own": own or {"amount": Decimal("0"), "operations": 0},
            "branch": rows,
            "branch_total": sum((Decimal(row["amount"]) for row in rows), Decimal("0")),
        }


def _uuid_or_new(given: str, error: str) -> tuple[str, str]:
    """Ключ отказа: сам `event_id`, если он uuid, иначе новый — с оригиналом.

    Оригинал уходит в `detail`: без него разбирать нечего — событие есть,
    а чьё оно, неизвестно.
    """
    import uuid as _uuid

    try:
        return str(_uuid.UUID(given)), error
    except (ValueError, AttributeError):
        replacement = repo.new_id()
        origin = f"event_id {given!r} не uuid, записан под {replacement}" if given \
            else "event_id отсутствует"
        return replacement, f"{error}; {origin}"
