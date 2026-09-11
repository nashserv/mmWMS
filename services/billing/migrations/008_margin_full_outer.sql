-- Кабинет, который съедает смену и ничего не приносит, обязан быть виден.
--
-- `LEFT JOIN cost x ON x.cabinet_id = c.id AND x.period = r.period` завязывал
-- себестоимость на период ВЫРУЧКИ. Нет выручки — `r.period` пуст, условие не
-- выполняется, и строка расходов не приезжает тоже: кабинет с разнесёнными
-- расходами и нулевой выручкой исчезал из отчёта целиком.
--
-- Это ровно тот кабинет, ради которого отчёт и существует: он показывает,
-- какой клиент прибылен, а какой съедает смену (файл 04). Отчёт молчал именно
-- о втором.
DROP VIEW IF EXISTS billing_margin_by_cabinet;

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
),
-- Пара (кабинет, период) собирается ОБЕИМИ сторонами: строка есть, если есть
-- хоть выручка, хоть расход.
grid AS (
    SELECT COALESCE(r.cabinet_id, x.cabinet_id) AS cabinet_id,
           COALESCE(r.period, x.period)         AS period,
           r.gross, r.partner_fee, r.net, r.operations,
           x.allocated
      FROM revenue r
      FULL OUTER JOIN cost x
        ON x.cabinet_id = r.cabinet_id AND x.period = r.period
)
SELECT c.id                                        AS cabinet_id,
       c.seller_external_id,
       c.name                                      AS cabinet_name,
       g.period                                    AS period,
       COALESCE(g.gross, 0)                        AS gross_revenue,
       COALESCE(g.partner_fee, 0)                  AS partner_fee,
       COALESCE(g.net, 0)                          AS net_revenue,
       COALESCE(g.allocated, 0)                    AS allocated_cost,
       COALESCE(g.net, 0) - COALESCE(g.allocated, 0) AS margin,
       COALESCE(g.operations, 0)                   AS operations
  FROM grid g
  JOIN cabinet c ON c.id = g.cabinet_id;

COMMENT ON VIEW billing_margin_by_cabinet IS
    'Первое, что показывает, какой кабинет прибылен, а какой съедает смену (файл 04).';
