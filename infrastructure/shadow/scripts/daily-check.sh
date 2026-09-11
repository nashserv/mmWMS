#!/usr/bin/env bash
# Ежедневная проверка теневого контура. Раздел 11, шаг 2: наблюдают неделю.
#
# Неделя без присмотра — это ровно инвариант 14: молчащий воркер считается
# сломанным. В боевом контуре четыре воркера стоят `Up` с зелёным healthcheck
# и нулём строк лога (раздел 3.5), и никто этого не замечает. Здесь замечаем.
#
# Признак жизни берём из базы (`wb_account.last_sync_at`), а не из /metrics:
# у каждого процесса свой реестр метрик, и воркеры своего HTTP не поднимают —
# спросить их через API нечем. База переживает и перезапуск, и ротацию логов.
#
#   cd infrastructure/shadow && ./scripts/daily-check.sh
#
set -uo pipefail
cd "$(dirname "$0")/.."

PSQL="docker compose -f compose.shadow.yaml exec -T postgres psql -U wms -d wms -tAF|"
issues=0

note() { printf '\033[31m  ! %s\033[0m\n' "$1"; issues=$((issues + 1)); }

echo "=== Опрос жив? ==="
$PSQL -c "
  SELECT external_id, status,
         coalesce(round(EXTRACT(EPOCH FROM (now() - last_sync_at))::numeric), -1),
         coalesce(sync_error_code, '—')
    FROM wb_account ORDER BY external_id" 2>/dev/null |
while IFS='|' read -r cab status age err; do
    [ -z "${cab:-}" ] && continue
    if [ "${age%%.*}" -lt 0 ] 2>/dev/null; then
        printf '  %-24s %s — НИ РАЗУ не опрошен\n' "$cab" "$status"
    elif [ "${age%%.*}" -gt 600 ] 2>/dev/null; then
        printf '  %-24s %s — последний опрос %s с назад, ошибка %s\n' "$cab" "$status" "$age" "$err"
    else
        printf '  %-24s %s — опрошен %s с назад\n' "$cab" "$status" "$age"
    fi
done

echo
echo "=== Задания приходят? (за сутки) ==="
$PSQL -c "
  SELECT coalesce(state, '—'), count(*)
    FROM wms_task WHERE created_at >= now() - interval '24 hours'
   GROUP BY state ORDER BY count(*) DESC" 2>/dev/null |
while IFS='|' read -r state n; do
    [ -z "${state:-}" ] && continue
    printf '  %-16s %s\n' "$state" "$n"
done

echo
echo "=== Расхождения: убывают ли? ==="
echo "  дата         расхождений   пар владелец×товар"
$PSQL -c "
  SELECT observed_on, sum(tasks), count(*)
    FROM shadow_divergence_report
   GROUP BY observed_on ORDER BY observed_on DESC LIMIT 10" 2>/dev/null |
while IFS='|' read -r day total pairs; do
    [ -z "${day:-}" ] && continue
    printf '  %-12s %11s %20s\n' "$day" "$total" "$pairs"
done

echo
echo "=== Что именно разошлось сегодня (топ-10) ==="
$PSQL -c "
  SELECT seller_external_id, coalesce(barcode, '—'), state, coalesce(wb_status, '—'), tasks
    FROM shadow_divergence_report
   WHERE observed_on = current_date
   ORDER BY tasks DESC LIMIT 10" 2>/dev/null |
while IFS='|' read -r seller barcode ours theirs n; do
    [ -z "${seller:-}" ] && continue
    printf '  %-22s %-15s у нас %-12s у WB %-10s заданий %s\n' \
        "$seller" "$barcode" "$ours" "$theirs" "$n"
done

echo
echo "--------------------------------------------------------------------"
echo "Пара cancelled / complete — то самое расхождение боевого контура на"
echo "2467 заданий (раздел 3). Если её здесь нет, новая WMS видит заказы там,"
echo "где старая их теряла: это и есть главный результат шага 2."
echo
echo "Пустой отчёт за сегодня — не то же самое, что «расхождений нет»."
echo "Сначала убедитесь по первому блоку, что опрос вообще шёл."
