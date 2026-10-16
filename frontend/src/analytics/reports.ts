import { debounce } from "../shared/debounce";
import { fetchJSON } from "../shared/http";
import { $, setText, esc, pct } from "../shared/dom";

// ============ Вкладка «Анализ» → подвкладка «Прогоны»: report_runs / report_rows / report_trades
// + источник «Реплеи» — живые тесты бота из /api/v1/analysis/replays ============

type RepId = number | string;
type RepSource = "harness" | "replay";

interface RepRun {
  id: RepId;
  file_name: string;
  kind: string;
  name: string | null;
  created_at: string | null;
  period: string[] | null;
  interval: string | null;
  robots: number;
  trades: number;
  wins: number;
  gw: number;
  gl: number;
  net: number;
  detail_trades: number;
  pf?: number | null;
  winrate?: number | null;
  open?: number;
  preset_id?: string | null;
  has_sidecar?: boolean;
  updated_at?: string | null;
}

interface RepMetrics {
  trades: number;
  wins: number;
  losses?: number;
  gw: number;
  gl: number;
  net: number;
  wr: number | null;
  pf: number | null;
  commission?: number | null;
  max_dd_pct?: number | null;
}

interface RepStrategy extends RepMetrics {
  strategy: string;
  tickers: number;
  exits: string[];
}

interface RepRow extends RepMetrics {
  id: number;
  strategy: string;
  exit: string;
  ticker: string;
}

interface RepCell extends RepMetrics {
  bucket: string;
}

interface RepSliceTable {
  strategy: string;
  cells: RepCell[];
  total: RepMetrics;
  best_bucket: string | null;
}

interface RepDim {
  dim: string;
  label: string;
  buckets: string[];
  table: RepSliceTable[];
}

interface RepTagItem {
  k: string;
  v: string | boolean | number | null;
}

interface RepTagGroup {
  group: string;
  items: RepTagItem[];
}

interface RepConfigTags {
  has_sidecar: boolean;
  preset_id: string | null;
  notes: string;
  groups: RepTagGroup[];
}

interface RepDetail {
  run: {
    id: RepId;
    file_name: string;
    kind: string;
    name: string | null;
    created_at: string | null;
    period: string[] | null;
    interval: string | null;
    commission?: number | null;
    slippage_bps?: number | null;
  };
  summary: RepMetrics & { strategies: number; tickers: number; open_positions?: number };
  strategies: RepStrategy[];
  rows: RepRow[];
  dims: string[];
  dim_labels: Record<string, string>;
  config_tags?: RepConfigTags;
}

interface RepTrade {
  id: number;
  strategy: string;
  exit: string;
  ticker: string;
  side: string;
  entry_time: string | null;
  exit_time: string | null;
  entry_price: number | null;
  exit_price: number | null;
  exit_reason: string | null;
  bars_held: number | null;
  net_pnl: number;
  sl_price: number | null;
  tp_price: number | null;
  mae_atr: number | null;
  mfe_atr: number | null;
  session: string | null;
  regime_adx: string | null;
  er_in: number | null;
}

interface RepTradesResp {
  total: number;
  limit: number;
  offset: number;
  summary: Record<string, number | null>;
  items: RepTrade[];
}

interface RepLeader {
  ticker: string;
  pct: number | null;
  vol: number | null;
  close: number | null;
  bars: number;
  first_ts: string | null;
  last_ts: string | null;
}

interface RepLeadersResp {
  window: string;
  interval: number;
  count: number;
  leaders: RepLeader[];
  outsiders: RepLeader[];
}

type RepTab = "strategies" | "slices" | "trades" | "market";
type RepWin = "day" | "week" | "month";

const REP_DIM_ORDER = ["session", "regime", "regime_adx", "er", "hour", "weekday", "side", "exit", "entry", "ticker"];
const REP_KIND_LABEL: Record<string, string> = {
  real: "real", wf: "wf", matrix: "matrix", exp: "exp", replay: "replay",
};
const REP_PAGE = 200;

let repLoaded = false;
let repSource: RepSource = "harness";
let repRuns: RepRun[] = [];
let repRunId: RepId | null = null;
let repDetail: RepDetail | null = null;
let repTab: RepTab = "strategies";
let repDim = "session";
let repSortKey = "trades";
let repSortDir: 1 | -1 = -1;
let repSlicesCache: Record<string, RepDim[]> = {};
let repTrades: RepTrade[] = [];
let repTradesTotal = 0;
let repTradesOffset = 0;
let repTradesSummary: Record<string, number | null> = {};
let repTF = { strategy: "", ticker: "", outcome: "", side: "" };
let repWin: RepWin = "week";
let repMarketLoaded = false;

