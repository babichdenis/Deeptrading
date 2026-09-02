import {
  createEnsembleRun,
  deleteEnsembleRun,
  fetchCatalog,
  fetchEnsembleRuns,
  type StrategyCardDto,
} from "./api";
import { connectWs } from "./ws";

const $ = (id: string) => document.getElementById(id) as HTMLElement;

let catalog: StrategyCardDto[] = [];
let queueTimer: ReturnType<typeof setInterval> | null = null;

const EXIT_DEFAULTS: Record<string, Record<string, number>> = {
  atr_stop: { period: 14, multiplier: 2, risk_reward: 2 },
  fixed_sl_tp: { stop_pct: 1.0, target_pct: 2.0 },
  atr_trailing: { period: 14, initial_stop_atr: 2, activation_atr: 1, trail_distance_atr: 2 },
};

const ALL_SETUPS = [
  "rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
  "range_compression_breakout", "macd_cross", "donchian_breakout",
];

// короткие имена функций для автоназвания
const SHORT_NAMES: Record<string, string> = {
  rsi_reversal: "RSI", bollinger_reclaim: "BB", pullback_ema: "PB",
  vwap_reclaim: "VWAP", range_compression_breakout: "SQZ",
  macd_cross: "MACD", donchian_breakout: "DON",
};

const DEFAULT_STOCKS = [
  { figi: "BBG008F2T3T2", ticker: "RUAL" },
  { figi: "BBG004S683W7", ticker: "AFLT" },
  { figi: "BBG004S681M2", ticker: "SNGSP" },
  { figi: "BBG004S68CP5", ticker: "MVID" },
  { figi: "BBG004S681B4", ticker: "NLMK" },
];

let selectedStocks = new Set<string>(DEFAULT_STOCKS.map((s) => s.figi));

function renderStockChips() {
  const wrap = $("el-figis");
  if (!wrap) return;
  wrap.innerHTML = DEFAULT_STOCKS.map((s) => `
    <button type="button" class="chip ${selectedStocks.has(s.figi) ? "selected" : ""}" data-figi="${s.figi}">
      ${s.ticker}
    </button>`).join("");
  wrap.querySelectorAll<HTMLButtonElement>(".chip").forEach((b) =>
    b.addEventListener("click", () => {
      const figi = b.dataset.figi!;
      if (selectedStocks.has(figi)) selectedStocks.delete(figi);
      else selectedStocks.add(figi);
      renderStockChips();
    }));
}

function renderSetupBlocks() {
  const wrap = $("el-setups");
  if (!wrap) return;
  const cards = catalog.filter((c) => ALL_SETUPS.includes(c.id));
  wrap.innerHTML = cards.map((c, i) => {
    const paramsHtml = Object.entries(c.params_schema).map(([key, spec]) => `
      <label class="mini-hint">${key}
        <input type="number" data-skey="${key}" value="${spec.default}" min="${spec.min ?? ""}" max="${spec.max ?? ""}" step="any" style="width:56px" />
      </label>`).join("");
    return `<div class="el-block-wrap" data-sid="${c.id}">
      <label class="el-block" style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" class="el-setup" data-sid="${c.id}" ${i < 7 ? "checked" : ""} />
        <span>${c.name}</span>
        <button type="button" class="btn-secondary btn-sm" data-reset="${c.id}" title="Сбросить в default">⟲</button>
      </label>
      <div class="el-block-params">${paramsHtml}</div>
    </div>`;
  }).join("");
  wrap.querySelectorAll<HTMLButtonElement>("[data-reset]").forEach((b) =>
    b.addEventListener("click", () => {
      const card = catalog.find((c) => c.id === b.dataset.reset);
      if (!card) return;
      const sid = card.id;
      wrap.querySelectorAll<HTMLInputElement>(`.el-block-wrap[data-sid="${sid}"] input[data-skey]`).forEach((inp) => {
        const spec = (card.params_schema as Record<string, any>)[inp.dataset.skey!];
        if (spec) inp.value = String(spec.default);
      });
    }),
  );
}

function renderExitParams() {
  const sel = $("el-exit") as HTMLSelectElement;
  const wrap = $("el-exit-params");
  const defs = EXIT_DEFAULTS[sel.value] ?? {};
  wrap.innerHTML = Object.entries(defs)
    .map(([k, v]) => `<label class="mini-hint">${k} <input type="number" data-exit-key="${k}" value="${v}" step="any" style="width:56px"></label>`)
    .join(" ");
}

function parseFigis(): string[] {
  return [...selectedStocks];
}

