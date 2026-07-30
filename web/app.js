"use strict";

const stateNode = document.getElementById("state");
const bodyNode = document.getElementById("status-body");
const cardsNode = document.getElementById("cards");
const platformFilter = document.getElementById("platform-filter");
const brandFilter = document.getElementById("brand-filter");
const outcomeFilter = document.getElementById("outcome-filter");
const freshnessFilter = document.getElementById("freshness-filter");
const modelFilter = document.getElementById("model-filter");
const sortOrder = document.getElementById("sort-order");
const historyLoad = document.getElementById("history-load");
const historyList = document.getElementById("history-list");
const historyPrev = document.getElementById("history-prev");
const historyNext = document.getElementById("history-next");
const historyPageNode = document.getElementById("history-page");
const HISTORY_PAGE_SIZE = 50;
let rows = [];
let historyRows = [];
let historyPage = 0;

function setState(name, message) {
  stateNode.dataset.state = name;
  stateNode.textContent = message;
}

function safeProductLink(row) {
  if (!row.product_url) return null;
  const allowed = {
    taobao: ["item.taobao.com", "detail.tmall.com"],
    jd: ["item.jd.com"],
    pdd: ["mobile.yangkeduo.com", "www.pinduoduo.com"],
  };
  try {
    const parsed = new URL(row.product_url);
    if (parsed.protocol !== "https:" || !(allowed[row.platform] || []).includes(parsed.hostname)) return null;
    return parsed.href;
  } catch (_error) {
    return null;
  }
}

function cell(value) {
  const node = document.createElement("td");
  node.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  return node;
}

function timestamp(row) {
  const value = Date.parse(row.finished_at || row.checked_at || "");
  return Number.isFinite(value) ? value : 0;
}

function currentPrice(row) {
  const value = Number(row.price);
  return row.outcome === "success" && Number.isFinite(value) ? value : Number.POSITIVE_INFINITY;
}

function render() {
  bodyNode.replaceChildren();
  cardsNode.replaceChildren();
  const query = modelFilter.value.trim().toLowerCase();
  const visible = rows.filter((row) =>
    (!platformFilter.value || row.platform === platformFilter.value) &&
    (!brandFilter.value || row.brand === brandFilter.value) &&
    (!outcomeFilter.value || row.outcome === outcomeFilter.value) &&
    (!freshnessFilter.value || row.price_status === freshnessFilter.value) &&
    (!query || String(row.model_key || "").toLowerCase().includes(query))
  );
  visible.sort(sortOrder.value === "price"
    ? (left, right) => currentPrice(left) - currentPrice(right) || timestamp(right) - timestamp(left)
    : (left, right) => timestamp(right) - timestamp(left));

  visible.forEach((row) => {
    const tr = document.createElement("tr");
    tr.append(cell(row.platform_name || row.platform), cell(row.model_key), cell(row.outcome));
    const priceCell = cell(row.price === null ? null : `¥${Number(row.price).toFixed(2)}`);
    const href = safeProductLink(row);
    if (href) {
      const link = document.createElement("a");
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = priceCell.textContent;
      priceCell.replaceChildren(link);
    }
    tr.append(priceCell, cell(row.rejection_reason || row.block_reason), cell(row.finished_at || row.checked_at));
    bodyNode.append(tr);

    const card = document.createElement("article");
    const title = document.createElement("h2");
    title.textContent = row.model_key;
    const detail = document.createElement("p");
    const shownPrice = row.price === null || row.price === undefined ? "无本轮价格" : `¥${Number(row.price).toFixed(2)}`;
    detail.textContent = `${row.platform_name || row.platform} · ${row.outcome} · ${shownPrice}`;
    card.append(title, detail);
    cardsNode.append(card);
  });
}

function addOptions(select, values) {
  [...new Set(values.filter((value) => value))].sort().forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  });
}

async function readJson(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) throw new Error("payload unavailable");
  return response.json();
}

async function historyExists() {
  try {
    const history = await fetch("data/price_history.json", {cache: "no-store"});
    if (!history.ok) return false;
    const events = await history.json();
    return Array.isArray(events) && events.length > 0;
  } catch (_error) {
    return false;
  }
}

function renderHistory() {
  historyList.replaceChildren();
  const pages = Math.max(1, Math.ceil(historyRows.length / HISTORY_PAGE_SIZE));
  historyPage = Math.min(historyPage, pages - 1);
  const start = historyPage * HISTORY_PAGE_SIZE;
  historyRows.slice(start, start + HISTORY_PAGE_SIZE).forEach((row) => {
    const item = document.createElement("li");
    item.textContent = `${row.platform_name || row.platform} · ${row.model_key} · ¥${Number(row.price).toFixed(2)} · ${row.observed_at}`;
    historyList.append(item);
  });
  historyPageNode.textContent = historyRows.length ? `${historyPage + 1} / ${pages}` : "无历史";
  historyPrev.disabled = historyPage === 0;
  historyNext.disabled = historyPage + 1 >= pages;
}

async function loadHistory() {
  try {
    const response = await fetch("data/price_history.json", {cache: "no-store"});
    if (!response.ok) throw new Error("history unavailable");
    const events = await response.json();
    historyRows = Array.isArray(events)
      ? events.slice().sort((left, right) => Date.parse(right.observed_at) - Date.parse(left.observed_at))
      : [];
    historyPage = 0;
    renderHistory();
    historyLoad.disabled = true;
  } catch (_error) {
    historyPageNode.textContent = "历史加载失败";
  }
}

async function load() {
  try {
    const manifest = await readJson("manifest.json");
    const audit = await readJson("audit.json");
    rows = await readJson("data/status.json");
    if (manifest.schema_version !== 4 || manifest.mode !== "live" || audit.structure_status !== "pass") {
      setState("structure-blocked", "structure-blocked：本轮结构审计未通过");
      return;
    }
    if (!Array.isArray(rows) || rows.length === 0) {
      const hasHistory = await historyExists();
      setState(hasHistory ? "history-only" : "empty",
        hasHistory ? "history-only：只有历史记录" : "empty：本轮确实无状态");
      return;
    }
    setState(audit.product_status === "pass" ? "ready" : "live-blocked",
      audit.product_status === "pass" ? "本轮产品门通过" : "live-blocked：真实阻断和失败已完整展示");
    document.getElementById("batch-summary").textContent = `${manifest.batch_id} · ${manifest.source_sha}`;
    addOptions(platformFilter, rows.map((row) => row.platform));
    addOptions(brandFilter, rows.map((row) => row.brand));
    addOptions(outcomeFilter, rows.map((row) => row.outcome));
    render();
  } catch (_error) {
    setState("structure-blocked", "structure-blocked：公开数据加载或校验失败");
  }
}

[platformFilter, brandFilter, outcomeFilter, freshnessFilter, modelFilter, sortOrder]
  .forEach((node) => node.addEventListener("input", render));
historyLoad.addEventListener("click", loadHistory);
historyPrev.addEventListener("click", () => {
  historyPage -= 1;
  renderHistory();
});
historyNext.addEventListener("click", () => {
  historyPage += 1;
  renderHistory();
});
load();
