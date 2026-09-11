"""Экраны рабочего места. Ванильный JS, как и остальной фронт платформы.

Экраны рисуются под то, как склад работает на самом деле (раздел 4 мастера):
человек стоит за ПК, в руках беспроводной сканер, читает с расстояния вытянутой
руки. Отсюда крупный шрифт, поле скана всегда в фокусе и ответ на скан без
перерисовки всей страницы — критерий раздела 10 требует реакции меньше 100 мс.

Сканер для браузера — это клавиатура: он «печатает» штрихкод и жмёт Enter.
Поэтому никакого «нажмите кнопку сканировать» здесь нет и быть не может.
"""
from __future__ import annotations

import json
from typing import Any

from .barcode import svg as barcode_svg

BASE_CSS = """
:root {
  --ink: #14181d; --muted: #5a6673; --line: #d6dce3; --bg: #f4f6f8;
  --ok: #0f7b3f; --warn: #9a6400; --stop: #b3261e; --accent: #17457a;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
       font: 16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { background: #fff; border-bottom: 1px solid var(--line); padding: 10px 18px;
         display: flex; gap: 18px; align-items: center; flex-wrap: wrap; }
header h1 { font-size: 18px; margin: 0 12px 0 0; }
nav a { color: var(--accent); text-decoration: none; margin-right: 14px; font-size: 15px; }
nav a.active { font-weight: 700; text-decoration: underline; }
main { padding: 18px; max-width: 1400px; }
.panel { background: #fff; border: 1px solid var(--line); border-radius: 8px;
         padding: 16px; margin-bottom: 16px; }
.panel h2 { margin: 0 0 12px; font-size: 17px; }
.row { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end; }
label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 4px; }
input, select, textarea { font: inherit; padding: 8px 10px; border: 1px solid var(--line);
                          border-radius: 6px; background: #fff; min-width: 160px; }
input.scan { font-size: 22px; font-family: ui-monospace, monospace; min-width: 320px;
             border: 2px solid var(--accent); }
button { font: inherit; padding: 9px 16px; border: 1px solid var(--accent); border-radius: 6px;
         background: var(--accent); color: #fff; cursor: pointer; }
button.ghost { background: #fff; color: var(--accent); }
button.danger { background: var(--stop); border-color: var(--stop); }
button:disabled { opacity: .5; cursor: not-allowed; }
table { width: 100%; border-collapse: collapse; font-size: 15px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { font-size: 13px; color: var(--muted); font-weight: 600; }
tr.done td { color: var(--muted); }
tr.selected td { background: #eaf1fa; }
code, .mono { font-family: ui-monospace, monospace; }
.addr { font-family: ui-monospace, monospace; font-size: 18px; font-weight: 700; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px;
        border: 1px solid var(--line); background: #fff; }
.pill.ok { color: var(--ok); border-color: var(--ok); }
.pill.warn { color: var(--warn); border-color: var(--warn); }
.pill.stop { color: var(--stop); border-color: var(--stop); }
.metric { display: inline-block; margin-right: 26px; }
.metric b { display: block; font-size: 30px; line-height: 1.1; }
.metric span { font-size: 13px; color: var(--muted); }
.note { color: var(--muted); font-size: 14px; }
.stopscreen { position: fixed; inset: 0; background: var(--stop); color: #fff; z-index: 50;
              display: none; padding: 40px; }
.stopscreen.on { display: block; }
.stopscreen h2 { font-size: 40px; margin: 0 0 16px; }
.stopscreen p { font-size: 22px; max-width: 900px; }
.stopscreen button { background: #fff; color: var(--stop); border-color: #fff;
                     font-size: 20px; padding: 14px 28px; margin-top: 24px; }
.flash { padding: 10px 14px; border-radius: 6px; margin-bottom: 12px; font-size: 15px; }
.flash.ok { background: #e6f4ec; color: var(--ok); }
.flash.err { background: #fbe9e7; color: var(--stop); }
.flash.warn { background: #fdf3e2; color: var(--warn); }
.hidden { display: none; }
@media print {
  header, nav, .noprint { display: none !important; }
  body { background: #fff; }
  .panel { border: none; padding: 0; }
}
"""

BASE_JS = """
const api = async (path, body) => {
  const response = await fetch(path, {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify(body || {})});
  let data = {};
  try { data = await response.json(); } catch (error) { data = {}; }
  return {ok: response.ok, status: response.status, data};
};
const el = (id) => document.getElementById(id);
const text = (value) => (value === null || value === undefined || value === '') ? '—' : String(value);
const flash = (target, kind, message) => {
  const box = el(target);
  if (!box) return;
  box.className = 'flash ' + kind;
  box.textContent = message;
  box.classList.remove('hidden');
};
const clearFlash = (target) => { const box = el(target); if (box) box.classList.add('hidden'); };
// Оператор запоминается в браузере станции: переспрашивать имя на каждый скан —
// это секунды на каждом задании и повод вводить чужое имя.
const remember = (key, value) => { try { localStorage.setItem(key, value); } catch (e) {} };
const recall = (key, fallback) => { try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; } };
const stopScreen = (message, detail) => {
  el('stop-title').textContent = message;
  el('stop-detail').textContent = detail || '';
  el('stopscreen').classList.add('on');
};
const clearStop = () => el('stopscreen').classList.remove('on');
"""

NAV = (
    ("/", "Подбор"),
    ("/receiving", "Приёмка"),
    ("/putaway", "Размещение"),
    ("/inventory", "Инвентаризация"),
    ("/shipping", "Отгрузка"),
    ("/supervisor", "Начальник склада"),
)


def _shell(title: str, active: str, body: str, script: str) -> str:
    links = "".join(
        f'<a href="{href}" class="{"active" if href == active else ""}">{label}</a>'
        for href, label in NAV)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — MM-Express</title>