function repN(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function repRub(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function repSignCls(n: number | null | undefined): string {
  if (n === null || n === undefined || n === 0) return "";
  return n > 0 ? "rep-pos" : "rep-neg";
}

function repTs(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getDate())}.${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function repExp(m: RepMetrics): number | null {
  return m.trades ? m.net / m.trades : null;
}

function repLosses(m: RepMetrics): number {
  return m.losses ?? m.trades - m.wins;
}

function repCard(key: string, value: string, cls = ""): string {
  return `<div class="rep-card"><span class="rep-c-k">${esc(key)}</span>` +
    `<span class="rep-c-v ${cls}">${value}</span></div>`;
}

function repListUrl(): string {
  if (repSource === "replay") return `/api/v1/analysis/replays`;
  const kind = (document.getElementById("rep-kind") as HTMLSelectElement | null)?.value ?? "";
  return `/api/v1/analysis/reports` + (kind ? `?kind=${encodeURIComponent(kind)}` : "");
}

function repRunApi(suffix: string): string {
  if (repRunId === null) return "";
  return repSource === "replay"
    ? `/api/v1/analysis/replays/${encodeURIComponent(String(repRunId))}${suffix}`
    : `/api/v1/analysis/reports/${repRunId}${suffix}`;
}

async function loadRepRuns(): Promise<void> {
  const list = $("rep-list");
  if (list) list.innerHTML = `<span class="mini-hint">загрузка…</span>`;
  try {
    const q = ((document.getElementById("rep-query") as HTMLInputElement | null)?.value ?? "").trim().toLowerCase();
    const resp = await fetchJSON<{ runs: RepRun[]; count: number }>(repListUrl());
    repRuns = resp.runs.filter((r) =>
      !q || r.file_name.toLowerCase().includes(q) || (r.name ?? "").toLowerCase().includes(q));
    setText("rep-sub", `${repRuns.length} из ${resp.count}`);
    if (list) list.innerHTML = "";
    if (!repRuns.length) {
      if (list) list.innerHTML = `<span class="mini-hint">${
        repSource === "replay"
          ? "нет реплей-тестов — запусти тест (страница Бот) или preset.py replay --start"
          : (q ? "нет прогонов по фильтру" : "нет прогонов — запусти scripts/import_reports.py")}</span>`;
      repRunId = null;
      repDetail = null;
      renderRepRunHead();
      renderRepActions();
      return;
    }
    if (repRunId === null || !repRuns.some((r) => r.id === repRunId)) repRunId = repRuns[0].id;
    renderRepList();
    await selectRepRun(repRunId);
  } catch (e) {
    if (list) list.innerHTML = `<span class="mini-hint">ошибка: ${esc(String(e))}</span>`;
  }
}

function renderRepList(): void {
  const list = $("rep-list");
  if (!list) return;
  list.innerHTML = repRuns.map((r) => {
    const kindCls = REP_KIND_LABEL[r.kind] ?? r.kind;
    const meta = repSource === "replay"
      ? `<span>${repTs(r.created_at)}</span><span>сделок ${repN(r.trades)}</span>` +
        (r.open ? `<span>открытых ${repN(r.open)}</span>` : "") +
        `<span class="${repSignCls(r.net)}">net ${repRub(r.net)}</span>`
      : `<span>${repTs(r.created_at)}</span><span>сделок ${repN(r.trades)}</span>` +
        `<span>роботов ${repN(r.robots)}</span>` +
        `<span class="${repSignCls(r.net)}">net ${repRub(r.net)}</span>`;
    const preset = r.preset_id
      ? `<span class="rep-preset" title="пресет">${esc(r.preset_id)}</span>` : "";
    return `<div class="rep-item ${r.id === repRunId ? "active" : ""}" data-run="${esc(String(r.id))}" title="${esc(r.file_name)}">` +
      `<div class="rep-i-name"><span class="rep-kind ${esc(kindCls)}">${esc(r.kind)}</span> ${esc(r.file_name)} ${preset}</div>` +
      `<div class="rep-i-meta">${meta}</div></div>`;
  }).join("");
}

async function selectRepRun(id: RepId): Promise<void> {
  repRunId = id;
  repDetail = null;
  repTrades = [];
  repTradesTotal = 0;
  repTradesOffset = 0;
  repTradesSummary = {};
  repSlicesCache = {};
  repDim = "session";
  renderRepList();
  renderRepActions();
  const head = $("rep-runhead");
  if (head) head.innerHTML = `<span class="mini-hint">загрузка прогона ${esc(String(id))}…</span>`;
  try {
    repDetail = await fetchJSON<RepDetail>(repRunApi(""));
    const dims = repDetail.dims;
    repDim = REP_DIM_ORDER.find((d) => dims.includes(d)) ?? dims[0] ?? "session";
    renderRepRunHead();
    renderRepActions();
    renderRepStrategies();
    renderRepDims();
    renderRepPane();
  } catch (e) {
    if (head) head.innerHTML = `<span class="mini-hint">ошибка загрузки: ${esc(String(e))}</span>`;
  }
}

function repTagValue(v: RepTagItem["v"]): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "вкл" : "выкл";
  return String(v);
}

function renderRepTags(tags: RepConfigTags): string {
  if (!tags.has_sidecar || !tags.groups.length) {
    return `<div class="rep-tags"><span class="mini-hint">⚠ нет сайдкара пресета — настройки теста неизвестны (запускай через preset.py replay или выбери пресет в модалке)</span></div>`;
  }
  return `<div class="rep-tags">` + tags.groups.map((g) =>
    `<div class="rep-tag-row"><span class="rep-tag-g">${esc(g.group)}</span>` +
    g.items.map((i) =>
      `<span class="td-chip" title="${esc(g.group)}: ${esc(String(i.k))}">${esc(String(i.k))}: ${esc(repTagValue(i.v))}</span>`
    ).join("") + `</div>`).join("") + `</div>`;
}

function renderRepRunHead(): void {
  const head = $("rep-runhead");
  if (!head) return;
  const d = repDetail;
  if (!d) {
    head.innerHTML = `<span class="mini-hint">${repRunId === null ? "выбери прогон слева" : "—"}</span>`;
    return;
  }
  const s = d.summary;
  const period = d.run.period && d.run.period.length ? d.run.period.join(" … ") : "—";
  head.innerHTML =
    `<b>${esc(d.run.file_name)}</b>` +
    `<span class="rep-kind ${esc(REP_KIND_LABEL[d.run.kind] ?? d.run.kind)}">${esc(d.run.kind)}</span>` +
    `<div class="rep-rh-stats">` +
    `<span>${repTs(d.run.created_at)}</span>` +
    `<span>период ${esc(period)}</span>` +
    `<span>TF ${esc(d.run.interval ?? "—")}</span>` +
    `<span>роботов ${repN(s.strategies)}</span>` +
    `<span>тикеров ${repN(s.tickers)}</span>` +
    `<span>сделок ${repN(s.trades)}</span>` +
    (s.open_positions ? `<span>открытых ${repN(s.open_positions)}</span>` : "") +
    `<span>Gross W <span class="rep-pos">${repRub(s.gw)}</span></span>` +
    `<span>Gross L <span class="rep-neg">${repRub(s.gl)}</span></span>` +
    `<span class="${repSignCls(s.net)}">net ${repRub(s.net)}</span>` +
    `<span>WR ${s.wr === null ? "—" : s.wr + "%"}</span>` +
    `<span>PF ${s.pf === null ? "—" : s.pf}</span>` +
    `</div>` +
    (d.config_tags ? renderRepTags(d.config_tags) : "");
}

function renderRepActions(): void {
  const box = $("rep-actions");
  if (!box) return;
  const show = repSource === "replay" && repDetail !== null && repRunId !== null;
  box.classList.toggle("hidden", !show);
  if (!show) {
    box.innerHTML = "";
    return;
  }
  const preset = repDetail?.config_tags?.preset_id;
  box.innerHTML =
    `<button class="btn-secondary btn-xs" data-repact="restart" title="Тот же конфиг и окно, новое имя">↻ Перезапустить как новый</button>` +
    `<button class="btn-secondary btn-xs btn-danger" data-repact="delete">🗑 Удалить тест</button>` +
    (preset ? `<span class="mini-hint">пресет ${esc(preset)} · перезапуск не трогает историю старого теста</span>`
            : `<span class="mini-hint">без пресета — конфиг возьмётся из текущих настроек бота</span>`);
}

const REP_SORT: Record<string, (s: RepStrategy) => number | string> = {
  strategy: (s) => s.strategy,
  trades: (s) => s.trades,
  wins: (s) => s.wins,
  losses: (s) => repLosses(s),
  wr: (s) => s.wr ?? -1,
  gw: (s) => s.gw,
  gl: (s) => s.gl,
  net: (s) => s.net,
  pf: (s) => s.pf ?? -1,
  expectancy: (s) => repExp(s) ?? -1,
  dd: (s) => s.max_dd_pct ?? -1,
  commission: (s) => s.commission ?? -1,
  tickers: (s) => s.tickers,
};

function renderRepStrategies(): void {
  const pane = $("rep-pane-strategies");
  if (!pane) return;
  const d = repDetail;
  if (!d) {
    pane.innerHTML = `<div class="rep-empty">прогон не выбран</div>`;
    return;
  }
  const cols: Array<[string, string]> = [
    ["strategy", "Робот"], ["trades", "Сделки"], ["wins", "Win"], ["losses", "Loss"],
    ["wr", "WR%"], ["gw", "Gross W ₽"], ["gl", "Gross L ₽"], ["net", "Net ₽"],
    ["pf", "PF"], ["expectancy", "Exp"], ["dd", "DD%"], ["commission", "Комис ₽"],
    ["tickers", "Тикеры"],
  ];
  const acc = REP_SORT[repSortKey] ?? REP_SORT["trades"];
  const rows = [...d.strategies].sort((a, b) => {
    const x = acc(a);
    const y = acc(b);
    const c = typeof x === "string" || typeof y === "string"
      ? String(x).localeCompare(String(y))
      : (x as number) - (y as number);
    return c * repSortDir;
  });
  const head = cols.map(([k, label]) =>
    `<th class="${k === "strategy" ? "" : "n sortable"} ${repSortKey === k ? "sorted" : ""}" data-k="${k}">` +
    `${esc(label)}${repSortKey === k ? (repSortDir < 0 ? " ↓" : " ↑") : ""}</th>`).join("");
  const body = rows.map((s) => {
    const exp = repExp(s);
    return `<tr>` +
      `<td title="${esc(s.exits.join(", "))}">${esc(s.strategy)}</td>` +
      `<td class="n">${repN(s.trades)}</td>` +
      `<td class="n">${repN(s.wins)}</td>` +
      `<td class="n">${repN(repLosses(s))}</td>` +
      `<td class="n">${s.wr === null ? "—" : s.wr}</td>` +
      `<td class="n rep-pos">${repRub(s.gw)}</td>` +
      `<td class="n rep-neg">${repRub(s.gl)}</td>` +
      `<td class="n ${repSignCls(s.net)}">${repRub(s.net)}</td>` +
      `<td class="n">${s.pf === null ? "—" : s.pf}</td>` +
      `<td class="n ${repSignCls(exp)}">${exp === null ? "—" : repRub(exp)}</td>` +
      `<td class="n">${s.max_dd_pct == null ? "—" : s.max_dd_pct.toFixed(1)}</td>` +
      `<td class="n">${s.commission == null ? "—" : repRub(s.commission)}</td>` +
      `<td class="n">${repN(s.tickers)}</td>` +
      `</tr>`;
  }).join("");
  const t = d.summary;
  const texp = repExp(t);
  const total = `<tr class="rep-total">` +
    `<td>ИТОГО · ${rows.length}</td>` +
    `<td class="n">${repN(t.trades)}</td><td class="n">${repN(t.wins)}</td>` +
    `<td class="n">${repN(repLosses(t))}</td>` +
    `<td class="n">${t.wr === null ? "—" : t.wr}</td>` +
    `<td class="n rep-pos">${repRub(t.gw)}</td><td class="n rep-neg">${repRub(t.gl)}</td>` +
    `<td class="n ${repSignCls(t.net)}">${repRub(t.net)}</td>` +
    `<td class="n">${t.pf === null ? "—" : t.pf}</td>` +
    `<td class="n ${repSignCls(texp)}">${texp === null ? "—" : repRub(texp)}</td>` +
    `<td class="n">${t.max_dd_pct == null ? "—" : t.max_dd_pct.toFixed(1)}</td>` +
    `<td class="n">${t.commission == null ? "—" : repRub(t.commission)}</td>` +
    `<td class="n">${repN(t.tickers)}</td></tr>`;
  pane.innerHTML =
    `<div class="mini-hint">по умолчанию сортировка по числу сделок (методология: не по net); клик по заголовку — сменить</div>` +
    `<div class="rep-table-wrap"><table class="stats-table"><thead><tr>${head}</tr></thead>` +
    `<tbody>${body}${total}</tbody></table></div>`;
}

function renderRepDims(): void {
  const box = $("rep-dims");
  if (!box) return;
  const d = repDetail;
  if (!d || !d.dims.length) {
    box.innerHTML = `<span class="mini-hint">${
      repSource === "replay" ? "по этому прогону измерений нет" : "срезы не посчитаны (нужен импорт с trades_detail)"}</span>`;
    return;
  }
  box.innerHTML = d.dims
    .slice()
    .sort((a, b) => {
      const ia = REP_DIM_ORDER.indexOf(a);
      const ib = REP_DIM_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    })
    .map((dim) =>
      `<button class="rep-dim ${dim === repDim ? "active" : ""}" data-dim="${esc(dim)}">` +
      `${esc(d.dim_labels[dim] ?? dim)}</button>`)
    .join("");
}

async function loadRepSlices(): Promise<void> {
  const box = $("rep-slices");
  if (!box || repRunId === null) return;
  const key = `${repRunId}:${repDim}`;
  if (repSlicesCache[key]) {
    renderRepSlices(repSlicesCache[key]);
    return;
  }
  box.innerHTML = `<div class="rep-empty">загрузка среза…</div>`;
  try {
    const resp = await fetchJSON<{ dims: RepDim[] }>(
      repRunApi(`/slices?dim=${encodeURIComponent(repDim)}`));
    repSlicesCache[key] = resp.dims;
    if (repRunId === null || `${repRunId}:${repDim}` !== key) return;
    renderRepSlices(resp.dims);
  } catch (e) {
    box.innerHTML = `<div class="rep-empty">ошибка: ${esc(String(e))}</div>`;
  }
}

function renderRepSlices(dims: RepDim[]): void {
  const box = $("rep-slices");
  if (!box) return;
  const d = dims[0];
  if (!d) {
    box.innerHTML = `<div class="rep-empty">по этому измерению срезов нет</div>`;
    return;
  }
  const head = `<th>Робот</th>` +
    d.buckets.map((b) => `<th class="n" title="${esc(b)}">${esc(b)}</th>`).join("") +
    `<th class="n">Итого</th>`;
  const body = d.table.map((row) => {
    const cells = d.buckets.map((b, i) => {
      const c = row.cells[i];
      if (!c) return `<td class="n">—</td>`;
      const cls = row.best_bucket === b ? ` class="n rep-best"` : ` class="n"`;
      return `<td${cls} title="best: ${row.best_bucket === b ? "да" : "—"}">` +
        `<b>${repN(c.trades)}</b>` +
        `<div class="rep-dim-label">${c.wr === null ? "—" : c.wr.toFixed(1) + "%"}</div></td>`;
    }).join("");
    const t = row.total;
    return `<tr><td title="${esc(String(row.best_bucket ?? ""))}">${esc(row.strategy)}</td>${cells}` +
      `<td class="n rep-total">${repN(t.trades)}</td></tr>`;
  }).join("");
  box.innerHTML =
    `<div class="mini-hint">${esc(d.label)} · ячейка: сделок и WR%; подсветка — лучший по net бакет робота (при ≥3 сделках)</div>` +
    `<div class="rep-table-wrap"><table class="stats-table"><thead><tr>${head}</tr></thead>` +
    `<tbody>${body}</tbody></table></div>`;
}

function renderRepTradesFilters(): void {
  const box = $("rep-trades-filters");
  if (!box) return;
  const strategies = repDetail ? repDetail.strategies.map((s) => s.strategy) : [];
  const opt = (v: string, label: string, sel: string) =>
    `<option value="${esc(v)}" ${v === sel ? "selected" : ""}>${esc(label)}</option>`;
  box.innerHTML =
    `<label>Робот <select data-repk="strategy">${opt("", "все", repTF.strategy)}` +
    strategies.map((s) => opt(s, s, repTF.strategy)).join("") + `</select></label>` +
    `<label>Тикер <input data-repk="ticker" type="search" value="${esc(repTF.ticker)}" placeholder="SBER" /></label>` +
    `<label>Итог <select data-repk="outcome">${opt("", "все", repTF.outcome)}` +
    `${opt("win", "только прибыльные", repTF.outcome)}${opt("loss", "только убыточные", repTF.outcome)}</select></label>` +
    `<label>Сторона <select data-repk="side">${opt("", "все", repTF.side)}` +
    `${opt("long", "LONG", repTF.side)}${opt("short", "SHORT", repTF.side)}</select></label>` +
    `<button class="btn-secondary btn-xs" data-repk-reset="1">сброс</button>`;
}

function repCardFrom(key: string, v: number | null | undefined, digits = 0, moneyFmt = false): string {
  const val = v === null || v === undefined ? "—" : (moneyFmt ? repRub(v) : repN(v, digits));
  return repCard(key, val, v == null ? "" : repSignCls(v));
}

async function loadRepTrades(reset: boolean): Promise<void> {
  const box = $("rep-trades");
  if (!box || repRunId === null) return;
  if (reset) {
    repTradesOffset = 0;
    repTrades = [];
    box.innerHTML = `<div class="rep-empty">загрузка сделок…</div>`;
  }
  const q = new URLSearchParams();
  if (repTF.strategy) q.set("strategy", repTF.strategy);
  if (repTF.ticker.trim()) q.set("ticker", repTF.ticker.trim().toUpperCase());
  if (repTF.outcome) q.set("outcome", repTF.outcome);
  if (repTF.side) q.set("side", repTF.side);
  q.set("limit", String(REP_PAGE));
  q.set("offset", String(repTradesOffset));
  try {
    const resp = await fetchJSON<RepTradesResp>(repRunApi(`/trades?${q.toString()}`));
    if (repRunId === null) return;
    repTrades = reset ? resp.items : repTrades.concat(resp.items);
    repTradesTotal = resp.total;
    repTradesOffset = repTrades.length;
    repTradesSummary = resp.summary;
    renderRepTrades();
  } catch (e) {
    box.innerHTML = `<div class="rep-empty">ошибка: ${esc(String(e))}</div>`;
  }
}

function renderRepTrades(): void {
  const box = $("rep-trades");
  const cards = $("rep-trades-summary");
  const more = $("rep-trades-more");
  if (!box) return;
  const s = repTradesSummary;
  if (cards) {
    cards.innerHTML =
      repCardFrom("Сделок", s.trades) +
      repCardFrom("WR%", s.wr, 2) +
      repCardFrom("Gross W ₽", s.gw, 2, true) +
      repCardFrom("Gross L ₽", s.gl, 2, true) +
      repCardFrom("Net ₽", s.net, 2, true) +
      repCardFrom("PF", s.pf, 3) +
      repCardFrom("Exp ₽", s.expectancy, 2, true) +
      repCardFrom("SL min", s.sl_min, 2) +
      repCardFrom("SL max", s.sl_max, 2) +
      repCardFrom("TP min", s.tp_min, 2) +
      repCardFrom("TP max", s.tp_max, 2) +
      repCardFrom("MAE max (ATR)", s.mae_atr_max, 2) +
      repCardFrom("MFE max (ATR)", s.mfe_atr_max, 2) +
      repCardFrom("Баров в сделке (ср)", s.bars_avg, 1) +
      repCardFrom("Худшая ₽", s.worst, 2, true) +
      repCardFrom("Лучшая ₽", s.best, 2, true);
  }
  if (!repTrades.length) {
    box.innerHTML = `<div class="rep-empty">нет сделок по фильтру (у прогона нет trades_detail)</div>`;
    if (more) more.innerHTML = "";
    return;
  }
  const cols = [
    "Вход", "Выход", "Робот", "Тикер", "Side", "Вход ₽", "Выход ₽", "Net ₽",
    "SL", "TP", "MAE", "MFE", "Бары", "Сессия", "Режим", "ER", "Выход", "Причина",
  ];
  const head = cols.map((c, i) =>
    `<th class="${i >= 5 && i <= 12 ? "n" : ""}">${esc(c)}</th>`).join("");
  const body = repTrades.map((t) =>
    `<tr>` +
    `<td>${repTs(t.entry_time)}</td><td>${repTs(t.exit_time)}</td>` +
    `<td>${esc(t.strategy)}</td><td>${esc(t.ticker)}</td>` +
    `<td>${esc(t.side)}</td>` +
    `<td class="n">${t.entry_price == null ? "—" : repN(t.entry_price, 2)}</td>` +
    `<td class="n">${t.exit_price == null ? "—" : repN(t.exit_price, 2)}</td>` +
    `<td class="n ${repSignCls(t.net_pnl)}">${repRub(t.net_pnl)}</td>` +
    `<td class="n">${t.sl_price == null ? "—" : repN(t.sl_price, 2)}</td>` +
    `<td class="n">${t.tp_price == null ? "—" : repN(t.tp_price, 2)}</td>` +
    `<td class="n">${t.mae_atr == null ? "—" : t.mae_atr.toFixed(2)}</td>` +
    `<td class="n">${t.mfe_atr == null ? "—" : t.mfe_atr.toFixed(2)}</td>` +
    `<td class="n">${t.bars_held ?? "—"}</td>` +
    `<td>${esc(t.session ?? "—")}</td><td>${esc(t.regime_adx ?? "—")}</td>` +
    `<td class="n">${t.er_in == null ? "—" : t.er_in.toFixed(2)}</td>` +
    `<td>${esc(t.exit)}</td><td>${esc(t.exit_reason ?? "—")}</td>` +
    `</tr>`).join("");
  box.innerHTML = `<div class="rep-table-wrap"><table class="stats-table">` +
    `<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  if (more) {
    const rest = repTradesTotal - repTrades.length;
    more.innerHTML = `<span>показано ${repN(repTrades.length)} из ${repN(repTradesTotal)}</span>` +
      (rest > 0 ? `<button class="btn-secondary btn-xs" id="rep-trades-next">показать ещё ${repN(Math.min(REP_PAGE, rest))}</button>` : "");
  }
}

async function loadRepMarket(): Promise<void> {
  const box = $("rep-market");
  if (!box) return;
  box.innerHTML = `<div class="rep-empty">загрузка…</div>`;
  try {
    const resp = await fetchJSON<RepLeadersResp>(
      `/api/v1/analysis/market/leaders?window=${repWin}&limit=15`);
    repMarketLoaded = true;
    const tbl = (rows: RepLeader[], title: string) => {
      if (!rows.length) return `<div class="rep-empty">${esc(title)}: нет данных по свечам</div>`;
      const body = rows.map((r) =>
        `<tr><td>${esc(r.ticker)}</td>` +
        `<td class="n ${repSignCls(r.pct)}">${r.pct === null ? "—" : pct(r.pct)}</td>` +
        `<td class="n">${r.vol === null ? "—" : r.vol.toFixed(2) + "%"}</td>` +
        `<td class="n">${r.close == null ? "—" : repN(r.close, 2)}</td>` +
        `<td class="n">${repN(r.bars)}</td>` +
        `<td>${repTs(r.last_ts)}</td></tr>`).join("");
      return `<div class="mini-hint">${esc(title)}</div>` +
        `<div class="rep-table-wrap"><table class="stats-table"><thead><tr>` +
        `<th>Тикер</th><th class="n">Доход</th><th class="n">Vol</th>` +
        `<th class="n">Close</th><th class="n">Баров</th><th>По состоянию</th>` +
        `</tr></thead><tbody>${body}</tbody></table></div>`;
    };
    box.innerHTML =
      `<div class="mini-hint">окно ${esc(resp.window)} · тикеров ${repN(resp.count)} · ` +
      `интервал свечей ${repN(resp.interval)}с</div>` +
      tbl(resp.leaders, "🏆 Лидеры") + tbl(resp.outsiders, "⚠ Аутсайдеры");
  } catch (e) {
    box.innerHTML = `<div class="rep-empty">ошибка: ${esc(String(e))}</div>`;
  }
}

function renderRepPane(): void {
  (["strategies", "slices", "trades", "market"] as RepTab[]).forEach((t) => {
    const pane = $(`rep-pane-${t}`);
    if (pane) pane.classList.toggle("hidden", t !== repTab);
  });
  document.querySelectorAll<HTMLButtonElement>(".rep-subtab").forEach((b) => {
    b.classList.toggle("active", b.dataset.reptab === repTab);
  });
  if (repTab === "strategies") {
    renderRepStrategies();
  } else if (repTab === "slices") {
    renderRepDims();
    if (repDetail) void loadRepSlices();
  } else if (repTab === "trades") {
    renderRepTradesFilters();
    if (repDetail && !repTrades.length) void loadRepTrades(true);
    else renderRepTrades();
  } else if (repTab === "market") {
    if (!repMarketLoaded) void loadRepMarket();
  }
}

async function ensureReportsLoaded(): Promise<void> {
  if (repLoaded) return;
  repLoaded = true;
  await loadRepRuns();
}

function initAnalyticsTabs(): void {
  document.querySelectorAll<HTMLButtonElement>("#page-analytics .an-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.antab;
      document.querySelectorAll<HTMLButtonElement>("#page-analytics .an-tab").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      const live = $("an-pane-live");
      const rep = $("an-pane-reports");
      if (live) live.classList.toggle("hidden", tab !== "live");
      if (rep) rep.classList.toggle("hidden", tab !== "reports");
      if (tab === "reports") void ensureReportsLoaded();
    });
  });

  document.querySelectorAll<HTMLButtonElement>(".rep-subtab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.reptab as RepTab | undefined;
      if (!tab) return;
      repTab = tab;
      renderRepPane();
    });
  });

  const reloadRunsSoon = debounce(() => { void loadRepRuns(); });
  const reloadTradesSoon = debounce(() => { void loadRepTrades(true); });
  $("rep-refresh")?.addEventListener("click", () => { void loadRepRuns(); });
  document.getElementById("rep-kind")?.addEventListener("change", () => { void loadRepRuns(); });
  document.getElementById("rep-query")?.addEventListener("input", reloadRunsSoon);
  document.getElementById("rep-source")?.addEventListener("change", (ev) => {
    const v = (ev.target as HTMLSelectElement).value;
    repSource = v === "replay" ? "replay" : "harness";
    const kindSel = document.getElementById("rep-kind");
    if (kindSel) kindSel.classList.toggle("hidden", repSource === "replay");
    repRunId = null;
    repDetail = null;
    repSlicesCache = {};
    repTrades = [];
    repTradesTotal = 0;
    void loadRepRuns();
  });

  $("rep-actions")?.addEventListener("click", (ev) => {
    const btn = (ev.target as HTMLElement | null)?.closest<HTMLElement>("[data-repact]");
    const act = btn?.dataset.repact;
    if (!act || repRunId === null) return;
    const name = String(repRunId);
    if (act === "delete") {
      if (!confirm(`Удалить тест «${name}»? Сделки, окно и сайдкар будут стёрты безвозвратно.`)) return;
      if (btn) btn.setAttribute("disabled", "true");
      void (async () => {
        try {
          await fetchJSON(`/api/v1/bot/tests/${encodeURIComponent(name)}`, { method: "DELETE" });
          repRunId = null;
          repDetail = null;
          await loadRepRuns();
        } catch (e) {
          alert(`Ошибка удаления: ${String(e)}`);
          if (btn) btn.removeAttribute("disabled");
        }
      })();
      return;
    }
    if (act === "restart") {
      if (btn) btn.setAttribute("disabled", "true");
      void (async () => {
        try {
          const out = await fetchJSON<{ test_name: string }>(
            `/api/v1/bot/tests/${encodeURIComponent(name)}/restart`, { method: "POST" });
          repRunId = out.test_name;
          await loadRepRuns();
        } catch (e) {
          alert(`Ошибка перезапуска: ${String(e)}`);
          if (btn) btn.removeAttribute("disabled");
        }
      })();
    }
  });

  $("rep-list")?.addEventListener("click", (ev) => {
    const item = (ev.target as HTMLElement | null)?.closest<HTMLElement>(".rep-item");
    const id = item?.dataset.run;
    if (!id) return;
    void selectRepRun(repSource === "replay" ? id : Number(id));
  });

  $("rep-dims")?.addEventListener("click", (ev) => {
    const btn = (ev.target as HTMLElement | null)?.closest<HTMLElement>(".rep-dim");
    const dim = btn?.dataset.dim;
    if (!dim || dim === repDim) return;
    repDim = dim;
    renderRepDims();
    void loadRepSlices();
  });

  $("rep-pane-strategies")?.addEventListener("click", (ev) => {
    const th = (ev.target as HTMLElement | null)?.closest<HTMLElement>("th[data-k]");
    if (!th) return;
    const k = th.dataset.k;
    if (!k) return;
    if (repSortKey === k) repSortDir = repSortDir < 0 ? 1 : -1;
    else { repSortKey = k; repSortDir = k === "strategy" ? 1 : -1; }
    renderRepStrategies();
  });

  $("rep-trades-filters")?.addEventListener("change", (ev) => {
    const el = ev.target as HTMLElement | null;
    const key = (el as HTMLElement | null)?.getAttribute("data-repk");
    if (!key || !(el instanceof HTMLSelectElement || el instanceof HTMLInputElement)) return;
    if (key in repTF) (repTF as Record<string, string>)[key] = el.value;
    void loadRepTrades(true);
  });
  $("rep-trades-filters")?.addEventListener("input", (ev) => {
    const el = ev.target;
    if (!(el instanceof HTMLInputElement) || el.getAttribute("data-repk") !== "ticker") return;
    repTF.ticker = el.value;
    reloadTradesSoon();
  });
  $("rep-trades-filters")?.addEventListener("click", (ev) => {
    const btn = (ev.target as HTMLElement | null)?.closest<HTMLElement>("[data-repk-reset]");
    if (!btn) return;
    repTF = { strategy: "", ticker: "", outcome: "", side: "" };
    renderRepTradesFilters();
    void loadRepTrades(true);
  });

  $("rep-trades-more")?.addEventListener("click", (ev) => {
    if (!(ev.target instanceof HTMLElement) || ev.target.id !== "rep-trades-next") return;
    void loadRepTrades(false);
  });

  $("rep-market-filters")?.addEventListener("click", (ev) => {
    const btn = (ev.target as HTMLElement | null)?.closest<HTMLElement>("[data-repwin]");
    const win = btn?.dataset.repwin as RepWin | undefined;
    if (!win || win === repWin) return;
    repWin = win;
    document.querySelectorAll<HTMLElement>("#rep-market-filters [data-repwin]").forEach((b) => {
      b.classList.toggle("active", b.dataset.repwin === repWin);
    });
    void loadRepMarket();
  });
}


export function prepareReplayReport(name: string): void {
  repSource = "replay";
  repRunId = name;
  repDetail = null;
  repLoaded = false;
  repSlicesCache = {};
  repTrades = [];
  repTradesTotal = 0;
  const src = document.getElementById("rep-source") as HTMLSelectElement | null;
  if (src) src.value = "replay";
  document.getElementById("rep-kind")?.classList.add("hidden");
}

export { repN, repRub, repSignCls, repTs, renderRepTags, repCardFrom, ensureReportsLoaded, initAnalyticsTabs };
export type { RepConfigTags };
