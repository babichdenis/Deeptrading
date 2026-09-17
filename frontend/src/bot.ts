declare global {
  interface Window { lucide?: { createIcons: (o?: unknown) => void } }
}

const API = window.location.port === "5173" ? `http://${window.location.hostname}:8000` : "";

import {
  botCancelPending,
  botCloseAll,
  botEnsembleConfig,
  botEnsemblePatch,
  botEnsembleReset,
  type EnsembleConfig,
  botEvents,
  type BotEventRow,
  type BotOrderRow,
  botPause,
  botReset,
  botSetMode,
  botStart,
  botStatus,
  botStop,
  fetchCatalog,
  type BotTradeRow,
  type StrategyCardDto,
  sandboxStatus,
  sandboxPositions,
  sandboxTrades,
  sandboxOrders,
  fetchScreener,
  screenerAddEligible,
  screenerRemoveEligible,
  type ScreenerRow,
  type SandboxPositionRow,
  type CarouselStatus,
  fetchTests,
  deleteTest,
  type TestRunRow,
  fetchTestStats,
  type TestStats,
  type StatsRow,
  fetchTestsCompare,
  type TestsCompare,
  type TestCompareEntry,
} from "./api";

const $ = (id: string) => document.getElementById(id) as HTMLElement;

let catalog: StrategyCardDto[] = [];
let lastRunning: boolean | null = null;
let entriesPaused = false;

function money(n: number | null | undefined): string {
  if (n == null) return "—";
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(n);
}

function price(n: number | null | undefined): string {
  if (n == null) return "—";
  const s = Number(n.toPrecision(10)).toString();
  return s.includes(".") ? s.replace(/\.?0+$/, "") : s;
}

function pnlAtStop(p: SandboxPositionRow): number {
  if (p.stop_loss == null) return 0;
  const isShort = p.side.toUpperCase() === "SHORT";
  return isShort
    ? (p.entry_price - p.stop_loss) * p.qty
    : (p.stop_loss - p.entry_price) * p.qty;
}

const _dtMSK = new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });

function fmtTime(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  return _dtMSK.format(d);
}

function fmtTimeOnly(ts: string): string {
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  const h = d.toLocaleString("ru-RU", { timeZone: "Europe/Moscow", hour: "2-digit", minute: "2-digit", hour12: false });
  return h;
}

function sideIcon(side: string): string {
  const s = side.toUpperCase();
  if (s === "LONG" || s === "BUY") return `<span class="side-up">▲</span>`;
  if (s === "SHORT" || s === "SELL") return `<span class="side-down">▼</span>`;
  return side;
}

const _logoFail = new Set<string>();
function tickerLogo(ticker: string, fullName?: string): string {
  const safe = ticker.replace(/[^A-Za-z0-9._-]/g, "");
  const img = `/icons/${safe}.png`;
  const letter = (safe[0] || "").toUpperCase() || "?";
  if (!fullName) {
    const sr = _srRows.find((x) => x.ticker === ticker);
    if (sr) fullName = sr.name;
  }
  const name = fullName ? ` title="${fullName.replace(/"/g, "&quot;")}"` : "";
  if (_logoFail.has(safe)) return `<span class="tick-logo${name}"><i>${letter}</i></span>`;
  return `<span class="tick-logo${name}"><img src="${img}" alt="" onload="this.style.opacity=1" onerror="this.style.display='none';this.nextElementSibling&&(this.nextElementSibling.style.display='flex');window.__logoFailed&window.__logoFailed('${safe}')"><i>${letter}</i></span>`;
}

declare global {
  interface Window { __logoFailed?: (safe: string) => void }
}
window.__logoFailed = (safe: string) => { _logoFail.add(safe); };
window.addEventListener("DOMContentLoaded", () => {
  const els = document.querySelectorAll<HTMLElement>("[data-logo]");
  els.forEach((el) => {
    const full = el.getAttribute("data-logo");
    if (full) {
      const im = new Image();
      im.src = `/icons/${full}.png`;
      im.onerror = () => _logoFail.add(full);
    }
  });
});

function priceTrend(cur: number, prev: number | null): string {
  if (prev == null || cur === prev) return "";
  return cur > prev ? " pos" : " neg";
}


async function doClosePosition(ticker: string, side: string, qty: number) {
  const action = side.toUpperCase() === "LONG" ? "продать" : "купить";
  if (!confirm(`Закрыть позицию ${ticker}? (${action} ${qty} шт)`)) return;
  try {
    const positions = await sandboxPositions();
    const pos = positions.find((p) => p.ticker === ticker);
    if (!pos) {
      alert("Позиция не найдена");
      return;
    }
    const resp = await fetch(`${API}/api/v1/bot/positions/close?figi=${encodeURIComponent(pos.figi)}`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    await pollOnce();
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
  }
}

// Expose close position function globally for onclick handlers
(window as any).__closePosition = doClosePosition;


function initBotResizer() {
  const wrap = document.getElementById("bot-chart-wrap");
  const resizer = document.getElementById("bot-resizer");
  if (!wrap || !resizer) return;
  const clamp = (h: number) => Math.max(240, Math.min(h, window.innerHeight - 160));
  const saved = Number(localStorage.getItem("botChartHeight"));
  wrap.style.height = `${clamp(saved > 0 ? saved : Math.round(window.innerHeight * 0.5))}px`;
  let startY = 0;
  let startH = 0;
  const onMove = (e: PointerEvent) => {
    wrap.style.height = `${clamp(startH + (e.clientY - startY))}px`;
  };
  const onUp = () => {
    resizer.classList.remove("dragging");
    localStorage.setItem("botChartHeight", String(wrap.clientHeight));
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
  };
  resizer.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    startY = e.clientY;
    startH = wrap.clientHeight;
    resizer.classList.add("dragging");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
}

const MARGIN_LEV_STEPS = [2, 3, 4, 5, 0]; // 0 = Max (как одобрит брокер)
function levSliderToValue(i: number): number { return MARGIN_LEV_STEPS[i] ?? 0; }
function levValueToSlider(v: number): number {
  const i = MARGIN_LEV_STEPS.indexOf(v);
  if (i >= 0) return i;
  return 4; // не из списка → Max
}
function levLabel(v: number): string { return v > 0 ? `×${v}` : "Max"; }
function levPaint(idx: number | string): void {
  const el = $("mg-lev") as HTMLInputElement | null;
  if (!el) return;
  const i = Math.min(4, Math.max(0, Number(idx) || 0));
  const pct = (i / 4) * 100;
  el.style.setProperty("--fill", `${pct}%`);
}
function levPaintFromValue(v: number): void {
  levPaint(levValueToSlider(v));
}

function sendBotConfigPatch(extra?: Record<string, unknown>) {
  const sessMap: Record<string, string> = {
    "sg-morning": "morning", "sg-day": "day", "sg-evening": "evening"
  };
  const sessKeys = ["sg-morning", "sg-day", "sg-evening"];
  const sessions = sessKeys.filter((x) => ($(x) as HTMLInputElement).checked).map((x) => sessMap[x]);
  const longOn = ($("dg-long") as HTMLInputElement)?.checked ?? true;
  const shortOn = ($("dg-short") as HTMLInputElement)?.checked ?? false;
  const mgMap: Record<string, string> = {
    "mg-morning": "morning", "mg-day": "day", "mg-evening": "evening"
  };
  const mgKeys = ["mg-morning", "mg-day", "mg-evening"];
  const margin_sessions = mgKeys
    .filter((x) => ($(x) as HTMLInputElement | null)?.checked)
    .map((x) => mgMap[x]);
  const levEl = $("mg-lev") as HTMLInputElement | null;
  const margin_leverage = levEl ? levSliderToValue(Number(levEl.value)) : 0;
  const rgMap: Record<string, string> = {
    "rg-neutral": "NEUTRAL", "rg-trendup": "TREND_UP", "rg-trenddown": "TREND_DOWN",
    "rg-highvol": "HIGH_VOLATILITY", "rg-range": "RANGE",
  };
  const trade_regimes = Object.keys(rgMap)
    .filter((x) => ($(x) as HTMLInputElement | null)?.checked)
    .map((x) => rgMap[x]);
  const body: Record<string, unknown> = {
    sessions: sessions.length ? sessions : ["day"],
    long_allowed: longOn,
    short_allowed: shortOn,
    margin_sessions,
    margin_leverage,
    trade_regimes,
    overnight: ($("sg-overnight") as HTMLInputElement | null)?.checked ?? false,
  };
  if (extra) Object.assign(body, extra);
  fetch(`${API}/api/v1/bot/config`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => {});
}

function applyChip(cb: HTMLInputElement | null, on: boolean) {
  if (!cb) return;
  if (cb.checked !== on) {
    cb.checked = on;
    cb.closest("label")?.classList.toggle("on", on);
  }
}

function initSessChips() {
  const sessMap: Record<string, string> = { morning: "sg-morning", day: "sg-day", evening: "sg-evening" };
  let saved: string[] = [];
  try {
    saved = JSON.parse(localStorage.getItem("bot_sess") || "[]");
  } catch { saved = []; }
  const sessKeys = Object.values(sessMap);
  const defaultSess = saved.length ? saved : ["sg-morning", "sg-day", "sg-evening"];
  for (const id of sessKeys) {
    const cb = $(id) as HTMLInputElement | null;
    if (!cb) continue;
    cb.checked = defaultSess.includes(id);
    cb.closest("label")?.classList.toggle("on", cb.checked);
    cb.addEventListener("change", () => {
      cb.closest("label")?.classList.toggle("on", cb.checked);
      const on = sessKeys.filter((x) => ($(x) as HTMLInputElement).checked);
      localStorage.setItem("bot_sess", JSON.stringify(on.length ? on : ["sg-morning", "sg-day", "sg-evening"]));
      sendBotConfigPatch();
    });
  }

  // Загружаем настройки с сервера (config-файл) и применяем ко всем чипам.
  void (async () => {
    try {
      const resp = await fetch(`${API}/api/v1/bot/config`);
      if (!resp.ok) return;
      const cfg = await resp.json() as Record<string, unknown>;
      // Сессии.
      const sArr = Array.isArray(cfg.sessions) ? cfg.sessions as string[] : null;
      if (sArr && sArr.length) {
        for (const [s, id] of Object.entries(sessMap)) {
          const cb = $(id) as HTMLInputElement | null;
          if (!cb) continue;
          const on = sArr.includes(s);
          if (cb.checked !== on) {
            cb.checked = on;
            cb.closest("label")?.classList.toggle("on", on);
          }
        }
        localStorage.setItem("bot_sess", JSON.stringify(sArr.map((x) => sessMap[x]).filter(Boolean)));
      }
      // Направления.
      const lOn = (cfg as { long_allowed?: boolean }).long_allowed;
      const sOn = (cfg as { short_allowed?: boolean }).short_allowed;
      const dLong = $("dg-long") as HTMLInputElement | null;
      const dShort = $("dg-short") as HTMLInputElement | null;
      if (typeof lOn === "boolean" && dLong) applyChip(dLong, lOn);
      if (typeof sOn === "boolean" && dShort) applyChip(dShort, sOn);
      // Маржа.
      const msArr = Array.isArray(cfg.margin_sessions) ? cfg.margin_sessions as string[] : null;
      if (msArr) {
        for (const [s, id] of Object.entries({ morning: "mg-morning", day: "mg-day", evening: "mg-evening" })) {
          const cb = $(id) as HTMLInputElement | null;
          if (!cb) continue;
          const on = msArr.includes(s);
          if (cb.checked !== on) {
            cb.checked = on;
            cb.closest("label")?.classList.toggle("on", on);
          }
        }
      } else { sendBotConfigPatch(); }
      const mlv = (cfg as { margin_leverage?: number }).margin_leverage;
      const levEl = $("mg-lev") as HTMLInputElement | null;
      if (typeof mlv === "number" && levEl) {
        const idx = levValueToSlider(mlv);
        if (Number(levEl.value) !== idx) {
          levEl.value = String(idx);
          const lbl = $("mg-lev-label");
          if (lbl) lbl.textContent = levLabel(mlv);
          levPaint(idx);
        }
      }
      // Режимы.
      const trArr = Array.isArray(cfg.trade_regimes) ? cfg.trade_regimes as string[] : null;
      const trMap: Record<string, string> = {
        NEUTRAL: "rg-neutral", TREND_UP: "rg-trendup", TREND_DOWN: "rg-trenddown",
        HIGH_VOLATILITY: "rg-highvol", RANGE: "rg-range",
      };
      if (trArr) {
        for (const [k, id] of Object.entries(trMap)) {
          const cb = $(id) as HTMLInputElement | null;
          if (!cb) continue;
          const on = trArr.includes(k);
          if (cb.checked !== on) {
            cb.checked = on;
            cb.closest("label")?.classList.toggle("on", on);
          }
        }
      }
      // Overnight.
      const ov = (cfg as { overnight?: boolean }).overnight;
      const ovEl = $("sg-overnight") as HTMLInputElement | null;
      if (typeof ov === "boolean" && ovEl) applyChip(ovEl, ov);
    } catch { /* бэкенд недоступен — остаёмся на localStorage */ }
  })();

  // Overnight: держать позиции через ночь.
  const ovEl = $("sg-overnight") as HTMLInputElement | null;
  if (ovEl) {
    let ovSaved = false;
    try { ovSaved = localStorage.getItem("bot_overnight") === "1"; } catch { ovSaved = false; }
    ovEl.checked = ovSaved;
    ovEl.closest("label")?.classList.toggle("on", ovSaved);
    ovEl.addEventListener("change", () => {
      ovEl.closest("label")?.classList.toggle("on", ovEl.checked);
      localStorage.setItem("bot_overnight", ovEl.checked ? "1" : "0");
      sendBotConfigPatch();
    });
  }
  // Блок «Маржа»: сессии, где торговля идёт с плечом.
  const mgKeys = ["mg-morning", "mg-day", "mg-evening"];
  let mgSaved: string[] = [];
  try { mgSaved = JSON.parse(localStorage.getItem("bot_margin") || "[]"); } catch { mgSaved = []; }
  const defMg = mgSaved.length ? mgSaved : ["mg-day"];
  for (const id of mgKeys) {
    const cb = $(id) as HTMLInputElement | null;
    if (!cb) continue;
    cb.checked = defMg.includes(id);
    cb.closest("label")?.classList.toggle("on", cb.checked);
    cb.addEventListener("change", () => {
      cb.closest("label")?.classList.toggle("on", cb.checked);
      const on = mgKeys.filter((x) => ($(x) as HTMLInputElement).checked);
      localStorage.setItem("bot_margin", JSON.stringify(on));
      sendBotConfigPatch();
    });
  }
  // Ползунок плеча при марже: 2-3-4-5-Max.
  const levEl = $("mg-lev") as HTMLInputElement | null;
  if (levEl) {
    const updLabel = () => {
      const v = levSliderToValue(Number(levEl.value));
      const lbl = $("mg-lev-label");
      if (lbl) lbl.textContent = levLabel(v);
    };
    const savedLev = localStorage.getItem("bot_margin_lev");
    if (savedLev != null) levEl.value = String(levValueToSlider(Number(savedLev)));
    updLabel();
    levPaint(levEl.value);
    levEl.addEventListener("input", () => {
      localStorage.setItem("bot_margin_lev", String(levSliderToValue(Number(levEl.value))));
      updLabel();
      levPaint(levEl.value);
      sendBotConfigPatch();
    });
  }
  // Блок «Режимы»: в каких режимах рынка разрешены входы.
  const rgIds = ["rg-neutral", "rg-trendup", "rg-trenddown", "rg-highvol", "rg-range"];
  let rgSaved: string[] = [];
  try { rgSaved = JSON.parse(localStorage.getItem("bot_regimes") || "[]"); } catch { rgSaved = []; }
  const defRg = rgSaved.length ? rgSaved : rgIds;
  for (const id of rgIds) {
    const cb = $(id) as HTMLInputElement | null;
    if (!cb) continue;
    cb.checked = defRg.includes(id);
    cb.closest("label")?.classList.toggle("on", cb.checked);
    cb.addEventListener("change", () => {
      cb.closest("label")?.classList.toggle("on", cb.checked);
      const on = rgIds.filter((x) => ($(x) as HTMLInputElement).checked);
      localStorage.setItem("bot_regimes", JSON.stringify(on));
      sendBotConfigPatch();
    });
  }
  const dirIds = ["dg-long", "dg-short"];
  let dirSaved: string[] = [];
  try { dirSaved = JSON.parse(localStorage.getItem("bot_dir") || "[]"); } catch { dirSaved = []; }
  const defDir = dirSaved.length ? dirSaved : ["dg-long"];
  for (const id of dirIds) {
    const cb = $(id) as HTMLInputElement | null;
    if (!cb) continue;
    cb.checked = defDir.includes(id);
    cb.closest("label")?.classList.toggle("on", cb.checked);
    cb.addEventListener("change", () => {
      cb.closest("label")?.classList.toggle("on", cb.checked);
      const on = dirIds.filter((x) => ($(x) as HTMLInputElement).checked);
      localStorage.setItem("bot_dir", JSON.stringify(on));
      sendBotConfigPatch();
    });
  }
}

export async function initBot(onStateChange?: (running: boolean) => void) {
  try { window.lucide?.createIcons(); } catch { /* иконки необязательны */ }
  initSessChips();
  initBotSettings();
  $("btn-bot-start").addEventListener("click", () => void doStart());
  $("btn-bot-stop").addEventListener("click", () => void doStop());
  $("btn-bot-pause").addEventListener("click", () => void doPause());

  // Переключение контура Live / Sandbox / Тест (сегментные кнопки в сайдбаре).
  document.querySelectorAll("#mode-switch .mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = (btn as HTMLElement).dataset.mode as "live" | "sandbox" | "test";
      void doToggleMode(mode);
    });
  });
  // Модалка запуска теста (имя + период реплея).
  const tOverlay = $("test-modal-overlay");
  if (tOverlay) {
    tOverlay.addEventListener("click", (e) => { if (e.target === tOverlay) closeTestModal(); });
    $("ts-close")?.addEventListener("click", closeTestModal);
    $("ts-run")?.addEventListener("click", () => void doRunTest());
  }
  initTestResults();

  document.querySelector("#bot-positions-table tbody")?.addEventListener("click", (e) => {
    const tr = (e.target as HTMLElement).closest("tr") as HTMLElement | null;
    if (!tr) return;
    const idx = Array.from(tr.parentElement!.children).indexOf(tr);
    const p = _lastPositions[idx];
    if (p) {
      _focusPos = { figi: p.figi, ticker: p.ticker };
      sendEmbedFocus(p.figi, p.ticker, positionAsTrade(p));
    }
  });
  document.querySelector("#bot-trades-table tbody")?.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    const tr = target.closest("tr.trade-row") as HTMLElement | null;
    if (!tr) return;
    const idx = Number(tr.dataset.idx);
    const t = _lastTrades[idx];
    if (!t) return;
    // Стрелка справа → toggle деталей (БЕЗ открытия графика).
    if (target.closest(".td-arrow")) {
      const key = String(tr.dataset.key || "");
      if (_openDetails.has(key)) _openDetails.delete(key); else _openDetails.add(key);
      renderTrades(_lastTrades);
      return;
    }
    // Любой другой клик по строке → фокус графика (без toggle деталей).
    _focusPos = null;
    sendEmbedFocus(t.figi, t.ticker, tradeAsTrade(t));
  });

  void pollOnce();
  setInterval(() => void pollOnce(onStateChange), 8000);
  setInterval(() => void pollLogs(), 1000);
  setupLogFilters();
  initBotResizer();
  initScreener();
}

