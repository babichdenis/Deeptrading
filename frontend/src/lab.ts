import { createLabChart } from "./labchart";
import { connectWs, subscribe as wsSubscribe } from "./ws";
import {
  createConfiguration,
  deleteConfiguration,
  deleteRun,
  enqueueTest,
  fetchCatalog,
  fetchConfigurations,
  fetchLabQueue,
  fetchLabSettings,
  getConfiguration,
  pauseRun,
  recycleRun,
  resumeRun,
  saveLabSettings,
  stopRun,
  updateConfiguration,
  fetchDecisions,
  type ConfigurationDto,
  type StrategyCardDto,
  type TestRunDto,
} from "./api";

declare global {
  interface Window {
    FIGI: string;
    LAB_INTERVAL: string;
    __tradeFocus?: Record<string, unknown> | null;
    __chartOverlay?: ((trade: Record<string, unknown>) => void) | null;
    __selectedTestTickers?: Set<string>;
  }
}

let catalog: StrategyCardDto[] = [];
let configs: ConfigurationDto[] = [];
let queue: TestRunDto[] = [];
let queueTimer: ReturnType<typeof setInterval> | null = null;
let liveViewRunId: string | null = null;
let editingId: string | null = null;
let selectedTickerForDetails: string | null = null;

const $ = (id: string) => document.getElementById(id) as HTMLElement;


const money = (n: number | null | undefined): string =>
  n == null ? "—" : new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(n);


const LIQUID_TICKERS = ["SBER", "GAZP", "LKOH", "GMKN", "ROSN", "MTSS", "TATN", "MGNT", "CHMF", "ALRS", "PLZL", "SNGS"];

const TAGS: Record<string, string> = {
  rsi_reversal: "RSI", bollinger_reclaim: "BB", pullback_ema: "PB",
  vwap_reclaim: "VWAP", range_compression_breakout: "SQZ",
};


const EXIT_SCHEMAS: Record<string, Record<string, { default: number }>> = {
  fixed_sl_tp: { stop_pct: { default: 1 }, target_pct: { default: 2 } },
  atr_stop: { period: { default: 14 }, multiplier: { default: 2 }, risk_reward: { default: 2 } },
  atr_trailing: { period: { default: 14 }, initial_stop_atr: { default: 2 }, activation_atr: { default: 1 }, trail_distance_atr: { default: 2 } },
};

function configDump(cfg: any): string {
  try {
    const members = (cfg.members ?? []) as Array<{strategy_id: string; params: Record<string, number>}>;
    const lines = members.map((m) => {
      const ps = Object.entries(m.params ?? {})
        .map(([k, v]) => `${k}=${typeof v === "number" ? Math.round(v * 10000) / 100 : v}`)
        .join(", ");
      return `<span class="cfg-fn">${m.strategy_id}${ps ? `<span class="dim"> (${ps})</span>` : ""}</span>`;
    });
    const exit = cfg.exit_policy?.id ?? "—";
    const exitParams = JSON.stringify(cfg.exit_policy?.params ?? {});
    const sess = cfg.session_policy ?? {};
    const rows: Array<[string, string]> = [
      ["ТФ", cfg.interval_name ?? "—"],
      ["Функции", lines.join(", ")],
      ["Кворум", `K=${cfg.quorum ?? "—"} (same-bar)`],
      ["Выход", `${exit} ${exitParams}`],
      ["Min hold", `${cfg.min_hold_bars ?? 0} бар`],
      ["Short", cfg.allow_short ? "да" : "нет"],
      ["Сессия", sess.overnight === false ? "закрытие в конце дня" : "перенос через ночь"],
    ];
    return `<div class="cfg-dump-table">${rows.map(([k, v]) =>
      `<div class="cfg-dump-row"><span class="cfg-dump-key">${k}</span><span class="cfg-dump-val">${v}</span></div>`
    ).join("")}</div>`;
  } catch {
    return cfg?.name ?? "";
  }
}

function verdictBadge(v: string): string {
  return `<span class="verdict-badge v-${v.toLowerCase()}">${v}</span>`;
}

export async function initLab() {
  catalog = (await fetchCatalog()).strategies.filter((c) => c.status === "AVAILABLE");
  $("btn-lab-settings")?.addEventListener("click", openSettingsDialog);
  $("btn-cfg-new").addEventListener("click", () => openEditor());
  $("btn-cfg-cancel").addEventListener("click", closeEditor);
  $("btn-cfg-save").addEventListener("click", () => void saveConfiguration(false));
  $("btn-cfg-save-run")?.addEventListener("click", () => void saveConfiguration(true));
  ($("cfg-exit") as HTMLSelectElement).addEventListener("change", renderExitParams);
  await refreshConfigs();
}

export async function refreshConfigs() {
  try {
    configs = await fetchConfigurations();
    queue = (await fetchLabQueue()).runs;
  } catch {
    return;
  }
  renderColumns();
  scheduleQueuePoll();
  void connectWs();
  for (const r of queue) {
    if (["QUEUED", "RUNNING", "PAUSED"].includes(r.status)) {
      wsSubscribe(`job:${r.run_id}`, () => scheduleWsRefresh());
    }
  }
  if (liveViewRunId) {
    const run = queue.find((r) => r.run_id === liveViewRunId);
    const panel = $("lab-results-v2");
    if (run && panel && ["RUNNING", "PAUSED"].includes(run.status)) {
      renderLiveRun(panel, run);
    } else if (run) {
      liveViewRunId = null;
      void showResults(run.config_id);
    } else {
      liveViewRunId = null;
    }
  }
}

let wsRefreshTimer: ReturnType<typeof setTimeout> | null = null;
let lastPollMs = 0;

function scheduleWsRefresh() {
  if (wsRefreshTimer) return;
  wsRefreshTimer = setTimeout(() => { wsRefreshTimer = null; void refreshConfigs(); }, 600);
}

function scheduleQueuePoll() {
  const hasActive = queue.some((r) => ["QUEUED", "RUNNING", "PAUSED"].includes(r.status));
  const wantMs = hasActive ? 1000 : 15000;
  if (queueTimer && lastPollMs === wantMs) return;
  if (queueTimer) clearInterval(queueTimer);
  lastPollMs = wantMs;
  queueTimer = setInterval(() => void refreshConfigs(), wantMs);
}

const cardSigs = new Map<HTMLElement, string>();

function splitFillStyle(left: number, right: number): string {
  const total = left + right;
  if (total <= 0) return "";
  const green = "rgba(34, 197, 94, 0.26)";
  const red = "rgba(239, 68, 68, 0.22)";
  if (right <= 0) return `linear-gradient(to right, ${green} 0%, ${green} 100%)`;
  if (left <= 0) return `linear-gradient(to right, ${red} 0%, ${red} 100%)`;
  const split = Math.round((100 * left) / total);
  return `linear-gradient(to right, ${green} ${Math.max(0, split - 7)}%, ${red} ${Math.min(100, split + 7)}%)`;
}

