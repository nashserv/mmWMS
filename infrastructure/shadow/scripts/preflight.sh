#!/usr/bin/env bash
# Проверка перед первым подъёмом shadow. Раздел 11, шаг 2.
#
# Смысл: доказать, что писать в кабинет клиента отсюда НЕЧЕМ. Живой токен
# отправит настоящие команды — создаст поставку, переведёт задание в
# собранное, перезапишет остатки. Необратимо, и узнает об этом клиент, а не мы
# (раздел 12).
#
# Скрипт ничего не запускает и ничего не меняет. Он только смотрит и, если
# что-то не так, отказывается продолжать.
#
#   cd infrastructure/shadow && ./scripts/preflight.sh
#
set -uo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f compose.shadow.yaml"
ok=0
fail=0

pass() { printf '  \033[32mОК\033[0m   %s\n' "$1"; ok=$((ok + 1)); }
bad()  { printf '  \033[31mНЕТ\033[0m  %s\n' "$1"; fail=$((fail + 1)); }

echo "=== 1. Запись в Wildberries запрещена ==="

# Спрашиваем сам код, а не повторяем его логику: повтор однажды разойдётся.
# Образ собираем отдельно и молча: иначе лог сборки приедет в ответ вместе с
# ним, и разобрать будет нечего.
$COMPOSE build wms >/dev/null 2>&1
verdict="$($COMPOSE run --rm --no-deps -T wms \
    python -c 'from app.wb import writes_allowed; print(writes_allowed("shadow"))' 2>/dev/null \
    | grep -oE '^(True|False)' | tail -1)"
case "$verdict" in
    False) pass "wb.writes_allowed('shadow') = False — сервис сам отказывается писать" ;;
    True)  bad  "wb.writes_allowed('shadow') = True. ЗАПИСЬ В КАБИНЕТ РАЗРЕШЕНА. Не поднимать." ;;
    *)     bad  "не удалось спросить сервис (образ собран? '$verdict')" ;;
esac

[ "${APP_ENV:-production}" = "production" ] \
    && pass "APP_ENV=production — режим shadow абсолютен" \
    || bad  "APP_ENV=${APP_ENV}: в локальном окружении запрет обходится"

[ -n "${WB_API_URL:-}" ] \
    && pass "WB_API_URL задан — сервис не считает, что рядом симулятор" \
    || bad  "WB_API_URL пуст: с пустым адресом запрет на запись обходится"

echo
echo "=== 2. Кабинет ровно один ==="

accounts="${SHADOW_WB_ACCOUNT:-}"
count=$(printf '%s' "$accounts" | tr ',' '\n' | grep -c '[^[:space:]]' || true)
case "$count" in
    1) pass "кабинет один: $accounts" ;;
    0) bad  "SHADOW_WB_ACCOUNT пуст — пустой список означает ВСЕ кабинеты" ;;
    *) bad  "кабинетов $count. Раздел 11: по одному, не пачкой, и разрешение на каждый отдельно" ;;
esac

echo
echo "=== 3. Публиковать события некуда ==="

if $COMPOSE config 2>/dev/null | grep -q 'RABBITMQ_URL'; then
    bad "в compose есть RABBITMQ_URL — события shadow уедут в шину, и биллинг выставит счёт второй раз"
else
    pass "брокер не настроен — опубликовать событие физически нечем"
fi

if $COMPOSE config --services 2>/dev/null | grep -qE 'outbox|wb-labels'; then
    bad "в contour'е есть публикатор outbox или воркер стикеров — оба пишут наружу"
else
    pass "ни публикатора outbox, ни воркера стикеров в контуре нет"
fi

echo
echo "=== 4. База своя, не боевая ==="

dsn="${DATABASE_URL:-}"
if printf '%s' "$dsn" | grep -qiE '91\.108\.239\.175|prod'; then
    bad "DATABASE_URL смотрит на боевую базу: $dsn"
else
    pass "shadow пишет в свою базу (том shadow-postgres)"
fi

echo
echo "=== 5. Живых токенов нет там, где им не место ==="

if [ -f ../stand/scripts/check-no-live-tokens.sh ]; then
    if bash ../stand/scripts/check-no-live-tokens.sh . >/dev/null 2>&1; then
        pass "в каталоге shadow ничего похожего на живой JWT нет"
    else
        bad "найдено похожее на живой токен WB — в Git и в конфигах его быть не может"
    fi
else
    bad "ворота check-no-live-tokens.sh не найдены"
fi

echo
echo "=== 6. Бюджет лимита назначен явно ==="

interval="${SHADOW_POLL_INTERVAL_SECONDS:-}"
if [ -z "$interval" ]; then
    bad "SHADOW_POLL_INTERVAL_SECONDS не задан — доля бюджета обязана быть решением, а не случайностью"
else
    rpm=$(awk -v i="$interval" 'BEGIN { printf "%.0f", 60.0 / i }')
    if [ "$rpm" -le 60 ]; then
        pass "опрос раз в ${interval} с = ${rpm} запросов в минуту из 300 на кабинет"
    else
        bad "опрос раз в ${interval} с = ${rpm} запросов в минуту: слишком много, боевому шлюзу не останется"
    fi
fi

echo
echo "=== 7. Клапан «собрать без остатка» выключен у теневого клиента ==="
echo "  Проверяется в базе ПОСЛЕ миграций и сида:"
echo "    SELECT seller_external_id, allow_ledger_short FROM owner;"
echo "  Все обязаны быть false. По умолчанию в схеме — true, и с ним каждое"
echo "  теневое задание создаст ложное расхождение и событие shortfall:"
echo "  остаток приезжает только на шаге 3, до него его нет ни у кого."

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
    printf '\033[32mпроверок пройдено %d, отказов нет\033[0m\n' "$ok"
    echo "Записывать в Wildberries отсюда нечем."
    echo
    echo "Это НЕ разрешение на запуск. Раздел 12: никакой деплой или изменение"
    echo "боевого контура без явного разрешения владельца на этот шаг."
    exit 0
fi
printf '\033[31mотказов %d из %d проверок — не поднимать\033[0m\n' "$fail" "$((ok + fail))"
exit 1
