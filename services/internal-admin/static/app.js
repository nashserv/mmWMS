"use strict";
// Ванильный JS (раздел 2.2 мастера). Ни сборки, ни фреймворка: экран админки
// меняется реже, чем правила денег, и держать ради него отдельный тулчейн
// дороже, чем написать двести строк.
//
// Логики о деньгах здесь нет ни строки. Всё, что похоже на решение, считает
// биллинг: вторая копия правил однажды разойдётся с первой, и разойдётся она
// в счёте клиента.

const API = "/api/admin/v1";
const SERVICES = ["receiving", "storage", "labeling", "packing", "picking", "shipping",
                  "returns", "order_processing"];
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
    else if (key === "style") node.setAttribute("style", value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
};

function token() { return $("#token").value.trim(); }

async function call(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (token()) headers.Authorization = `Bearer ${token()}`;
  const response = await fetch(API + path, Object.assign({}, options, { headers }));
  const body = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) {
    // Отказ показываем как есть: «партнёр не в вашей ветке» объясняет
    // происходящее лучше, чем пустая таблица.
    throw new Error(body.error || body.reason || `HTTP ${response.status}`);
  }
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

function table(columns, rows, rowClass) {
  const head = el("tr", {}, columns.map((column) =>
    el("th", { class: column.num ? "num" : null }, column.title)));
  const body = rows.map((row) => el("tr", { class: rowClass ? rowClass(row) : null },
    columns.map((column) => el("td", { class: column.num ? "num" : null },
      column.value(row) ?? "—"))));
  if (!rows.length) {
    return el("p", { class: "muted" }, "пусто");
  }
  return el("table", {}, [el("thead", {}, head), el("tbody", {}, body)]);
}

function money(value) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString("ru-RU", { minimumFractionDigits: 2,
                                                 maximumFractionDigits: 2 });
}

// ------------------------------------------------------------------- вкладки

document.querySelectorAll("#tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((other) =>
      other.classList.toggle("active", other === button));
    document.querySelectorAll(".tab").forEach((tab) =>
      tab.classList.toggle("active", tab.id === button.dataset.tab));
    render(button.dataset.tab);
  });
});

// -------------------------------------------------------------------- экраны

async function renderOverview() {
  const data = await call(`/overview`);
  $("#need-onboarding").textContent = data.needs_onboarding.length;
  const unbilledTotal = data.unbilled.reduce((sum, row) => sum + Number(row.events || 0), 0);
  $("#unbilled-total").textContent = data.restricted.unbilled ? "нет доступа" : unbilledTotal;
  const losing = data.margin.filter((row) => Number(row.margin) < 0).length;
  $("#loss-making").textContent = data.restricted.margin ? "нет доступа" : losing;

  $("#unbilled").replaceChildren(data.restricted.unbilled
    ? el("p", { class: "muted" }, "отчёт виден администратору и бухгалтеру")
    : table([
        { title: "Причина", value: (row) => row.reason },
        { title: "Событий", num: true, value: (row) => row.events },
        { title: "С", value: (row) => (row.since || "").slice(0, 19).replace("T", " ") },
      ], data.unbilled));

  $("#margin").replaceChildren(data.restricted.margin
    ? el("p", { class: "muted" }, "себестоимость — внутреннее число MM-Express")
    : table([
        { title: "Кабинет", value: (row) => row.cabinet_name || row.seller_external_id },
        { title: "Выручка", num: true, value: (row) => money(row.gross_revenue) },
        { title: "Партнёру", num: true, value: (row) => money(row.partner_fee) },
        { title: "Нам", num: true, value: (row) => money(row.net_revenue) },
        { title: "Себестоимость", num: true, value: (row) => money(row.allocated_cost) },
        { title: "Маржа", num: true, value: (row) => money(row.margin) },
        { title: "Операций", num: true, value: (row) => row.operations },
      ], data.margin, (row) => (Number(row.margin) < 0 ? "negative" : null)));
}

