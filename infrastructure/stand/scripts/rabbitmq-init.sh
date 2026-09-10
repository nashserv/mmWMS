#!/bin/sh
# Заводит exchange mmx.events, очереди стенда и привязки.
# POSIX sh: образ curlimages/curl на busybox, bash в нём нет.
#
# Почему отдельным шагом, а не файлом определений RabbitMQ: load_definitions
# конфликтует с RABBITMQ_DEFAULT_USER — определения загружаются раньше, чем
# создаётся пользователь, и раздача прав падает с {no_such_user}. Чинить это
# файлом определений значит закоммитить в репозиторий хеш пароля. Здесь
# учётные данные приходят из окружения и в Git не попадают.

set -eu

HOST="${RABBITMQ_HOST:-rabbitmq}"
PORT="${RABBITMQ_MANAGEMENT_PORT:-15672}"
USER="${RABBITMQ_USER:?set outside Git}"
PASSWORD="${RABBITMQ_PASSWORD:?set outside Git}"
API="http://${HOST}:${PORT}/api"
VHOST="%2F"

# Exchange и очередь заводятся PUT (идемпотентно по имени), привязка — POST:
# у привязки нет имени, которым её можно было бы адресовать.
declare_it() {
    method="$1"; path="$2"; body="$3"; what="$4"
    code="$(curl -sS -o /dev/null -w '%{http_code}' -u "${USER}:${PASSWORD}" \
        -H 'Content-Type: application/json' -X "${method}" "${API}/${path}" -d "${body}")"
    case "$code" in
        20*) echo "  ${what}: ok" ;;
        *)   echo "  ${what}: HTTP ${code}" >&2; return 1 ;;
    esac
}

until curl -sS -f -u "${USER}:${PASSWORD}" "${API}/overview" >/dev/null 2>&1; do
    echo "жду management API RabbitMQ..."
    sleep 2
done

echo "объявляю топологию шины:"
declare_it PUT "exchanges/${VHOST}/mmx.events" \
    '{"type":"topic","durable":true,"auto_delete":false,"internal":false}' \
    "exchange mmx.events"

# Очередь-наблюдатель стенда: ловит всё, чтобы прогон мог проверить формат
# события, не поднимая настоящего потребителя.
declare_it PUT "queues/${VHOST}/stand.wms.events" '{"durable":true,"auto_delete":false}' \
    "queue stand.wms.events"
declare_it PUT "queues/${VHOST}/stand.dead-letters" '{"durable":true,"auto_delete":false}' \
    "queue stand.dead-letters"
declare_it POST "bindings/${VHOST}/e/mmx.events/q/stand.wms.events" '{"routing_key":"#"}' \
    "binding mmx.events -> stand.wms.events"

echo "топология шины готова"
