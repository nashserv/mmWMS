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

# Под systemd переменные приходят из `EnvironmentFile`; руками — их нет.
# Читаем `.env` сами, если их ещё нет: скрипт обязан работать и так и так,
# иначе «проверю руками перед сном» упирается в отсутствующий пароль.
if [ -z "${POSTGRES_PASSWORD:-}" ] && [ -f "${ROOT}/infrastructure/stand/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${ROOT}/infrastructure/stand/.env"
    set +a
fi

# Адреса собираются ЗДЕСЬ из кусков, лежащих в `.env`.
#
# В `.env` пароли, а прогону нужны готовые DSN. Держать их там же значило бы
# хранить пароль дважды и однажды поправить только одну копию. Пустая
# переменная — красный прогон с внятным сообщением, а не тихий пропуск.
: "${POSTGRES_PASSWORD:?нужен POSTGRES_PASSWORD из infrastructure/stand/.env}"
: "${RABBITMQ_PASSWORD:?нужен RABBITMQ_PASSWORD из infrastructure/stand/.env}"
export WMS_BASE_URL="${WMS_BASE_URL:-http://127.0.0.1:8080}"
export DATABASE_URL="${DATABASE_URL:-postgresql://wms:${POSTGRES_PASSWORD}@127.0.0.1:5432/wms}"
export BILLING_DATABASE_URL="${BILLING_DATABASE_URL:-postgresql://wms:${POSTGRES_PASSWORD}@127.0.0.1:5432/billing}"
export RABBITMQ_URL="${RABBITMQ_URL:-amqp://${RABBITMQ_USER:-mmx}:${RABBITMQ_PASSWORD}@127.0.0.1:5672/}"
export WB_SIMULATOR_URL="${WB_SIMULATOR_URL:-http://127.0.0.1:8090}"
export PRINT_AGENT_STATS_URL="${PRINT_AGENT_STATS_URL:-http://127.0.0.1:8091/}"
export WORKSTATION_BASE_URL="${WORKSTATION_BASE_URL:-http://127.0.0.1:8081}"

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
