-- Поток 0. Журнал остатков — единственный источник истины (раздел 7, инвариант 3).
--
-- Здесь три триггера, и все три существуют потому, что дисциплина кода уже один раз
-- не удержала инвариант: в боевом контуре резерв дописывался молча (раздел 3.1).
--   1. stock_move нельзя менять и удалять — только дописывать.
--   2. stock_balance пересчитывается проекцией из stock_move, автоматически.
--   3. stock_balance нельзя писать напрямую — только через проекцию.

CREATE TABLE stock_move (
    -- bigint identity, а не uuid: журнал append-only и читается по порядку,
    -- монотонный ключ здесь и есть порядок записи.
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    owner_id   uuid NOT NULL REFERENCES owner (id),
    sku_id     uuid NOT NULL,
    cell_from  uuid REFERENCES cell (id),
    cell_to    uuid REFERENCES cell (id),
    box_from   uuid REFERENCES box (id),
    box_to     uuid REFERENCES box (id),
    -- NULL со стороны from — товар пришёл извне (приёмка);
    -- NULL со стороны to — товар ушёл со склада (отгрузка).
    state_from wms_stock_state,
    state_to   wms_stock_state,
    qty        integer NOT NULL CHECK (qty > 0),
    -- Зачем произошло движение. 'ledger_short' — клапан «собрать без остатка»
    -- (раздел 6.5): именно он делает молчаливую дописку видимой.
    reason     text NOT NULL,
    doc_type   text NOT NULL CHECK (doc_type IN (
                   'opening', 'receipt', 'putaway', 'move', 'reservation',
                   'pick', 'pack', 'shipment', 'inventory', 'return', 'adjustment')),
    doc_ref    text,
    actor_id   uuid,
    -- Инвариант 5: повтор команды — тот же ответ, а не второе движение.
    idem_key   text UNIQUE NOT NULL,
    CONSTRAINT stock_move_has_a_side CHECK (state_from IS NOT NULL OR state_to IS NOT NULL),
    -- Всякий товар в состоянии лежит в какой-то ячейке: состояние без адреса —
    -- это остаток, который потом никто не найдёт на складе.
    CONSTRAINT stock_move_from_is_addressed CHECK ((state_from IS NULL) = (cell_from IS NULL)),
    CONSTRAINT stock_move_to_is_addressed CHECK ((state_to IS NULL) = (cell_to IS NULL)),
    CONSTRAINT stock_move_box_from_needs_cell CHECK (box_from IS NULL OR cell_from IS NOT NULL),
    CONSTRAINT stock_move_box_to_needs_cell CHECK (box_to IS NULL OR cell_to IS NOT NULL),
    CONSTRAINT stock_move_sku_matches_owner
        FOREIGN KEY (sku_id, owner_id) REFERENCES sku (id, owner_id)
);

CREATE INDEX stock_move_ts_idx ON stock_move (ts);
CREATE INDEX stock_move_owner_sku_idx ON stock_move (owner_id, sku_id, ts DESC);
CREATE INDEX stock_move_doc_idx ON stock_move (doc_type, doc_ref);
-- Разбор клапана «собрать без остатка»: экран начальника склада читает отсюда.
CREATE INDEX stock_move_ledger_short_idx ON stock_move (ts DESC) WHERE reason = 'ledger_short';