function renderColumns() {
  renderLeft();
  renderMiddle();
  renderRight();
}

function renderLeft() {
  const wrap = $("cfg-list");
  const configIdsWithAnyRun = new Set(queue.map((r) => r.config_id));
  const waiting = configs.filter((c) => !configIdsWithAnyRun.has(c.configuration_id));
  if (!waiting.length) {
    wrap.innerHTML = `<span class="mini-hint">все конфигурации в очереди или протестированы — соберите новую (＋)</span>`;
    return;
  }
  wrap.innerHTML = "";
  for (const c of waiting) {
    const card = document.createElement("div");
    card.className = "cfg-card";
    const chips = c.members.map((m) => `${m.strategy_id}(${JSON.stringify(m.params ?? {})})`).join(", ");
    const lastNet = c.lab_result?.totals?.total_net ?? c.lab_result?.summary?.summary?.net;
    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span class="cfg-name">${c.name}</span>
        <span class="mini-hint">${c.interval_name}</span>
      </div>
      <div class="mini-hint" style="margin-top:4px">${c.members.length} стратегий: ${chips}</div>
      <div class="cfg-dump">${configDump(c)}</div>
      ${lastNet != null ? `<div class="mini-hint">последний результат: <b class="${lastNet >= 0 ? "pos" : "neg"}">${money(lastNet)} ₽</b></div>` : ""}
      <div class="btn-row" style="margin-top:8px">
        <button class="btn-primary btn-sm" data-act="tolab">🧪 Тестировать</button>
        <button class="btn-secondary" data-act="edit" title="Редактировать">✏️</button>
        <button class="btn-secondary" data-act="delete" title="Удалить">🗑</button>
        ${c.lab_result?.totals ? `<button class="btn-secondary" data-act="results" title="Результаты">📊</button>` : ""}
      </div>`;
    card.addEventListener("click", () => {
      if (c.lab_result?.totals) void showResults(c.configuration_id);
    });
    card.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((b) =>
      b.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const act = b.dataset.act!;
        if (act === "tolab") {
          const fromV = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10);
          const toV = new Date().toISOString().slice(0, 10);
          await enqueueTest(c.configuration_id, [], fromV, toV, 30);
          await refreshConfigs();
        } else if (act === "results") await showResults(c.configuration_id);
        else if (act === "edit") await editConfiguration(c);
        else if (act === "delete") {
          if (confirm(`Удалить конфигурацию «${c.name}»?`)) {
            try { await deleteConfiguration(c.configuration_id); } finally { await refreshConfigs(); }
          }
        }
      }),
    );
    wrap.appendChild(card);
  }
}

function bindRunCardButtons(card: HTMLElement, r: TestRunDto) {
  card.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((b) =>
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const act = b.dataset.act!;
      if (act === "pause") await pauseRun(r.run_id);
      else if (act === "resume") await resumeRun(r.run_id);
      else if (act === "stop") await stopRun(r.run_id);
      await refreshConfigs();
    }),
  );
}

function renderMiddle() {
  const wrap = $("lab-running");
  const active = queue.filter((r) => ["QUEUED", "RUNNING", "PAUSED"].includes(r.status));
  if (!active.length) {
    wrap.innerHTML = `<span class="mini-hint">очередь пуста</span>`;
    return;
  }

  const existing = wrap.querySelectorAll<HTMLElement>(".cfg-card[data-run-id]");
  const activeIds = new Set(active.map((r) => r.run_id));
  existing.forEach((el) => {
    if (!activeIds.has(el.dataset.runId!)) el.remove();
  });

  for (const r of active) {
    const p = r.progress ?? { done: 0, total: r.tickers.length * 2, current: "" };
    const pct = p.total > 0 ? Math.min(100, Math.round((p.done / p.total) * 100)) : 0;
    const inQueue = r.status === "QUEUED" ? "в очереди" : r.status === "PAUSED" ? "на паузе" : `идёт: ${p.current ?? ""}`;

    const spAll = ((p as any).stock_progress ?? {}) as Record<string, { done: number; total: number }>;
    const segSig = r.tickers.map((t) => { const sp = spAll[t]; return sp ? Math.round((sp.done / (sp.total || 1)) * 20) : 0; }).join(",");
    const sig = `${r.status}|${pct}|${inQueue}|${segSig}`;
    const spAllE = ((p as any).stock_progress ?? {}) as Record<string, { done: number; total: number }>;
    const segs = r.tickers.map((t) => { const sp = spAllE[t]; const f = sp ? Math.min(1, sp.done / (sp.total || 1)) : 0; return `<span class="lab-seg is-${f >= 1 ? "done" : f > 0 ? "current" : "wait"}" data-ticker="${t}" title="${t}"><span class="lab-seg-fill" style="width:${f * 100}%"></span></span>`; }).join("");
    let card = wrap.querySelector<HTMLElement>(`.cfg-card[data-run-id="${r.run_id}"]`);
    if (card) {
      if (cardSigs.get(card) === sig) continue;
      cardSigs.set(card, sig);
      card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span class="cfg-name">${r.config_name}</span>
        <span class="status-badge st-in_lab js-status">${inQueue}</span>
      </div>
      <div class="mini-hint" style="margin-top:4px">${r.interval_name} · ${r.tickers.length} акций</div>
      <div class="mini-hint">период: ${(r.date_from ?? "").slice(0, 10) || "?"} → ${(r.date_to ?? "").slice(0, 10) || "сейчас"}</div>
      ${segs ? `<div class="lab-midbar">${segs}</div>` : ""}
      <div class="progress-track"><div class="progress-fill js-fill" style="width:${Math.max(3, pct)}%"></div></div>
      <div class="mini-hint js-current" style="margin-top:4px">${inQueue}</div>
      <div class="btn-row" style="margin-top:8px">
        ${r.status === "QUEUED" || r.status === "RUNNING" ? `<button class="btn-secondary" data-act="pause">⏸</button>` : `<button class="btn-secondary" data-act="resume">▶</button>`}
        <button class="btn-secondary" data-act="stop" title="Убрать из очереди влево">■</button>
      </div>`;
      bindRunCardButtons(card!, r);
      continue;
    }

    card = document.createElement("div");
    card.className = "cfg-card";
    card.dataset.runId = r.run_id;
    card.innerHTML = `<input type="hidden" data-sig="${sig}">${segs ? `<div class="lab-midbar">${segs}</div>` : ""}` + `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span class="cfg-name">${r.config_name}</span>
        <span class="status-badge st-in_lab">${inQueue}</span>
      </div>
      <div class="mini-hint" style="margin-top:4px">${r.interval_name} · ${r.tickers.length} акций</div>
      <div class="mini-hint">период: ${(r.date_from ?? "").slice(0, 10) || "?"} → ${(r.date_to ?? "").slice(0, 10) || "сейчас"}</div>
      <div class="progress-track"><div class="progress-fill js-fill" style="width:${Math.max(3, pct)}%"></div></div>
      <div class="mini-hint js-current" style="margin-top:4px">${inQueue}</div>
      <div class="btn-row" style="margin-top:8px">
        ${r.status === "QUEUED" || r.status === "RUNNING" ? `<button class="btn-secondary" data-act="pause">⏸</button>` : ""}
        ${r.status === "PAUSED" ? `<button class="btn-secondary" data-act="resume">▶</button>` : ""}
        <button class="btn-secondary" data-act="stop" title="Снять с очереди">■</button>
      </div>`;
    bindRunCardButtons(card, r);
    wrap.appendChild(card);
  }
}

