"use strict";
// Ванильный JS (раздел 2.2 мастера).
//
// Ни одного числа портал не считает сам и ни одного не запоминает. Остаток —
// у склада, деньги — у биллинга. Сумма, посчитанная здесь и разошедшаяся с
// той, что в счёте, — это ровно 3528 против 92, только в другом месте.

const API = "/api/portal/v1";
const SERVICE_NAMES = {
  receiving: "Приёмка", storage: "Хранение", labeling: "Стикеровка", packing: "Упаковка",
  picking: "Сборка", shipping: "Отгрузка", returns: "Возврат",
  order_processing: "Обработка заказов",
};

const $ = (selector) => document.querySelector(selector);
const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
};

function token() { return $("#token").value.trim(); }

async function call(path) {
  const headers = {};
  if (token()) headers.Authorization = `Bearer ${token()}`;
  const response = await fetch(API + path, { headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || body.reason || `HTTP ${response.status}`);
  return body;
}

function toast(message, isError) {
  const node = $("#toast");
  node.textContent = message;
  node.style.background = isError ? "#c0392b" : "#1b1f24";
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 6000);
}

function table(columns, rows) {
  if (!rows.length) return el("p", { class: "muted" }, "пусто");
  return el("table", {}, [
    el("thead", {}, el("tr", {}, columns.map((column) =>
      el("th", { class: column.num ? "num" : null }, column.title)))),
    el("tbody", {}, rows.map((row) => el("tr", {}, columns.map((column) =>
      el("td", { class: column.num ? "num" : null }, column.value(row) ?? "—"))))),
  ]);
}

const money = (value) => Number(value || 0).toLocaleString("ru-RU",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });

document.querySelectorAll("#tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((other) =>
      other.classList.toggle("active", other === button));
    document.querySelectorAll(".tab").forEach((tab) =>
      tab.classList.toggle("active", tab.id === button.dataset.tab));
    render(button.dataset.tab);
  });
});

async function renderStock() {
  const data = await call("/stock");
  $("#total-available").textContent = data.total_available;
  $("#total-reserved").textContent = data.total_reserved;
  $("#total-good").textContent = data.total_good;
  $("#stock-table").replaceChildren(table([
    { title: "Штрихкод", value: (row) => row.barcode },
    { title: "На полке", num: true, value: (row) => row.good },
    { title: "В резерве", num: true, value: (row) => row.reserved },
    { title: "Свободно", num: true, value: (row) => row.available },
  ], data.stock || []));
  $("#placements").replaceChildren(table([
    { title: "Коробка", value: (row) => row.box_barcode },
    { title: "Ячейка", value: (row) => row.cell },
    { title: "Количество", num: true, value: (row) => row.quantity },
    { title: "Комментарий склада", value: (row) => row.comment },
  ], data.placements || []));
}

async function renderCharges() {
  const period = new FormData($("#period-form")).get("period");
  const query = period ? `?period=${encodeURIComponent(period)}` : "";
  const data = await call("/accruals" + query);
  $("#explanation").textContent = data.explanation;
  $("#total-amount").textContent = money(data.totals.amount) + " ₽";
  $("#total-net").textContent = money(data.totals.net_amount) + " ₽";
  $("#total-partner").textContent = money(data.totals.partner_amount) + " ₽";
  $("#download-act").onclick = (event) => {
    event.preventDefault();
    downloadAct(data.period);
  };
  $("#charge-table").replaceChildren(table([
    { title: "Дата", value: (row) => row.occurred_on },
    { title: "Услуга", value: (row) => SERVICE_NAMES[row.service] || row.service },
    { title: "Кол-во", num: true, value: (row) => row.quantity },
    { title: "К оплате", num: true, value: (row) => money(row.amount) },
    { title: "MM-Express", num: true, value: (row) => money(row.net_amount) },
    { title: "Партнёру", num: true, value: (row) => money(row.partner_amount) },
    { title: "Партнёр", value: (row) => row.partner_name || "—" },
  ], data.accruals || []));
}

async function downloadAct(period) {
  // Через fetch, а не ссылкой: акт отдаётся под токеном, а токен в адресной
  // строке — это токен в истории браузера и в логе прокси.
  try {
    const response = await fetch(`${API}/accruals.csv?period=${encodeURIComponent(period)}`,
                                 { headers: { Authorization: `Bearer ${token()}` } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    const link = el("a", { href: URL.createObjectURL(blob), download: `act-${period}.csv` });
    document.body.append(link);
    link.click();
    link.remove();
    toast("акт выгружен");
  } catch (error) { toast(error.message, true); }
}

async function renderExports() {
  const data = await call("/exports");
  $("#export-list").replaceChildren(table([
    { title: "Когда", value: (row) => row.at.slice(0, 19).replace("T", " ") },
    { title: "Что", value: (row) => row.kind },
    { title: "Период", value: (row) => row.period },
    { title: "Строк", num: true, value: (row) => row.rows_count },
  ], data.exports || []));
}

async function render(tab) {
  try {
    if (tab === "stock") await renderStock();
    else if (tab === "charges") await renderCharges();
    else if (tab === "exports") await renderExports();
  } catch (error) { toast(error.message, true); }
}

$("#period-form").addEventListener("submit", (event) => {
  event.preventDefault();
  renderCharges().catch((error) => toast(error.message, true));
});

$("#token").addEventListener("change", async () => {
  localStorage.setItem("mmx-portal-token", token());
  await whoami();
  render(document.querySelector("#tabs button.active").dataset.tab);
});

async function whoami() {
  try {
    const me = await call("/me");
    $("#whoami").textContent = `${me.seller} · ${(me.roles || []).join(", ")}`;
  } catch (error) {
    $("#whoami").textContent = error.message;
  }
}

$("#token").value = localStorage.getItem("mmx-portal-token") || "";
whoami().then(() => render("stock"));