function _loadLGF() {
  const saved = localStorage.getItem("log_lgf");
  if (saved) { try { return JSON.parse(saved); } catch {} }
  return { candles: true, signals: true, trades: true, events: true, tech: true };
}
const LGF: Record<string, boolean> = _loadLGF();
let _logDateFilter = localStorage.getItem("log_date_filter") ?? new Date().toLocaleDateString("sv-SE");
let _allLogs: string[] = [];
let _lastPositions: SandboxPositionRow[] = [];
let _lastTrades: BotTradeRow[] = [];
let _prevVol: Record<string, number> = {};  // ticker -> предыдущий Vol (для стрелки направления)
let _curMode = "sandbox";  // текущий контур (из bot.status.config.mode)
let _openDetails = new Set<string>();  // ключи раскрытых деталей сделок (можно много)
let _lastVotes: Array<{ figi: string; ticker: string; buy: number; sell: number; votes: number; side: string; regime?: string | null; vol?: number | null; vol_abs?: number | null }> = [];
let _lastUniverse: Array<{ figi: string; ticker: string; atr_pct?: number }> = [];
let _chartInit = false;
// Режим/окно теста из статуса (replay). График в iframe использует это, чтобы
// в тест-режиме грузить свечи за период реплея, а не «живые» последние бары.
let _embedMode = "sandbox";
let _embedReplayStart = "";
let _embedReplayEnd = "";
// Позиция, на которую сейчас смотрит график (клик по таблице позиций). При poll,
// если стоп/TP/trailing изменились — пересылаем обновлённый trade в iframe.
let _focusPos: { figi: string; ticker: string } | null = null;
let _lastFocusSig = "";

function botEmbedFrame(): HTMLIFrameElement | null {
  return document.getElementById("bot-chart-frame") as HTMLIFrameElement | null;
}
function botIso(s?: string | null): string | undefined {
  if (!s) return undefined;
  return s.includes("T") ? s : s.replace(" ", "T");
}
function sendEmbedFocus(figi: string, ticker: string, trade: Record<string, unknown> | null) {
  const f = botEmbedFrame();
  if (!f || !f.contentWindow) return;
  const hist = _lastTrades
    .filter((t) => t.figi === figi)
    .slice(-80)
    .map((t) => ({
      side: t.side,
      entry_time: botIso((t as { entry_time?: string }).entry_time ?? t.ts),
      ts: t.ts ? botIso(t.ts) : undefined,
      entry_price: (t as { entry_price?: number }).entry_price ?? t.price,
      exit_price: (t as { exit_price?: number }).exit_price,
      qty: t.qty,
      net_pnl: t.net_pnl,
    }));
  const msg = {
    type: "focus",
    figi,
    ticker,
    trade,
    trades: hist,
    mode: _embedMode,
    replay_start: _embedReplayStart,
    replay_end: _embedReplayEnd,
  };
  f.contentWindow.postMessage(msg, "*");
}

// Уведомляем iframe-график о смене контура/окна теста. Без активной сделки
// график должен перестать тянуть «живые» свечи и переключиться на период реплея.
function sendEmbedMode() {
  const f = botEmbedFrame();
  if (!f || !f.contentWindow) return;
  f.contentWindow.postMessage(
    { type: "mode", mode: _embedMode, replay_start: _embedReplayStart, replay_end: _embedReplayEnd },
    "*",
  );
}
function positionAsTrade(p: SandboxPositionRow): Record<string, unknown> {
  const t = p as unknown as { meta?: string; exit_meta?: string | null; entry_reason?: string | null };
  return {
    side: p.side, entry_time: botIso(p.entry_time), entry_price: p.entry_price,
    stop_loss: p.stop_loss, take_profit: p.take_profit,
    meta: t.meta, exit_meta: t.exit_meta, entry_reason: t.entry_reason,
  };
}
function tradeAsTrade(t: BotTradeRow): Record<string, unknown> {
  const e = t as unknown as { meta?: string; exit_meta?: string | null; entry_reason?: string | null; stop_loss?: number | null; take_profit?: number | null };
  return {
    side: t.side,
    entry_time: botIso((t as { entry_time?: string }).entry_time ?? t.ts),
    exit_time: t.ts ? botIso(t.ts) : undefined,
    entry_price: (t as { entry_price?: number }).entry_price ?? t.price,
    exit_price: (t as { exit_price?: number }).exit_price,
    stop_loss: e.stop_loss,
    take_profit: e.take_profit,
    net_pnl: t.net_pnl,
    meta: e.meta, exit_meta: e.exit_meta, entry_reason: e.entry_reason,
  };
}


function logKind(l: string): string {
  if (l.includes("TECHINFO")) return "tech";
  if (l.includes("СВЕЧА")) return "candles";
  if (l.includes("СИГНАЛ")) return "signals";
  if (l.includes("СДЕЛКА") || l.includes("ВЫХОД")) return "trades";
  return "events";
}

function renderLogs() {
  const el = $("bot-live-logs");
  if (!el) return;
  let rows = _allLogs.filter((l) => LGF[logKind(l) as keyof typeof LGF]);
  if (_logDateFilter) rows = rows.filter((l) => l.startsWith("[" + _logDateFilter));
  rows = rows.slice(-500);
  el.innerHTML = rows.length
    ? rows.map((l) => {
        let cls = "";
        if (l.includes("TECHINFO")) cls = ' class="lg-tech"';
        else if (l.includes("КВОРУМ")) cls = ' class="lg-warn"';
        else if (l.includes("ПРОПУСК") || l.includes("ошибк") || l.includes("ERROR")) cls = ' class="lg-err"';
        else if (l.includes("СИГНАЛ")) cls = ' class="lg-sig"';
        else if (l.includes("ВЫХОД") || l.includes("ЗАКРЫТИЕ")) cls = ' class="lg-exit"';
        else if (l.includes("СДЕЛКА")) cls = ' class="lg-fill"';
        else if (l.includes("СВЕЧА")) cls = ' class="lg-cnd"';
        return `<div${cls}>${l}</div>`;
      }).join("")
    : '<span style="color:#666">нет записей под фильтр</span>';
  const cnt = $("log-count");
  if (cnt) cnt.textContent = String(_allLogs.length);
  el.scrollTop = el.scrollHeight;
}

async function pollLogs() {
  const el = $("bot-live-logs");
  if (!el) return;
  try {
    const r = await fetch(`${API}/api/v1/bot/logs?limit=400`);
    if (!r.ok) return;
    const data = await r.json();
    _allLogs = data.logs || [];
    renderLogs();
  } catch {
    /* ignore */
  }
}

function setupLogFilters() {
  const setLG = (id: string, key: keyof typeof LGF) => {
    const cb = $(id) as HTMLInputElement | null;
    if (cb) cb.addEventListener("change", () => {
      LGF[key] = cb.checked;
      cb.closest("label")?.classList.toggle("on", cb.checked);
      localStorage.setItem("log_lgf", JSON.stringify(LGF));
      renderLogs();
      if (key === "candles") {
        void fetch(`${API}/api/v1/bot/logconfig`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ log_candles: cb.checked }),
        });
      }
    });
  };
  for (const [id, key] of [["lg-candles","candles"],["lg-signals","signals"],["lg-trades","trades"],["lg-events","events"],["lg-tech","tech"]]) {
    const cb = $(id) as HTMLInputElement | null;
    if (cb) { cb.checked = !!LGF[key]; cb.closest("label")?.classList.toggle("on", cb.checked); }
  }
  setLG("lg-candles", "candles");
  setLG("lg-signals", "signals");
  setLG("lg-trades", "trades");
  setLG("lg-events", "events");
  setLG("lg-tech", "tech");
  const dateInput = $("log-filter-date") as HTMLInputElement | null;
  if (dateInput) {
    dateInput.value = _logDateFilter;
    dateInput.addEventListener("change", () => {
      _logDateFilter = dateInput.value;
      localStorage.setItem("log_date_filter", _logDateFilter);
      renderLogs();
    });
  }
  const clear = $("lg-clear");
  if (clear) clear.addEventListener("click", () => {
    _allLogs = [];
    if (dateInput) { dateInput.value = ""; _logDateFilter = ""; localStorage.removeItem("log_date_filter"); }
    renderLogs();
    void fetch(`${API}/api/v1/bot/logs/clear`, { method: "POST" });
  });
}

async function doPause() {
  try {
    const r = await botPause(!entriesPaused);
    entriesPaused = r.entries_paused;
    updatePauseButton();
    await pollOnce();
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
  }
}

function updatePauseButton() {
  const btn = $("btn-bot-pause");
  btn.textContent = entriesPaused ? "▶ Снять паузу" : "⏸ Пауза входов";
  btn.classList.toggle("paused", entriesPaused);
}

async function doCancelPending() {
  try {
    await botCancelPending();
    await pollOnce();
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
  }
}

async function doCloseAll() {
  if (!confirm("Закрыть ВСЕ позиции бота по последней доступной цене?")) return;
  try {
    await botCloseAll();
    await pollOnce();
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
  }
}

function readParams(): Record<string, number> {
  const out: Record<string, number> = {};
  document
    .querySelectorAll<HTMLInputElement>("#bot-strategy-params input")
    .forEach((inp) => (out[inp.dataset.key!] = Number(inp.value)));
  return out;
}

function renderStrategyParams() {
  const card = catalog.find((c) => c.id === ($("bot-strategy") as HTMLSelectElement).value);
  const schema = card?.params_schema ?? {};
  $("bot-strategy-params").innerHTML = Object.entries(schema)
    .map(
      ([k, s]) =>
        `<label>${k}<input type="number" data-key="${k}" value="${s.default}" step="any" /></label>`
    )
    .join("");
}

function currentSessions(): string[] {
  const map: Record<string, string> = { "sg-morning": "morning", "sg-day": "day", "sg-evening": "evening" };
  const out = Object.keys(map).filter((id) => ($(id) as HTMLInputElement | null)?.checked).map((id) => map[id]);
  return out.length ? out : ["morning", "day", "evening"];
}

interface BotCfg { stop: number; target: number; topn: number; slMode: string; atrPeriod: number; atrMult: number; atrRr: number; commission: number; reentry: number; overnight: boolean; confirmFlip: number; ensembleQuorum: number; }

function loadBotCfg(): BotCfg {
  const d: BotCfg = { stop: 2.5, target: 2.5, topn: 20, slMode: "atr", atrPeriod: 14, atrMult: 4.0, atrRr: 4.0, commission: 0.3, reentry: 15, overnight: false, confirmFlip: 2, ensembleQuorum: 2 };
  try {
    const raw = localStorage.getItem("bot_cfg");
    if (raw) Object.assign(d, JSON.parse(raw));
  } catch { /* noop */ }
  return d;
}


async function fetchTradingStatus() {
  try {
    const resp = await fetch(`${API}/api/v1/bot/trading_status`);
    const data = await resp.json();
    const el = document.getElementById("bs-trading-status");
    if (el) {
      const session = data.session || "—";
      const status = data.status || "—";
      el.textContent = status;
      el.classList.remove("pos", "neg", "warn");
      if (session === "trading") el.classList.add("pos");
      else if (session === "pre_market" || session === "clearing") el.classList.add("warn");
      else el.classList.add("neg");
      el.title = "MOEX: " + status + " (" + session + ")";
    }
  } catch { /* noop */ }
}

function toggleSlSections(mode: string) {
  const atrSection = $("bs-atr-section");
  const fixedSection = $("bs-fixed-section");
  if (atrSection) atrSection.classList.toggle("bs-hidden", mode !== "atr");
  if (fixedSection) fixedSection.classList.toggle("bs-hidden", mode !== "fixed");
}

