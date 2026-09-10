-- Поток 0. Справочники и домены базы wms.
-- Источник: раздел 7 мастер-контекста. Схема заморожена: правки только потоком 0
-- с одновременным обновлением потоков A, B, C (правило 9.5.2).
--
-- Принятые здесь конкретизации мастера перечислены в docs/schema-decisions.md —
-- читать вместе с этим файлом.

-- Состояния товара (раздел 7). Домен, а не enum-тип: список заморожен контрактом,
-- но добавить значение в CHECK дешевле, чем в pg enum, а перечислять его в каждой
-- из трёх таблиц журнала — значит однажды разойтись.
CREATE DOMAIN wms_stock_state AS text
    CHECK (VALUE IN ('good', 'reserved', 'quarantine', 'defect', 'blocked', 'processing'));

-- Владелец товара — первый класс. Весь склад чужой (раздел 1), поэтому owner_id
-- обязателен везде и вынесен во внешние ключи: перепутанный владелец — это
-- отгрузка чужой вещи, а не строчка в отчёте.
CREATE TABLE owner (
    id                 uuid PRIMARY KEY,
    seller_external_id text UNIQUE NOT NULL,
    name               text NOT NULL,
    inn                text,
    active             boolean NOT NULL DEFAULT true,
    -- Клапан «собрать без остатка» (раздел 6.5): выключается по владельцу, не глобально.
    allow_ledger_short boolean NOT NULL DEFAULT true,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sku (
    id         uuid PRIMARY KEY,
    owner_id   uuid NOT NULL REFERENCES owner (id),
    seller_sku text,
    -- У Wildberries поле называется sku, но приходит в нём штрихкод (раздел 3.2).
    -- Штрихкод определяет вещь на полке, артикул — только модель.
    barcode    text NOT NULL,
    name       text,
    dims_mm    jsonb,
    weight_g   integer,
    -- Страховой запас: в WB публикуется good - reserved - buffer (инвариант 7).
    buffer     integer NOT NULL DEFAULT 0 CHECK (buffer >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_id, barcode),
    -- Ключ для составных внешних ключей: не даёт сослаться на SKU чужого владельца.
    UNIQUE (id, owner_id),
    CONSTRAINT sku_dims_shape CHECK (
        dims_mm IS NULL
        OR (dims_mm ? 'l' AND dims_mm ? 'w' AND dims_mm ? 'h')
    ),
    CONSTRAINT sku_weight_positive CHECK (weight_g IS NULL OR weight_g > 0)
);

CREATE INDEX sku_barcode_idx ON sku (barcode);

CREATE TABLE warehouse (
    id   uuid PRIMARY KEY,
    code text UNIQUE NOT NULL
);

CREATE TABLE zone (
    id           uuid PRIMARY KEY,
    warehouse_id uuid NOT NULL REFERENCES warehouse (id),
    code         text NOT NULL,
    kind         text NOT NULL CHECK (kind IN (
                     'receiving', 'storage', 'picking', 'packing',
                     'shipping', 'quarantine', 'defect')),
    UNIQUE (warehouse_id, code)
);

CREATE TABLE cell (
    id          uuid PRIMARY KEY,
    zone_id     uuid NOT NULL REFERENCES zone (id),
    address     text UNIQUE NOT NULL,
    capacity    integer CHECK (capacity IS NULL OR capacity > 0),
    -- Порядок обхода склада сборщиком. Заполняется вручную (раздел 13, вопрос 1
    -- открыт): лист подбора сортируется по нему, иначе сборщик ходит зигзагом.
    route_order integer,
    active      boolean NOT NULL DEFAULT true
);

CREATE INDEX cell_route_order_idx ON cell (route_order) WHERE active;

-- Коробка — фактическая единица адресации на складе (раздел 2.9). Коробка стоит
-- в ячейке; ячейка — адрес, коробка — контейнер.
CREATE TABLE box (
    id           uuid PRIMARY KEY,
    barcode      text UNIQUE NOT NULL,
    owner_id     uuid NOT NULL REFERENCES owner (id),
    sku_id       uuid REFERENCES sku (id),
    cell_id      uuid REFERENCES cell (id),
    -- 0 означает «не считали», а не «пусто» (раздел 2.9). Отличать по counted.
    quantity     integer NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    counted      boolean NOT NULL DEFAULT false,
    -- Обязателен по требованию склада: «через месяц стоят сотни одинаковых коробок».
    comment      text NOT NULL CHECK (length(btrim(comment)) > 0),
    sequence     integer,
    total_boxes  integer,
    state        text NOT NULL DEFAULT 'stored' CHECK (state IN ('stored', 'removed')),
    created_by   uuid,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, owner_id),
    CONSTRAINT box_owner_matches_sku FOREIGN KEY (sku_id, owner_id) REFERENCES sku (id, owner_id),
    CONSTRAINT box_sequence_within_total CHECK (
        sequence IS NULL OR total_boxes IS NULL OR (sequence >= 1 AND sequence <= total_boxes)
    )
);

CREATE INDEX box_cell_idx ON box (cell_id) WHERE state = 'stored';
CREATE INDEX box_owner_sku_idx ON box (owner_id, sku_id) WHERE state = 'stored';

-- Рабочее место: ПК плюс свой принтер этикеток (раздел 4). Пять станций,
-- пять принтеров, очередь печати не нужна.
CREATE TABLE station (
    id            uuid PRIMARY KEY,
    name          text UNIQUE NOT NULL,
    printer_host  text,
    printer_port  integer NOT NULL DEFAULT 9100 CHECK (printer_port BETWEEN 1 AND 65535),
    printer_model text,
    -- Сегодня принтеры USB, поэтому 'agent' (решение владельца 14). 'tcp' оставлен
    -- на случай сетевого принтера, чтобы не менять схему ради замены железа.
    transport     text NOT NULL DEFAULT 'agent' CHECK (transport IN ('tcp', 'agent')),
    active        boolean NOT NULL DEFAULT true,
    -- Сетевому принтеру адрес обязателен, агенту — нет: агент сам держит соединение.
    CONSTRAINT station_tcp_needs_host CHECK (transport <> 'tcp' OR printer_host IS NOT NULL)
);
