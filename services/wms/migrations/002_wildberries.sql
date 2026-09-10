-- Поток 0. Интеграция с Wildberries (раздел 7, блок «Интеграция с Wildberries»).
-- wms становится держателем токенов WB (раздел 12): в базе лежит только ссылка на
-- секрет, само значение — в секрет-провайдере.
--
-- wb_label создаётся в 003 вместе с wms_task: у него внешний ключ на задание.

CREATE TABLE wb_account (
    id                uuid PRIMARY KEY,
    owner_id          uuid NOT NULL REFERENCES owner (id),
    external_id       text UNIQUE NOT NULL,
    display_name      text NOT NULL,
    -- Ссылка на секрет, НЕ значение токена (инвариант 15, раздел 12). CHECK ниже
    -- отбивает случайную запись живого JWT: на стенде это отправило бы настоящие
    -- команды в кабинет клиента, и узнал бы об этом клиент, а не мы.
    secret_ref        text NOT NULL,
    scopes            jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- shadow — только GET /api/v3/orders, ни одной записи в WB (раздел 11, шаг 2).
    -- Переключение кабинета в live делается по одному, с возможностью отката.
    mode              text NOT NULL DEFAULT 'shadow' CHECK (mode IN ('shadow', 'live')),
    status            text NOT NULL DEFAULT 'DISABLED'
                          CHECK (status IN ('ACTIVE', 'AUTH_ERROR', 'RATE_LIMITED', 'DISABLED')),
    token_type        text CHECK (token_type IS NULL OR token_type IN ('BASIC', 'SERVICE')),
    token_expires_at  timestamptz,
    last_verified_at  timestamptz,
    last_sync_at      timestamptz,
    next_sync_at      timestamptz,
    -- Лизинг опроса: воркер занимает аккаунт на время цикла, чтобы два опросчика
    -- не били в один кабинет и не жгли общий лимит 300 запросов в минуту.
    sync_claimed_at   timestamptz,
    sync_attempts     integer NOT NULL DEFAULT 0 CHECK (sync_attempts >= 0),
    sync_error_code   text,
    wb_warehouse_id   bigint,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, owner_id),
    CONSTRAINT wb_account_secret_ref_is_not_a_token CHECK (
        secret_ref !~ '^eyJ[A-Za-z0-9_-]+\.' AND length(secret_ref) <= 512
    )
);

CREATE INDEX wb_account_due_idx ON wb_account (next_sync_at)
    WHERE status IN ('ACTIVE', 'RATE_LIMITED');

-- Курсор опроса с перекрытием (раздел 6.7): WB отдаёт задания постранично,
-- перекрытие закрывает дыру на границе окна.
CREATE TABLE wb_sync_cursor (
    account_id   uuid PRIMARY KEY REFERENCES wb_account (id) ON DELETE CASCADE,
    cursor       text,
    last_seen_at timestamptz
);

-- Ограничитель — защита от бана, а не механизм задержки (раздел 6.4). В нормальной
-- работе окно пустое; срабатывает на массовой приёмке, когда 500 SKU дали бы 500 вызовов.
CREATE TABLE wb_rate_limit (
    account_id    uuid NOT NULL REFERENCES wb_account (id) ON DELETE CASCADE,
    window_start  timestamptz NOT NULL,
    used          integer NOT NULL DEFAULT 0 CHECK (used >= 0),
    blocked_until timestamptz,
    PRIMARY KEY (account_id, window_start)
);

CREATE TABLE wb_supply (
    id            uuid PRIMARY KEY,
    wb_account_id uuid NOT NULL REFERENCES wb_account (id),
    -- Идентификатор поставки на стороне WB (WB-GI-…). NULL, пока поставка
    -- ещё не создана в WB, — накопительная поставка открывается лениво.
    wb_supply_id  text,
    state         text NOT NULL DEFAULT 'open'
                      CHECK (state IN ('open', 'closed', 'delivered', 'retired')),
    closed_at     timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (wb_account_id, wb_supply_id)
);

-- Одна накопительная поставка на кабинет: одна поставка — один кабинет (приложение D).
CREATE UNIQUE INDEX wb_supply_one_open_per_account_idx
    ON wb_supply (wb_account_id) WHERE state = 'open';

-- Журнал публикаций остатка. Нужен, чтобы отвечать на вопрос «когда мы последний
-- раз сказали WB про этот кабинет и что он ответил» (критерий раздела 10).
CREATE TABLE wb_stock_push (
    id         uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES wb_account (id),
    pushed_at  timestamptz NOT NULL DEFAULT now(),
    rows       integer NOT NULL CHECK (rows >= 0),
    result     jsonb NOT NULL
);

CREATE INDEX wb_stock_push_account_idx ON wb_stock_push (account_id, pushed_at DESC);