async function loadPartnersInto(select) {
  const data = await call("/partners");
  select.replaceChildren(el("option", { value: "" }, "— без партнёра —"),
    ...data.partners.map((partner) =>
      el("option", { value: partner.id }, `${"— ".repeat(partner.depth)}${partner.name}`)));
  return data.partners;
}

async function renderPartners() {
  const partners = await loadPartnersInto($("#markup-partner"));
  $("#partner-tree").replaceChildren(table([
    { title: "Партнёр", value: (row) =>
        el("span", { class: "tree-row", style: `--depth:${row.depth}` }, row.name) },
    { title: "Кабинетов", num: true, value: (row) => row.active_cabinets },
    { title: "Пользователь", value: (row) => row.user_id || "не заведён" },
    { title: "", value: (row) => el("button", { class: "action", onclick: async () => {
        try {
          const period = new Date().toISOString().slice(0, 7);
          const commission = await call(
            `/partners/${row.id}/commission?period=${period}`);
          toast(`${row.name}: своя ${money(commission.own.amount)} ₽, ` +
                `ветка ${money(commission.branch_total)} ₽ за ${period}`);
        } catch (error) { toast(error.message, true); }
      } }, "комиссия") },
  ], partners));
  $("#markup-service").replaceChildren(...SERVICES.map((service) =>
    el("option", { value: service }, SERVICE_NAMES[service])));
}

async function renderTariffs() {
  const data = await call("/tariffs");
  $("#tariff-list").replaceChildren(table([
    { title: "Услуга", value: (row) => SERVICE_NAMES[row.service] || row.service },
    { title: "Код", value: (row) => row.code },
    { title: "Единица", value: (row) => row.unit },
    { title: "Цена MM-Express", num: true, value: (row) =>
        (row.tiers || []).map((tier) => money(tier.unit_price)).join(" / ") },
    { title: "Наценка по умолчанию", num: true, value: (row) => money(row.partner_fee) },
    { title: "Действует с", value: (row) => row.effective_from },
    { title: "Состояние", value: (row) => row.approved
        ? el("span", { class: "badge ok" }, "утверждена")
        : el("span", { class: "badge warn" }, "не утверждена") },
    { title: "", value: (row) => (row.approved || !row.version_id) ? "" :
        el("button", { class: "action", onclick: async () => {
          try {
            await call(`/tariff-versions/${row.version_id}/approve`, { method: "POST",
              body: JSON.stringify({}) });
            toast("версия утверждена");
            renderTariffs();
          } catch (error) { toast(error.message, true); }
        } }, "утвердить") },
  ], data.tariffs || []));
}

async function renderAccruals() {
  const period = new FormData($("#accrual-filter")).get("period");
  const data = await call("/accruals" + (period ? `?period=${encodeURIComponent(period)}` : ""));
  $("#accrual-list").replaceChildren(table([
    { title: "Дата", value: (row) => row.occurred_on },
    { title: "Кабинет", value: (row) => row.cabinet_seller },
    { title: "Услуга", value: (row) => SERVICE_NAMES[row.service] || row.service },
    { title: "Кол-во", num: true, value: (row) => row.quantity },
    { title: "Клиент платит", num: true, value: (row) => money(row.amount) },
    { title: "Партнёру", num: true, value: (row) => money(row.partner_amount) },
    { title: "MM-Express", num: true, value: (row) => money(row.net_amount) },
    { title: "Партнёр", value: (row) => row.partner_name || "—" },
    { title: "Выплата", value: (row) => row.partner_payout_state },
  ], data.accruals || []));
}

