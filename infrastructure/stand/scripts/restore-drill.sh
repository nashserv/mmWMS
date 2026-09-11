#!/usr/bin/env bash
# Учение: доказать, что из бэкапа можно вернуться. Гоняется, пока не понадобилось.
#
# Дамп, из которого ни разу не восстанавливались, — это надежда, а не бэкап.
# Учение разворачивает САМЫЙ СВЕЖИЙ дамп рабочей базы в отдельную, догоняет её
# миграциями до текущей схемы и сносит. Рабочую базу оно не трогает вовсе.
#
# Красное учение означает одно из двух, и оба надо знать заранее, а не в день
# аварии: дамп не разворачивается, либо схема дампа разошлась с кодом.

set -uo pipefail

STAND="$(cd "$(dirname "$0")/.." && pwd)"
DB="${1:-wms}"
PROBE="${DB}_restore_drill"
IMAGE="${POSTGRES_IMAGE:-postgres:16.10-alpine3.22}"
NETWORK="${STAND_NETWORK:-mmx-stand_default}"

# shellcheck disable=SC1091
set -a; . "${STAND}/.env"; set +a
: "${POSTGRES_PASSWORD:?нужен POSTGRES_PASSWORD в infrastructure/stand/.env}"

latest="$(ls -1t "${STAND}/backups/"*"-${DB}.sql.gz" 2>/dev/null | head -1)"
if [ -z "$latest" ]; then
    echo "УЧЕНИЕ НЕ ПРОШЛО: дампов базы ${DB} нет вовсе" >&2
    exit 1
fi
echo "учение: ${latest##*/} → ${PROBE}"

run() { docker run --rm --network "$NETWORK" \
        -v "${STAND}:/stand" -v "${STAND}/../../services:/services:ro" \
        -w /stand -e POSTGRES_PASSWORD -e POSTGRES_HOST=postgres \
        --entrypoint bash "$IMAGE" "$@"; }

cleanup() {
    docker exec mmx-stand-postgres-1 psql -U wms -d postgres -q \
        -c "DROP DATABASE IF EXISTS ${PROBE}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cleanup
if ! run scripts/restore.sh "backups/${latest##*/}" "$PROBE"; then
    echo "УЧЕНИЕ НЕ ПРОШЛО: дамп не разворачивается" >&2
    exit 1
fi

# Догоняем восстановленную базу до текущей схемы. Это и есть вторая половина
# проверки: бэкап, из которого нельзя выйти на сегодняшний код, бесполезен.
out="$(docker run --rm --network "$NETWORK" \
        -v "${STAND}/scripts/migrate.sh:/migrate.sh:ro" \
        -v "${STAND}/../../services/${DB%%_*}/migrations:/migrations:ro" \
        -e DATABASE_URL="postgresql://wms:${POSTGRES_PASSWORD}@postgres:5432/${PROBE}" \
        --entrypoint bash "$IMAGE" /migrate.sh 2>&1)" || {
    echo "$out" >&2
    echo "УЧЕНИЕ НЕ ПРОШЛО: миграции не легли на восстановленную базу" >&2
    exit 1
}
echo "  ${out##*$'\n'}"
echo "учение пройдено: из дампа ${DB} можно вернуться и догнать схему"