function initBotSettings() {
  void fetchTradingStatus();
  setInterval(() => void fetchTradingStatus(), 30000);

  const overlay = $("bot-modal-overlay");
  const btn = $("btn-bot-settings");
  if (!overlay || !btn) return;
  const close = () => overlay.classList.add("hidden");
  const open = () => {
    const cfg = loadBotCfg();
    ($("bs-sl") as HTMLInputElement).value = String(cfg.stop);
    ($("bs-tp") as HTMLInputElement).value = String(cfg.target);
    ($("bs-topn") as HTMLInputElement).value = String(cfg.topn);
    ($("bs-atr-period") as HTMLInputElement).value = String(cfg.atrPeriod);
    ($("bs-atr-mult") as HTMLInputElement).value = String(cfg.atrMult);
    ($("bs-atr-rr") as HTMLInputElement).value = String(cfg.atrRr);
    // Set radio button
  const radios = document.querySelectorAll('input[name="sl-mode"]') as NodeListOf<HTMLInputElement>;
    radios.forEach(r => { r.checked = r.value === cfg.slMode; });
    toggleSlSections(cfg.slMode);
    ($("bs-commission") as HTMLInputElement).value = String(cfg.commission);
    ($("bs-reentry") as HTMLInputElement).value = String(cfg.reentry);
    ($("bs-confirm-flip") as HTMLInputElement).value = String(cfg.confirmFlip);
    ($("bs-quorum") as HTMLInputElement).value = String(cfg.ensembleQuorum);
    overlay.classList.remove("hidden");
  };
  btn.addEventListener("click", open);
  const closeBtn = $("bs-close");
  if (closeBtn) closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
  const save = $("bs-save");
  if (save) save.addEventListener("click", () => {
    const sl = Number(($("bs-sl") as HTMLInputElement).value) || 2.5;
    const tp = Number(($("bs-tp") as HTMLInputElement).value) || 2.5;
    const topn = Math.max(2, Math.min(15, Number(($("bs-topn") as HTMLInputElement).value) || 6));
    const slMode = (document.querySelector('input[name="sl-mode"]:checked') as HTMLInputElement)?.value || "atr";
    const atrPeriod = Math.max(5, Number(($("bs-atr-period") as HTMLInputElement).value) || 14);
    const atrMult = Math.max(0.5, Number(($("bs-atr-mult") as HTMLInputElement).value) || 4.0);
    const atrRr = Math.max(0.5, Number(($("bs-atr-rr") as HTMLInputElement).value) || 4.0);
    const commission = Math.max(0, Number(($("bs-commission") as HTMLInputElement).value) || 0.3);
    const reentry = Math.max(0, Number(($("bs-reentry") as HTMLInputElement).value) || 15);
    const confirmFlip = Math.max(0, Number(($("bs-confirm-flip") as HTMLInputElement).value) || 2);
    const ensembleQuorum = Math.max(1, Number(($("bs-quorum") as HTMLInputElement).value) || 2);
    localStorage.setItem("bot_cfg", JSON.stringify({ stop: sl, target: tp, topn, slMode, atrPeriod, atrMult, atrRr, commission, reentry, confirmFlip, ensembleQuorum }));
    sendBotConfigPatch({
      stop_pct: sl / 100,
      target_pct: tp / 100,
      top_n: topn,
      sl_mode: slMode,
      atr_period: atrPeriod,
      atr_multiplier: atrMult,
      atr_risk_reward: atrRr,
      commission_rate: commission,
      reentry_cooldown_bars: reentry,
      overnight: ($("sg-overnight") as HTMLInputElement | null)?.checked ?? false,
      confirm_flip: confirmFlip,
      ensemble_quorum: ensembleQuorum,
    });
    close();
  });
  // Radio buttons for SL mode
  const radios = document.querySelectorAll('input[name="sl-mode"]') as NodeListOf<HTMLInputElement>;
  radios.forEach(r => r.addEventListener("change", () => toggleSlSections(r.value)));
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
}

async function doStart() {
  try {
    $("btn-bot-start").classList.add("busy");
    const cfg = loadBotCfg();
    await botStart({
      strategy_id: "ensemble_v4",
      params: {},
      interval_name: "5min",
      top_n: cfg.topn,
      commission_rate: cfg.commission,
      reentry_cooldown_bars: cfg.reentry,
      overnight: ($("sg-overnight") as HTMLInputElement | null)?.checked ?? cfg.overnight,
      qty_per_trade: 1,
      stop_pct: cfg.stop / 100,
      target_pct: cfg.target / 100,
      sl_mode: cfg.slMode,
      atr_period: cfg.atrPeriod,
      atr_multiplier: cfg.atrMult,
      atr_risk_reward: cfg.atrRr,
      allow_short: false,
      initial_cash: 10000,
      mode: "sandbox",
      use_ensemble: true,
      ensemble_capital: 0,
      ensemble_quorum: 2,
      ensemble_session: "main",
      sessions: currentSessions(),
      leverage: ($("sg-lev") as HTMLInputElement | null)?.checked ? 2 : 1,
      long_allowed: ($("dg-long") as HTMLInputElement | null)?.checked ?? true,
      short_allowed: ($("dg-short") as HTMLInputElement | null)?.checked ?? false,
    });
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    $("btn-bot-start").classList.remove("busy");
    await pollOnce();
  }
}

async function doStop() {
  try {
    await botStop();
  } finally {
    await pollOnce();
  }
}

async function doToggleMode(mode: "live" | "sandbox" | "test") {
  // Тест запускается через модалку (имя + период реплея).
  if (mode === "test") {
    openTestModal();
    return;
  }
  const warn = mode === "live"
    ? "Переключить на LIVE (реальные деньги)?\nБот будет ОСТАНОВЛЕН и перезапущен на боевом счёте."
    : "Переключить на SANDBOX (тестовый счёт)?\nБот будет остановлен и перезапущен.";
  if (!confirm(warn)) return;
  const btn = document.querySelector(`#mode-switch .mode-btn[data-mode="${mode}"]`) as HTMLButtonElement | null;
  if (btn) btn.classList.add("busy");
  try {
    await botSetMode(mode);
    _curMode = mode;
    _setActiveModeBtn(mode);
  } catch (e) {
    alert("Ошибка переключения: " + (e instanceof Error ? e.message : String(e)));
  } finally {
    if (btn) btn.classList.remove("busy");
    await pollOnce();
  }
}

function _setActiveModeBtn(mode: string) {
  document.querySelectorAll("#mode-switch .mode-btn").forEach((b) => {
    const m = (b as HTMLElement).dataset.mode;
    b.classList.toggle("active", m === mode);
  });
}

function openTestModal() {
  const overlay = $("test-modal-overlay");
  if (!overlay) return;
  // Дефолты: конец = сейчас, начало = 7 дней назад, локальное время (datetime-local).
  const start = $("ts-start") as HTMLInputElement | null;
  const end = $("ts-end") as HTMLInputElement | null;
  const now = new Date();
  if (end) end.value = fmtLocalDT(now);
  if (start) {
    const s = new Date(now.getTime() - 7 * 86400000);
    start.value = fmtLocalDT(s);
  }
  overlay.classList.remove("hidden");
}