async function renderAudit() {
  const rows = await call("/audit?limit=100");
  $("#audit-list").replaceChildren(table([
    { title: "Когда", value: (row) => row.at.slice(0, 19).replace("T", " ") },
    { title: "Кто", value: (row) => row.actor_id },
    { title: "Роли", value: (row) => (row.actor_roles || []).join(", ") },
    { title: "Действие", value: (row) => row.action },
    { title: "Над чем", value: (row) => row.subject },
    { title: "Итог", value: (row) => row.status < 400
        ? el("span", { class: "badge ok" }, row.status)
        : el("span", { class: "badge warn" }, row.status) },
  ], rows));
}

async function render(tab) {
  try {
    if (tab === "overview") await renderOverview();
    else if (tab === "partners") await renderPartners();
    else if (tab === "tariffs") await renderTariffs();
    else if (tab === "accruals") await renderAccruals();
    else if (tab === "audit") await renderAudit();
    else if (tab === "onboarding") await loadPartnersInto($("#onboard-partner"));
  } catch (error) {
    toast(error.message, true);
  }
}

// ------------------------------------------------------------------- формы

$("#onboard-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  // «штрихкод;количество;ячейка» на строку — как диктует владелец, дающий
  // остаток, а не как удобно форме.
  const opening = String(form.get("opening_stock") || "").split("\n")
    .map((line) => line.trim()).filter(Boolean)
    .map((line) => {
      const [barcode, quantity, cell] = line.split(";").map((part) => part.trim());
      return { barcode, quantity: Number(quantity), cell_address: cell, state: "good" };
    });
  const body = {
    seller_external_id: form.get("seller_external_id"),
    name: form.get("name"),
    inn: form.get("inn") || null,
    contract_reference: form.get("contract_reference"),
    partner_id: form.get("partner_id") || null,
    wb_account_external_id: form.get("wb_account_external_id") || null,
    secret_ref: form.get("secret_ref") || null,
    from_date: form.get("from_date") || null,
    opening_stock: opening,
  };
  try {
    const result = await call("/onboarding", { method: "POST", body: JSON.stringify(body) });
    // Показываем шаги целиком: часть из них идёт в склад вне транзакции, и
    // «сделано столько-то, повторить с такого шага» — честный ответ.
    $("#onboard-result").replaceChildren(
      el("h3", {}, result.state === "ok" ? "Клиент заведён" : "Заведён не до конца"),
      table([
        { title: "Шаг", value: (row) => row.step },
        { title: "Состояние", value: (row) => row.state },
        { title: "Подробности", value: (row) =>
            row.detail || row.reference || row.owner_id || row.secret_ref || "" },
      ], result.steps || []));
    toast(result.state === "ok" ? "клиент заведён" : "заведён не до конца — см. шаги",
          result.state !== "ok");
  } catch (error) {
    $("#onboard-result").replaceChildren(el("pre", {}, error.message));
    toast(error.message, true);
  }
});

$("#markup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    await call(`/partners/${form.get("partner_id")}/markups`, {
      method: "POST",
      body: JSON.stringify({ service: form.get("service"), markup: form.get("markup"),
                             from_date: form.get("from_date") || null }),
    });
    toast("наценка поставлена: прошлые периоды не пересчитаны");
  } catch (error) { toast(error.message, true); }
});

$("#accrual-filter").addEventListener("submit", (event) => {
  event.preventDefault();
  renderAccruals().catch((error) => toast(error.message, true));
});

// ------------------------------------------------------------------- вход

$("#token").addEventListener("change", async () => {
  localStorage.setItem("mmx-admin-token", token());
  await whoami();
  render(document.querySelector("#tabs button.active").dataset.tab);
});

async function whoami() {
  try {
    const principal = await call("/whoami");
    $("#whoami").textContent = principal.active
      ? `${principal.user_id} · ${(principal.roles || []).join(", ")}`
      : (principal.reason || "не представлен");
  } catch (error) {
    $("#whoami").textContent = error.message;
  }
}

$("#token").value = localStorage.getItem("mmx-admin-token") || "";
whoami().then(() => render("overview"));
