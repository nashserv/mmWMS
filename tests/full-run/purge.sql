-- Уборка данных полного прогона. Раздел 9.6.
--
-- Раньше уборка отменяла задания через API. Это было неверно дважды.
--
-- Во-первых, отмена — складская операция: она возвращает товар на полку,
-- пишет движения и публикует остаток. Сто шестьдесят отмен подряд выбирали
-- лимит кабинета (300 запросов в минуту), и следующий прогон не мог опросить
-- WB вовремя.
--
-- Во-вторых, симулятор об отмене не знает: у него заказ остаётся `new`, а у
-- нас становится `cancelled`. Сверка честно называет это расхождением, и
-- `diverged` копился от прогона к прогону — 401 штука к моменту аудита.
--
-- Поэтому данные прогона удаляются, а не отменяются: они синтетические и
-- пересоздаются следующим же запуском. Симулятор сбрасывается отдельно
-- (`POST /__stand__/reset`), чтобы обе стороны начинали с чистого листа.
--
-- Триггеры журнала снимаются на время транзакции: удаляются не записи о
-- настоящих движениях товара, а данные прогона целиком. Инвариант 3 про то,
-- что журнал не ПЕРЕПИСЫВАЮТ — движение отменяется встречным движением;
-- здесь отменять нечего, вещи не было.

-- Временная таблица `run_owner` со списком владельцев прогона создаётся
-- вызывающим отдельным запросом: psycopg не выполняет многооператорный скрипт
-- с параметрами, а список клиентов подставлять в текст руками нельзя.

BEGIN;

ALTER TABLE stock_move DISABLE TRIGGER stock_move_no_rewrite;
ALTER TABLE stock_move DISABLE TRIGGER stock_move_project_balance;
ALTER TABLE stock_balance DISABLE TRIGGER stock_balance_projection_only;

UPDATE wms_task SET label_id = NULL, reservation_id = NULL
 WHERE owner_id IN (SELECT id FROM run_owner);

DELETE FROM wb_label   WHERE task_id IN (SELECT id FROM wms_task WHERE owner_id IN (SELECT id FROM run_owner));

-- Сессии подбора запоминаются ДО удаления строк: клиент у сессии не записан,
-- и связь с прогоном живёт только в `pick_line.owner_id`. Удалить строки, а
-- потом искать сессии, уже нечем — они становятся сиротами, неотличимыми от
-- настоящих. Так их и накопилось 195 к концу аудита: шаг 8 открывает пять
-- сессий за прогон, и ни одна не убиралась.
CREATE TEMP TABLE run_session ON COMMIT DROP AS
SELECT DISTINCT session_id AS id FROM pick_line
 WHERE owner_id IN (SELECT id FROM run_owner);

DELETE FROM pick_line  WHERE owner_id IN (SELECT id FROM run_owner);
DELETE FROM pick_session WHERE id IN (SELECT id FROM run_session);

-- Сессия без единой строки следа в `pick_line` не оставляет, и по владельцу
-- её не найти вовсе: шаг 8 открывает пять сессий, а задание достаётся не
-- каждой. Час — с запасом: живая сессия набирает строки в первые секунды,
-- пустая через час означает, что сборщик ушёл, ничего не взяв.
DELETE FROM pick_session s
 WHERE NOT EXISTS (SELECT 1 FROM pick_line l WHERE l.session_id = s.id)
   AND s.started_at < now() - interval '1 hour';
DELETE FROM discrepancy WHERE owner_id IN (SELECT id FROM run_owner);
DELETE FROM reservation WHERE owner_id IN (SELECT id FROM run_owner);
DELETE FROM wms_return  WHERE owner_id IN (SELECT id FROM run_owner);
DELETE FROM wms_task    WHERE owner_id IN (SELECT id FROM run_owner);
DELETE FROM receipt_line WHERE receipt_id IN (SELECT id FROM receipt WHERE owner_id IN (SELECT id FROM run_owner));
DELETE FROM receipt      WHERE owner_id IN (SELECT id FROM run_owner);
DELETE FROM inventory_count_line WHERE count_id IN (SELECT id FROM inventory_count WHERE owner_id IN (SELECT id FROM run_owner));
DELETE FROM inventory_count WHERE owner_id IN (SELECT id FROM run_owner);
DELETE FROM shipment     WHERE owner_id IN (SELECT id FROM run_owner);

DELETE FROM wb_stock_push  WHERE account_id IN (SELECT id FROM wb_account WHERE owner_id IN (SELECT id FROM run_owner));
DELETE FROM wb_rate_limit  WHERE account_id IN (SELECT id FROM wb_account WHERE owner_id IN (SELECT id FROM run_owner));
DELETE FROM wb_sync_cursor WHERE account_id IN (SELECT id FROM wb_account WHERE owner_id IN (SELECT id FROM run_owner));
DELETE FROM wb_supply      WHERE wb_account_id IN (SELECT id FROM wb_account WHERE owner_id IN (SELECT id FROM run_owner));
DELETE FROM wb_account     WHERE owner_id IN (SELECT id FROM run_owner);

DELETE FROM stock_balance WHERE owner_id IN (SELECT id FROM run_owner);
DELETE FROM stock_move    WHERE owner_id IN (SELECT id FROM run_owner);
DELETE FROM box WHERE owner_id IN (SELECT id FROM run_owner);
DELETE FROM sku WHERE owner_id IN (SELECT id FROM run_owner);

-- Расхождения, накопленные прошлыми уборками через API: они больше не
-- появятся, но уже записанные надо убрать, иначе алерт TasksDiverged горит
-- вечно из-за заданий, которых нет.
DELETE FROM shadow_divergence_report
 WHERE seller_external_id IN (SELECT seller_external_id FROM run_owner_name);

DELETE FROM owner WHERE id IN (SELECT id FROM run_owner);
-- Ячейки прогона. Процент удвоен: psycopg принимает одиночный за
-- плейсхолдер, а параметры в этом скрипте есть.
DELETE FROM cell  WHERE address LIKE 'FR-%%';

ALTER TABLE stock_move ENABLE TRIGGER stock_move_no_rewrite;
ALTER TABLE stock_move ENABLE TRIGGER stock_move_project_balance;
ALTER TABLE stock_balance ENABLE TRIGGER stock_balance_projection_only;

COMMIT;
