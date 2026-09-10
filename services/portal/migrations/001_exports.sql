-- Поток C. База ЛК клиента.
--
-- **Здесь намеренно нет таблицы остатков.** Сегодня портал показывает 3528
-- единиц при реальном остатке 92 (файл 04) — ровно потому, что держит своё
-- зеркало кабинета Wildberries. Одно число получается только у того, у кого
-- один источник, поэтому остаток портал спрашивает у `wms` при каждом показе
-- и не хранит. Таблица-зеркало, заведённая «для скорости», вернёт расхождение
-- в тот же день.
--
-- Остаётся то, что действительно принадлежит порталу: кто и что выгрузил.

CREATE TABLE portal_export (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    -- Пользователь identity. Внешнего ключа нет: база на сервис (раздел 2.2).
    actor_id    text NOT NULL,
    seller_external_id text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('act', 'accruals', 'stock')),
    period      text,
    rows_count  integer NOT NULL DEFAULT 0
);

CREATE INDEX portal_export_seller_idx ON portal_export (seller_external_id, at DESC);

COMMENT ON TABLE portal_export IS
    'Клиент выгружает акт сам, без участия бухгалтера (файл 04). Здесь — кто и когда это сделал.';