function bindTestedCard(card: HTMLElement, r: TestRunDto) {
  card.addEventListener("click", () => void showResults(r.config_id));
  card.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((b) =>
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const act = b.dataset.act!;
      if (act === "results") await showResults(r.config_id);
      else if (act === "recycle") { await recycleRun(r.run_id); await refreshConfigs(); }
      else if (act === "delete") { if (confirm("Удалить этот тест?")) { await deleteRun(r.run_id); await refreshConfigs(); } }
    }),
  );
}

function renderRight() {
  const wrap = $("cfg-tested");
  const tests = queue.filter((r) => ["DONE", "FAILED", "CANCELLED"].includes(r.status));
  if (!tests.length) {
    wrap.innerHTML = `<span class="mini-hint">ещё нет завершённых прогонов — поставьте конфигурацию на тест</span>`;
    return;
  }
  wrap.innerHTML = "";
  for (const r of tests) {
    const t = r.lab_result?.totals ?? {};
    const net = t.total_net ?? 0;
    const p = r.progress ?? { done: 0, total: r.tickers.length * 2, current: "" };
    const pct = p.total > 0 ? Math.min(100, Math.round((p.done / p.total) * 100)) : 0;
    const isActive = r.status === "RUNNING" || r.status === "PAUSED";
    const cardSig = `${r.run_id}|${r.status}|${net}|${t.trades ?? 0}`;
    let card = wrap.querySelector<HTMLElement>(`.cfg-card[data-cfg-id="${r.config_id}"]`);
    if (card) {
      if (cardSigs.get(card) === cardSig) continue;
      cardSigs.set(card, cardSig);
      const fresh = document.createElement("div");
      fresh.className = "cfg-card";
      fresh.dataset.cfgId = r.config_id;
      fresh.innerHTML = card!.innerHTML;
      card!.replaceWith(fresh);
      bindTestedCard(fresh, r);
      continue;
    }
    card = document.createElement("div");
    cardSigs.set(card, cardSig);
    card.className = "cfg-card";
    card.dataset.cfgId = r.config_id;
    card.innerHTML = `<input type="hidden" data-sig="${cardSig}">` + `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span class="cfg-name">${r.config_name}</span>
        ${isActive
          ? `<span class="status-badge st-in_lab">${r.status === "PAUSED" ? "пауза" : "идёт"}</span>`
          : `<span class="js-pnl ${net >= 0 ? "pos" : "neg"}" style="font-weight:700">${money(net)} ₽</span>`}
      </div>
      <div class="cfg-dump">${configDump({ name: r.config_name, interval_name: r.interval_name, quorum: r.quorum, members: r.members ?? [], exit_policy: r.exit_policy ?? {}, min_hold_bars: r.min_hold_bars ?? 0, allow_short: r.allow_short ?? false })}</div>
      <div class="mini-hint" style="margin-top:4px">акций: ${r.tickers.length} · ${r.tickers.slice(0, 6).join(", ")}${r.tickers.length > 6 ? "…" : ""}</div>
      <div class="mini-hint">период: ${(r.date_from ?? "").slice(0, 10) || "?"} → ${(r.date_to ?? "").slice(0, 10) || "сейчас"}</div>
      ${!isActive ? `<div class="mini-hint">сделок: ${t.trades ?? 0} · акций: ${t.stocks ?? 0} · прибыльных: ${t.positive_stocks ?? 0}</div>` : ""}
      ${isActive ? `<div class="progress-track" style="margin-top:6px"><div class="progress-fill" style="width:${Math.max(3, pct)}%"></div></div>` : ""}
      <div class="btn-row" style="margin-top:8px">
        ${isActive ? `<button class="btn-secondary" data-act="pause" title="Пауза">⏸</button>` : ""}
        ${r.status === "PAUSED" ? `<button class="btn-secondary" data-act="resume" title="Продолжить">▶</button>` : ""}
        ${isActive ? `<button class="btn-secondary" data-act="stop" title="Снять с очереди">■</button>` : ""}
        <button class="btn-secondary" data-act="results" title="Результаты">📊</button>
        <button class="btn-secondary" data-act="recycle" title="Вернуть в левую колонку">⬅️</button>
        <button class="btn-secondary" data-act="delete" title="Удалить">🗑</button>
      </div>`;
    bindTestedCard(card, r);
    wrap.appendChild(card);
  }
}

// ---------- редактор конфигурации ----------

function closeEditor() {
  $("cfg-editor").classList.add("hidden");
  document.getElementById("modal-backdrop")?.classList.add("hidden");
  editingId = null;
}

function openEditor() {
  $("cfg-editor").classList.remove("hidden");
  document.getElementById("modal-backdrop")?.classList.remove("hidden");
  const wrap = $("cfg-strategies");
  wrap.innerHTML = "";
  for (const c of catalog) {
    const el = document.createElement("div");
    el.className = "scard";
    el.innerHTML = `
      <label class="scard-head" style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" data-sid="${c.id}" />
        <span class="scard-name">${c.name}</span>
        <span class="badge fam-${c.family}">${c.family}</span>
        <span class="wave-badge">W${c.wave}</span>
      </label>
      <div class="scard-rules">▲ ${c.long_rule}<br />▼ ${c.short_rule}</div>
      <div class="params-line hidden" id="cfgp-${c.id}">
        ${Object.entries(c.params_schema)
          .map(([k, s2]) => `<label>${k}<input type="number" data-key="${k}" value="${s2.default}" step="any" /></label>`)
          .join("")}
      </div>`
      .replace('<div class="params-line" id="cfgp-${c.id}">', '<div class="params-line" id="cfgp-' + c.id + '">');
    el.querySelector<HTMLInputElement>("input[type=checkbox]")!.addEventListener("change", (ev) => {
      $(`cfgp-${c.id}`).classList.toggle("hidden", !(ev.target as HTMLInputElement).checked);
      updateQuorumOptions();
      autoName();
    });
    wrap.appendChild(el);
  }
  const nameInp = $("cfg-name") as HTMLInputElement;
  nameInp.value = "";
  nameInp.dataset.auto = "1";
  nameInp.placeholder = "Название (авто, можно изменить)";
  updateQuorumOptions();
  renderExitParams();
  autoName();
  initTestSection();
}