function fmtLocalDT(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function toUTCISO(v: string): string {
  if (!v) return "";
  try { return new Date(v).toISOString(); } catch { return ""; }
}

function closeTestModal() {
  $("test-modal-overlay")?.classList.add("hidden");
}

async function doRunTest() {
  const name = ($("ts-name") as HTMLInputElement)?.value.trim() ?? "";
  const start = toUTCISO(($("ts-start") as HTMLInputElement)?.value ?? "");
  const end = toUTCISO(($("ts-end") as HTMLInputElement)?.value ?? "");
  if (!name || !start) {
    alert('Укажите название теста и начало периода.');
    return;
  }
  const btn = $("ts-run") as HTMLButtonElement | null;
  if (btn) { btn.disabled = true; btn.textContent = "Запускаю…"; }
  try {
    await botSetMode("test", { test_name: name, replay_start: start, replay_end: end });
    _curMode = "test";
    _setActiveModeBtn("test");
    closeTestModal();
  } catch (e) {
    alert("Ошибка запуска теста: " + (e instanceof Error ? e.message : String(e)));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Запустить"; }
  }
}

async function doReset() {
  try {
    await botReset(10000);
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    await pollOnce();
  }
}


export async function pollOnce(onStateChange?: (running: boolean) => void) {
  const pillTop = $("bot-pill-top");
  if (pillTop) {
    pillTop.classList.remove("on", "off");
    pillTop.classList.add("warn");
  }

  const bst = await (async () => {
    try {
      const r = await fetch(`${API}/api/v1/bot/status`);
      return r.ok ? await r.json() : null;
    } catch {
      return null;
    }
  })();

  const backendAlive = !!bst;
  const engineRunning = bst ? !!bst.running : false;
  const botErr = bst && bst.error ? bst.error : null;
  if (bst && typeof bst.entries_paused === "boolean") entriesPaused = bst.entries_paused;

  if (pillTop) {
    pillTop.classList.remove("warn");
    pillTop.classList.toggle("on", engineRunning);
    pillTop.classList.toggle("off", !backendAlive);
  }
  $("bot-state-text").textContent =
    botErr ? `Бот ошибка: ${botErr.slice(0, 60)}`
      : engineRunning ? `Бот работает · ${bst.mode}`
      : backendAlive ? "Бот остановлен" : "Бот недоступен";

  const hdrBotState = $("hdr-bot-state");
  if (hdrBotState) {
    hdrBotState.textContent =
      botErr ? "Ошибка"
        : engineRunning ? "Бот активен"
        : backendAlive ? "Бот остановлен" : "Бот недоступен";
  }

  // IMOEX guard: всплеск индекса (блокировки против входа) + свежесть свечей MOEX.
  const igEl = $("bs-imoex-guard");  const igText = $("bs-imoex-text");
  if (igEl && igText) {
    const ig = bst ? bst.imoex_guard : null;
    igEl.classList.remove("on", "off");
    igText.classList.remove("pos", "neg", "warn");
    let igTxt = "—";
    let igTitle = "IMOEX guard: нет данных";
    const _d5 = ig?.dir_pct_5m != null ? Number(ig.dir_pct_5m) : null;
    const _d20 = ig?.dir_pct_20m != null ? Number(ig.dir_pct_20m) : 0;
    const _br = (bst as any)?.breadth;
    const _brTxt = _br && _br.up_pct != null
      ? ` · объём: ↑${Number(_br.up_pct).toFixed(0)}% / ↓${(100 - Number(_br.up_pct)).toFixed(0)}%`
      : "";
    if (!ig) {
      igEl.classList.add("off");
    } else if (!ig.enabled) {
      igTxt = "выкл";
      igEl.classList.add("off");
      igTitle = "IMOEX guard выключен (imoex_guard=false)";
    } else if (ig.stale) {
      igTxt = "НЕТ СВЕЧЕЙ";
      igText.classList.add("neg");
      const ageMin = ig.age_sec != null ? Math.round(Number(ig.age_sec) / 60) : null;
      igTitle = `Свечи IMOEX не обновляются! ${ig.last_candle ? "последняя: " + ig.last_candle : ""}`
        + (ageMin != null ? ` (${ageMin} мин назад)` : "")
        + " — входы против направления индекса НЕ защищены";
    } else if (ig.active > 0) {
      igTxt = `↑${Number(ig.pct).toFixed(2)}% блок SELL`;
      igText.classList.add("neg");
      igTitle = `Всплеск IMOEX ВВЕРХ — SELL-входы запрещены (блокировок: ${ig.blocks ?? 0})`
        + ` · 20м ${_d20 >= 0 ? "+" : ""}${_d20.toFixed(2)}%${_brTxt}`;
    } else if (ig.active < 0) {
      igTxt = `↓${Number(ig.pct).toFixed(2)}% блок BUY`;
      igText.classList.add("neg");
      igTitle = `Всплеск IMOEX ВНИЗ — BUY-входы запрещены (блокировок: ${ig.blocks ?? 0})`
        + ` · 20м ${_d20 >= 0 ? "+" : ""}${_d20.toFixed(2)}%${_brTxt}`;
    } else {
      const arrow = _d20 > 0.05 ? "↑" : _d20 < -0.05 ? "↓" : "→";
      igTxt = `${arrow}${_d20 >= 0 ? "+" : ""}${_d20.toFixed(2)}%`;
      igText.classList.add(_d20 > 0.05 ? "pos" : _d20 < -0.05 ? "neg" : "warn");
      const ageMin = ig.age_sec != null ? Math.round(Number(ig.age_sec) / 60) : null;
      igTitle = `IMOEX ${arrow} ${_d20 >= 0 ? "+" : ""}${_d20.toFixed(2)}% за 20м`
        + (_d5 != null ? ` · 5м ${_d5 >= 0 ? "+" : ""}${_d5.toFixed(2)}%` : "")
        + _brTxt
        + (_br && _br.up_n != null ? ` (бумаг: ↑${_br.up_n} / ↓${_br.down_n})` : "")
        + ` · свеча: ${ig.last_candle ? new Date(ig.last_candle).toLocaleTimeString("ru-RU", { hour12: false }) : "—"}`
        + (ageMin != null ? ` (${ageMin} мин)` : "")
        + ` · блокировок: ${ig.blocks ?? 0}`;
    }
    if (ig && ig.enabled && !ig.stale && ig.active === 0) {
      igTitle += ` · порог ${ig.on_pct}%/${ig.window_min}м`
        + (ig.chase_pct ? ` · chase ≥${ig.chase_pct}%` : "")
        + (ig.min_beta ? ` · beta ≥${ig.min_beta}` : "");
    }
    igText.textContent = igTxt;
    igEl.title = igTitle;
  }

  // AI-гейт (4-я вкладка правого сайдбара): обновляем, если открыта.
  if (!$("sr-pane-aigate")?.classList.contains("hidden")) void renderAiGate();

  $("btn-bot-start").classList.toggle("hidden", engineRunning);
  $("btn-bot-stop").classList.toggle("hidden", !engineRunning);
  $("btn-bot-pause").classList.toggle("hidden", !engineRunning);
  updatePauseButton();

  const modeChip = $("bot-mode");
  if (bst && bst.running && bst.mode !== "—") {
    modeChip.textContent = bst.mode;
    modeChip.classList.remove("hidden");
  } else {
    modeChip.classList.add("hidden");
  }

  const envBadge = $("mode-switch");
  if (envBadge) {
    const em = bst && bst.config && (bst.config as { mode?: string }).mode;
    const mm = String(em || "").toLowerCase();
    const tn = (bst?.config as any)?.test_name;
    let newMode = "sandbox";
    if (mm === "live") newMode = "live";
    else if (mm === "test") newMode = "test";
    else newMode = "sandbox";
    const newStart = String((bst?.config as any)?.replay_start ?? "");
    const newEnd = String((bst?.config as any)?.replay_end ?? "");
    const modeOrWindowChanged =
      _curMode !== newMode || _embedReplayStart !== newStart || _embedReplayEnd !== newEnd;
    _curMode = newMode;
    _embedMode = newMode;
    _embedReplayStart = newStart;
    _embedReplayEnd = newEnd;
    _setActiveModeBtn(_curMode);
    if (modeOrWindowChanged) sendEmbedMode();
    const testChip = $("bot-mode-test");
    if (testChip && mm === "test") {
      testChip.textContent = tn ? `Тест: ${tn}` : "Тест";
      testChip.classList.remove("hidden");
    } else if (testChip) {
      testChip.classList.add("hidden");
    }
  }
  const sessEl = $("bs-session");
  if (sessEl) {
    const s = bst && bst.session ? bst.session : "—";
    sessEl.textContent = s;
    sessEl.classList.remove("pos", "neg");
    if (s === "TRADING") sessEl.classList.add("pos");
    else if (s === "OPENING" || s === "EVENING") sessEl.classList.add("pos");
  }

  // Синхронизация чекбоксов «Маржа» с сервером (margin_sessions из конфига бота).
  const mgMap: Record<string, string> = { morning: "mg-morning", day: "mg-day", evening: "mg-evening" };
  const ms = (bst && bst.config && (bst.config as { margin_sessions?: string[] }).margin_sessions) || null;
  if (Array.isArray(ms)) {
    for (const [s, id] of Object.entries(mgMap)) {
      const cb = $(id) as HTMLInputElement | null;
      if (!cb) continue;
      const on = ms.includes(s);
      if (cb.checked !== on) {
        cb.checked = on;
        cb.closest("label")?.classList.toggle("on", on);
      }
    }
  }
  // Синхронизация чекбокса «Overnight» с сервером.
  const ovCfg = (bst && bst.config && (bst.config as { overnight?: boolean }).overnight);
  const ovElSync = $("sg-overnight") as HTMLInputElement | null;
  if (ovElSync && typeof ovCfg === "boolean") {
    if (ovElSync.checked !== ovCfg) {
      ovElSync.checked = ovCfg;
      ovElSync.closest("label")?.classList.toggle("on", ovCfg);
    }
  }
  // Синхронизация ползунка плеча маржи с сервером.
  const mlv = (bst && bst.config && (bst.config as { margin_leverage?: number }).margin_leverage);
  const levElSync = $("mg-lev") as HTMLInputElement | null;
  if (levElSync && typeof mlv === "number") {
    const idx = levValueToSlider(mlv);
    if (Number(levElSync.value) !== idx) {
      levElSync.value = String(idx);
      const lbl = $("mg-lev-label");
      if (lbl) lbl.textContent = levLabel(mlv);
      levPaint(idx);
    }
  }
  // Синхронизация режимов рынка с сервером.
  const trMap: Record<string, string> = {
    NEUTRAL: "rg-neutral", TREND_UP: "rg-trendup", TREND_DOWN: "rg-trenddown",
    HIGH_VOLATILITY: "rg-highvol", RANGE: "rg-range",
  };
  const tr = (bst && bst.config && (bst.config as { trade_regimes?: string[] }).trade_regimes) || null;
  if (Array.isArray(tr)) {
    for (const [k, id] of Object.entries(trMap)) {
      const cb = $(id) as HTMLInputElement | null;
      if (!cb) continue;
      const on = tr.includes(k);
      if (cb.checked !== on) {
        cb.checked = on;
        cb.closest("label")?.classList.toggle("on", on);
      }
    }
  }
  // Вкладка «Голоса»: тикеры юниверса + число голосов на последнем 5m баре.
  _lastVotes = ((bst as { votes?: Array<{ figi: string; ticker: string; buy: number; sell: number; votes: number; side: string; regime?: string | null; vol?: number | null; vol_abs?: number | null }> } | null)?.votes) || [];
  _lastUniverse = ((bst as { universe?: Array<{ figi: string; ticker: string; atr_pct?: number }> } | null)?.universe) || [];
  renderVotes(_lastVotes, _lastUniverse);

  if (lastRunning !== engineRunning) {
    lastRunning = engineRunning;
    onStateChange?.(engineRunning);
  }

  let st: any = null;
  try {
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), 5000);
    st = await sandboxStatus(ctrl.signal);
    clearTimeout(tid);
  } catch {
    st = { running: false, portfolio: { equity: 0, pnl: 0, positions_open: 0, cash: 0, initial_cash: 10000 } };
  }

  const eqChip = $("equity-chip");
  if (eqChip) eqChip.textContent = `${money(st.portfolio.equity)} ₽`;
  const bsCash = $("bs-cash");
  if (bsCash) {
    const ff = (st.portfolio as { free_funds?: number }).free_funds;
    bsCash.textContent = `${money(ff != null ? ff : st.portfolio.cash)} ₽`;
    bsCash.title = "Свободные средства = ликвидный портфель − начальная маржа (реально доступно для сделок)";
  }
  const bsEq = $("bs-equity");
  if (bsEq) bsEq.textContent = `${money(st.portfolio.equity)} ₽`;
  const pnlEl = $("bs-pnl");
  if (pnlEl) {
    pnlEl.textContent = `${st.portfolio.pnl >= 0 ? "+" : ""}${money(st.portfolio.pnl)} ₽`;
    pnlEl.className = "stat-v " + (st.portfolio.pnl >= 0 ? "pos" : "neg");
  }
  const bsCur = $("bs-currencies");
  if (bsCur) bsCur.textContent = st.portfolio.tinkoff_currencies != null ? `${money(st.portfolio.tinkoff_currencies)} ₽` : "—";
  const bsShares = $("bs-shares");
  if (bsShares) {
    const sh = st.portfolio.tinkoff_shares ?? 0;
    bsShares.textContent = `${sh < 0 ? "−" : sh > 0 ? "+" : ""}${money(Math.abs(sh))} ₽`;
    bsShares.className = sh < 0 ? "neg" : sh > 0 ? "pos" : "";
  }

  if (!$("page-bot")) return;

  try {
  const positions: SandboxPositionRow[] = await sandboxPositions().catch(() => []);
  _lastPositions = positions;
  // График смотрит на открытую позицию — при изменении SL/TP/trailing пересылаем
  // обновлённый trade в iframe, чтобы линия SL двигалась за трейлингом.
  if (_focusPos) {
    const fp = positions.find((x) => x.figi === _focusPos!.figi);
    if (fp) {
      const sig = `${fp.side}|${fp.entry_price}|${fp.stop_loss}|${fp.take_profit}|${fp.trail_active ? 1 : 0}`;
      if (sig !== _lastFocusSig) {
        _lastFocusSig = sig;
        sendEmbedFocus(fp.figi, fp.ticker, positionAsTrade(fp));
      }
    }
  }
  const totalOwn = positions.reduce((s, p) => s + p.entry_price * p.qty / (p.leverage || 1), 0);
  const bsPosVal = $("bs-positions-value");
  const ownPos = (st?.portfolio as any)?.own_in_positions;
  const startMargin = (st?.portfolio as { starting_margin?: number })?.starting_margin;
  if (bsPosVal) {
    bsPosVal.textContent = `${money(startMargin != null ? startMargin : (ownPos != null ? ownPos : totalOwn))} ₽`;
    bsPosVal.title = "Обеспечение под позиции (начальная маржа, заморожено брокером)";
  }
  _lastPositions = positions;
  document.querySelector("#bot-positions-table tbody")!.innerHTML =
    positions
      .map((p) => {
        const trend = priceTrend(p.current_price, p.prev_close);
        const closeAction = p.side.toUpperCase() === "LONG" ? "Продать" : "Купить";
        const closeClass = p.side.toUpperCase() === "LONG" ? "btn-sell" : "btn-buy";
        const lev = p.leverage || 1;
        const notional = p.notional ?? (p.entry_price * p.qty);
        const own = p.own_money ?? (notional / lev);
        const pnl = p.unrealized_pnl ?? ((p.current_price - p.entry_price) * p.qty);
        const roi = own > 0 ? (pnl / own * 100) : 0;
        const reg = p.regime || "";
        const regColor = reg === "TREND_UP" ? "#2ecc71" : reg === "TREND_DOWN" ? "#e74c3c" : reg === "HIGH_VOLATILITY" ? "#f39c12" : reg === "RANGE" ? "#3498db" : "var(--text-dim)";
        const regLabel: Record<string, string> = {
          HIGH_VOLATILITY: "HV", TREND_UP: "Trend↑", TREND_DOWN: "Trend↓",
          RANGE: "Range", NEUTRAL: "Neutral",
        };
        const regChip = reg ? `<span style="font-size:10px;color:${regColor};font-weight:600">${regLabel[reg] || reg}</span>` : "—";
        // Подсветка SL/TP, если цена ближе 1.5 ATR к уровню.
        const nearSl = p.dist_sl_atr != null && p.dist_sl_atr <= 1.5;
        const nearTp = p.dist_tp_atr != null && p.dist_tp_atr <= 1.5;
        const atrTitle = (v2: number | null | undefined, lbl: string) =>
          v2 != null ? `${lbl}: ${v2.toFixed(2)} ATR до уровня` : lbl;
        const v = p.vol;
        // Стрелка направления объёма относительно среднего: ×≥1 — повышенный (↑), <1 — пониженный (↓).
        const vArrow = v != null ? (v >= 1 ? "↑" : "↓") : "";
        const volCell = v != null
          ? `<span style="color:${vArrow === "↑" ? "var(--up)" : "var(--down)"}">${vArrow}</span>×${v.toFixed(2)}`
          : "—";
        return `<tr>` +
          `<td class="num" style="font-size:10px;color:var(--text-dim)">${fmtTime(p.entry_time)}</td>` +
          `<td>${tickerLogo(p.ticker)}<b>${p.ticker}</b></td>` +
          `<td>${sideIcon(p.side)}</td>` +
          `<td class="num">${p.qty}</td>` +
          `<td class="num">${price(p.entry_price)}</td>` +
          `<td class="num dim">${money(notional)}₽ <span class="dim">(${money(own)}₽ обесп.${p.risk_rate ? `, риск ${(p.risk_rate * 100).toFixed(1)}%` : ""})</span></td>` +
          `<td class="num${trend}">${price(p.current_price)}</td>` +
          `<td class="num ${pnl >= 0 ? "pos" : "neg"}" title="gross (без комиссий)${p.net_pnl_est != null ? " · net ≈ " + money(p.net_pnl_est) + "₽ (с комиссией)" : ""}">${pnl >= 0 ? "+" : ""}${money(pnl)}₽${p.net_pnl_est != null ? `<span class="dim" style="font-size:9px"> / ${p.net_pnl_est >= 0 ? "+" : ""}${money(p.net_pnl_est)}₽</span>` : ""}</td>` +
          `<td class="num ${roi >= 0 ? "pos" : "neg"}">${roi >= 0 ? "+" : ""}${roi.toFixed(1)}%</td>` +
          `<td class="num">×${lev}</td>` +
          `<td class="num" title="${p.regime_reason || ""}${p.regime_atr_pct != null ? " | ATR " + p.regime_atr_pct + "%" : ""}${p.regime_adx != null ? " | ADX " + p.regime_adx : ""}">${regChip}</td>` +
          `<td class="num${p.vol != null && p.vol >= 1 ? " pos" : ""}" title="Vol = объём бара / средний объём (×50 бар). ↑ — выше среднего, ↓ — ниже">${volCell}</td>` +
          `<td class="num${nearSl ? " near-sl" : ""}" style="color:${p.trail_active ? "#f39c12" : "var(--text-dim)"}" title="${atrTitle(p.dist_sl_atr, "SL")}${p.trail_active ? " · трейлинг активен" : " · статичный SL"}">${p.stop_loss != null ? Number(p.stop_loss).toFixed(4) : "—"}${p.trail_active ? " ◆" : ""}</td>` +
          `<td class="num${nearTp ? " near-tp" : ""}" title="${atrTitle(p.dist_tp_atr, "TP")}${p.trail_active ? " · P&L при выходе по стопу" : ""}">${p.trail_active && p.stop_loss != null
              ? `<span style="color:#3b82f6">${pnlAtStop(p) >= 0 ? "+" : ""}${money(pnlAtStop(p))}₽</span>`
              : p.take_profit != null ? price(p.take_profit) : "—"}</td>` +
          `<td><button class="btn-sm ${closeClass}" onclick="window.__closePosition('${p.ticker}','${p.side}',${p.qty})">${closeAction}</button></td>` +
          `</tr>`;
      })
      .join("") || `<tr><td colspan=15 style="color:var(--text-dim)">нет открытых позиций</td></tr>`;

  const trades: BotTradeRow[] = await sandboxTrades(500).catch(() => []);
  _lastTrades = trades;
  renderTrades(trades);
  if (!_chartInit && positions.length > 0) {
    _chartInit = true;
    const first = positions[0];
    _focusPos = { figi: first.figi, ticker: first.ticker };
    sendEmbedFocus(first.figi, first.ticker, positionAsTrade(first));
  }
  } catch (err) {
    console.error("render", err);
    const ps = $("portfolio-summary");
    if (ps) ps.innerHTML = `<div class="mini-hint" style="color:#ff5252">ошибка рендера: ${String(err)}</div>`;
  }
  hideEmptyTables();
}

async function pollEvents() {
  if (!$("page-bot").classList.contains("active")) return;
  let evs: BotEventRow[];
  try {
    evs = await botEvents(80);
  } catch {
    return;
  }
  const box = $("bot-events");
  if (!evs.length) {
    box.innerHTML = `<div class="mini-hint">событий пока нет</div>`;
    return;
  }
  box.innerHTML = evs
    .map((e) => {
      const t = fmtTimeOnly(String(e.ts));
      const side = String((e.payload as { side?: string } | null)?.side ?? "");
      const pnl = (e.payload as { net_pnl?: number } | null)?.net_pnl;
      const cls =
        e.type.includes("REJECTED") || e.type.includes("ERROR") || e.type.includes("BREAKER")
          ? "ev-err"
          : e.type === "ORDER_FILLED"
            ? side === "SELL" || e.payload?.action === "close" ? "ev-sell" : "ev-buy"
            : e.type === "POSITION_OPENED"
              ? String(e.payload?.side) === "SHORT" ? "ev-sell" : "ev-buy"
              : e.type.startsWith("PAUSE") || e.type.includes("CANCEL") ? "ev-warn" : "ev-info";
      const pnlTxt = pnl != null ? ` · P&L ${pnl >= 0 ? "+" : ""}${pnl}` : "";
      return `<div class="ev-line ${cls}"><span class="ev-ts">${t}</span>` +
        `<b>${e.type}</b>${e.ticker ? ` ${e.ticker}` : ""}${side ? ` ${side}` : ""}` +
        `${e.reason ? ` · ${e.reason}` : ""}${pnlTxt}</div>`;
    })
    .join("");
}


function hideEmptyTables() {
  document.querySelectorAll("table.runs-table").forEach((t) => {
    const tb = t.querySelector("tbody");
    if (!tb) return;
    const empty = tb.children.length === 0;
    (t as HTMLElement).style.display = empty ? "none" : "";
  });
  document.querySelectorAll(".pages .card").forEach((c) => {
    const el = c as HTMLElement;
    if (el.id === "portfolio-summary" || el.id === "bot-events") return;
    const visibleKids = Array.from(el.children).filter((ch) => {
      const h = ch as HTMLElement;
      return h.style.display !== "none" && (h.offsetParent !== null || h.style.position === "fixed");
    });
    if (el.children.length === 0) el.style.display = "none";
    else if (visibleKids.length === 0) el.style.display = "none";
    else el.style.display = "";
  });
}

