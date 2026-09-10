-- Поток C. Справочник партнёров, дерево менеджеров, история закреплений кабинетов.
-- Источник: раздел 1 мастер-контекста (три уровня продаж, наценочная модель)
-- и раздел «Справочник партнёров и дерево менеджеров» файла 04.
--
-- Почему это первая миграция биллинга: без партнёра у начисления нет ответа на
-- вопрос «чья это наценка». Сегодня 15 ₽ Зардала живут в partner_fee версии
-- тарифа, то есть в цене, а не в том, кто её получил. Пока справочника нет,
-- вознаграждение партнёра нельзя ни посчитать, ни выплатить, ни объяснить клиенту.

-- Нужен для EXCLUDE-ограничений: uuid/text равенством и период пересечением
-- в одном индексе. Без btree_gist историю закреплений пришлось бы стеречь
-- триггером, а триггер не держит гонку двух параллельных вставок.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Услуги MM-Express. Домен, а не enum-тип: список открыт (раздел 13, вопрос 3
-- ещё не закрыт владельцем), а добавить значение в CHECK дешевле, чем в pg enum.
CREATE DOMAIN billing_service AS text
    CHECK (VALUE IN (
        'receiving',         -- приёмка, шт/короб
        'storage',           -- хранение, коробко-место × сутки (крон, не событие)
        'labeling',          -- стикеровка, шт
        'packing',           -- упаковка, шт по типу упаковки
        'picking',           -- сборка, заказ
        'shipping',          -- отгрузка, поставка
        'returns',           -- возврат, шт
        'order_processing'   -- обработка заказов WB (тарифицируется на проде сегодня)
    ));

-- Роль партнёра относительно кабинета. Дерево (partner.parent_id) отвечает на
-- вопрос «кто над кем», роль — на вопрос «в каком качестве закреплён здесь».
CREATE DOMAIN billing_partner_role AS text
    CHECK (VALUE IN ('senior_manager', 'account_manager'));

-- === Партнёр ===============================================================
-- Дерево произвольной глубины: сегодня корень один (Зардал), но допущение
-- раздела 13, вопрос 7 — «старшие менеджеры кроме Зардала могут появиться».
-- Схема, которая этого не умеет, переделывается вместе с историей начислений.
CREATE TABLE partner (
    id         uuid PRIMARY KEY,
    parent_id  uuid REFERENCES partner (id),
    name       text NOT NULL,
    -- Пользователь identity, под которым партнёр входит в ЛК. Nullable:
    -- партнёр в справочнике заводится раньше, чем ему выдают доступ.
    user_id    uuid,
    active     boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT partner_not_own_parent CHECK (parent_id IS DISTINCT FROM id)
);

CREATE INDEX partner_parent_idx ON partner (parent_id);
CREATE UNIQUE INDEX partner_user_idx ON partner (user_id) WHERE user_id IS NOT NULL;

-- Цикл в дереве менеджеров — это бесконечный обход при расчёте «своей ветки»
-- и, значит, зависший отчёт о комиссии. Ловим на записи, а не на чтении.
CREATE FUNCTION partner_no_cycle() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    ancestor uuid := NEW.parent_id;
    depth    int  := 0;
BEGIN
    WHILE ancestor IS NOT NULL LOOP
        IF ancestor = NEW.id THEN
            RAISE EXCEPTION 'цикл в дереве партнёров: % не может быть потомком самого себя', NEW.id;
        END IF;
        depth := depth + 1;
        IF depth > 32 THEN
            RAISE EXCEPTION 'дерево партнёров глубже 32 уровней — почти наверняка цикл';
        END IF;
        SELECT parent_id INTO ancestor FROM partner WHERE id = ancestor;
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER partner_no_cycle
    BEFORE INSERT OR UPDATE OF parent_id ON partner
    FOR EACH ROW EXECUTE FUNCTION partner_no_cycle();

