#!/usr/bin/env bash
# База на сервис (раздел 2.2 мастера) — один инстанс Postgres, много баз.
set -euo pipefail
for db in wb_gateway workstation catalog billing returns portal internal_admin identity; do
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
        -c "CREATE DATABASE ${db} OWNER ${POSTGRES_USER};"
done
echo "базы созданы"
