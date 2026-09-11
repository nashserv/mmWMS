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

# DSN берётся из СОБРАННОЙ конфигурации compose, а не из окружения оболочки.
# `${DATABASE_URL:-}` был пуст всегда: его задаёт compose из своего `.env`, а
# в оболочке preflight его нет. Проверка проходила, ничего не проверив, — и
# именно она обязана поймать «shadow пишет в боевую базу».
dsn="$($COMPOSE config 2>/dev/null \
       | grep -oE 'DATABASE_URL: [^[:space:]]+' | head -1 | cut -d' ' -f2-)"
if [ -z "$dsn" ]; then
    bad "не удалось прочитать DATABASE_URL из docker compose config — проверять нечего"
elif printf '%s' "$dsn" | grep -qiE '91\.108\.239\.175|185\.73\.126\.232|prod'; then
    bad "DATABASE_URL смотрит на боевую базу: $dsn"
elif ! printf '%s' "$dsn" | grep -q '@postgres:'; then
    bad "DATABASE_URL указывает не на собственный postgres контура: $dsn"
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
    # Порог считает poll-budget.py по тому, сколько тратит БОЕВОЙ шлюз
    # (вопрос 11 раздела 13). Число 60 здесь было взято из воздуха: у
    # кабинета, где бой тратит 280 запросов в минуту, шестьдесят наших — это
    # блокировка, а у кабинета с двадцатью можно и больше.
    prod_rpm="${SHADOW_PROD_RPM:-}"
    if [ -z "$prod_rpm" ]; then
        bad "SHADOW_PROD_RPM не задан: сколько тратит боевой шлюз — вопрос 11 раздела 13, и без ответа долю бюджета не назначить"
    else
        allowed="$(python3 ./scripts/poll-budget.py --prod-rpm "$prod_rpm" --only-max-rpm 2>/dev/null)"
        if [ -z "$allowed" ]; then
            bad "poll-budget.py не посчитал допустимый предел — проверять нечего"
        elif [ "$rpm" -le "$allowed" ]; then
            pass "опрос раз в ${interval} с = ${rpm} запросов в минуту при пределе ${allowed} (бой тратит ${prod_rpm})"
        else
            bad "опрос раз в ${interval} с = ${rpm} запросов в минуту при пределе ${allowed}: боевому шлюзу не останется"
        fi
    fi
fi

echo
echo "=== 7. Клапан «собрать без остатка» выключен у теневого клиента ==="

# Проверка SQL-запросом, а не текстом на экране. Инструкция, которую надо
# выполнить глазами, — это инструкция, которую однажды не выполнят: по
# умолчанию в схеме `true`, и с ним КАЖДОЕ теневое задание создаст ложное
# расхождение и событие shortfall.
if $COMPOSE ps --status running postgres >/dev/null 2>&1 \
   && [ -n "$($COMPOSE ps -q postgres 2>/dev/null)" ]; then
    valve="$($COMPOSE exec -T postgres psql -U wms -d wms -tAc \
             "SELECT count(*) FROM owner WHERE allow_ledger_short" 2>/dev/null)"
    if [ -z "$valve" ]; then
        bad "не удалось спросить базу про allow_ledger_short"
    elif [ "$valve" = "0" ]; then
        pass "клапан выключен у всех владельцев"
    else
        bad "клапан включён у ${valve} владельцев: UPDATE owner SET allow_ledger_short = false"
    fi
else
    echo "  контур ещё не поднят — проверка отложена до первого подъёма:"
    echo "    docker compose -f compose.shadow.yaml exec postgres \\"
    echo "      psql -U wms -d wms -c 'UPDATE owner SET allow_ledger_short = false'"
    echo "  затем повторить preflight."
fi

echo
echo "=== 8. Кабинет заведён и виден ==="

wanted="${SHADOW_WB_ACCOUNT:-}"
if [ -z "$wanted" ]; then
    bad "SHADOW_WB_ACCOUNT не задан: неизвестно, какой кабинет наблюдаем"
elif [ -n "$($COMPOSE ps -q postgres 2>/dev/null)" ]; then
    found="$($COMPOSE exec -T postgres psql -U wms -d wms -tAc \
             "SELECT count(*) FROM wb_account WHERE external_id = '${wanted}' AND mode = 'shadow'" \
             2>/dev/null)"
    if [ "$found" = "1" ]; then
        pass "кабинет ${wanted} заведён в режиме shadow"
    else
        bad "кабинета ${wanted} в режиме shadow в базе нет: опрашивать нечего (см. README, шаг 5)"
    fi
else
    echo "  контур ещё не поднят — проверка отложена."
fi

echo
echo "=== 9. Секрет кабинета разрешается ==="

# Спрашиваем САМ сервис, а не смотрим на файл: имя файла выводится из
# `secret_ref`, и ошибиться в этом выводе легко — а узнать о ней иначе можно
# только по 401 от Wildberries через сутки наблюдения.
if [ -n "$wanted" ]; then
    resolved="$($COMPOSE run --rm --no-deps -T wms python -c "
import sys
from app.secrets import provider, SecretUnavailable
try:
    token = provider().resolve('vault://mmx/wb/${wanted}')
except SecretUnavailable as failure:
    print(f'НЕТ: {failure}')
    sys.exit(0)
print('ОК' if token else 'НЕТ: секрет пуст')
" 2>/dev/null | tail -1)"
    case "$resolved" in
        ОК*) pass "секрет кабинета ${wanted} читается сервисом" ;;
        *)   bad "секрет кабинета ${wanted} не разрешается: ${resolved:-нет ответа}. Проверьте WB_SECRET_HOST_DIR и имя файла (README, шаг 4)" ;;
    esac
fi

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