<style>{BASE_CSS}</style></head>
<body>
<header><h1>{title}</h1><nav>{links}</nav>
<span id="conn" class="pill">связь…</span></header>
<div class="stopscreen" id="stopscreen">
  <h2 id="stop-title">Стоп</h2><p id="stop-detail"></p>
  <button onclick="clearStop()">Понятно, разберусь</button>
</div>
<main>{body}</main>
<script>{BASE_JS}
// Индикатор связи показывает не «сервис жив», а «опрос проходит»: порт может
// быть открыт при полностью неработающем опросе (раздел 3.5 мастера).
const refreshConn = async () => {{
  const {{ok, data}} = await api('/api/workstation/v1/status');
  const pill = el('conn');
  if (!ok) {{ pill.className = 'pill stop'; pill.textContent = 'нет связи с сервисом'; return; }}
  const projection = data.projection || {{}};
  const bus = data.bus || {{}};
  if (projection.stale) {{ pill.className = 'pill stop'; pill.textContent = 'опрос не проходит'; }}
  else {{
    pill.className = 'pill ok';
    pill.textContent = 'опрос идёт' + (bus.connected ? ' + шина' : ' (шина выключена — не мешает)');
  }}
}};
setInterval(refreshConn, 5000); refreshConn();
{script}
</script></body></html>"""


# --------------------------------------------------------------------- подбор

PICKING_BODY = """
<div class="panel noprint">
  <h2>Смена</h2>
  <div class="row">
    <div><label>Сборщик</label><input id="actor" placeholder="фамилия или табельный"></div>
    <div><label>Станция (для печати)</label><input id="station" placeholder="uuid станции"></div>
    <div><label>Заданий за обход</label><input id="limit" type="number" value="20" min="1" max="200"></div>
    <button id="take">Взять задания</button>
    <button class="ghost" id="refresh">Обновить</button>
  </div>
  <div id="session-flash" class="flash hidden"></div>
  <p class="note" id="queue-note"></p>
</div>

<div class="panel" id="session-panel" style="display:none">
  <h2>Лист подбора <span class="mono" id="picklist-code"></span></h2>
  <div class="row noprint">
    <a id="print-sheet" target="_blank"><button class="ghost">Печать листа</button></a>
    <button class="ghost" id="finish">Закрыть сессию</button>
  </div>
  <p class="note">Порядок строк — маршрут обхода склада (<code>route_order</code>),
     а не порядок заказов.</p>
  <table><thead><tr>
    <th>#</th><th>Адрес</th><th>Коробка</th><th>Товар</th><th>Штрихкод</th>
    <th>Кол-во</th><th>Владелец</th><th>Скан</th><th>Стикер</th>
  </tr></thead><tbody id="lines"></tbody></table>
</div>

<div class="panel noprint" id="scan-panel" style="display:none">
  <h2>Скан у стойки</h2>
  <div class="row">
    <div><label>Штрихкод товара (сканер)</label>
      <input id="rack-scan" class="scan" autocomplete="off" placeholder="ждёт скана"></div>
  </div>
  <div id="scan-flash" class="flash hidden"></div>
</div>

<div class="panel noprint" id="pack-panel" style="display:none">
  <h2>Упаковка — контрольный скан</h2>
  <p class="note">Главный рубеж качества: единственное место, где проверяется,
     что в коробке лежит то, что должно. Несовпадение останавливает упаковку.</p>
  <div class="row">
    <div><label>Задание</label><select id="pack-task"></select></div>
    <div><label>Короб (необязательно)</label><input id="pack-box" placeholder="штрихкод короба"></div>
    <div><label>Контрольный скан</label>
      <input id="pack-scan" class="scan" autocomplete="off" placeholder="ждёт скана"></div>
  </div>
  <div id="pack-flash" class="flash hidden"></div>
  <div class="row" style="margin-top:12px">
    <button id="print" disabled>Печать этикетки</button>
    <button class="ghost" id="reprint" disabled>Перепечатать</button>
    <span class="note" id="print-timing"></span>
  </div>
</div>

<div class="panel noprint">
  <h2>Отмена задания</h2>
  <p class="note">Причина обязательна — и кодом, и словами. В боевом контуре
     2645 отмен не имеют ни одной причины, разобрать их нечем.</p>
  <div class="row">
    <div><label>Задание</label><select id="cancel-task"></select></div>
    <div><label>Причина</label><select id="cancel-code">
      <option value="operator_short">не нашли товар на полке</option>
      <option value="operator_damaged">товар повреждён</option>
      <option value="operator_wrong_item">на полке лежит не то</option>
      <option value="wb_cancelled">отменил Wildberries</option>
      <option value="supervisor_decision">решение начальника смены</option>
    </select></div>
    <div style="flex:1"><label>Что именно произошло</label>
      <input id="cancel-comment" style="width:100%" placeholder="без этого отмена не пройдёт"></div>
    <button class="danger" id="cancel">Отменить задание</button>
  </div>
  <div id="cancel-flash" class="flash hidden"></div>
</div>

<div class="panel noprint">
  <h2>Вернуть на полку</h2>
  <p class="note">Сборщик взял вещь и кладёт её обратно. Резерв остаётся: вещь
     на той же полке и по-прежнему нужна этому заказу. Адрес и причина
     обязательны — без адреса товар «возвращается» в никуда, без причины
     непонятно, на какой полке спотыкаются.</p>
  <div class="row">
    <div><label>Задание</label><select id="shelf-task"></select></div>
    <div><label>Ячейка</label><input id="shelf-cell" placeholder="адрес, куда положили"></div>
    <div><label>Короб (необязательно)</label><input id="shelf-box" placeholder="штрихкод короба"></div>
    <div style="flex:1"><label>Почему вернули</label>
      <input id="shelf-reason" style="width:100%" placeholder="без этого возврат не пройдёт"></div>
    <button class="ghost" id="shelf">Вернуть на полку</button>
  </div>
  <div id="shelf-flash" class="flash hidden"></div>
</div>
"""

PICKING_JS = """
let session = null;
let lines = [];

