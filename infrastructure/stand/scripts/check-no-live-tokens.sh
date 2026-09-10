#!/usr/bin/env bash
# Ворота стенда: живых токенов Wildberries здесь быть не должно (раздел 12 мастера).
#
# Живой токен на стенде отправит настоящие команды в кабинет клиента — создаст
# поставку, переведёт задание в собранное, перезапишет остатки. Это необратимо,
# и узнает об этом клиент, а не мы.
#
# Скрипт — не напоминалка, а ворота: он поднимается первым сервисом compose,
# и остальные ждут его успешного завершения. Не прошло — стенд не встал.
#
# Код возврата: 0 — чисто, 1 — найдено похожее на живой секрет, 2 — нечего проверять.

set -euo pipefail

TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
    TARGETS=("/scan")
fi

# Форма JWT: три сегмента base64url через точку, начинается с eyJ ({" в base64).
# Токены Wildberries — именно JWT (приложение D мастера).
JWT_RE='eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'

# Присвоения секретов с непустым значением, не похожим на заглушку.
SECRET_ASSIGN_RE='(WB_TOKEN|WB_API_KEY|WB_CLIENT_SECRET|API_KEY|api_key|secret_ref|authorization)[[:space:]]*[:=][[:space:]]*"?[A-Za-z0-9_/+.-]{20,}'

# Явные заглушки — их наличие ожидаемо и не является находкой.
PLACEHOLDER_RE='(test-token-|development-only-|change-me|placeholder|REDACTED|vault://|\$\{[A-Za-z_]+:\?)'

found=0
scanned=0

scan_one() {
    local path="$1"
    local label="$2"

    while IFS= read -r hit; do
        # Отбрасываем строки, которые целиком объясняются заглушкой.
        if grep -Eq "$PLACEHOLDER_RE" <<<"$hit"; then
            continue
        fi
        if [ $found -eq 0 ]; then
            echo "СТЕНД НЕ ПОДНЯТ: найдено похожее на живой секрет Wildberries." >&2
            echo >&2
        fi
        found=1
        # Значение не печатаем: иначе секрет утечёт в лог CI (инвариант 15).
        echo "  ${label}: $(cut -d: -f1,2 <<<"$hit")" >&2
    done < <(grep -rEn -e "$JWT_RE" -e "$SECRET_ASSIGN_RE" "$path" 2>/dev/null || true)
}

for target in "${TARGETS[@]}"; do
    if [ ! -e "$target" ]; then
        echo "проверять нечего: $target не существует" >&2
        continue
    fi
    scanned=$((scanned + 1))
    scan_one "$target" "$target"
done

if [ "$scanned" -eq 0 ]; then
    echo "СТЕНД НЕ ПОДНЯТ: ни один из путей проверки не существует." >&2
    echo "Пустая проверка — это не пройденная проверка." >&2
    exit 2
fi

if [ $found -ne 0 ]; then
    echo >&2
    echo "Заменить на заглушку вида test-token-{n} или ссылку vault://, затем повторить." >&2
    echo "Значения намеренно не показаны, чтобы не утекли в лог." >&2
    exit 1
fi

echo "живых токенов WB не найдено, проверено путей: ${scanned}"