function renderTrades(trades: BotTradeRow[]) {
  document.querySelector("#bot-trades-table tbody")!.innerHTML =
    trades
      .map(
        (t, i) => {
          const isOpen = !t.ts || t.exit_reason === "на торгах" || t.exit_price == null;
          const inT = fmtTime(t.entry_time ?? t.ts);
          const timeCell = isOpen
            ? `${inT} · <b style="color:#ffd740">на торгах</b>`
            : `${inT}${t.ts && t.ts !== t.entry_time ? " → " + fmtTime(t.ts) : ""}`;
          const pxCell = isOpen
            ? `${price(t.entry_price ?? t.price)} → <b style="color:#ffd740">на торгах</b>`
            : `${price(t.entry_price ?? t.price)}${t.exit_price ? " → " + price(t.exit_price) : ""}`;
          const pnlCell = isOpen
            ? `<b style="color:#ffd740">на торгах</b>`
            : `<b class="${t.net_pnl >= 0 ? "pos" : "neg"}">${t.net_pnl >= 0 ? "+" : ""}${money(t.net_pnl)}</b>`;
          // Чип причины выхода: stop → красный, открытая → жёлтый, иное → серый.
          let reasonChip: string;
          if (isOpen) {
            reasonChip = `<span class="exit-chip pending">Открыта · ждём выхода</span>`;
          } else if (t.exit_reason === "stop_loss") {
            reasonChip = `<span class="exit-chip stop">STOP LOSS${t.stop_loss != null ? ` <i>· SL ${price(t.stop_loss)}</i>` : ""}</span>`;
          } else {
            reasonChip = `<span class="exit-chip">${t.exit_reason || "—"}</span>`;
          }
          // Метка bias из meta.entry.against_bias (пишется в ensemble_strategy).
          let biasTag = "";
          try {
            const m = t.meta ? JSON.parse(t.meta) : null;
            const ab = m?.entry?.against_bias;
            if (ab === true) biasTag = ` <span class="warn-bias">△ против bias</span>`;
            else if (ab === false) biasTag = ` <span class="ok-bias">✓ по bias</span>`;
          } catch { /* noop */ }
          // Бейдж режима (если есть в meta).
          let regimeBadge = "";
          try {
            const m = t.meta ? JSON.parse(t.meta) : null;
            const rg = String(m?.regime ?? "").replace("HIGH_VOLATILITY", "HV");
            if (rg) regimeBadge = ` <span class="rg-badge">${rg}</span>`;
          } catch { /* noop */ }
          // R-множитель = net_pnl / риск (|entry−SL|×qty).
          let rVal = ""; let rCls = "";
          if (!isOpen && t.stop_loss != null && t.entry_price && t.stop_loss !== t.entry_price) {
            const risk = Math.abs(t.entry_price - t.stop_loss) * t.qty;
            if (risk > 0) {
              const r = t.net_pnl / risk;
              rVal = `${r >= 0 ? "+" : ""}${r.toFixed(1)}`;
              rCls = r >= 0 ? "pos" : "neg";
            }
          }
          // Вход (голоса ансамбля) из meta.entry.reason/features.
          let entryCell = "";
          try {
            const m = t.meta ? JSON.parse(t.meta) : null;
            const er = String(m?.entry?.reason ?? "");
            const f = (m?.entry?.features || {}) as Record<string, unknown>;
            if (er) {
              let votes = "";
              if (typeof f.long_votes === "number" || typeof f.short_votes === "number") {
                votes = ` ↑${f.long_votes ?? 0}/↓${f.short_votes ?? 0}`;
              } else if (typeof f.votes_up === "number" || typeof f.votes_dn === "number") {
                votes = ` ↑${f.votes_up ?? 0}/↓${f.votes_dn ?? 0}`;
              } else if (typeof f.votes === "number") {
                votes = ` v${f.votes}`;
              }
              const up: string[] = []; const dn: string[] = [];
              for (const [k, v] of Object.entries(f)) {
                if (k.startsWith("m1_") || k.startsWith("m5_")) {
                  const nm = k.replace(/^m[15]_/, "");
                  if (v === "BUY") up.push(nm); else if (v === "SELL") dn.push(nm);
                }
              }
              const tip = (up.length || dn.length) ? `↑ ${up.join(",") || "—"} | ↓ ${dn.join(",") || "—"}` : er;
              const short = er.replace("_ensemble", "").replace("neutral_", "neu:").replace("range_", "rng:").replace("hv_", "hv:");
              entryCell = `<span class="entry-chip" title="${esc(tip)}">${esc(short)}${votes}</span>`;
            }
          } catch { /* noop */ }
          const selected = _openDetails.has(`${t.ticker}|${t.entry_time}`);
          return `<tr class="trade-row${selected ? " selected" : ""}" data-idx="${i}" data-key="${t.ticker}|${t.entry_time}" style="cursor:pointer">` +
            `<td class="td-time">${timeCell}</td>` +
            `<td>${tickerLogo(t.ticker)}<b>${t.ticker}</b>${regimeBadge}${biasTag}</td>` +
            `<td>${sideIcon(t.side)}</td>` +
            `<td class="num">${t.qty}</td>` +
            `<td class="num td-px">${pxCell}</td>` +
            `<td class="num">${pnlCell}</td>` +
            `<td class="num" style="color:var(--text-dim)">${!isOpen && t.commission != null ? money(t.commission) : "—"}</td>` +
            `<td>${reasonChip}</td>` +
            `<td>${entryCell || "—"}</td>` +
            `<td class="num ${rCls}">${rVal || "—"}</td>` +
            `<td class="td-arrow" title="раскрыть детали">${selected ? "▲" : "▼"}</td>` +
            `</tr>` + (selected ? tradeDetailsHtml(t) : "");
        }
      )
      .join("") || `<tr><td colspan=11 style="color:var(--text-dim)">пока нет сделок</td></tr>`;
}

// Раскрытие деталей сделки: состав ансамбля, голоса кворума, bias и entry при входе.
function tradeDetailsHtml(t: BotTradeRow): string {
  let m: Record<string, unknown> | null = null;
  try { m = t.meta ? JSON.parse(t.meta) : null; } catch { m = null; }
  const entry = (m?.entry as Record<string, unknown>) || {};
  const qe = (m?.quorum_event as Record<string, unknown>) || {};
  const setups = (m?.setups as Record<string, Record<string, unknown>>) || {};
  const vol = (m?.volume as Record<string, number>) || {};
  const regNow = m?.regime as string | undefined;
  const ab = entry.against_bias;
  const chip = (txt: string, col: string) =>
    `<span style="display:inline-block;padding:1px 7px;margin:2px;border-radius:6px;border:1px solid ${col};color:${col};font-size:10px;font-weight:700">${txt}</span>`;
  const fnum = (x: number | undefined) => (x == null ? "—" : new Intl.NumberFormat("ru-RU").format(Math.round(x)));
  const rows: string[] = [];
  // Entry
  rows.push(`<div style="margin:2px 0"><b style="color:var(--text-dim)">Вход:</b> ${String(t.side)} · ${String(entry.reason ?? "—")}` +
    (ab === true ? ` ${chip("ПРОТИВ bias", "#e74c3c")}` : ab === false ? ` ${chip("по bias", "#2ecc71")}` : "") +
    (entry.features ? ` · level ${(entry.features as Record<string, unknown>).breakout_level ?? "—"}` : "") + `</div>`);
  // Голоса функций (ensemble_vote): кто ↑ / кто ↓
  try {
    const f = (entry.features as Record<string, unknown>) || {};
    const up: string[] = [];
    const dn: string[] = [];
    for (const [k, v] of Object.entries(f)) {
      if (k.startsWith("m1_") || k.startsWith("m5_")) {
        const nm = k.replace(/^m[15]_/, "");
        if (v === "BUY") up.push(nm);
        else if (v === "SELL") dn.push(nm);
      }
    }
    if (up.length || dn.length) {
      rows.push(`<div style="margin:2px 0"><b style="color:var(--text-dim)">Голоса:</b> ` +
        `<span style="color:var(--up)">↑ ${up.join(", ") || "—"}</span> &nbsp;` +
        `<span style="color:var(--down)">↓ ${dn.join(", ") || "—"}</span></div>`);
    }
  } catch { /* noop */ }
  // Объём и режим на входе
  if (vol.v != null) {
    rows.push(`<div style="margin:2px 0"><b style="color:var(--text-dim)">Объём (вход):</b> ` +
      `${fnum(vol.v)} <span style="color:var(--text-dim)">(max ${fnum(vol.max)}, min ${fnum(vol.min)})</span>` +
      ` · <b style="color:var(--text-dim)">Режим:</b> ${regNow || "—"}</div>`);
  }
  // Quorum
  if (Object.keys(qe).length) {
    rows.push(`<div style="margin:2px 0"><b style="color:var(--text-dim)">Кворум:</b> ${String(qe.side ?? "")} · голосов ${String(qe.votes ?? "?")}/${String(qe.total_members ?? "?")} (k=${String(qe.quorum_k ?? "?")}) · BUY ${String(qe.buy_votes ?? 0)} / SELL ${String(qe.sell_votes ?? 0)}</div>`);
    const mf = (qe.members_for as string[]) || [];
    const op = (qe.opposition as string[]) || [];
    if (mf.length) rows.push(`<div style="margin:2px 0"><b style="color:var(--text-dim)">За:</b> ${mf.map((x) => chip(x, "#2ecc71")).join("")}</div>`);
    if (op.length) rows.push(`<div style="margin:2px 0"><b style="color:var(--text-dim)">Против:</b> ${op.map((x) => chip(x, "#e74c3c")).join("")}</div>`);
  }
  // Setups
  const sk = Object.keys(setups);
  if (sk.length) {
    rows.push(`<div style="margin:2px 0"><b style="color:var(--text-dim)">Стратегии (5m):</b> ${sk.map((k) => {
      const s = setups[k];
      const b = Number(s.BUY ?? 0), sl = Number(s.SELL ?? 0);
      const col = b > sl ? "#2ecc71" : sl > b ? "#e74c3c" : "var(--text-dim)";
      return chip(`${k}: ↑${b}/↓${sl}`, col);
    }).join("")}</div>`);
  }
  if (!rows.length) rows.push(`<div style="color:var(--text-dim)">нет данных meta</div>`);
  return `<tr class="trade-details-row"><td colspan="10" style="background:var(--bg-soft);padding:8px 12px;font-size:11px;line-height:1.6">${rows.join("")}</td></tr>`;
}

// ===== правый сайдбар: рынок TQBR (скринер) =====
let _srRows: ScreenerRow[] = [];
let _srSortKey: keyof ScreenerRow = (localStorage.getItem("deeptrading_sr_sort") as keyof ScreenerRow) || "turnover";
let _srSortAsc = localStorage.getItem("deeptrading_sr_sort_asc") === "1";
// Сортировка вкладки «Голоса» (с памятью).
let _votesSortKey: "ticker" | "atr" | "votes" = (localStorage.getItem("deeptrading_votes_sort") as "ticker" | "atr" | "votes") || "ticker";
let _votesSortAsc = localStorage.getItem("deeptrading_votes_sort_asc") === "1";
let _srLoading = false;
let _srQuery = localStorage.getItem("deeptrading_sr_q") ?? "";
let _srMinTurnoverM = Number(localStorage.getItem("deeptrading_sr_min_t") ?? 0) || 0;
const _srPrevPrice = new Map<string, number>();

function _srCmpNum(v: number | null | undefined): number {
  return v == null ? -Infinity : v;
}

function _srCompare(a: ScreenerRow, b: ScreenerRow): number {
  const k = _srSortKey;
  if (k === "ticker") return _srSortAsc
    ? a.ticker.localeCompare(b.ticker, "ru")
    : b.ticker.localeCompare(a.ticker, "ru");
  if (k === "name") return _srSortAsc
    ? a.name.localeCompare(b.name, "ru")
    : b.name.localeCompare(a.name, "ru");
  const x = _srCmpNum(a[k] as number | null);
  const y = _srCmpNum(b[k] as number | null);
  return _srSortAsc ? x - y : y - x;
}

function _renderScreenerTable() {
  const tbody = $("sr-tbody");
  if (!tbody) return;
  const q = _srQuery.trim().toLowerCase();
  const minT = _srMinTurnoverM * 1e6;
  const filtered = _srRows.filter((r) => {
    if (minT > 0 && (r.turnover ?? 0) < minT) return false;
    if (q && !r.ticker.toLowerCase().includes(q) && !r.name.toLowerCase().includes(q)) return false;
    return true;
  });
  const sorted = filtered.sort(_srCompare);
  tbody.innerHTML = sorted
    .map((r) => {
      const prev = _srPrevPrice.get(r.ticker);
      const flash = prev != null && r.price != null && prev > 0
        ? (r.price > prev ? " sr-up" : r.price < prev ? " sr-down" : "")
        : "";
      const arrows = prev != null && r.price != null && prev > 0
        ? (r.price > prev ? " ▲" : r.price < prev ? " ▼" : "")
        : "";
      const uni = r.in_universe ? " sr-universe" : "";
      const priceTxt = r.price != null ? price(r.price) + " ₽" : "—";
      const turnTxt = r.turnover != null ? "₽" + money(r.turnover) : "—";
      const volTxt = r.rng_pct != null ? r.rng_pct.toFixed(2) + "%" : "—";
      return `<tr class="sr-row${uni}" title="${r.name}">` +
        `<td class="ticker-cell">${tickerLogo(r.ticker, r.name)}<span class="ticker-txt">${r.ticker}</span></td>` +
        `<td class="sr-num${flash}">${r.price != null ? priceTxt + arrows : `<span class="sr-dim">—</span>`}</td>` +
        `<td class="sr-num">${r.turnover != null ? turnTxt : `<span class="sr-dim">—</span>`}</td>` +
        `<td class="sr-num">${r.rng_pct != null ? `<span style="color:${r.rng_pct >= 3 ? "var(--gold)" : "var(--text)"}">${volTxt}</span>` : `<span class="sr-dim">—</span>`}</td>` +
        `<td class="sr-num sr-col-action"><button class="${r.in_universe ? "sr-toggle sr-toggle-active" : "sr-toggle"}" data-ticker="${r.ticker}" title="${r.in_universe ? "Убрать из карусели" : "Добавить в карусель"}">${r.in_universe ? "●" : "○"}</button></td>` +
        `</tr>`;
    })
    .join("") || `<tr><td colspan=5 class="sr-empty">ничего не найдено</td></tr>`;
  const countEl = $("sr-count");
  if (countEl) countEl.textContent = `${sorted.length} / ${_srRows.length}`;
  const tsEl = $("sr-ts");
  if (tsEl) tsEl.textContent = `обнов. ${fmtTimeOnly(new Date().toISOString())}`;
  document.querySelectorAll("#sr-table thead th").forEach((th) => {
    const el = th as HTMLElement;
    const key = el.dataset.sort as keyof ScreenerRow | undefined;
    const sortedNow = key === _srSortKey;
    el.classList.toggle("sorted", sortedNow === true);
    const arrow = el.querySelector(".sr-arrow");
    if (arrow) arrow.textContent = sortedNow ? (_srSortAsc ? "▲" : "▼") : "";
  });
}

async function _loadScreener(silent = false) {
  if (_srLoading) return;
  _srLoading = true;
  try {
    const d = await fetchScreener();
    _srRows = d.items;
    _renderScreenerTable();
    _renderCarouselStatus(d.carousel);
    _srPrevPrice.clear();
    for (const r of _srRows) if (r.price != null) _srPrevPrice.set(r.ticker, r.price);
  } catch {
    if (!silent && _srRows.length === 0) {
      const tbody = $("sr-tbody");
      if (tbody) tbody.innerHTML = `<tr><td colspan=5 class="sr-empty">ошибка загрузки рынка</td></tr>`;
    }
    const tsEl = $("sr-ts");
    if (tsEl) tsEl.textContent = `обнов. ${fmtTimeOnly(new Date().toISOString())} (устарело)`;
  } finally {
    _srLoading = false;
  }
}

async function _toggleEligible(ticker: string, r: ScreenerRow) {
  let ok = false;
  try {
    if (r.in_universe) ok = (await screenerRemoveEligible(ticker)).ok;
    else ok = (await screenerAddEligible(ticker)).ok;
  } catch {
    ok = false;
  }
  if (ok) {
    r.in_universe = !r.in_universe;
    _renderScreenerTable();
    void _loadScreener(true);
  }
}

const _SR_ACT_COLORS: Record<string, string> = {
  add: "#7ed67e",
  remove: "#ff6b6b",
  ready: "#9dbdff",
};

