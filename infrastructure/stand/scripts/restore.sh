#!/usr/bin/env bash
# Восстановление базы из дампа. Обратная дорога к backup.sh.
#
# Дампы делались с первого дня, а восстановиться из них не пробовал никто.
# Непроверенный бэкап бэкапом не является: узнать, что он не разворачивается,
# можно ровно в тот день, когда он понадобился. Поэтому скрипт не только
# восстанавливает, но и ПРОВЕРЯЕТ результат — схемой и данными.
#
#   restore.sh <дамп.sql.gz> <база> [--force]
#
# Без `--force` скрипт откажется писать в базу, в которой есть таблицы. Это не
# перестраховка: «восстановить не туда» — это потерять рабочие данные, пытаясь
# спасти другие. Ошибиться легко, потому что имена похожи: wms и wms_test.
#
# Код возврата: 0 — восстановлено и проверено, 1 — отказ или расхождение.

set -uo pipefail

DUMP="${1:-}"
TARGET="${2:-}"
FORCE="${3:-}"

if [ -z "$DUMP" ] || [ -z "$TARGET" ]; then
    echo "как звать: restore.sh <дамп.sql.gz> <база> [--force]" >&2
    exit 1
fi

: "${POSTGRES_PASSWORD:?нужен POSTGRES_PASSWORD из infrastructure/stand/.env}"
HOST="${POSTGRES_HOST:-127.0.0.1}"
PORT="${POSTGRES_PORT:-5432}"
USER="${POSTGRES_USER:-wms}"
ADMIN="postgresql://${USER}:${POSTGRES_PASSWORD}@${HOST}:${PORT}/postgres"
INTO="postgresql://${USER}:${POSTGRES_PASSWORD}@${HOST}:${PORT}/${TARGET}"

if [ ! -f "$DUMP" ]; then
    echo "дампа ${DUMP} нет" >&2
    exit 1
fi

# Пустой дамп выглядит как дамп ровно до того дня, когда из него надо
# восстановиться. backup.sh такие удаляет, но дамп мог приехать и не от него.
if [ ! -s "$DUMP" ]; then
    echo "дамп ${DUMP} пуст — восстанавливать нечего" >&2
    exit 1
fi

psql_into() { psql "$INTO" -v ON_ERROR_STOP=1 -tAq "$@"; }

echo "восстановление ${DUMP} → ${TARGET}"

exists="$(psql "$ADMIN" -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${TARGET}'" 2>/dev/null || true)"
if [ "$exists" != "1" ]; then
    echo "  базы ${TARGET} нет — создаю"
    psql "$ADMIN" -q -c "CREATE DATABASE \"${TARGET}\"" || exit 1
else
    tables="$(psql_into -c \
        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'" 2>/dev/null || echo 0)"
    if [ "${tables:-0}" -gt 0 ] && [ "$FORCE" != "--force" ]; then
        echo "  ОТКАЗ: в базе ${TARGET} уже ${tables} таблиц." >&2
        echo "  Восстановление затрёт их. Если это и нужно — повторите с --force." >&2
        echo "  Перед этим стоит снять дамп самой ${TARGET}: терять данные, спасая" >&2
        echo "  другие, — самый обидный способ их потерять." >&2
        exit 1
    fi
    if [ "${tables:-0}" -gt 0 ]; then
        echo "  --force: сношу схему public в ${TARGET} (${tables} таблиц)"
        psql_into -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null || exit 1
    fi
fi

# Дамп разворачивается ОДНОЙ транзакцией: половина восстановленной базы хуже,
# чем пустая, — она выглядит рабочей.
# `-o /dev/null` глушит вывод самих запросов дампа (`setval` и прочее): он
# ничего не говорит о результате, а прячет строки, которые говорят.
if ! gzip -dc "$DUMP" | psql "$INTO" -v ON_ERROR_STOP=1 --single-transaction -q -o /dev/null; then
    echo "  ВОССТАНОВЛЕНИЕ НЕ УДАЛОСЬ" >&2
    exit 1
fi

echo "  развёрнуто, проверяю"

# --- проверка 1: схема на месте --------------------------------------------
tables="$(psql_into -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
if [ "${tables:-0}" -eq 0 ]; then
    echo "  ПУСТО: ни одной таблицы. Дамп развернулся, но в нём ничего нет." >&2
    exit 1
fi
echo "  таблиц: ${tables}"

# --- проверка 2: журнал миграций сходится с репозиторием ---------------------
#
# Главное, ради чего проверка существует. Восстановленная база обязана быть той
# же схемы, что и код рядом: иначе сервис поднимется и начнёт падать на первом
# же запросе к колонке, которой в дампе не было.
applied="$(psql_into -c \
    "SELECT count(*) FROM schema_migrations" 2>/dev/null || echo "нет")"
if [ "$applied" = "нет" ]; then
    echo "  в дампе нет schema_migrations: это база не нашей схемы или дамп до неё" >&2
else
    echo "  миграций в журнале: ${applied}"
fi

# --- проверка 3: данные, а не только структура ------------------------------
rows="$(psql_into -c "
    SELECT COALESCE(sum(n_live_tup), 0) FROM pg_stat_user_tables
")"
echo "  строк (оценка): ${rows}"

echo "восстановлено: ${TARGET}"
echo
echo "Дальше — накатить миграции на восстановленную базу:"
echo "  DATABASE_URL='${INTO//${POSTGRES_PASSWORD}/*****}' MIGRATIONS_DIR=... ./migrate.sh"
echo "Ответ «миграции применены: 0» означает, что схема дампа совпадает с кодом."
