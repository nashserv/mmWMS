-- Раскладка комиссии обязана совпасть с наценкой ТОЧНО, а не «не больше».
--
-- Проверялось только `split > total`: недоплата партнёру проходила молча.
-- Строка комиссии, которую не записали — сбой, ошибка округления, ветка кода,
-- забывшая одного из двух партнёров, — оставляла начисление внешне исправным,
-- а партнёра без части его денег. Заметить это можно было только сложив
-- комиссии руками, чего никто не делает.
--
-- Триггер отложенный (`DEFERRABLE INITIALLY DEFERRED`) и проверяется на
-- коммите: до конца транзакции раскладка законно неполна — строки пишутся по
-- одной.
CREATE OR REPLACE FUNCTION billing_commission_matches_accrual() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target uuid := COALESCE(NEW.accrual_id, OLD.accrual_id);
    split  numeric(14, 2);
    total  numeric(14, 2);
BEGIN
    SELECT COALESCE(sum(amount), 0) INTO split
      FROM billing_commission WHERE accrual_id = target;
    SELECT partner_amount INTO total FROM billing_accrual WHERE id = target;
    -- Начисления нет — его удалили вместе с комиссиями, проверять нечего.
    IF total IS NULL THEN
        RETURN NULL;
    END IF;
    IF split <> total THEN
        RAISE EXCEPTION
            'раскладка комиссии % не сходится с наценкой начисления % (%): '
            'разница %',
            split, target, total, total - split;
    END IF;
    RETURN NULL;
END;
$$;
