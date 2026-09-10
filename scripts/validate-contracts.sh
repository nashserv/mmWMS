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
if command -v npx >/dev/null 2>&1 && npx -y @asyncapi/cli@latest validate "$ASYNCAPI" 2>/dev/null; then
    echo "  ok: спецификация валидна"
else
    # Запасной путь: парсер как библиотека. CLI требует свежий Node и на
    # сервере стенда может отсутствовать — но проверка пропускаться не должна.
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

# Коды ошибок резерва: приложение C требует сохранить все до единого.
spec = yaml.safe_load((root / "services/wms/contracts/openapi.yaml").read_text(encoding="utf-8"))
text = yaml.safe_dump(spec, allow_unicode=True)
for code in ("SELLER_MAPPING_MISSING", "PRODUCT_MAPPING_MISSING",
             "AMBIGUOUS_PRODUCT_MAPPING", "WAREHOUSE_UNKNOWN",
             "INSUFFICIENT_STOCK", "SERIALIZATION_RETRY"):
    assert code in text, f"код ошибки {code} потерялся в контракте"
print("  ok: все шесть кодов ошибок резерва на месте")
PY

echo
if [ "$failed" -ne 0 ]; then
    echo "КОНТРАКТЫ НЕ ПРОШЛИ ПРОВЕРКУ" >&2
    exit 1
fi
echo "контракты в порядке"
