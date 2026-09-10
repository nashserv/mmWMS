-- Поток C. Роли и права. **Только роли** — механику JWT/JWKS этот поток не трогает
-- (файл 04, «Владеет»).
--
-- Сервис identity существует на платформе и живёт в боевом контуре
-- (/root/MM-express-final-platform-integration); его исходников в этом
-- репозитории нет, и выдумывать их описание значило бы записать в стенд
-- неправду (README стенда, раздел «Сервисы платформы»). Поэтому миграция
-- строго **дополняющая**: свои таблицы с префиксом identity_, ни одной правки
-- в чужие. Когда исходники приедут, она накатывается на ту же базу поверх.
--
-- Что добавляется: четыре роли склада к четырём существующим и выдача роли с
-- областью действия. Права по дереву менеджеров считает биллинг — дерево
-- живёт там, а identity отвечает на вопрос «кто этот человек и что ему можно»,
-- а не «какие кабинеты у этого партнёра».

CREATE TABLE IF NOT EXISTS identity_role (
    code        text PRIMARY KEY,
    name        text NOT NULL,
    -- office — люди за столом, floor — люди на складе, partner — продажи.
    kind        text NOT NULL CHECK (kind IN ('office', 'floor', 'partner')),
    description text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Область действия роли. Права по организации и продавцу были и раньше
-- (раздел 2.11); дерева менеджеров не было — отсюда partner_branch.
CREATE TABLE IF NOT EXISTS identity_role_grant (
    id          uuid PRIMARY KEY,
    -- Пользователь платформенного identity. Внешнего ключа нет намеренно:
    -- таблица пользователей принадлежит чужому сервису, и ссылаться на неё из
    -- дополняющей миграции — значит однажды уронить чужой деплой.
    user_id     uuid NOT NULL,
    role_code   text NOT NULL REFERENCES identity_role (code),
    -- global — везде; organization/seller — как было; partner_branch — ветка
    -- дерева менеджеров: менеджер видит своих, старший — всю свою ветку.
    scope_kind  text NOT NULL DEFAULT 'global'
                CHECK (scope_kind IN ('global', 'organization', 'seller', 'partner_branch')),
    -- Идентификатор области: организация, продавец либо партнёр — корень ветки.
    scope_id    text,
    granted_by  text NOT NULL,
    granted_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz,
    revoked_by  text,
    CONSTRAINT identity_role_grant_scope_needs_id CHECK (
        scope_kind = 'global' OR scope_id IS NOT NULL
    ),
    CONSTRAINT identity_role_grant_revoked_has_author CHECK (
        revoked_at IS NULL OR revoked_by IS NOT NULL
    )
);

-- Одна и та же роль на ту же область дважды — это две записи об одном праве,
-- и отзыв одной оставит вторую. Действующая выдача может быть только одна.
CREATE UNIQUE INDEX IF NOT EXISTS identity_role_grant_active_idx
    ON identity_role_grant (user_id, role_code, scope_kind, COALESCE(scope_id, ''))
 WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS identity_role_grant_user_idx
    ON identity_role_grant (user_id) WHERE revoked_at IS NULL;

-- Четыре существующие роли (раздел 2.11) и четыре новые складские (файл 04).
-- Существующие перечислены не для того, чтобы завести их заново, а чтобы
-- справочник ролей был полным: приложение, которое видит только половину
-- ролей, половину прав и проверит.
INSERT INTO identity_role (code, name, kind, description) VALUES
    ('owner',          'Владелец',            'office',
     'Владелец организации клиента. Видит свой кабинет целиком.'),
    ('manager',        'Менеджер',            'office',
     'Сотрудник клиента с доступом к кабинету.'),
    ('admin',          'Администратор',       'office',
     'Администратор MM-Express. Онбординг, тарифы, справочники.'),
    ('accountant',     'Бухгалтер',           'office',
     'Счета, акты, закрытие периода, признание вознаграждения партнёра.'),
    ('warehouse_head', 'Начальник склада',    'floor',
     'Витрина выработки смены, расхождения, клапан «собрать без остатка».'),
    ('picker',         'Сборщик',             'floor',
     'Сессия подбора, скан у стойки, упаковка, печать этикетки.'),
    ('receiver',       'Приёмщик',            'floor',
     'Приёмка, пересчёт, расхождения, размещение в коробки и ячейки.'),
    ('logist',         'Логист',              'floor',
     'Поставки, передача в доставку, короба.'),
    ('senior_manager', 'Старший менеджер',    'partner',
     'Партнёр — корень ветки. Видит свою ветку целиком и её комиссию.'),
    ('account_manager','Менеджер кабинетов',  'partner',
     'Партнёр — ведёт кабинеты. Видит только своих клиентов и свою комиссию.')
ON CONFLICT (code) DO NOTHING;
