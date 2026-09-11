-- Ежедневный отчёт расхождений — раздел 11, шаг 2.
--
-- До этой таблицы отчёт уходил только в лог. Для шага 2 этого мало: наблюдать
-- полагается НЕДЕЛЮ, а смысл наблюдения — увидеть, сходятся ли наши задания с
-- боевыми и становится ли расхождений меньше день ото дня. По логу этого не
-- сравнить: он ротируется, он теряется при пересоздании контейнера, и в нём
-- нельзя посчитать «вчера двенадцать, сегодня три».
--
-- Копить расхождения молча — ровно то, что делает боевой контур сегодня
-- (раздел 3: 2467 расхождений, у всех 2645 отмен причина NULL). Отчёт,
-- который негде прочитать, от молчания отличается мало.

CREATE TABLE shadow_divergence_report (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Сутки наблюдения. Отчёт снимается несколько раз в день, но сравнивают
    -- его посуточно, поэтому день вынесен отдельным полем.
    observed_on        date        NOT NULL DEFAULT current_date,
    observed_at        timestamptz NOT NULL DEFAULT now(),
    seller_external_id text        NOT NULL,
    barcode            text,
    -- Наше состояние против того, что говорит Wildberries. Пара и есть
    -- расхождение: «у нас cancelled, у WB complete» — это те самые 2467.
    state              text        NOT NULL,
    wb_status          text,
    tasks              integer     NOT NULL CHECK (tasks > 0),
    -- Самое старое расхождение в группе: показывает, копится оно или свежее.
    oldest             timestamptz
);

-- Сравнение дня с днём — главный и почти единственный запрос к этой таблице.
CREATE INDEX shadow_divergence_report_day_idx
    ON shadow_divergence_report (observed_on DESC, seller_external_id);

COMMENT ON TABLE shadow_divergence_report IS
    'Снимки расхождений с Wildberries в shadow (раздел 11, шаг 2). '
    'Сравнивать посуточно: сходятся ли таблицы заданий и убывает ли разница.';