function initDates() {
  const fromEl = $("el-date-from") as HTMLInputElement;
  const toEl = $("el-date-to") as HTMLInputElement;
  if (!fromEl || !toEl) return;
  const saved = localStorage.getItem("enslab-dates");
  if (saved) {
    try {
      const d = JSON.parse(saved);
      fromEl.value = d.from ?? "";
      toEl.value = d.to ?? "";
      return;
    } catch { /* ignore */ }
  }
  // текущий месяц по умолчанию
  const now = new Date();
  const y = now.getFullYear();
  const m = now.getMonth();
  const first = new Date(y, m, 1);
  const last = new Date(y, m + 1, 0);
  const iso = (dt: Date) => `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
  fromEl.value = iso(first);
  toEl.value = iso(last);
  // сохраняем
  const save = () => localStorage.setItem("enslab-dates", JSON.stringify({ from: fromEl.value, to: toEl.value }));
  fromEl.addEventListener("change", save);
  toEl.addEventListener("change", save);
}

function makeAutoName(): string {
  const setups = [...document.querySelectorAll<HTMLInputElement>(".el-setup:checked")]
    .map((b) => SHORT_NAMES[b.dataset.sid!] ?? b.dataset.sid)
    .join("+") || "no-setup";
  const tf = ($("el-entry-tf") as HTMLSelectElement).value.replace("min", "m");
  const bias = ($("el-bias-mode") as HTMLSelectElement).value;
  const cd = ($("el-cooldown") as HTMLInputElement).value;
  const n = selectedStocks.size;
  return `[${bias}|${setups}|${tf}|cd${cd}|x${n}]`;
}

function renderQuorumOptions() {
  const sel = $("cfg-quorum") as HTMLSelectElement;
  if (!sel) return;
  const n = document.querySelectorAll<HTMLInputElement>(".el-setup:checked").length || 1;
  const cur = Number(sel.value) || 2;
  const def = Math.min(Math.max(cur, 1), n);
  sel.innerHTML = Array.from({ length: n }, (_, i) =>
    `<option value="${i + 1}"${i + 1 === def ? " selected" : ""}>${i + 1}-of-${n}</option>`,
  ).join("");
}

function collectBody(): Record<string, unknown> {
  const setups = [...document.querySelectorAll<HTMLInputElement>(".el-setup:checked")]
    .map((b) => {
      const params: Record<string, number> = {};
      document.querySelectorAll<HTMLInputElement>(`.el-block-wrap[data-sid="${b.dataset.sid}"] input[data-skey]`)
        .forEach((inp) => { params[inp.dataset.skey!] = Number(inp.value); });
      return { strategy_id: b.dataset.sid, tf: "5min", params };
    });
  const exitId = ($("el-exit") as HTMLSelectElement).value;
  const exitParams: Record<string, number> = {};
  document.querySelectorAll<HTMLInputElement>("#el-exit-params input[data-exit-key]")
    .forEach((inp) => { exitParams[inp.dataset.exitKey!] = Number(inp.value); });
  const fromEl = $("el-date-from") as HTMLInputElement;
  const toEl = $("el-date-to") as HTMLInputElement;
  const body: Record<string, unknown> = {
    figis: parseFigis(),
    days: Number(($("el-days") as HTMLInputElement).value) || 30,
    capital: Number(($("el-capital") as HTMLInputElement).value) || 100000,
    bias_mode: ($("el-bias-mode") as HTMLSelectElement).value,
    bias: { tf: "hour", period: Number(($("el-bias-period") as HTMLInputElement).value) || 50 },
    entry_tf: ($("el-entry-tf") as HTMLSelectElement).value,
    entry_session: ($("el-session") as HTMLSelectElement).value,
    quorum: Number(($("cfg-quorum") as HTMLSelectElement).value) || 2,
    same_side_reentry_cooldown_bars: Number(($("el-cooldown") as HTMLInputElement).value) || 0,
    lot: 10,
    use_all_setups: setups.length === ALL_SETUPS.length,
    drop_useless: true,
    setups,
    entry: { tf: ($("el-entry-tf") as HTMLSelectElement).value, lookback: Number(($("el-entry-lookback") as HTMLInputElement).value) || 1 },
    exit_policy: { id: exitId, params: exitParams },
  };
  if (fromEl?.value) body.from_ts = `${fromEl.value}T00:00:00Z`;
  if (toEl?.value) body.to_ts = `${toEl.value}T23:59:00Z`;
  return body;
}

function fmtTs(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function refreshQueue() {
  const wrap = $("el-queue");
  if (!wrap) return;
  try {
    const runs = await fetchEnsembleRuns(20);
    if (runs.length === 0) {
      wrap.innerHTML = `<span class="mini-hint">пусто</span>`;
      return;
    }
    wrap.innerHTML = runs.map((r) => {
      const p = r.progress ?? { done: 0, total: 0, current: "", by_stock: {} };
      const by = p.by_stock ?? {};
      const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
      const stocks = Object.entries(by).map(([figi, sp]) => {
        const st = sp.done >= sp.total ? "🟢" : "🟡";
        return `<span title="${figi}">${st}${figi.slice(0, 4)}</span>`;
      }).join(" ");
      const res = r.result
        ? `<div class="mini-hint">net <b class="${r.result.total_net >= 0 ? "pos" : "neg"}">${r.result.total_net.toFixed(0)} ₽</b> · ${r.result.positive_stocks}/${r.result.stocks} в плюсе</div>`
        : "";
      const resRows = r.result
        ? `<div class="mini-hint">${r.result.by_stock.map((s) =>
            `${s.ticker}: ${s.net != null ? s.net.toFixed(0) : s.error ?? "?"} ₽ (PF ${s.pf ?? "?"})`).join(" · ")}</div>`
        : "";
      return `<div class="el-run ${r.status === "DONE" ? "" : ""}">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <b>${((r.params.figis as string[] | undefined) ?? []).length} акций</b>
          <span class="badge">${r.status}</span>
          <span class="mini-hint">${fmtTs(r.created_at)}</span>
          <button class="btn-secondary btn-sm" data-del="${r.run_id}">✕</button>
        </div>
        <div class="progress-track" style="height:6px"><div class="progress-fill" style="width:${Math.max(3, pct)}%"></div></div>
        <div class="mini-hint">${p.done}/${p.total} · ${p.current ?? ""} ${stocks}</div>
        ${res}${resRows}
        ${r.error ? `<div class="mini-hint neg">${r.error}</div>` : ""}
      </div>`;
    }).join("");
    wrap.querySelectorAll<HTMLButtonElement>("[data-del]").forEach((b) =>
      b.addEventListener("click", () => { void deleteEnsembleRun(b.dataset.del!); void refreshQueue(); }));
  } catch (e) {
    wrap.innerHTML = `<span class="mini-hint">${e instanceof Error ? e.message : String(e)}</span>`;
  }
}

async function runToLab() {
  const btn = $("btn-enslab-run");
  const note = $("el-note");
  btn.classList.add("busy");
  try {
    const figis = parseFigis();
    if (figis.length === 0) throw new Error("укажите хотя бы одну акцию");
    const body = collectBody();
    const res = await createEnsembleRun(body);
    note.textContent = `Конфигурация ${res.run_id.slice(0, 8)} поставлена в очередь (${figis.length} акций).`;
    await refreshQueue();
  } catch (e) {
    note.textContent = e instanceof Error ? e.message : String(e);
  } finally {
    btn.classList.remove("busy");
  }
}

export async function initEnsLab() {
  try {
    const cat = await fetchCatalog();
    catalog = cat.strategies;
  } catch { /* каталог подтянется позже */ }
  renderSetupBlocks();
  renderExitParams();
  renderStockChips();
  initDates();
  renderQuorumOptions();
  document.querySelectorAll<HTMLInputElement>(".el-setup").forEach((cb) =>
    cb.addEventListener("change", () => { renderQuorumOptions(); applyAutoName(); }));
  const nameInput = $("cfg-name") as HTMLInputElement;
  let nameTouched = false;
  nameInput.addEventListener("input", () => { nameTouched = true; });
  const applyAutoName = () => {
    if (nameTouched) return;
    nameInput.value = makeAutoName();
  };
  $("btn-cfg-autoname")?.addEventListener("click", () => {
    nameTouched = false;
    nameInput.value = makeAutoName();
  });
  document.querySelectorAll<HTMLElement>(".el-setup, #el-entry-tf, #el-bias-mode, #el-cooldown")
    .forEach((el) => el.addEventListener("change", applyAutoName));
  ($("el-exit") as HTMLSelectElement).addEventListener("change", renderExitParams);
  $("btn-enslab-run")?.addEventListener("click", () => void runToLab());
  connectWs();
  await refreshQueue();
  if (queueTimer) clearInterval(queueTimer);
  queueTimer = setInterval(() => void refreshQueue(), 4000);
}
