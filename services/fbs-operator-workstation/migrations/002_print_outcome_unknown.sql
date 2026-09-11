-- Исход печати «неизвестно».
--
-- Агент не ответил: байты ушли в сокет, и принтер мог напечатать — или не
-- напечатать. Записать `failed` значит сказать человеку «не напечаталось» и
-- получить вторую наклейку на ту же вещь; записать `written` — соврать в
-- другую сторону. Схема обязана принимать третий ответ.
--
-- Без этого значения `finish_print` молча не менял строку: `execute` ловит
-- любое исключение и возвращает `None`, печать оставалась в `queued`, и
-- разобрать её потом было нечем.
ALTER TABLE workstation_print_job DROP CONSTRAINT IF EXISTS workstation_print_job_outcome_check;
ALTER TABLE workstation_print_job ADD CONSTRAINT workstation_print_job_outcome_check
    CHECK (outcome IN ('queued', 'sent', 'written', 'failed', 'unknown'));