-- === Кабинет ===============================================================
-- Кабинет = ИП = продавец на Wildberries (раздел 1). База на сервис (раздел 2.2),
-- поэтому внешнего ключа на owner в базе wms нет и быть не может: биллинг ведёт
-- свой справочник и синхронизирует его при онбординге.
--
-- 21 кабинет WB против 4 записей в sellers (раздел 3.6) — ровно то, что здесь
-- чинится: кабинет заводится один раз и одним потоком, а не руками в трёх базах.
CREATE TABLE cabinet (
    id                 uuid PRIMARY KEY,
    -- Ключ владельца товара в wms (owner.seller_external_id). Кабинетов
    -- Wildberries у него может быть несколько — они в cabinet_wb_account.
    seller_external_id text UNIQUE NOT NULL,
    -- Тот же владелец в базе wms (owner.id). Внешним ключом быть не может —
    -- база на сервис (раздел 2.2), — но события физического действия несут
    -- именно этот uuid (owner_id в payload), а не внешний ключ продавца.
    -- Без него стикеровка и подбор не находят кабинет и уходят в unbilled.
    wms_owner_id       uuid UNIQUE,
    name               text NOT NULL,
    inn                text,
    -- Организация, на которую выставляется счёт (seller_id в приложении E).
    organization_id    text,
    active             boolean NOT NULL DEFAULT true,
    -- Кабинет, доехавший до биллинга событием, а не онбордингом. Не отказ в
    -- начислении (иначе выручка теряется молча), а видимая метка: такой кабинет
    -- попадает в счётчик Prometheus и в отчёт «кого не завели».
    needs_onboarding   boolean NOT NULL DEFAULT false,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX cabinet_needs_onboarding_idx ON cabinet (needs_onboarding) WHERE needs_onboarding;

-- Кабинеты Wildberries клиента. Отдельной таблицей, а не полем: в сиде стенда
-- у двух владельцев по два кабинета, а на проде 21 кабинет WB против 4 записей
-- в sellers (раздел 3.6). Поле «один кабинет на клиента» это просто не описывает.
CREATE TABLE cabinet_wb_account (
    id           uuid PRIMARY KEY,
    cabinet_id   uuid NOT NULL REFERENCES cabinet (id),
    external_id  text UNIQUE NOT NULL,
    display_name text,
    -- Ссылка на секрет, не секрет. Токены WB — это JWT (приложение D), и в
    -- базу биллинга они не попадают никогда (инвариант 15). Проверка стоит в
    -- схеме, как у wb_account на стенде: код можно обойти, CHECK нельзя.
    secret_ref   text CHECK (secret_ref IS NULL OR secret_ref !~ '^eyJ'),
    active       boolean NOT NULL DEFAULT true,
    connected_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX cabinet_wb_account_cabinet_idx ON cabinet_wb_account (cabinet_id);

-- === Закрепление кабинета за партнёром — история, а не поле =================
-- Партнёра передают другому, а прошлые периоды пересчитываться не должны
-- (файл 04). Прямое поле partner_id на кабинете это не выражает: смена партнёра
-- переписала бы и вознаграждение за уже закрытые месяцы.
--
-- to_date исключающая (полуинтервал [from_date, to_date)): закрепление,
-- закончившееся 1 сентября, и начавшееся 1 сентября — это один день без дыры
-- и без наложения, а не спор о том, кому принадлежит операция того дня.
CREATE TABLE cabinet_assignment (
    id         uuid PRIMARY KEY,
    cabinet_id uuid NOT NULL REFERENCES cabinet (id),
    partner_id uuid NOT NULL REFERENCES partner (id),
    role       billing_partner_role NOT NULL,
    from_date  date NOT NULL,
    to_date    date,
    comment    text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT cabinet_assignment_period CHECK (to_date IS NULL OR to_date > from_date),
    -- Два партнёра в одной роли на один кабинет в один день — это два счёта на
    -- одну операцию. Ограничение стоит в базе, потому что писать в неё будут и
    -- админка, и онбординг, и миграция данных.
    CONSTRAINT cabinet_assignment_no_overlap EXCLUDE USING gist (
        cabinet_id WITH =,
        role WITH =,
        daterange(from_date, to_date, '[)') WITH &&
    )
);

CREATE INDEX cabinet_assignment_partner_idx ON cabinet_assignment (partner_id, from_date);
CREATE INDEX cabinet_assignment_cabinet_idx ON cabinet_assignment (cabinet_id, from_date);

-- === Слой наценки ==========================================================
-- Наценка партнёра по услуге, с историей (файл 04). Раздел 13, вопрос 5:
-- допущение — одна ставка на партнёра, переопределяется на конкретного клиента.
-- Отсюда cabinet_id NULL = ставка партнёра по умолчанию, cabinet_id NOT NULL =
-- переопределение для этого кабинета.
CREATE TABLE price_layer (
    id         uuid PRIMARY KEY,
    partner_id uuid NOT NULL REFERENCES partner (id),
    cabinet_id uuid REFERENCES cabinet (id),
    service    billing_service NOT NULL,
    -- Наценка за единицу услуги в рублях. Это надбавка сверх тарифа
    -- MM-Express, а не доля из него: клиент платит 45 = 30 наших + 15 партнёра
    -- (раздел 1, «перепродажа услуги дороже, а не комиссия из наших 30»).
    markup     numeric(14, 2) NOT NULL CHECK (markup >= 0),
    from_date  date NOT NULL,
    to_date    date,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT price_layer_period CHECK (to_date IS NULL OR to_date > from_date),
    -- Ставка партнёра по умолчанию: не больше одной на услугу в каждый день.
    CONSTRAINT price_layer_default_no_overlap EXCLUDE USING gist (
        partner_id WITH =,
        service WITH =,
        daterange(from_date, to_date, '[)') WITH &&
    ) WHERE (cabinet_id IS NULL),
    -- Переопределение на кабинет: тоже не больше одного на услугу в день.
    CONSTRAINT price_layer_cabinet_no_overlap EXCLUDE USING gist (
        partner_id WITH =,
        cabinet_id WITH =,
        service WITH =,
        daterange(from_date, to_date, '[)') WITH &&
    ) WHERE (cabinet_id IS NOT NULL)
);

CREATE INDEX price_layer_lookup_idx ON price_layer (partner_id, service, from_date);

-- === Ветка партнёра ========================================================
-- «Менеджер видит только своих клиентов и свою комиссию, старший — свою ветку
-- целиком» (файл 04, раздел «Роли и права»). Право на строку начисления
-- вычисляется отсюда, а не собирается в приложении: забытый фильтр в одном
-- обработчике показывает менеджеру чужую комиссию.
CREATE FUNCTION partner_subtree(root uuid) RETURNS TABLE (partner_id uuid, depth int)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE branch AS (
        SELECT p.id, 0 AS depth FROM partner p WHERE p.id = root
        UNION ALL
        SELECT child.id, branch.depth + 1
          FROM partner child
          JOIN branch ON child.parent_id = branch.id
         WHERE branch.depth < 32
    )
    SELECT id, depth FROM branch;
$$;

COMMENT ON FUNCTION partner_subtree(uuid) IS
    'Партнёр и все его потомки. Основа прав: старший менеджер видит свою ветку целиком.';
