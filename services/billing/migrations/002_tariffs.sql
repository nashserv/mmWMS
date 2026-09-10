-- Поток C. Тарифы на все услуги склада, версии и объёмные ступени.
--
-- Форма взята из биллинга as-is (раздел 2.10 мастера) без переизобретения:
-- TariffVersion(effective_from, effective_to, approved, tiers, accumulation,
-- partner_fee) и VolumeTier(up_to, unit_price, minimum). Новое здесь одно —
-- услуг стало семь вместо четырёх источников событий (файл 04), потому что
-- сегодня склад делает приёмку, хранение, стикеровку и упаковку бесплатно.
--
-- Цены — заглушки: раздел 13, вопрос 3 («список услуг с ценами») владельцем
-- не закрыт. Допущение того же вопроса: тарифы строятся с ценами-заглушками и
-- правятся в админке. Поэтому цифры живут в seed/, а не в миграции.

CREATE TABLE billing_tariff (
    id         uuid PRIMARY KEY,
    code       text UNIQUE NOT NULL,
    service    billing_service NOT NULL,
    name       text NOT NULL,
    -- Единица тарификации из таблицы услуг файла 04: шт, короб,
    -- коробко-место × сутки, заказ, поставка.
    unit       text NOT NULL,
    -- Тариф по умолчанию для услуги — тот, по которому считается кабинет без
    -- собственного договора. Ровно один на услугу: иначе «по какому считать»
    -- решает порядок строк, то есть случай.
    is_default boolean NOT NULL DEFAULT false,
    active     boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX billing_tariff_one_default_per_service
    ON billing_tariff (service) WHERE is_default;

CREATE TABLE billing_tariff_version (
    id             uuid PRIMARY KEY,
    tariff_id      uuid NOT NULL REFERENCES billing_tariff (id),
    effective_from date NOT NULL,
    effective_to   date,
    -- Неутверждённая версия не тарифицирует. Цена, приехавшая в счёт клиента
    -- без чьей-либо подписи, — это спор с клиентом, а не строка в таблице.
    approved       boolean NOT NULL DEFAULT false,
    approved_by    text,
    approved_at    timestamptz,
    -- per_event — начисление на каждое событие (так работает биллинг as-is).
    -- per_period — накопление за период (хранение считается кроном за сутки).
    accumulation   text NOT NULL DEFAULT 'per_event'
                   CHECK (accumulation IN ('per_event', 'per_period')),
    -- Наценка партнёра по умолчанию для этой версии тарифа — то самое поле
    -- as-is (раздел 2.10). Оно остаётся ответом последней надежды: настоящая
    -- ставка живёт в price_layer партнёра, потому что «15 ₽ Зардала» — это
    -- свойство партнёра, а не свойство цены.
    partner_fee    numeric(14, 2) NOT NULL DEFAULT 0 CHECK (partner_fee >= 0),
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_tariff_version_period CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT billing_tariff_version_approval CHECK (
        (approved AND approved_by IS NOT NULL AND approved_at IS NOT NULL)
        OR NOT approved
    ),
    -- Две действующие версии одного тарифа в один день — две разные цены на
    -- одну операцию.
    CONSTRAINT billing_tariff_version_no_overlap EXCLUDE USING gist (
        tariff_id WITH =,
        daterange(effective_from, effective_to, '[)') WITH &&
    )
);

CREATE INDEX billing_tariff_version_lookup_idx
    ON billing_tariff_version (tariff_id, effective_from);

-- Объёмная ступень: до up_to единиц включительно цена unit_price, минимум
-- minimum за начисление. up_to IS NULL — последняя, «и далее».
CREATE TABLE billing_tariff_tier (
    id         uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES billing_tariff_version (id) ON DELETE CASCADE,
    up_to      numeric(14, 3) CHECK (up_to IS NULL OR up_to > 0),
    -- Цена MM-Express за единицу. Наценка партнёра сюда не входит: клиент
    -- платит unit_price + markup (раздел 1). Смешать их в одном числе значит
    -- потерять маржу — ровно то, что на проде и не считается.
    unit_price numeric(14, 2) NOT NULL CHECK (unit_price >= 0),
    minimum    numeric(14, 2) NOT NULL DEFAULT 0 CHECK (minimum >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Ступени различаются границей; NULL-граница у версии одна.
CREATE UNIQUE INDEX billing_tariff_tier_bound_idx
    ON billing_tariff_tier (version_id, up_to) WHERE up_to IS NOT NULL;
CREATE UNIQUE INDEX billing_tariff_tier_tail_idx
    ON billing_tariff_tier (version_id) WHERE up_to IS NULL;

-- === Договор кабинета ======================================================
-- Онбординг начинается с договора (файл 04). Тариф закрепляется за кабинетом
-- с историей — по той же причине, что и партнёр: смена тарифа не должна
-- переписывать закрытые месяцы.
CREATE TABLE billing_contract (
    id         uuid PRIMARY KEY,
    cabinet_id uuid NOT NULL REFERENCES cabinet (id),
    reference  text UNIQUE NOT NULL,
    signed_at  date,
    from_date  date NOT NULL,
    to_date    date,
    comment    text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_contract_period CHECK (to_date IS NULL OR to_date > from_date),
    CONSTRAINT billing_contract_no_overlap EXCLUDE USING gist (
        cabinet_id WITH =,
        daterange(from_date, to_date, '[)') WITH &&
    )
);

CREATE TABLE billing_tariff_assignment (
    id          uuid PRIMARY KEY,
    cabinet_id  uuid NOT NULL REFERENCES cabinet (id),
    tariff_id   uuid NOT NULL REFERENCES billing_tariff (id),
    contract_id uuid REFERENCES billing_contract (id),
    service     billing_service NOT NULL,
    from_date   date NOT NULL,
    to_date     date,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_tariff_assignment_period CHECK (to_date IS NULL OR to_date > from_date),
    CONSTRAINT billing_tariff_assignment_no_overlap EXCLUDE USING gist (
        cabinet_id WITH =,
        service WITH =,
        daterange(from_date, to_date, '[)') WITH &&
    )
);

CREATE INDEX billing_tariff_assignment_lookup_idx
    ON billing_tariff_assignment (cabinet_id, service, from_date);

-- === Что вообще тарифицируется =============================================
-- Таблица, а не цепочка if'ов в коде. На проде тарифицируются четыре источника
-- событий из десятков (раздел 3.4) — не потому, что так решили, а потому, что
-- список зашит в обработчик и не виден никому. Здесь он данные: видно, что
-- включено, что выключено и почему.
CREATE TABLE billing_billable_event (
    event_type     text PRIMARY KEY,
    service        billing_service NOT NULL,
    -- Где в payload лежит количество. Пусто — количество равно 1 (одно
    -- физическое действие, одна единица).
    quantity_path  text,
    -- Где в payload искать кабинет, по порядку. Событий пишут разные сервисы,
    -- и поле владельца у них называется по-разному (seller_id в приложении E,
    -- seller_external_id в контракте wms).
    seller_paths   text[] NOT NULL DEFAULT ARRAY['seller_id', 'seller_external_id',
                                                 'owner_external_id', 'organization_id',
                                                 'owner_id'],
    active         boolean NOT NULL DEFAULT true,
    -- Зачем включено или почему выключено. Обязателен: выключенная строка без
    -- объяснения через месяц читается как «забыли включить».
    comment        text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE billing_billable_event IS
    'Событие шины -> услуга. Правится в админке, а не релизом: список того, что '
    'склад делает бесплатно, обязан быть виден без чтения кода.';
