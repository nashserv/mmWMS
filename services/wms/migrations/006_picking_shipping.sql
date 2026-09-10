-- Поток 0. Подбор, отгрузка, возвраты (раздел 7).

-- Сессия подбора многопродавцовая: один обход собирает задания нескольких
-- клиентов (раздел 4), поэтому владелец живёт на строке, а не на сессии.
CREATE TABLE pick_session (
    id                uuid PRIMARY KEY,
    actor_id          uuid NOT NULL,
    station_id        uuid REFERENCES station (id),
    state             text NOT NULL DEFAULT 'open'
                          CHECK (state IN ('open', 'picking', 'completed', 'cancelled')),
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    -- Штрихкод бумажного листа подбора: сборщик идёт по складу с листом,
    -- и по этому коду лист возвращается к своей сессии (раздел 4).
    picklist_barcode  text UNIQUE
);

CREATE INDEX pick_session_actor_idx ON pick_session (actor_id, state);

CREATE TABLE pick_line (
    id          uuid PRIMARY KEY,
    session_id  uuid NOT NULL REFERENCES pick_session (id) ON DELETE CASCADE,
    task_id     uuid NOT NULL REFERENCES wms_task (id),
    owner_id    uuid NOT NULL REFERENCES owner (id),
    sku_id      uuid NOT NULL,
    cell_id     uuid REFERENCES cell (id),
    box_id      uuid,
    qty         integer NOT NULL CHECK (qty > 0),
    scanned_at  timestamptz,
    -- Главный рубеж качества — контрольный скан (раздел 4). Отклонённый скан
    -- обязан сохраниться: по нему видно, что именно человек взял не то.
    scan_result text CHECK (scan_result IS NULL OR scan_result IN (
                    'ok', 'wrong_barcode', 'wrong_owner', 'not_found', 'short')),
    CONSTRAINT pick_line_sku_matches_owner
        FOREIGN KEY (sku_id, owner_id) REFERENCES sku (id, owner_id),
    CONSTRAINT pick_line_box_matches_owner
        FOREIGN KEY (box_id, owner_id) REFERENCES box (id, owner_id),
    -- Изоляция владельца доходит до строки листа подбора (инвариант 6).
    CONSTRAINT pick_line_task_matches_owner
        FOREIGN KEY (task_id, owner_id) REFERENCES wms_task (id, owner_id),
    CONSTRAINT pick_line_scan_is_timed
        CHECK ((scan_result IS NULL) = (scanned_at IS NULL))
);

-- Задание не должно попасть в две сессии подбора одновременно.
CREATE UNIQUE INDEX pick_line_one_task_per_open_session_idx ON pick_line (task_id);

CREATE INDEX pick_line_session_idx ON pick_line (session_id);

CREATE TABLE shipment (
    id           uuid PRIMARY KEY,
    owner_id     uuid NOT NULL REFERENCES owner (id),
    wb_supply_id uuid REFERENCES wb_supply (id),
    state        text NOT NULL DEFAULT 'open' CHECK (state IN (
                     'open', 'closed', 'handed_to_wb', 'accepted_by_wb', 'cancelled')),
    closed_at    timestamptz,
    -- HANDED_TO_WB подтверждается человеком, ACCEPTED_BY_WB — только сверкой
    -- с WB (раздел 2.12). Раздел 7 этих полей не перечисляет, но без них
    -- нечем измерить критерий раздела 10 «подтверждён человеком > 95 %» и
    -- нечем выполнить assert шага 11 прогона. См. docs/schema-decisions.md.
    handed_by    uuid,
    handed_at    timestamptz,
    accepted_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT shipment_handover_is_confirmed_by_a_human
        CHECK (state <> 'handed_to_wb' OR (handed_by IS NOT NULL AND handed_at IS NOT NULL)),
    -- «Передано» не доказательство приёмки: приёмку подтверждает только сверка.
    CONSTRAINT shipment_acceptance_is_reconciled
        CHECK (state <> 'accepted_by_wb' OR accepted_at IS NOT NULL)
);

CREATE INDEX shipment_owner_state_idx ON shipment (owner_id, state);

CREATE TABLE wms_return (
    id              uuid PRIMARY KEY,
    task_id         uuid REFERENCES wms_task (id),
    -- Ключ идемпотентности возврата (инвариант 5, приложение C).
    return_event_id text UNIQUE NOT NULL,
    owner_id        uuid NOT NULL REFERENCES owner (id),
    state           text NOT NULL DEFAULT 'expected'
                        CHECK (state IN ('expected', 'received', 'decided')),
    decision        text CHECK (decision IS NULL OR decision IN ('resellable', 'defective')),
    reason          text,
    received_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT wms_return_decision_is_final
        CHECK (state <> 'decided' OR decision IS NOT NULL),
    CONSTRAINT wms_return_received_has_time
        CHECK (state = 'expected' OR received_at IS NOT NULL)
);

CREATE INDEX wms_return_owner_state_idx ON wms_return (owner_id, state);