el('actor').value = recall('actor', '');
el('station').value = recall('station', '');
el('actor').addEventListener('change', () => remember('actor', el('actor').value));
el('station').addEventListener('change', () => remember('station', el('station').value));

const renderLines = () => {
  const body = el('lines');
  body.innerHTML = '';
  const pack = el('pack-task'); const cancel = el('cancel-task');
  const shelf = el('shelf-task');
  pack.innerHTML = ''; cancel.innerHTML = ''; shelf.innerHTML = '';
  lines.forEach((line, index) => {
    const tr = document.createElement('tr');
    if (line.scan_result === 'ok') tr.className = 'done';
    const label = line.label_ready
      ? '<span class="pill ok">готов</span>'
      : '<span class="pill warn">нет</span>';
    const scan = line.scan_result
      ? (line.scan_result === 'ok' ? '<span class="pill ok">снят</span>'
                                   : '<span class="pill stop">' + line.scan_result + '</span>')
      : '<span class="pill">ждёт</span>';
    tr.innerHTML = '<td>' + (index + 1) + '</td>'
      + '<td class="addr">' + text(line.cell_address) + '</td>'
      + '<td class="mono">' + text(line.box_barcode) + '</td>'
      + '<td>' + text(line.name) + '</td>'
      + '<td class="mono">' + text(line.barcode) + '</td>'
      + '<td>' + text(line.quantity) + '</td>'
      + '<td>' + text(line.owner_external_id) + '</td>'
      + '<td>' + scan + '</td><td>' + label + '</td>';
    body.appendChild(tr);
    for (const select of [pack, cancel, shelf]) {
      const option = document.createElement('option');
      option.value = line.task_id;
      option.textContent = text(line.name) + ' · ' + line.barcode + ' · ' + text(line.cell_address);
      select.appendChild(option);
    }
  });
  el('print').disabled = lines.length === 0;
  el('reprint').disabled = lines.length === 0;
  el('shelf').disabled = lines.length === 0;
};

const loadSession = async () => {
  if (!session) return;
  const {ok, data} = await api('/api/workstation/v1/sessions/' + session);
  if (!ok) return;
  lines = data.lines || [];
  renderLines();
};

el('take').onclick = async () => {
  clearFlash('session-flash');
  const actor = el('actor').value.trim();
  if (!actor) { flash('session-flash', 'err', 'кто собирает? Без имени задание не за кем закрепить'); return; }
  const {ok, data} = await api('/api/workstation/v1/sessions', {
    actor_id: actor, station_id: el('station').value.trim() || null,
    limit: Number(el('limit').value) || 20});
  if (!ok) { flash('session-flash', 'err', data.error || 'не удалось взять задания'); return; }
  if (!data.session_id) { flash('session-flash', 'warn', data.note || 'свободных заданий нет'); return; }
  session = data.session_id;
  el('picklist-code').textContent = data.picklist_barcode;
  el('print-sheet').href = '/picklist/' + session;
  el('session-panel').style.display = '';
  el('scan-panel').style.display = '';
  el('pack-panel').style.display = '';
  flash('session-flash', 'ok', 'выдано заданий: ' + (data.tasks || []).length);
  await loadSession();
  el('rack-scan').focus();
};

el('refresh').onclick = async () => {
  await api('/api/workstation/v1/poll');
  const {data} = await api('/api/workstation/v1/tasks', {});
  const path = data.full_path || {};
  el('queue-note').textContent = 'ждёт подбора: ' + text(data.pickable)
    + ' · в wms: ' + text(path.available_in_wms) + ' · на экране: ' + text(path.on_screen)
    + (path.stale ? ' · ВНИМАНИЕ: опрос не проходит, данные устарели' : '');
  await loadSession();
};

el('finish').onclick = async () => {
  if (!session) return;
  await api('/api/workstation/v1/sessions/' + session + '/finish');
  flash('session-flash', 'ok', 'сессия закрыта');
  session = null; lines = []; renderLines();
  el('session-panel').style.display = 'none';
};

// Сканер печатает штрихкод и жмёт Enter — обрабатываем именно это.
el('rack-scan').addEventListener('keydown', async (event) => {
  if (event.key !== 'Enter') return;
  const barcode = event.target.value.trim();
  event.target.value = '';
  if (!barcode || !session) return;
  const line = lines.find((row) => row.barcode === barcode && row.scan_result !== 'ok')
            || lines.find((row) => row.scan_result !== 'ok');
  if (!line) { flash('scan-flash', 'warn', 'все строки листа уже сняты'); return; }
  const {ok, data} = await api('/api/workstation/v1/scan', {
    task_id: line.task_id, barcode, actor_id: el('actor').value.trim(),
    session_id: session, station_id: el('station').value.trim() || null});
  if (ok && data.accepted) flash('scan-flash', 'ok', 'снято: ' + barcode);
  else flash('scan-flash', 'err', (data.message || data.error || 'скан отклонён'));
  await loadSession();
});

el('pack-scan').addEventListener('keydown', async (event) => {
  if (event.key !== 'Enter') return;
  const control = event.target.value.trim();
  event.target.value = '';
  if (!control) return;
  const {ok, data} = await api('/api/workstation/v1/pack', {
    task_id: el('pack-task').value, control_barcode: control,
    actor_id: el('actor').value.trim(), station_id: el('station').value.trim() || null,
    box_barcode: el('pack-box').value.trim() || null, session_id: session});
  if (!ok || !data.accepted) {
    // Красный экран во всю страницу: упаковка остановлена, продолжать нельзя,
    // пока человек не решит — переподбор или расхождение.
    stopScreen('Не тот товар', data.message || data.error || 'контрольный скан не сошёлся');
    await loadSession();
    return;
  }
  flash('pack-flash', 'ok', data.message || 'упаковано');
  await loadSession();
});