function _renderCarouselStatus(c: CarouselStatus | undefined) {
  const el = $("sr-carousel-panel");
  if (!el || !c) return;
  const active = c.active_count ?? 0;
  const eligible = c.eligible_count ?? 0;
  const pending = c.pending ?? [];
  const awaiting = pending.filter((p) => p.need_download);
  const readyWait = pending.filter((p) => !p.need_download);
  const botState = c.bot_running
    ? `<span class="sr-cs-dot sr-cs-dot-on"></span>бот запущен`
    : `<span class="sr-cs-dot sr-cs-dot-off"></span>бот остановлен`;
  const pendingHtml = pending.length
    ? `<div class="sr-cs-pending">` +
      pending.map((p) =>
        `<span class="sr-cs-tag${p.need_download ? " sr-cs-tag-wait" : ""}" title="${p.candle_count} свечей 1min">${p.ticker} <i>${p.candle_count}</i></span>`
      ).join("") +
      `</div>`
    : "";
  const logHtml = (c.log ?? []).slice(0, 6).map((l) =>
    `<div class="sr-cs-log-row"><span class="sr-cs-log-ts">${l.ts}</span> <span class="sr-cs-log-act" style="color:${_SR_ACT_COLORS[l.action] ?? "var(--text-dim)"}">${l.msg}</span></div>`
  ).join("");
  el.innerHTML =
    `<div class="sr-cs-title">🎠 Карусель бота <span class="sr-cs-bot">${botState}</span></div>` +
    `<div class="sr-cs-grid">` +
    `<span class="sr-cs-k">active</span><b class="sr-cs-v sr-cs-v-ok">${active} ${active === eligible ? "" : `/ ${eligible} eligible`}</b>` +
    (pending.length
      ? `<span class="sr-cs-k">pending</span><b class="sr-cs-v">${pending.length} <span class="sr-cs-sub">(${awaiting.length} без свечей, ${readyWait.length} ждут бота)</span></b>`
      : `<span class="sr-cs-k">pending</span><b class="sr-cs-v">0</b>`) +
    `<span class="sr-cs-k">eligible</span><b class="sr-cs-v">${eligible}</b>` +
    `</div>` +
    pendingHtml +
    (logHtml ? `<div class="sr-cs-log">${logHtml}</div>` : "");
}

function _agEsc(s: unknown): string {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
}

// AI-гейт (4-я вкладка правого сайдбара): ожидающие заявки + решения нейросети.
async function renderAiGate() {
  const list = $("ag-list");
  const sub = $("ag-sub");
  const summ = $("ag-summary");
  if (!list) return;
  let approvals: any = null;
  let decs: any[] = [];
  let notes: any[] = [];
  try {
    const [a, d, n] = await Promise.all([
      fetch(`${API}/api/v1/bot/approvals`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
      fetch(`${API}/api/v1/bot/ai_decisions?limit=30`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
      fetch(`${API}/api/v1/bot/ai_notes?limit=30`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]);
    approvals = a;
    decs = (d && d.decisions) || [];
    notes = (n && n.notes) || [];
  } catch { /* сеть/бэкенд недоступны */ }
  const shadow = decs.some((x) => !!x.shadow);
  if (sub) sub.textContent = approvals && approvals.enabled ? (shadow ? "shadow (не применяет)" : "боевой") : "выключен";
  if (summ) {
    if (!approvals) {
      summ.textContent = "нет данных";
    } else {
      const pend = (approvals.pending || []) as any[];
      summ.innerHTML = `таймаут ${Math.round(Number(approvals.timeout_sec) || 0)}с · default <b>${_agEsc(approvals.default)}</b> · ожидают: <b>${pend.length}</b>` +
        (pend.length ? " · " + pend.map((p) => `${_agEsc(p.ticker)} ${_agEsc(p.side)}`).join(", ") : "");
    }
  }
  if (!decs.length) {
    list.innerHTML = '<span class="ag-empty">нет решений</span>';
  } else {
    list.innerHTML = decs.slice().reverse().map((d) => {
    const dec = String(d.decision || "").toLowerCase();
    const cls = dec === "approve" ? "approve" : dec === "reject" ? "reject" : "skip";
    const label = dec === "approve" ? "✓ одобрил" : dec === "reject" ? "✕ отклонил" : "… пропуск";
    const t = d.ts ? new Date(d.ts).toLocaleTimeString("ru-RU", { hour12: false }) : "";
    const conf = d.confidence != null ? ` · conf ${Number(d.confidence).toFixed(2)}` : "";
    const lat = d.latency_ms != null ? ` · ${Math.round(Number(d.latency_ms))}мс` : "";
    const prov = d.provider ? ` · ${_agEsc(d.provider)}` : "";
    const agree = d.agreement === true ? " · 🤝" : (d.agreement === false ? " · ≠" : "");
    const applied = d.applied ? " · применено" : (d.shadow ? " · shadow" : "");
    return `<div class="ag-row ${cls}${d.shadow ? " shadow" : ""}" title="${_agEsc(d.reason)}">
      <div class="ag-r1">
        <span class="ag-t">${_agEsc(t)}</span>
        <span class="ag-tk">${_agEsc(d.ticker)} ${_agEsc(d.side)}${d.qty ? " ×" + _agEsc(d.qty) : ""}</span>
        <span class="ag-dec">${label}</span>
      </div>
      <div class="ag-r2">${_agEsc(d.reason)}<span class="ag-meta">${_agEsc(d.model || "")}${prov}${conf}${lat}${agree}${applied}</span></div>
      ${d.advice ? `<div class="ag-advice" title="совет нейросети">💡 ${_agEsc(d.advice)}</div>` : ""}
    </div>`;
    }).join("");
  }
  // Вахтёр позиций: заметки llama (hold/tighten/close) — словами, без управления.
  const notesEl = $("ag-notes");
  const notesSub = $("ag-watch-sub");
  if (notesEl) {
    if (notesSub) notesSub.textContent = notes.length ? `${notes.length} свежих` : "—";
    if (!notes.length) {
      notesEl.innerHTML = '<span class="ag-empty">нет заметок (позиции вне риска или вахтёр не запущен)</span>';
    } else {
      const actLabel: Record<string, string> = {
        hold: "✋ держать", tighten: "🔧 подтянуть", close: "✕ закрыть", watch: "… наблюдать",
      };
      const tightLabel = (n: any): string => {
        const apd = ((n.applied_levels || {}).applied) || {};
        const hasSl = apd.sl != null, hasTp = apd.tp != null;
        if (hasSl && hasTp) return "🔧 SL+TP";
        if (hasTp) return "🔧 TP";
        if (hasSl) return "🔧 SL";
        return "🔧 подтянуть";
      };
      notesEl.innerHTML = notes.slice().reverse().map((n) => {
        const act = String(n.action || "watch").toLowerCase();
        const t = n.ts ? new Date(n.ts).toLocaleTimeString("ru-RU", { hour12: false }) : "";
        const dsl = n.dist_sl_atr != null ? ` · ${Number(n.dist_sl_atr).toFixed(2)} ATR до SL` : "";
        const dtp = n.dist_tp_atr != null ? ` · ${Number(n.dist_tp_atr).toFixed(2)} ATR до TP` : "";
        const pnl = n.pnl != null ? ` · P&L ${Number(n.pnl) >= 0 ? "+" : ""}${Math.round(Number(n.pnl))}₽` : "";
        return `<div class="ag-note ${act}" title="${_agEsc(n.note)}">
          <div class="ag-r1"><span class="ag-t">${_agEsc(t)}</span><span class="ag-tk">${_agEsc(n.ticker)} ${_agEsc(n.side)}</span><span class="ag-dec">${act === "tighten" ? tightLabel(n) : (actLabel[act] || act)}</span></div>
          <div class="ag-r2">${_agEsc(n.note)}<span class="ag-meta">${_agEsc(n.model || "")}${dsl}${dtp}${pnl}</span></div>
          ${n.advice ? `<div class="ag-advice">💡 ${_agEsc(n.advice)}</div>` : ""}
        </div>`;
      }).join("");
    }
  }
}

let _aiPromptKind: "gate" | "watch" | "trader" = "gate";
let _aiControlCache: any = null;

async function loadAiPrompt() {
  if (!$("ag-prompt")) return;
  try {
    const d = await fetch(`${API}/api/v1/bot/ai_control`)
      .then((r) => (r.ok ? r.json() : null)).catch(() => null);
    _aiControlCache = d || {};
  } catch {
    _aiControlCache = {};
  }
  renderAiPromptEditor();
}

function renderAiPromptEditor() {
  const d = _aiControlCache || {};
  const ed = $("ag-prompt-edit") as HTMLTextAreaElement | null;
  if (!ed) return;
  const ov = String((d.prompts || {})[_aiPromptKind] || "");
  const def = String((((d.defaults || {})[_aiPromptKind]) || {}).system || "");
  ed.value = ov.trim() ? ov : def;
  const info = $("ag-prompt-info");
  if (info) {
    const who = { gate: "🚦 гейт", watch: "👁 вахтёр", trader: "🤖 трейдер" }[_aiPromptKind];
    info.textContent = `${who} · ${ov.trim() ? "переопределён (UI)" : "дефолт воркера"}` +
      ` · ${ed.value.length} симв`;
  }
  const inp = $("ag-send-input") as HTMLTextAreaElement | null;
  if (inp && document.activeElement !== inp) inp.value = String(d.note || "");
  document.querySelectorAll("#ag-prompt .ag-ptab").forEach((b) => {
    (b as HTMLElement).classList.toggle("active",
      (b as HTMLElement).dataset.kind === _aiPromptKind);
  });
}

async function saveAiPrompt(kind: string, value: string) {
  const st = $("ag-prompt-status");
  try {
    const r = await fetch(`${API}/api/v1/bot/ai_control`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompts: { [kind]: value } }),
    });
    if (st) st.textContent = r.ok ? "✓ сохранено" : "ошибка сохранения";
  } catch {
    if (st) st.textContent = "ошибка сети";
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 2500);
}

async function saveAiNote(value: string) {
  const st = $("ag-send-status");
  try {
    const r = await fetch(`${API}/api/v1/bot/ai_control`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: value }),
    });
    if (st) st.textContent = r.ok ? "✓ отправлено (AI увидит в следующем цикле)" : "ошибка";
  } catch {
    if (st) st.textContent = "ошибка сети";
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 4000);
}

function initScreener() {
  document.querySelectorAll("#sr-table thead th").forEach((th) => {
    th.addEventListener("click", () => {
      const key = (th as HTMLElement).dataset.sort as keyof ScreenerRow | undefined;
      if (!key) return;
      if (_srSortKey === key) _srSortAsc = !_srSortAsc;
      else {
        _srSortKey = key;
        _srSortAsc = key === "ticker" || key === "name";
      }
      try {
        localStorage.setItem("deeptrading_sr_sort", _srSortKey);
        localStorage.setItem("deeptrading_sr_sort_asc", _srSortAsc ? "1" : "0");
      } catch { /* noop */ }
      _renderScreenerTable();
    });
  });
  const qEl = $("sr-query") as HTMLInputElement | null;
  if (qEl) {
    qEl.value = _srQuery;
    qEl.addEventListener("input", () => {
      _srQuery = qEl.value;
      try { localStorage.setItem("deeptrading_sr_q", _srQuery); } catch { /* noop */ }
      _renderScreenerTable();
    });
  }
  const tEl = $("sr-min-t") as HTMLInputElement | null;
  if (tEl) {
    tEl.value = String(_srMinTurnoverM);
    tEl.addEventListener("input", () => {
      _srMinTurnoverM = Number(tEl.value) || 0;
      try { localStorage.setItem("deeptrading_sr_min_t", String(_srMinTurnoverM)); } catch { /* noop */ }
      _renderScreenerTable();
    });
  }
  const btn = $("sr-refresh");
  if (btn) btn.addEventListener("click", () => void _loadScreener(false));
  const tbodyEl = $("sr-tbody");
  if (tbodyEl) {
    tbodyEl.addEventListener("click", (e) => {
      const b = (e.target as HTMLElement).closest(".sr-toggle") as HTMLButtonElement | null;
      if (!b) return;
      const tk = b.dataset.ticker;
      if (!tk) return;
      const r = _srRows.find((x) => x.ticker === tk);
      if (!r) return;
      b.disabled = true;
      void _toggleEligible(tk, r).finally(() => { b.disabled = false; });
    });
  }
  initSidebarRightResize();
  // Вкладки правого сайдбара: Рынок / Голоса
  document.querySelectorAll(".sr-tab").forEach((b) => {
    b.addEventListener("click", () => {
      const tab = (b as HTMLElement).dataset.srtab;
      document.querySelectorAll(".sr-tab").forEach((x) => x.classList.toggle("active", x === b));
      $("sr-pane-market")?.classList.toggle("hidden", tab !== "market");
      $("sr-pane-votes")?.classList.toggle("hidden", tab !== "votes");
      $("sr-pane-aigate")?.classList.toggle("hidden", tab !== "aigate");
      if (tab === "aigate") void renderAiGate();
    });
  });
  $("stats-refresh")?.addEventListener("click", () => { void renderStats(); });
  $("ag-refresh")?.addEventListener("click", () => { void renderAiGate(); });
  $("ag-prompt-btn")?.addEventListener("click", () => {
    const p = $("ag-prompt");
    if (!p) return;
    p.classList.toggle("hidden");
    if (!p.classList.contains("hidden")) void loadAiPrompt();
  });
  document.querySelectorAll("#ag-prompt .ag-ptab").forEach((b) => {
    b.addEventListener("click", () => {
      _aiPromptKind = ((b as HTMLElement).dataset.kind as any) || "gate";
      renderAiPromptEditor();
    });
  });
  $("ag-prompt-save")?.addEventListener("click", () => {
    const ed = $("ag-prompt-edit") as HTMLTextAreaElement | null;
    if (ed) void saveAiPrompt(_aiPromptKind, ed.value);
  });
  $("ag-prompt-reset")?.addEventListener("click", () => {
    const ed = $("ag-prompt-edit") as HTMLTextAreaElement | null;
    if (ed) { ed.value = ""; void saveAiPrompt(_aiPromptKind, ""); }
  });
  $("ag-send-btn")?.addEventListener("click", () => {
    const inp = $("ag-send-input") as HTMLTextAreaElement | null;
    if (inp) void saveAiNote(inp.value);
  });
  $("ag-full")?.addEventListener("click", () => {
    $("sr-pane-aigate")?.classList.toggle("sr-fullscreen");
  });
  $("stats-apply")?.addEventListener("click", () => { void renderStats(); });
  ($("stats-mode") as HTMLSelectElement | null)?.addEventListener("change", () => { void renderStats(); });
  $("stats-cmp-toggle")?.addEventListener("click", () => {
    const p = $("stats-cmp-panel");
    if (!p) return;
    p.classList.toggle("hidden");
    if (!p.classList.contains("hidden")) void loadCompareList();
  });
  $("stats-cmp-run")?.addEventListener("click", () => { void runCompare(); });
  // Клик по плашке тикера → открыть график в существующем chart
  $("votes-config-btn")?.addEventListener("click", toggleEnsembleEditor);
  $("votes-grid")?.addEventListener("click", (e) => {
    const el = (e.target as HTMLElement).closest(".vote-chip") as HTMLElement | null;
    if (!el) return;
    const figi = el.dataset.figi || "";
    const ticker = el.dataset.ticker || "";
    if (figi) { _focusPos = null; sendEmbedFocus(figi, ticker, null); }
  });
  // Сортировка вкладки «Голоса» (с памятью)
  const updVotesArrows = () => {
    document.querySelectorAll(".votes-sort").forEach((b) => {
      const el = b as HTMLElement;
      const arrow = el.querySelector(".vs-arrow");
      if (arrow) arrow.textContent = el.dataset.vsort === _votesSortKey ? (_votesSortAsc ? "▲" : "▼") : "";
      el.classList.toggle("active", el.dataset.vsort === _votesSortKey);
    });
  };
  document.querySelectorAll(".votes-sort").forEach((b) => {
    b.addEventListener("click", () => {
      const k = (b as HTMLElement).dataset.vsort as "ticker" | "atr" | "votes";
      if (_votesSortKey === k) _votesSortAsc = !_votesSortAsc;
      else { _votesSortKey = k; _votesSortAsc = k === "ticker"; }
      try {
        localStorage.setItem("deeptrading_votes_sort", _votesSortKey);
        localStorage.setItem("deeptrading_votes_sort_asc", _votesSortAsc ? "1" : "0");
      } catch { /* noop */ }
      updVotesArrows();
      renderVotes(_lastVotes, _lastUniverse);
    });
  });
  updVotesArrows();
  void _loadScreener(true);
  setInterval(() => void _loadScreener(false), 45000);
}

