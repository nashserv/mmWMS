-- Поток B. База рабочего места сборщика и приёмщика.
--
-- Здесь НЕТ копии заданий wms. Проекция экрана живёт в памяти и
-- восстанавливается одним опросом `/tasks/pull` (раздел 6.1 мастера): вторая
-- копия чужого состояния однажды разойдётся с первой — ровно так и вышло, что
-- на экране одно, в шлюзе другое, а в Odoo третье.
--
-- Хранится только то, что рабочее место знает само и о чём больше никто не
-- узнает, если это не записать: кто, когда, чем и по какой причине.

-- Сессия подбора глазами рабочего места. Настоящая сессия живёт в wms
-- (`pick_session` миграции 006); здесь — то, что нужно бумажному листу:
-- штрихкод, состав и время, чтобы лист, найденный на складе через час, можно
-- было вернуть к своей сессии.
CREATE TABLE workstation_pick_session (
    id               uuid PRIMARY KEY,
    actor_id         text NOT NULL,
    station_id       uuid,
    state            text NOT NULL DEFAULT 'open'
                         CHECK (state IN ('open', 'picking', 'completed', 'cancelled')),
    -- Штрихкод листа: физический лист привязывается к сессии в базе
    -- (раздел «Лист подбора» файла 03).
    picklist_barcode text UNIQUE NOT NULL,
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    CONSTRAINT workstation_pick_session_finished_when_done
        CHECK ((state IN ('completed', 'cancelled')) = (finished_at IS NOT NULL))
);

CREATE INDEX workstation_pick_session_actor_idx
    ON workstation_pick_session (actor_id, state);

-- Строка листа. Адрес и коробка — обязательная часть строки, а не украшение:
-- сборщик должен видеть, куда идти, а не искать глазами.
CREATE TABLE workstation_pick_line (
    id                 uuid PRIMARY KEY,
    session_id         uuid NOT NULL REFERENCES workstation_pick_session (id) ON DELETE CASCADE,
    task_id            text NOT NULL,
    owner_external_id  text NOT NULL,
    barcode            text NOT NULL,
    name               text,
    quantity           integer NOT NULL CHECK (quantity > 0),
    cell_address       text,
    box_barcode        text,
    -- Порядок обхода: лист печатается змейкой по стеллажам, а не по порядку
    -- заказов.
    route_order        integer,
    scanned_at         timestamptz,
    scan_result        text CHECK (scan_result IS NULL OR scan_result IN (
                           'ok', 'wrong_barcode', 'wrong_owner', 'not_found', 'short')),
    UNIQUE (session_id, task_id),
    -- Скан без времени — это тот самый «подбор», которого не было: в боевом
    -- контуре 1 запись сессии на 6497 заданий (раздел 3 мастера).
    CONSTRAINT workstation_pick_line_scan_is_timed
        CHECK ((scan_result IS NULL) = (scanned_at IS NULL))
);

CREATE INDEX workstation_pick_line_task_idx ON workstation_pick_line (task_id);

