#!/usr/bin/env bash
# Снятие обезличенного дампа с боевого контура для стенда (пункт 7 файла 01).
#
# ЗАПУСКАЕТСЯ ЧЕЛОВЕКОМ С ДОСТУПОМ, вручную, осознанно. Не вызывается из
# compose и не является частью поднятия стенда.
#
# ГЛАВНОЕ РЕШЕНИЕ: обезличивание происходит В ЗАПРОСЕ, при выгрузке. Файла с
# живыми токенами не возникает на диске ни на секунду — иначе он останется в
# истории файловой системы, в бэкапе ноутбука и однажды в Git. Поэтому здесь
# нет шага «выгрузили, потом почистили».
#
# Боевой сервер 91.108.239.175 — ТОЛЬКО ЧТЕНИЕ (раздел 12 мастера). Скрипт не
# выполняет ни одного INSERT/UPDATE/DELETE и открывает транзакцию READ ONLY.
#
# Использование:
#   PROD_DSN_GATEWAY=postgresql://readonly@91.108.239.175:5432/wb_gateway \
#   PROD_DSN_CATALOG=postgresql://readonly@91.108.239.175:5432/catalog \
#   PROD_DSN_BILLING=postgresql://readonly@91.108.239.175:5432/billing \
#   ./dump-from-prod.sh
#
# Результат — CSV в ../dump/, который затем проверяется теми же воротами,
# что и стенд (check-no-live-tokens.sh). Не прошло — дамп удаляется.

set -euo pipefail

OUT_DIR="${OUT_DIR:-$(cd "$(dirname "$0")/.." && pwd)/dump}"
TASK_WINDOW_DAYS="${TASK_WINDOW_DAYS:-7}"

PROD_DSN_GATEWAY="${PROD_DSN_GATEWAY:?set outside Git}"
PROD_DSN_CATALOG="${PROD_DSN_CATALOG:?set outside Git}"
PROD_DSN_BILLING="${PROD_DSN_BILLING:?set outside Git}"

mkdir -p "$OUT_DIR"

# READ ONLY на уровне транзакции: даже опечатка в запросе не сможет ничего
# записать в боевую базу.
export_csv() {
    local dsn="$1" target="$2" query="$3"
    echo "выгружаю ${target}"
    psql "$dsn" -v ON_ERROR_STOP=1 --quiet \
        -c "BEGIN TRANSACTION READ ONLY;" \
        -c "\\copy (${query}) TO '${OUT_DIR}/${target}' WITH (FORMAT csv, HEADER true)" \
        -c "COMMIT;"
}

# --------------------------------------------------------------- кабинеты
# secret_ref и всё, что может оказаться токеном, не покидает боевую базу:
# в выгрузку идёт синтетическая заглушка, привязанная к номеру строки.
export_csv "$PROD_DSN_GATEWAY" "sellers.csv" "
    SELECT id,
           name,
           internal_number,
           status,
           created_at
      FROM sellers
     ORDER BY created_at
"

export_csv "$PROD_DSN_GATEWAY" "wb_accounts.csv" "
    SELECT id,
           seller_id,
           display_name,
           external_id,
           'test-token-' || row_number() OVER (ORDER BY created_at) AS secret_ref,
           scopes,
           mode,
           status,
           created_at
      FROM wb_accounts
     ORDER BY created_at
"

# --------------------------------------------------------------- каталог
export_csv "$PROD_DSN_CATALOG" "catalog_product.csv" "
    SELECT *
      FROM catalog_product
     ORDER BY 1
"

export_csv "$PROD_DSN_CATALOG" "catalog_mapping.csv" "
    SELECT *
      FROM catalog_mapping
     ORDER BY 1
"

# --------------------------------------------------------------- задания
# Неделя — ради реалистичного объёма при тестах, а не ради полноты истории.
export_csv "$PROD_DSN_GATEWAY" "wb_assembly_tasks.csv" "
    SELECT id,
           seller_id,
           wb_account_id,
           wb_order_id,
           wb_order_uid,
           wb_status,
           canonical_status,
           version,
           wms_task_id,
           wb_supply_id,
           manual_review_reason,
           created_at,
           updated_at
      FROM wb_assembly_tasks
     WHERE created_at >= now() - interval '${TASK_WINDOW_DAYS} days'
     ORDER BY created_at
"

# --------------------------------------------------------------- тарифы
# ИНН заменяется: он не влияет на работу стенда, но это персональные данные.
export_csv "$PROD_DSN_BILLING" "billing_tariff.csv" "
    SELECT *
      FROM billing_tariff
     ORDER BY 1
"

export_csv "$PROD_DSN_BILLING" "billing_tariff_version.csv" "
    SELECT *
      FROM billing_tariff_version
     ORDER BY 1
"

# --------------------------------------------------------------- ворота
# Дамп проверяется тем же скриптом, что и стенд. Проверять своё же
# обезличивание глазами — это как раз тот способ, которым токены и уезжают.
echo
echo "проверяю выгрузку на живые секреты"
if ! "$(dirname "$0")/check-no-live-tokens.sh" "$OUT_DIR"; then
    echo >&2
    echo "ДАМП УДАЛЁН: в выгрузке найдено похожее на живой секрет." >&2
    echo "Это ошибка в запросах обезличивания выше, а не повод чистить руками." >&2
    rm -rf "${OUT_DIR:?}"/*.csv
    exit 1
fi

echo
echo "дамп готов: ${OUT_DIR}"
wc -l "${OUT_DIR}"/*.csv