function initTestSection() {
  const chipsWrap = $("cfg-test-chips");
  if (!chipsWrap) return;
  if (!(window.__selectedTestTickers ?? null)) {
    window.__selectedTestTickers = new Set<string>(LIQUID_TICKERS.slice(0, 8));
  }
  const chipsHtml = LIQUID_TICKERS.map((t) =>
    `<button class="chip ${window.__selectedTestTickers!.has(t) ? "selected" : ""}" data-t="${t}">${t}</button>`).join("");
  chipsWrap.innerHTML = chipsHtml;
  chipsWrap.querySelectorAll<HTMLButtonElement>(".chip").forEach((b) =>
    b.addEventListener("click", () => {
      const t = b.dataset.t!;
      if (window.__selectedTestTickers!.has(t)) window.__selectedTestTickers!.delete(t);
      else window.__selectedTestTickers!.add(t);
      b.classList.toggle("selected", window.__selectedTestTickers!.has(t));
    }),
  );
  const from = $("cfg-test-from") as HTMLInputElement;
  const to = $("cfg-test-to") as HTMLInputElement;
  const today = new Date();
  const monthAgo = new Date(Date.now() - 30 * 864e5);
  to.value = today.toISOString().slice(0, 10);
  from.value = monthAgo.toISOString().slice(0, 10);
}

function autoName() {
  const checked = [...document.querySelectorAll<HTMLInputElement>("#cfg-strategies input:checked")]
    .map((b) => catalog.find((c) => c.id === b.dataset.sid)?.id ?? "");
  if (!checked.length) return;
  const q = Number(($("cfg-quorum") as HTMLSelectElement).value) || Math.min(2, checked.length);
  const tf = ($("cfg-interval") as HTMLSelectElement)?.value || window.LAB_INTERVAL || "hour";
  const nameInput = $("cfg-name") as HTMLInputElement;
  if (nameInput.dataset.auto === "1") {
    nameInput.value = `${checked.map((c) => TAGS[c] ?? c).join("+")} ${q}of${checked.length} · ${tf}`;
  }
}

function updateQuorumOptions() {
  const n = document.querySelectorAll<HTMLInputElement>("#cfg-strategies input:checked").length || 1;
  const sel = $("cfg-quorum") as HTMLSelectElement;
  const def = Math.min(2, n);
  sel.innerHTML = Array.from({ length: n }, (_, i) =>
    `<option value="${i + 1}"${i + 1 === def ? " selected" : ""}>${i + 1}-of-${n}</option>`,
  ).join("");
  autoName();
}

function renderExitParams() {
  const id = ($("cfg-exit") as HTMLSelectElement).value;
  const schema = EXIT_SCHEMAS[id] ?? {};
  $("cfg-exit-params").innerHTML = Object.entries(schema)
    .map(([k, v]) => `<label>${k}<input type="number" data-key="${k}" value="${v.default}" step="any" /></label>`)
    .join("");
}

async function saveConfiguration(runAfter = false) {
  const members: Array<{ strategy_id: string; params: Record<string, number> }> = [];
  document.querySelectorAll<HTMLInputElement>("#cfg-strategies input:checked").forEach((box) => {
    const sid = box.dataset.sid!;
    const params: Record<string, number> = {};
    document.querySelectorAll<HTMLInputElement>(`#cfgp-${sid} input`).forEach((inp) => {
      params[inp.dataset.key!] = Number(inp.value);
    });
    members.push({ strategy_id: sid, params });
  });
  if (!members.length) {
    alert("Выберите минимум одну стратегию");
    return;
  }
  const exitParams: Record<string, number> = {};
  document.querySelectorAll<HTMLInputElement>("#cfg-exit-params input").forEach((inp) => {
    exitParams[inp.dataset.key!] = Number(inp.value);
  });

  const body = {
    name: ($("cfg-name") as HTMLInputElement).value,
    interval_name: ($("cfg-interval") as HTMLSelectElement).value,
    members,
    quorum: Number(($("cfg-quorum") as HTMLSelectElement).value),
    exit_policy: { id: ($("cfg-exit") as HTMLSelectElement).value, params: exitParams },
    min_hold_bars: Number(($("cfg-minhold") as HTMLInputElement).value),
    allow_short: ($("cfg-short") as HTMLInputElement).checked,
    session_policy: { entry_cutoff_bars: 0, overnight: true },
    figi: window.FIGI,
  };

  try {
    $("btn-cfg-save").classList.add("busy");
    let savedId = editingId;
    if (editingId) {
      await updateConfiguration(editingId, body);
    } else {
      const created = await createConfiguration(body);
      savedId = created.configuration_id;
    }
    closeEditor();
    editingId = null;
    await refreshConfigs();
    if (runAfter && savedId) {
      const tickers = [...(window.__selectedTestTickers ?? new Set<string>())];
      const fromV = ($("cfg-test-from") as HTMLInputElement).value;
      const toV = ($("cfg-test-to") as HTMLInputElement).value;
      const days = Math.max(1, Math.round((Date.parse(toV) - Date.parse(fromV)) / 864e5) || 30);
      await launchTest(savedId, tickers, days, fromV, toV);
    }
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    $("btn-cfg-save").classList.remove("busy");
  }
}

async function editConfiguration(c: ConfigurationDto) {
  const full = await getConfiguration(c.configuration_id);
  editingId = c.configuration_id;
  openEditor();
  const nameInp = $("cfg-name") as HTMLInputElement;
  nameInp.value = full.name;
  delete nameInp.dataset.auto;
  for (const cb of document.querySelectorAll<HTMLInputElement>("#cfg-strategies input")) {
    const m = full.members.find((x) => x.strategy_id === cb.dataset.sid);
    cb.checked = !!m;
    cb.dispatchEvent(new Event("change"));
    if (m) {
      document.querySelectorAll<HTMLInputElement>(`#cfgp-${cb.dataset.sid} input`).forEach((inp) => {
        if (m.params[inp.dataset.key!] != null) inp.value = String(m.params[inp.dataset.key!]);
      });
    }
  }
  updateQuorumOptions();
  ($("cfg-interval") as HTMLSelectElement).value = full.interval_name || "hour";
  ($("cfg-exit") as HTMLSelectElement).value = full.exit_policy.id;
  renderExitParams();
  document.querySelectorAll<HTMLInputElement>("#cfg-exit-params input").forEach((inp) => {
    const v = full.exit_policy.params[inp.dataset.key!];
    if (v != null) inp.value = String(v);
  });
  ($("cfg-minhold") as HTMLInputElement).value = String(full.min_hold_bars ?? 0);
  ($("cfg-short") as HTMLInputElement).checked = !!full.allow_short;
}

