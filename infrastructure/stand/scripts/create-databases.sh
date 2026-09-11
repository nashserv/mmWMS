#!/usr/bin/env bash
# База на сервис (раздел 2.2 мастера) — один инстанс Postgres, много баз.
#
# Идемпотентен и вызывается не только при инициализации тома: как
# docker-entrypoint-initdb.d он отрабатывает ровно один раз в жизни тома, а
# новая база нужна и потом — например, `wms_test` появилась через неделю после
# того, как том был создан, и «правильный путь» из README не работал ни у кого.
set -euo pipefail

PSQL=(psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-wms}" --dbname postgres)

# `*_test` — отдельные базы для юнит-тестов. Рядом с рабочими крутятся
# настоящие воркеры: против общей базы тест и воркер разбирают одни и те же
# задания, и тест краснеет там, где сломан не он. А ещё каждый прогон
# оставляет в базе своего клиента и свой кабинет — так в `wms` натекло 971
# владельца из 997 (`docs/schema-decisions.md`).
#
# Базы тестов заводятся ЗДЕСЬ, а не conftest'ом: тест, который заводит себе
# базу сам, делает это правом, которого у него в CI может не быть, — и
# краснеет не своей причиной.
for db in wb_gateway workstation workstation_test catalog billing returns \
          portal internal_admin identity wms_test billing_test \
          identity_test portal_test internal_admin_test; do
    if "${PSQL[@]}" -tAc "SELECT 1 FROM pg_database WHERE datname = '${db}'" | grep -q 1; then
        echo "база ${db} уже есть"
        continue
    fi
    "${PSQL[@]}" -c "CREATE DATABASE ${db} OWNER ${POSTGRES_USER:-wms};"
    echo "база ${db} создана"
done
echo "базы на месте"
