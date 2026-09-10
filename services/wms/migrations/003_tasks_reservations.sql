-- Поток 0. Задание, резерв, локальный стикер (раздел 7).
--
-- wb_label по мастеру относится к блоку интеграции с WB, но создаётся здесь:
-- у него внешний ключ на wms_task, а задание появляется только в этой миграции.
--
-- Задание и резерв связаны в обе стороны (wms_task.reservation_id ↔
-- reservation.task_id). Циклическую пару разводим в конце файла отдельными
-- ALTER TABLE — иначе ни одну из таблиц не создать первой.

CREATE TABLE wms_task (
    id                   uuid PRIMARY KEY,
    -- Ключ идемпотентности опроса WB: повторный опрос того же заказа —
    -- ON CONFLICT DO NOTHING, а не второе задание (инвариант 5, раздел 6.2).
    wb_order_id          bigint UNIQUE NOT NULL,
    wb_order_uid         text,
    wb_account_id        uuid NOT NULL,
    owner_id             uuid NOT NULL REFERENCES owner (id),
    sku_id               uuid,
    barcode              text,
    quantity             integer NOT NULL CHECK (quantity > 0),
    -- Срок WB. Выдача сборщикам сортируется по нему, а не по времени создания
    -- (раздел 7): просроченное задание дороже свежего.
    deadline             timestamptz,
    -- Автомат раздела 7 обрывается на shipped, но раздел 2.12 и диагностика
    -- раздела 3 считают HANDED_TO_WB и ACCEPTED_BY_WB именно по заданиям
    -- (55 и 3274 штуки). Без этих двух состояний нечем выразить «передано
    -- человеком» и «подтверждено сверкой», а это разные факты: complete у WB
    -- не доказательство приёмки. См. docs/schema-decisions.md.
    state                text NOT NULL DEFAULT 'new' CHECK (state IN (
                             'new', 'reserved', 'picking', 'picked', 'packed',
                             'labeled', 'in_supply', 'shipped', 'handed', 'accepted',
                             'short', 'cancelled', 'manual_review', 'diverged')),
    wb_status            text CHECK (wb_status IS NULL OR wb_status IN ('new', 'confirm', 'complete', 'cancel')),
    reservation_id       uuid,
    label_id             uuid,
    package_ref          text,
    supply_id            uuid REFERENCES wb_supply (id),
    -- Выдача сборщику с лизингом: задание освобождается, если сборщик пропал.
    assignee             uuid,
    claimed_at           timestamptz,
    claim_expires_at     timestamptz,
    -- Инвариант 11: причина отмены обязательна на уровне схемы, а не дисциплины.
    -- В боевом контуре у всех 2645 отмен причина была NULL (раздел 3).
    cancel_reason        text,
    -- Коды из раздела 3.2: PRODUCT_MAPPING_MISSING, AMBIGUOUS_PRODUCT_MAPPING,
    -- SELLER_MAPPING_MISSING. Немаппленный товар обязан попадать сюда, а не
    -- утекать в тихую отмену.
    manual_review_code   text,
    manual_review_reason text,
    last_reconciled_at   timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    version              integer NOT NULL DEFAULT 1,
    UNIQUE (id, owner_id),
    CONSTRAINT wms_task_account_matches_owner
        FOREIGN KEY (wb_account_id, owner_id) REFERENCES wb_account (id, owner_id),
    -- Изоляция владельца (инвариант 6): сослаться на SKU чужого владельца нельзя.
    CONSTRAINT wms_task_sku_matches_owner
        FOREIGN KEY (sku_id, owner_id) REFERENCES sku (id, owner_id),
    CONSTRAINT wms_task_cancel_reason_required
        CHECK (state <> 'cancelled' OR cancel_reason IS NOT NULL),
    CONSTRAINT wms_task_manual_review_code_required
        CHECK (state <> 'manual_review' OR manual_review_code IS NOT NULL),
    -- Задание без маппинга не имеет SKU — именно поэтому оно в manual_review.
    -- Во всех остальных состояниях SKU обязан быть известен.
    CONSTRAINT wms_task_sku_known_unless_manual_review
        CHECK (sku_id IS NOT NULL OR state IN ('new', 'manual_review', 'cancelled')),
    -- Выданное задание всегда имеет срок лизинга, иначе оно зависнет за сборщиком навсегда.
    CONSTRAINT wms_task_claim_is_leased
        CHECK (assignee IS NULL OR (claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL))
);

