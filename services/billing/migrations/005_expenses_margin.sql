-- Поток C. Постоянные расходы, их разнесение по кабинетам и маржа.
--
-- billing_fixed_expense на проде пуст (раздел 2.10), поэтому маржа не считается
-- вовсе: видна выручка и не видно, какой из 21 кабинета съедает смену.
--
-- Правило разнесения — колонка, а не соглашение в голове. Файл 04 требует
-- прямо: «зафиксировать в коде, не оставлять implicit». Разнесённые суммы
-- сохраняются строками, а не считаются на лету: отчёт за закрытый месяц
-- обязан читаться одинаково и сегодня, и через год после смены правила.

CREATE TABLE billing_fixed_expense (
    id         uuid PRIMARY KEY,
    period     text NOT NULL CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    -- payroll — ФОТ смены, rent — аренда, consumables — расходники,
    -- software — сервисы и лицензии, other — прочее с обязательным комментарием.
    category   text NOT NULL CHECK (category IN ('payroll', 'rent', 'consumables',
                                                 'software', 'other')),
    amount     numeric(14, 2) NOT NULL CHECK (amount >= 0),
    -- Как разносить: by_operations — пропорционально числу тарифицируемых
    -- операций кабинета, by_revenue — пропорционально выручке,
    -- direct — целиком на один кабинет (тогда cabinet_id обязателен).
    allocation_rule text NOT NULL DEFAULT 'by_operations'
        CHECK (allocation_rule IN ('by_operations', 'by_revenue', 'direct')),
    cabinet_id uuid REFERENCES cabinet (id),
    comment    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_fixed_expense_direct_has_cabinet CHECK (
        allocation_rule <> 'direct' OR cabinet_id IS NOT NULL
    )
);

CREATE INDEX billing_fixed_expense_period_idx ON billing_fixed_expense (period);

CREATE TABLE billing_expense_allocation (
    id          uuid PRIMARY KEY,
    expense_id  uuid NOT NULL REFERENCES billing_fixed_expense (id) ON DELETE CASCADE,
    cabinet_id  uuid NOT NULL REFERENCES cabinet (id),
    period      text NOT NULL,
    amount      numeric(14, 2) NOT NULL CHECK (amount >= 0),
    -- База разнесения и доля кабинета в ней: без них цифра «на вас отнесено
    -- 4210 ₽» неоспорима и потому бесполезна в разговоре с клиентом.
    basis       text NOT NULL,
    basis_value numeric(18, 4) NOT NULL,
    basis_total numeric(18, 4) NOT NULL CHECK (basis_total > 0),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_expense_allocation_once UNIQUE (expense_id, cabinet_id)
);

CREATE INDEX billing_expense_allocation_period_idx
    ON billing_expense_allocation (period, cabinet_id);

-- === Маржа по кабинету =====================================================
-- выручка − наценка партнёра − себестоимость = маржа (файл 04).
-- Представление, а не таблица: пересчитывается из начислений и разнесений,
-- которые уже зафиксированы.
CREATE VIEW billing_margin_by_cabinet AS
WITH revenue AS (
    SELECT a.cabinet_id,
           a.period,
           sum(a.amount)         AS gross,
           sum(a.partner_amount) AS partner_fee,
           sum(a.net_amount)     AS net,
           count(*)              AS operations
      FROM billing_accrual a
     GROUP BY a.cabinet_id, a.period
),
cost AS (
    SELECT e.cabinet_id, e.period, sum(e.amount) AS allocated
      FROM billing_expense_allocation e
     GROUP BY e.cabinet_id, e.period
)
SELECT c.id                                   AS cabinet_id,
       c.seller_external_id,
       c.name                                 AS cabinet_name,
       COALESCE(r.period, x.period)           AS period,
       COALESCE(r.gross, 0)                   AS gross_revenue,
       COALESCE(r.partner_fee, 0)             AS partner_fee,
       COALESCE(r.net, 0)                     AS net_revenue,
       COALESCE(x.allocated, 0)               AS allocated_cost,
       COALESCE(r.net, 0) - COALESCE(x.allocated, 0) AS margin,
       COALESCE(r.operations, 0)              AS operations
  FROM cabinet c
  LEFT JOIN revenue r ON r.cabinet_id = c.id
  LEFT JOIN cost x ON x.cabinet_id = c.id AND x.period = r.period
 WHERE r.period IS NOT NULL OR x.period IS NOT NULL;

COMMENT ON VIEW billing_margin_by_cabinet IS
    'Первое, что показывает, какой кабинет прибылен, а какой съедает смену (файл 04).';