-- Баланс — проекция, а не самостоятельная запись.
--
-- Мастер описывает ключ как PRIMARY KEY(owner_id, sku_id, cell_id, box_id, state).
-- Первичным ключом это быть не может: box_id допускает NULL (товар лежит в ячейке
-- вне коробки — например, принят, но ещё не разложен), а колонки PK обязаны быть
-- NOT NULL. Поэтому тот же самый ключ выражен через UNIQUE NULLS NOT DISTINCT
-- (Postgres 15+): NULL-коробка считается одним и тем же значением, и на строку
-- по-прежнему приходится ровно один баланс. См. docs/schema-decisions.md.
CREATE TABLE stock_balance (
    owner_id uuid NOT NULL REFERENCES owner (id),
    sku_id   uuid NOT NULL,
    cell_id  uuid NOT NULL REFERENCES cell (id),
    box_id   uuid,
    state    wms_stock_state NOT NULL,
    -- Отрицательный остаток разрешён СОЗНАТЕЛЬНО. Клапан «собрать без остатка»
    -- (раздел 6.5) резервирует товар, которого по учёту нет, — и тогда good
    -- уходит в минус. Запретить минус значило бы либо уронить смену, либо
    -- вернуться к молчаливой дописке. Минус виден, считается и разбирается.
    qty      integer NOT NULL,
    CONSTRAINT stock_balance_key UNIQUE NULLS NOT DISTINCT (owner_id, sku_id, cell_id, box_id, state),
    CONSTRAINT stock_balance_sku_matches_owner
        FOREIGN KEY (sku_id, owner_id) REFERENCES sku (id, owner_id),
    CONSTRAINT stock_balance_box_matches_owner
        FOREIGN KEY (box_id, owner_id) REFERENCES box (id, owner_id)
);

-- Публикация остатка в WB: available = good - reserved - buffer (инвариант 7).
CREATE INDEX stock_balance_owner_sku_idx ON stock_balance (owner_id, sku_id, state);
-- Отрицательный остаток — рабочая очередь инвентаризации, а не аварийный случай.
CREATE INDEX stock_balance_negative_idx ON stock_balance (owner_id, sku_id) WHERE qty < 0;

-- 1. Журнал только дописывается.
CREATE FUNCTION wms_stock_move_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'stock_move is append-only: % is forbidden (инвариант 3)', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER stock_move_no_rewrite
    BEFORE UPDATE OR DELETE ON stock_move
    FOR EACH ROW EXECUTE FUNCTION wms_stock_move_append_only();

CREATE TRIGGER stock_move_no_truncate
    BEFORE TRUNCATE ON stock_move
    FOR EACH STATEMENT EXECUTE FUNCTION wms_stock_move_append_only();

-- 2. Проекция движения в баланс. Живёт в базе, а не в приложении, чтобы
-- «забыть пересчитать баланс» стало технически невозможно.
CREATE FUNCTION wms_stock_move_project() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state_from IS NOT NULL THEN
        INSERT INTO stock_balance AS balance (owner_id, sku_id, cell_id, box_id, state, qty)
        VALUES (NEW.owner_id, NEW.sku_id, NEW.cell_from, NEW.box_from, NEW.state_from, -NEW.qty)
        ON CONFLICT ON CONSTRAINT stock_balance_key
        DO UPDATE SET qty = balance.qty - NEW.qty;
    END IF;

    IF NEW.state_to IS NOT NULL THEN
        INSERT INTO stock_balance AS balance (owner_id, sku_id, cell_id, box_id, state, qty)
        VALUES (NEW.owner_id, NEW.sku_id, NEW.cell_to, NEW.box_to, NEW.state_to, NEW.qty)
        ON CONFLICT ON CONSTRAINT stock_balance_key
        DO UPDATE SET qty = balance.qty + NEW.qty;
    END IF;

    RETURN NULL;
END;
$$;

CREATE TRIGGER stock_move_project_balance
    AFTER INSERT ON stock_move
    FOR EACH ROW EXECUTE FUNCTION wms_stock_move_project();

-- 3. Прямая запись в баланс запрещена.
-- pg_trigger_depth() = 1 означает «нас позвали из обычного запроса клиента»;
-- проекция выше работает на глубине 2, потому что сама вызвана триггером.
CREATE FUNCTION wms_stock_balance_projection_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION
            'stock_balance is a projection of stock_move: пишите движение, не баланс (инвариант 3)'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER stock_balance_projection_only
    BEFORE INSERT OR UPDATE OR DELETE ON stock_balance
    FOR EACH ROW EXECUTE FUNCTION wms_stock_balance_projection_only();