-- Очередь выдачи сборщикам: SELECT ... WHERE state='reserved' AND assignee IS NULL
-- ORDER BY deadline FOR UPDATE SKIP LOCKED (раздел 7). Частичный индекс держит
-- горячую выборку короткой при 10 000 заданий в час.
CREATE INDEX wms_task_pull_queue_idx ON wms_task (deadline)
    WHERE state = 'reserved' AND assignee IS NULL;

-- Возврат зависших лизингов.
CREATE INDEX wms_task_expired_claim_idx ON wms_task (claim_expires_at)
    WHERE assignee IS NOT NULL;

CREATE INDEX wms_task_owner_state_idx ON wms_task (owner_id, state);
CREATE INDEX wms_task_account_idx ON wms_task (wb_account_id, state);

-- Расхождение с WB — состояние, которое будит человека (инвариант 10).
CREATE INDEX wms_task_diverged_idx ON wms_task (updated_at) WHERE state = 'diverged';

CREATE TABLE reservation (
    id             uuid PRIMARY KEY,
    task_id        uuid NOT NULL REFERENCES wms_task (id),
    owner_id       uuid NOT NULL REFERENCES owner (id),
    sku_id         uuid NOT NULL,
    cell_id        uuid REFERENCES cell (id),
    box_id         uuid,
    qty            integer NOT NULL CHECK (qty > 0),
    state          text NOT NULL DEFAULT 'held' CHECK (state IN ('held', 'consumed', 'released')),
    expires_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    released_at    timestamptz,
    release_reason text,
    CONSTRAINT reservation_sku_matches_owner
        FOREIGN KEY (sku_id, owner_id) REFERENCES sku (id, owner_id),
    CONSTRAINT reservation_box_matches_owner
        FOREIGN KEY (box_id, owner_id) REFERENCES box (id, owner_id),
    CONSTRAINT reservation_task_matches_owner
        FOREIGN KEY (task_id, owner_id) REFERENCES wms_task (id, owner_id),
    -- Снятый резерв обязан объяснить, почему он снят: это вход в разбор отмен.
    CONSTRAINT reservation_release_is_explained
        CHECK (state <> 'released' OR (released_at IS NOT NULL AND release_reason IS NOT NULL))
);

-- На задание живёт ровно один действующий резерв.
CREATE UNIQUE INDEX reservation_one_held_per_task_idx
    ON reservation (task_id) WHERE state = 'held';

CREATE INDEX reservation_stock_key_idx ON reservation (owner_id, sku_id, cell_id, box_id)
    WHERE state = 'held';

-- Стикер лежит локально ДО того, как человек нажал печать (инвариант 9).
-- Тянется заранее, пачкой до 100 штук, сразу после резерва (раздел 6.6).
CREATE TABLE wb_label (
    id             uuid PRIMARY KEY,
    task_id        uuid UNIQUE NOT NULL REFERENCES wms_task (id),
    -- Целевое значение zplv: 1–3 КБ текста против 20–100 КБ картинки, принтер
    -- печатает нативно без растеризации. png оставлен как откат, если реальный
    -- принтер не распознает ZPL (раздел 13, вопрос 2 — ещё открыт).
    format         text NOT NULL DEFAULT 'zplv' CHECK (format IN ('png', 'svg', 'zplv', 'zplh')),
    payload        bytea NOT NULL,
    checksum       text NOT NULL,
    version        integer NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 2147483647),
    fetched_at     timestamptz NOT NULL DEFAULT now(),
    -- Отмена задания после выдачи стикера: заказ освобождается из поставки,
    -- локальный стикер помечается недействительным (раздел 6.6).
    invalidated_at timestamptz,
    CONSTRAINT wb_label_payload_limit CHECK (length(payload) <= 10 * 1024 * 1024),
    CONSTRAINT wb_label_checksum_is_sha256 CHECK (checksum ~ '^[0-9a-f]{64}$')
);

CREATE INDEX wb_label_valid_idx ON wb_label (task_id) WHERE invalidated_at IS NULL;

-- Развязка циклических ссылок: обе таблицы уже существуют.
ALTER TABLE wms_task
    ADD CONSTRAINT wms_task_reservation_fk FOREIGN KEY (reservation_id) REFERENCES reservation (id),
    ADD CONSTRAINT wms_task_label_fk FOREIGN KEY (label_id) REFERENCES wb_label (id);

-- updated_at ведёт база, а не дисциплина вызывающего кода: на него смотрят
-- метрики возраста задания и алерт «воркер молчит» (инвариант 14).
CREATE FUNCTION wms_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER wms_task_touch_updated_at
    BEFORE UPDATE ON wms_task
    FOR EACH ROW EXECUTE FUNCTION wms_touch_updated_at();
