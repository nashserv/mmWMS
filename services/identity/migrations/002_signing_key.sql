-- Ключ подписи токенов. Раздел 12 мастера: `wms` доверяет JWT по JWKS, а
-- значит кто-то обязан эти JWT подписывать.
--
-- Ключ лежит в базе, а не генерируется при старте, по одной причине: иначе
-- перезапуск identity обесценивает все выданные токены, и смена на складе
-- встаёт на ровном месте. В бою ключ приходит переменной окружения из
-- секрет-провайдера; таблица — для стенда и для того, чтобы ротация была
-- операцией с историей, а не заменой файла.
--
-- Приватная часть здесь хранится только на стенде. В защищённых средах
-- identity стартует с ключом из окружения и в таблицу ничего не пишет:
-- проверяется это кодом при старте (fail-closed).

CREATE TABLE identity_signing_key (
    kid         text PRIMARY KEY,
    -- Ed25519: короткие ключи, быстрая проверка, нет параметров, которые можно
    -- выбрать неправильно. RSA оставлен не был бы лучше — только длиннее.
    algorithm   text NOT NULL DEFAULT 'EdDSA' CHECK (algorithm = 'EdDSA'),
    private_pem text NOT NULL,
    public_pem  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    -- Ротация: старый ключ остаётся в JWKS, пока живы выданные им токены,
    -- но новые подписываются уже новым.
    retired_at  timestamptz,
    CONSTRAINT identity_signing_key_pem_is_not_a_token
        CHECK (private_pem LIKE '-----BEGIN%')
);

-- Действующий ключ ровно один: два «текущих» — это вопрос «каким подписывать»,
-- на который нет ответа.
CREATE UNIQUE INDEX identity_signing_key_active_idx
    ON identity_signing_key ((retired_at IS NULL)) WHERE retired_at IS NULL;

COMMENT ON TABLE identity_signing_key IS
    'Ключи подписи JWT. Публичная часть отдаётся по /.well-known/jwks.json.';
