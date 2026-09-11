#!/usr/bin/env bash
# Проверка контрактов сервиса wms.
#
# Контракты заморожены потоком 0 и меняются только им, синхронно всем трём
# потокам (правило 9.5.2). Эта проверка обязана падать в CI, а не находиться
# глазами на ревью: разошедшийся контракт три агента обнаружат неделей позже,
# каждый по-своему.
#
# Подход взят из CI платформы (см. reference/wb-fbs-gateway-template/README.md):
# валидация спецификаций отдельным шагом, помимо тестов.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OPENAPI="${ROOT}/services/wms/contracts/openapi.yaml"
ASYNCAPI="${ROOT}/services/wms/contracts/asyncapi.yaml"

failed=0
step() { echo; echo "── $1"; }

step "YAML разбирается"
python3 - "$OPENAPI" "$ASYNCAPI" <<'PY' || failed=1
import sys, yaml
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as handle:
        yaml.safe_load(handle)
    print(f"  ok: {path.split('/')[-1]}")
PY

step "OpenAPI 3.1 валиден"
if python3 -c "import openapi_spec_validator" 2>/dev/null; then
    python3 - "$OPENAPI" <<'PY' || failed=1
import sys, yaml
from openapi_spec_validator import validate
with open(sys.argv[1], encoding="utf-8") as handle:
    validate(yaml.safe_load(handle))
print("  ok: спецификация валидна")
PY
else
    echo "  ПРОПУЩЕНО: нет openapi-spec-validator (pip install openapi-spec-validator)" >&2
    failed=1
fi

step "AsyncAPI валиден"
# Версия CLI закреплена: `@latest` означает, что вчера зелёный контракт
# сегодня красный по чужой причине — и проверку начинают пропускать.
ASYNCAPI_CLI="@asyncapi/cli@3.4.0"
if command -v npx >/dev/null 2>&1; then
    # stderr НЕ глушится. `2>/dev/null` прятал и настоящую ошибку контракта:
    # проверка тихо уходила в запасной путь, а тот проверяет втрое меньше,
    # и «контракт валиден» значило «до него не дошли руки».
    if npx -y "$ASYNCAPI_CLI" validate "$ASYNCAPI"; then
        echo "  ok: спецификация валидна ($ASYNCAPI_CLI)"
    else
        echo "  ОШИБКА: $ASYNCAPI_CLI отверг спецификацию" >&2
        failed=1
    fi
else
    # Запасной путь: парсер как библиотека. CLI требует Node, и на сервере
    # стенда его может не быть — но проверка пропускаться не должна, и о том,
    # что она урезана, здесь говорится вслух.
    echo "  ВНИМАНИЕ: npx недоступен, проверка урезана до конверта и схем" >&2
    if python3 -c "import jsonschema" 2>/dev/null; then
        python3 - "$ASYNCAPI" <<'PY' || failed=1
import sys, yaml
with open(sys.argv[1], encoding="utf-8") as handle:
    spec = yaml.safe_load(handle)
assert spec.get("asyncapi", "").startswith("3."), "ожидается AsyncAPI 3.x"
schemas = spec.get("components", {}).get("schemas", {})
envelope = schemas.get("EventEnvelope")
assert envelope, "нет схемы EventEnvelope"
required = set(envelope.get("required", []))
expected = {"event_id", "tenant_id", "type", "occurred_at", "payload", "correlation_id"}
assert required == expected, f"конверт разошёлся с разделом 2.4: {required ^ expected}"
assert envelope.get("additionalProperties") is False, \
    "конверт обязан быть additionalProperties: false (инвариант 15)"
print(f"  ok: конверт и {len(schemas)} схем на месте")
PY
    else
        echo "  ПРОПУЩЕНО: нет ни @asyncapi/cli, ни jsonschema" >&2
        failed=1
    fi
fi

step "Контракт и схема БД не разошлись"
python3 - "$ROOT" <<'PY' || failed=1
import pathlib, re, sys, yaml

root = pathlib.Path(sys.argv[1])
migrations = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted((root / "services/wms/migrations").glob("*.sql")))

# Состояния задания заморожены в CHECK миграции 003. Контракт обязан знать
# ровно их: разошедшийся список — это поток B, рисующий несуществующий экран.
match = re.search(r"state\s+text NOT NULL DEFAULT 'new' CHECK \(state IN \(([^)]+)\)\)",
                  migrations, re.S)
assert match, "не нашёл CHECK состояний задания в миграциях"
db_states = set(re.findall(r"'([a-z_]+)'", match.group(1)))

mapping = (root / "docs/state-mapping.md").read_text(encoding="utf-8")
# Берём только основную таблицу раздела 1: в документе есть ещё таблицы
# состояний возврата, состояний товара и статусов WB — это другие оси.
section = re.search(r"^## 1\. Основная таблица\s*(.+?)^## ", mapping, re.S | re.M)
assert section, "не нашёл основную таблицу в docs/state-mapping.md"
doc_states = set(re.findall(r"^\| `([a-z_]+)` \|", section.group(1), re.M))

missing = db_states - doc_states
assert not missing, f"состояния есть в схеме, но не в state-mapping.md: {sorted(missing)}"
extra = doc_states - db_states
assert not extra, f"состояния есть в state-mapping.md, но не в схеме: {sorted(extra)}"
print(f"  ok: {len(db_states)} состояний задания совпадают в схеме и таблице маппинга")

# Коды ошибок резерва: приложение C требует сохранить все до единого, а
# добавленные потоком 0 обязаны быть в ОБОИХ контрактах — потребитель
# разбирает их и по openapi, и по asyncapi.
spec = yaml.safe_load((root / "services/wms/contracts/openapi.yaml").read_text(encoding="utf-8"))
events = yaml.safe_load((root / "services/wms/contracts/asyncapi.yaml").read_text(encoding="utf-8"))
text = yaml.safe_dump(spec, allow_unicode=True)
events_text = yaml.safe_dump(events, allow_unicode=True)
inherited = ("SELLER_MAPPING_MISSING", "PRODUCT_MAPPING_MISSING",
             "AMBIGUOUS_PRODUCT_MAPPING", "WAREHOUSE_UNKNOWN",
             "INSUFFICIENT_STOCK", "SERIALIZATION_RETRY")
added = ("OWNER_INACTIVE", "UNPROCESSABLE_ORDER")
for code in inherited + added:
    assert code in text, f"код ошибки {code} потерялся в openapi.yaml"
    assert code in events_text, f"код ошибки {code} потерялся в asyncapi.yaml"
print(f"  ok: {len(inherited) + len(added)} кодов отказа резерва на месте в обоих контрактах")

# Версии двух файлов совпадают: клиенту важно, с какой версией СЕРВИСА он
# говорит, а не какой из двух документов кто-то поправил.
api_version = str((spec.get("info") or {}).get("version") or "")
events_version = str((events.get("info") or {}).get("version") or "")
assert api_version == events_version, (
    f"версии контрактов разошлись: openapi {api_version}, asyncapi {events_version}")
changelog = (root / "services/wms/contracts/CHANGELOG.md").read_text(encoding="utf-8")
assert f"## {api_version}" in changelog, (
    f"версия {api_version} не описана в contracts/CHANGELOG.md: "
    f"изменение контракта без записи — это правка, о которой узнают в бою")
print(f"  ok: версия {api_version} одна на оба файла и описана в changelog")
PY

echo
if [ "$failed" -ne 0 ]; then
    echo "КОНТРАКТЫ НЕ ПРОШЛИ ПРОВЕРКУ" >&2
    exit 1
fi
echo "контракты в порядке"
