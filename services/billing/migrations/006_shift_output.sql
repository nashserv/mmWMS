-- Поток C. Выработка смены — кто сколько сделал (файл 04).
--
-- Без этого не посчитать ни ФОТ по факту, ни себестоимость операции, а значит
-- и маржа из 005 остаётся неполной: расход разносится по объёму операций
-- клиента, но не по тому, сколько человеко-часов он реально стоил.
--
-- Источник — события физических действий: wms.item.scanned.v1,
-- wms.packing.completed.v1, приёмки и инвентаризации (раздел 4, приложение A).

CREATE TABLE billing_shift_output (
    id          uuid PRIMARY KEY,
    event_id    uuid UNIQUE NOT NULL,
    event_type  text NOT NULL,
    -- Пользователь identity, выполнивший действие. Если событие пришло без
    -- actor_id — строка не создаётся, а событие уходит в billing_unbilled:
    -- выработка без исполнителя не выработка.
    actor_id    uuid NOT NULL,
    occurred_on date NOT NULL,
    occurred_at timestamptz NOT NULL,
    -- Смена: сутки склада. Дневная и ночная не разделяются — склад работает
    -- одной сменой (раздел 4); при появлении второй здесь добавится код смены.
    operation   text NOT NULL,
    cabinet_id  uuid REFERENCES cabinet (id),
    quantity    numeric(14, 3) NOT NULL DEFAULT 1 CHECK (quantity > 0),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX billing_shift_output_day_idx ON billing_shift_output (occurred_on, actor_id);
CREATE INDEX billing_shift_output_actor_idx ON billing_shift_output (actor_id, occurred_on);

-- Витрина для начальника склада: человек, день, что и сколько.
CREATE VIEW billing_shift_summary AS
SELECT actor_id,
       occurred_on,
       operation,
       count(*)      AS actions,
       sum(quantity) AS units
  FROM billing_shift_output
 GROUP BY actor_id, occurred_on, operation;
