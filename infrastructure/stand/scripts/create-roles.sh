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

# ИМЯ РОЛИ — С ПРЕФИКСОМ `svc_`, и это не стиль.
#
# Владелец всех баз стенда зовётся `wms` (POSTGRES_USER). Роль для сервиса
# склада, названная просто `wms`, — это тот же самый роль-объект: ветка
# `ALTER ROLE ... PASSWORD` переписала бы пароль СУПЕРПОЛЬЗОВАТЕЛЯ на
# SERVICE_DB_PASSWORD_WMS, и всё, что ходит по POSTGRES_PASSWORD — миграции,
# сиды, бэкап, тесты, — перестало бы подключаться. Скрипт при этом отработал
# бы «успешно».
#
# Префикс делает роль сервиса отдельным объектом при любом POSTGRES_USER.

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
    role="svc_${service}"

    # Роль сервиса не имеет права совпасть с владельцем баз: иначе скрипт
    # молча меняет пароль суперпользователя.
    if [ "$role" = "${POSTGRES_USER:-wms}" ]; then
        echo "  ОТКАЗ: роль ${role} совпадает с владельцем баз" >&2
        exit 1
    fi

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
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${role}') THEN
        CREATE ROLE ${role} LOGIN PASSWORD '${password}';
    ELSE
        ALTER ROLE ${role} WITH LOGIN PASSWORD '${password}';
    END IF;
END
\$\$;
GRANT CONNECT ON DATABASE ${database} TO ${role};
SQL

    # Права внутри базы выдаются в ней самой. `ALTER DEFAULT PRIVILEGES` —
    # чтобы таблица, созданная следующей миграцией, тоже была доступна: без
    # этого сервис падает на первом же новом объекте.
    psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-wms}" --dbname "${database}" <<SQL
GRANT USAGE ON SCHEMA public TO ${role};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ${role};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ${role};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ${role};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO ${role};
SQL
    granted=$((granted + 1))
    echo "  ${role} → ${database}"
done

echo "ролей настроено: ${granted}"
