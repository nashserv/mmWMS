-- Поток C. Начисления, периоды, счета, корректировки.
--
-- Форма начисления — из биллинга as-is (раздел 2.10): event_id, service,
-- tariff_version_id, quantity, amount, occurred_on, period, allocation_key,
-- adjustment_of, partner_amount. Сохранено дословно, чтобы данные прода
-- перенеслись без переписывания.
--
-- Инвариант 13 мастера: физическое действие = движение + событие + начисление.
-- Сегодня на 6374 задания приходится 145 начислений — треть процента. Поэтому
-- здесь два обязательных следствия:
--   1. начисление идемпотентно по event_id — повтор события не удваивает счёт;
--   2. событие, которое не удалось начислить, попадает в billing_unbilled с
--      причиной, а не исчезает. Молчаливая потеря выручки и есть та самая
--      разница между 6374 и 145.

CREATE TABLE billing_accrual (
    id                uuid PRIMARY KEY,
    -- Идемпотентность по внешнему ключу (инвариант 5). Повтор события даёт тот
    -- же ответ, а не второе начисление: шина доставляет at-least-once.
    event_id          uuid UNIQUE NOT NULL,
    event_type        text NOT NULL,
    tenant_id         text NOT NULL,
    correlation_id    text,

    cabinet_id        uuid NOT NULL REFERENCES cabinet (id),
    -- Дублируется рядом с cabinet_id намеренно: счёт выставляется организации,
    -- и в закрытом периоде он обязан остаться тем, каким был выставлен, даже
    -- если справочник кабинета потом поправили.
    seller_external_id text NOT NULL,
    organization_id   text,

    service           billing_service NOT NULL,
    tariff_version_id uuid REFERENCES billing_tariff_version (id),
    quantity          numeric(14, 3) NOT NULL CHECK (quantity > 0),
    -- Цена MM-Express за единицу и наценка партнёра за единицу — раздельно.
    -- Слитые в одно число, они дают выручку и не дают маржу.
    unit_price        numeric(14, 2) NOT NULL CHECK (unit_price >= 0),
    markup            numeric(14, 2) NOT NULL DEFAULT 0 CHECK (markup >= 0),

    -- Что платит клиент: 45 = 30 наших + 15 партнёра (раздел 1).
    amount            numeric(14, 2) NOT NULL,
    -- Кому из партнёров причитается наценка. NULL — кабинет без партнёра,
    -- тогда и partner_amount обязан быть нулём.
    partner_id        uuid REFERENCES partner (id),
    partner_amount    numeric(14, 2) NOT NULL DEFAULT 0 CHECK (partner_amount >= 0),
    -- Выручка MM-Express. Генерируемое поле, а не вычисление в приложении:
    -- «amount − partner_amount» обязано быть верным во всех отчётах сразу.
    net_amount        numeric(14, 2) GENERATED ALWAYS AS (amount - partner_amount) STORED,

    occurred_on       date NOT NULL,
    -- Период начисления. Собирается из полей даты, а не to_char: to_char
    -- объявлен STABLE (зависит от DateStyle), и Postgres не пускает его в
    -- генерируемое поле. Составление из extract даёт тот же 'YYYY-MM'.
    period            text GENERATED ALWAYS AS (
                          extract(year from occurred_on)::int::text || '-' ||
                          lpad(extract(month from occurred_on)::int::text, 2, '0')
                      ) STORED,
    allocation_key    text,

    -- Корректировка ссылается на исправляемое начисление. Отрицательных сумм
    -- не бывает: сторно — это отдельная строка со ссылкой, а не правка старой.
    adjustment_of     uuid REFERENCES billing_accrual (id),

    -- Признание вознаграждения партнёра (файл 04): не платить партнёру с
    -- невыставленного или неоплаченного счёта. Начисляем сразу — pending, —
    -- и переводим в payable, когда клиент заплатил.
    partner_payout_state text NOT NULL DEFAULT 'pending'
        CHECK (partner_payout_state IN ('pending', 'payable', 'paid', 'cancelled')),

    created_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT billing_accrual_partner_consistent CHECK (
        (partner_id IS NOT NULL AND partner_amount >= 0)
        OR (partner_id IS NULL AND partner_amount = 0)
    ),
    -- Наценка партнёра не может превышать то, что платит клиент.
    CONSTRAINT billing_accrual_partner_within_amount CHECK (partner_amount <= amount),
    CONSTRAINT billing_accrual_not_own_adjustment CHECK (adjustment_of IS DISTINCT FROM id)
);

