-- Поток 0. Outbox — собственная публикация событий напрямую в RabbitMQ,
-- с ретеншеном и партиционированием (раздел 6.7).
--
-- В боевом контуре integration_outbox дорос до 136 210 записей без ретеншена
-- вообще (раздел 3.6). Здесь партиции по месяцам заведены с первого дня, чтобы
-- чистка была DROP PARTITION, а не DELETE по живой таблице.

CREATE TABLE outbox (
    id             bigint GENERATED ALWAYS AS IDENTITY,
    event_id       uuid NOT NULL,
    type           text NOT NULL,
    tenant_id      text NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 128),
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    payload        jsonb NOT NULL,
    correlation_id text NOT NULL,
    -- Агрегат, в пределах которого sequence монотонен. Раздел 7 этого поля не
    -- перечисляет, но приложение E требует монотонности sequence «в пределах
    -- задания», а без ссылки на задание это требование не выразить и не
    -- проиндексировать. Шаблон wb-fbs-gateway держит здесь aggregate_id — берём
    -- ту же форму. См. docs/schema-decisions.md.
    aggregate_id   uuid NOT NULL,
    -- ТРЕБОВАНИЕ К РЕАЛИЗАЦИИ (приложение E, последний абзац): sequence
    -- инкрементируется под блокировкой строки агрегата в ТОЙ ЖЕ транзакции,
    -- что и движение товара. Потребители полагаются на монотонность.
    sequence       bigint NOT NULL CHECK (sequence >= 0),
    published_at   timestamptz,
    attempts       integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error     text,
    -- Партиционирование требует, чтобы ключ раздела входил в каждый уникальный
    -- индекс. Поэтому event_id уникален в паре с occurred_at, а не сам по себе,
    -- как в разделе 7: глобального уникального индекса на партиционированной
    -- таблице Postgres не поддерживает. На практике это тот же контракт —
    -- event_id это uuid, и occurred_at приходит вместе с ним в одном событии.
    PRIMARY KEY (id, occurred_at),
    CONSTRAINT outbox_event_id_key UNIQUE (event_id, occurred_at),
    CONSTRAINT outbox_sequence_key UNIQUE (aggregate_id, sequence, occurred_at)
) PARTITION BY RANGE (occurred_at);

-- Очередь публикатора: неопубликованные события в порядке появления.
CREATE INDEX outbox_pending_idx ON outbox (occurred_at, id) WHERE published_at IS NULL;
CREATE INDEX outbox_type_idx ON outbox (type, occurred_at DESC);
CREATE INDEX outbox_aggregate_idx ON outbox (aggregate_id, sequence);

-- Заводит месячную партицию, если её ещё нет. Вызывается из обслуживания;
-- повторный вызов безопасен.
CREATE FUNCTION wms_outbox_ensure_partition(month_start date) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    first_day   date := date_trunc('month', month_start)::date;
    next_month  date := (date_trunc('month', month_start) + interval '1 month')::date;
    part_name   text := format('outbox_%s', to_char(first_day, 'YYYY_MM'));
BEGIN
    IF to_regclass(format('public.%I', part_name)) IS NOT NULL THEN
        RETURN;
    END IF;
    EXECUTE format(
        'CREATE TABLE %I PARTITION OF outbox FOR VALUES FROM (%L) TO (%L)',
        part_name, first_day, next_month);
END;
$$;

-- Партиции на текущий месяц и три вперёд: публикация не должна упереться
-- в отсутствующий раздел в первую же ночь после Нового года.
SELECT wms_outbox_ensure_partition((date_trunc('month', now()) + (n || ' month')::interval)::date)
FROM generate_series(0, 3) AS n;

-- Страховочный раздел. Если обслуживание проспало — событие всё равно ляжет,
-- а не потеряется. Непустой DEFAULT замедляет присоединение новых партиций,
-- поэтому его наполнение — сигнал, что обслуживание не отработало.
CREATE TABLE outbox_default PARTITION OF outbox DEFAULT;

-- Ретеншен: удаляется только опубликованное и только целыми партициями.
-- Неопубликованное событие не удаляется никогда — это потеря факта, а не мусор.
CREATE FUNCTION wms_outbox_drop_published_before(cutoff date) RETURNS TABLE (dropped text)
LANGUAGE plpgsql AS $$
DECLARE
    part record;
    unpublished bigint;
BEGIN
    FOR part IN
        SELECT c.relname,
               pg_get_expr(c.relpartbound, c.oid) AS bounds
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        WHERE i.inhparent = 'public.outbox'::regclass
          AND c.relname <> 'outbox_default'
    LOOP
        -- Границы партиции разбирать не будем: спросим саму партицию.
        EXECUTE format('SELECT count(*) FROM %I WHERE published_at IS NULL', part.relname)
            INTO unpublished;
        CONTINUE WHEN unpublished > 0;
        EXECUTE format('SELECT count(*) FROM %I WHERE occurred_at >= %L', part.relname, cutoff)
            INTO unpublished;
        CONTINUE WHEN unpublished > 0;
        EXECUTE format('DROP TABLE %I', part.relname);
        dropped := part.relname;
        RETURN NEXT;
    END LOOP;
END;
$$;
