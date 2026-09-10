#!/usr/bin/env bash
# Накатывает миграции по порядку имён и запоминает применённое.
#
# Существующие миграции не переписываются (правило 9.5) — только добавляются
# новые. Поэтому применение идёт по журналу, а не «накатить всё заново».

set -euo pipefail

DATABASE_URL="${DATABASE_URL:?set outside Git}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-/migrations}"

psql_do() { psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q "$@"; }

until pg_isready -d "$DATABASE_URL" >/dev/null 2>&1; do
    echo "жду postgres..."
    sleep 1
done

psql_do -c "CREATE TABLE IF NOT EXISTS schema_migrations (
                filename text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now());"

shopt -s nullglob
applied=0
for file in $(printf '%s\n' "$MIGRATIONS_DIR"/*.sql | sort); do
    name="$(basename "$file")"
    known="$(psql "$DATABASE_URL" -tAc \
        "SELECT 1 FROM schema_migrations WHERE filename = '${name//\'/\'\'}'")"
    if [ "$known" = "1" ]; then
        continue
    fi
    echo "применяю ${name}"
    # Миграция и отметка о ней — одной транзакцией: иначе после падения
    # посреди файла журнал разойдётся с базой.
    psql_do --single-transaction \
        -f "$file" \
        -c "INSERT INTO schema_migrations (filename) VALUES ('${name//\'/\'\'}');"
    applied=$((applied + 1))
done

echo "миграции применены: ${applied}"
