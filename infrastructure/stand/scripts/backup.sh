#!/usr/bin/env bash
# Ежедневный дамп всех баз стенда с ротацией.
#
# Правило 8 проекта: ничего не удаляем в рабочей базе без pg_dump. Скрипт
# делает так, чтобы бэкап существовал ДО того, как понадобится, а не после.
set -uo pipefail

KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
OUT=/backups

while true; do
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    databases="$(psql -h postgres -U wms -d postgres -tAc \
        "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'")"
    for db in $databases; do
        if pg_dump -h postgres -U wms -d "$db" 2>/dev/null | gzip > "${OUT}/${stamp}-${db}.sql.gz"; then
            echo "$(date -u +%FT%TZ) дамп ${db} готов"
        else
            # Неудачный дамп не должен притворяться удачным: пустой файл выглядит
            # как бэкап ровно до того дня, когда из него надо восстановиться.
            rm -f "${OUT}/${stamp}-${db}.sql.gz"
            echo "$(date -u +%FT%TZ) ДАМП ${db} НЕ УДАЛСЯ" >&2
        fi
    done

    deleted="$(find "$OUT" -name '*.sql.gz' -mtime "+${KEEP_DAYS}" -delete -print | wc -l)"
    [ "$deleted" -gt 0 ] && echo "$(date -u +%FT%TZ) удалено старых дампов: ${deleted}"

    sleep 86400
done