async function launchTest(
  configId: string,
  tickers: string[],
  days: number,
  fromV: string,
  toV: string,
) {
  try {
    await enqueueTest(configId, tickers, fromV || null, toV || null, days);
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
    return;
  }
  await refreshConfigs();
}

export async function createFromActiveRuns(
  members: Array<{ strategy_id: string; params: Record<string, number>; role?: string }>,
  quorum: number,
  exitPolicy?: { id: string; params: Record<string, number> },
): Promise<string> {
  const cfg = await createConfiguration({
    name: `Из графика · ${new Date().toLocaleDateString("ru-RU")} · ${members.length} стр.`,
    interval_name: window.LAB_INTERVAL || "hour",
    members,
    quorum,
    exit_policy: exitPolicy ?? { id: "fixed_sl_tp", params: { stop_pct: 1, target_pct: 2 } },
    min_hold_bars: 0,
    allow_short: false,
    session_policy: { entry_cutoff_bars: 0, overnight: true },
    figi: window.FIGI,
  });
  await refreshConfigs();
  return cfg.configuration_id;
}

function closeTestDialog() {
  document.getElementById("test-backdrop")?.remove();
  document.querySelector(".modal-card")?.remove();
}

function openSettingsDialog() {
  closeTestDialog();
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  backdrop.id = "settings-backdrop";
  const modal = document.createElement("div");
  modal.className = "modal-card";
  modal.innerHTML = `
    <h3 style="margin-bottom:10px">⚙️ Настройки Lab</h3>
    <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--text-dim)">
      Максимум тестов одновременно
      <input type="number" id="ls-max" value="1" min="1" max="8" style="padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)" />
    </label>
    <div class="btn-row" style="margin-top:14px">
      <button class="btn-primary" id="ls-save">💾 Сохранить</button>
      <button class="btn-secondary" id="ls-cancel">Отмена</button>
    </div>`;
  document.body.appendChild(backdrop);
  document.body.appendChild(modal);

  void fetchLabSettings().then((s2) => {
    ($("ls-max") as HTMLInputElement).value = String(s2.max_concurrent_tests);
  }).catch(() => {});

  const close = () => { backdrop.remove(); modal.remove(); };
  backdrop.addEventListener("click", close);
  modal.querySelector("#ls-cancel")!.addEventListener("click", close);
  modal.querySelector("#ls-save")!.addEventListener("click", async () => {
    const v = Number(($("ls-max") as HTMLInputElement).value) || 1;
    await saveLabSettings(v).catch((e) => alert(e.message));
    close();
  });
}

async function showResults(configId: string) {
  let full: ConfigurationDto;
  try {
    full = await getConfiguration(configId);
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
    return;
  }
  const panel = $("lab-results-v2");
  panel.classList.remove("hidden");

  const run = queue.find((q) => q.config_id === configId);
  const isActive = run && ["RUNNING", "PAUSED"].includes(run.status);

  if (isActive) {
    renderLiveRun(panel, run!);
  } else if (full.lab_result?.totals) {
    renderLab(full);
  } else if (full.lab_result?.summary) {
    renderPreview(full);
  } else if (full.lab_result?.per_stock?.length) {
    const runLike = {
      config_name: full.name, interval_name: full.interval_name,
      date_from: ((full.lab_result as any)?.progress?.window_from ?? "") as string, date_to: ((full.lab_result as any)?.progress?.window_to ?? "") as string,
      tickers: (full.lab_result.per_stock as any[]).map((x) => x.ticker),
      progress: { done: (full.lab_result.per_stock as any[]).length, total: (full.lab_result.per_stock as any[]).length, current: "", ...(full.lab_result.progress ?? {}) },
      lab_result: full.lab_result, status: "DONE", run_id: "",
      config_id: full.configuration_id,
    } as unknown as TestRunDto;
    renderLiveRun(panel, runLike);
  } else if (full.lab_result?.error || full.status === "FAILED") {
    panel.innerHTML = `<div class="empty-note">Ошибка теста: ${full.lab_result?.error ?? full.status}</div>`;
  } else {
    panel.innerHTML = `<div class="empty-note">Результатов пока нет. Нажмите «🧪 Тестировать».</div>`;
  }
}

