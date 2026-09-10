-- Поток 0. Приёмка, расхождения, инвентаризация (раздел 7).
-- Пять приёмщиков работают параллельно (раздел 4), поэтому приёмка идёт
-- документом с идемпотентным reference, а не «просто добавить на склад».

CREATE TABLE receipt (
    id           uuid PRIMARY KEY,
    owner_id     uuid NOT NULL REFERENCES owner (id),
    -- Ключ идемпотентности приёмки (инвариант 5, приложение C).
    reference    text UNIQUE NOT NULL,
    warehouse_id uuid NOT NULL REFERENCES warehouse (id),
    state        text NOT NULL DEFAULT 'draft'
                     CHECK (state IN ('draft', 'counting', 'accepted', 'cancelled')),
    actor_id     uuid,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, owner_id)
);

CREATE INDEX receipt_owner_state_idx ON receipt (owner_id, state);

CREATE TABLE receipt_line (
    id           uuid PRIMARY KEY,
    receipt_id   uuid NOT NULL REFERENCES receipt (id) ON DELETE CASCADE,
    sku_id       uuid NOT NULL REFERENCES sku (id),
    expected_qty integer CHECK (expected_qty IS NULL OR expected_qty >= 0),
    -- Баланс двигается по факту пересчёта, а не по ожиданию (прогон 9.6, шаг 3).
    actual_qty   integer CHECK (actual_qty IS NULL OR actual_qty >= 0),
    box_id       uuid REFERENCES box (id),
    cell_id      uuid REFERENCES cell (id)
);

CREATE INDEX receipt_line_receipt_idx ON receipt_line (receipt_id);

-- Расхождение — то, чего в боевом контуре не было вообще: _force_reservation
-- дописывал резерв молча, «инвентаризация разберёт», но инвентаризации никто
-- не сообщал (раздел 3.1). Теперь у каждой такой дописки есть строка с адресом.
CREATE TABLE discrepancy (
    id         uuid PRIMARY KEY,
    receipt_id uuid REFERENCES receipt (id),
    task_id    uuid REFERENCES wms_task (id),
    owner_id   uuid NOT NULL REFERENCES owner (id),
    sku_id     uuid NOT NULL,
    -- Раздел 7 перечисляет у discrepancy только владельца, SKU и количество,
    -- но раздел 6.5 требует писать сюда ещё и ячейку, и payload события
    -- wms.stock.shortfall.v1 её содержит. Расхождение внутри мастера закрыто
    -- в пользу 6.5 — см. docs/schema-decisions.md.
    cell_id    uuid REFERENCES cell (id),
    kind       text NOT NULL CHECK (kind IN (
                   'shortage', 'surplus', 'mismatch', 'damage', 'ledger_short')),
    qty        integer NOT NULL CHECK (qty > 0),
    decision   text NOT NULL DEFAULT 'pending'
                   CHECK (decision IN ('pending', 'accepted', 'rejected', 'written_off')),
    liable     text CHECK (liable IS NULL OR liable IN ('owner', 'warehouse', 'carrier', 'unknown')),
    comment    text,
    actor_id   uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT discrepancy_sku_matches_owner
        FOREIGN KEY (sku_id, owner_id) REFERENCES sku (id, owner_id),
    -- Расхождение всегда откуда-то взялось: из приёмки либо из задания.
    -- Ничьё расхождение — это расхождение, которое никто не пойдёт разбирать.
    CONSTRAINT discrepancy_has_a_source
        CHECK (receipt_id IS NOT NULL OR task_id IS NOT NULL)
);

CREATE INDEX discrepancy_open_idx ON discrepancy (owner_id, kind, created_at DESC)
    WHERE decision = 'pending';
-- Экран начальника склада: сборки без остатка за смену (раздел 6.5).
CREATE INDEX discrepancy_ledger_short_idx ON discrepancy (created_at DESC)
    WHERE kind = 'ledger_short';

CREATE TABLE inventory_count (
    id         uuid PRIMARY KEY,
    owner_id   uuid NOT NULL REFERENCES owner (id),
    reference  text UNIQUE NOT NULL,
    scope      text NOT NULL CHECK (scope IN ('partial', 'full')),
    state      text NOT NULL DEFAULT 'draft'
                   CHECK (state IN ('draft', 'counting', 'applied', 'cancelled')),
    actor_id   uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    applied_at timestamptz,
    CONSTRAINT inventory_count_applied_has_time
        CHECK (state <> 'applied' OR applied_at IS NOT NULL)
);

CREATE TABLE inventory_count_line (
    id           uuid PRIMARY KEY,
    count_id     uuid NOT NULL REFERENCES inventory_count (id) ON DELETE CASCADE,
    sku_id       uuid NOT NULL REFERENCES sku (id),
    cell_id      uuid REFERENCES cell (id),
    box_id       uuid REFERENCES box (id),
    expected_qty integer,
    fact_qty     integer CHECK (fact_qty IS NULL OR fact_qty >= 0)
);

CREATE INDEX inventory_count_line_count_idx ON inventory_count_line (count_id);