// Ключ печати живёт на экране, а не на сервере: двойной клик обязан быть
// ОДНОЙ печатью, и решает это тот, кто видел оба клика. Кнопка при этом
// блокируется до ответа — человек, нажавший дважды, получал две этикетки на
// одну вещь и наклеивал вторую на следующую.
let printing = false;
const printKeys = {};
const printKeyFor = (taskId, reprint) => {
  if (reprint) { return 'reprint-' + taskId + '-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10); }
  if (!printKeys[taskId]) { printKeys[taskId] = 'print-' + taskId + '-' + Date.now(); }
  return printKeys[taskId];
};

const doPrint = async (reprint) => {
  if (printing) { return; }
  const station = el('station').value.trim();
  if (!station) { flash('pack-flash', 'err', 'не указана станция — неизвестно, на какой принтер печатать'); return; }
  const reason = reprint ? (prompt('Причина перепечатки (обязательна):') || '') : null;
  if (reprint && !reason.trim()) { flash('pack-flash', 'err', 'перепечатка без причины не выполняется'); return; }
  const taskId = el('pack-task').value;
  printing = true;
  el('print').disabled = true;
  el('reprint').disabled = true;
  const started = performance.now();
  try {
    const {ok, status, data} = await api('/api/workstation/v1/print', {
      task_id: taskId, station_id: station,
      actor_id: el('actor').value.trim(), reprint, reason, copies: 1,
      idempotency_key: printKeyFor(taskId, reprint)});
    if (status === 202) {
      // Исход неизвестен: байты ушли, ответа нет. Человеку нужно посмотреть
      // на принтер, а не нажать ещё раз.
      flash('pack-flash', 'err', data.error || 'исход печати неизвестен — посмотрите на принтер');
      return;
    }
    if (!ok) { flash('pack-flash', 'err', data.error || 'печать не прошла'); return; }
    el('print-timing').textContent =
      'до спулера ' + text(data.click_to_agent_ms) + ' мс · запись в устройство '
      + text(data.agent_write_ms) + ' мс · экран ' + Math.round(performance.now() - started) + ' мс'
      + ' · формат ' + text(data.label_format);
    flash('pack-flash', 'ok', 'этикетка отправлена в принтер');
  } finally {
    printing = false;
    el('print').disabled = false;
    el('reprint').disabled = false;
  }
};
el('print').onclick = () => doPrint(false);
el('reprint').onclick = () => doPrint(true);

el('shelf').onclick = async () => {
  const cell = el('shelf-cell').value.trim();
  const reason = el('shelf-reason').value.trim();
  if (!cell) { flash('shelf-flash', 'err', 'без адреса ячейки товар вернётся в никуда'); return; }
  if (!reason) { flash('shelf-flash', 'err', 'без причины возврат не разобрать'); return; }
  const {ok, data} = await api('/api/workstation/v1/return-to-shelf', {
    task_id: el('shelf-task').value, cell_address: cell,
    box_barcode: el('shelf-box').value.trim() || null,
    actor_id: el('actor').value.trim(), reason});
  if (!ok) { flash('shelf-flash', 'err', data.error || 'возврат не прошёл'); return; }
  el('shelf-reason').value = '';
  flash('shelf-flash', 'ok', 'вернули на полку: ' + text(data.cell_address));
  await loadSession();
};

el('cancel').onclick = async () => {
  const comment = el('cancel-comment').value.trim();
  if (!comment) { flash('cancel-flash', 'err', 'причина словами обязательна'); return; }
  const {ok, data} = await api('/api/workstation/v1/cancel', {
    task_id: el('cancel-task').value, reason_code: el('cancel-code').value,
    comment, actor_id: el('actor').value.trim()});
  if (!ok) { flash('cancel-flash', 'err', data.error || 'отмена не прошла'); return; }
  flash('cancel-flash', 'ok', 'отменено: ' + data.cancel_reason);
  el('cancel-comment').value = '';
  await loadSession();
};

setInterval(() => { el('refresh').click(); }, 5000);
el('refresh').click();
"""


# --------------------------------------------------------------------- приёмка

RECEIVING_BODY = """
<div class="panel">
  <h2>Приёмка — пересчёт</h2>
  <p class="note">Баланс двигается по факту, а не по ожиданию. Разница
     оформляется расхождением с типом и виновным, а не подгонкой числа.</p>
  <div class="row">
    <div><label>Приёмщик</label><input id="actor"></div>
    <div><label>Клиент (seller_external_id)</label><input id="seller"></div>
    <div><label>Номер документа</label><input id="reference" placeholder="ключ идемпотентности"></div>
    <button class="ghost" id="load">Показать открытые приёмки</button>
  </div>
  <div class="row" style="margin-top:10px">
    <div><label>Имя клиента (если склад видит впервые)</label><input id="seller-name"></div>
    <div><label>ИНН</label><input id="seller-inn"></div>
  </div>
  <div id="flash" class="flash hidden"></div>
</div>

<div class="panel">
  <h2>Строки</h2>
  <table><thead><tr>
    <th>Штрихкод</th><th>Товар</th><th>Ожидается</th><th>Факт</th>
    <th>Коробка</th><th>Ячейка</th><th>Комментарий</th><th></th>
  </tr></thead><tbody id="lines"></tbody></table>
  <div class="row" style="margin-top:12px">
    <button class="ghost" id="add">Добавить строку</button>
    <button id="submit">Отправить приёмку</button>
  </div>
</div>

<div class="panel">
  <h2>Открытые приёмки и расхождения</h2>
  <div id="open"></div>
</div>
"""

RECEIVING_JS = """
el('actor').value = recall('actor', '');
el('seller').value = recall('seller', '');

const addLine = (values) => {
  const tr = document.createElement('tr');
  const cell = (name, type, value) =>
    '<td><input data-name="' + name + '" type="' + type + '" value="' + (value || '') + '"></td>';
  tr.innerHTML = cell('barcode', 'text', values && values.barcode)
    + cell('name', 'text', values && values.name)
    + cell('expected_qty', 'number', values && values.expected_qty)
    + cell('actual_qty', 'number', values && values.actual_qty)
    + cell('box_barcode', 'text', values && values.box_barcode)
    + cell('cell_address', 'text', values && values.cell_address)
    + cell('comment', 'text', values && values.comment)
    + '<td><button class="ghost">убрать</button></td>';
  tr.querySelector('button').onclick = () => tr.remove();
  el('lines').appendChild(tr);
};
el('add').onclick = () => addLine();

const collect = () => Array.from(el('lines').querySelectorAll('tr')).map((tr) => {
  const row = {};
  tr.querySelectorAll('input').forEach((input) => {
    if (input.value !== '') row[input.dataset.name] = input.type === 'number'
      ? Number(input.value) : input.value;
  });
  return row;
}).filter((row) => row.barcode);

el('submit').onclick = async () => {
  remember('actor', el('actor').value); remember('seller', el('seller').value);
  const {ok, data} = await api('/api/workstation/v1/receiving/submit', {
    seller_external_id: el('seller').value.trim(),
    reference: el('reference').value.trim(),
    lines: collect(), actor_id: el('actor').value.trim(),
    seller_name: el('seller-name').value.trim() || null,
    seller_inn: el('seller-inn').value.trim() || null});
  if (!ok) { flash('flash', 'err', data.error || 'приёмка не прошла'); return; }
  const gaps = (data.discrepancies || []).length;
  flash('flash', gaps ? 'warn' : 'ok',
    'приёмка ' + text(data.reference) + ': ' + (data.duplicate ? 'повтор, ничего не добавлено' : 'принята')
    + (gaps ? ' · расхождений: ' + gaps : '')
    + (data.owner_created ? ' · клиент заведён этой приёмкой' : ''));
  el('load').click();
};

el('load').onclick = async () => {
  const {ok, data} = await api('/api/workstation/v1/receiving/screen', {
    owner_external_id: el('seller').value.trim() || null});
  const box = el('open');
  if (!ok) { box.innerHTML = '<p class="flash err">' + (data.error || 'wms недоступен') + '</p>'; return; }
  const receipts = data.receipts || [];
  if (!receipts.length) { box.innerHTML = '<p class="note">открытых приёмок нет</p>'; return; }
  box.innerHTML = receipts.map((receipt) => {
    const rows = (receipt.discrepancies || []).map((item) =>
      '<tr><td>' + text(item.kind) + '</td><td class="mono">' + text(item.barcode)
      + '</td><td>' + text(item.qty) + '</td><td>' + text(item.decision)
      + '</td><td>' + text(item.liable) + '</td><td class="addr">' + text(item.cell_address)
      + '</td></tr>').join('');
    return '<div class="panel"><h2>' + text(receipt.reference) + ' · ' + text(receipt.owner_external_id)
      + ' <span class="pill">' + text(receipt.state) + '</span></h2>'
      + (rows ? '<table><thead><tr><th>Тип</th><th>Штрихкод</th><th>Кол-во</th><th>Решение</th>'
              + '<th>Виноват</th><th>Ячейка</th></tr></thead><tbody>' + rows + '</tbody></table>'
              : '<p class="note">расхождений нет</p>')
      + '<button class="ghost" onclick="fillFrom(' + JSON.stringify(JSON.stringify(receipt)).replace(/"/g, '&quot;') + ')">Взять в пересчёт</button></div>';
  }).join('');
};

window.fillFrom = (payload) => {
  const receipt = JSON.parse(payload);
  el('reference').value = receipt.reference || '';
  el('seller').value = receipt.owner_external_id || '';
  el('lines').innerHTML = '';
  (receipt.lines || []).forEach(addLine);
};

addLine();
el('load').click();
"""


# --------------------------------------------------------------------- размещение

PUTAWAY_BODY = """
<div class="panel">
  <h2>Размещение принятого</h2>
  <p class="note">Подсказка адреса не обязательна к исполнению: кладите куда
     удобно, но факт фиксируется коробкой. Один товар на коробку — практика
     склада, она сохраняется.</p>
  <div class="row">
    <div><label>Клиент</label><input id="seller"></div>
    <button class="ghost" id="load">Показать</button>
  </div>
  <div id="items"></div>
</div>

<div class="panel">
  <h2>Коробка</h2>
  <p class="note">Комментарий обязателен: через месяц на складе стоят сотни
     одинаковых коробок, и без пометки нужную не найти.
     Количество 0 означает «не считали», а не «пусто» — для этого есть флажок.</p>
  <div class="row">
    <div><label>Штрихкод коробки</label><input id="box" class="mono"></div>
    <div><label>Штрихкод товара</label><input id="product" class="mono"></div>
    <div><label>Ячейка</label><input id="cell"></div>
    <div><label>Количество</label><input id="qty" type="number" value="0" min="0"></div>
    <div><label>Пересчитано</label><br><input id="counted" type="checkbox"></div>
  </div>
  <div class="row" style="margin-top:10px">
    <div style="flex:1"><label>Комментарий (обязателен)</label>
      <input id="comment" style="width:100%" placeholder="чем эта коробка отличается от соседних"></div>
    <button id="save">Завести коробку</button>
  </div>
  <div id="flash" class="flash hidden"></div>
</div>
"""

PUTAWAY_JS = """
el('seller').value = recall('seller', '');
el('load').onclick = async () => {
  const {ok, data} = await api('/api/workstation/v1/putaway/screen', {
    owner_external_id: el('seller').value.trim() || null});
  const box = el('items');
  if (!ok) { box.innerHTML = '<p class="flash err">' + (data.error || 'wms недоступен') + '</p>'; return; }
  const items = data.items || [];
  if (!items.length) { box.innerHTML = '<p class="note">разложить нечего</p>'; return; }
  box.innerHTML = '<table><thead><tr><th>Владелец</th><th>Штрихкод</th><th>Товар</th>'
    + '<th>Разложить</th><th>Куда положить</th><th></th></tr></thead><tbody>'
    + items.map((item) => {
        const cells = (item.suggested_cells || []).map((cell) =>
          '<span class="pill' + (cell.holds_same_sku ? ' ok' : '') + '">' + text(cell.cell_address)
          + (cell.free_capacity != null ? ' · свободно ' + cell.free_capacity : '') + '</span>').join(' ');
        return '<tr><td>' + text(item.owner_external_id) + '</td><td class="mono">' + text(item.barcode)
          + '</td><td>' + text(item.name) + '</td><td>' + text(item.qty_to_place) + '</td><td>'
          + (cells || '<span class="note">подсказок нет</span>') + '</td>'
          + '<td><button class="ghost" onclick="pick(' + JSON.stringify(JSON.stringify(item)).replace(/"/g, '&quot;') + ')">Взять</button></td></tr>';
      }).join('') + '</tbody></table>';
};

window.pick = (payload) => {
  const item = JSON.parse(payload);
  el('product').value = item.barcode || '';
  el('qty').value = item.qty_to_place || 0;
  el('seller').value = item.owner_external_id || el('seller').value;
  const first = (item.suggested_cells || [])[0];
  if (first) el('cell').value = first.cell_address || '';
  el('box').focus();
};

el('save').onclick = async () => {
  const {ok, data} = await api('/api/workstation/v1/putaway/box', {
    barcode: el('box').value.trim(), seller_external_id: el('seller').value.trim(),
    comment: el('comment').value.trim(), product_barcode: el('product').value.trim() || null,
    cell_address: el('cell').value.trim() || null, quantity: Number(el('qty').value) || 0,
    counted: el('counted').checked});
  if (!ok) { flash('flash', 'err', data.error || 'коробка не заведена'); return; }
  flash('flash', 'ok', 'коробка ' + el('box').value + ' поставлена в ' + (el('cell').value || 'адрес не указан'));
  el('box').value = ''; el('comment').value = '';
  el('load').click();
};
el('load').click();
"""


# --------------------------------------------------------------------- инвентаризация

INVENTORY_BODY = """
<div class="panel">
  <h2>Инвентаризация</h2>
  <p class="note">Учётное количество скрыто до ввода факта: считающий не должен
     видеть цифру заранее, иначе пересчитывается бумажка, а не полка.</p>
  <div class="row">
    <div><label>Клиент</label><input id="seller"></div>
    <div><label>Охват</label><select id="scope">
      <option value="partial">частичная</option><option value="full">полная</option></select></div>
    <div><label>Номер документа</label><input id="reference"></div>
    <button class="ghost" id="load">Получить лист</button>
    <div><label>Показать учёт</label><br><input id="reveal" type="checkbox"></div>
  </div>
  <div id="flash" class="flash hidden"></div>
</div>

<div class="panel">
  <h2>Пересчёт</h2>
  <table><thead><tr>
    <th>Ячейка</th><th>Коробка</th><th>Штрихкод</th><th>Товар</th><th>Учёт</th><th>Факт</th><th>Разница</th>
  </tr></thead><tbody id="lines"></tbody></table>
  <div class="row" style="margin-top:12px"><button id="submit">Применить пересчёт</button></div>
</div>
"""

INVENTORY_JS = """
let sheet = [];
el('seller').value = recall('seller', '');

const render = () => {
  const reveal = el('reveal').checked;
  el('lines').innerHTML = sheet.map((line, index) =>
    '<tr><td class="addr">' + text(line.cell_address) + '</td>'
    + '<td class="mono">' + text(line.box_barcode) + '</td>'
    + '<td class="mono">' + text(line.barcode) + '</td>'
    + '<td>' + text(line.name) + '</td>'
    + '<td>' + (reveal ? text(line.expected_qty) : '<span class="note">скрыто</span>') + '</td>'
    + '<td><input type="number" min="0" data-index="' + index + '" class="fact"></td>'
    + '<td class="diff" data-index="' + index + '">—</td></tr>').join('');
  document.querySelectorAll('.fact').forEach((input) => {
    input.oninput = () => {
      const index = Number(input.dataset.index);
      const expected = Number(sheet[index].expected_qty || 0);
      const cellNode = document.querySelector('.diff[data-index="' + index + '"]');
      if (input.value === '') { cellNode.textContent = '—'; return; }
      const diff = Number(input.value) - expected;
      cellNode.innerHTML = diff === 0 ? '<span class="pill ok">сходится</span>'
        : '<span class="pill ' + (diff < 0 ? 'stop' : 'warn') + '">' + (diff > 0 ? '+' : '') + diff + '</span>';
    };
  });
};
el('reveal').onchange = render;

el('load').onclick = async () => {
  const {ok, data} = await api('/api/workstation/v1/inventory/sheet', {
    seller_external_id: el('seller').value.trim(), scope: el('scope').value});
  if (!ok) { flash('flash', 'err', data.error || 'лист не получен'); return; }
  sheet = data.lines || [];
  render();
  flash('flash', 'ok', 'строк в листе: ' + sheet.length);
};

el('submit').onclick = async () => {
  const lines = Array.from(document.querySelectorAll('.fact')).map((input) => {
    if (input.value === '') return null;
    const line = sheet[Number(input.dataset.index)];
    return {barcode: line.barcode, cell_address: line.cell_address,
            box_barcode: line.box_barcode, expected_qty: line.expected_qty,
            fact_qty: Number(input.value)};
  }).filter(Boolean);
  const {ok, data} = await api('/api/workstation/v1/inventory/count', {
    seller_external_id: el('seller').value.trim(), reference: el('reference').value.trim(),
    scope: el('scope').value, lines});
  if (!ok) { flash('flash', 'err', data.error || 'пересчёт не применён'); return; }
  flash('flash', 'ok', 'применено, движений: ' + text(data.moves)
    + (data.duplicate ? ' (повтор, ничего не добавлено)' : ''));
};
"""


# --------------------------------------------------------------------- отгрузка

SHIPPING_BODY = """
<div class="panel">
  <h2>Собрано и готово к поставке</h2>
  <div class="row">
    <div><label>Клиент</label><input id="seller"></div>
    <button class="ghost" id="load">Обновить</button>
  </div>
  <table><thead><tr><th></th><th>Задание</th><th>Заказ WB</th><th>Владелец</th>
    <th>Штрихкод</th><th>Состояние</th></tr></thead><tbody id="tasks"></tbody></table>
</div>

<div class="panel">
  <h2>Поставка</h2>
  <div class="row">
    <div><label>Поставка WB</label><input id="supply" class="mono" placeholder="WB-GI-…"></div>
    <button class="ghost" data-action="open">Открыть</button>
    <button class="ghost" data-action="add_orders">Добавить выбранные</button>
    <button class="ghost" data-action="close">Закрыть</button>
    <button class="ghost" data-action="deliver">Передать в доставку</button>
  </div>
  <div id="flash" class="flash hidden"></div>
</div>

<div class="panel">
  <h2>Передача перевозчику</h2>
  <p class="note">HANDED_TO_WB ставит человек, а не автоматика: статус WB
     <code>complete</code> приёмку не доказывает. Сегодня подтверждено меньше 1 %
     отгруженных, цель — 95 %.</p>
  <div class="row">
    <div style="flex:1"><label>Кто передал (подпись)</label>
      <input id="handed-by" style="width:100%" placeholder="фамилия того, кто отдал поставку"></div>
    <button id="handover">Подтверждаю передачу</button>
  </div>
</div>
"""

SHIPPING_JS = """
el('seller').value = recall('seller', '');
let tasks = [];

el('load').onclick = async () => {
  const {ok, data} = await api('/api/workstation/v1/shipping/picked', {
    owner_external_id: el('seller').value.trim() || null});
  if (!ok) { flash('flash', 'err', data.error || 'wms недоступен'); return; }
  tasks = data.tasks || [];
  el('tasks').innerHTML = tasks.map((task, index) =>
    '<tr><td><input type="checkbox" data-index="' + index + '" class="pick"></td>'
    + '<td class="mono">' + text(task.task_id) + '</td>'
    + '<td class="mono">' + text(task.wb_order_id) + '</td>'
    + '<td>' + text(task.owner_external_id || task.seller_external_id) + '</td>'
    + '<td class="mono">' + text(task.barcode) + '</td>'
    + '<td><span class="pill">' + text(task.state) + '</span></td></tr>').join('');
};

const selected = () => Array.from(document.querySelectorAll('.pick:checked'))
  .map((input) => tasks[Number(input.dataset.index)].task_id).filter(Boolean);

document.querySelectorAll('[data-action]').forEach((button) => {
  button.onclick = async () => {
    const {ok, data} = await api('/api/workstation/v1/shipping/action', {
      seller_external_id: el('seller').value.trim(), action: button.dataset.action,
      wb_supply_id: el('supply').value.trim() || null,
      task_ids: button.dataset.action === 'add_orders' ? selected() : null});
    if (!ok) { flash('flash', 'err', data.error || 'не вышло'); return; }
    if (data.wb_supply_id) el('supply').value = data.wb_supply_id;
    flash('flash', 'ok', 'поставка ' + text(data.wb_supply_id) + ': ' + text(data.state)
      + ' · заказов ' + text(data.orders));
  };
});

el('handover').onclick = async () => {
  const who = el('handed-by').value.trim();
  if (!who) { flash('flash', 'err', 'подпись обязательна: передачу подтверждает человек'); return; }
  const {ok, data} = await api('/api/workstation/v1/shipping/action', {
    seller_external_id: el('seller').value.trim(), action: 'hand_over',
    wb_supply_id: el('supply').value.trim() || null, handed_over_by: who});
  if (!ok) { flash('flash', 'err', data.error || 'подтверждение не прошло'); return; }
  flash('flash', 'ok', 'передано: ' + text(data.handed_by) + ' в ' + text(data.handed_at));
};
el('load').click();
"""


# --------------------------------------------------------------------- начальник склада

SUPERVISOR_BODY = """
<div class="panel">
  <h2>Полный путь задания</h2>
  <p class="note">Три числа подряд. Расхождение между соседними — это и есть
     «новые заказы иногда не падают в приложение», выраженное числом.</p>
  <div id="path"></div>
</div>

<div class="panel"><h2>Расхождение с Wildberries</h2><div id="diverged"></div></div>
<div class="panel"><h2>Сборка без остатка (клапан 6.5)</h2><div id="ledger"></div></div>
<div class="panel"><h2>Расхождения приёмки</h2><div id="disc"></div></div>
<div class="panel"><h2>Отклонённые сканы</h2><div id="scans"></div></div>
<div class="panel"><h2>Печать</h2><div id="printing"></div></div>
<div class="panel"><h2>Отмены</h2><div id="cancels"></div></div>
<div class="panel"><h2>Открытые сессии подбора</h2><div id="sessions"></div></div>
"""

SUPERVISOR_JS = """
const load = async () => {
  const {ok, data} = await api('/api/workstation/v1/supervisor/screen');
  if (!ok) return;
  const path = data.full_path || {};
  el('path').innerHTML =
      metric(path.available_in_wms, 'ждёт подбора в wms')
    + metric(path.on_screen, 'на экране рабочего места')
    + metric(path.gap, 'дыра между ними')
    + (path.stale ? '<p class="flash err">опрос не проходит: ' + text(path.last_error) + '</p>'
                  : '<p class="flash ok">опрос идёт, данные свежие</p>');

  const diverged = data.diverged || {};
  el('diverged').innerHTML = metric(diverged.count, 'заданий разошлось со статусом WB')
    + '<p class="note">' + text(diverged.note) + '</p>'
    + ((diverged.tasks || []).length ? table(['Задание', 'Заказ WB', 'Наш статус', 'Статус WB'],
        diverged.tasks.map((task) => [task.task_id, task.wb_order_id, task.state, task.wb_status]))
      : '');

  const ledger = data.ledger_short || {};
  el('ledger').innerHTML = ledger.available
    ? table(['Владелец', 'Штрихкод', 'Ячейка', 'Кол-во', 'Когда'],
        (ledger.items || []).map((item) => [item.owner_external_id, item.barcode,
          item.cell_address, item.qty, item.created_at]))
    : '<p class="flash warn">' + text(ledger.reason) + '</p>';

  const disc = data.discrepancies || {};
  el('disc').innerHTML = disc.available
    ? metric(disc.pending, 'ждут решения') + table(
        ['Документ', 'Владелец', 'Тип', 'Штрихкод', 'Кол-во', 'Решение', 'Виноват'],
        (disc.items || []).map((item) => [item.reference, item.owner_external_id, item.kind,
          item.barcode, item.qty, item.decision, item.liable]))
    : '<p class="flash err">' + text(disc.reason) + '</p>';

  el('scans').innerHTML = table(['Задание', 'Этап', 'Штрихкод', 'Разбор', 'Кто', 'Когда'],
    (data.rejected_scans || []).map((scan) => [scan.task_id, scan.stage, scan.barcode,
      scan.scan_result, scan.actor_id, scan.scanned_at]));

  const printing = data.printing || {};
  const write = printing.last_write || {};
  el('printing').innerHTML = metric(printing.total_24h, 'печатей за сутки')
    + metric(printing.reprints_24h, 'из них перепечаток')
    + metric(printing.failed_24h, 'не напечаталось')
    + metric(printing.worst_agent_write_ms, 'худшая запись в устройство, мс (предел 50)')
    + '<p class="note">последняя запись: задание ' + text(write.task_id) + ', '
    + text(write.last_write_ms) + ' мс</p>';

  const cancels = data.cancellations || {};
  el('cancels').innerHTML = metric(cancels.without_reason, 'отмен без причины (должно быть 0)')
    + '<p class="note">' + text(cancels.note) + '</p>';

  el('sessions').innerHTML = table(['Лист', 'Сборщик', 'Строк', 'Снято', 'Начата'],
    (data.sessions || []).map((session) => [session.picklist_barcode, session.actor_id,
      session.lines_total, session.lines_picked, session.started_at]));
};

const metric = (value, caption) =>
  '<div class="metric"><b>' + text(value) + '</b><span>' + caption + '</span></div>';
const table = (headers, rows) => rows.length
  ? '<table><thead><tr>' + headers.map((h) => '<th>' + h + '</th>').join('')
    + '</tr></thead><tbody>' + rows.map((row) => '<tr>'
    + row.map((value) => '<td>' + text(value) + '</td>').join('') + '</tr>').join('')
    + '</tbody></table>'
  : '<p class="note">пусто</p>';

setInterval(load, 10000); load();
"""


PAGES = {
    "picking": ("Подбор", "/", PICKING_BODY, PICKING_JS),
    "receiving": ("Приёмка", "/receiving", RECEIVING_BODY, RECEIVING_JS),
    "putaway": ("Размещение", "/putaway", PUTAWAY_BODY, PUTAWAY_JS),
    "inventory": ("Инвентаризация", "/inventory", INVENTORY_BODY, INVENTORY_JS),
    "shipping": ("Отгрузка", "/shipping", SHIPPING_BODY, SHIPPING_JS),
    "supervisor": ("Начальник склада", "/supervisor", SUPERVISOR_BODY, SUPERVISOR_JS),
}


def page(name: str) -> str:
    title, active, body, script = PAGES[name]
    return _shell(title, active, body, script)


def error_page(message: str) -> str:
    return _shell("Ошибка", "/", f'<div class="panel"><p class="flash err">{message}</p></div>', "")


def picklist_page(view: dict[str, Any]) -> str:
    """Бумажный лист подбора.

    Печатается браузером станции. Здесь нет ни одной кнопки и ни одного
    запроса: лист уходит на склад в руках человека, и всё, что ему нужно
    знать, обязано быть напечатано — адрес, коробка, штрихкод и что именно
    брать.
    """
    session = view.get("session") or {}
    barcode = str(session.get("picklist_barcode") or "")
    lines = view.get("lines") or []
    rows = "".join(
        f"<tr><td>{index}</td>"
        f'<td class="addr">{_esc(line.get("cell_address"))}</td>'
        f'<td class="mono">{_esc(line.get("box_barcode"))}</td>'
        f'<td>{_esc(line.get("name"))}</td>'
        f'<td class="mono">{_esc(line.get("barcode"))}</td>'
        f'<td style="font-size:20px;font-weight:700">{_esc(line.get("quantity"))}</td>'
        f'<td>{_esc(line.get("owner_external_id"))}</td>'
        f"<td style='width:80px'>&nbsp;</td></tr>"
        for index, line in enumerate(lines, start=1))
    code = barcode_svg(barcode, module_width=2.0, height=60.0) if barcode else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Лист подбора {_esc(barcode)}</title>
<style>{BASE_CSS}
body {{ background: #fff; }}
main {{ padding: 12px; }}
</style></head><body><main>
<div class="panel">
  <div class="row" style="justify-content:space-between;align-items:flex-start">
    <div><h2 style="font-size:22px;margin:0">Лист подбора</h2>
      <p class="note">Сборщик: {_esc(session.get("actor_id"))} ·
         строк: {len(lines)} · начат: {_esc(session.get("started_at"))}</p></div>
    <div>{code}</div>
  </div>
  <table><thead><tr><th>#</th><th>Адрес</th><th>Коробка</th><th>Товар</th>
    <th>Штрихкод</th><th>Кол-во</th><th>Владелец</th><th>Отметка</th></tr></thead>
  <tbody>{rows}</tbody></table>
  <p class="note">Порядок строк — маршрут обхода склада. Сканируйте штрихкод
     коробки и товара у стойки; контрольный скан делается при упаковке за ПК.</p>
</div>
<script class="noprint">window.print();</script>
</main></body></html>"""


def _esc(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
