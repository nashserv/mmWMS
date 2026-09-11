#!/usr/bin/env bash
# Накатывает миграции по порядку имён и запоминает применённое.
#
# Существующие миграции не переписываются (правило 9.5) — только добавляются
# новые. Поэтому применение идёт по журналу, а не «накатить всё заново».
#
# Три свойства, ради которых скрипт написан именно так:
#
#   1. Один накатывающий за раз (`pg_advisory_lock`). Два контейнера миграций,
#      стартовавшие одновременно — обычное дело при `docker compose up`, — без
#      блокировки накатывают один и тот же файл дважды: второй падает посреди
#      `CREATE TABLE`, и журнал расходится с базой.
#   2. Контрольная сумма файла в журнале. Применённая миграция обязана
#      остаться той же: правка уже накатанного файла означает, что стенд и
#      бой разошлись схемой, а по журналу это не видно — имя-то прежнее.
#   3. Имя файла экранируется переменной psql (`:'name'`), а не вклеивается
#      в строку запроса. Файл с кавычкой в имени — редкость, а SQL-инъекция
#      из имени файла в скрипте, который запускают от владельца базы, — это
#      конец разговора. Переменные psql подставляются только в том, что
#      приходит из файла или со stdin: `-c` их не разбирает, поэтому запросы
#      идут через stdin.

set -euo pipefail

DATABASE_URL="${DATABASE_URL:?set outside Git}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-/migrations}"
# Номер блокировки — произвольный, но один на все базы: важно лишь, чтобы два
# накатывающих выбрали одно и то же число.
LOCK_ID="${MIGRATE_LOCK_ID:-8412771}"

psql_do() { psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q "$@"; }

until pg_isready -d "$DATABASE_URL" >/dev/null 2>&1; do
    echo "жду postgres..."
    sleep 1
done

psql_do -c "CREATE TABLE IF NOT EXISTS schema_migrations (
                filename text PRIMARY KEY,
                checksum text,
                applied_at timestamptz NOT NULL DEFAULT now());"
# Колонка добавляется отдельно: у стендов, заведённых до этой версии, таблица
# уже есть, и `CREATE TABLE IF NOT EXISTS` её не тронет.
psql_do -c "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum text;"

shopt -s nullglob
applied=0
drift=0

for file in $(printf '%s\n' "$MIGRATIONS_DIR"/*.sql | sort); do
    name="$(basename "$file")"
    sum="$(sha256sum "$file" | cut -d' ' -f1)"

    # Имя подставляется переменной psql (`:'name'`), а не строкой в запрос:
    # `:'…'` экранирует значение так же, как `quote_literal`.
    known="$(psql "$DATABASE_URL" -tA -v ON_ERROR_STOP=1 -v name="$name" <<'SQL'
SELECT COALESCE(checksum, '') FROM schema_migrations WHERE filename = :'name';
SQL
)"

    if [ -n "$known" ]; then
        if [ "$known" != "$sum" ] && [ "$known" != "" ]; then
            echo "РАСХОЖДЕНИЕ: ${name} применён с другой контрольной суммой" >&2
            echo "  в журнале: ${known}" >&2
            echo "  на диске:  ${sum}" >&2
            echo "  Применённая миграция не переписывается (правило 9.5): схема" >&2
            echo "  базы и файл разошлись, и по имени это не видно." >&2
            drift=1
        fi
        continue
    fi
    # Пустая сумма в журнале — миграция накатана прежней версией скрипта.
    # Дописываем сумму, не накатывая повторно.
    accounted="$(psql "$DATABASE_URL" -tA -v name="$name" <<'SQL'
SELECT 1 FROM schema_migrations WHERE filename = :'name';
SQL
)"
    if [ "$accounted" = "1" ]; then
        psql_do -v name="$name" -v sum="$sum" <<'SQL'
UPDATE schema_migrations SET checksum = :'sum' WHERE filename = :'name';
SQL
        continue
    fi

    echo "применяю ${name}"
    # Блокировка берётся на время применения ОДНОГО файла и отпускается
    # коммитом: `pg_advisory_xact_lock` живёт ровно транзакцию.
    #
    # Миграция и отметка о ней — той же транзакцией: иначе после падения
    # посреди файла журнал разойдётся с базой.
    # Блокировка, сам файл и отметка — одной транзакцией через stdin:
    # `\i` вставляет файл, а переменные psql здесь разбираются.
    psql_do --single-transaction \
        -v name="$name" -v sum="$sum" -v lock="$LOCK_ID" -v file="$file" <<'SQL'
SELECT pg_advisory_xact_lock(:'lock'::bigint);
-- Путь БЕЗ кавычек: `\i` берёт аргумент как есть, и `:'file'` передал бы
-- ему имя вместе с кавычками. Путь наш собственный и пробелов не содержит;
-- имя файла, которое идёт в SQL, экранируется отдельно (`:'name'`).
\i :file
INSERT INTO schema_migrations (filename, checksum)
VALUES (:'name', :'sum')
ON CONFLICT (filename) DO UPDATE SET checksum = EXCLUDED.checksum;
SQL
    applied=$((applied + 1))
done

if [ "$drift" -ne 0 ]; then
    echo "миграции применены: ${applied}, но есть расхождение контрольных сумм" >&2
    exit 1
fi

echo "миграции применены: ${applied}"
