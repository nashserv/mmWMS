#!/usr/bin/env bash
# Загрузить порядок обхода склада — раздел 13, вопрос 1.
#
# Лист подбора сортируется по `cell.route_order`. Сегодня его НЕ ВЫСТАВЛЯЕТ
# никто: ячейка заводится при первом упоминании адреса, и порядок обхода у неё
# остаётся пустым. Сборщик с таким листом ходит по складу зигзагом — пять
# сборщиков, 300–750 заданий в час (раздел 4), и каждый лишний круг стоит
# времени смены.
#
# Схема зон и формат адреса — вопрос к владельцу, и он открыт. Но когда ответ
# будет, он должен стать ЗАГРУЗКОЙ ДАННЫХ, а не переделкой: этот скрипт и есть
# то место, куда ответ кладут.
#
#   DATABASE_URL=postgresql://... ./load-route-order.sh route-order.csv
#
# Формат CSV (первая строка — заголовок):
#
#   cell_address,route_order
#   A-01-01,10
#   A-01-02,20
#   A-02-01,30
#
# Порядок задаётся с промежутками (10, 20, 30), чтобы вставить ячейку между
# существующими можно было не перенумеровывая весь склад.
#
# Идемпотентен: повторная загрузка того же файла ничего не меняет. Ячейки,
# которых ещё нет, заводятся в зоне STORAGE склада RUM — так же, как их
# заводит сам сервис при первом упоминании адреса.
#
set -euo pipefail

DATABASE_URL="${DATABASE_URL:?set outside Git}"
CSV="${1:?укажите файл: ./load-route-order.sh route-order.csv}"
WAREHOUSE="${WAREHOUSE_CODE:-RUM}"
ZONE="${ZONE_CODE:-STORAGE}"

[ -f "$CSV" ] || { echo "файл не найден: $CSV" >&2; exit 1; }

echo "Загружаю порядок обхода из $CSV (склад $WAREHOUSE, зона $ZONE)..."

# Файл целиком одной транзакцией: половина загруженного порядка обхода хуже,
# чем ни одного — сборщик пойдёт по маршруту, которого нет.
{
    echo "BEGIN;"
    echo "CREATE TEMP TABLE route_in (cell_address text PRIMARY KEY, route_order integer);"
    echo "\\copy route_in (cell_address, route_order) FROM '${CSV}' WITH (FORMAT csv, HEADER true)"
    cat <<SQL
-- Склад и зона — по тем же правилам, что у сервиса при первом упоминании.
INSERT INTO warehouse (id, code) VALUES (gen_random_uuid(), '${WAREHOUSE}')
    ON CONFLICT (code) DO NOTHING;

INSERT INTO zone (id, warehouse_id, code, kind)
SELECT gen_random_uuid(), w.id, '${ZONE}', 'storage'
  FROM warehouse w WHERE w.code = '${WAREHOUSE}'
    ON CONFLICT (warehouse_id, code) DO NOTHING;

-- Ячейки, которых ещё нет.
INSERT INTO cell (id, zone_id, address, route_order)
SELECT gen_random_uuid(), z.id, r.cell_address, r.route_order
  FROM route_in r
  CROSS JOIN (SELECT z.id FROM zone z JOIN warehouse w ON w.id = z.warehouse_id
               WHERE w.code = '${WAREHOUSE}' AND z.code = '${ZONE}') z
 WHERE NOT EXISTS (SELECT 1 FROM cell c WHERE c.address = r.cell_address);

-- Порядок обхода у уже заведённых.
UPDATE cell c SET route_order = r.route_order
  FROM route_in r
 WHERE c.address = r.cell_address
   AND c.route_order IS DISTINCT FROM r.route_order;

SELECT count(*) AS "ячеек в файле" FROM route_in;
SELECT count(*) AS "ячеек без порядка обхода осталось"
  FROM cell WHERE active AND route_order IS NULL;
COMMIT;
SQL
} | psql "$DATABASE_URL" -v ON_ERROR_STOP=1

echo
echo "Готово. Если во второй строке не ноль — у этих ячеек порядка обхода нет,"
echo "и в листе подбора они окажутся в конце (NULLS LAST). Это видно, а не тихо."