function renderLiveRun(panel: HTMLElement, r: TestRunDto) {
  liveViewRunId = r.run_id;
  const p = r.progress ?? { done: 0, total: r.tickers.length * 2, current: "" };
  const totalPct = p.total > 0 ? Math.min(100, Math.round((p.done / p.total) * 100)) : 0;
  const perStock = (r.lab_result?.per_stock ?? []) as Array<{
    ticker: string; mode: string; active_days: number; trades: number;
    pnl: { total: number; pos: number; neg: number };
    wins_total?: number; wins_l?: number; wins_s?: number;
    losses_total?: number; losses_l?: number; losses_s?: number;
  }>;
  const byTicker = new Map<string, any>();
  for (const st of perStock) {
    const g = byTicker.get(st.ticker) ?? { ticker: st.ticker, rows: [] };
    g.rows.push(st);
    byTicker.set(st.ticker, g);
  }
  const tickerList = [...new Set([...(r.progress as any)?.tickers ?? r.tickers, ...byTicker.keys()])];
  const stockProgress = ((r.progress as any)?.stock_progress ?? {}) as Record<string, { done: number; total: number }>;

  const stockRows = tickerList.map((t) => {
    const rows = byTicker.get(t)?.rows ?? [];
    const rec = rows.length ? rows[0] : undefined;
    const trades = rec?.trades ?? 0;
    const net = rec?.pnl?.total ?? 0;
    const pos = rec?.pnl?.pos ?? 0;
    const neg = rec?.pnl?.neg ?? 0;
    const wTotal = rec?.wins_total ?? 0;
    const wL = rec?.wins_l ?? 0;
    const wS = rec?.wins_s ?? 0;
    const lTotal = rec?.losses_total ?? 0;
    const lL = rec?.losses_l ?? 0;
    const lS = rec?.losses_s ?? 0;
    const sp = stockProgress[t] ?? { done: 0, total: 1 };
    const spPct = sp.total > 0 ? Math.min(100, Math.round((sp.done / sp.total) * 100)) : 0;
    const wlFill = splitFillStyle(wTotal, lTotal);
    const gross = (rec as any)?.gross ?? null;
    const costs = (rec as any)?.costs ?? null;
    const pf = (rec as any)?.pf ?? null;
    return `
      <tr>
        <td><b>${t}</b></td>
        <td class="num">${rec?.active_days ?? 0}</td>
        <td class="num">${trades}</td>
        <td class="num">${gross != null ? money(gross) : "—"}</td>
        <td class="num">${costs != null ? money(costs) : "—"}</td>
        <td class="num ${net >= 0 ? "pos" : "neg"}"><b>${money(net)}</b> <span class="mini-hint">(+${money(pos)} / ${money(neg)})</span></td>
        <td class="num">${pf != null ? pf.toFixed(2) : "—"}</td>
        <td class="lab-wl-cell" style="${wlFill ? `background-image:${wlFill}` : ""}">${wTotal} (L-${wL} / S-${wS}) / ${lTotal} (L-${lL} / S-${lS})</td>
        <td style="width:100px"><div class="progress-track sm"><div class="progress-fill" style="width:${spPct}%"></div></div></td>
      </tr>`;
  }).join("");
  const liveSig = `${r.status}|${p.done}|${p.current}|${stockRows}`;
  if (panel.dataset.liveSig === liveSig) return;
  panel.dataset.liveSig = liveSig;

  panel.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px">
      <b>${r.config_name}</b>
      <span class="mini-hint">${r.interval_name} · период: ${(r.date_from ?? "").slice(0, 10) || "?"} → ${(r.date_to ?? "").slice(0, 10) || "сейчас"}</span>
      <span class="status-badge st-in_lab">${r.status === "PAUSED" ? "пауза" : "тест идёт"}</span>
    </div>
    <div class="cfg-dump">${configDump({ name: r.config_name, interval_name: r.interval_name, quorum: r.quorum, members: r.members ?? [], exit_policy: r.exit_policy ?? {}, min_hold_bars: r.min_hold_bars ?? 0, allow_short: r.allow_short ?? false })}</div>
    <div class="mini-hint" style="margin-top:4px">акции: ${r.tickers.join(", ")}</div>
    <div class="mini-hint" style="margin-bottom:4px;margin-top:8px">Текущая акция: ${p.current ?? "—"}</div>
    <div class="progress-track"><div class="progress-fill" style="width:${Math.max(3, totalPct)}%"></div></div>
    <div class="mini-hint" style="margin-bottom:4px;margin-top:6px">Общий прогресс: ${p.done}/${p.total} акций</div>
    <div class="progress-track"><div class="progress-fill" style="width:${Math.max(3, totalPct)}%"></div></div>
    <table class="runs-table" style="margin-top:10px">
      <thead>
        <tr><th>Акция</th><th>Дней</th><th>Сделок</th><th>Gross</th><th>Costs</th><th>Net</th><th>PF</th><th>W (L/S) / У</th><th>Прогресс</th></tr>
      </thead>
      <tbody>${stockRows || `<tr><td colspan="9" class="mini-hint">ожидание данных прогона…</td></tr>`}</tbody>
    </table>
    <div class="totals-line" style="margin-top:8px">
      <span>Сводка (по готовым акциям)</span>
      <span>акций готово: <b>${perStock.length}/${tickerList.length}</b></span>
    </div>`;
}

function funnelBlock(funnel: Record<string, unknown>): string {
  const rows = Object.entries(funnel)
    .filter(([k]) => k !== "quorum_signals")
    .map(([k, v]) => {
      const d = v as { BUY?: number; SELL?: number };
      return `<tr><td>${k.replace(":raw", "")}</td><td class="num">${d.BUY ?? 0}</td><td class="num">${d.SELL ?? 0}</td></tr>`;
    })
    .join("");
  const q = Number(funnel.quorum_signals ?? 0);
  return `<h3 class="pg-h3">Воронка сигналов</h3>
    <table class="runs-table">
      <thead><tr><th>Стратегия</th><th>BUY</th><th>SELL</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="totals-line"><span>Кворум-сигналов всего: <b>${q}</b></span>
    <span class="mini-hint">если мало — стратегии редко соглашаются на этом ТФ/периоде</span></div>`;
}

const fmtT = (iso: unknown): string => {
  const d = new Date(String(iso));
  return isNaN(d.getTime()) ? "—" : d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
};

function tradesTable(trades: Array<Record<string, unknown>>): string {
  if (!trades.length) return `<div class="empty-note">сделок нет</div>`;
  const rows = trades
    .map(
      (t) =>
        `<tr class="trade-row-click">
          <td><b>${String(t.side)}</b> <span class="mini-hint">[${String(t.mode)}]</span></td>
          <td>${fmtT(t.entry_time)} · ${money(Number(t.entry_price))}</td>
          <td>${fmtT(t.exit_time)} · ${money(Number(t.exit_price))}</td>
          <td>${money(Number(t.gross_pnl))} <span class="mini-hint">(комисс. −${money(Number(t.commission))})</span></td>
          <td>${t.exit_reason ?? "—"}</td>
          <td class="num ${Number(t.net_pnl) >= 0 ? "pos" : "neg"}"><b>${money(Number(t.net_pnl))}</b></td>
        </tr>`,
    )
    .join("");
  return `<table class="runs-table">
    <thead><tr><th>Side</th><th>Вход (время · цена)</th><th>Выход (время · цена)</th><th>Gross (комисс.)</th><th>Причина</th><th>PnL</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function bindOnchartButtons(panel: HTMLElement, trades: Array<Record<string, unknown>>) {
  panel.querySelectorAll<HTMLButtonElement>(".btn-onchart").forEach((b) =>
    b.addEventListener("click", () => {
      const trade = trades[Number(b.dataset.i)];
      if (!trade || !window.__chartOverlay) return;
      window.__chartOverlay(trade);
      document.querySelector<HTMLButtonElement>('.nav-btn[data-page="chart"]')?.click();
    }),
  );
}

function metricsGrid(s: Record<string, number>, extra?: Array<[string, string, boolean?]>) {
  const cell = (k: string, v: string, pos?: boolean) =>
    `<div class="metric"><span class="k">${k}</span><span class="v${pos != null ? (pos ? " pos" : " neg") : ""}">${v}</span></div>`;
  return `<div class="metrics-grid">
    ${cell("Net P&L", money(s.net), s.net >= 0)}
    ${cell("Profit factor", String(s.profit_factor ?? "—"))}
    ${cell("Win rate", `${s.win_rate ?? "—"}%`)}
    ${cell("Сделок", String(s.trades ?? 0))}
    ${cell("Expectancy", money(s.expectancy), (s.expectancy ?? 0) >= 0)}
    ${extra?.map(([k, v, pos]) => cell(k, v, pos)).join("") ?? ""}
  </div>`;
}

function renderPreview(full: ConfigurationDto) {
  const lr = full.lab_result ?? {};
  const summary = (lr.summary ?? {}) as Record<string, any>;
  const s: Record<string, number> = summary.summary ?? {};
  const trades = (lr.trades_preview ?? []) as Array<Record<string, unknown>>;
  const verdict = ((summary.verdict ?? {}) as Record<string, unknown>).preview_verdict ?? "preview";

  const panel = $("lab-results-v2");
  panel.classList.remove("hidden");
  panel.innerHTML = `
    <div class="notice-preview">⚡ PREVIEW — быстрый расчёт на одной акции (${lr.figi ?? window.FIGI}). Не полноценный backtest.</div>
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px">
      <b>${full.name}</b> ${verdictBadge(String(verdict))}
    </div>
    ${metricsGrid(s, [["Max DD", `${summary.max_drawdown_pct ?? "—"}%`, false]])}
    <h3 class="pg-h3">Воронка сигналов</h3>
    ${funnelBlock(lr.funnel ?? {})}
    <h3 class="pg-h3">Preview-сделки</h3>
    ${tradesTable(trades)}
  `;
  bindOnchartButtons(panel, trades);
}

function renderLab(full: ConfigurationDto) {
  const lab = full.lab_result ?? {};
  const totals = (lab.totals ?? {}) as Record<string, number>;
  const perStock = (lab.per_stock ?? []) as Array<{
    ticker: string; mode: string; active_days: number; trades: number;
    pnl: { total: number; pos: number; neg: number };
  }>;
  const allTrades = (lab.trades ?? []) as Array<Record<string, unknown>>;

  const by = new Map<string, { ticker: string; trades: number; wins: number; winsL: number; winsS: number; losses: number; lossesL: number; lossesS: number; total: number; pos: number; neg: number; days: number }>();
  for (const p of perStock) {
    const g = by.get(p.ticker) ?? { ticker: p.ticker, trades: 0, wins: 0, winsL: 0, winsS: 0, losses: 0, lossesL: 0, lossesS: 0, total: 0, pos: 0, neg: 0, days: 0 };
    g.trades += Number(p.trades ?? 0);
    g.wins += Number((p as any).wins_total ?? 0);
    g.winsL += Number((p as any).wins_l ?? 0);
    g.winsS += Number((p as any).wins_s ?? 0);
    g.losses += Number((p as any).losses_total ?? 0);
    g.lossesL += Number((p as any).losses_l ?? 0);
    g.lossesS += Number((p as any).losses_s ?? 0);
    g.total += Number(p.pnl?.total ?? 0);
    g.pos += Number(p.pnl?.pos ?? 0);
    g.neg += Number(p.pnl?.neg ?? 0);
    g.days = Math.max(g.days, Number(p.active_days ?? 0));
    by.set(p.ticker, g);
  }
  const rows = [...by.values()].sort((a, b) => b.total - a.total);

  const panel = $("lab-results-v2");
  panel.classList.remove("hidden");
  panel.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px">
      <b>${full.name}</b>
      ${full.lab_result?.config_snapshot ? `<span class="mini-hint">[${(configSnap(full) as any)?.interval_name}]</span>` : ""}
      ${full.research_status ? verdictBadge(full.research_status) : ""}
    </div>
    <div class="totals-line">
      <span>Сделок: <b>${totals.trades ?? 0}</b></span>
      <span class="pos">+${totals.wins ?? 0}</span>
      <span class="neg">−${totals.losses ?? 0}</span>
      <span>PnL: <b class="${(totals.total_net ?? 0) >= 0 ? "pos" : "neg"}">${money(totals.total_net)} ₽</b></span>
      <span>Прибыльных акций: <b>${totals.positive_stocks ?? 0} / ${totals.stocks ?? 0}</b></span>
    </div>
    ${lab.funnel ? funnelBlock(lab.funnel as Record<string, unknown>) : ""}
    ${lab.elapsed_sec != null ? `<div class="mini-hint">Расчёт занял ${lab.elapsed_sec} с · сделок выполнено: ${allTrades.length}</div>` : ""}
    <details class="collapsible" id="lab-decisions-box">
      <summary>Decisions — почему сигнал стал/не стал сделкой</summary>
      <div style="display:flex;gap:8px;align-items:center;margin:8px 0">
        <select id="dec-strategy" class="input">
          ${Object.entries(((full.source_runs ?? {}) as Record<string, string>)).map(([k, v]) => `<option value="${v}">${k}</option>`).join("")}
        </select>
        <label class="mini-hint">min_hold <input id="dec-minhold" type="number" min="0" max="100" value="0" style="width:56px"></label>
        <button id="dec-load" class="btn-secondary">Показать</button>
        <span id="dec-counts" class="mini-hint"></span>
      </div>
      <div id="dec-table-holder"><span class="mini-hint">выберите стратегию и нажмите «Показать»</span></div>
    </details>
    <h3 class="pg-h3">По акциям — long и short в одной строке</h3>
    <table class="runs-table">
      <thead><tr><th>Тикер</th><th>Дней</th><th>W (L / S) / У (L / S)</th><th>Сделки</th><th>PnL (+ / −)</th></tr></thead>
      <tbody>
        ${rows.map((r) => `
          <tr class="trade-row-click" data-ticker="${r.ticker}">
            <td><b>${r.ticker}</b></td>
            <td class="num">${r.days}</td>
            <td>${r.wins} (L-${r.winsL} / S-${r.winsS}) / ${r.losses} (L-${r.lossesL} / S-${r.lossesS})</td>
            <td class="num">${r.trades}</td>
            <td class="num ${(r.total) >= 0 ? "pos" : "neg"}"><b>${money(r.total)}</b>
              <span class="mini-hint">( +${money(r.pos)} / ${money(r.neg)} )</span></td>
          </tr>`).join("")}
      </tbody>
    </table>
    <div class="metrics-grid" style="margin-top:10px">
      <div class="metric"><span class="k">Серийных убытков макс.</span><span class="v">${lab.consecutive_losses ?? "—"}</span></div>
      <div class="metric"><span class="k">Net без топ-1</span><span class="v ${(lab.top1_analysis?.net_without_top1 ?? 0) >= 0 ? "pos" : "neg"}">${money(lab.top1_analysis?.net_without_top1)}</span></div>
      <div class="metric"><span class="k">Топ-1</span><span class="v">${lab.top1_analysis?.top1_figi ?? "—"}: ${money(lab.top1_analysis?.top1_net)}</span></div>
      <div class="metric"><span class="k">Концентрация</span><span class="v">${lab.top1_analysis?.concentration_pct ?? "—"}%</span></div>
    </div>
    <h3 class="pg-h3">P&L по дням</h3>
    <div class="day-cells">
      ${((lab.by_day ?? []) as Array<{ date: string; net: number }>).map((d) => `
        <div class="day-cell ${d.net >= 0 ? "day-pos" : "day-neg"}" title="${d.date}: ${money(d.net)} ₽">
          <span class="d">${d.date.slice(5)}</span><span class="n">${money(d.net)}</span>
        </div>`).join("")}
    </div>
    <details class="collapsible">
      <summary>Все сделки (${allTrades.length}) — клик по строке откроет график</summary>
      <div id="lab-trades-holder" style="margin-top:8px">${tradesTable(allTrades)}</div>
    </details>
  `;

  const decBtn = panel.querySelector<HTMLButtonElement>("#dec-load");
  decBtn?.addEventListener("click", async () => {
    const runId = panel.querySelector<HTMLSelectElement>("#dec-strategy")?.value;
    const mh = Number(panel.querySelector<HTMLInputElement>("#dec-minhold")?.value ?? 0);
    const holder = panel.querySelector<HTMLElement>("#dec-table-holder");
    if (!runId || !holder) return;
    holder.innerHTML = `<span class="mini-hint">считаю…</span>`;
    try {
      const d = await fetchDecisions(runId, mh);
      const rows = (d.decisions ?? []).map((x) => `
        <tr>
          <td>${fmtT(x.ts)}</td>
          <td><b>${x.side}</b></td>
          <td class="mini-hint">${x.details.position_state_before} → ${x.details.position_state_after}</td>
          <td class="${x.decision === "ACCEPTED" ? "pos" : x.decision === "REJECTED" ? "neg" : ""}"><b>${x.decision}</b></td>
          <td class="mini-hint">${x.reason_code}${x.reason ? ` · ${x.reason}` : ""}</td>
        </tr>`).join("");
      holder.innerHTML = `<table class="runs-table">
        <thead><tr><th>Время</th><th>Сигнал</th><th>State</th><th>Decision</th><th>Reason</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="mini-hint">сигналов нет</td></tr>`}</tbody></table>`;
      const c = d.counts ?? {};
      panel.querySelector<HTMLElement>("#dec-counts")!.textContent =
        `принято: ${c.ACCEPTED ?? 0} · игнор: ${c.IGNORED ?? 0} · отказ: ${c.REJECTED ?? 0}`;
    } catch (e) {
      holder.innerHTML = `<span class="neg">ошибка: ${e instanceof Error ? e.message : String(e)}</span>`;
    }
  });
  panel.querySelectorAll<HTMLInputElement>("tr.trade-row-click[data-ticker]").forEach((row) => {
    row.addEventListener("click", () => {
      selectedTickerForDetails = row.dataset.ticker!;
      renderTickerSummary(panel, full, allTrades.filter((t) => String(t.ticker) === selectedTickerForDetails));
    });
  });

  panel.querySelectorAll<HTMLButtonElement>(".btn-onchart").forEach((b) => {
    b.addEventListener("click", () => {
      const trade = allTrades[Number(b.dataset.i)];
      if (!trade || !window.__chartOverlay) return;
      window.__chartOverlay(trade);
      document.querySelector<HTMLButtonElement>('.nav-btn[data-page="chart"]')?.click();
    });
  });
}