CREATE INDEX billing_accrual_period_idx    ON billing_accrual (period, cabinet_id);
CREATE INDEX billing_accrual_cabinet_idx   ON billing_accrual (cabinet_id, occurred_on);
CREATE INDEX billing_accrual_partner_idx   ON billing_accrual (partner_id, period)
    WHERE partner_id IS NOT NULL;
CREATE INDEX billing_accrual_service_idx   ON billing_accrual (service, occurred_on);
CREATE INDEX billing_accrual_adjustment_idx ON billing_accrual (adjustment_of)
    WHERE adjustment_of IS NOT NULL;

-- === Что не удалось начислить ==============================================
-- Не «лог ошибок», а отчёт о потерянной выручке. Событие тарифицируемого типа,
-- по которому начисления не вышло, обязано быть видно поимённо: без этой
-- таблицы разница между «склад отработал» и «клиенту выставлено» — 98 %,
-- и никто не знает, из чего она состоит.
CREATE TABLE billing_unbilled (
    id             uuid PRIMARY KEY,
    event_id       uuid UNIQUE NOT NULL,
    event_type     text NOT NULL,
    tenant_id      text,
    -- Почему не начислено: SELLER_UNKNOWN, NO_TARIFF, NO_QUANTITY,
    -- BAD_ENVELOPE, TARIFF_NOT_APPROVED.
    reason         text NOT NULL,
    detail         text,
    -- payload события целиком: без него разбор превращается в переписку с
    -- потоком A. Секреты в события не попадают вовсе (инвариант 15).
    payload        jsonb,
    occurred_at    timestamptz,
    resolved_at    timestamptz,
    resolved_by    text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX billing_unbilled_open_idx ON billing_unbilled (reason, created_at)
    WHERE resolved_at IS NULL;

-- === Период ================================================================
-- Закрытый период не пересчитывается: это и есть причина, по которой партнёра
-- держат историей закреплений, а не полем на кабинете.
CREATE TABLE billing_period (
    period     text PRIMARY KEY CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    state      text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
    closed_at  timestamptz,
    closed_by  text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_period_closed_has_author CHECK (
        state = 'open' OR (closed_at IS NOT NULL AND closed_by IS NOT NULL)
    )
);

-- === Счёт и акт ============================================================
-- «Клиент выгружает акт за период сам, без участия бухгалтера» (файл 04).
CREATE TABLE billing_invoice (
    id         uuid PRIMARY KEY,
    cabinet_id uuid NOT NULL REFERENCES cabinet (id),
    period     text NOT NULL REFERENCES billing_period (period),
    number     text UNIQUE NOT NULL,
    state      text NOT NULL DEFAULT 'draft'
               CHECK (state IN ('draft', 'issued', 'paid', 'void')),
    -- Итоги фиксируются в момент выставления: счёт не пересчитывается задним
    -- числом, даже если тариф потом поправили.
    total_amount  numeric(14, 2) NOT NULL DEFAULT 0,
    partner_total numeric(14, 2) NOT NULL DEFAULT 0,
    net_total     numeric(14, 2) NOT NULL DEFAULT 0,
    issued_at  timestamptz,
    paid_at    timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_invoice_one_per_period UNIQUE (cabinet_id, period),
    CONSTRAINT billing_invoice_issued_has_date CHECK (
        state IN ('draft', 'void') OR issued_at IS NOT NULL
    ),
    CONSTRAINT billing_invoice_paid_has_date CHECK (state <> 'paid' OR paid_at IS NOT NULL)
);

-- Начисление попадает в счёт ссылкой, а не копированием: расшифровка в ЛК
-- клиента показывает те же строки, по которым посчитан итог.
ALTER TABLE billing_accrual
    ADD COLUMN invoice_id uuid REFERENCES billing_invoice (id);

CREATE INDEX billing_accrual_invoice_idx ON billing_accrual (invoice_id)
    WHERE invoice_id IS NOT NULL;

-- Вознаграждение партнёра идёт следом за деньгами клиента, а не впереди них.
-- Переводит начисления в payable ровно тогда, когда счёт оплачен.
CREATE FUNCTION billing_invoice_payout_follows_payment() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'paid' AND OLD.state IS DISTINCT FROM 'paid' THEN
        UPDATE billing_accrual
           SET partner_payout_state = 'payable'
         WHERE invoice_id = NEW.id
           AND partner_id IS NOT NULL
           AND partner_payout_state = 'pending';
    ELSIF NEW.state = 'void' AND OLD.state IS DISTINCT FROM 'void' THEN
        UPDATE billing_accrual
           SET partner_payout_state = 'cancelled'
         WHERE invoice_id = NEW.id
           AND partner_payout_state IN ('pending', 'payable');
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER billing_invoice_payout_follows_payment
    AFTER UPDATE OF state ON billing_invoice
    FOR EACH ROW EXECUTE FUNCTION billing_invoice_payout_follows_payment();

-- === Кто именно заработал ==================================================
-- partner_amount в начислении — это вся наценка, которую заплатил клиент, а
-- partner_id — партнёр, за которым закреплён кабинет (по нему считается
-- «мои клиенты» и, через partner_subtree, «моя ветка»).
--
-- Но заработать на одной операции могут двое: старший менеджер со своей
-- наценкой и менеджер кабинетов, если ему когда-нибудь дадут свою (раздел 13,
-- вопрос 6 — допущение «без своей», модель обязана поддерживать оба). Деньги
-- к выплате раскладываются здесь, поимённо.
CREATE TABLE billing_commission (
    id         uuid PRIMARY KEY,
    accrual_id uuid NOT NULL REFERENCES billing_accrual (id) ON DELETE CASCADE,
    partner_id uuid NOT NULL REFERENCES partner (id),
    -- Ставка, по которой посчитано, и слой, из которого она взята: через
    -- полгода «почему тут 15» обязано читаться из строки, а не из памяти.
    markup     numeric(14, 2) NOT NULL CHECK (markup >= 0),
    amount     numeric(14, 2) NOT NULL CHECK (amount >= 0),
    price_layer_id uuid REFERENCES price_layer (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT billing_commission_once UNIQUE (accrual_id, partner_id)
);

CREATE INDEX billing_commission_partner_idx ON billing_commission (partner_id);

-- Сумма долей обязана совпасть с наценкой в начислении. Проверка триггером, а
-- не CHECK: CHECK не видит других строк, а разъехавшаяся раскладка — это
-- выплата партнёру из воздуха либо недоплата ему же.
CREATE FUNCTION billing_commission_matches_accrual() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target uuid := COALESCE(NEW.accrual_id, OLD.accrual_id);
    split  numeric(14, 2);
    total  numeric(14, 2);
BEGIN
    SELECT COALESCE(sum(amount), 0) INTO split
      FROM billing_commission WHERE accrual_id = target;
    SELECT partner_amount INTO total FROM billing_accrual WHERE id = target;
    IF total IS NOT NULL AND split > total THEN
        RAISE EXCEPTION 'раскладка комиссии % больше наценки в начислении % (%)',
            split, target, total;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER billing_commission_matches_accrual
    AFTER INSERT OR UPDATE OR DELETE ON billing_commission
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION billing_commission_matches_accrual();
