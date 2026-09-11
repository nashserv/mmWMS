-- Этап 2 аудита: журнал команд, недостающие ключи и индексы.
--
-- Три разные вещи в одной миграции, потому что они про одно: команда,
-- выполненная дважды, обязана выглядеть как выполненная один раз.

-- 1. Журнал команд с внешним ключом идемпотентности (инвариант 5).
--
-- До него ключ только проверялся на непустоту: повтор `deliver` заводил
-- вторую поставку и второе тарифицируемое событие `wb.supply.shipped.v1` —
-- то есть второй счёт клиенту за ту же машину.
CREATE TABLE IF NOT EXISTS command_log (
    idempotency_key text PRIMARY KEY,
    command         text NOT NULL,
    aggregate_id    uuid,
    -- Ответ первой попытки. Повтор получает его дословно, с duplicate: true.
    result          jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS command_log_created_idx ON command_log (created_at);
CREATE INDEX IF NOT EXISTS command_log_aggregate_idx ON command_log (command, aggregate_id);

-- 2. Возврат на полку — законный исход скана.
--
-- Сборщик взял вещь и положил обратно: это не 'ok' и не ошибка штрихкода.
-- Без своего значения факт терялся, а он и есть сигнал «на этой полке
-- спотыкаются».
ALTER TABLE pick_line DROP CONSTRAINT IF EXISTS pick_line_scan_result_check;
ALTER TABLE pick_line ADD CONSTRAINT pick_line_scan_result_check
    CHECK (scan_result IS NULL OR scan_result IN (
        'ok', 'wrong_barcode', 'wrong_owner', 'not_found', 'short', 'returned'));

-- 3. Номер документа уникален в пределах владельца, а не глобально.
--
-- `reference` искался по всей таблице: приёмка клиента A с номером «ТН-1»
-- отдавалась клиенту B как его собственная — вместе с чужими строками.
-- Изоляция владельца доходит до номера документа (инвариант 6).
ALTER TABLE receipt DROP CONSTRAINT IF EXISTS receipt_reference_key;
CREATE UNIQUE INDEX IF NOT EXISTS receipt_owner_reference_idx
    ON receipt (owner_id, reference);

ALTER TABLE inventory_count DROP CONSTRAINT IF EXISTS inventory_count_reference_key;
CREATE UNIQUE INDEX IF NOT EXISTS inventory_count_owner_reference_idx
    ON inventory_count (owner_id, reference);

-- 4. Поставка Wildberries — одна отгрузка, а не сколько получилось.
--
-- `ensure_shipment` искал отгрузку по поставке и заводил новую, если не нашёл.
-- Два одновременных `deliver` по одной поставке заводили две отгрузки и два
-- счёта клиенту. Ключ снимает это на уровне схемы, а не дисциплины.
CREATE UNIQUE INDEX IF NOT EXISTS shipment_one_per_supply_idx
    ON shipment (wb_supply_id) WHERE wb_supply_id IS NOT NULL;

-- 5. Индексы под запросы, которые уже есть в коде.
--
-- Без них планировщик читает таблицу целиком: на стенде это незаметно, на
-- шести тысячах заданий боевого контура — нет.

-- Сборка поставки: «что лежит в этой поставке».
CREATE INDEX IF NOT EXISTS wms_task_supply_idx
    ON wms_task (supply_id) WHERE supply_id IS NOT NULL;

-- Очередь сборщика: состояние плюс срок Wildberries (раздел 7).
CREATE INDEX IF NOT EXISTS wms_task_state_deadline_idx
    ON wms_task (state, deadline NULLS LAST);

-- «Что лежит в этой коробке» — запрос экрана и проверки перед снятием коробки.
CREATE INDEX IF NOT EXISTS stock_balance_box_idx
    ON stock_balance (box_id) WHERE box_id IS NOT NULL;

-- Экран расхождений читает их по приёмке и по заданию.
CREATE INDEX IF NOT EXISTS discrepancy_receipt_idx
    ON discrepancy (receipt_id) WHERE receipt_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS discrepancy_task_idx
    ON discrepancy (task_id) WHERE task_id IS NOT NULL;

-- 6. Когда стикер напечатали впервые.
--
-- `wms.label.attached.v1` уходило при КАЖДОЙ печати: пять перепечаток из-за
-- зажёванной ленты давали пять событий «этикетка наклеена», и потребитель
-- события считал по ним наклейки. Наклейка одна, печатей сколько угодно.
ALTER TABLE wb_label ADD COLUMN IF NOT EXISTS printed_at timestamptz;
ALTER TABLE wb_label ADD COLUMN IF NOT EXISTS prints integer NOT NULL DEFAULT 0;
