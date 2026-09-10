-- Поток C. Журнал действий администратора.
--
-- Файл 04 требует от онбординга «ни одного ручного SQL-запроса в процессе».
-- Требование бессмысленно без второй половины: если процесс идёт через
-- админку, то кто, когда и что в ней сделал, обязано быть записано. Иначе
-- вопрос «почему у этого кабинета тариф с прошлого понедельника» снова
-- решается перепиской.
--
-- Журнал пишется в базе админки, а не биллинга: биллинг отвечает за деньги,
-- админка — за то, кто их трогал.

CREATE TABLE admin_audit (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    -- Пользователь identity. Внешнего ключа нет: база на сервис (раздел 2.2).
    actor_id    text NOT NULL,
    actor_roles text[] NOT NULL DEFAULT '{}',
    action      text NOT NULL,
    -- На что подействовали: кабинет, партнёр, версия тарифа, период.
    subject     text,
    -- Что отправили. Секретов здесь нет и быть не может: онбординг принимает
    -- ссылку на секрет, а не токен (инвариант 15), и это единственное поле,
    -- которое хоть как-то похоже на чувствительное.
    request     jsonb,
    -- Чем кончилось: код ответа биллинга и его тело. Неудачная попытка нужна
    -- в журнале не меньше удачной — по ней и разбирают «почему не завелось».
    status      integer NOT NULL,
    response    jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX admin_audit_actor_idx   ON admin_audit (actor_id, at DESC);
CREATE INDEX admin_audit_subject_idx ON admin_audit (subject, at DESC) WHERE subject IS NOT NULL;
CREATE INDEX admin_audit_action_idx  ON admin_audit (action, at DESC);

COMMENT ON TABLE admin_audit IS
    'Кто, когда и что сделал в админке. Без него «ни одного ручного SQL» — только половина правила.';
