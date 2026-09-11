#!/usr/bin/env bash
# Роль Postgres на сервис, а не один суперпользователь на всех.
#
# Все девять сервисов ходили в базу под `wms` — владельцем ВСЕХ баз стенда.
# Это значит, что ошибка в запросе портала может уронить таблицу склада, а
# утёкший пароль биллинга открывает всё сразу. Разделение ролей не мешает
# ничему: у каждого сервиса своя база (раздел 2.2), и права ему нужны ровно
# в ней.
#
# Скрипт идемпотентен: роль есть — пароль обновляется, права выдаются заново.
# Пароли приходят снаружи Git, по одному на сервис.
set -euo pipefail

PSQL=(psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-wms}" --dbname postgres)

# сервис:база — ровно те пары, что описаны в compose.
SERVICES="
wms:wms
wms:wms_test
workstation:workstation
workstation:workstation_test
billing:billing
billing:billing_test
identity:identity
identity:identity_test
portal:portal
portal:portal_test
admin:internal_admin
admin:internal_admin_test
"

granted=0
for pair in $SERVICES; do
    service="${pair%%:*}"
    database="${pair##*:}"

    # Пароль роли: SERVICE_DB_PASSWORD_<СЕРВИС>. Нет — роль пропускается, и
    # об этом говорится вслух: молча оставить сервис под суперпользователем
    # значит не сделать ничего, но выглядеть сделанным.
    variable="SERVICE_DB_PASSWORD_$(printf '%s' "$service" | tr 'a-z' 'A-Z')"
    password="${!variable:-}"
    if [ -z "$password" ]; then
        echo "  ПРОПУЩЕНО: ${variable} не задан, ${service} останется под суперпользователем" >&2
        continue
    fi

    "${PSQL[@]}" <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${service}') THEN
        CREATE ROLE ${service} LOGIN PASSWORD '${password}';
    ELSE
        ALTER ROLE ${service} WITH LOGIN PASSWORD '${password}';
    END IF;
END
\$\$;
GRANT CONNECT ON DATABASE ${database} TO ${service};
SQL

    # Права внутри базы выдаются в ней самой. `ALTER DEFAULT PRIVILEGES` —
    # чтобы таблица, созданная следующей миграцией, тоже была доступна: без
    # этого сервис падает на первом же новом объекте.
    psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-wms}" --dbname "${database}" <<SQL
GRANT USAGE ON SCHEMA public TO ${service};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ${service};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ${service};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ${service};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO ${service};
SQL
    granted=$((granted + 1))
    echo "  ${service} → ${database}"
done

echo "ролей настроено: ${granted}"
