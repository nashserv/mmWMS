#!/usr/bin/env bash
# Ночной полный прогон с отчётом.
#
# Отчёт кладётся в `tests/full-run/reports/<дата>.md` вместе с SHA стенда и
# репозитория: «прогон был зелёный» без указания, ЧТО гоняли, не значит
# ничего — образ, собранный вчера из другого клона, выглядит точно так же.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
REPORTS="${ROOT}/tests/full-run/reports"
mkdir -p "$REPORTS"

DATE="$(date +%Y-%m-%d)"
REPORT="${REPORTS}/${DATE}.md"
STARTED="$(date --iso-8601=seconds)"

cd "$ROOT"
REPO_SHA="$(git rev-parse HEAD 2>/dev/null || echo неизвестно)"
DIRTY=""
git diff --quiet 2>/dev/null || DIRTY=" (есть незакоммиченные правки)"
STAND_SHA="$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
             mmx-stand-wms:latest 2>/dev/null || echo неизвестно)"

# Перед прогоном — ворота стенда. Живой токен WB здесь означает, что прогон
# отправит настоящие команды в кабинет клиента (раздел 12).
GUARD_OUT="$(bash "${ROOT}/infrastructure/stand/scripts/check-no-live-tokens.sh" \
             "${ROOT}/infrastructure/stand" "${ROOT}/reference" 2>&1)"
GUARD_CODE=$?

OUTPUT=""
CODE=0
if [ "$GUARD_CODE" -ne 0 ]; then
    OUTPUT="$GUARD_OUT"
    CODE=$GUARD_CODE
else
    OUTPUT="$(cd "${ROOT}/tests/full-run" && python3 -m pytest -q 2>&1)"
    CODE=$?
fi

{
    echo "# Полный прогон ${DATE}"
    echo
    echo "| | |"
    echo "|---|---|"
    echo "| начат | ${STARTED} |"
    echo "| завершён | $(date --iso-8601=seconds) |"
    echo "| коммит репозитория | \`${REPO_SHA}\`${DIRTY} |"
    echo "| образ стенда собран из | \`${STAND_SHA}\` |"
    echo "| исход | $([ "$CODE" -eq 0 ] && echo "зелёный" || echo "КРАСНЫЙ (код ${CODE})") |"
    echo
    if [ "$GUARD_CODE" -ne 0 ]; then
        echo "## Ворота стенда не пройдены"
        echo
        echo "Прогон НЕ запускался: на стенде найдено похожее на живой токен"
        echo "Wildberries. Запуск отправил бы настоящие команды в кабинет клиента."
        echo
    fi
    echo '```'
    echo "$OUTPUT"
    echo '```'
} > "$REPORT"

echo "отчёт: ${REPORT}"
exit "$CODE"