// Вкладка «Голоса»: список тикеров юниверса, подсветка по числу голосов
// (1 — синий, 2 — жёлтый, 3+ — зелёный). Клик открывает график.
function renderVotes(votes: Array<{ figi: string; ticker: string; buy: number; sell: number; votes: number; side: string; regime?: string | null; vol?: number | null; vol_abs?: number | null }>, universe: Array<{ figi: string; ticker: string; atr_pct?: number }>) {
  const grid = $("votes-grid");
  if (!grid) return;
  const byFigi: Record<string, { buy: number; sell: number; votes: number; side: string; regime?: string | null; vol?: number | null; vol_abs?: number | null }> = {};
  for (const v of votes || []) byFigi[v.figi] = v;
  const items = (universe || []).map((u) => {
    const v = byFigi[u.figi] || { buy: 0, sell: 0, votes: 0, side: "" };
    return { figi: u.figi, ticker: u.ticker, atr_pct: u.atr_pct, ...v };
  }).sort((a, b) => {
    const d = _votesSortKey === "atr"
      ? (a.atr_pct ?? 0) - (b.atr_pct ?? 0)
      : _votesSortKey === "votes"
        ? (a.votes - b.votes) || a.ticker.localeCompare(b.ticker)
        : a.ticker.localeCompare(b.ticker);
    return _votesSortAsc ? d : -d;
  });
  const cnt = $("votes-count");
  if (cnt) cnt.textContent = `${items.filter((x) => x.votes > 0).length} / ${items.length} с голосами`;
  const regShort: Record<string, string> = { HIGH_VOLATILITY: "HV", TREND_UP: "↑", TREND_DOWN: "↓", RANGE: "FLAT", NEUTRAL: "NEU" };
  const fmtVol = (x: number | null | undefined) => (x == null ? "—" : x >= 1000 ? `${(x / 1000).toFixed(1)}k` : `${Math.round(x)}`);
  grid.innerHTML = items.map((it) => {
    const lvl = it.votes >= 3 ? 3 : it.votes === 2 ? 2 : it.votes === 1 ? 1 : 0;
    // Направление: из голосов, иначе из режима.
    let dir = "";
    if (it.votes > 0) dir = it.side === "BUY" ? "▲" : "▼";
    else if (it.regime === "TREND_UP") dir = "▲";
    else if (it.regime === "TREND_DOWN") dir = "▼";
    const dirCol = dir === "▲" ? "var(--up)" : dir === "▼" ? "var(--down)" : "var(--text-dim)";
    const atr = it.atr_pct != null ? `${it.atr_pct.toFixed(2)}%` : "—";
    const reg = it.regime ? (regShort[it.regime] || it.regime) : "—";
    return `<div class="vote-chip vote-${lvl}" data-figi="${it.figi}" data-ticker="${it.ticker}" title="Голоса: BUY ${it.buy} / SELL ${it.sell} · Режим ${it.regime || "—"} · Объём ${fmtVol(it.vol_abs)} · Волат ${atr}">` +
      `<span class="vc-tk">${it.ticker}</span>` +
      `<span class="vc-dir" style="color:${dirCol}">${dir || "•"}</span>` +
      `<span class="vc-reg">${reg}</span>` +
      `<span class="vc-info">V ${fmtVol(it.vol_abs)}</span>` +
      `<span class="vc-info">${atr}</span>` +
      `<span class="vc-v">${it.votes > 0 ? it.votes : ""}</span></div>`;
  }).join("") || `<div class="mini-hint" style="color:#666">нет данных</div>`;
}

let _ensembleCfg: EnsembleConfig | null = null;
const _REGIMES = ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE"];
const _REG_SHORT: Record<string, string> = { NEUTRAL: "Нейтр", HIGH_VOLATILITY: "Волат", TREND_UP: "Тренд↑", TREND_DOWN: "Тренд↓", RANGE: "Флэт" };

async function toggleEnsembleEditor() {
  const ed = $("votes-editor");
  if (!ed) return;
  if (!ed.classList.contains("hidden")) { ed.classList.add("hidden"); return; }
  ed.classList.remove("hidden");
  ed.innerHTML = `<div class="mini-hint">загрузка…</div>`;
  try { _ensembleCfg = await botEnsembleConfig(); } catch { _ensembleCfg = null; }
  renderEnsembleEditor();
}

function renderEnsembleEditor() {
  const ed = $("votes-editor");
  if (!ed) return;
  const cfg = _ensembleCfg;
  if (!cfg) { ed.innerHTML = `<div class="mini-hint">не удалось загрузить</div>`; return; }
  const all = cfg._all_strategies || cfg.setups.map((s) => s.strategy_id);
  const byId: Record<string, EnsembleConfig["setups"][number]> = {};
  for (const s of cfg.setups) byId[s.strategy_id] = s;
  const rows = all.map((id) => {
    const s = byId[id] || { strategy_id: id, enabled: false, tf: "5min", params: {} };
    const params = Object.entries(s.params || {}).map(([k, v]) =>
      `<label class="ens-param">${k}<input type="number" step="any" data-sid="${id}" data-pk="${k}" value="${v}"></label>`).join("");
    const rf = (cfg.regime_setups_filter || {})[id] || [];
    const regs = _REGIMES.map((r) => `<label class="ens-reg"><input type="checkbox" data-sid="${id}" data-reg="${r}" ${rf.includes(r) ? "checked" : ""}>${_REG_SHORT[r]}</label>`).join("");
    return `<div class="ens-row ${s.enabled ? "on" : ""}">` +
      `<label class="ens-enable"><input type="checkbox" data-sid="${id}" data-role="enable" ${s.enabled ? "checked" : ""}> <b>${id}</b></label>` +
      `<div class="ens-params">${params || "<span class='mini-hint'>нет параметров</span>"}</div>` +
      `<div class="ens-regs" title="Ограничить стратегию режимами (пусто = все)">${regs}</div></div>`;
  }).join("");
  ed.innerHTML =
    `<div class="ens-head">` +
    `<label class="ens-q">Кворум <input type="number" id="ens-quorum" min="1" max="8" value="${cfg.quorum}"></label>` +
    `<label class="ens-q">Режим <select id="ens-neutral">${["semi_flip", "flip", "no_flip"].map((m) => `<option ${cfg.neutral_mode === m ? "selected" : ""}>${m}</option>`).join("")}</select></label>` +
    `<label class="ens-q">Vol_thr <input type="number" step="0.1" id="ens-volthr" value="${cfg.vol_thr || 0}"></label></div>` +
    `<div class="ens-list">${rows}</div>` +
    `<div class="ens-actions">` +
    `<button class="btn-primary btn-xs" id="ens-save">💾 Сохранить</button>` +
    `<button class="btn-secondary btn-xs" id="ens-reset">↺ Сброс</button>` +
    `<span class="mini-hint" id="ens-status"></span></div>`;
  $("ens-save")?.addEventListener("click", saveEnsembleEditor);
  $("ens-reset")?.addEventListener("click", async () => {
    try { const r = await botEnsembleReset(); _ensembleCfg = r.config; renderEnsembleEditor(); } catch { /* noop */ }
  });
}

async function saveEnsembleEditor() {
  const ed = $("votes-editor");
  if (!ed || !_ensembleCfg) return;
  const setups = _ensembleCfg.setups.map((s) => ({ ...s }));
  const byId: Record<string, EnsembleConfig["setups"][number]> = {};
  for (const s of setups) byId[s.strategy_id] = s;
  const all = _ensembleCfg._all_strategies || setups.map((s) => s.strategy_id);
  for (const id of all) {
    if (!byId[id]) { byId[id] = { strategy_id: id, enabled: false, tf: "5min", params: {} }; setups.push(byId[id]); }
  }
  const regFilter: Record<string, string[]> = {};
  ed.querySelectorAll("input[type=checkbox]").forEach((el) => {
    const inp = el as HTMLInputElement;
    const sid = inp.dataset.sid as string;
    if (!byId[sid]) return;
    if (inp.dataset.role === "enable") byId[sid].enabled = inp.checked;
    else if (inp.dataset.reg) { if (inp.checked) (regFilter[sid] = regFilter[sid] || []).push(inp.dataset.reg as string); }
  });
  ed.querySelectorAll("input[type=number][data-pk]").forEach((el) => {
    const inp = el as HTMLInputElement;
    const sid = inp.dataset.sid as string;
    if (byId[sid]) byId[sid].params[inp.dataset.pk as string] = Number(inp.value);
  });
  const quorum = Number(($("ens-quorum") as HTMLInputElement)?.value || _ensembleCfg.quorum);
  const neutral = ($("ens-neutral") as HTMLSelectElement)?.value || _ensembleCfg.neutral_mode;
  const volthr = Number(($("ens-volthr") as HTMLInputElement)?.value || 0);
  const st = $("ens-status");
  if (st) st.textContent = "сохранение…";
  try {
    const r = await botEnsemblePatch({ quorum, neutral_mode: neutral, vol_thr: volthr, setups, regime_setups_filter: regFilter });
    _ensembleCfg = r.config;
    if (st) st.textContent = `✓ применено (стратегий ${r.applied_strategies})`;
  } catch {
    if (st) st.textContent = "ошибка сохранения";
  }
}

async function loadCompareList() {
  const list = $("stats-cmp-list");
  if (!list) return;
  list.innerHTML = `<span class="mini-hint">загрузка…</span>`;
  try {
    const tests = await fetchTests();
    if (!tests.length) { list.innerHTML = `<span class="mini-hint">нет тестов</span>`; return; }
    list.innerHTML = tests.map((t) => {
      const dt = (t.replay_start || "").slice(0, 10);
      const n = t.net ?? 0;
      return `<label><input type="checkbox" value="${esc(t.name)}"> ${esc(t.name)} ` +
        `<span style="color:var(--text-dim)">${dt} · ${t.trades} · ${n >= 0 ? "+" : ""}${n}₽</span></label>`;
    }).join("");
  } catch {
    list.innerHTML = `<span class="mini-hint">ошибка загрузки</span>`;
  }
}

