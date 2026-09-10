-- Поток C. Приём событий шины, собственный outbox, идемпотентность команд.
--
-- Outbox/inbox в каждом сервисе отдельно, общей библиотеки нет (раздел 2.2).
-- Идемпотентность — таблица *_idempotency с claim / complete / abort, как в
-- остальных сервисах платформы.

-- === Inbox =================================================================
-- Событие сначала ложится сюда одной транзакцией с начислением, и только
-- потом подтверждается брокеру. Иначе падение между «начислил» и «ack»
-- начисляет второй раз, а падение между «ack» и «начислил» теряет выручку.
CREATE TABLE billing_inbox (
    event_id       uuid PRIMARY KEY,
    event_type     text NOT NULL,
    tenant_id      text,
    correlation_id text,
    payload        jsonb NOT NULL,
    occurred_at    timestamptz,
    received_at    timestamptz NOT NULL DEFAULT now(),
    processed_at   timestamptz,
    -- accrued — начислено, unbilled — разобрано и не тарифицируется,
    -- failed — обработчик упал, событие ждёт повтора.
    outcome        text CHECK (outcome IN ('accrued', 'skipped', 'unbilled', 'failed')),
    attempts       integer NOT NULL DEFAULT 0,
    last_error     text
);

CREATE INDEX billing_inbox_pending_idx ON billing_inbox (received_at)
    WHERE processed_at IS NULL;
CREATE INDEX billing_inbox_type_idx ON billing_inbox (event_type, received_at);

-- === Outbox ================================================================
-- То, что биллинг сообщает наружу: начисление создано, счёт выставлен,
-- вознаграждение партнёра признано. Порядок в пределах сущности держит
-- sequence, инкрементируемый в той же транзакции (приложение E).
CREATE TABLE billing_outbox (
    id             bigserial PRIMARY KEY,
    event_id       uuid UNIQUE NOT NULL,
    type           text NOT NULL,
    tenant_id      text NOT NULL,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    payload        jsonb NOT NULL,
    correlation_id text,
    sequence       bigint,
    published_at   timestamptz,
    attempts       integer NOT NULL DEFAULT 0,
    last_error     text
);

CREATE INDEX billing_outbox_unpublished_idx ON billing_outbox (id)
    WHERE published_at IS NULL;

-- === Идемпотентность команд ================================================
-- claim / complete / abort: повтор запроса с тем же ключом отдаёт тот же
-- ответ, а не выполняет операцию второй раз (инвариант 5).
CREATE TABLE billing_idempotency (
    key         text PRIMARY KEY,
    operation   text NOT NULL,
    state       text NOT NULL DEFAULT 'claimed'
                CHECK (state IN ('claimed', 'completed', 'aborted')),
    -- Ответ, отданный в первый раз. Повтор получает его же дословно.
    response    jsonb,
    claimed_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT billing_idempotency_finished CHECK (
        state = 'claimed' OR finished_at IS NOT NULL
    )
);

CREATE INDEX billing_idempotency_stale_idx ON billing_idempotency (claimed_at)
    WHERE state = 'claimed';
