"""Административные операции: партнёры, кабинеты, тарифы, счета, расходы.

Отдельно от service.py намеренно: там — конвейер событий, который крутится сам,
здесь — то, что делает человек. Смешивать их значит однажды уронить начисления
релизом админки.

Главное требование файла 04 к этому модулю: **ни одного ручного SQL-запроса в
онбординге**. Сегодня 21 кабинет WB заведён при 4 записях в sellers — это следы
ручной, непоследовательной заводки, которая должна исчезнуть.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from . import repositories as repo
from .db import Database
from .domain import PartnerRole, Service
from .money import money
from .wms_client import WmsClient, WmsUnavailable

# Форма JWT. Токены Wildberries — именно JWT (приложение D), и попасть в базу
# биллинга открытым текстом они не имеют права никогда (инвариант 15).
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
# Ссылка на секрет: схема провайдера, а не значение.
_SECRET_REF_RE = re.compile(r"^(vault|secret|env|stand-fake-secret)[:/-]")


class OnboardingError(RuntimeError):
    """Онбординг остановлен. Причина — в тексте, полдела не остаётся в базе."""


def check_secret_ref(value: str | None) -> str:
    """Пропускает только ссылку на секрет, но не сам секрет.

    Проверка стоит на входе, а не в ревью: ревью пропускает, а цикл — нет.
    Токен, доехавший до базы, уже утёк, даже если его потом удалить.
    """
    text = (value or "").strip()
    if not text:
        raise OnboardingError("secret_ref обязателен: кабинет без ссылки на секрет не подключить")
    if _JWT_RE.search(text):
        # Значение не показываем — иначе секрет утечёт в текст ошибки и в лог.
        raise OnboardingError(
            "в secret_ref передан живой токен WB. Нужна ссылка вида vault://mmx/wb/<кабинет>, "
            "значение токена в биллинг не передаётся никогда (инвариант 15)")
    if not _SECRET_REF_RE.match(text):
        raise OnboardingError(
            f"secret_ref {text!r} не похож на ссылку на секрет: ожидается vault://…, secret://… "
            f"или заглушка стенда stand-fake-secret-…")
    return text


class Admin:
    def __init__(self, database: Database, wms: WmsClient | None = None) -> None:
        self.db = database
        self.wms = wms or WmsClient()

    # -------------------------------------------------------------- партнёры

    def create_partner(self, name: str, *, parent_id: str | None = None,
                       user_id: str | None = None) -> dict[str, Any]:
        with self.db.transaction() as cursor:
            return dict(repo.create_partner(cursor, name, parent_id=parent_id, user_id=user_id))

    def partner_tree(self) -> list[dict[str, Any]]:
        with self.db.cursor() as cursor:
            return [dict(row) for row in repo.partner_tree(cursor)]

    def set_markup(self, partner_id: str, service: str, markup: Decimal, from_date: date,
                   *, cabinet_id: str | None = None) -> dict[str, Any]:
        Service(service)
        with self.db.transaction() as cursor:
            return dict(repo.set_price_layer(cursor, partner_id, service, money(markup),
                                             from_date, cabinet_id=cabinet_id))

    def partner_cabinets(self, partner_id: str, *, on: date | None = None) -> list[dict[str, Any]]:
        """Клиенты партнёра — своя ветка целиком (файл 04, «Роли и права»)."""
        moment = on or date.today()
        with self.db.cursor() as cursor:
            branch = repo.subtree_ids(cursor, partner_id)
            cursor.execute(
                """
                SELECT c.*, ca.partner_id, ca.role, ca.from_date
                  FROM cabinet c
                  JOIN cabinet_assignment ca ON ca.cabinet_id = c.id
                 WHERE ca.partner_id = ANY(%s)
                   AND ca.from_date <= %s AND (ca.to_date IS NULL OR ca.to_date > %s)
                 ORDER BY c.seller_external_id
                """,
                (branch, moment, moment))
            return [dict(row) for row in cursor.fetchall()]

    def assign_cabinet(self, cabinet_id: str, partner_id: str, role: str,
                       from_date: date, *, comment: str | None = None) -> dict[str, Any]:
        PartnerRole(role)
        with self.db.transaction() as cursor:
            return dict(repo.assign_cabinet(cursor, cabinet_id, partner_id, role, from_date,
                                            comment=comment))

    # --------------------------------------------------------------- тарифы

    def create_tariff(self, code: str, service: str, name: str, unit: str,
                      *, is_default: bool = False) -> dict[str, Any]:
        Service(service)
        with self.db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_tariff (id, code, service, name, unit, is_default)
                     VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, unit = EXCLUDED.unit
                  RETURNING *
                """,
                (repo.new_id(), code, service, name, unit, is_default))
            return dict(cursor.fetchone())

    def add_version(self, tariff_id: str, effective_from: date, tiers: Sequence[dict[str, Any]],
                    *, partner_fee: Decimal = Decimal("0"),
                    accumulation: str = "per_event") -> dict[str, Any]:
        """Новая версия тарифа. Предыдущая закрывается этой же датой.

        Закрытие, а не замена: начисления закрытых месяцев ссылаются на версию,
        по которой посчитаны, и обязаны читаться так же и через год.
        """
        if not tiers:
            raise OnboardingError("версия тарифа без ступеней не тарифицирует ничего")
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE billing_tariff_version SET effective_to = %s "
                " WHERE tariff_id = %s AND effective_to IS NULL AND effective_from < %s",
                (effective_from, tariff_id, effective_from))
            cursor.execute(
                """
                INSERT INTO billing_tariff_version (id, tariff_id, effective_from,
                                                    accumulation, partner_fee)
                     VALUES (%s, %s, %s, %s, %s) RETURNING *
                """,
                (repo.new_id(), tariff_id, effective_from, accumulation, money(partner_fee)))
            version = dict(cursor.fetchone())
            for tier in tiers:
                cursor.execute(
                    """
                    INSERT INTO billing_tariff_tier (id, version_id, up_to, unit_price, minimum)
                         VALUES (%s, %s, %s, %s, %s)
                    """,
                    (repo.new_id(), version["id"],
                     (Decimal(str(tier["up_to"])) if tier.get("up_to") is not None else None),
                     money(Decimal(str(tier["unit_price"]))),
                     money(Decimal(str(tier.get("minimum", 0))))))
            return version

    def approve_version(self, version_id: str, approved_by: str) -> dict[str, Any]:
        """Цена без подписи в счёт клиенту не идёт."""
        if not (approved_by or "").strip():
            raise OnboardingError("утверждение тарифа требует имени: цена без подписи спорна")
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE billing_tariff_version SET approved = true, approved_by = %s, "
                "approved_at = now() WHERE id = %s RETURNING *",
                (approved_by, version_id))
            row = cursor.fetchone()
            if row is None:
                raise OnboardingError(f"версия тарифа {version_id} не найдена")
            return dict(row)

    def tariffs(self) -> list[dict[str, Any]]:
        with self.db.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.*, v.id AS version_id, v.effective_from, v.effective_to, v.approved,
                       v.partner_fee, v.accumulation,
                       (SELECT json_agg(json_build_object('up_to', tr.up_to,
                                                          'unit_price', tr.unit_price,
                                                          'minimum', tr.minimum))
                          FROM billing_tariff_tier tr WHERE tr.version_id = v.id) AS tiers
                  FROM billing_tariff t
             LEFT JOIN billing_tariff_version v ON v.tariff_id = t.id AND v.effective_to IS NULL
                 ORDER BY t.service, t.code
                """)
            return [dict(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------- онбординг

    def onboard(self, *, seller_external_id: str, name: str, inn: str | None,
                contract_reference: str, partner_id: str | None,
                wb_account_external_id: str | None = None, secret_ref: str | None = None,
                tariffs: dict[str, str] | None = None,
                opening_stock: Sequence[dict[str, Any]] = (),
                from_date: date | None = None,
                role: str = PartnerRole.ACCOUNT_MANAGER.value) -> dict[str, Any]:
        """Онбординг клиента одним потоком (файл 04).

        Договор → тариф → партнёр → токен WB → владелец в `wms` → ячейки →
        начальный остаток. Порядок именно такой: пока в биллинге нет договора и
        партнёра, заводить владельца на складе не за чем — операция приедет и
        не будет знать, кому её выставлять.

        Вызовы в `wms` идут снаружи транзакции (инвариант 2). Поэтому шаг,
        упавший на стороне склада, оставляет клиента заведённым в биллинге и
        сообщает, что именно повторить, — а не откатывает половину молча.
        """
        start = from_date or date.today()
        secret = check_secret_ref(secret_ref) if wb_account_external_id else None

        with self.db.transaction() as cursor:
            cabinet = repo.register_cabinet(
                cursor, seller_external_id, name=name, inn=inn, needs_onboarding=False)
            cursor.execute("UPDATE cabinet SET needs_onboarding = false WHERE id = %s",
                           (cabinet["id"],))
            if wb_account_external_id:
                # В базу биллинга едет ссылка на секрет, проверенная выше.
                # Значение токена не проходит через этот сервис вообще.
                repo.attach_wb_account(cursor, str(cabinet["id"]), wb_account_external_id,
                                       name, secret)
            cursor.execute(
                """
                INSERT INTO billing_contract (id, cabinet_id, reference, from_date, signed_at)
                     VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (reference) DO UPDATE SET cabinet_id = EXCLUDED.cabinet_id
                  RETURNING *
                """,
                (repo.new_id(), cabinet["id"], contract_reference, start, start))
            contract = dict(cursor.fetchone())

            for service, tariff_id in (tariffs or {}).items():
                Service(service)
                cursor.execute(
                    """
                    INSERT INTO billing_tariff_assignment (id, cabinet_id, tariff_id,
                                                           contract_id, service, from_date)
                         VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (repo.new_id(), cabinet["id"], tariff_id, contract["id"], service, start))

            if partner_id:
                repo.assign_cabinet(cursor, str(cabinet["id"]), partner_id, role, start,
                                    comment=f"онбординг по договору {contract_reference}")
            cabinet = dict(cabinet)

        steps: list[dict[str, Any]] = [
            {"step": "договор", "state": "ok", "reference": contract_reference},
            {"step": "тарифы", "state": "ok", "services": sorted((tariffs or {}).keys())},
            {"step": "партнёр", "state": "ok" if partner_id else "пропущен",
             "partner_id": partner_id},
        ]

        # --- дальше вызовы в wms, вне транзакции --------------------------
        try:
            created = self.wms.ensure_owner(seller_external_id, name, inn)
            # Запоминаем owner.id склада: события физического действия несут
            # именно его, а не внешний ключ продавца.
            owner_id = ((created.get("seller") or {}).get("owner_id")
                        or (created.get("seller") or {}).get("id"))
            if owner_id:
                with self.db.transaction() as cursor:
                    cursor.execute(
                        "UPDATE cabinet SET wms_owner_id = %s::uuid WHERE id = %s "
                        "  AND wms_owner_id IS DISTINCT FROM %s::uuid",
                        (str(owner_id), cabinet["id"], str(owner_id)))
            steps.append({"step": "владелец в wms", "state": "ok", "owner_id": owner_id})
        except WmsUnavailable as failure:
            steps.append({"step": "владелец в wms", "state": "ошибка", "detail": str(failure)})
            return {"cabinet": cabinet, "steps": steps, "state": "incomplete"}

        if wb_account_external_id and secret:
            try:
                self.wms.connect_wb_account(wb_account_external_id, seller_external_id,
                                            name, secret)
                steps.append({"step": "кабинет WB", "state": "ok",
                              "secret_ref": secret, "token": "не передавался"})
            except WmsUnavailable as failure:
                steps.append({"step": "кабинет WB", "state": "ошибка", "detail": str(failure)})

        if opening_stock:
            reference = f"OPEN-{seller_external_id}-{start.isoformat()}"
            try:
                for line in opening_stock:
                    self.wms.ensure_product(seller_external_id, str(line["barcode"]),
                                            line.get("seller_sku"), line.get("name"))
                result = self.wms.load_opening_stock(seller_external_id, reference,
                                                     [dict(line) for line in opening_stock])
                steps.append({"step": "начальный остаток", "state": result.get("state", "ok"),
                              "reference": reference, "lines": len(opening_stock)})
            except WmsUnavailable as failure:
                steps.append({"step": "начальный остаток", "state": "ошибка",
                              "detail": str(failure)})

        state = "ok" if all(step["state"] in ("ok", "applied", "пропущен") for step in steps) \
            else "incomplete"
        return {"cabinet": cabinet, "contract": contract, "steps": steps, "state": state}

    def cabinets(self, *, only_needing_onboarding: bool = False) -> list[dict[str, Any]]:
        with self.db.cursor() as cursor:
            return [dict(row) for row in repo.cabinets(
                cursor, only_needing_onboarding=only_needing_onboarding)]

    # --------------------------------------------------- периоды, счета, акты

    def close_period(self, period: str, closed_by: str) -> dict[str, Any]:
        with self.db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_period (period, state, closed_at, closed_by)
                     VALUES (%s, 'closed', now(), %s)
                ON CONFLICT (period) DO UPDATE
                    SET state = 'closed', closed_at = now(), closed_by = EXCLUDED.closed_by
                  RETURNING *
                """,
                (period, closed_by))
            return dict(cursor.fetchone())

    def issue_invoice(self, cabinet_id: str, period: str, number: str) -> dict[str, Any]:
        """Счёт за период по начислениям, которые уже лежат в базе.

        Итоги фиксируются в счёте, а строки остаются ссылками: расшифровка в ЛК
        клиента показывает те же начисления, по которым посчитан итог, включая
        видимую наценку партнёра (файл 04, «ЛК клиента»).
        """
        with self.db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO billing_period (period) VALUES (%s) ON CONFLICT DO NOTHING",
                (period,))
            cursor.execute(
                """
                INSERT INTO billing_invoice (id, cabinet_id, period, number, state, issued_at)
                     VALUES (%s, %s, %s, %s, 'issued', now())
                ON CONFLICT (cabinet_id, period) DO UPDATE SET number = EXCLUDED.number
                  RETURNING *
                """,
                (repo.new_id(), cabinet_id, period, number))
            invoice = dict(cursor.fetchone())
            cursor.execute(
                "UPDATE billing_accrual SET invoice_id = %s "
                " WHERE cabinet_id = %s AND period = %s AND invoice_id IS NULL",
                (invoice["id"], cabinet_id, period))
            cursor.execute(
                """
                UPDATE billing_invoice SET
                    total_amount  = totals.amount,
                    partner_total = totals.partner_amount,
                    net_total     = totals.net_amount
                  FROM (SELECT COALESCE(sum(amount), 0) AS amount,
                               COALESCE(sum(partner_amount), 0) AS partner_amount,
                               COALESCE(sum(net_amount), 0) AS net_amount
                          FROM billing_accrual WHERE invoice_id = %s) totals
                 WHERE billing_invoice.id = %s
             RETURNING billing_invoice.*
                """,
                (invoice["id"], invoice["id"]))
            return dict(cursor.fetchone())

    def pay_invoice(self, invoice_id: str) -> dict[str, Any]:
        """Оплата счёта. Триггер переводит вознаграждение партнёра в payable.

        Признание по факту оплаты (файл 04): не платить партнёру с
        невыставленного или неоплаченного счёта.
        """
        with self.db.transaction() as cursor:
            cursor.execute(
                "UPDATE billing_invoice SET state = 'paid', paid_at = now() "
                " WHERE id = %s RETURNING *", (invoice_id,))
            row = cursor.fetchone()
            if row is None:
                raise OnboardingError(f"счёт {invoice_id} не найден")
            return dict(row)

    def invoice(self, invoice_id: str) -> dict[str, Any]:
        """Счёт с расшифровкой: клиент видит 45 и понимает, из чего они."""
        with self.db.cursor() as cursor:
            cursor.execute("SELECT * FROM billing_invoice WHERE id = %s", (invoice_id,))
            invoice = cursor.fetchone()
            if invoice is None:
                raise OnboardingError(f"счёт {invoice_id} не найден")
            cursor.execute(
                """
                SELECT a.service, count(*) AS operations, sum(a.quantity) AS quantity,
                       sum(a.amount) AS amount, sum(a.partner_amount) AS partner_amount,
                       sum(a.net_amount) AS net_amount,
                       max(a.unit_price) AS unit_price, max(a.markup) AS markup
                  FROM billing_accrual a WHERE a.invoice_id = %s
                 GROUP BY a.service ORDER BY a.service
                """,
                (invoice_id,))
            lines = [dict(row) for row in cursor.fetchall()]
        return {"invoice": dict(invoice), "lines": lines}

    # ---------------------------------------------------- расходы и себестоимость

    def add_expense(self, period: str, category: str, amount: Decimal, comment: str,
                    *, allocation_rule: str = "by_operations",
                    cabinet_id: str | None = None) -> dict[str, Any]:
        if not (comment or "").strip():
            raise OnboardingError("расход без комментария неразбираем через месяц")
        with self.db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_fixed_expense (id, period, category, amount,
                                                   allocation_rule, cabinet_id, comment)
                     VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *
                """,
                (repo.new_id(), period, category, money(amount), allocation_rule,
                 cabinet_id, comment))
            return dict(cursor.fetchone())

    def allocate_period(self, period: str) -> dict[str, Any]:
        """Разносит постоянные расходы периода по кабинетам.

        Правило разнесения — колонка расхода, а не соглашение: файл 04 требует
        зафиксировать его явно. Разнесённые суммы сохраняются строками вместе с
        базой и долей — отчёт за закрытый месяц читается одинаково и через год,
        и «на вас отнесено 4210 ₽» можно объяснить клиенту.
        """
        with self.db.transaction() as cursor:
            cursor.execute("SELECT * FROM billing_fixed_expense WHERE period = %s", (period,))
            expenses = list(cursor.fetchall())
            if not expenses:
                return {"period": period, "expenses": 0, "allocations": 0}

            cursor.execute(
                """
                SELECT cabinet_id, count(*) AS operations, COALESCE(sum(net_amount), 0) AS revenue
                  FROM billing_accrual WHERE period = %s GROUP BY cabinet_id
                """,
                (period,))
            basis_rows = [dict(row) for row in cursor.fetchall()]

            cursor.execute(
                "DELETE FROM billing_expense_allocation WHERE period = %s", (period,))

            written = 0
            for expense in expenses:
                rule = expense["allocation_rule"]
                if rule == "direct":
                    pairs = [(str(expense["cabinet_id"]), Decimal("1"))]
                    total = Decimal("1")
                    basis = "direct"
                else:
                    field = "operations" if rule == "by_operations" else "revenue"
                    basis = field
                    pairs = [(str(row["cabinet_id"]), Decimal(row[field])) for row in basis_rows
                             if Decimal(row[field]) > 0]
                    total = sum((value for _, value in pairs), Decimal("0"))
                if total <= 0 or not pairs:
                    # Расход есть, а базы разнесения нет: за период не было ни
                    # одной операции. Молча делить не на что — строка остаётся
                    # неразнесённой и видна в отчёте.
                    continue
                amount = Decimal(expense["amount"])
                spent = Decimal("0")
                for index, (cabinet_id, value) in enumerate(pairs):
                    share = (money(amount * value / total) if index < len(pairs) - 1
                             else money(amount - spent))
                    spent += share
                    cursor.execute(
                        """
                        INSERT INTO billing_expense_allocation
                            (id, expense_id, cabinet_id, period, amount, basis,
                             basis_value, basis_total)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (repo.new_id(), expense["id"], cabinet_id, period, share, basis,
                         value, total))
                    written += 1
            return {"period": period, "expenses": len(expenses), "allocations": written}