async function runCompare() {
  const out = $("stats-cmp-result");
  if (!out) return;
  const names = Array.from(document.querySelectorAll("#stats-cmp-list input:checked"))
    .map((x) => (x as HTMLInputElement).value);
  if (!names.length) { out.innerHTML = `<div class="mini-hint">выбери тесты</div>`; return; }
  out.innerHTML = `<div class="mini-hint">загрузка…</div>`;
  let d: TestsCompare;
  try { d = await fetchTestsCompare(names); }
  catch { out.innerHTML = `<div class="mini-hint">ошибка</div>`; return; }
  const ts = Object.keys(d.tests);
  const netCell = (v: number) => `<td style="color:${v >= 0 ? "var(--up)" : "var(--down)"}">${v >= 0 ? "+" : ""}${v}</td>`;
  const sec = (title: string, pick: (e: TestCompareEntry) => StatsRow[]) => {
    const maps = ts.map((n) => {
      const m: Record<string, StatsRow> = {};
      for (const r of pick(d.tests[n])) m[r.key] = r;
      return m;
    });
    const keys = Array.from(new Set(maps.flatMap((m) => Object.keys(m))));
    if (!keys.length) return "";
    const head = `<tr><th>${title}</th>${ts.map((n) => `<th>${esc(n)}</th>`).join("")}</tr>`;
    const body = keys.map((k) => `<tr><td>${esc(k)}</td>${maps.map((m) => netCell(m[k]?.net ?? 0)).join("")}</tr>`).join("");
    return `<div class="stats-cmp-h">${title}</div><table class="stats-cmp-table"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  };
  const ohead = `<tr><th>Overall</th>${ts.map((n) => `<th>${esc(n)}</th>`).join("")}</tr>`;
  const orows = (["trades", "wr", "net", "pf"] as const).map((k) =>
    `<tr><td>${k}</td>${ts.map((n) => {
      const v = (d.tests[n].overall as unknown as Record<string, number | null>)[k];
      return `<td>${v ?? "—"}</td>`;
    }).join("")}</tr>`).join("");
  out.innerHTML = `<div class="stats-cmp-h">Overall</div><table class="stats-cmp-table"><thead>${ohead}</thead><tbody>${orows}</tbody></table>` +
    sec("По режиму", (e) => e.by_regime) +
    sec("По голосам", (e) => e.by_strategy) +
    sec("По направлению", (e) => e.by_side) +
    sec("По выходам", (e) => e.by_exit_reason);
}

export async function renderStats() {
  // Строка размера слота (pos_pct) — на странице «Анализ».
  try {
    const [rc, rs] = await Promise.all([
      fetch(`${API}/api/v1/bot/config`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
      fetch(`${API}/api/v1/bot/status`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]);
    const el = $("an-sizing");
    if (el && rc) {
      const ls = (rs && rs.long_short) || {};
      el.textContent = `слот: ${Math.round((Number(rc.pos_pct) || 0.4) * 100)}% от EQ` +
        ` · макс позиций: ${rc.max_positions ?? "—"}` +
        ` · кап экспозиции: ${Math.round((Number(rc.max_exposure_pct) || 1) * 100)}%` +
        ` · L/S: ${ls.longs ?? "—"}/${ls.shorts ?? "—"}`;
    }
  } catch { /* ignore */ }
  // Блок движений: срезы по горизонтам (1д/1н/1м/3м).
  try {
    const mvEl = $("an-movers");
    if (mvEl) {
      const mv = await fetch(`${API}/api/v1/screener/movers?top=4`)
        .then((r) => (r.ok ? r.json() : null)).catch(() => null);
      if (mv && mv.ok && mv.horizons) {
        const fmt = (arr: any[]) => (arr || []).map((x: any) =>
          `<span class="mv-tk">${x.ticker}</span> <span class="${x.chg >= 0 ? "mv-up" : "mv-dn"}">${x.chg > 0 ? "+" : ""}${x.chg}%</span>` +
          (x.streak ? ` <span class="dim">${x.streak}д</span>` : "")).join("<br>");
        const rows = Object.entries(mv.horizons).map(([lbl, h]: [string, any]) => {
          const c = h.counts || {};
          const cnt = `≥5%: ${c["5"]?.[0] ?? 0}/${c["5"]?.[1] ?? 0}<br>≥10%: ${c["10"]?.[0] ?? 0}/${c["10"]?.[1] ?? 0}<br>≥20%: ${c["20"]?.[0] ?? 0}/${c["20"]?.[1] ?? 0}`;
          return `<tr><td class="mv-h">${lbl}</td><td>${fmt(h.up) || "—"}</td>` +
                 `<td>${fmt(h.down) || "—"}</td><td class="dim num">${cnt}</td></tr>`;
        }).join("");
        mvEl.innerHTML = `<div class="an-ag-head">📊 Движения · рост/падение по горизонтам (${mv.horizons["1д"]?.n ?? 0} тикеров)</div>` +
          `<table class="an-table"><thead><tr><th>Горизонт</th><th>▲ Рост</th><th>▼ Падение</th>` +
          `<th class="num">шт (рост/пад)</th></tr></thead><tbody>${rows}</tbody></table>`;
      }
    }
  } catch { /* ignore */ }

  // Блок портфеля: экспозиции/сектора/маржа/стресс.
  try {
    const pfEl = $("an-portfolio");
    if (pfEl) {
      const pf = await fetch(`${API}/api/v1/bot/portfolio_summary`)
        .then((r) => (r.ok ? r.json() : null)).catch(() => null);
      if (pf && pf.equity != null) {
        const sec = Object.entries(pf.sector_pct || {}).sort((a: any, b: any) => b[1] - a[1]).slice(0, 4)
          .map(([k, v]: [string, any]) => `${k} ${Math.round(Number(v) * 100)}%`).join(", ");
        const st = pf.stress_pct || {};
        const RU: Record<string, string> = {
          portfolio_limit: "портфель", max_positions: "слоты", max_exposure: "экспозиция",
          ls_balance: "L/S", ai_reject: "AI-отказ", ai_reject_cooldown: "AI-кулдаун",
          ai_already_pending: "AI-очередь", imoex_guard: "IMOEX", loss_streak_hold: "серия убытков",
          already_held: "уже в позиции", cooldown: "кулдаун", session_filter: "сессия",
          trend_alignment: "тренд", entries_paused: "пауза", long_disabled: "лонги off",
          short_disabled: "шорты off", opposite_hold: "противоположная",
        };
        const skips = Object.entries(pf.skip_counts || {})
          .sort((a: any, b: any) => b[1] - a[1]).slice(0, 5)
          .map(([k, v]: [string, any]) => `${RU[k] || k} ${v}`).join(" · ");
        const rg = pf.regime || {};
        const _rgRu: Record<string, string> = { bear: "🐻 bear", bull: "🐂 bull",
                                                reversal: "⚠ разворот", neutral: "→ нейтраль" };
        const rgTxt = rg.state
          ? ` · режим: ${_rgRu[rg.state] || rg.state}${rg.pct_20m != null ? ` (20м ${rg.pct_20m >= 0 ? "+" : ""}${Number(rg.pct_20m).toFixed(2)}%)` : ""}`
          : "";
        const qn = (pf.queue || []).length;
        const q0 = pf.queue?.[0];
        const qf = q0?.factors || {};
        const qTop = qn
          ? ` · очередь: ${qn} (топ ${q0?.ticker} ${Math.round(q0?.score || 0)}` +
            ` · hist ${qf.hist ?? "—"} rs ${qf.rs ?? "—"} conf ${qf.conf ?? "—"} liq ${qf.liq ?? "—"} fit ${qf.fit ?? "—"})` +
            (q0?.veto?.length ? ` ⛔${q0.veto.join(",")}` : "")
          : "";
        const rk = pf.rank || {};
        const rkTxt = rk.enabled && rk.top
          ? ` · рейтинг: топ-${rk.top_n} (разведка ${rk.explore}) — ${rk.top.slice(0, 5).join(", ")}`
          : "";
        const dd = pf.dd || {};
        const ddTxt = dd.peak ? ` · просадка ${Math.round((dd.dd_pct || 0) * 1000) / 10}% от пика` : "";
        const cell = (v: string, cls = "") => `<td class="${cls}">${v}</td>`;
        const row2 = (a: string, b: string, c2 = "", d = "") =>
          `<tr>${cell(a, "mv-h")}${cell(b)}${cell(c2)}${cell(d, "dim")}</tr>`;
        pfEl.innerHTML =
          `<div class="an-ag-head">📦 Портфель · equity ${Math.round(pf.equity || 0)}₽</div>` +
          `<table class="an-table"><tbody>` +
          row2("Экспозиция", `L ${Math.round((pf.long_exposure_pct || 0) * 100)}% · ` +
            `S ${Math.round((pf.short_exposure_pct || 0) * 100)}% · net ${Math.round((pf.net_exposure_pct || 0) * 100)}%`,
            `gross ×${pf.gross_leverage ?? "—"}`) +
          row2("Маржа", `${Math.round((pf.margin_use_pct || 0) * 100)}% · свободно ${Math.round(pf.margin_free || 0)}₽`,
            `стресс ±5%: ${Math.round((st["imoex_+5%"] || 0) * 100)}% / ${Math.round((st["imoex_-5%"] || 0) * 100)}%`) +
          (rgTxt ? row2("Режим", rgTxt.replace(/^ · режим: /, "")) : "") +
          (qTop ? row2("Очередь", qTop.replace(/^ · очередь: /, "")) : "") +
          (rkTxt ? row2("Рейтинг", rkTxt.replace(/^ · рейтинг: /, "")) : "") +
          (ddTxt ? row2("Просадка", ddTxt.replace(/^ · просадка /, "")) : "") +
          (sec ? row2("Сектора", sec) : "") +
          (skips ? row2("⛔ Пропуски входа", skips) : "") +
          `</tbody></table>`;
      }
    }
  } catch { /* ignore */ }

  // Блок AI-гейта: «сэкономлено/упущено» по контрфакту (трекер).
  try {
    const ag = $("an-aigate");
    if (ag) {
      const st = await fetch(`${API}/api/v1/bot/ai_stats?days=7`)
        .then((r) => (r.ok ? r.json() : null)).catch(() => null);
      if (st && st.totals) {
        const t = st.totals;
        const provs = Object.entries(st.by_provider || {}).map(([p, v]: [string, any]) =>
          `<span class="an-ag-prov"><b>${p}</b>: n=${v.n} · ✓${v.approve}/✕${v.reject} · saved ${money(v.saved)}₽ / missed ${money(v.missed)}₽ · ${Math.round(v.lat)}мс</span>`).join("");
        const tm = (st.top_missed || []).map((x: any) => `${x.ticker} −${money(x.missed)}₽`).join(", ");
        const tsv = (st.top_saved || []).map((x: any) => `${x.ticker} +${money(x.saved)}₽`).join(", ");
        ag.innerHTML =
          `<div class="an-ag-head">🤖 AI-гейт (${st.days} дн): <b class="${t.impact >= 0 ? "pos" : "neg"}">${t.impact >= 0 ? "+" : ""}${money(t.impact)}₽</b>` +
          ` · сэкономлено ${money(t.saved)}₽ · упущено ${money(t.missed)}₽` +
          ` · решений ${t.n} (✓${t.approve} ✕${t.reject} …${t.skip})` +
          ` · согласие 🤝${t.agree}/≠${t.disagree}</div>` +
          `<div class="an-ag-prov">${provs || "—"}</div>` +
          (tm ? `<div class="an-ag-top">Больше всего упущено: ${tm}</div>` : "") +
          (tsv ? `<div class="an-ag-top">Сэкономлено: ${tsv}</div>` : "");
      } else {
        ag.innerHTML = '<div class="an-ag-head">🤖 AI-гейт: нет данных (трекер ещё не набрал исходы)</div>';
      }
    }
  } catch { /* ignore */ }
  // Блок AI-отчёта: разбор рынка + предложения по механизму бота.
  try {
    const ar = $("an-aireport");
    if (ar) {
      const rp = await fetch(`${API}/api/v1/bot/ai_report`)
        .then((r) => (r.ok ? r.json() : null)).catch(() => null);
      if (rp && rp.analysis) {
        const sugg = (rp.suggestions || []).map((x: any) => `<li>${_agEsc(String(x))}</li>`).join("");
        const acts = (rp.actions || []).map((a: any) =>
          `<span class="ar-act">${_agEsc(a.action || "")} ${_agEsc(a.ticker || "")}` +
          `${a.side ? " " + _agEsc(a.side) : ""}</span>`).join(" ");
        ar.innerHTML = `<div class="an-ag-head">🧠 AI о рынке <span class="dim">${_agEsc(rp.model || "")}` +
          `${rp.now_msk ? " · " + _agEsc(rp.now_msk) : ""}</span></div>` +
          `<div class="ar-text">${_agEsc(rp.analysis)}</div>` +
          (sugg ? `<div class="ar-sugg"><b>Предложения по механизму бота:</b><ul>${sugg}</ul></div>` : "") +
          (acts ? `<div class="ar-acts">Действия: ${acts}</div>` : "");
      } else {
        ar.innerHTML = '<div class="an-ag-head">🧠 AI о рынке: отчёт ещё не получен (ai_trader не запущен)</div>';
      }
    }
  } catch { /* ignore */ }
  const body = $("stats-body");
  if (!body) return;
  body.innerHTML = `<div class="mini-hint">загрузка…</div>`;
  const fromEl = $("stats-from") as HTMLInputElement | null;
  const toEl = $("stats-to") as HTMLInputElement | null;
  const modeEl = $("stats-mode") as HTMLSelectElement | null;
  const from = fromEl?.value || "";
  const to = toEl?.value || "";
  const mode = modeEl?.value || "";
  let st: TestStats;
  try {
    st = await fetchTestStats("", from, to, mode);
  } catch {
    body.innerHTML = `<div class="mini-hint">не удалось загрузить</div>`;
    return;
  }
  const tn = $("stats-test");
  if (tn) {
    const src = st.mode === "paper" ? "тест" : st.mode;
    const per = st.date_from || st.date_to ? ` · ${st.date_from ?? "…"}…${st.date_to ?? "…"}` : "";
    tn.textContent = (st.test_name ? `${st.test_name} · ${src}` : src) + per;
  }
  const o = st.overall;
  if (!o) {
    body.innerHTML = `<div class="mini-hint">нет данных по прогону</div>`;
    return;
  }
  const sec = (title: string, rows: StatsRow[] | undefined) => {
    if (!rows || !rows.length) return "";
    const trs = rows.map((r) =>
      `<tr><td>${esc(r.key)}</td><td class="n">${r.trades}</td><td class="n">${r.wr}%</td>` +
      `<td class="n" style="color:${r.net >= 0 ? "var(--up)" : "var(--down)"}">${r.net >= 0 ? "+" : ""}${r.net}</td>` +
      `<td class="n">${r.pf ?? "—"}</td></tr>`).join("");
    return `<div class="stats-sec"><div class="stats-title">${title}</div>` +
      `<table class="stats-table"><thead><tr><th>Ключ</th><th>N</th><th>WR</th><th>Net</th><th>PF</th></tr></thead>` +
      `<tbody>${trs}</tbody></table></div>`;
  };
  body.innerHTML =
    `<div class="stats-overall">` +
    `<div><span>Сделок</span><b>${o.trades}</b></div>` +
    `<div><span>WR</span><b>${o.wr}%</b></div>` +
    `<div><span>Net</span><b style="color:${o.net >= 0 ? "var(--up)" : "var(--down)"}">${o.net >= 0 ? "+" : ""}${o.net}₽</b></div>` +
    `<div><span>PF</span><b>${o.pf ?? "—"}</b></div>` +
    `<div><span>Gross+</span><b>+${o.gross_win}₽</b></div>` +
    `<div><span>Gross−</span><b>${o.gross_loss}₽</b></div>` +
    `<div><span>Avg win</span><b>+${o.avg_win}₽</b></div>` +
    `<div><span>Avg loss</span><b>${o.avg_loss}₽</b></div>` +
    `<div><span>Открыто</span><b>${st.open_positions}</b></div></div>` +
    sec("По направлению", st.by_side) +
    sec("По bias", st.by_bias) +
    sec("По режиму", st.by_regime) +
    sec("По сессии", st.by_session) +
    sec("По акциям", st.by_ticker) +
    sec("По входам", st.by_entry_reason) +
    sec("По выходам", st.by_exit_reason) +
    sec("По стратегиям (голоса)", st.by_strategy) +
    sec("По кворуму", st.by_quorum);
}

function initSidebarRightResize() {
  const sb = document.getElementById("sidebar-right");
  if (!sb) return;
  const MIN = 240, MAX = 460;
  const KEY = "deeptrading_sidebar_r_w";
  try {
    const saved = localStorage.getItem(KEY);
    if (saved) {
      const w = Math.max(MIN, Math.min(MAX, Number(saved)));
      if (w > 0) sb.style.width = w + "px";
    }
  } catch { /* noop */ }

  let dragging = false;
  let startX = 0;
  let startW = 0;

  const onMove = (e: PointerEvent) => {
    if (!dragging) return;
    const w = Math.max(MIN, Math.min(MAX, startW + (startX - e.clientX)));
    sb.style.width = `${w}px`;
  };
  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("sb-resizing");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    try {
      localStorage.setItem(KEY, String(Math.round(sb.getBoundingClientRect().width)));
    } catch { /* noop */ }
  };
  const grip = document.querySelector("#sidebar-right .sidebar-grip") as HTMLElement | null;
  (grip ?? sb).addEventListener("pointerdown", (e) => {
    const rect = sb.getBoundingClientRect();
    if (!grip && e.clientX > rect.left + 8) return;
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    startW = rect.width;
    document.body.classList.add("sb-resizing");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
}

let _testResults: TestRunRow[] = [];
let _selectedTest: string | null = null;
let _lastRunTest: string | null = null;

function initTestResults() {
  $("btn-test-new")?.addEventListener("click", openTestModal);
  document.querySelector("#bot-tests-table tbody")?.addEventListener("click", (e) => {
    const tr = (e.target as HTMLElement).closest("tr") as HTMLElement | null;
    if (!tr) return;
    const name = tr.dataset.name || "";
    const action = (e.target as HTMLElement).closest("[data-action]");
    // Клик по строке = реальный replay-прогон этого теста через движок (как Live).
    if (!action) { _selectTest(name); return; }
    const act = action.getAttribute("data-action")!;
    if (act === "delete") void _deleteTest(name);
    else if (act === "rerun") void _rerunTest(name);
    else _selectTest(name);
  });
  void _refreshTestResults();
}

async function _refreshTestResults() {
  try { _testResults = await fetchTests(); } catch { _testResults = []; }
  renderTests();
}

function renderTests() {
  const tbody = document.querySelector("#bot-tests-table tbody");
  if (!tbody) return;
  if (!_testResults.length) {
    tbody.innerHTML = `<tr><td colspan="11" style="color:var(--text-dim);text-align:center">Нет сохранённых тестов</td></tr>`;
    return;
  }
  const _m = (n: number) => `${n >= 0 ? "+" : ""}${n.toFixed(0)} ₽`;
  tbody.innerHTML = _testResults.map((t) => {
    const sel = t.name === _selectedTest;
    return `<tr class="${sel ? "selected" : ""}" data-name="${esc(t.name)}">
      <td>${esc(t.name)}</td>
      <td>${esc(t.replay_start ? t.replay_start.slice(0, 10) : "—")}${t.replay_end ? " → " + esc(t.replay_end.slice(0, 10)) : ""}</td>
      <td>${t.trades}</td>
      <td class="pos">${t.wins}</td>
      <td class="neg">${t.losses}</td>
      <td class="pos">${_m(t.gross_win)}</td>
      <td class="neg">${_m(-t.gross_loss)}</td>
      <td class="${t.net >= 0 ? "pos" : "neg"}" style="font-weight:700">${_m(t.net)}</td>
      <td>${t.pf.toFixed(2)}</td>
      <td>${t.winrate}%</td>
      <td>
        <button data-action="rerun" class="btn-secondary btn-sm" title="Перезапустить" style="padding:2px 6px;font-size:10px">↻</button>
        <button data-action="delete" class="btn-danger btn-sm" title="Удалить" style="padding:2px 6px;font-size:10px">✕</button>
      </td>
    </tr>`;
  }).join("");
}

async function _deleteTest(name: string) {
  if (!confirm(`Удалить тест ${name}? Это удалит все сделки этого прогона.`)) return;
  try {
    await deleteTest(name);
    if (_selectedTest === name) _selectedTest = null;
    await _refreshTestResults();
  } catch (e) { alert(e instanceof Error ? e.message : String(e)); }
}

// Выбор теста = реальный replay-прогон через движок: результаты идут в
// ОСНОВНЫЕ таблицы бота (последние сделки/позиции), как при Live-прогоне.
function _selectTest(name: string) {
  if (_selectedTest === name) return;
  _selectedTest = name;
  renderTests();
  void _rerunTest(name);
}

async function _rerunTest(name: string) {
  const t = _testResults.find((x) => x.name === name);
  if (!t || !t.replay_start) { alert("Нет периода для перезапуска"); return; }
  if (_selectedTest !== name) _selectedTest = name;
  if (t.replay_start === _lastRunTest) { renderTests(); return; }
  _lastRunTest = t.replay_start;
  try {
    await botSetMode("test", { test_name: name, replay_start: t.replay_start, replay_end: t.replay_end });
    _curMode = "test";
    _setActiveModeBtn("test");
  } catch (e) { alert(e instanceof Error ? e.message : String(e)); }
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