-- Журнал сканов целиком, включая отклонённые. Отклонённый скан обязан
-- сохраниться: по нему видно, что именно человек взял не то, и без него
-- контрольный скан не рубеж качества, а формальность.
CREATE TABLE workstation_scan (
    id          uuid PRIMARY KEY,
    task_id     text NOT NULL,
    -- У стойки и при упаковке — разные сканы с разной ценой ошибки.
    stage       text NOT NULL CHECK (stage IN ('rack', 'control')),
    barcode     text NOT NULL,
    scan_result text NOT NULL CHECK (scan_result IN (
                    'ok', 'wrong_barcode', 'wrong_owner', 'not_found', 'short')),
    accepted    boolean NOT NULL,
    actor_id    text,
    station_id  uuid,
    session_id  uuid REFERENCES workstation_pick_session (id) ON DELETE SET NULL,
    scanned_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX workstation_scan_task_idx ON workstation_scan (task_id, scanned_at DESC);
CREATE INDEX workstation_scan_rejected_idx
    ON workstation_scan (scanned_at DESC) WHERE NOT accepted;

-- Печать. Ключ идемпотентности — тот же, что уехал в wms: повторное нажатие
-- при зависшем экране не должно печатать вторую этикетку.
CREATE TABLE workstation_print_job (
    id                uuid PRIMARY KEY,
    idempotency_key   text UNIQUE NOT NULL,
    task_id           text NOT NULL,
    station_id        uuid NOT NULL,
    label_format      text NOT NULL,
    checksum          text NOT NULL,
    payload_bytes     integer NOT NULL CHECK (payload_bytes >= 0),
    copies            integer NOT NULL DEFAULT 1 CHECK (copies BETWEEN 1 AND 10),
    reprint           boolean NOT NULL DEFAULT false,
    -- Причина обязательна при перепечатке: доля перепечаток — метрика
    -- качества этикетки и принтера, и без причины её нечем разбирать.
    reason            text,
    outcome           text NOT NULL DEFAULT 'queued'
                          CHECK (outcome IN ('queued', 'sent', 'written', 'failed')),
    error             text,
    actor_id          text,
    requested_at      timestamptz NOT NULL DEFAULT now(),
    sent_at           timestamptz,
    written_at        timestamptz,
    -- Две половины бюджета раздела 10: до агента и до записи в устройство.
    click_to_agent_ms numeric(10, 3),
    agent_write_ms    numeric(10, 3),
    CONSTRAINT workstation_print_job_reprint_has_reason
        CHECK (NOT reprint OR (reason IS NOT NULL AND btrim(reason) <> ''))
);

CREATE INDEX workstation_print_job_task_idx ON workstation_print_job (task_id, requested_at DESC);
CREATE INDEX workstation_print_job_station_idx
    ON workstation_print_job (station_id, requested_at DESC);

-- Что рабочее место сделало с заданием само. Это не копия состояния wms, а
-- журнал собственных действий: кто упаковал, на какой станции напечатал, по
-- какой причине отменил.
CREATE TABLE workstation_task (
    task_id            text PRIMARY KEY,
    owner_external_id  text NOT NULL,
    barcode            text NOT NULL,
    screen_status      text NOT NULL,
    session_id         uuid REFERENCES workstation_pick_session (id) ON DELETE SET NULL,
    station_id         uuid,
    actor_id           text,
    picked_at          timestamptz,
    packed_at          timestamptz,
    printed_at         timestamptz,
    handed_at          timestamptz,
    cancelled_at       timestamptz,
    -- Инвариант 11 мастера. В боевом контуре у всех 2645 отмен причина была
    -- NULL, и разобрать их задним числом стало нечем. Здесь база не примет
    -- отмену без причины — во всех местах кода сразу, а не там, где вспомнили.
    cancel_reason      text,
    cancel_reason_code text,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT workstation_task_cancel_has_reason CHECK (
        cancelled_at IS NULL
        OR (cancel_reason IS NOT NULL AND btrim(cancel_reason) <> ''
            AND cancel_reason_code IS NOT NULL AND btrim(cancel_reason_code) <> ''))
);

CREATE INDEX workstation_task_session_idx ON workstation_task (session_id);

-- Принтер рабочего места. Таблицу `station` ведёт wms (раздел 7 мастера);
-- здесь лежит то, что знает только сама станция: имя принтера в её ОС, живо
-- ли соединение с агентом и — главное — какой формат этикетки этот принтер
-- реально понял.
--
-- Вопрос 2 раздела 13 мастера открыт: XP-420B заявляет эмуляцию ZPL, но
-- встречаются отчёты, что она начинается только с XP-460B. Поэтому формат не
-- записан в конфиг как факт, а проверяется на живом принтере и хранится
-- вместе с датой проверки.
CREATE TABLE workstation_printer (
    station_id       uuid PRIMARY KEY,
    station_name     text UNIQUE NOT NULL,
    printer_name     text,
    transport        text NOT NULL DEFAULT 'agent' CHECK (transport IN ('agent', 'tcp')),
    active           boolean NOT NULL DEFAULT true,
    confirmed_format text CHECK (confirmed_format IS NULL
                                 OR confirmed_format IN ('zplv', 'zplh', 'tspl', 'png')),
    probed_at        timestamptz,
    probe_note       text,
    last_seen_at     timestamptz,
    CONSTRAINT workstation_printer_probe_is_dated
        CHECK ((confirmed_format IS NULL) = (probed_at IS NULL))
);