function configSnap(full: ConfigurationDto): unknown {
  return full.lab_result?.config_snapshot ?? null;
}

function renderTickerSummary(
  panel: HTMLElement,
  _full: ConfigurationDto,
  trades: Array<Record<string, unknown>>,
) {
  const ticker = selectedTickerForDetails!;
  const longs = trades.filter((t) => String(t.mode) === "long");
  const shorts = trades.filter((t) => String(t.mode) === "short");
  const sum = (arr: Array<Record<string, unknown>>, k: string) =>
    arr.reduce((acc, t) => acc + Number(t[k] ?? 0), 0);

  const chartBoxId = "ticker-chart-box";
  let chartAnchor = panel.querySelector("#" + chartBoxId) as HTMLElement | null;
  if (!chartAnchor) {
    chartAnchor = document.createElement("div");
    chartAnchor.id = chartBoxId;
    panel.appendChild(chartAnchor);
  }
  chartAnchor.innerHTML = `<div id="lab-chart" class="lab-chart-box"><span class="mini-hint">Загружаю свечи для Lab-графика…</span></div>`;

  const holderId = "ticker-trades-holder";
  let holder = panel.querySelector("#" + holderId) as HTMLElement | null;
  if (!holder) {
    holder = document.createElement("div");
    holder.id = holderId;
  }
  holder.innerHTML = `
    <h3 class="pg-h3">Сводка: ${ticker}</h3>
    <div class="totals-line">
      <span>Сделок: <b>${trades.length}</b></span>
      <span><span class="arr-up">▲</span> Long: ${longs.length} (${money(sum(longs, "net_pnl"))} ₽)</span>
      <span><span class="arr-dn">▼</span> Short: ${shorts.length} (${money(sum(shorts, "net_pnl"))} ₽)</span>
      <span>PnL: <b class="${sum(trades, "net_pnl") >= 0 ? "pos" : "neg"}">${money(sum(trades, "net_pnl"))} ₽</b></span>
    </div>
    ${tradesTable(trades)}`;
  panel.appendChild(holder);

  const cfgInterval = configs.find((c) => c.configuration_id === (window as any).__lastConfigId)?.interval_name ?? "hour";
  const sel = configs.find((c) => c.preview_figi === (window as any).FIGI);
  void sel;
  const interval_name = ((configSnap(_full) as any)?.interval_name as string) ?? cfgInterval ?? "hour";

  fetch(`/api/candles/${window.FIGI}?interval_name=${interval_name}&limit=5000`)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`candles ${r.status}`))))
    .then((d: { candles: Array<Record<string, unknown>> }) => {
      const t0 = trades.length ? String(trades[0].entry_time) : "";
      const t1 = trades.length ? String(trades[trades.length - 1].exit_time) : "";
      const candles = d.candles.filter((c) => !t0 || (String(c.ts) >= t0 && String(c.ts) <= t1)).map((c) => ({
        ts: String(c.ts), open: Number(c.open), high: Number(c.high),
        low: Number(c.low), close: Number(c.close), volume: Number(c.volume),
      }));
      createLabChart(chartAnchor!.querySelector("#lab-chart") as HTMLElement, candles, trades as never[]);
    })
    .catch((e) => (chartAnchor!.innerHTML = `<span class="mini-hint">${e.message}</span>`));
}
