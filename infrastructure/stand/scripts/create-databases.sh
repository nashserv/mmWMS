#!/usr/bin/env bash
# База на сервис (раздел 2.2 мастера) — один инстанс Postgres, много баз.
set -euo pipefail
# wms_test — отдельная база для тестов сервиса. На стенде рядом с ними крутятся
# настоящие воркеры (опрос WB, стикеры, сверка): против общей базы тест и воркер
# разбирают одни и те же задания, и тест краснеет «завёл 3 из 5», хотя сломан не
# он. В CI такой базы нет — там Postgres поднимается на прогон и воркеров нет.
for db in wb_gateway workstation catalog billing returns portal internal_admin identity wms_test; do
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
        -c "CREATE DATABASE ${db} OWNER ${POSTGRES_USER};"
done
echo "базы созданы"
