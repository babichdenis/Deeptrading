import "./style.css";
import { CandlestickSeries, HistogramSeries, LineSeries, createChart, createSeriesMarkers, LineStyle, PriceScaleMode, type CandlestickData, type HistogramData, type IChartApi, type IPriceLine, type ISeriesApi, type ISeriesMarkersPluginApi, type LineData, type SeriesMarker, type Time, type UTCTimestamp } from "lightweight-charts";
import { fetchAnalysis, fetchCatalog, computeSignals, fetchTestStats, fetchTests, fetchTestsCompare, botGates, botGatesToggle, botEnsembleConfig, botEnsemblePatch, botFunnel, botAiControlPut, type AnalysisDto, type StrategyCardDto, type StatsRow, type TestCompareEntry, type TestsCompare, type TestStats, type GatesReport, type EnsembleConfig } from "./api";

const IS_EMBEDDED = new URLSearchParams(window.location.search).get("embedded") === "1";

const API = window.location.port === "5173" || window.location.port === "5174"
  ? `http://${window.location.hostname}:8000`
  : "";

void API;

const PAGES = ["chart", "bot", "analytics"] as const;
type Page = (typeof PAGES)[number];

function showPage(page: Page): void {
  document.querySelectorAll<HTMLElement>(".nav-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.page === page);
  });
  document.querySelectorAll<HTMLElement>(".page").forEach((p) => {
    p.classList.toggle("active", p.id === `page-${page}`);
  });
  document.getElementById("app")?.setAttribute("data-active-page", page);
  if (page === "bot") {
    initBotTab();
    initBotChartResizer();
  } else if (page === "analytics") {
    void renderStats();
    void ensureReportsLoaded();
  }
}

function initNav(): void {
  document.querySelectorAll<HTMLElement>(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const page = btn.dataset.page as Page | undefined;
      if (page && (PAGES as readonly string[]).includes(page)) showPage(page);
    });
  });
  showPage("bot");
}

async function fetchJSON<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${url}`, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function $(id: string): HTMLElement | null { return document.getElementById(id); }

function setText(id: string, text: string): void { const el = $(id); if (el) el.textContent = text; }



function showModal(id: string): void { $(id)?.classList.remove("hidden"); }
function hideModal(id: string): void { $(id)?.classList.add("hidden"); }

function money(n: number): string { return n.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

function pct(n: number): string { return (n >= 0 ? "+" : "") + n.toFixed(2) + "%"; }

function fmtShortDT(iso?: string): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch { return iso; }
}

function fmtEmDateTime(ts: string): string {
  try {
    return new Date(ts).toLocaleString("ru-RU", { timeZone: "Europe/Moscow", day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch { return ts; }
}

let botPollTimer: number | null = null;
let botRunning = false;
let botPaused = false;
let _botChartInit = false;
let _botFocusFigi: string | null = null;
let _botFocusSig = "";

async function pollBotStatus(): Promise<void> {
  try {
    const status = await fetchJSON<{
      running: boolean;
      equity: number;
      cash: number;
      positions_value: number;
      pnl: number;
      currencies: number;
      shares_value: number;
      session: string;
      trading_status: string;
      imoex_guard?: { active?: number | boolean; pct?: number; dir_pct_20m?: number; dir_pct_5m?: number; blocks?: number; age_sec?: number; last_candle?: string; enabled?: boolean; stale?: boolean };
      ai_mode: string;
      mode: string;
      config?: { mode?: string };
      entries_paused?: boolean;
      universe?: Array<{ figi: string; ticker?: string; atr_pct?: number }>;
      votes?: Array<{ figi: string; ticker?: string; buy?: number; sell?: number; votes?: number; side?: string; regime?: string | null; vol_abs?: number | null }>;
    }>("/api/v1/bot/status");
    _srUniverse = new Map((status.universe ?? []).map((u) => [u.figi, { atr_pct: u.atr_pct }]));
    _srVotes = new Map((status.votes ?? []).map((v) => [v.figi, { votes: v.votes ?? 0, side: v.side ?? "", regime: v.regime ?? null, buy: v.buy ?? 0, sell: v.sell ?? 0, vol_abs: v.vol_abs ?? null }]));
    if (_srRows.length) renderSrTable();
    renderReplayBar(status);
    botRunning = status.running;
    // Шапка: цифры по-режимно из /api/v1/sandbox/status (test→тест, live→брокер,
    // sandbox→sandbox) — как в оригинале. /api/v1/bot/status остаётся источником режима.
    try {
      const ctl = new AbortController();
      const tid = setTimeout(() => ctl.abort(), 5000);
      const sbst = await fetchJSON<{ portfolio?: { equity?: number; free_funds?: number; cash?: number; pnl?: number; tinkoff_currencies?: number | null; tinkoff_shares?: number | null; starting_margin?: number | null; own_in_positions?: number | null } }>("/api/v1/sandbox/status", { signal: ctl.signal });
      clearTimeout(tid);
      const pf = sbst.portfolio;
      if (pf) {
        setText("bs-equity", money(pf.equity ?? 0));
        const ff = pf.free_funds ?? pf.cash;
        setText("bs-cash", money(ff ?? 0));
        const margin = pf.starting_margin ?? pf.own_in_positions;
        setText("bs-positions-value", margin != null ? money(margin) : "—");
        const pnl = pf.pnl ?? 0;
        setText("bs-pnl", (pnl >= 0 ? "+" : "-") + money(Math.abs(pnl)));
        ($("bs-pnl") as HTMLElement).style.color = pnl >= 0 ? "var(--up)" : "var(--down)";
        setText("bs-currencies", pf.tinkoff_currencies != null ? money(pf.tinkoff_currencies) : "—");
        const shEl = $("bs-shares");
        const sh = pf.tinkoff_shares;
        if (shEl) {
          if (sh != null) {
            shEl.textContent = `${sh < 0 ? "−" : sh > 0 ? "+" : ""}${money(Math.abs(sh))}`;
            shEl.classList.remove("pos", "neg");
            shEl.classList.add(sh < 0 ? "neg" : sh > 0 ? "pos" : "");
          } else {
            shEl.textContent = "—";
          }
        }
      }
    } catch { /* sandbox/status может лежать при реплее — не роняем опрос */ }
    setText("bs-session", status.session);
    // MOEX: одна плашка = режим рынка + направление индекса (IMOEX guard, 20м).
    const moexEl = $("bs-moex");
    const moexText = $("bs-trading-status");
    const moexDot = $("bs-moex-dot") as HTMLElement | null;
    if (moexText) {
      const sess = String(status.session ?? "").toUpperCase();
      const regLabel = sess === "TRADING" ? "торги" : sess === "PRE_MARKET" ? "пре-маркет" : sess === "CLEARING" ? "клиринг" : "закрыто";
      const ig = status.imoex_guard as { active?: number; pct?: number; dir_pct_20m?: number; dir_pct_5m?: number; blocks?: number; last_candle?: string; age_sec?: number } | undefined;
      let dirTxt: string, dirCls: string;
      if (ig && ig.active != null && Number(ig.active) > 0) { dirTxt = `↑${Number(ig.pct).toFixed(2)}% SELL-блок`; dirCls = "neg"; }
      else if (ig && ig.active != null && Number(ig.active) < 0) { dirTxt = `↓${Number(ig.pct).toFixed(2)}% BUY-блок`; dirCls = "neg"; }
      else {
        const d20 = ig && ig.dir_pct_20m != null ? Number(ig.dir_pct_20m) : null;
        if (d20 == null) { dirTxt = ""; dirCls = "warn"; }
        else {
          const arrow = d20 > 0.05 ? "↑" : d20 < -0.05 ? "↓" : "→";
          dirTxt = `${arrow}${d20 >= 0 ? "+" : ""}${d20.toFixed(2)}%`;
          dirCls = d20 > 0.05 ? "pos" : d20 < -0.05 ? "neg" : "warn";
        }
      }
      moexText.classList.remove("pos", "neg", "warn");
      moexText.textContent = dirTxt ? `${regLabel} ${dirTxt}` : regLabel;
      moexText.classList.add(dirTxt ? dirCls : "neg");
      const dotBg = sess === "TRADING" ? "var(--up)" : (sess === "PRE_MARKET" || sess === "CLEARING") ? "var(--gold)" : "var(--down)";
      if (moexDot) moexDot.style.background = dotBg;
      if (moexEl) {
        const d5 = ig && ig.dir_pct_5m != null ? ` · 5м ${Number(ig.dir_pct_5m) >= 0 ? "+" : ""}${Number(ig.dir_pct_5m).toFixed(2)}%` : "";
        const bk = ig && ig.active != null && ig.active !== 0 ? ` · блокировок: ${ig.blocks ?? 0}` : "";
        const ageMin = ig && ig.age_sec != null ? " · свеча " + Math.round(Number(ig.age_sec) / 60) + " мин назад" : "";
        moexEl.title = `MOEX: ${regLabel} · направление индекса за 20м${dirTxt ? " " + dirTxt : ""}${d5}${bk}${ageMin}`;
      }
    }
    const aiSel = $("ai-mode-select") as HTMLSelectElement;
    // Не затираем выбор пользователя, пока его PUT ещё в полёте.
    if (aiSel && status.ai_mode && aiSel.dataset.aiUser !== "1") aiSel.value = status.ai_mode;
    const pill = $("bot-pill-top");
    const stateText = $("bot-state-text");
    const modeChip = $("bot-mode");
    const curMode = String(((status as { config?: { mode?: string } }).config?.mode) ?? status.mode ?? "sandbox").toLowerCase();
    const modeKey = curMode.startsWith("test") ? "test" : (curMode === "live" ? "live" : curMode === "sandbox" ? "sandbox" : null);
    _setActiveModeBtn(modeKey);
    const testBlock = $("bot-test-block");
    if (testBlock) testBlock.style.display = modeKey === "test" ? "" : "none";
    if (pill && stateText) {
      if (status.running) {
        pill.classList.remove("off"); pill.classList.add("on");
        stateText.textContent = "Бот работает";
        if (modeChip) { modeChip.textContent = String(status.config?.mode ?? status.mode ?? ""); modeChip.classList.remove("hidden"); }
      } else {
        pill.classList.remove("on"); pill.classList.add("off");
        stateText.textContent = "Бот выключен";
        if (modeChip) modeChip.classList.add("hidden");
      }
    }
    const syncBtn = (id: string) => {
      const b = $(id) as HTMLButtonElement | null;
      if (b) {
        if (status.running) { b.textContent = "■ Остановить"; b.classList.add("stop"); }
        else { b.textContent = "▶ Запустить бота"; b.classList.remove("stop"); }
      }
    };
    syncBtn("btn-bot-toggle");
    botPaused = !!status.entries_paused;
    const pbtn = $("btn-bot-pause");
    if (pbtn) {
      pbtn.classList.toggle("hidden", !status.running);
      updatePauseButton();
    }
  } catch (e) {
    console.warn("bot status poll error", e);
  }
}

const _mskFmtTimeR = new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow", hour: "2-digit", minute: "2-digit", hour12: false });
const _mskFmtDateR = new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit" });
function renderReplayBar(status: any): void {
  const bar = document.getElementById("bot-replay-bar");
  if (!bar) return;
  const rp = status?.replay;
  const _mode = String(status?.mode || "");
  const _testMode = _mode === "test" || _mode.indexOf("test:") === 0 || _mode === "replay";
  const active = !!(rp && rp.active) || _testMode;
  bar.style.display = active ? "flex" : "none";
  if (!active) { (window as any).__replayT0 = 0; return; }
  const fmt = (iso: unknown): string => {
    if (!iso) return "—";
    try {
      const d = new Date(String(iso));
      if (isNaN(d.getTime())) return "—";
      return _mskFmtDateR.format(d) + " " + _mskFmtTimeR.format(d);
    } catch { return "—"; }
  };
  const win = rp ? fmt(rp.start) + " → " + fmt(rp.end) : _mode;
  const pct = rp ? Math.max(0, Math.min(100, Number(rp.pct ?? 0))) : 0;
  const winEl = document.getElementById("bot-replay-window");
  const timeEl = document.getElementById("bot-replay-time");
  const pctEl = document.getElementById("bot-replay-pct");
  const fill = document.getElementById("bot-replay-fill");
  if (winEl) winEl.textContent = (rp ? "окно " : "тест ") + win;
  if (timeEl) timeEl.textContent = "свеча " + fmt(rp ? (rp.now_msk || rp.now) : null);
  // Замер времени теста: база — backend started_at (рестарт на старте теста),
  // фолбэк — первый кадр активного окна в этой вкладке.
  let _t0 = Date.parse(String((rp as any)?.wall_start || (status as any)?.started_at || ""));
  if (!isFinite(_t0) || _t0 <= 0) {
    const _wkey = String(rp?.start ?? "") + ">" + String(rp?.end ?? "");
    if ((window as any).__replayWin !== _wkey) {
      (window as any).__replayWin = _wkey;
      (window as any).__replayT0 = Date.now();
    }
    _t0 = (window as any).__replayT0 || Date.now();
  }
  const _el = Math.max(0, Date.now() - _t0);
  const _f2 = (v: number) => String(v).padStart(2, "0");
  const _hh = Math.floor(_el / 3600000);
  const _mm = Math.floor((_el % 3600000) / 60000);
  const _ss = Math.floor((_el % 60000) / 1000);
  const _elapsed = (_hh > 0 ? _hh + ":" : "") + _f2(_mm) + ":" + _f2(_ss);
  let _eta = "";
  if (pct > 5 && pct < 100) {
    const _rem = (_el / (pct / 100)) - _el;
    const _rh = Math.floor(_rem / 3600000);
    const _rm = Math.floor((_rem % 3600000) / 60000);
    const _rs = Math.floor((_rem % 60000) / 1000);
    _eta = " / ~" + (_rh > 0 ? _rh + ":" : "") + _f2(_rm) + ":" + _f2(_rs);
  }
  if (pctEl) pctEl.textContent = (rp ? pct.toFixed(1) + "%" + (rp.pace ? " · " + rp.pace : "") + " · " : "") +
    "⏱ " + _elapsed + _eta;
  if (fill) fill.style.width = pct.toFixed(1) + "%";
}

async function pollPositions(): Promise<void> {
  try {
    const data = await fetchJSON<{ positions: Array<{
      figi: string; ticker: string; side: string; qty: number; entry_price: number;
      entry_time: string; stop_loss?: number | null; take_profit?: number | null;
      trail_active?: boolean; current_price?: number | null; unrealized_pnl?: number | null;
      roi_pct?: number | null; leverage?: number | null; notional?: number | null;
      own_money?: number | null;
      regime?: string | null; regime_entry?: string; vol?: number | null;
    }> }>("/api/v1/sandbox/positions");
    const tbody = document.querySelector("#bot-positions-table tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    for (const p of data.positions) {
      const tr = document.createElement("tr");
      tr.dataset.figi = p.figi;
      const entryPrice = p.entry_price ?? 0;
      const qty = p.qty ?? 0;
      const lev = p.leverage ?? 1;
      const notional = p.notional ?? entryPrice * qty;
      const own = p.own_money ?? (lev > 0 ? notional / lev : notional);
      const cur = p.current_price ?? entryPrice;
      const pnl = p.unrealized_pnl ?? (p.side === "LONG" ? (cur - entryPrice) * qty : (entryPrice - cur) * qty);
      const pnlCls = pnl >= 0 ? "pos" : "neg";
      const roi = p.roi_pct ?? 0;
      const sl = p.stop_loss ?? null;
      const tp = p.take_profit ?? null;
      const vol = typeof p.vol === "number" ? p.vol : null;
      const entryTime = fmtShortDT(p.entry_time);
      tr.innerHTML = `
        <td>${entryTime}</td>
        <td>${p.ticker}</td>
        <td><span class="${p.side === "LONG" ? "pos" : "neg"}">${p.side}</span></td>
        <td>${qty}</td>
        <td>${money(entryPrice)}</td>
        <td>${money(notional)}₽ <span class="dim">(${money(own)}₽ обесп.)</span></td>
        <td>${money(cur)}</td>
        <td class="${pnlCls}">${pnl >= 0 ? "+" : ""}${money(pnl)}</td>
        <td class="${pnlCls}">${pct(roi)}</td>
        <td>${lev.toFixed(1)}x</td>
        <td>${p.regime_entry || "—"} / ${p.regime || "—"}</td>
        <td>${vol != null ? vol.toFixed(1) + "%" : "—"}</td>
        <td>${sl != null ? money(sl) + (p.trail_active ? " · trail" : "") : "—"}</td>
        <td>${tp != null ? money(tp) : "—"}</td>
        <td><button class="btn-sm ${p.side === "LONG" ? "btn-sell" : "btn-buy"}" data-action="close" data-figi="${p.figi}">${p.side === "LONG" ? "Продать" : "Купить"}</button></td>
      `;
      tbody.appendChild(tr);
    }
    tbody.querySelectorAll<HTMLButtonElement>("button[data-action=close]").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const figi = btn.dataset.figi;
        if (!figi) return;
        btn.disabled = true; btn.textContent = "...";
        try { await fetchJSON(`/api/v1/bot/positions/close?figi=${encodeURIComponent(figi)}`, { method: "POST" }); }
        catch (err) { alert("Ошибка закрытия: " + err); }
finally { await pollPositions(); }
      });
    });
    tbody.querySelectorAll<HTMLTableRowElement>("tr[data-figi]").forEach(tr => {
      tr.addEventListener("click", () => {
        const figi = tr.dataset.figi;
        if (!figi) return;
        const p = data.positions.find((x) => x.figi === figi);
        if (!p) return;
        focusBotChart({
          figi, ticker: p.ticker,
          trade: {
            entry_price: p.entry_price,
            stop_loss: p.stop_loss ?? undefined,
            take_profit: p.take_profit ?? undefined,
          },
          trades: [{ side: p.side, entry_time: p.entry_time, exit_time: undefined }],
          centerSec: emEpoch(p.entry_time) ?? undefined,
        });
      });
    });
    // Автофокус: график сразу смотрит на первую открытую позицию (как в оригинале).
    // Дальше — только обновление линий у ФОКУСИРОВАННОЙ позиции: смена SL/TP/trailing
    // шлёт overlay-сообщение, а не focus, иначе каждые 5с грузились бы свечи заново.
    if (data.positions.length > 0) {
      if (!_botChartInit) {
        _botChartInit = true;
        const first = data.positions[0];
        focusBotChart({
          figi: first.figi, ticker: first.ticker,
          trade: {
            entry_price: first.entry_price,
            stop_loss: first.stop_loss ?? undefined,
            take_profit: first.take_profit ?? undefined,
          },
          trades: [],
        });
      } else if (_botFocusFigi) {
        const cur = data.positions.find((x) => x.figi === _botFocusFigi);
        if (cur) {
          const overlayMsg: Omit<FocusMsg, "type"> = {
            figi: cur.figi, ticker: cur.ticker,
            trade: {
              entry_price: cur.entry_price,
              stop_loss: cur.stop_loss ?? undefined,
              take_profit: cur.take_profit ?? undefined,
            },
            trades: [{ side: cur.side, entry_time: cur.entry_time, exit_time: undefined }],
          };
          const sig = focusSignature({ ...overlayMsg, type: "focus" });
          if (sig !== _botFocusSig) {
            _botFocusSig = sig;
            overlayBotChart(overlayMsg);
          }
        }
      }
    }

  } catch (e) {
    console.warn("pollPositions error", e);
  }
}

async function pollTrades(): Promise<void> {
  try {
    const data = await fetchJSON<{ trades: SandboxTrade[] }>("/api/v1/sandbox/trades?limit=100");
    _lastTrades = data.trades;
    _lastTradeHist = new Map();
    for (const t of data.trades) {
      const row = _lastTradeHist.get(t.figi) ?? [];
      row.push({ side: t.side, entry_time: t.entry_time, exit_time: t.ts });
      _lastTradeHist.set(t.figi, row);
    }
    bindTradesClicks();
    renderTrades();
  } catch (e) {
    console.warn("pollTrades error", e);
  }
}

function bindTradesClicks(): void {
  const tbody = document.querySelector<HTMLTableSectionElement>("#bot-trades-table tbody");
  if (!tbody || tbody.dataset.bound) return;
  tbody.dataset.bound = "1";
  tbody.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;
    const tr = target.closest<HTMLTableRowElement>("tr.trade-row");
    if (!tr) return;
    const idx = Number(tr.dataset.idx ?? "-1");
    const t = _lastTrades[idx];
    if (!t) return;
    // Стрелка справа → toggle деталей (БЕЗ открытия графика).
    if (target.closest(".td-arrow")) {
      const key = tr.dataset.key ?? "";
      if (_openDetails.has(key)) _openDetails.delete(key); else _openDetails.add(key);
      renderTrades();
      return;
    }
    // Любой другой клик по строке → фокус графика на этой сделке (все таймфреймы, центр по сделке).
    const figi = tr.dataset.figi;
    if (!figi) return;
    const enSec = emEpoch(t.entry_time);
    const exSec = t.ts ? emEpoch(t.ts) : null;
    const centerSec = enSec != null && exSec != null ? Math.floor((enSec + exSec) / 2) : (enSec ?? undefined);
    focusBotChart({
      figi, ticker: t.ticker,
      trade: {
        entry_price: t.entry_price ?? undefined,
        exit_price: t.exit_price ?? undefined,
        stop_loss: t.stop_loss ?? undefined,
        take_profit: t.take_profit ?? undefined,
      },
      trades: _lastTradeHist.get(figi) ?? [],
      centerSec,
    });
  });
}

function renderTrades(): void {
  const tbody = document.querySelector<HTMLTableSectionElement>("#bot-trades-table tbody");
  if (!tbody) return;
  bindHmToggle();
  tbody.innerHTML = "";
  const trades = _lastTrades;
  for (let i = 0; i < trades.length; i++) {
    const t = trades[i];
    const tr = document.createElement("tr");
    tr.dataset.figi = t.figi;
    const key = `${t.ticker}|${t.entry_time}`;
    const selected = _openDetails.has(key);
    tr.dataset.idx = String(i);
    tr.dataset.key = key;
    tr.classList.add("trade-row");
    if (selected) tr.classList.add("selected");
    const isLong = String(t.side).toUpperCase() === "BUY" || String(t.side).toUpperCase() === "LONG";
    const isOpen = !t.ts || t.exit_reason === "на торгах" || t.exit_price == null;
    const entry = fmtShortDT(t.entry_time);
    const exit = t.ts ? fmtShortDT(t.ts) : "—";
    const notional = t.notional ?? (t.entry_price ?? 0) * (t.qty ?? 0);
    const lev = t.leverage ?? 1;
    const own = t.own_money ?? (lev > 0 ? notional / lev : notional);
    const netPnl = isOpen ? 0 : (t.net_pnl ?? 0);
    const pnlCls = netPnl >= 0 ? "pos" : "neg";
    const comm = t.commission ?? 0;
    // Мета входа/выхода: RSI с направлением 1m, режим входа, трейлинг.
    let _me: any = null;
    let _xm: any = null;
    try { _me = t.meta ? JSON.parse(t.meta) : null; } catch { _me = null; }
    try { _xm = t.exit_meta ? JSON.parse(t.exit_meta) : null; } catch { _xm = null; }
    const _entryM = ((_me?.entry as Record<string, unknown>) || {}) as Record<string, unknown>;
    // RSI-плоские ключи старых сделок лежат на верхнем уровне meta (rsi, rsi_5m…):
    // читаем оттуда, если в карточке входа их нет.
    const _rsiAt = (k: string): unknown => (_entryM[k] !== undefined ? _entryM[k] : _me?.[k]);
    const _rsiSigTf = typeof _entryM.rsi_sig_tf === "string" ? _entryM.rsi_sig_tf as string : "10min";
    const _rsiChip = (lbl: string, vRaw: unknown, pRaw: unknown): string => {
      if (typeof vRaw !== "number") return "";
      const v = vRaw as number;
      const p = typeof pRaw === "number" ? pRaw as number : null;
      const d = p == null ? 0 : v - p;
      const arrow = p == null ? "" : d > 0 ? "↑" : d < 0 ? "↓" : "→";
      const cls = p == null ? "dim" : d > 0 ? "pos" : d < 0 ? "neg" : "dim";
      return `<span class="dim">${lbl} <span class="${cls}">${v.toFixed(1)}${arrow}</span></span>`;
    };
    const _rsiChips = [
      _rsiChip("1м", _rsiAt("rsi"), _rsiAt("rsi_prev")),
      _rsiChip("5м", _rsiAt("rsi_5m"), _rsiAt("rsi_5m_prev")),
      _rsiChip(_rsiSigTf.replace("min", "м"), _rsiAt("rsi_sig"), _rsiAt("rsi_sig_prev")),
    ].filter(Boolean).join(" · ");
    // Режим входа: карточка (meta.regime) → плоский entry_regime → поле строки.
    const _regime = String(_me?.regime ?? _me?.entry_regime ?? t.regime ?? "");
    const _regChip = _regimeShort(_regime);
    const _trailOut = !!((_xm?.trailing ?? _xm?.trail_active ?? _me?.trailing ?? _me?.trail_active) === true);
    let exitChip = `<span class="exit-chip">${t.exit_reason}</span>`;
    if (_trailOut) exitChip = `<span class="exit-chip">Trailing</span>`;
    else if (t.exit_reason === "stop_loss") exitChip = `<span class="exit-chip stop">SL</span>`;
    else if (t.exit_reason === "take_profit") exitChip = `<span class="exit-chip">TP</span>`;
    else if (t.exit_reason === "flip") exitChip = `<span class="exit-chip">Flip</span>`;
    else if (t.exit_reason === "session_end") exitChip = `<span class="exit-chip pending">Session</span>`;
    else if (isOpen) exitChip = `<span class="exit-chip pending">Открыта</span>`;
    // Вход: кто проголосовал и в каком режиме. Источники — карточка входа
    // (meta.entry / meta.quorum_event), затем колонки строки (entry_reason,
    // strategy_id) — чтобы старые сделки тоже не были пустыми.
    let entryCell = "—";
    try {
      const m = _me;
      const _e0 = (m?.entry || {}) as Record<string, unknown>;
      const er = String(_e0.reason ?? m?.entry_reason ?? t.entry_reason ?? "");
      const _sid0 = String(_e0.strategy_id ?? m?.strategy_id ?? t.strategy_id ?? "");
      const f = ((_e0.features || m?.quorum_event || m || {}) as Record<string, unknown>) || {};
      const _f = f as Record<string, unknown>;
      const erShown = er || _sid0;
      if (erShown) {
        let votes = "";
        if (typeof _f.long_votes === "number" || typeof _f.short_votes === "number") votes = ` ↑${_f.long_votes ?? 0}/↓${_f.short_votes ?? 0}`;
        else if (typeof _f.votes_up === "number" || typeof _f.votes_dn === "number") votes = ` ↑${_f.votes_up ?? 0}/↓${_f.votes_dn ?? 0}`;
        else if (typeof _f.votes === "number" || typeof _f.buy_votes === "number" || typeof _f.sell_votes === "number") {
          const bv = typeof _f.buy_votes === "number" ? _f.buy_votes : 0;
          const sv = typeof _f.sell_votes === "number" ? _f.sell_votes : 0;
          votes = ` v${typeof _f.votes === "number" ? _f.votes : bv + sv}`;
        }
        const short = (erShown.replace("_ensemble", "").replace("neutral_", "neu:").replace("range_", "rng:").replace("hv_", "hv:"));
        const title = `${er || _sid0}${votes ? " · голоса " + votes.slice(1) : ""}${_regime ? " · режим " + _regime : ""}`;
        entryCell = `<span class="entry-chip" title="${esc(title)}">${esc(short)}${esc(votes)}</span>`;
      }
      // Режим входа — в строке таблицы (требование владельца), даже если чип входа пуст.
      if (_regChip) {
        entryCell += ` <span class="dim regime-cell" title="режим рынка на входе: ${esc(_regime)}">${esc(_regChip)}</span>`;
      }
    } catch { /* noop */ }
    // R-множитель = net_pnl / риск (|entry−SL|×qty).
    let rVal = "—"; let rCls = "";
    if (!isOpen && t.stop_loss != null && t.entry_price != null && t.stop_loss !== t.entry_price) {
      const risk = Math.abs(t.entry_price - t.stop_loss) * (t.qty ?? 0);
      if (risk > 0) {
        const r = netPnl / risk;
        rVal = `${r >= 0 ? "+" : ""}${r.toFixed(2)}`;
        rCls = `class="num ${r >= 0 ? "pos" : "neg"}"`;
      }
    }
    tr.innerHTML = `
      <td>${entry} → ${exit}</td>
      <td>${t.ticker}</td>
      <td><span class="${isLong ? "pos" : "neg"}">${isLong ? "LONG" : "SHORT"}</span>${_rsiChips ? ` · ${_rsiChips}` : ""}${_regime ? ` <span class="dim">·&nbsp;${_regime}</span>` : ""}</td>
      <td>${t.qty}</td>
      <td>${money(notional)}₽ <span class="dim">(${money(own)}₽)</span></td>
      <td>${lev.toFixed(1)}x</td>
      <td>${money(t.entry_price)}${t.exit_price != null ? " → " + money(t.exit_price) : ""}</td>
      <td class="${pnlCls}">${isOpen ? "на торгах" : (netPnl >= 0 ? "+" : "") + money(netPnl)}</td>
      <td>${isOpen ? "—" : money(comm)}</td>
      <td>${exitChip}</td>
      <td>${entryCell}</td>
      <td ${rCls}>${rVal}</td>
      <td class="td-arrow" title="раскрыть детали">${selected ? "▲" : "▼"}</td>
    `;
    tbody.appendChild(tr);
    if (selected) {
      const details = document.createElement("tr");
      details.className = "trade-details-row";
      details.innerHTML = tradeDetailsHtml(t);
      tbody.appendChild(details);
    }
  }
  if (!trades.length) {
    tbody.innerHTML = `<tr><td colspan="13" style="color:var(--text-dim)">пока нет сделок</td></tr>`;
  }
}

// Детали сделки: двухколоночная карточка — вход слева, выход справа, цифры в таблицах,
// чипы в едином стиле (.td-chip). Внизу на всю ширину — тепловая карта дня.
function tradeDetailsHtml(t: SandboxTrade): string {
  let m: Record<string, unknown> | null = null;
  try { m = t.meta ? JSON.parse(t.meta) : null; } catch { m = null; }
  const entry = (m?.entry as Record<string, unknown>) || {};
  const qe = (m?.quorum_event as Record<string, unknown>) || {};
  const setups = (m?.setups as Record<string, Record<string, unknown>>) || {};
  const vol = (m?.volume as Record<string, number>) || {};
  const regNow = (m?.regime ?? m?.entry_regime ?? t.regime) as string | undefined;
  const ab = entry.against_bias;
  const chip = (txt: string, c: string) => `<span class="td-chip" style="color:${c};border-color:${c}">${txt}</span>`;
  const fnum = (x: number | undefined) => (x == null ? "—" : new Intl.NumberFormat("ru-RU").format(Math.round(x)));
  const kv = (k: string, v: string) => `<tr><td class="td-k">${k}</td><td class="td-v">${v}</td></tr>`;
  const colHtml = (title: string, rows: string[]) =>
    `<div class="td-col"><div class="td-title">${title}</div><table class="td-tbl">${rows.join("")}</table></div>`;

  // Левая колонка: ВХОД.
  const L: string[] = [];
  const _eReason = String(entry.reason ?? t.entry_reason ?? t.strategy_id ?? "—");
  L.push(kv("Вход", `${String(t.side)} · ${_eReason}` +
    (ab === true ? " " + chip("ПРОТИВ bias", "#e74c3c") : ab === false ? " " + chip("по bias", "#2ecc71") : "")));
  // Движок и режим входа — по требованию владельца видно всегда, даже если
  // карточка входа записана частично (старые сделки тестовых прогонов).
  const _eSid = String(entry.strategy_id ?? m?.strategy_id ?? t.strategy_id ?? "");
  if (_eSid) {
    const _eVotes = entry.votes ?? m?.votes ?? null;
    const _eRobot = String(entry.robot ?? "");
    L.push(kv("Движок", esc(_eSid) + (_eVotes != null ? ` · голосов: ${String(_eVotes)}` : "") + (_eRobot ? ` (${esc(_eRobot)})` : "")));
  }
  const _eReg = String(regNow ?? "");
  if (_eReg) L.push(kv("Режим входа", esc(_eReg)));
  if (entry.features) L.push(kv("Level", String((entry.features as Record<string, unknown>).breakout_level ?? "—")));
  // RSI на баре входа (rsi_filter-гейт 40–60): вне коридора — предупреждение.
  // RSI 1м / 5м / сигнального ТФ (10м) на баре сигнала — значение + стрелка направления.
  const _pushRsi = (lbl: string, vRaw: unknown, pRaw: unknown, gate = ""): void => {
    if (typeof vRaw !== "number") return;
    const v = vRaw as number;
    const p = typeof pRaw === "number" ? pRaw as number : null;
    const dir = p == null ? "" : v > p ? " ↑" : v < p ? " ↓" : " →";
    const dirColor = p == null ? "" : v > p ? "var(--up)" : v < p ? "var(--down)" : "var(--text-dim)";
    const out = v > 60 || v < 40;
    L.push(kv(`RSI ${lbl} (вход)`,
      `<span style="color:${out ? "var(--warn)" : "var(--text)"};font-weight:${out ? 700 : 400}">${v.toFixed(1)}${dir ? `<span style="color:${dirColor}">${dir}</span>` : ""}</span>${gate ? ` <span class="td-dim">${gate}</span>` : ""}`));
  };
  const _sigTfLbl = typeof entry.rsi_sig_tf === "string" ? String(entry.rsi_sig_tf).replace("min", "м") : "10м";
  _pushRsi("1м", entry.rsi, entry.rsi_prev);
  _pushRsi("5м", entry.rsi_5m, entry.rsi_5m_prev);
  let _gateLbl = "";
  if (Array.isArray(entry.rsi_gate) && entry.rsi_gate.length === 2) {
    const _gm = String(entry.rsi_gate_mode || "inverse");
    _gateLbl = _gm === "neutral"
      ? `(зона ${Number(entry.rsi_gate[0])}–${Number(entry.rsi_gate[1])})`
      : `(гейт ${Number(entry.rsi_gate[0])}–${Number(entry.rsi_gate[1])})`;
  }
  _pushRsi(_sigTfLbl, entry.rsi_sig, entry.rsi_sig_prev, _gateLbl);
  // Начальный SL/TP в ATR, R и ROI (из sandbox/trades: sl_atr/tp_atr/rr_initial/roi_*).
  if (t.sl_atr != null || t.tp_atr != null || t.rr_initial != null) {
    const up = (x: string) => `<span style="color:var(--up)">${x}</span>`;
    const dn = (x: string) => `<span style="color:var(--down)">${x}</span>`;
    L.push(kv("SL (вход)", `${t.stop_loss != null ? money(t.stop_loss) : "—"} [${t.sl_atr != null ? dn(`${t.sl_atr.toFixed(2)} ATR`) : "—"} / ROI ${t.roi_sl_pct != null ? dn(`${t.roi_sl_pct.toFixed(1)}%`) : "—"}]`));
    L.push(kv("TP (вход)", `${t.take_profit != null ? money(t.take_profit) : "—"} [${t.tp_atr != null ? up(`${t.tp_atr.toFixed(2)} ATR`) : "—"} / ROI ${t.roi_tp_pct != null ? up(`+${t.roi_tp_pct.toFixed(1)}%`) : "—"}]`));
    if (t.rr_initial != null) L.push(kv("R план", `1:${t.rr_initial.toFixed(2)}`));
  }
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
      L.push(kv("Голоса", `<span style="color:var(--up)">↑ ${up.join(", ") || "—"}</span> · <span style="color:var(--down)">↓ ${dn.join(", ") || "—"}</span>`));
    }
  } catch { /* noop */ }
  if (vol.v != null) {
    L.push(kv("Объём (вход)", `${fnum(vol.v)} <span class="td-dim">(max ${fnum(vol.max)}, min ${fnum(vol.min)})</span>${regNow ? ` · Режим ${regNow}` : ""}`));
  }
  if (Object.keys(qe).length) {
    L.push(kv("Кворум", `${String(qe.side ?? "")} · голосов ${String(qe.votes ?? "?")}/${String(qe.total_members ?? "?")} (k=${String(qe.quorum_k ?? "?")}) · BUY ${String(qe.buy_votes ?? 0)} / SELL ${String(qe.sell_votes ?? 0)}`));
    const mf = (qe.members_for as string[]) || [];
    const op = (qe.opposition as string[]) || [];
    if (mf.length) L.push(kv("За", mf.map((x) => chip(x, "#2ecc71")).join(" ")));
    if (op.length) L.push(kv("Против", op.map((x) => chip(x, "#e74c3c")).join(" ")));
  }
  const sk = Object.keys(setups);
  if (sk.length) {
    // ТФ в подписи — из данных конфига (сетапы могут быть на 5m или 10m). Счётчики —
    // НАКОПЛЕННЫЕ сигналы каждой стратегии за всю сессию, а не голоса момента входа:
    // в кворуме голосуют только участники события (чипы «За»/«Против» выше).
    const tfSet = new Set(sk.map((k) => String((setups[k] as Record<string, unknown>).tf || "5min")));
    const tfLabel = ([...tfSet].join("+") || "5min").replace("min", "m");
    L.push(kv(`Сигналы стратегий (${tfLabel})`, sk.map((k) => {
      const s = setups[k];
      const b = Number(s.BUY ?? 0), sv = Number(s.SELL ?? 0);
      const c = b > sv ? "#2ecc71" : sv > b ? "#e74c3c" : "var(--text-dim)";
      return chip(`${k}: ↑${b}/↓${sv}`, c);
    }).join(" ")) + `<div class="td-dim" style="margin-top:3px">накопленные сигналы за сессию (все стратегии); в кворуме на входе голосовали только чипы «За»/«Против»</div>`);
  }
  if (!L.length) L.push(kv("Вход", "—"));

  // Правая колонка: ВЫХОД.
  const R: string[] = [];
  // Max P&L: пик прибыли + время + цена + ATR на пике + R + ROI (из runtime._st_close / API).
  if (t.max_pnl != null && !(t.ts == null || t.exit_price == null)) {
    const mp = t.max_pnl;
    let mpTime = "—";
    try {
      const d = new Date(String(t.max_pnl_time));
      if (!isNaN(d.getTime())) mpTime = fmtShortDT(String(t.max_pnl_time));
    } catch { /* noop */ }
    R.push(kv("Max P&L", `<b class="${mp >= 0 ? "pos" : "neg"}">${mp >= 0 ? "+" : ""}${money(mp)}</b> · в ${mpTime}` +
      (t.max_pnl_price != null ? ` · цена ${money(t.max_pnl_price)}` : "")));
    // ATR на пике — в абсолютных единицах цены (как на входе); % только как запасной вариант для старых сделок.
    if (t.max_pnl_atr != null) R.push(kv("ATR (пик)", `${money(t.max_pnl_atr)} ₽`));
    else if (t.max_pnl_atr_pct != null) R.push(kv("ATR (пик)", `${t.max_pnl_atr_pct}%`));
    if (t.max_pnl_mae_atr != null)
      R.push(kv("MAE до пика", `${t.max_pnl_mae_atr.toFixed(2)} ATR` +
        (t.max_pnl_mae_pct != null ? ` · ${t.max_pnl_mae_pct.toFixed(2)}%` : "")));
    else if (t.max_pnl_mae_pct != null) R.push(kv("MAE до пика", `${t.max_pnl_mae_pct.toFixed(2)}%`));
    // R в том же формате, что «R план» на входе (1:X.XX).
    if (t.max_pnl_r != null) R.push(kv("R (пик)", `<span class="${t.max_pnl_r >= 0 ? "pos" : "neg"}">1:${t.max_pnl_r.toFixed(2)}</span>`));
    if (t.max_pnl_roi_pct != null) R.push(kv("ROI (пик)", `<span class="${t.max_pnl_roi_pct >= 0 ? "pos" : "neg"}">${t.max_pnl_roi_pct >= 0 ? "+" : ""}${t.max_pnl_roi_pct.toFixed(1)}%</span>`));
  }
  // Инфо-трейлинг: где бы сработал трейл (виртуальный след, SL/TP не трогали).
  if (t.trail_info && t.exit_price != null) {
    const ti = t.trail_info;
    let hitTime = "—";
    try {
      const d = new Date(String(ti.hit_time));
      if (!isNaN(d.getTime())) hitTime = fmtShortDT(String(ti.hit_time));
    } catch { /* noop */ }
    R.push(kv("Трейл (инфо)", ti.activated ? "вкл" : "не активировался"));
    R.push(kv("Закрыл бы (вирт. трейл)", ti.hit_price != null ? `<b style="color:var(--warn)">${money(ti.hit_price)}</b> в ${hitTime}` : "не сработал"));
    if (ti.hit_pnl != null) R.push(kv("Забрал бы прибыль", `<span class="${ti.hit_pnl >= 0 ? "pos" : "neg"}">${ti.hit_pnl >= 0 ? "+" : ""}${money(ti.hit_pnl)} ₽</span>`));
    R.push(kv("Стоп / дист", `${ti.trail_stop != null ? money(ti.trail_stop) : "—"} · ${ti.trail_dist_atr != null ? `${ti.trail_dist_atr.toFixed(2)}×ATR` : "—"}`));
    if (ti.hit_price != null && ti.hit_r != null) R.push(kv("R (трейл)", `<span class="${ti.hit_r >= 0 ? "pos" : "neg"}">1:${ti.hit_r.toFixed(2)}</span>`));
    if (ti.hit_price != null && ti.hit_roi_pct != null) R.push(kv("ROI (трейл)", `<span class="${ti.hit_roi_pct >= 0 ? "pos" : "neg"}">${ti.hit_roi_pct >= 0 ? "+" : ""}${ti.hit_roi_pct.toFixed(1)}%</span>`));
  }
  if (!R.length) R.push(kv("Выход", "—"));

  // Тепловая карта торгового дня: по кнопке вкл/выкл (считается только по запросу — дорогая).
  // Состояние хранится в localStorage ("hmDay"), по умолчанию выключена.
  let hm = "";
  try {
    const hmKey = `hm-${t.ticker}-${String(t.entry_time).replace(/[^0-9]/g, "")}`;
    const hmOn = (() => { try { return localStorage.getItem("hmDay") === "1"; } catch { return false; } })();
    // Как на вкладке «Анализ»: готовый HTML из кэша вживляется сразу — без «загрузка…»,
    // без пересчёта на каждом опросе (пересчёт только если кэша нет или он устарел).
    const cached = hmOn ? _hmDayGet(t.figi, String(t.entry_time).slice(0, 10)) : null;
    hm = `<div style="margin:8px 0 2px"><b style="color:var(--text-dim)">Тепловая карта дня (МСК 06:00–24:00, шаг 30 мин):</b> ` +
      `<button data-hm-toggle="1" title="Показать/скрыть тепловую карту (считается по запросу)" style="font-size:10px;padding:1px 10px;border-radius:6px;border:1px solid var(--text-dim);background:transparent;color:var(--text);cursor:pointer">${hmOn ? "выкл" : "вкл"}</button>` +
      (cached ? cached.html : hmOn ? ` <span data-hm="${esc(hmKey)}" style="color:var(--text-dim)">загрузка…</span>` : ` <span style="color:var(--text-dim)">выключена</span>`) +
      `</div>`;
    if (hmOn && (!cached || !cached.fresh)) queueMicrotask(() => { void renderTradeDayHeatmap(hmKey, t.figi, String(t.entry_time)); });
  } catch { /* noop */ }

  const body = `<div class="td-grid">${colHtml("ВХОД", L)}${colHtml("ВЫХОД", R)}</div>`;
  return `<td colspan="13" style="background:var(--bg-soft);padding:8px 12px;font-size:11px;line-height:1.5">` + body + hm + `</td>`;
}

// Тепловая карта торгового дня в карточке сделки: фиксированная сетка 30-мин слотов
// 06:00–24:00 МСК (свечи в БД в UTC → срез 03:00–21:00 UTC того же дня), в ячейке
// % слота + bias (часовой ТФ) и режим H1 из /api/v1/bot/heatmap?meta=1.
// Не пересчитывается на каждом опросе: готовый HTML вживляется из кэша;
// прошедшие дни кэшируются навсегда (данные дня не меняются), текущий — 60 сек.
const _hmDayCache = new Map<string, { html: string; at: number }>();
const _hmMetaCache = new Map<string, { m: Map<string, { b?: number; bh?: number; r?: string; r30?: (string | null)[] }>; at: number }>();

function _hmDayGet(figi: string, day: string): { html: string; fresh: boolean } | null {
  const hit = _hmDayCache.get(`${figi}|${day}`);
  if (!hit) return null;
  const todayUtc = new Date().toISOString().slice(0, 10);
  const fresh = day < todayUtc || Date.now() - hit.at < 60_000;
  return { html: hit.html, fresh };
}

async function _hmFetchMeta(figi: string): Promise<Map<string, { b?: number; bh?: number; r?: string; r30?: (string | null)[] }>> {
  const hit = _hmMetaCache.get(figi);
  if (hit && Date.now() - hit.at < 300_000) return hit.m; // мета (bias/режим) меняется медленно — 5 мин
  const m = new Map<string, { b?: number; bh?: number; r?: string; r30?: (string | null)[] }>();
  try {
    const resp = await fetch(`${API}/api/v1/bot/heatmap?days=10&meta=1&figi=${encodeURIComponent(figi)}`);
    const d = await resp.json() as { tickers?: Array<{ bars?: Array<{ h: string; b?: number; bh?: number; r?: string; r30?: (string | null)[] }> }> };
    for (const b of (d.tickers?.[0]?.bars ?? [])) m.set(String(b.h).slice(0, 13), { b: b.b, bh: b.bh, r: b.r, r30: b.r30 });
    _hmMetaCache.set(figi, { m, at: Date.now() });
  } catch { /* без меты — только % слотов */ }
  return m;
}

async function renderTradeDayHeatmap(key: string, figi: string, entryTime: string): Promise<void> {
  const day = entryTime.slice(0, 10);
  const ck = `${figi}|${day}`;
  const cached = _hmDayGet(figi, day);
  let html = cached?.html ?? "";
  if (!html) {
    try {
      const meta = await _hmFetchMeta(figi);
      // Свечи приходят в UTC: торговой сессии 06:00–24:00 МСК соответствует 03:00–21:00 UTC.
      const loadCandles = async (iv: string, lim: number): Promise<Map<number, { o: number; c: number; h: number; l: number }>> => {
        const r2 = await fetch(`${API}/api/candles/${encodeURIComponent(figi)}?interval_name=${iv}&limit=${lim}`);
        const d2 = await r2.json() as { candles?: Array<{ ts: string; open: number; close: number; high: number; low: number }> };
        const m2 = new Map<number, { o: number; c: number; h: number; l: number }>();
        for (const c of (d2.candles ?? [])) {
          const ts = String(c.ts);
          if (ts.slice(0, 10) !== day) continue;
          const um = Number(ts.slice(11, 13)) * 60 + Number(ts.slice(14, 16));
          if (um < 180 || um >= 1260) continue;
          const s2 = Math.floor((um - 180) / 30);
          const w = m2.get(s2);
          if (!w) m2.set(s2, { o: c.open, c: c.close, h: c.high, l: c.low });
          else { w.c = c.close; w.h = Math.max(w.h, c.high); w.l = Math.min(w.l, c.low); }
        }
        return m2;
      };
      // 5-мин свечи; если за этот день в БД их мало (утро не скачано) — добираем 1-мин.
      let byS = await loadCandles("5min", 3000);
      if (byS.size < 8) byS = await loadCandles("1min", 8000);
      const slotLabel = (s: number) => `${String(6 + Math.floor(s / 2)).padStart(2, "0")}:${s % 2 ? "30" : "00"}`;
      // Направление — треугольники: зелёный ▲ = лонг, красный ▼ = шорт, · = нет данных.
      const _bs = (x?: number): { s: string; c: string } => x === 1 ? { s: "▲", c: "var(--up)" } : x === -1 ? { s: "▼", c: "var(--down)" } : { s: "·", c: "var(--text-dim)" };
      const _rs = (r?: string): { s: string; c: string } => {
        switch (r) {
          case "HIGH_VOLATILITY": return { s: "VOL", c: "#ffb020" };
          case "TREND_UP": return { s: "TRUP", c: "var(--up)" };
          case "TREND_DOWN": return { s: "TRDN", c: "var(--down)" };
          case "RANGE": return { s: "RNG", c: "#4da3ff" };
          default: return { s: "NTR", c: "var(--text-dim)" };
        }
      };
      const cells: string[] = [];
      for (let s = 0; s < 36; s++) {
        const lab = slotLabel(s);
        const v = byS.get(s);
        // Мета ключуется по часу UTC старта слота (МСК = UTC+3): слот 06:00 МСК → 03:00 UTC.
        const m = meta.get(`${day}T${String(3 + Math.floor(s / 2)).padStart(2, "0")}`);
        // Режим — из 30m-сетки (r30): чётный слот (:00) / нечётный (:30); fallback — часовой r.
        const rSlot = m?.r30?.[s % 2] ?? m?.r;
        const mtxt = m ? ` · bias-д ${m.b ?? 0} · bias-ч ${m.bh ?? 0} · режим ${rSlot ?? "—"}` : "";
        let bg = "var(--bg-soft)";
        let pctHtml = `<div style="font-size:10px;color:var(--text-dim)">·</div>`;
        let ttip = `${lab}–${slotLabel(s + 1)} МСК${mtxt}`;
        if (v) {
          const pct = ((v.c - v.o) / v.o) * 100;
          const inten = Math.min(1, Math.abs(pct) / 0.8);
          bg = pct >= 0 ? `rgba(46,204,113,${(0.12 + inten * 0.6).toFixed(2)})` : `rgba(231,76,60,${(0.12 + inten * 0.6).toFixed(2)})`;
          pctHtml = `<div style="font-size:10px;font-weight:700;${pct >= 0 ? "color:var(--up)" : "color:var(--down)"}">${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%</div>` +
            `<div style="font-size:9px;color:var(--text)">${money(v.c)}</div>`;
          ttip = `${lab}–${slotLabel(s + 1)} МСК · ${pct >= 0 ? "+" : ""}${pct.toFixed(2)}% · o ${money(v.o)} · h ${money(v.h)} · l ${money(v.l)} · c ${money(v.c)}${mtxt}`;
        }
        const bs = _bs(m?.bh ?? m?.b);
        const rs = _rs(rSlot);
        cells.push(`<div title="${esc(ttip)}" style="background:${bg};border:1px solid transparent;border-radius:4px;min-width:38px;padding:2px 3px;text-align:center;line-height:1.2">` +
          `<div style="font-size:9px;color:var(--text-dim)">${lab}</div>${pctHtml}` +
          `<div style="font-size:8px;white-space:nowrap"><span style="color:${bs.c};font-weight:700">${bs.s}</span> <span style="color:${rs.c}">${rs.s}</span></div>` +
          `</div>`);
      }
      html = `<div style="display:flex;flex-wrap:wrap;gap:3px;margin-top:3px">${cells.join("")}</div>`;
      _hmDayCache.set(ck, { html, at: Date.now() });
    } catch {
      html = `<span style="color:var(--text-dim)">ошибка загрузки свечей</span>`;
    }
  }
  for (const el of document.querySelectorAll<HTMLElement>(`[data-hm="${key}"]`)) el.outerHTML = html;
}

// Кнопка «вкл/выкл» тепловой карты дня: клик делегирован на document, состояние в localStorage.
let _hmToggleBound = false;
function bindHmToggle(): void {
  if (_hmToggleBound) return;
  _hmToggleBound = true;
  document.addEventListener("click", (ev) => {
    const el = ev.target as HTMLElement | null;
    const btn = el && typeof el.closest === "function" ? el.closest<HTMLButtonElement>("button[data-hm-toggle]") : null;
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    let on = false;
    try { on = localStorage.getItem("hmDay") === "1"; } catch { /* noop */ }
    try { localStorage.setItem("hmDay", on ? "0" : "1"); } catch { /* noop */ }
    renderTrades();
  });
}

interface BotTestRow {
  name: string;
  replay_start: string;
  replay_end: string;
  created_at: string;
  trades: number;
  wins: number;
  losses: number;
  gross_win: number;
  gross_loss: number;
  net: number;
  pf: number;
  winrate: number;
  positions_open: number;
}

interface TestTradeRow {
  figi: string;
  ticker: string;
  side: string;
  qty: number;
  entry_price: number;
  exit_price: number | null;
  entry_time: string;
  ts: string | null;
  stop_loss: number | null;
  take_profit: number | null;
  commission: number;
  net_pnl: number | null;
  exit_reason: string;
  entry_reason: string;
  test_name: string | null;
}

let botTestRows: BotTestRow[] = [];
let botTestSort = { k: "created_at", dir: -1 };
let botTestQuery = "";
const botTestSelected = new Set<string>();
let botTestDrawerName = "";

function botTestFiltered(): BotTestRow[] {
  const q = botTestQuery.trim().toLowerCase();
  const rows = botTestRows.filter((t) => !q || t.name.toLowerCase().includes(q));
  const k = botTestSort.k;
  const dir = botTestSort.dir;
  return rows.sort((a, b) => {
    const av = (a as unknown as Record<string, string | number>)[k];
    const bv = (b as unknown as Record<string, string | number>)[k];
    if (typeof av === "string" || typeof bv === "string") {
      return String(av ?? "").localeCompare(String(bv ?? "")) * dir;
    }
    return (Number(av ?? 0) - Number(bv ?? 0)) * dir;
  });
}

function renderTests(): void {
  const tbody = document.querySelector("#bot-tests-table tbody");
  if (!tbody) return;
  const rows = botTestFiltered();
  tbody.innerHTML = "";
  for (const t of rows) {
    const tr = document.createElement("tr");
    tr.dataset.testId = t.name;
    tr.classList.toggle("selected", botTestSelected.has(t.name));
    tr.classList.toggle("active", botTestDrawerName === t.name);
    const netCls = t.net >= 0 ? "pos" : "neg";
    const period = `${repTs(t.replay_start || t.created_at)}${t.replay_end ? " → " + repTs(t.replay_end) : ""}`;
    tr.innerHTML = `
      <td class="tcheck"><input type="checkbox" data-test-check="${esc(t.name)}"
        ${botTestSelected.has(t.name) ? "checked" : ""} /></td>
      <td class="test-name">${esc(t.name)}</td>
      <td>${period}</td>
      <td>${t.trades}</td>
      <td>${t.wins}</td>
      <td>${t.losses}</td>
      <td class="pos">+${money(t.gross_win)}</td>
      <td class="neg">-${money(t.gross_loss)}</td>
      <td class="${netCls}">${t.net >= 0 ? "+" : ""}${money(t.net)}</td>
      <td>${(t.pf ?? 0).toFixed(2)}</td>
      <td>${(t.winrate ?? 0).toFixed(1)}%</td>
      <td>${t.positions_open || "—"}</td>
      <td class="row-actions">
        <button class="btn-sm" data-action="restart" data-test-id="${esc(t.name)}"
          title="Новый прогон тем же конфигом (старый сохраняется)">↻</button>
        <button class="btn-sm btn-danger" data-action="delete" data-test-id="${esc(t.name)}"
          title="Удалить тест (сделки + окно + сайдкар)">🗑</button>
      </td>
    `;
    tbody.appendChild(tr);
  }
  const cnt = $("bot-test-count");
  if (cnt) cnt.textContent = `${rows.length} из ${botTestRows.length}`;
  const del = $("btn-test-delete-selected") as HTMLButtonElement | null;
  if (del) del.disabled = botTestSelected.size === 0;
  const sel = $("bot-test-selcount");
  if (sel) sel.textContent = String(botTestSelected.size);
  const checkall = $("bot-test-checkall") as HTMLInputElement | null;
  if (checkall) checkall.checked = rows.length > 0 && rows.every((r) => botTestSelected.has(r.name));
  document.querySelectorAll<HTMLTableCellElement>("#bot-tests-table thead th[data-k]").forEach((th) => {
    const k = th.dataset.k;
    th.classList.toggle("sorted", k === botTestSort.k);
    const base = th.textContent?.replace(/[ ↑↓]+$/, "") ?? "";
    th.textContent = base + (k === botTestSort.k ? (botTestSort.dir < 0 ? " ↓" : " ↑") : "");
  });
}

async function pollTests(): Promise<void> {
  try {
    const data = await fetchJSON<{ tests: BotTestRow[] }>("/api/v1/bot/tests");
    botTestRows = data.tests ?? [];
    for (const n of [...botTestSelected]) {
      if (!botTestRows.some((t) => t.name === n)) botTestSelected.delete(n);
    }
    renderTests();
    if (botTestDrawerName && !botTestRows.some((t) => t.name === botTestDrawerName)) closeTestDrawer();
  } catch (e) {
    console.warn("pollTests error", e);
  }
}

async function deleteTests(names: string[]): Promise<void> {
  if (!names.length) return;
  const label = names.length === 1 ? `«${names[0]}»` : `${names.length} тестов`;
  if (!confirm(`Удалить ${label}? Сделки, окна и сайдкары будут стёрты безвозвратно.`)) return;
  try {
    if (names.length === 1) {
      await fetchJSON(`/api/v1/bot/tests/${encodeURIComponent(names[0])}`, { method: "DELETE" });
    } else {
      await fetchJSON("/api/v1/bot/tests/delete", {
        method: "POST", body: JSON.stringify({ names }),
      });
    }
    for (const n of names) {
      botTestSelected.delete(n);
      if (botTestDrawerName === n) closeTestDrawer();
    }
    await pollTests();
  } catch (e) {
    alert("Ошибка удаления: " + e);
  }
}

async function restartTest(name: string, btn?: HTMLElement): Promise<void> {
  if (btn) { btn.setAttribute("disabled", "true"); btn.textContent = "…"; }
  try {
    const out = await fetchJSON<{ test_name: string }>(
      `/api/v1/bot/tests/${encodeURIComponent(name)}/restart`, { method: "POST" });
    await pollTests();
    const hint = `Создан новый тест «${out.test_name}» (старый «${name}» сохранён)`;
    setText("bot-test-count", hint);
    setTimeout(() => renderTests(), 4000);
  } catch (e) {
    alert("Ошибка перезапуска: " + e);
  } finally {
    if (btn) { btn.removeAttribute("disabled"); btn.textContent = "↻"; }
  }
}

function closeTestDrawer(): void {
  botTestDrawerName = "";
  $("bot-test-drawer")?.classList.add("hidden");
  renderTests();
}

async function openTestDrawer(name: string): Promise<void> {
  botTestDrawerName = name;
  $("bot-test-drawer")?.classList.remove("hidden");
  setText("btd-title", name);
  setText("btd-sub", "загрузка сделок…");
  const cards = $("btd-cards");
  if (cards) cards.innerHTML = "";
  const body = $("btd-body");
  if (body) body.innerHTML = `<div class="rep-empty">загрузка сделок…</div>`;
  renderTests();
  try {
    const d = await fetchJSON<{ test_name: string; trades: TestTradeRow[] }>(
      `/api/v1/bot/tests/${encodeURIComponent(name)}`);
    if (botTestDrawerName !== name) return;
    const closed = d.trades.filter((t) => t.ts && t.net_pnl !== null);
    const open = d.trades.filter((t) => !t.ts);
    const wins = closed.filter((t) => (t.net_pnl ?? 0) > 0);
    const losses = closed.filter((t) => (t.net_pnl ?? 0) <= 0);
    const gw = wins.reduce((s, t) => s + (t.net_pnl ?? 0), 0);
    const gl = Math.abs(losses.reduce((s, t) => s + (t.net_pnl ?? 0), 0));
    const net = gw - gl;
    const wr = closed.length ? (wins.length / closed.length) * 100 : null;
    const pf = gl > 0 ? gw / gl : (gw > 0 ? 999 : null);
    const exp = closed.length ? net / closed.length : null;
    setText("btd-sub", `${closed.length} закрытых · открытых ${open.length}`);
    if (cards) {
      cards.innerHTML =
        repCardFrom("Сделок", closed.length) +
        repCardFrom("Открытых", open.length) +
        repCardFrom("Wins", wins.length) +
        repCardFrom("Losses", losses.length) +
        repCardFrom("WR%", wr, 2) +
        repCardFrom("Gross W ₽", gw, 2, true) +
        repCardFrom("Gross L ₽", gl, 2, true) +
        repCardFrom("Net ₽", net, 2, true) +
        repCardFrom("PF", pf, 3) +
        repCardFrom("Exp ₽", exp, 2, true);
    }
    if (body) {
      if (!d.trades.length) {
        body.innerHTML = `<div class="rep-empty">у теста пока нет сделок</div>`;
        return;
      }
      const cols = ["Вход", "Выход", "Тикер", "Side", "Qty", "Вход ₽", "Выход ₽",
        "SL", "TP", "Net ₽", "Комис", "Причина выхода", "Вход (голоса)"];
      const head = cols.map((c, i) => `<th class="${i >= 4 && i <= 10 ? "n" : ""}">${esc(c)}</th>`).join("");
      const rows = d.trades.map((t) =>
        `<tr>` +
        `<td>${repTs(t.entry_time)}</td><td>${repTs(t.ts)}</td>` +
        `<td>${esc(t.ticker)}</td><td>${esc(t.side)}</td><td class="n">${t.qty}</td>` +
        `<td class="n">${t.entry_price == null ? "—" : repN(t.entry_price, 2)}</td>` +
        `<td class="n">${t.exit_price == null ? "—" : repN(t.exit_price, 2)}</td>` +
        `<td class="n">${t.stop_loss == null ? "—" : repN(t.stop_loss, 2)}</td>` +
        `<td class="n">${t.take_profit == null ? "—" : repN(t.take_profit, 2)}</td>` +
        `<td class="n ${repSignCls(t.net_pnl)}">${t.net_pnl == null ? "открыта" : repRub(t.net_pnl)}</td>` +
        `<td class="n">${repRub(t.commission ?? 0)}</td>` +
        `<td>${esc(t.exit_reason || "—")}</td>` +
        `<td>${esc(t.entry_reason || "—")}</td>` +
        `</tr>`).join("");
      body.innerHTML = `<table class="stats-table"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
    }
  } catch (e) {
    if (body) body.innerHTML = `<div class="rep-empty">ошибка: ${esc(String(e))}</div>`;
  }
}

let testPresets: Record<string, Record<string, unknown>> = {};

async function loadTestPresets(): Promise<void> {
  const sel = $("ts-preset") as HTMLSelectElement | null;
  if (!sel) return;
  if (sel.dataset.loaded === "1") return;
  try {
    const d = await fetchJSON<{ presets: Array<{ id: string; name: string; preset: Record<string, unknown> }> }>(
      "/api/v1/bot/presets");
    for (const p of d.presets ?? []) {
      testPresets[p.id] = p.preset;
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = `${p.id} — ${p.name}`;
      sel.appendChild(o);
    }
    sel.dataset.loaded = "1";
  } catch (e) {
    console.warn("presets load error", e);
  }
}

function initTestsBlock(): void {
  $("bot-test-search")?.addEventListener("input", (ev) => {
    botTestQuery = (ev.target as HTMLInputElement).value;
    renderTests();
  });
  $("btn-test-refresh")?.addEventListener("click", () => { void pollTests(); });
  $("bot-test-checkall")?.addEventListener("change", (ev) => {
    const on = (ev.target as HTMLInputElement).checked;
    for (const t of botTestFiltered()) {
      if (on) botTestSelected.add(t.name);
      else botTestSelected.delete(t.name);
    }
    renderTests();
  });
  $("btn-test-delete-selected")?.addEventListener("click", () => {
    void deleteTests([...botTestSelected]);
  });
  document.querySelector("#bot-tests-table thead")?.addEventListener("click", (ev) => {
    const th = (ev.target as HTMLElement | null)?.closest<HTMLElement>("th[data-k]");
    const k = th?.dataset.k;
    if (!k) return;
    if (botTestSort.k === k) botTestSort.dir = -botTestSort.dir;
    else { botTestSort.k = k; botTestSort.dir = k === "name" ? 1 : -1; }
    renderTests();
  });
  document.querySelector("#bot-tests-table tbody")?.addEventListener("click", (ev) => {
    const el = ev.target as HTMLElement;
    const check = el.closest<HTMLElement>("input[data-test-check]");
    if (check instanceof HTMLInputElement) {
      const n = check.dataset.testCheck;
      if (n) {
        if (check.checked) botTestSelected.add(n);
        else botTestSelected.delete(n);
        renderTests();
      }
      ev.stopPropagation();
      return;
    }
    const btn = el.closest<HTMLElement>("button[data-action]");
    if (btn) {
      const n = btn.dataset.testId;
      if (!n) return;
      if (btn.dataset.action === "delete") void deleteTests([n]);
      else if (btn.dataset.action === "restart") void restartTest(n, btn);
      ev.stopPropagation();
      return;
    }
    const row = el.closest<HTMLElement>("tr[data-test-id]");
    const name = row?.dataset.testId;
    if (name) void openTestDrawer(name);
  });
  $("btd-close")?.addEventListener("click", () => closeTestDrawer());
  $("btd-open-analysis")?.addEventListener("click", () => {
    const name = botTestDrawerName;
    if (!name) return;
    repSource = "replay";
    repRunId = name;
    repDetail = null;
    repLoaded = false;
    repSlicesCache = {};
    repTrades = [];
    repTradesTotal = 0;
    const src = document.getElementById("rep-source") as HTMLSelectElement | null;
    if (src) src.value = "replay";
    const kindSel = document.getElementById("rep-kind");
    if (kindSel) kindSel.classList.add("hidden");
    showPage("analytics");
    document.querySelector<HTMLButtonElement>('#page-analytics .an-tab[data-antab="reports"]')?.click();
  });
  void loadTestPresets();
}

let sidebarRendered = false;
function renderSidebar(): void {
  const body = document.getElementById("sidebar-body");
  if (!body || sidebarRendered) return;
  sidebarRendered = true;
  body.innerHTML = `
    <div class="sidebar-logs">
      <div class="logs-heading">
        <div class="sl-title">📜 ЛОГИ <span>(МСК)</span></div>
        <div class="lg-head-btns">
          <button class="lg-btn" id="lg-copy" title="Скопировать видимые логи как текст">⧉</button>
          <button class="lg-btn lg-clear-btn" id="lg-clear" title="Очистить экран логов">🗑</button>
        </div>
      </div>
      <div class="log-toolbar">
        <label class="lg-chip on"><input type="checkbox" id="lg-candles"><span>Свечи</span></label>
        <label class="lg-chip on"><input type="checkbox" id="lg-signals"><span>Сигналы</span></label>
        <label class="lg-chip on"><input type="checkbox" id="lg-trades"><span>Сделки</span></label>
        <label class="lg-chip on"><input type="checkbox" id="lg-events"><span>События</span></label>
        <label class="lg-chip on"><input type="checkbox" id="lg-tech"><span>Тех</span></label>
      </div>
      <div class="log-toolbar log-toolbar-r2">
        <select id="lg-level" class="log-select" title="Фильтр по уровню">
          <option value="">уровень: любой</option>
          <option value="debug">debug</option>
          <option value="info">info</option>
          <option value="warn">warn ⚠</option>
          <option value="error">error ✕</option>
        </select>
        <input type="text" id="lg-q" class="log-q" placeholder="🔍 поиск…" />
        <input type="text" id="lg-ticker" class="log-q log-q-t" placeholder="тикер…" />
      </div>
      <div class="log-toolbar log-toolbar-r3">
        <input type="date" id="log-filter-date" class="log-date-input" title="Фильтр по дате (МСК). Очисти, чтобы видеть все дни" />
        <span class="lg-err-pill" id="lg-err-pill" title="Ошибок и предупреждений среди видимых логов"></span>
      </div>
      <div class="bot-live-logs" id="bot-live-logs"><span class="lg-empty">Бот не запущен — логов нет</span></div>
      <div class="log-footer"><span class="lf-count">Логи <b id="log-count">0</b></span></div>
    </div>`;
  setupLogFilters();
}

// ─── Логи: единый контур, структурированные плашки (порт из оригинала) ───
interface LogItem { id: number; ts: string; level: string; source: string; msg: string; rep?: number }

function _loadLGF(): Record<string, boolean> {
  const saved = localStorage.getItem("log_lgf");
  if (saved) { try { const p = JSON.parse(saved); if (p && typeof p === "object") return p; } catch { /* ignore */ } }
  return { candles: true, signals: true, trades: true, events: true, tech: true, gates: true };
}
const LGF: Record<string, boolean> = _loadLGF();
let _logDateFilter = localStorage.getItem("log_date_filter") ?? new Date(Date.now() + 3 * 3600e3).toISOString().slice(0, 10);
let _logLevelSel = localStorage.getItem("log_level") || "";
let _logQ = localStorage.getItem("log_q") || "";
let _logTicker = localStorage.getItem("log_ticker") || "";

let _logs: LogItem[] = [];
const _seenIds = new Set<number>();
let _logSeq = 0;
let _logHasMore = true;
let _logLoading = false;
let _stickBottom = true;
const LOG_CAP_DOM = 1500;

const LG_SRC_LABEL: Record<string, string> = {
  "t_tech.invest.lo": "tinkoff",
  "app.bot.stream_m": "streams",
  "portfolio_reconcile": "reconcile",
  "portfolio_reconc": "reconcile",
  "candle_feed": "candles",
  "lab.queue": "queue",
  "bot": "bot",
};
const LG_SRC_KEYS = Object.keys(LG_SRC_LABEL).sort((a, b) => b.length - a.length);
function lgSrcLabel(src: string): string {
  const k = LG_SRC_KEYS.find((p) => src.startsWith(p));
  if (k) return LG_SRC_LABEL[k];
  const s = src.replace(/_/g, " ");
  return s.length > 12 ? s.slice(0, 12) + "…" : s;
}

const _LOG_KW: Record<string, RegExp> = {
  candles: /новые свеч|свеч|candle/i,
  signals: /сигнал|свитч|флип/i,
  trades: /сделк|вход|выход|закрыт|открыт|игнор выхода|трейлинг/i,
  gates: /^ГЕЙТ |ПРОПУСК ВХОДА/i,
};
function logCategory(it: LogItem): string {
  const src = it.source || "";
  const msg = it.msg;
  if (src.startsWith("candle_feed")) return "candles";
  if (src.startsWith("portfolio_reconc")) return "trades";
  const m = msg.toLocaleLowerCase();
  if (m.includes("techinfo") || m.includes("flush")) return "tech";
  if (src.startsWith("gate") || _LOG_KW.gates.test(msg)) return "gates";
  if (_LOG_KW.candles.test(msg)) return "candles";
  if (_LOG_KW.signals.test(msg)) return "signals";
  if (_LOG_KW.trades.test(msg)) return "trades";
  return "events";
}

function _itemVisible(it: LogItem): boolean {
  if (!LGF[logCategory(it)]) return false;
  const lv = (it.level || "info").toLowerCase();
  if (_logLevelSel && lv !== _logLevelSel) return false;
  if (_logQ && !it.msg.toLowerCase().includes(_logQ.toLowerCase())) return false;
  if (_logDateFilter && !it.ts.startsWith(_logDateFilter)) return false;
  if (_logTicker) {
    const re = new RegExp("\\b" + _logTicker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b", "i");
    if (!re.test(it.msg)) return false;
  }
  return true;
}

function lgRowEl(it: LogItem): HTMLElement {
  const lv = (it.level || "info").toLowerCase();
  const row = document.createElement("div");
  row.className = `lg-row lg-k-${logCategory(it)} lg-lv-${lv}`;
  row.dataset.id = String(it.id);
  const hd = document.createElement("div"); hd.className = "lg-hd";
  const t = document.createElement("span"); t.className = "lg-t";
  let _todayMsk = "";
  try { _todayMsk = new Date(Date.now() + 3 * 3600e3).toISOString().slice(0, 10); } catch { /* noop */ }
  const _d = it.ts.slice(0, 10), _tm = it.ts.slice(11, 19);
  t.textContent = (_d && _d !== _todayMsk ? `${it.ts.slice(5, 10)} ${_tm}` : _tm) + "  ";
  t.title = it.ts;
  const badge = document.createElement("span"); badge.className = "lg-lv"; badge.textContent = lv.toUpperCase().slice(0, 5) + " ";
  badge.title = `уровень: ${lv}`;
  const src = document.createElement("span"); src.className = "lg-src"; src.textContent = lgSrcLabel(it.source) + " ";
  src.title = it.source;
  const rep = document.createElement("span"); rep.className = "lg-rep"; rep.textContent = it.rep && it.rep > 1 ? "×" + it.rep : "";
  rep.title = "одинаковых записей подряд";
  const copy = document.createElement("button"); copy.className = "lg-copy-row"; copy.textContent = "⧉";
  copy.title = "Скопировать строку";
  copy.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const txt = `[${it.ts}] ${it.msg}`;
    (navigator.clipboard?.writeText(txt) ?? Promise.reject("no clipboard")).then(() => {
      copy.textContent = "✓";
      setTimeout(() => { copy.textContent = "⧉"; }, 900);
    }).catch(() => { });
  });
  hd.append(t, badge, src, rep, copy);
  const msg = document.createElement("div"); msg.className = "lg-txt"; msg.textContent = it.msg;
  row.append(hd, msg);
  return row;
}

function _insertLogs(items: LogItem[], prepend: boolean): void {
  const el = $("bot-live-logs");
  if (!el) return;
  const fresh: LogItem[] = [];
  for (const it of items) {
    if (_seenIds.has(it.id)) {
      if (!prepend && it.rep && it.rep > 1) {
        const row = el.querySelector(`[data-id="${it.id}"]`) as HTMLElement | null;
        const repEl = row?.querySelector(".lg-rep");
        if (repEl && repEl.textContent !== "×" + it.rep) repEl.textContent = "×" + it.rep;
        const sameId = _logs.find((x) => x.id === it.id);
        if (sameId) sameId.rep = it.rep;
      }
      continue;
    }
    _seenIds.add(it.id);
    fresh.push(it);
  }
  if (!fresh.length) return;
  if (prepend) {
    _logs = fresh.concat(_logs);
    const rows = fresh.map(lgRowEl);
    for (const row of rows.reverse()) el.insertBefore(row, el.firstChild);
  } else {
    _logs = _logs.concat(fresh);
    const frag = document.createDocumentFragment();
    for (const it of fresh) if (_itemVisible(it)) frag.appendChild(lgRowEl(it));
    if (frag.childElementCount) el.appendChild(frag);
  }
  while (el.children.length > LOG_CAP_DOM) {
    const first = el.firstElementChild as HTMLElement | null;
    const h = first ? first.getBoundingClientRect().height : 0;
    if (first) first.remove();
    if (el.scrollTop > 0) el.scrollTop -= h;
  }
}

function reRenderLogs(): void {
  const el = $("bot-live-logs");
  if (!el) return;
  const frag = document.createDocumentFragment();
  for (const it of _logs) if (_itemVisible(it)) frag.appendChild(lgRowEl(it));
  el.innerHTML = "";
  el.appendChild(frag);
  updateLogMeta(el);
}

function updateLogMeta(el: HTMLElement): void {
  const cnt = $("log-count");
  if (cnt) cnt.textContent = String(_logs.length);
  if (_stickBottom) el.scrollTop = el.scrollHeight;
  let errs = 0, warns = 0;
  for (let i = 0; i < el.children.length; i++) {
    const c = el.children[i].classList;
    if (c.contains("lg-lv-error")) errs++;
    else if (c.contains("lg-lv-warn")) warns++;
  }
  const pill = $("lg-err-pill");
  if (pill) {
    const total = errs + warns;
    if (total) {
      pill.classList.add("on");
      pill.textContent = errs ? `✕ ${errs} ошибок · ⚠ ${warns}` : `⚠ ${warns} warn`;
    } else {
      pill.classList.remove("on");
    }
  }
}

function _applyLogParams(p: URLSearchParams): void {
  if (_logLevelSel) p.set("level", _logLevelSel);
  if (_logQ) p.set("q", _logQ);
  if (_logTicker) p.set("ticker", _logTicker);
  if (_logDateFilter) p.set("date", _logDateFilter);
}

async function pollLogs(): Promise<void> {
  const el = $("bot-live-logs");
  if (!el) return;
  try {
    const p = new URLSearchParams({ limit: "300", after_id: String(_logSeq) });
    _applyLogParams(p);
    const data = await fetchJSON<{ items?: LogItem[] }>(`/api/v1/bot/logs?${p.toString()}`);
    const items: LogItem[] = data.items || [];
    if (_logSeq === 0 && !items.length && el.children.length === 0) {
      el.innerHTML = '<span class="lg-empty">нет записей под фильтр</span>';
    }
    if (_logSeq === 0 && items.length) {
      _logs = [];
      _seenIds.clear();
      _logHasMore = true;
      el.innerHTML = "";
      _insertLogs(items, false);
    } else {
      _insertLogs(items, false);
    }
    if (items.length) _logSeq = Math.max(_logSeq, items[items.length - 1].id);
    updateLogMeta(el);
  } catch { /* ignore */ }
}

async function loadLogOlder(): Promise<void> {
  if (_logLoading || !_logHasMore) return;
  const el = $("bot-live-logs");
  const first = _logs[0];
  if (!el || !first) return;
  _logLoading = true;
  try {
    const p = new URLSearchParams({ limit: "300", before: first.ts });
    _applyLogParams(p);
    const data = await fetchJSON<{ items?: LogItem[]; has_more?: boolean }>(`/api/v1/bot/logs/history?${p.toString()}`);
    const items: LogItem[] = data.items || [];
    _logHasMore = data.has_more !== false;
    if (items.length) {
      const prevH = el.scrollHeight;
      _insertLogs(items, true);
      el.scrollTop = el.scrollTop + (el.scrollHeight - prevH);
    }
  } catch { /* ignore */ } finally {
    _logLoading = false;
  }
}

async function copyVisibleLogs(): Promise<void> {
  const lines = _logs.filter(_itemVisible).map((i) => `[${i.ts}] ${(i.level || "info").toUpperCase()} ${i.source}: ${i.msg}`);
  const text = lines.join("\n");
  const done = () => {
    const btn = $("lg-copy");
    if (btn) { btn.textContent = "✓"; setTimeout(() => { if (btn) btn.textContent = "⧉"; }, 1200); }
  };
  try { await navigator.clipboard.writeText(text); done(); }
  catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { /* ignore */ }
    ta.remove();
    done();
  }
}

function setupLogFilters(): void {
  const resetLogs = () => { _logSeq = 0; _logHasMore = true; void pollLogs(); };
  const chipDefs: Array<[string, string]> = [["lg-candles", "candles"], ["lg-signals", "signals"], ["lg-trades", "trades"], ["lg-events", "events"], ["lg-tech", "tech"], ["lg-gates", "gates"]];
  for (const [id, key] of chipDefs) {
    const cb = $(id) as HTMLInputElement | null;
    if (!cb) continue;
    cb.checked = !!LGF[key];
    cb.closest("label")?.classList.toggle("on", !!LGF[key]);
    cb.addEventListener("change", () => {
      LGF[key] = cb.checked;
      cb.closest("label")?.classList.toggle("on", cb.checked);
      localStorage.setItem("log_lgf", JSON.stringify(LGF));
      if (key === "candles") {
        void fetch(`${API}/api/v1/bot/logconfig`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ log_candles: cb.checked }) });
      }
      reRenderLogs();
    });
  }
  const level = $("lg-level") as HTMLSelectElement | null;
  if (level) {
    level.value = _logLevelSel;
    level.addEventListener("change", () => { _logLevelSel = level.value; localStorage.setItem("log_level", _logLevelSel); resetLogs(); });
  }
  const q = $("lg-q") as HTMLInputElement | null;
  if (q) {
    q.value = _logQ;
    let tId: ReturnType<typeof setTimeout> | undefined;
    q.addEventListener("input", () => { clearTimeout(tId); tId = setTimeout(() => { _logQ = q.value.trim().toLowerCase(); localStorage.setItem("log_q", _logQ); resetLogs(); }, 300); });
  }
  const tick = $("lg-ticker") as HTMLInputElement | null;
  if (tick) {
    tick.value = _logTicker;
    let tId2: ReturnType<typeof setTimeout> | undefined;
    tick.addEventListener("input", () => { clearTimeout(tId2); tId2 = setTimeout(() => { _logTicker = tick.value.trim().toUpperCase(); localStorage.setItem("log_ticker", _logTicker); resetLogs(); }, 300); });
  }
  const dateInput = $("log-filter-date") as HTMLInputElement | null;
  if (dateInput) {
    dateInput.value = _logDateFilter;
    dateInput.addEventListener("change", () => { _logDateFilter = dateInput.value; localStorage.setItem("log_date_filter", _logDateFilter); resetLogs(); });
  }
  const copy = $("lg-copy");
  if (copy) copy.addEventListener("click", () => { void copyVisibleLogs(); });
  const clear = $("lg-clear");
  if (clear) clear.addEventListener("click", () => {
    _logs = [];
    _seenIds.clear();
    _logSeq = 0;
    _logHasMore = true;
    const el = $("bot-live-logs");
    if (el) el.innerHTML = "";
    if (dateInput) { dateInput.value = ""; _logDateFilter = ""; localStorage.removeItem("log_date_filter"); }
    updateLogMeta($("bot-live-logs") || document.body);
    void fetch(`${API}/api/v1/bot/logs/clear`, { method: "POST" });
  });
  const el = $("bot-live-logs");
  if (el) {
    el.addEventListener("scroll", () => {
      _stickBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 60;
      if (el.scrollTop <= 30) void loadLogOlder();
    });
  }
}

function startBotPolling(): void {
  if (botPollTimer) return;
  pollBotStatus(); pollPositions(); pollTrades(); pollTests();
  void loadScreener();
  botPollTimer = window.setInterval(() => {
    pollBotStatus(); pollPositions(); pollTrades(); pollTests();
    if ((pollTick++ % 3) === 0) void loadScreener();
  }, 5000);
}

let pollTick = 0;

function startPolling(): void {
  startBotPolling();
  renderSidebar();
  pollLogs();
  window.setInterval(() => void pollLogs(), 1000);
}

async function toggleBot(): Promise<void> {
  const btn = $("btn-bot-toggle") as HTMLButtonElement;
  if (!btn) return;
  btn.disabled = true;
  try {
    if (botRunning) {
      await fetchJSON("/api/v1/bot/stop", { method: "POST" });
    } else {
      await fetchJSON("/api/v1/bot/start", { method: "POST" });
    }
    await pollBotStatus();
  } catch (e) { alert("Ошибка: " + e); }
  finally { btn.disabled = false; }
}

let botTabInit = false;

function toUTCISO(v: string): string {
  if (!v) return "";
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? v : d.toISOString();
}

function _setActiveModeBtn(mode: string | null): void {
  document.querySelectorAll<HTMLButtonElement>("#mode-switch .mode-btn").forEach((b) => {
    b.classList.toggle("active", mode != null && b.dataset.mode === mode);
  });
}

async function doToggleMode(mode: "live" | "sandbox" | "test"): Promise<void> {
  if (mode === "test") { showModal("test-modal-overlay"); return; }
  const warn = mode === "live"
    ? "Переключить на LIVE (реальные деньги)?\nБот будет ОСТАНОВЛЕН и перезапущен на боевом счёте."
    : "Переключить на SANDBOX (тестовый счёт)?\nБот будет остановлен и перезапущен.";
  if (!confirm(warn)) return;
  const mb = document.querySelector(`#mode-switch .mode-btn[data-mode="${mode}"]`) as HTMLButtonElement | null;
  if (mb) mb.classList.add("busy");
  try {
    await fetchJSON("/api/v1/bot/mode", { method: "POST", body: JSON.stringify({ mode }) });
    _setActiveModeBtn(mode);
    await pollBotStatus();
  } catch (e) { alert("Ошибка переключения: " + e); }
  finally { if (mb) mb.classList.remove("busy"); }
}

function updatePauseButton(): void {
  const b = $("btn-bot-pause");
  if (!b) return;
  b.textContent = botPaused ? "▶ Снять паузу" : "⏸ Пауза входов";
  b.classList.toggle("paused", botPaused);
}

async function doPause(): Promise<void> {
  try {
    const r = await fetchJSON<{ entries_paused: boolean }>("/api/v1/bot/pause", { method: "POST", body: JSON.stringify({ paused: !botPaused }) });
    botPaused = r.entries_paused;
    updatePauseButton();
    void pollBotStatus();
  } catch (e) { alert(e instanceof Error ? e.message : String(e)); }
}

function initBotTab(): void {
  if (botTabInit) { startBotPolling(); return; }
  botTabInit = true;
  initSessChips();
  initTestsBlock();
  const btn = $("btn-bot-toggle");
  $("btn-bot-settings")?.addEventListener("click", () => { void fillBotSettings(); showModal("bot-modal-overlay"); });
  $("btn-bot-pause")?.addEventListener("click", () => void doPause());
  $("stats-refresh")?.addEventListener("click", () => { void renderStats(); });
  $("stats-apply")?.addEventListener("click", () => { void renderStats(); });
  ($("stats-mode") as HTMLSelectElement | null)?.addEventListener("change", () => { void renderStats(); });
  $("stats-cmp-toggle")?.addEventListener("click", () => {
    const p = $("stats-cmp-panel");
    if (!p) return;
    p.classList.toggle("hidden");
    if (!p.classList.contains("hidden")) void loadCompareList();
  });
  $("stats-cmp-run")?.addEventListener("click", () => { void runCompare(); });
  document.querySelectorAll("#mode-switch .mode-btn").forEach((mb) => {
    mb.addEventListener("click", () => {
      const m = (mb as HTMLElement).dataset.mode as "live" | "sandbox" | "test" | undefined;
      if (m) void doToggleMode(m);
    });
  });
  btn?.addEventListener("click", toggleBot);
  $("bs-close")?.addEventListener("click", () => hideModal("bot-modal-overlay"));
  $("bot-pill-top")?.addEventListener("click", () => showModal("bot-modal-overlay"));
  $("btn-test-new")?.addEventListener("click", () => {
    void loadTestPresets();
    showModal("test-modal-overlay");
  });
  $("ts-close")?.addEventListener("click", () => hideModal("test-modal-overlay"));
  $("ts-run")?.addEventListener("click", async () => {
    const name = ($("ts-name") as HTMLInputElement)?.value?.trim();
    const start = ($("ts-start") as HTMLInputElement)?.value;
    const end = ($("ts-end") as HTMLInputElement)?.value;
    const logdb = ($("ts-logdb") as HTMLInputElement)?.checked ?? false;
    const presetId = ($("ts-preset") as HTMLSelectElement)?.value ?? "";
    const preset = presetId ? testPresets[presetId] : undefined;
    if (!start) { alert("Укажите начало периода"); return; }
    let finalName = name;
    if (preset) {
      const stamp = new Date();
      const p2 = (n: number) => String(n).padStart(2, "0");
      const ts = `${stamp.getFullYear()}${p2(stamp.getMonth() + 1)}${p2(stamp.getDate())}` +
        `-${p2(stamp.getHours())}${p2(stamp.getMinutes())}`;
      if (!finalName) finalName = `${presetId} ${ts}`;
    }
    if (!finalName) { alert("Укажите название теста"); return; }
    const btn = $("ts-run") as HTMLButtonElement;
    btn.disabled = true; btn.textContent = "Запускаю…";
    try {
      const payload: Record<string, unknown> = {
        mode: "test", test_name: finalName,
        replay_start: toUTCISO(start), replay_end: end ? toUTCISO(end) : "",
        replay_log_persist: logdb,
      };
      if (preset) payload.preset = preset;
      await fetchJSON("/api/v1/bot/mode", { method: "POST", body: JSON.stringify(payload) });
      _setActiveModeBtn("test");
      hideModal("test-modal-overlay");
      await pollBotStatus();
      await pollTests();
    } catch (e) { alert("Ошибка запуска теста: " + e); }
    finally { btn.disabled = false; btn.textContent = "Запустить"; }
  });

  document.querySelectorAll("#bot-modal-overlay input[name=sl-mode]").forEach(r => {
    r.addEventListener("change", () => {
      const v = (r as HTMLInputElement).value;
      $("bs-atr-section")?.classList.toggle("bs-hidden", v !== "atr");
      $("bs-fixed-section")?.classList.toggle("bs-hidden", v !== "fixed");
    });
  });
  document.querySelectorAll("#bot-modal-overlay input[name=sl-source]").forEach(r => {
    r.addEventListener("change", _applySlSourceUI);
  });
  $("bs-save")?.addEventListener("click", async () => {
    const mode = (document.querySelector("#bot-modal-overlay input[name=sl-mode]:checked") as HTMLInputElement)?.value || "atr";
    const slSource = (document.querySelector("#bot-modal-overlay input[name=sl-source]:checked") as HTMLInputElement)?.value || "manual";
    const payload = {
      sl_mode: mode,
      sl_source: slSource,
      atr_period: Number(($("bs-atr-period") as HTMLInputElement)?.value || 14),
      atr_mult: Number(($("bs-atr-mult") as HTMLInputElement)?.value || 4),
      atr_rr: Number(($("bs-atr-rr") as HTMLInputElement)?.value || 4),
      sl_override: Number(($("bs-sl-override") as HTMLInputElement)?.value || 0),
      rr_override: Number(($("bs-rr-override") as HTMLInputElement)?.value || 0),
      fixed_sl: Number(($("bs-sl") as HTMLInputElement)?.value || 2.5),
      fixed_tp: Number(($("bs-tp") as HTMLInputElement)?.value || 2.5),
      top_n: Number(($("bs-topn") as HTMLInputElement)?.value || 20),
      commission: Number(($("bs-commission") as HTMLInputElement)?.value || 0.3),
      reentry_cooldown: Number(($("bs-reentry") as HTMLInputElement)?.value || 15),
      confirm_flip: Number(($("bs-confirm-flip") as HTMLInputElement)?.value || 2),
      quorum: Number(($("bs-quorum") as HTMLInputElement)?.value || 2),
    };
    const btn = $("bs-save") as HTMLButtonElement;
    btn.disabled = true; btn.textContent = "Сохранение...";
    try { await fetchJSON("/api/v1/bot/settings", { method: "POST", body: JSON.stringify(payload) }); hideModal("bot-modal-overlay"); }
    catch (e) { alert("Ошибка: " + e); }
    finally { btn.disabled = false; btn.textContent = "Сохранить"; }
  });

  startPolling();
}

function _applySlSourceUI(): void {
  const v = (document.querySelector("#bot-modal-overlay input[name=sl-source]:checked") as HTMLInputElement)?.value || "manual";
  $("bs-optuna-section")?.classList.toggle("bs-hidden", v !== "optuna");
  $("bs-atr-section")?.classList.toggle("bs-hidden", v !== "manual");
  const hint = $("bs-src-hint");
  if (hint) hint.textContent = v === "optuna"
    ? "Параметры берутся из optuna по каждому инструменту; override > 0 переопределяет."
    : "Глобальные множители из конфига (перекрывают per-ticker optuna).";
}

async function fillBotSettings(): Promise<void> {
  try {
    const c = await fetchJSON<Record<string, unknown>>("/api/v1/bot/ensemble");
    const set = (id: string, v: unknown) => {
      const el = $(id) as HTMLInputElement | null;
      if (el && v != null) el.value = String(v);
    };
    const src = c.sl_source === "optuna" ? "optuna" : "manual";
    const radio = document.querySelector(`#bot-modal-overlay input[name=sl-source][value="${src}"]`) as HTMLInputElement | null;
    if (radio) radio.checked = true;
    set("bs-sl-override", c.sl_override);
    set("bs-rr-override", c.rr_override);
    set("bs-atr-mult", c.sl_mult);
    set("bs-atr-rr", c.rr);
    set("bs-quorum", c.quorum);
    _applySlSourceUI();
  } catch { /* бот/конфиг недоступен — значения по умолчанию из разметки */ }
}

// ============ Пилы-фильтры бота → PATCH /api/v1/bot/config + слайдеры плеча/слота ============

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
function sendBotConfigPatch(extra?: Record<string, unknown>): void {
  const sessMap: Record<string, string> = {
    "sg-morning": "morning", "sg-day": "day", "sg-evening": "evening"
  };
  const sessKeys = ["sg-morning", "sg-day", "sg-evening"];
  const sessions = sessKeys.filter((x) => ($(x) as HTMLInputElement | null)?.checked ?? false).map((x) => sessMap[x]);
  const longOn = ($("dg-long") as HTMLInputElement | null)?.checked ?? true;
  const shortOn = ($("dg-short") as HTMLInputElement | null)?.checked ?? false;
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
  const _szPos = $("sz-pos") as HTMLInputElement | null;
  const _szMax = $("sz-max") as HTMLInputElement | null;
  const _szEq = $("sz-eq") as HTMLInputElement | null;
  const body: Record<string, unknown> = {
    sessions: sessions.length ? sessions : ["day"],
    ...(_szPos ? { pos_pct: Math.max(0.1, Math.min(1.0, Number(_szPos.value) / 100)) } : {}),
    ...(_szMax ? { max_positions: Math.max(1, Math.min(10, Number(_szMax.value))) } : {}),
    ...(_szEq ? { pos_equity_mult: Math.max(1, Math.min(6, Number(_szEq.value))) } : {}),
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

function applyChip(cb: HTMLInputElement | null, on: boolean): void {
  if (!cb) return;
  if (cb.checked !== on) {
    cb.checked = on;
    cb.closest("label")?.classList.toggle("on", on);
  }
}

function initSessChips(): void {
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
      const _sp = $("sz-pos") as HTMLInputElement | null;
      const _spl = $("sz-pos-label");
      const _spv = Math.round(Number((cfg as { pos_pct?: number }).pos_pct || 0.3) * 100);
      if (_sp && document.activeElement !== _sp) {
        _sp.value = String(Math.max(10, Math.min(100, _spv)));
        if (_spl) _spl.textContent = `${_sp.value}%`;
      }
      const _sm = $("sz-max") as HTMLInputElement | null;
      const _sml = $("sz-max-label");
      const _smv = Number((cfg as { max_positions?: number }).max_positions || 6);
      if (_sm && document.activeElement !== _sm) {
        _sm.value = String(Math.max(1, Math.min(10, _smv)));
        if (_sml) _sml.textContent = _sm.value;
      }
      const _se = $("sz-eq") as HTMLInputElement | null;
      const _sel = $("sz-eq-label");
      const _sev = Number((cfg as { pos_equity_mult?: number }).pos_equity_mult || 1);
      if (_se && document.activeElement !== _se) {
        _se.value = String(Math.max(1, Math.min(6, _sev)));
        if (_sel) _sel.textContent = `×${Number(_se.value)}`;
      }
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
      // Сверка с брокером.
      const rec = (cfg as { reconcile_enabled?: boolean }).reconcile_enabled;
      const recEl = $("rec-on") as HTMLInputElement | null;
      if (typeof rec === "boolean" && recEl) applyChip(recEl, rec);
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
  // Сверка с брокером (позиции/кэш) — только в торговое время.
  const recEl = $("rec-on") as HTMLInputElement | null;
  if (recEl) {
    let recSaved = true;
    try { recSaved = localStorage.getItem("bot_reconcile") !== "0"; } catch { recSaved = true; }
    recEl.checked = recSaved;
    recEl.closest("label")?.classList.toggle("on", recSaved);
    recEl.addEventListener("change", () => {
      recEl.closest("label")?.classList.toggle("on", recEl.checked);
      localStorage.setItem("bot_reconcile", recEl.checked ? "1" : "0");
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
  // Слайдеры слота и макс. позиций (в сайдбаре) → PATCH при отпускании.
  ($("sz-pos") as HTMLInputElement | null)?.addEventListener("input", () => {
    const v = ($("sz-pos") as HTMLInputElement).value;
    const l = $("sz-pos-label");
    if (l) l.textContent = `${v}%`;
  });
  ($("sz-pos") as HTMLInputElement | null)?.addEventListener("change", () => sendBotConfigPatch());
  ($("sz-max") as HTMLInputElement | null)?.addEventListener("input", () => {
    const v = ($("sz-max") as HTMLInputElement).value;
    const l = $("sz-max-label");
    if (l) l.textContent = v;
  });
  ($("sz-max") as HTMLInputElement | null)?.addEventListener("change", () => sendBotConfigPatch());
  ($("sz-eq") as HTMLInputElement | null)?.addEventListener("input", () => {
    const v = ($("sz-eq") as HTMLInputElement).value;
    const l = $("sz-eq-label");
    if (l) l.textContent = `×${Number(v)}`;
  });
  ($("sz-eq") as HTMLInputElement | null)?.addEventListener("change", () => sendBotConfigPatch());
}

// ============ Вкладка «Анализ»: статистика прогона, сравнение тестов, AI-гейт, движения ============

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escFlag(v: unknown): string {
  const t = typeof v === "object" && v !== null ? JSON.stringify(v) : String(v ?? "");
  return esc(t.slice(0, 18));
}

function _agEsc(s: unknown): string {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
}

let _ahDays: number = 5;
let _ahSort: 0 | 1 | 2 = 1; // 0 = порядок universe, 1 = падение→взлёт, 2 = взлёт→падение
let _ahMeta: boolean = true; // в ячейках показывать bias (сверху) и режим (снизу)
let _ahColor: "win" | "hour" | "day" = "win"; // цвет ячейки: от начала окна / часа / дня
let _ahBiasTf: "day" | "hour" = "day"; // bias сверху: дневной или часовой
let _ahTrades: boolean = true; // показывать сделки на heatmap (обводка входа + путь до выхода)

async function loadCompareList(): Promise<void> {
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

async function runCompare(): Promise<void> {
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

async function renderHeatmap(): Promise<void> {
  const el = $("an-heatmap");
  if (!el) return;
  try {
    const days = typeof _ahDays === "number" ? _ahDays : 5;
    const metaQ = _ahMeta ? "&meta=1" : "";
    const d = await fetch(`${API}/api/v1/bot/heatmap?days=${days}${metaQ}`)
      .then((r) => (r.ok ? r.json() : null)).catch(() => null) as
      { ok?: boolean; days?: number; count?: number; tickers?: Array<{ ticker?: string; figi?: string; bars?: Array<{ h: string; o?: number; c: number; b?: number; bh?: number; r?: string }> }> } | null;
    const tks = (d && d.tickers) || [];
    const trd = _ahTrades ? await fetch(`${API}/api/v1/sandbox/trades?limit=300`)
      .then((r) => (r.ok ? r.json() : null)).catch(() => null) : null;
    interface HmTrade { inH: string; outH: string | null; side: string; pnl: number | null; open: boolean; qty: number; }
    const tradesByTk = new Map<string, HmTrade[]>();
    if (trd && Array.isArray(trd.trades)) {
      for (const tr of trd.trades as Array<Record<string, unknown>>) {
        const tk = String(tr.ticker || "");
        if (!tk) continue;
        const inISO = String(tr.entry_time || "");
        if (!inISO) continue;
        const inH = inISO.slice(0, 13);
        const hasTs = !!tr.ts && String(tr.ts).length > 0;
        const open = !hasTs || tr.exit_price == null;
        const outH = hasTs ? String(tr.ts).slice(0, 13) : null;
        if (!tradesByTk.has(tk)) tradesByTk.set(tk, []);
        tradesByTk.get(tk)!.push({
          inH, outH,
          side: String(tr.side || "LONG").toUpperCase(),
          pnl: tr.net_pnl != null ? Number(tr.net_pnl) : null,
          open, qty: Number(tr.qty || 0),
        });
      }
    }
    if (!tks.length) {
      el.innerHTML = `<div class="dim">нет данных heatmap</div>`;
      return;
    }
    const hourSet = new Set<string>();
    for (const t of tks) for (const b of (t.bars || [])) hourSet.add(b.h);
    const hours = Array.from(hourSet).sort();
    // --- Сделки на heatmap: обводка входа, путь до выхода (или до текущего часа у открытых) ---
    const _winEnd = hours.length ? hours[hours.length - 1].slice(0, 13) : "";
    const tradesSpan = new Map<string, Set<string>>(); // tk → часы, покрытые сделкой
    const tradesIn = new Map<string, Set<string>>();    // tk → часы входа
    const tradesOut = new Map<string, Set<string>>();   // tk → часы выхода
    const tradesInfo = new Map<string, Map<string, HmTrade[]>>(); // tk → час(входа) → сделки (тултип)
    if (_ahTrades) {
      for (const [tk, arr] of tradesByTk) {
        for (const tr of arr) {
          const h0 = tr.inH;
          const h1 = tr.open ? _winEnd : (tr.outH || tr.inH);
          const inc = new Set<string>();
          for (const h of hours) {
            const hk = h.slice(0, 13);
            if (hk >= h0 && hk <= h1) inc.add(hk);
          }
          if (!inc.size) continue;
          if (!tradesSpan.has(tk)) { tradesSpan.set(tk, new Set()); tradesIn.set(tk, new Set()); tradesOut.set(tk, new Set()); tradesInfo.set(tk, new Map()); }
          for (const hk of inc) tradesSpan.get(tk)!.add(hk);
          if (inc.has(h0)) tradesIn.get(tk)!.add(h0);
          if (tr.outH && tr.outH !== tr.inH && inc.has(tr.outH)) tradesOut.get(tk)!.add(tr.outH);
          const im = tradesInfo.get(tk)!;
          if (!im.has(h0)) im.set(h0, []);
          im.get(h0)!.push({ ...tr });
        }
      }
    }
    const _f = new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    const _day = new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow", weekday: "short", day: "2-digit", month: "2-digit" });
    const pctByTk = new Map<string, Map<string, number>>();
    const pctHByTk = new Map<string, Map<string, number>>();
    const pctDByTk = new Map<string, Map<string, number>>();
    const closeByTk = new Map<string, Map<string, number>>();
    const openByTk = new Map<string, Map<string, number>>();
    const metaByTk = new Map<string, Map<string, { b?: number; bh?: number; r?: string }>>();
    let maxAbs = 0.01;
    let maxAbsHour = 0.01;
    let maxAbsDay = 0.01;
    for (const t of tks) {
      const tk = String(t.ticker || "?");
      const pm = new Map<string, number>();
      const ph = new Map<string, number>();
      const pd = new Map<string, number>();
      const cm = new Map<string, number>();
      const om = new Map<string, number>();
      const mm = new Map<string, { b?: number; bh?: number; r?: string }>();
      const bars = t.bars || [];
      const first = bars[0]?.c;
      const dayBase = new Map<string, number>();
      for (const b of bars) {
        cm.set(b.h, b.c);
        if (typeof b.o === "number") om.set(b.h, b.o);
        if (typeof b.b === "number" || typeof b.bh === "number" || b.r) mm.set(b.h, { b: b.b, bh: b.bh, r: b.r });
        if (first) {
          const p = ((b.c / first) - 1) * 100;
          pm.set(b.h, p);
          if (Math.abs(p) > maxAbs) maxAbs = Math.abs(p);
        }
        if (typeof b.o === "number" && b.o > 0) {
          const phh = ((b.c / b.o) - 1) * 100;
          ph.set(b.h, phh);
          if (Math.abs(phh) > maxAbsHour) maxAbsHour = Math.abs(phh);
        }
        const dk = _day.format(new Date(b.h));
        if (!dayBase.has(dk)) dayBase.set(dk, b.c);
      }
      for (const b of bars) {
        const base = dayBase.get(_day.format(new Date(b.h)));
        if (base) {
          const pdd = ((b.c / base) - 1) * 100;
          pd.set(b.h, pdd);
          if (Math.abs(pdd) > maxAbsDay) maxAbsDay = Math.abs(pdd);
        }
      }
      closeByTk.set(tk, cm);
      openByTk.set(tk, om);
      pctByTk.set(tk, pm);
      pctHByTk.set(tk, ph);
      pctDByTk.set(tk, pd);
      if (mm.size) metaByTk.set(tk, mm);
    }
    // Итог окна по каждой акции: % последнего доступного бара.
    const totByTk = new Map<string, number>();
    for (const t of tks) {
      const tk = String(t.ticker || "?");
      const pm = pctByTk.get(tk);
      const bars = t.bars || [];
      const lastH = bars[bars.length - 1]?.h;
      if (pm && lastH) {
        const v = pm.get(lastH);
        if (v != null) totByTk.set(tk, v);
      }
    }
    const sorted = tks.slice();
    if (_ahSort !== 0) {
      sorted.sort((a, b) => {
        const ta = totByTk.get(String(a.ticker || "?")) ?? 0;
        const tb = totByTk.get(String(b.ticker || "?")) ?? 0;
        return _ahSort === 1 ? ta - tb : tb - ta;
      });
    }
    const _col = (p: number, ma: number): string => {
      const a = Math.min(1, Math.abs(p) / (ma * 0.6));
      return p >= 0 ? `rgba(0,170,90,${(0.10 + 0.9 * a).toFixed(3)})` : `rgba(225,60,60,${(0.10 + 0.9 * a).toFixed(3)})`;
    };
    const _biasSym = (v?: number): { s: string; cls: string } =>
      v === 1 ? { s: "BUY", cls: "up" } : v === -1 ? { s: "SELL", cls: "dn" } : { s: "·", cls: "z" };
    const _regSym = (r?: string): { s: string; cls: string } => {
      switch (r) {
        case "HIGH_VOLATILITY": return { s: "VOL", cls: "vol" };
        case "TREND_UP": return { s: "TRUP", cls: "tu" };
        case "TREND_DOWN": return { s: "TRDN", cls: "td" };
        case "RANGE": return { s: "RNG", cls: "rng" };
        default: return { s: "NTR", cls: "nt" };
      }
    };
    const _px = (v?: number): string => {
      if (v == null) return "·";
      return v >= 1000 ? String(Math.round(v)) : v.toFixed(2);
    };
    const rowsDesc = hours.slice().reverse();
    const ma = _ahColor === "hour" ? maxAbsHour : _ahColor === "day" ? maxAbsDay : maxAbs;
    const _colorLbl = _ahColor === "win" ? "окно" : _ahColor === "hour" ? "час" : "день";
    let html = `<div class="an-ag-head">📉 Heatmap: ${tks.length} акций · шаг 1 час · окно ${days}д · цвет: от начала ${_colorLbl}${_ahMeta ? ` · bias ${_ahBiasTf === "hour" ? "часовой" : "дневной"}(верх)+режим(низ)` : ""}</div>`;
    html += `<div class="ah-btns">${[1, 3, 5].map((x) => `<button class="btn-secondary btn-xs${x === days ? " active" : ""}" data-ah-days="${x}">${x}д</button>`).join("")}` +
      `<button class="btn-secondary btn-xs${_ahSort !== 0 ? " active" : ""}" data-ah-sort title="сортировка колонок по итогу окна">${_ahSort === 2 ? "взлёт→падение" : _ahSort === 1 ? "падение→взлёт" : "порядок"}</button>` +
      `<button class="btn-secondary btn-xs" data-ah-color title="цвет ячейки: % от начала окна / от начала часа / от начала дня">цвет: ${_colorLbl}</button>` +
      `<button class="btn-secondary btn-xs${_ahMeta ? " active" : ""}" data-ah-meta title="в каждой ячейке: сверху bias, снизу режим">bias+режим</button>` +
      `<button class="btn-secondary btn-xs${_ahBiasTf === "hour" ? " active" : ""}" data-ah-bias title="какой bias показывать сверху: дневной или часовой">bias: ${_ahBiasTf === "day" ? "день" : "час"}</button>` +
      `<button class="btn-secondary btn-xs${_ahTrades ? " active" : ""}" data-ah-trades title="показывать сделки: обводка входа ● и путь ▍ до выхода">🫰 сделки</button></div>`;
    html += `<div class="ah-scroll"><table class="ah${_ahMeta ? " ah-meta" : ""}"><thead><tr><th class="ah-corner">час</th>`;
    for (const t of sorted) {
      const tk = String(t.ticker || "?");
      const tot = totByTk.get(tk);
      const totS = tot == null ? "" : (tot >= 0 ? ` (+${tot.toFixed(1)}%)` : ` (${tot.toFixed(1)}%)`);
      html += `<th class="ah-h" title="${_agEsc(`${tk} итог за ${days}д:${totS}`)}">${_agEsc(t.ticker)}</th>`;
    }
    html += `</tr></thead><tbody>`;
    let curDay = "";
    for (const h of rowsDesc) {
      const day = _day.format(new Date(h));
      if (day !== curDay) {
        curDay = day;
        html += `<tr class="ah-day"><td colspan="${sorted.length + 1}">${_agEsc(day)}</td></tr>`;
      }
      html += `<tr><td class="ah-hh">${_f.format(new Date(h))}</td>`;
      for (const t of sorted) {
        const tk = String(t.ticker || "?");
        const c = closeByTk.get(tk)?.get(h);
        const pWin = pctByTk.get(tk)?.get(h);
        // Сделки могут заходить и в пустые ячейки (нет бара) — рисуем маркер и на них.
        const hK = h.slice(0, 13);
        const isSpan = _ahTrades && tradesSpan.get(tk)?.has(hK);
        const isIn = _ahTrades && tradesIn.get(tk)?.has(hK);
        const isOut = _ahTrades && tradesOut.get(tk)?.has(hK);
        let trCls = "";
        if (isSpan || isIn || isOut) {
          trCls = " ah-tr";
          if (isIn) trCls += " ah-tr-in";
          if (isOut) trCls += " ah-tr-out";
          if (isSpan && !isIn && !isOut) trCls += " ah-tr-path";
          // Для открытых позиций путь пунктиром.
          if (isSpan && !isOut) {
            const im = tradesInfo.get(tk)?.get(hK);
            if (im?.some((x) => x.open)) trCls += " ah-tr-open";
          }
        }
        let trTtl = "";
        if (isIn) {
          const im = tradesInfo.get(tk)?.get(hK) || [];
          if (im.length) {
            trTtl = im.map((x) =>
              `${x.side} ${x.qty}шт · вход ${hK}:59` +
              (x.open ? " · ⭳ открыта" : ` → выход ${x.outH || "?"} · ${x.pnl != null ? (x.pnl >= 0 ? "+" : "") + Math.round(x.pnl) + "₽" : "?"}`)
            ).join("\n");
          }
        }
        if (c == null || pWin == null) {
          const cls = `ah-e${trCls}`;
          html += `<td class="${cls}"${trTtl ? ` title="${_agEsc(trTtl)}"` : ""}></td>`;
          continue;
        }
        let pUse = pWin;
        if (_ahColor === "hour") pUse = pctHByTk.get(tk)?.get(h) ?? pWin;
        else if (_ahColor === "day") pUse = pctDByTk.get(tk)?.get(h) ?? pWin;
        const m = metaByTk.get(tk)?.get(h);
        let ttl = `${tk} · ${_f.format(new Date(h))} · ${c.toFixed(2)}₽ · окно ${pWin >= 0 ? "+" : ""}${pWin.toFixed(1)}%` +
          (m ? ` · ${_colorLbl} ${pUse >= 0 ? "+" : ""}${pUse.toFixed(1)}% · bias-д ${m.b ?? 0}` +
            ` · bias-ч ${m.bh ?? 0} · режим ${m.r ?? "—"}` : "");
        if (trTtl) ttl += `\n— Сделка —\n${trTtl}`;
        const cls = `ah-c${trCls}`;
        if (_ahMeta && m) {
          const bs = _biasSym(_ahBiasTf === "hour" ? m.bh : m.b);
          const rs = _regSym(m.r);
          html += `<td class="${cls}" style="background:${_col(pUse, ma)}" title="${_agEsc(ttl)}">` +
            `<span class="ah-op">${_px(openByTk.get(tk)?.get(h))}</span>` +
            `<span class="ah-bi ${bs.cls}">${bs.s}</span><span class="ah-re ${rs.cls}">${rs.s}</span></td>`;
        } else {
          html += `<td class="${cls}" style="background:${_col(pUse, ma)}" title="${_agEsc(ttl)}"></td>`;
        }
      }
      html += `</tr>`;
    }
    html += `</tbody></table></div>`;
    el.innerHTML = html;
    el.querySelectorAll<HTMLButtonElement>("[data-ah-days]").forEach((b) => {
      b.addEventListener("click", () => {
        _ahDays = Number(b.dataset.ahDays) || 5;
        void renderHeatmap();
      });
    });
    el.querySelectorAll<HTMLButtonElement>("[data-ah-sort]").forEach((b) => {
      b.addEventListener("click", () => {
        _ahSort = (_ahSort + 1) % 3 as 0 | 1 | 2;
        void renderHeatmap();
      });
    });
    el.querySelectorAll<HTMLButtonElement>("[data-ah-meta]").forEach((b) => {
      b.addEventListener("click", () => {
        _ahMeta = !_ahMeta;
        void renderHeatmap();
      });
    });
    el.querySelectorAll<HTMLButtonElement>("[data-ah-color]").forEach((b) => {
      b.addEventListener("click", () => {
        _ahColor = _ahColor === "win" ? "hour" : _ahColor === "hour" ? "day" : "win";
        void renderHeatmap();
      });
    });
    el.querySelectorAll<HTMLButtonElement>("[data-ah-bias]").forEach((b) => {
      b.addEventListener("click", () => {
        _ahBiasTf = _ahBiasTf === "day" ? "hour" : "day";
        void renderHeatmap();
      });
    });
    el.querySelectorAll<HTMLButtonElement>("[data-ah-trades]").forEach((b) => {
      b.addEventListener("click", () => {
        _ahTrades = !_ahTrades;
        void renderHeatmap();
      });
    });
  } catch { /* ignore */ }
}

async function renderStats(): Promise<void> {
  // Строка размера слота (pos_pct) — на странице «Анализ».
  try {
    const [rc, rs] = await Promise.all([
      fetch(`${API}/api/v1/bot/config`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
      fetch(`${API}/api/v1/bot/status`).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]);
    const el = $("an-sizing");
    if (el && rc) {
      const ls = (rs && rs.long_short) || {};
      const rcc = rc as { pos_pct?: number; max_positions?: number | null; max_exposure_pct?: number };
      el.textContent = `слот: ${Math.round((Number(rcc.pos_pct) || 0.4) * 100)}% от EQ` +
        ` · макс позиций: ${rcc.max_positions ?? "—"}` +
        ` · кап экспозиции: ${Math.round((Number(rcc.max_exposure_pct) || 1) * 100)}%` +
        ` · L/S: ${(ls as { longs?: number | null; shorts?: number | null }).longs ?? "—"}/${(ls as { longs?: number | null; shorts?: number | null }).shorts ?? "—"}`;
    }
  } catch { /* ignore */ }
  // Блок движений: срезы по горизонтам (1д/1н/1м/3м).
  try {
    const mvEl = $("an-movers");
    if (mvEl) {
      const mv = await fetch(`${API}/api/v1/screener/movers?top=4`)
        .then((r) => (r.ok ? r.json() : null)).catch(() => null);
      const mvD = mv as { ok?: boolean; horizons?: Record<string, { n?: number; up?: Array<{ ticker?: string; chg?: number; streak?: number }>; down?: Array<{ ticker?: string; chg?: number; streak?: number }>; counts?: Record<string, [number, number] | undefined> }> } | null;
      if (mvD && mvD.ok && mvD.horizons) {
        const fmt = (arr: Array<{ ticker?: string; chg?: number; streak?: number }> | undefined) => (arr || []).map((x) =>
          `<span class="mv-tk">${_agEsc(x.ticker)}</span> <span class="${(x.chg ?? 0) >= 0 ? "mv-up" : "mv-dn"}">${(x.chg ?? 0) > 0 ? "+" : ""}${_agEsc(x.chg)}%</span>` +
          ((x.streak ?? 0) ? ` <span class="dim">${x.streak}д</span>` : "")).join("<br>");
        const rows = Object.entries(mvD.horizons).map(([lbl, h]) => {
          const c = h.counts || {};
          const cnt = `≥5%: ${c["5"]?.[0] ?? 0}/${c["5"]?.[1] ?? 0}<br>≥10%: ${c["10"]?.[0] ?? 0}/${c["10"]?.[1] ?? 0}<br>≥20%: ${c["20"]?.[0] ?? 0}/${c["20"]?.[1] ?? 0}`;
          return `<tr><td class="mv-h">${_agEsc(lbl)}</td><td>${fmt(h.up) || "—"}</td>` +
                 `<td>${fmt(h.down) || "—"}</td><td class="dim num">${cnt}</td></tr>`;
        }).join("");
        mvEl.innerHTML = `<div class="an-ag-head">📊 Движения · рост/падение по горизонтам (${mvD.horizons["1д"]?.n ?? 0} тикеров)</div>` +
          `<table class="an-table"><thead><tr><th>Горизонт</th><th>▲ Рост</th><th>▼ Падение</th>` +
          `<th class="num">шт (рост/пад)</th></tr></thead><tbody>${rows}</tbody></table>`;
      }
    }
  } catch { /* ignore */ }

  // Heatmap: все акции universe по X, время (час) по Y, цвет = накопленное падение/взлёт.
  await renderHeatmap();

  // Блок портфеля: экспозиции/сектора/маржа/стресс.
  try {
    const pfEl = $("an-portfolio");
    if (pfEl) {
      const pf = await fetch(`${API}/api/v1/bot/portfolio_summary`)
        .then((r) => (r.ok ? r.json() : null)).catch(() => null);
      const pfD = pf as {
        equity?: number; sector_pct?: Record<string, number>; stress_pct?: Record<string, number>;
        skip_counts?: Record<string, number>; regime?: { state?: string; pct_20m?: number | null };
        queue?: Array<{ ticker?: string; score?: number; factors?: Record<string, unknown>; veto?: string[] }>;
        rank?: { enabled?: boolean; top_n?: number; explore?: number; top?: string[] };
        dd?: { peak?: number; dd_pct?: number }; long_exposure_pct?: number; short_exposure_pct?: number;
        net_exposure_pct?: number; gross_leverage?: number; margin_use_pct?: number; margin_free?: number;
      } | null;
      if (pfD && pfD.equity != null) {
        const sec = Object.entries(pfD.sector_pct || {}).sort((a, b) => b[1] - a[1]).slice(0, 4)
          .map(([k, v]) => `${k} ${Math.round(Number(v) * 100)}%`).join(", ");
        const st = pfD.stress_pct || {};
        const RU: Record<string, string> = {
          portfolio_limit: "портфель", max_positions: "слоты", max_exposure: "экспозиция",
          ls_balance: "L/S", ai_reject: "AI-отказ", ai_reject_cooldown: "AI-кулдаун",
          ai_already_pending: "AI-очередь", imoex_guard: "IMOEX", loss_streak_hold: "серия убытков",
          already_held: "уже в позиции", cooldown: "кулдаун", session_filter: "сессия",
          trend_alignment: "тренд", entries_paused: "пауза", long_disabled: "лонги off",
          short_disabled: "шорты off", opposite_hold: "противоположная",
        };
        const skips = Object.entries(pfD.skip_counts || {})
          .sort((a, b) => b[1] - a[1]).slice(0, 5)
          .map(([k, v]) => `${RU[k] || k} ${v}`).join(" · ");
        const rg = pfD.regime || {};
        const _rgRu: Record<string, string> = { bear: "🐻 bear", bull: "🐂 bull",
                                                reversal: "⚠ разворот", neutral: "→ нейтраль" };
        const rgTxt = rg.state
          ? ` · режим: ${_rgRu[rg.state] || rg.state}${rg.pct_20m != null ? ` (20м ${rg.pct_20m >= 0 ? "+" : ""}${Number(rg.pct_20m).toFixed(2)}%)` : ""}`
          : "";
        const qn = (pfD.queue || []).length;
        const q0 = pfD.queue?.[0];
        const qf = q0?.factors || {};
        const qTop = qn
          ? ` · очередь: ${qn} (топ ${q0?.ticker} ${Math.round(q0?.score || 0)}` +
            ` · hist ${_agEsc(qf.hist)} rs ${_agEsc(qf.rs)} conf ${_agEsc(qf.conf)} liq ${_agEsc(qf.liq)} fit ${_agEsc(qf.fit)})` +
            (q0?.veto?.length ? ` ⛔${q0.veto.join(",")}` : "")
          : "";
        const rk = pfD.rank || {};
        const rkTxt = rk.enabled && rk.top
          ? ` · рейтинг: топ-${rk.top_n} (разведка ${rk.explore}) — ${rk.top.slice(0, 5).join(", ")}`
          : "";
        const dd = pfD.dd || {};
        const ddTxt = dd.peak ? ` · просадка ${Math.round((dd.dd_pct || 0) * 1000) / 10}% от пика` : "";
        const cell = (v: string, cls = "") => `<td class="${cls}">${v}</td>`;
        const row2 = (a: string, b: string, c2 = "", d = "") =>
          `<tr>${cell(a, "mv-h")}${cell(b)}${cell(c2)}${cell(d, "dim")}</tr>`;
        pfEl.innerHTML =
          `<div class="an-ag-head">📦 Портфель · equity ${Math.round(pfD.equity || 0)}₽</div>` +
          `<table class="an-table"><tbody>` +
          row2("Экспозиция", `L ${Math.round((pfD.long_exposure_pct || 0) * 100)}% · ` +
            `S ${Math.round((pfD.short_exposure_pct || 0) * 100)}% · net ${Math.round((pfD.net_exposure_pct || 0) * 100)}%`,
            `gross ×${pfD.gross_leverage ?? "—"}`) +
          row2("Маржа", `${Math.round((pfD.margin_use_pct || 0) * 100)}% · свободно ${Math.round(pfD.margin_free || 0)}₽`,
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
      const stD = st as {
        days?: number; totals?: { impact?: number; saved?: number; missed?: number; n?: number; approve?: number; reject?: number; skip?: number; agree?: number; disagree?: number };
        by_provider?: Record<string, { n?: number; approve?: number; reject?: number; saved?: number; missed?: number; lat?: number }>;
        top_missed?: Array<{ ticker?: string; missed?: number }>; top_saved?: Array<{ ticker?: string; saved?: number }>;
      } | null;
      if (stD && stD.totals) {
        const t = stD.totals;
        const provs = Object.entries(stD.by_provider || {}).map(([p, v]) =>
          `<span class="an-ag-prov"><b>${_agEsc(p)}</b>: n=${v.n} · ✓${v.approve}/✕${v.reject} · saved ${money(v.saved || 0)}₽ / missed ${money(v.missed || 0)}₽ · ${Math.round(v.lat || 0)}мс</span>`).join("");
        const tm = (stD.top_missed || []).map((x) => `${_agEsc(x.ticker)} −${money(x.missed || 0)}₽`).join(", ");
        const tsv = (stD.top_saved || []).map((x) => `${_agEsc(x.ticker)} +${money(x.saved || 0)}₽`).join(", ");
        ag.innerHTML =
          `<div class="an-ag-head">🤖 AI-гейт (${stD.days ?? 7} дн): <b class="${(t.impact ?? 0) >= 0 ? "pos" : "neg"}">${(t.impact ?? 0) >= 0 ? "+" : ""}${money(t.impact || 0)}₽</b>` +
          ` · сэкономлено ${money(t.saved || 0)}₽ · упущено ${money(t.missed || 0)}₽` +
          ` · решений ${t.n ?? 0} (✓${t.approve ?? 0} ✕${t.reject ?? 0} …${t.skip ?? 0})` +
          ` · согласие 🤝${t.agree ?? 0}/≠${t.disagree ?? 0}</div>` +
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
      const rpD = rp as {
        model?: string; now_msk?: string; usage?: { total?: number; prompt?: number; completion?: number; estimated?: boolean };
        analysis?: string; suggestions?: unknown[]; actions?: Array<{ action?: string; ticker?: string; side?: string }>;
      } | null;
      if (rpD && rpD.analysis) {
        const sugg = (rpD.suggestions || []).map((x) => `<li>${_agEsc(String(x))}</li>`).join("");
        const acts = (rpD.actions || []).map((a) =>
          `<span class="ar-act">${_agEsc(a.action || "")} ${_agEsc(a.ticker || "")}` +
          `${a.side ? " " + _agEsc(a.side) : ""}</span>`).join(" ");
        const u = rpD.usage || {};
        const uTxt = u.total ? ` · ~${(Number(u.total) / 1000).toFixed(1)}k токенов` +
          ` (prompt ${(Number(u.prompt) / 1000).toFixed(1)}k + out ${(Number(u.completion) / 1000).toFixed(1)}k${u.estimated ? ", оценка" : ""})` : "";
        ar.innerHTML = `<div class="an-ag-head">🧠 AI о рынке <span class="dim">${_agEsc(rpD.model || "")}` +
          `${rpD.now_msk ? " · " + _agEsc(rpD.now_msk) : ""}${uTxt}</span></div>` +
          `<div class="ar-text">${_agEsc(rpD.analysis)}</div>` +
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
  const SEC_MAX = 12;
  const sec = (title: string, rows: StatsRow[] | undefined) => {
    if (!rows || !rows.length) return "";
    const _rest = Math.max(0, rows.length - SEC_MAX);
    const trs = rows.slice(0, SEC_MAX).map((r) =>
      `<tr><td>${esc(r.key)}</td><td class="n">${r.trades}</td><td class="n">${r.wr}%</td>` +
      `<td class="n" style="color:${r.net >= 0 ? "var(--up)" : "var(--down)"}">${r.net >= 0 ? "+" : ""}${r.net}</td>` +
      `<td class="n">${r.pf ?? "—"}</td></tr>`).join("");
    return `<div class="stats-sec"><div class="stats-title">${title}` +
      ` <span class="dim">${rows.length}${_rest ? `, ещё ${_rest}` : ""}</span></div>` +
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

let railInit = false;
// ─── Правый сайдбар «Рынок · Голоса»: скринер + карусель (порт из оригинала) ───
interface SrItem { figi: string; ticker: string; name: string; price: number | null; turnover: number | null; rng_pct: number | null; in_universe: boolean; lot?: number }
interface SrUniInfo { atr_pct?: number }
interface SrVoteInfo { votes: number; side: string; regime: string | null; buy: number; sell: number; vol_abs: number | null }
interface SrRow extends SrItem { atr: number | null; votes: number; side: string; regime: string | null; buy: number; sell: number; vol_abs: number | null }
interface CarouselStatus {
  bot_running?: boolean;
  eligible_count?: number;
  active_count?: number;
  pending?: Array<{ ticker: string; candle_count?: number; need_download?: boolean }>;
  log?: Array<{ ts: string; action: string; msg: string }>;
}

let _srRows: SrItem[] = [];
let _srUniverse = new Map<string, SrUniInfo>();
let _srVotes = new Map<string, SrVoteInfo>();
let _srSortKey = (localStorage.getItem("deeptrading_sr_sort") as string) || "turnover";
let _srSortAsc = localStorage.getItem("deeptrading_sr_sort_asc") === "1";
let _srUnivOnly = localStorage.getItem("deeptrading_sr_univ") === "1";
let _srQuery = localStorage.getItem("deeptrading_sr_q") ?? "";
let _srMinTurnoverM = Number(localStorage.getItem("deeptrading_sr_min_t") ?? 0) || 0;
let _srLoading = false;
const _srPrevPrice = new Map<string, number>();

function _srCmpNum(v: number | null | undefined): number { return v == null ? -Infinity : v; }

function fmtTimeOnly(iso?: string): string {
  try { return new Date(iso || Date.now()).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Europe/Moscow" }); }
  catch { return ""; }
}

const _SR_REG_RANK: Record<string, number> = { HIGH_VOLATILITY: 4, TREND_UP: 3, TREND_DOWN: 2, RANGE: 1, NEUTRAL: 0 };

function _srCompare(a: SrRow, b: SrRow): number {
  const k = _srSortKey;
  if (k === "ticker") return _srSortAsc ? a.ticker.localeCompare(b.ticker, "ru") : b.ticker.localeCompare(a.ticker, "ru");
  if (k === "name") return _srSortAsc ? a.name.localeCompare(b.name, "ru") : b.name.localeCompare(a.name, "ru");
  if (k === "atr_pct") { const x = _srCmpNum(a.atr), y = _srCmpNum(b.atr); return _srSortAsc ? x - y : y - x; }
  if (k === "votes") { const x = _srCmpNum(a.votes), y = _srCmpNum(b.votes); return _srSortAsc ? x - y : y - x; }
  if (k === "regime") {
    const x = a.regime ? (_SR_REG_RANK[a.regime] ?? -1) : -Infinity;
    const y = b.regime ? (_SR_REG_RANK[b.regime] ?? -1) : -Infinity;
    return _srSortAsc ? x - y : y - x;
  }
  const x = _srCmpNum(a[k as keyof SrItem] as number | null);
  const y = _srCmpNum(b[k as keyof SrItem] as number | null);
  return _srSortAsc ? x - y : y - x;
}

const _logoFail = new Set<string>();
declare global {
  interface Window { __logoFailed?: (safe: string) => void }
}
window.__logoFailed = (safe: string): void => { _logoFail.add(safe); };
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
  return `<span class="tick-logo${name}"><img src="${img}" alt="" onload="this.style.opacity=1" onerror="this.style.display='none';this.nextElementSibling&&(this.nextElementSibling.style.display='flex');window.__logoFailed&&window.__logoFailed('${safe}')"><i>${letter}</i></span>`;
}

function renderSrTable(): void {
  const tbody = $("sr-tbody");
  if (!tbody) return;
  const enriched: SrRow[] = _srRows.map((r) => {
    const u = _srUniverse.get(r.figi);
    const v = _srVotes.get(r.figi);
    return {
      ...r,
      atr: u?.atr_pct ?? null,
      votes: v?.votes ?? 0,
      side: v?.side ?? "",
      regime: v?.regime ?? null,
      buy: v?.buy ?? 0,
      sell: v?.sell ?? 0,
      vol_abs: v?.vol_abs ?? null,
    };
  });
  const q = _srQuery.trim().toLowerCase();
  const minT = _srMinTurnoverM * 1e6;
  const filtered = enriched.filter((r) => {
    if (_srUnivOnly && !_srUniverse.has(r.figi)) return false;
    if (minT > 0 && _srCmpNum(r.turnover) < minT) return false;
    if (q && !r.ticker.toLowerCase().includes(q) && !r.name.toLowerCase().includes(q)) return false;
    return true;
  });
  const sorted = filtered.sort(_srCompare);
  const regShort: Record<string, string> = { HIGH_VOLATILITY: "Волат", TREND_UP: "Тренд↑", TREND_DOWN: "Тренд↓", RANGE: "Флэт", NEUTRAL: "Нейтр" };
  const fmtVol = (x: number | null | undefined) => (x == null ? "—" : x >= 1000 ? `${(x / 1000).toFixed(1)}k` : `${Math.round(x)}`);
  tbody.innerHTML = sorted
    .map((r) => {
      const prev = _srPrevPrice.get(r.ticker);
      const flash = prev != null && r.price != null && prev > 0
        ? (r.price > prev ? " sr-up" : r.price < prev ? " sr-down" : "")
        : "";
      const arrows = prev != null && r.price != null && prev > 0
        ? (r.price > prev ? " ▲" : r.price < prev ? " ▼" : "")
        : "";
      const inU = _srUniverse.has(r.figi);
      const rowCls = inU ? " sr-universe" : "";
      const priceTxt = r.price != null ? money(r.price) + " ₽" : "—";
      const turnTxt = r.turnover != null ? "₽" + money(r.turnover) : "—";
      const volTxt = r.rng_pct != null ? r.rng_pct.toFixed(2) + "%" : "—";
      const atrTxt = r.atr != null ? r.atr.toFixed(2) + "%" : "—";
      const votesCell = r.votes > 0
        ? `<span class="sv-chip sv-${Math.min(r.votes, 3)}" title="Голоса: BUY ${r.buy} / SELL ${r.sell} · Режим ${r.regime || "—"} · Объём ${fmtVol(r.vol_abs)}">${r.side === "BUY" ? "▲" : "▼"} ${r.votes}</span>`
        : inU ? `<span class="sr-dim">·</span>`
        : `<span class="sr-dim">—</span>`;
      const reg = r.regime ? (regShort[r.regime] || r.regime) : null;
      const regTxt = reg ?? "—";
      const regCol = r.regime === "HIGH_VOLATILITY" ? "var(--gold)"
        : r.regime === "TREND_UP" ? "var(--up)"
        : r.regime === "TREND_DOWN" ? "var(--down)"
        : "var(--text-dim)";
      const rowTitle = inU
        ? `${r.name} · Рынок бота${r.regime ? ` · режим ${r.regime}` : ""} — клик откроет график`
        : `${r.name} — клик откроет график`;
      return `<tr class="sr-row${rowCls}"${inU ? ` data-figi="${r.figi}" data-ticker="${r.ticker}"` : ""} title="${rowTitle}">` +
        `<td class="ticker-cell">${tickerLogo(r.ticker, r.name)}<span class="ticker-txt">${r.ticker}</span></td>` +
        `<td class="sr-num${flash}">${r.price != null ? priceTxt + arrows : `<span class="sr-dim">—</span>`}</td>` +
        `<td class="sr-num">${r.turnover != null ? turnTxt : `<span class="sr-dim">—</span>`}</td>` +
        `<td class="sr-num">${r.rng_pct != null ? `<span style="color:${(r.rng_pct ?? 0) >= 3 ? "var(--gold)" : "var(--text)"}">${volTxt}</span>` : `<span class="sr-dim">—</span>`}</td>` +
        `<td class="sr-num">${r.atr != null ? `<span style="color:${r.atr >= 0.3 ? "var(--gold)" : "var(--text)"}">${atrTxt}</span>` : `<span class="sr-dim">—</span>`}</td>` +
        `<td class="sr-num">${votesCell}</td>` +
        `<td class="sr-num">${reg ? `<span class="sr-reg" style="color:${regCol}">${regTxt}</span>` : `<span class="sr-dim">${regTxt}</span>`}</td>` +
        `<td class="sr-num sr-col-action"><button class="${r.in_universe ? "sr-toggle sr-toggle-active" : "sr-toggle"}" data-ticker="${r.ticker}" title="${r.in_universe ? "Убрать из карусели" : "Добавить в карусель"}">${r.in_universe ? "●" : "○"}</button></td>` +
        `</tr>`;
    })
    .join("") || `<tr><td colspan="8" class="sr-empty">ничего не найдено</td></tr>`;
  const countEl = $("sr-count");
  if (countEl) countEl.textContent = `${sorted.length} / ${_srRows.length}`;
  const tsEl = $("sr-ts");
  if (tsEl) tsEl.textContent = `обнов. ${fmtTimeOnly()}`;
  document.querySelectorAll<HTMLElement>("#sr-table thead th[data-sort]").forEach((el) => {
    const key = el.dataset.sort;
    el.classList.toggle("sorted", key === _srSortKey);
    const arrow = el.querySelector(".sr-arrow");
    if (arrow) arrow.textContent = key === _srSortKey ? (_srSortAsc ? "▲" : "▼") : "";
  });
  tbody.querySelectorAll<HTMLElement>("tr").forEach((row) => {
    row.addEventListener("click", () => {
      const figi = row.dataset.figi;
      if (!figi) return;
      try { localStorage.setItem("deeptrading_chart_figi", figi); } catch { }
      // График открываем во вкладке «Бот» (iframe), как в оригинале, а не в отдельной page-chart.
      focusBotChart({ figi, ticker: row.dataset.ticker ?? "", trade: null, trades: [] });
      showPage("bot");
    });
  });
  tbody.querySelectorAll<HTMLButtonElement>(".sr-toggle").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const ticker = btn.dataset.ticker;
      const r = _srRows.find((x) => x.ticker === ticker);
      if (!ticker || !r) return;
      btn.disabled = true;
      try {
        if (r.in_universe) {
          await fetchJSON<{ ok: boolean }>(`/api/v1/screener/eligible/${encodeURIComponent(ticker)}`, { method: "DELETE" });
        } else {
          await fetchJSON<{ ok: boolean }>("/api/v1/screener/eligible", { method: "POST", body: JSON.stringify({ ticker }) });
        }
        void loadScreener(true);
      } catch (err) { alert("Ошибка: " + err); }
      finally { btn.disabled = false; }
    });
  });
}

const _SR_ACT_COLORS: Record<string, string> = {
  add: "#7ed67e",
  remove: "#ff6b6b",
  ready: "#9dbdff",
};

function renderCarouselStatus(c: CarouselStatus | undefined): void {
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
        `<span class="sr-cs-tag${p.need_download ? " sr-cs-tag-wait" : ""}" title="${p.candle_count ?? 0} свечей 1min">${p.ticker} <i>${p.candle_count ?? 0}</i></span>`
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

async function loadScreener(silent = false): Promise<void> {
  if (_srLoading) return;
  _srLoading = true;
  try {
    const d = await fetchJSON<{ count: number; items: SrItem[]; carousel: CarouselStatus }>("/api/v1/screener");
    _srRows = d.items || [];
    renderSrTable();
    renderCarouselStatus(d.carousel);
    _srPrevPrice.clear();
    for (const r of _srRows) if (r.price != null) _srPrevPrice.set(r.ticker, r.price);
  } catch {
    if (!silent && _srRows.length === 0) {
      const tbody = $("sr-tbody");
      if (tbody) tbody.innerHTML = `<tr><td colspan="8" class="sr-empty">ошибка загрузки рынка</td></tr>`;
    }
    const tsEl = $("sr-ts");
    if (tsEl) tsEl.textContent = `обнов. ${fmtTimeOnly()} (устарело)`;
  } finally {
    _srLoading = false;
  }
}

function initRightRail(): void {
  if (railInit) { loadScreener(); return; }
  railInit = true;
  document.querySelectorAll<HTMLElement>(".sr-tab").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll<HTMLElement>(".sr-tab").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll(".sr-tabpane").forEach(p => p.classList.add("hidden"));
      const pane = $(`sr-pane-${btn.dataset.srtab}`);
      pane?.classList.remove("hidden");
      if (btn.dataset.srtab === "aigate") {
        const ag = document.getElementById("ag-list");
        if (ag) {
          const saved = Number(localStorage.getItem("agListHeight"));
          if (saved > 0) {
            const maxH = (pane ? pane.clientHeight : window.innerHeight) - 220;
            ag.style.height = `${Math.max(60, Math.min(saved, Math.max(80, maxH)))}px`;
          }
        }
        void renderAiGate();
      }
    });
  });
  $("sr-refresh")?.addEventListener("click", () => void loadScreener());
  $("votes-config-btn")?.addEventListener("click", () => $("votes-editor")?.classList.toggle("hidden"));
  const q = $("sr-query") as HTMLInputElement | null;
  if (q) {
    q.value = _srQuery;
    let tId: ReturnType<typeof setTimeout> | undefined;
    q.addEventListener("input", () => {
      clearTimeout(tId);
      tId = setTimeout(() => { _srQuery = q.value; localStorage.setItem("deeptrading_sr_q", _srQuery); renderSrTable(); }, 250);
    });
  }
  const minT = $("sr-min-t") as HTMLInputElement | null;
  if (minT) {
    minT.value = String(_srMinTurnoverM);
    minT.addEventListener("change", () => { _srMinTurnoverM = Number(minT.value) || 0; localStorage.setItem("deeptrading_sr_min_t", String(_srMinTurnoverM)); renderSrTable(); });
  }
  const uni = $("sr-univ-only") as HTMLInputElement | null;
  if (uni) {
    uni.checked = _srUnivOnly;
    uni.addEventListener("change", () => { _srUnivOnly = uni.checked; localStorage.setItem("deeptrading_sr_univ", _srUnivOnly ? "1" : "0"); renderSrTable(); });
  }
  document.querySelectorAll<HTMLElement>("#sr-table thead th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort || "turnover";
      if (_srSortKey === key) _srSortAsc = !_srSortAsc;
      else { _srSortKey = key; _srSortAsc = key === "ticker" || key === "name"; }
      localStorage.setItem("deeptrading_sr_sort", _srSortKey);
      localStorage.setItem("deeptrading_sr_sort_asc", _srSortAsc ? "1" : "0");
      renderSrTable();
    });
  });
  loadScreener();
}

let aigateInit = false;
let _aiPromptKind: "gate" | "watch" | "trader" = "gate";
let _aiControlCache: { prompts?: Record<string, string>; note?: string; defaults?: Record<string, { system?: string }> } | null = null;

function agEsc(s: unknown): string {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
}

function agOpenChart(ticker: string, opts?: { sl?: number; tp?: number }): void {
  const sr = _srRows.find((x) => x.ticker === ticker);
  const figi = sr?.figi;
  if (!figi) { void agOpenChartLazy(ticker, opts); return; }
  agOpenApplyChart(sr.figi, ticker, opts);
}

async function agOpenChartLazy(ticker: string, opts?: { sl?: number; tp?: number }): Promise<void> {
  try {
    const d = await fetch(`${API}/api/v1/instruments?search=${encodeURIComponent(ticker)}&limit=2`)
      .then((r) => (r.ok ? r.json() : null)).catch(() => null);
    const items: Array<{ ticker: string; figi: string }> = (d && d.items) || [];
    const it = items.find((x) => x.ticker === ticker) ?? items[0];
    if (it) agOpenApplyChart(it.figi, ticker, opts);
  } catch { /* ignore */ }
}

function agOpenApplyChart(figi: string, ticker: string, opts?: { sl?: number; tp?: number }): void {
  focusBotChart({
    figi,
    ticker,
    trade: opts && (opts.sl != null || opts.tp != null)
      ? { entry_price: 0, stop_loss: opts.sl, take_profit: opts.tp }
      : null,
    trades: [],
  });
  // подсветить строку в таблицах основной части (позиции/сделки)
  for (const sel of ["#bot-positions-table tbody", "#bot-trades-table tbody"]) {
    document.querySelectorAll<HTMLTableRowElement>(`${sel} tr[data-figi]`).forEach((tr) => {
      if (tr.dataset.figi !== figi) return;
      tr.classList.remove("ag-highlight");
      void tr.offsetWidth;
      tr.classList.add("ag-highlight");
      tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }
}

async function renderAiGate(): Promise<void> {
  const list = $("ag-list");
  const sub = $("ag-sub");
  const summ = $("ag-summary");
  if (!list) return;
  let approvals: { enabled?: boolean; contour?: string; timeout_sec?: number; default?: string; pending?: Array<{ ticker: string; side: string }> } | null = null;
  let decs: Array<Record<string, unknown>> = [];
  let notes: Array<Record<string, unknown>> = [];
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
  const _contour = approvals && approvals.contour ? ` · ${String(approvals.contour)}` : "";
  if (sub) sub.textContent = (approvals && approvals.enabled ? (shadow ? "shadow (не применяет)" : "боевой") : "выключен") + _contour;
  if (summ) {
    if (!approvals) {
      summ.textContent = "нет данных";
    } else {
      const pend = (approvals.pending || []) as Array<{ ticker: string; side: string }>;
      summ.innerHTML = `таймаут ${Math.round(Number(approvals.timeout_sec) || 0)}с · default <b>${agEsc(approvals.default)}</b> · ожидают: <b>${pend.length}</b>` +
        (pend.length ? " · " + pend.map((p) => `${agEsc(p.ticker)} ${agEsc(p.side)}`).join(", ") : "");
    }
  }
  if (!decs.length) {
    list.innerHTML = '<span class="ag-empty">нет решений</span>';
  } else {
    list.innerHTML = decs.slice().reverse().map((d) => {
      const dec = String(d.decision || "").toLowerCase();
      const cls = dec === "approve" ? "approve" : dec === "reject" ? "reject" : "skip";
      const label = dec === "approve" ? "✓ одобрил" : dec === "reject" ? "✕ отклонил" : "… пропуск";
      const t = d.ts ? new Date(String(d.ts)).toLocaleTimeString("ru-RU", { hour12: false }) : "";
      const conf = d.confidence != null ? ` · conf ${Number(d.confidence).toFixed(2)}` : "";
      const lat = d.latency_ms != null ? ` · ${Math.round(Number(d.latency_ms))}мс` : "";
      const prov = d.provider ? ` · ${agEsc(d.provider)}` : "";
      const agree = d.agreement === true ? " · 🤝" : (d.agreement === false ? " · ≠" : "");
      const applied = d.applied ? " · применено" : (d.shadow ? " · shadow" : "");
      const toks = d.usage && (d.usage as { total?: number }).total ? ` · ${(Number((d.usage as { total?: number }).total) / 1000).toFixed(1)}k tok` : "";
      return `<div class="ag-row ${cls}${d.shadow ? " shadow" : ""}" data-ticker="${agEsc(d.ticker)}" title="${agEsc(d.reason)}">
        <div class="ag-r1">
          <span class="ag-t">${agEsc(t)}</span>
          <span class="ag-tk">${agEsc(d.ticker)} ${agEsc(d.side)}${d.qty ? " ×" + agEsc(d.qty) : ""}</span>
          <span class="ag-dec">${label}</span>
        </div>
        <div class="ag-r2">${agEsc(d.reason)}<span class="ag-meta">${agEsc(d.model || "")}${prov}${conf}${lat}${toks}${agree}${applied}</span></div>
        ${d.advice ? `<div class="ag-advice" title="совет нейросети">💡 ${agEsc(d.advice)}</div>` : ""}
      </div>`;
    }).join("");
  }
  // 👉 клик по решению открывает график в основной части
  list.querySelectorAll<HTMLElement>(".ag-row[data-ticker]").forEach((row) => {
    row.addEventListener("click", () => agOpenChart(String(row.dataset.ticker || "")));
  });
  // Вахтёр позиций: заметки (hold/tighten/close) — словами, без управления.
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
      const tightLabel = (n: Record<string, unknown>): string => {
        const apd = ((n.applied_levels as { applied?: { sl?: number; tp?: number } } | undefined)?.applied) || {};
        const hasSl = apd.sl != null, hasTp = apd.tp != null;
        if (hasSl && hasTp) return "🔧 SL+TP";
        if (hasTp) return "🔧 TP";
        if (hasSl) return "🔧 SL";
        return "🔧 подтянуть";
      };
      notesEl.innerHTML = notes.slice().reverse().map((n) => {
        const act = String(n.action || "watch").toLowerCase();
        const t = n.ts ? new Date(String(n.ts)).toLocaleTimeString("ru-RU", { hour12: false }) : "";
        const dsl = n.dist_sl_atr != null ? ` · ${Number(n.dist_sl_atr).toFixed(2)} ATR до SL` : "";
        const dtp = n.dist_tp_atr != null ? ` · ${Number(n.dist_tp_atr).toFixed(2)} ATR до TP` : "";
        const pnl = n.pnl != null ? ` · P&L ${Number(n.pnl) >= 0 ? "+" : ""}${Math.round(Number(n.pnl))}₽` : "";
        return `<div class="ag-note ${act}" data-ticker="${agEsc(n.ticker)}" title="${agEsc(n.note)}">
          <div class="ag-r1"><span class="ag-t">${agEsc(t)}</span><span class="ag-tk">${agEsc(n.ticker)} ${agEsc(n.side)}</span><span class="ag-dec">${act === "tighten" ? tightLabel(n) : (actLabel[act] || act)}</span></div>
          <div class="ag-r2">${agEsc(n.note)}<span class="ag-meta">${agEsc(n.model || "")}${dsl}${dtp}${pnl}</span></div>
          ${n.advice ? `<div class="ag-advice">💡 ${agEsc(n.advice)}</div>` : ""}
        </div>`;
      }).join("");
      // 👉 клик по заметке открывает график тикера (со SL/TP, если позиция есть)
      notesEl.querySelectorAll<HTMLElement>(".ag-note[data-ticker]").forEach((row) => {
        row.addEventListener("click", () => agOpenChart(String(row.dataset.ticker || "")));
      });
    }
  }
}

async function loadAiPrompt(): Promise<void> {
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

function renderAiPromptEditor(): void {
  const d = _aiControlCache || {};
  const ed = $("ag-prompt-edit") as HTMLTextAreaElement | null;
  if (!ed) return;
  const ov = String(((d.prompts || {})[_aiPromptKind]) || "");
  const def = String((((d.defaults || {})[_aiPromptKind]) || {}).system || "");
  ed.value = ov.trim() ? ov : def;
  const info = $("ag-prompt-info");
  if (info) {
    const who = { gate: "🚦 гейт", watch: "👁 вахтёр", trader: "🤖 трейдер" }[_aiPromptKind] || "";
    info.textContent = `${who} · ${ov.trim() ? "переопределён (UI)" : "дефолт воркера"}` +
      ` · ${ed.value.length} симв`;
  }
  const inp = $("ag-send-input") as HTMLTextAreaElement | null;
  if (inp && document.activeElement !== inp) inp.value = String(d.note || "");
  document.querySelectorAll<HTMLElement>("#ag-prompt .ag-ptab").forEach((b) => {
    b.classList.toggle("active", b.dataset.kind === _aiPromptKind);
  });
}

async function saveAiPrompt(kind: string, value: string): Promise<void> {
  const st = $("ag-prompt-status");
  try {
    const r = await fetch(`${API}/api/v1/bot/ai_control`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompts: { [kind]: value } }),
    });
    if (st) st.textContent = r.ok ? "✓ сохранено" : "ошибка сохранения";
    if (r.ok && _aiControlCache) {
      _aiControlCache.prompts = _aiControlCache.prompts || {};
      _aiControlCache.prompts[kind] = value;
    }
  } catch {
    if (st) st.textContent = "ошибка сети";
  }
  setTimeout(() => { if (st) st.textContent = ""; }, 2500);
}

async function saveAiNote(value: string): Promise<void> {
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

function initAgResizer(): void {
  const list = document.getElementById("ag-list");
  const resizer = document.getElementById("ag-resizer");
  if (!list || !resizer) return;
  const pane = document.getElementById("sr-pane-aigate");
  const clamp = (h: number): number => {
    const base = (pane && !pane.classList.contains("hidden")) ? pane.clientHeight : window.innerHeight;
    const maxH = base - 220;
    return Math.max(60, Math.min(h, Math.max(80, maxH)));
  };
  const saved = Number(localStorage.getItem("agListHeight"));
  if (saved > 0) list.style.height = `${clamp(saved)}px`;
  let startY = 0;
  let startH = 0;
  const onMove = (e: PointerEvent): void => {
    list.style.height = `${clamp(startH + (e.clientY - startY))}px`;
  };
  const onUp = (): void => {
    resizer.classList.remove("dragging");
    localStorage.setItem("agListHeight", String(list.clientHeight));
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
  };
  resizer.addEventListener("pointerdown", (e: PointerEvent) => {
    e.preventDefault();
    resizer.classList.add("dragging");
    startY = e.clientY;
    startH = list.clientHeight;
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
}

function initAIgate(): void {
  if (aigateInit) { void renderAiGate(); void loadAiPrompt(); return; }
  aigateInit = true;
  $("ag-refresh")?.addEventListener("click", () => { void renderAiGate(); void loadAiPrompt(); });
  $("ag-prompt-btn")?.addEventListener("click", () => $("ag-prompt")?.classList.toggle("hidden"));
  document.querySelectorAll<HTMLElement>("#ag-prompt .ag-ptab").forEach((b) => {
    b.addEventListener("click", () => {
      const kind = b.dataset.kind as "gate" | "watch" | "trader";
      if (kind) { _aiPromptKind = kind; renderAiPromptEditor(); }
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
    const pane = $("sr-pane-aigate");
    pane?.classList.toggle("fullscreen");
    document.body.style.overflow = pane?.classList.contains("fullscreen") ? "hidden" : "";
  });
  initAgResizer();
  void renderAiGate();
  void loadAiPrompt();
  // Автообновление AI-гейта, пока вкладка открыта (решения/заметки приходят живьём).
  window.setInterval(() => {
    const pane = $("sr-pane-aigate");
    if (pane && !pane.classList.contains("hidden")) {
      void renderAiGate();
      void loadAiPrompt();
    }
  }, 10000);
}

function initSidebarResize(): void { bindSidebarDrag("sidebar", 1, 190, 560, "deeptrading_sidebar_w"); }

function initRightRailResize(): void { bindSidebarDrag("sidebar-right", -1, 240, 460, "deeptrading_rail_w"); }

function bindSidebarDrag(elId: string, dir: number, min: number, max: number, key: string): void {
  const sb = document.getElementById(elId);
  if (!sb || sb.dataset.resizeInit) return;
  sb.dataset.resizeInit = "1";
  const ckey = elId === "sidebar" ? "deeptrading_sidebar_collapsed" : "deeptrading_rail_collapsed_v2";
  try {
    const saved = localStorage.getItem(key);
    if (saved) {
      const w = Math.max(min, Math.min(max, Number(saved)));
      if (w > 0) { sb.style.width = w + "px"; sb.style.flexBasis = w + "px"; }
    }
  } catch { }
  let dragging = false; let startX = 0; let startW = 0;
  const onMove = (e: PointerEvent) => { if (!dragging) return; const w = Math.max(min, Math.min(max, startW + dir * (e.clientX - startX))); sb.style.width = `${w}px`; sb.style.flexBasis = `${w}px`; };
  const onUp = () => { if (!dragging) return; dragging = false; document.body.classList.remove("sb-resizing"); window.removeEventListener("pointermove", onMove); window.removeEventListener("pointerup", onUp); try { localStorage.setItem(key, String(Math.round(sb.getBoundingClientRect().width))); } catch { } };
  sb.addEventListener("pointerdown", (e) => {
    const t = e.target as HTMLElement;
    if (t.closest(".sidebar-collapse")) return;
    const collapsed = sb.classList.contains("collapsed");
    if (!collapsed && !t.closest(".sidebar-grip")) return;
    e.preventDefault();
    sb.classList.remove("collapsed");
    try { localStorage.setItem(ckey, "0"); } catch { }
    dragging = true; startX = e.clientX; startW = sb.getBoundingClientRect().width;
    document.body.classList.add("sb-resizing");
    window.addEventListener("pointermove", onMove); window.addEventListener("pointerup", onUp);
  });
}

function initSidebarCollapse(): void {
  const binds: ReadonlyArray<readonly [string, string, string]> = [
    ["sidebar", "sidebar-collapse", "deeptrading_sidebar_collapsed"],
    ["sidebar-right", "rail-collapse", "deeptrading_rail_collapsed_v2"],
  ];
  for (const [elId, btnId, key] of binds) {
    const el = document.getElementById(elId);
    const btn = document.getElementById(btnId);
    if (!el || !btn) continue;
    const apply = (collapsed: boolean): void => {
      el.classList.toggle("collapsed", collapsed);
      try { localStorage.setItem(key, collapsed ? "1" : "0"); } catch { }
    };
    let collapsed = false;
    try { collapsed = localStorage.getItem(key) === "1"; } catch { }
    apply(collapsed);
    btn.addEventListener("click", () => apply(!el.classList.contains("collapsed")));
  }
}

function initBotChartResizer(): void {
  const wrap = document.getElementById("bot-chart-wrap");
  const resizer = document.getElementById("bot-resizer");
  if (!wrap || !resizer) return;
  if (resizer.dataset.init) return;
  resizer.dataset.init = "1";
  const KEY = "deeptrading_bot_chart_h";
  try { const saved = localStorage.getItem(KEY); if (saved) { const h = Math.max(240, Math.min(window.innerHeight - 160, Number(saved))); wrap.style.height = `${h}px`; } } catch { }
  let dragging = false; let startY = 0; let startH = 0;
  const onMove = (e: PointerEvent) => { if (!dragging) return; const h = Math.max(240, Math.min(window.innerHeight - 160, startH + (startY - e.clientY))); wrap.style.height = `${h}px`; };
  const onUp = () => { if (!dragging) return; dragging = false; resizer.classList.remove("dragging"); window.removeEventListener("pointermove", onMove); window.removeEventListener("pointerup", onUp); try { localStorage.setItem(KEY, String(wrap.clientHeight)); } catch { } };
  resizer.addEventListener("pointerdown", (e) => { e.preventDefault(); dragging = true; startY = e.clientY; startH = wrap.clientHeight; resizer.classList.add("dragging"); window.addEventListener("pointermove", onMove); window.addEventListener("pointerup", onUp); });
}

// ─── Embedded-режим (iframe ?embedded=1): только график, без шапки/сайдбаров ───

interface EmbedTrade { side?: string; entry_time?: string; exit_time?: string; }
interface FocusMsg {
  type?: string;
  figi?: string;
  ticker?: string;
  trade?: { entry_price?: number; exit_price?: number; stop_loss?: number; take_profit?: number } | null;
  trades?: EmbedTrade[];
  centerSec?: number;
  halfBars?: number;
}

interface SandboxTrade {
  figi: string; ticker: string; side: string; qty: number;
  entry_price: number; exit_price: number | null;
  entry_time: string; ts: string | null; stop_loss: number | null; take_profit: number | null;
  net_pnl: number | null; commission: number; exit_reason: string; meta?: string | null;
  exit_meta?: string | null;
  entry_reason?: string | null;
  strategy_id?: string | null;
  regime?: string | null;
  leverage?: number | null; notional?: number | null; own_money?: number | null;
  max_pnl?: number | null; max_pnl_time?: string | null; max_pnl_price?: number | null;
  max_pnl_atr_pct?: number | null; max_pnl_atr?: number | null; max_pnl_r?: number | null; max_pnl_roi_pct?: number | null;
  max_pnl_mae_atr?: number | null;
  max_pnl_mae_pct?: number | null;
  trail_info?: TrailInfo | null;
  sl_atr?: number | null; tp_atr?: number | null; rr_initial?: number | null;
  roi_sl_pct?: number | null; roi_tp_pct?: number | null;
}

interface TrailInfo {
  activated?: boolean;
  trail_stop?: number | null;
  trail_dist_atr?: number | null;
  hit_time?: string | null;
  hit_price?: number | null;
  hit_reason?: string | null;
  hit_pnl?: number | null;
  hit_r?: number | null;
  hit_roi_pct?: number | null;
}
const _REGIME_SHORT: Record<string, string> = {
  HIGH_VOLATILITY: "волат", TREND_UP: "тренд↑", TREND_DOWN: "тренд↓",
  RANGE: "флэт", NEUTRAL: "нейтр", NO_REGIME: "нет",
};
function _regimeShort(reg: string): string {
  const r = String(reg || "").trim();
  if (!r) return "";
  return _REGIME_SHORT[r] || r.replace(/_/g, " ").toLowerCase();
}

let _lastTrades: SandboxTrade[] = [];
let _lastTradeHist = new Map<string, Array<Record<string, unknown>>>();
const _openDetails = new Set<string>();

let emChart: IChartApi | null = null;
let emSeries: ISeriesApi<"Candlestick"> | null = null;
let emMarkers: ISeriesMarkersPluginApi<Time> | null = null;
let emLines: IPriceLine[] = [];
let emMarkersSig: ISeriesMarkersPluginApi<Time> | null = null;
let emVolume: ISeriesApi<"Histogram"> | null = null;
let emSma: ISeriesApi<"Line"> | null = null;
let emEma: ISeriesApi<"Line"> | null = null;
let emBbU: ISeriesApi<"Line"> | null = null;
let emBbL: ISeriesApi<"Line"> | null = null;
let emMacdHist: ISeriesApi<"Histogram"> | null = null;
let emMacdLine: ISeriesApi<"Line"> | null = null;
let emSigLine: ISeriesApi<"Line"> | null = null;
let emRsi: ISeriesApi<"Line"> | null = null;
let emOscPane = false;
let lastAnalysis: AnalysisDto | null = null;
const _IV_SEC: Record<string, number> = { "1min": 60, "5min": 300, "15min": 900, hour: 3600, day: 86400 };
let _plottedFigi = "";
let _priceScaleFigi = "";
let _overlaySig = "";
let chartToolbarInit = false;
let indicators = loadIndicatorState();
let chartInterval = "1min";
let lastFocus: FocusMsg | null = null;
let _lastFocusKey = "";

// ——— Стабилизация графика: режимы, координатор запросов, снапшот вьюпорта ———

type ChartMode = "LIVE_FOLLOW" | "USER_EXPLORE" | "TRADE_FOCUS";
let chartMode: ChartMode = "LIVE_FOLLOW";

let _chartReqId = 0;
let _chartAbort: AbortController | null = null;
let _loadInFlight = false;

// Что сейчас реально нарисовано — нужно, чтобы решать setData vs update()
let _applied: { figi: string; interval: string; firstTime: number; length: number } | null = null;

function _candleSig(c: { ts: string; open: number; high: number; low: number; close: number; volume: number }): string {
  return `${c.ts}|${c.open}|${c.high}|${c.low}|${c.close}|${c.volume}`;
}

function _isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

// Снапшот вьюпорта ДО изменения данных: после setData индексы/logical уже другие.
type ViewportSnap = { range: { from: Time; to: Time } | null; following: boolean };

function captureViewport(): ViewportSnap {
  if (!emChart) return { range: null, following: true };
  try {
    const lg = emChart.timeScale().getVisibleLogicalRange();
    const len = lastAnalysis?.candles.length ?? 0;
    const following = lg != null && lg.to >= len - 5 && lg.to >= 0;
    return { range: emChart.timeScale().getVisibleRange(), following };
  } catch {
    return { range: null, following: true };
  }
}

function restoreViewport(snap: ViewportSnap): void {
  if (!emChart) return;
  try {
    if (snap.following) { emChart.timeScale().scrollToRealTime(); return; }
    if (snap.range) { emChart.timeScale().setVisibleRange(snap.range); return; }
    emChart.timeScale().fitContent();
  } catch { /* noop */ }
}

function _setChartMode(next: ChartMode): void {
  if (chartMode === next) return;
  chartMode = next;
  console.debug("[chart] mode=" + next);
}

// Дефолтное окно: последние 6 часов, а не все 2000 баров. Раньше fitContent()
// впихивал в экран ~2.5 суток — свечи становились в пиксель, и минутные бары
// визуально читались как 5-минутные.
const DEFAULT_VIEW_HOURS = 6;

function _applyLastWindow(times: ReadonlyArray<number | UTCTimestamp>, ivSec: number): void {
  if (!emChart) return;
  if (!times.length) { emChart.timeScale().fitContent(); return; }
  const bars = Math.max(30, Math.round((DEFAULT_VIEW_HOURS * 3600) / ivSec));
  const from = times[Math.max(0, times.length - bars)] as UTCTimestamp;
  const to = times[times.length - 1] as UTCTimestamp;
  emChart.timeScale().setVisibleRange({ from, to });
}

// Логарифмическая шкала цен (включена по умолчанию — при смене акций с разной
// ценой 200₽→2500₽ и картинка, и скачки визуально сглаживаются).
let priceScaleLog = true;
{
  try { priceScaleLog = localStorage.getItem("deeptrading_price_scale_log") !== "0"; } catch { }
}

function loadIndicatorState(): { sma: boolean; ema: boolean; bb: boolean; rsi: boolean; macd: boolean } {
  const fallback = { sma: true, ema: false, bb: false, rsi: false, macd: true };
  try {
    const raw = localStorage.getItem("deeptrading_indicators");
    if (!raw) return fallback;
    return { ...fallback, ...(JSON.parse(raw) as Partial<typeof fallback>) };
  } catch { return fallback; }
}

// ——— Мост родитель↔iframe графика ———
// Единая точка входа: любой выбор пользователя (позиция/сделка/скринер/AI-gate)
// обязан идти через focusBotChart(), иначе автофокус по positions вернёт график
// на прежний тикер, а refresh по SL/TP перезагрузит свечи без нужды.
let _botFrameReady = false;
let _botPendingFocus: FocusMsg | null = null;
let _botBridgeInit = false;

function _postToBotFrame(msg: FocusMsg): void {
  const frame = document.getElementById("bot-chart-frame") as HTMLIFrameElement | null;
  if (!frame?.contentWindow) return;
  frame.contentWindow.postMessage(msg, "*");
}

function focusSignature(msg: FocusMsg): string {
  const t = msg.trade ?? null;
  const ts = (msg.trades ?? []).map((x) => `${x.side}@${x.entry_time}`).join(",");
  return `${msg.ticker ?? ""}|${t?.entry_price ?? ""}|${t?.stop_loss ?? ""}|${t?.take_profit ?? ""}|${ts}`;
}

function focusBotChart(msg: Omit<FocusMsg, "type">): void {
  const full: FocusMsg = { type: "focus", ...msg };
  _botFocusFigi = msg.figi ?? null;
  _botFocusSig = focusSignature(full);
  _botPendingFocus = full;
  if (!_botFrameReady) return; // до chart-ready держим в очереди
  _postToBotFrame(full);
}

// Только линии входа/SL/TP и маркеры — без перезапроса свечей.
function overlayBotChart(msg: Omit<FocusMsg, "type">): void {
  if (!_botFrameReady) return;
  _postToBotFrame({ ...msg, type: "overlay" });
}

function initBotChartBridge(): void {
  if (_botBridgeInit) return;
  _botBridgeInit = true;
  window.addEventListener("message", (e: MessageEvent) => {
    const d = e.data as { type?: string } | null;
    if (!d || d.type !== "chart-ready") return;
    _botFrameReady = true;
    if (_botPendingFocus) _postToBotFrame(_botPendingFocus);
  });
}

function emTimeLabel(t: unknown): string {
  let d: Date;
  if (typeof t === "number") d = new Date(t * 1000);
  else if (t && typeof t === "object") {
    const b = t as { year: number; month: number; day: number };
    d = new Date(Date.UTC(b.year, b.month - 1, b.day));
  } else return String(t);
  return d.toLocaleString("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function emTickMark(t: unknown): string {
  // Тики по шкале времени: lightweight-charts рисует их из UTC — конвертим в МСК.
  if (t === " " || t === "," || t == null) return String(t ?? "");
  if (typeof t === "number") {
    return new Date(t * 1000).toLocaleString("ru-RU", { timeZone: "Europe/Moscow", hour: "2-digit", minute: "2-digit" });
  }
  if (typeof t === "object") {
    const b = t as { year: number; month: number; day: number };
    return new Date(Date.UTC(b.year, b.month - 1, b.day))
      .toLocaleString("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit" });
  }
  return String(t);
}

function emEpoch(iso?: string): number | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime();
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

function emCandleTimes(a: AnalysisDto): UTCTimestamp[] {
  return a.candles.map((c) => Math.floor(new Date(c.ts).getTime() / 1000) as UTCTimestamp);
}

function currentViewState(): { centerSec?: number; halfBars?: number } {
  if (!emChart) return {};
  const vr = emChart.timeScale().getVisibleRange();
  if (!vr) return {};
  const from = typeof vr.from === "number" ? vr.from : null;
  const to = typeof vr.to === "number" ? vr.to : null;
  if (from == null || to == null) return {};
  const lr = emChart.timeScale().getVisibleLogicalRange();
  const halfBars = lr ? Math.max(20, Math.round(Math.max(1, lr.to - lr.from) / 2)) : 50;
  return { centerSec: Math.floor((from + to) / 2), halfBars };
}

// Текущая видимая ширина в барах — её сохраняем при переходе к сделке,
// чтобы клик по строке сдвигал камеру, а не менял масштаб.
function _visibleBarSpan(): number | null {
  if (!emChart) return null;
  try {
    const lg = emChart.timeScale().getVisibleLogicalRange();
    if (!lg) return null;
    const span = lg.to - lg.from;
    return span >= 2 ? span : null;
  } catch { return null; }
}

function _applyViewCenter(centerSec?: number, halfBars?: number, times?: number[], ivSec = 60): void {
  if (!emChart) return;
  const tsArr = times ?? (lastAnalysis ? emCandleTimes(lastAnalysis) : []);
  if (!tsArr.length) { emChart.timeScale().fitContent(); return; }
  if (centerSec == null) { _applyLastWindow(tsArr, ivSec); return; }
  let best = 0; let bd = Infinity;
  for (let i = 0; i < tsArr.length; i++) {
    const d = Math.abs(tsArr[i] - centerSec);
    if (d < bd) { bd = d; best = i; }
  }
  // Раньше здесь стоял хардкод half=20 баров: клик по сделке сужал окно до 40 минут
  // вместо дефолтных 6 часов — это и был «дикий зум». Теперь приоритет:
  // текущая видимая ширина → msg.halfBars (смена ТФ) → дефолтные 6 часов.
  const defaultHalf = Math.max(1, Math.round((DEFAULT_VIEW_HOURS * 3600) / ivSec / 2));
  const span = _visibleBarSpan();
  const half = Math.max(1, halfBars != null ? Math.round(halfBars) : (span != null ? Math.round(span / 2) : defaultHalf));
  const last = tsArr.length - 1;
  let from: number; let to: number;
  if (best + half <= last) {
    // Сделка не у правого края — держим по центру, пустого поля справа нет.
    const f = best - half;
    from = f >= 0 ? tsArr[f] : Math.floor(tsArr[0] - (half - f) * ivSec);
    to = tsArr[best + half];
  } else {
    // Сделка у правого края данных — окно прижимаем влево, сохраняя ту же ширину.
    to = tsArr[last];
    from = tsArr[Math.max(0, last - half * 2)];
  }
  emChart.timeScale().setVisibleRange({ from: from as UTCTimestamp, to: to as UTCTimestamp });
}

function _applySeriesData(a: AnalysisDto, opts?: { incremental?: boolean }): void {
  if (!emChart || !emSeries || !emVolume) return;
  const times = emCandleTimes(a);
  if (!times.length) return;
  const n = a.candles.length;
  // Инкрементально только если окно не сдвинулось (первый бар тот же) —
  // тогда новые бары можно дописать update(), не перерисовывая 2000 точек.
  const inc = !!opts?.incremental && !!_applied
    && _applied.figi === (a.figi ?? "") && _applied.interval === chartInterval
    && _applied.firstTime === times[0] && n >= _applied.length;
  const from = inc ? Math.max(0, _applied!.length - 1) : 0;
  if (!inc) {
    emSeries.setData(a.candles.map((c, i) => ({
      time: times[i], open: c.open, high: c.high, low: c.low, close: c.close,
    })));
    emVolume.setData(a.candles.map((c, i) => ({
      time: times[i], value: c.volume,
      color: c.close >= c.open ? "rgba(38,166,154,0.45)" : "rgba(239,83,80,0.45)",
    })));
  } else {
    for (let i = from; i < n; i++) {
      const c = a.candles[i];
      emSeries.update({ time: times[i], open: c.open, high: c.high, low: c.low, close: c.close });
      emVolume.update({
        time: times[i], value: c.volume,
        color: c.close >= c.open ? "rgba(38,166,154,0.45)" : "rgba(239,83,80,0.45)",
      });
    }
  }
  const linePts = (arr: Array<number | null> | undefined): LineData[] => {
    if (!arr) return [];
    const out: LineData[] = [];
    for (let i = 0; i < n; i++) {
      const v = arr[i];
      if (v != null) out.push({ time: times[i], value: v });
    }
    return out;
  };
  const updLine = (s: ISeriesApi<"Line"> | null, arr: Array<number | null> | undefined): void => {
    if (!s) return;
    if (!inc) { s.setData(linePts(arr)); return; }
    for (let i = from; i < n; i++) {
      const v = arr?.[i];
      if (v != null) s.update({ time: times[i], value: v });
    }
  };
  updLine(emSma, a.sma20);
  updLine(emEma, a.ema50);
  updLine(emBbU, a.bb_upper);
  updLine(emBbL, a.bb_lower);
  const macdHist: HistogramData[] = [];
  const macdLine: LineData[] = [];
  const signalLine: LineData[] = [];
  const rsiLine: LineData[] = [];
  for (let i = 0; i < n; i++) {
    const m = a.macd.macd[i];
    if (m != null) macdLine.push({ time: times[i], value: m });
    const sg = a.macd.signal[i];
    if (sg != null) signalLine.push({ time: times[i], value: sg });
    const h = a.macd.hist[i];
    if (h != null) {
      macdHist.push({ time: times[i], value: h, color: h >= 0 ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)" });
    }
    const rv = a.rsi?.[i];
    if (rv != null) rsiLine.push({ time: times[i], value: rv });
  }
  const updMacdLine = (s: ISeriesApi<"Line"> | null, pts: LineData[]): void => {
    if (!s) return;
    if (!inc) { s.setData(pts); return; }
    for (let i = from; i < n; i++) {
      const m = a.macd.macd[i];
      if (m != null) s.update({ time: times[i], value: m });
    }
  };
  const updSigLine = (s: ISeriesApi<"Line"> | null): void => {
    if (!s) return;
    if (!inc) { s.setData(signalLine); return; }
    for (let i = from; i < n; i++) {
      const sg = a.macd.signal[i];
      if (sg != null) s.update({ time: times[i], value: sg });
    }
  };
  const updHist = (s: ISeriesApi<"Histogram"> | null, get: (i: number) => number | null): void => {
    if (!s) return;
    if (!inc) { s.setData(macdHist); return; }
    for (let i = from; i < n; i++) {
      const h = get(i);
      if (h == null) continue;
      s.update({ time: times[i], value: h, color: h >= 0 ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)" });
    }
  };
  updMacdLine(emMacdLine, macdLine);
  updSigLine(emSigLine);
  updHist(emMacdHist, (i) => a.macd.hist[i]);
  if (emRsi) {
    if (!inc) emRsi.setData(rsiLine);
    else {
      for (let i = from; i < n; i++) {
        const rv = a.rsi?.[i];
        if (rv != null) emRsi.update({ time: times[i], value: rv });
      }
    }
  }
  _applied = { figi: a.figi ?? "", interval: chartInterval, firstTime: times[0], length: n };
}

function updateEmLegend(time: Time | null): void {
  const el = $("ohlc-legend");
  if (!el || !lastAnalysis || !lastAnalysis.candles.length) return;
  let idx = lastAnalysis.candles.length - 1;
  if (time != null) {
    const t = Number(time) * 1000;
    for (let i = 0; i < lastAnalysis.candles.length; i++) {
      const ct = new Date(lastAnalysis.candles[i].ts).getTime();
      if (ct === t) { idx = i; break; }
      if (ct > t) { idx = Math.max(0, i - 1); break; }
    }
  }
  const c = lastAnalysis.candles[idx];
  const prevClose = idx > 0 ? lastAnalysis.candles[idx - 1].close : c.open;
  const changePct = prevClose !== 0 ? ((c.close - prevClose) / prevClose) * 100 : 0;
  const dirClass = changePct >= 0 ? "up" : "down";
  const sign = changePct >= 0 ? "+" : "";
  const priceFmt = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const volFmt = new Intl.NumberFormat("ru-RU", { notation: "compact" });
  el.innerHTML = [
    `<span class="lbl">O</span> <span class="val">${priceFmt.format(c.open)}</span>`,
    `<span class="lbl">H</span> <span class="val">${priceFmt.format(c.high)}</span>`,
    `<span class="lbl">L</span> <span class="val">${priceFmt.format(c.low)}</span>`,
    `<span class="lbl">C</span> <span class="val ${dirClass}">${priceFmt.format(c.close)}</span>`,
    `<span class="val ${dirClass}">${sign}${changePct.toFixed(2)}%</span>`,
    `<span class="lbl">Vol</span> <span class="val">${volFmt.format(c.volume)}</span>`,
  ].join(" ");
}

function _applyOverlay(msg: FocusMsg): void {
  if (!emSeries) return;
  const trade = msg.trade ?? null;
  const sig = JSON.stringify([msg.trades ?? [], trade]);
  if (sig === _overlaySig) return; // ничего не изменилось — не дёргаем маркеры/линии
  _overlaySig = sig;
  const markers: SeriesMarker<Time>[] = [];
  for (const t of msg.trades ?? []) {
    const en = emEpoch(t.entry_time);
    const ex = emEpoch(t.exit_time);
    const up = t.side !== "SHORT";
    if (en != null) {
      markers.push({ time: en as UTCTimestamp, position: "belowBar", color: up ? "#00d9a0" : "#ff5d6c", shape: up ? "arrowUp" : "arrowDown", text: up ? "L" : "S" });
    }
    if (ex != null) {
      markers.push({ time: ex as UTCTimestamp, position: "aboveBar", color: up ? "#ff5d6c" : "#00d9a0", shape: up ? "arrowDown" : "arrowUp", text: "X" });
    }
  }
  emMarkers?.setMarkers(markers.slice(-300));

  emLines.forEach((l) => emSeries!.removePriceLine(l));
  emLines = [];
  const tr = msg.trade;
  const line = (price: number | undefined, color: string, title: string) => {
    if (price == null || !Number.isFinite(price)) return;
    emLines.push(emSeries!.createPriceLine({ price, color, lineWidth: 1, lineStyle: LineStyle.Dashed, title }));
  };
  line(tr?.entry_price, "#f4b942", `${msg.ticker ?? ""} Вход ${tr?.entry_price?.toFixed(2) ?? ""}`);
  line(tr?.stop_loss, "#ff5d6c", `SL ${tr?.stop_loss?.toFixed(2) ?? ""}`);
  line(tr?.take_profit, "#00d9a0", `TP ${tr?.take_profit?.toFixed(2) ?? ""}`);
}

function _clearSeries(): void {
  emSeries?.setData([]);
  emVolume?.setData([]);
  emSma?.setData([]);
  emEma?.setData([]);
  emBbU?.setData([]);
  emBbL?.setData([]);
  emMacdLine?.setData([]);
  emSigLine?.setData([]);
  emMacdHist?.setData([]);
  emRsi?.setData([]);
}

async function loadEmbedChart(msg: FocusMsg, opts?: { full?: boolean }): Promise<void> {
  const figi = msg.figi;
  if (!figi || !emChart || !emSeries) return;
  // Координатор: каждый новый запрос гасит предыдущий, и ответ применяется
  // только если он всё ещё актуален (тот же figi, тот же таймфрейм, свежая ревизия).
  const requestId = ++_chartReqId;
  _chartAbort?.abort();
  const ac = new AbortController();
  _chartAbort = ac;
  _loadInFlight = true;
  const intervalAt = chartInterval;
  const stale = (): boolean => requestId !== _chartReqId || chartInterval !== intervalAt || lastFocus?.figi !== figi;
  lastFocus = msg;
  const firstShow = figi !== _plottedFigi;
  // Прайс-шкалу калибруем только при СМЕНЕ ИНСТРУМЕНТА. Переход по сделкам
  // того же тикера (тот же figi) обязан оставить и масштаб по цене, и по
  // времени — просто сместить окно к времени сделки.
  const priceJump = figi !== _priceScaleFigi;
  _plottedFigi = figi;
  _priceScaleFigi = figi;
  if (firstShow) skladOnChartFigiChange(figi);
  const focusKey = figi + "|" + (msg.centerSec ?? "");
  const keepView = !firstShow && focusKey === _lastFocusKey;
  _lastFocusKey = focusKey;
  console.debug("[chart] focus", "figi=" + figi, "centerSec=" + (msg.centerSec ?? "-"), "halfBars=" + (msg.halfBars ?? "-"), "trades=" + (msg.trades?.length ?? 0), "firstShow=" + firstShow, "keepView=" + keepView);
  const tickerEl = $("symbol-ticker");
  if (tickerEl) tickerEl.textContent = msg.ticker || figi;
  // Фокус на конкретной сделке/позиции: окно свечей должно ДОХОДИТЬ до нужной даты,
  // иначе (лимит 2000 баров при 1м ≈ 2 дня) центр уезжает на самый старый бар.
  const ivSec = _IV_SEC[intervalAt] ?? 60;
  // Запас справа от сделки должен перекрывать всю сохраняемую ширину окна,
  // иначе «не прижать влево» нечем — данных после сделки просто не будет.
  const spanBars = _visibleBarSpan();
  // Переход к сделке: ширину окна НЕ меняем — берём текущую видимую, иначе
  // _applyViewCenter() с halfBars=undefined брал 20 баров и график дико
  // приближался (40 минут вместо, скажем, 6 часов). Явно заданный halfBars
  // (смена ТФ, currentViewState) по-прежнему главнее.
  const focusHalfBars = msg.halfBars ?? spanBars ?? undefined;
  const focusBuffer = msg.centerSec != null
    ? Math.max(12 * ivSec, (msg.halfBars ?? 100) * ivSec * 2, (spanBars ?? 0) * ivSec + 2 * ivSec)
    : null;
  const snap = captureViewport();
  let a: AnalysisDto | null = null;
  try {
    a = msg.centerSec != null && focusBuffer != null
      ? await fetchAnalysis(figi, intervalAt, 5000, new Date((msg.centerSec + focusBuffer) * 1000).toISOString(), ac.signal)
      : await fetchAnalysis(figi, intervalAt, 2000, undefined, ac.signal);
  } catch (err) {
    if (_isAbort(err) || stale()) return; // фокус успел смениться — тихо уходим
    console.warn("analysis error", err);
    a = null;
  }
  if (stale()) { _loadInFlight = false; return; }
  if (!a || !a.candles || !a.candles.length) {
    // Fallback: свечи без индикаторов.
    _clearSeries();
    _applied = null;
    try {
      const res = await fetch(`${API}/api/candles/${encodeURIComponent(figi)}?interval_name=${encodeURIComponent(intervalAt)}&limit=5000`, { signal: ac.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      const candles: Array<{ ts: string; open: number; high: number; low: number; close: number }> = json.candles ?? [];
      if (stale()) { _loadInFlight = false; return; }
      const ftimes = candles.map((c) => Math.floor(new Date(c.ts).getTime() / 1000) as UTCTimestamp);
      emSeries.setData(candles.map((c, i) => ({
        time: ftimes[i], open: c.open, high: c.high, low: c.low, close: c.close,
      })));
      lastAnalysis = null;
      const nm = $("symbol-name");
      if (nm) nm.textContent = "";
      updateEmLegend(null);
      _resetPriceScale(priceJump);
      _applyOverlay(msg);
      if (!keepView) _applyViewCenter(msg.centerSec, focusHalfBars, ftimes as unknown as number[], ivSec);
      else restoreViewport(snap);
      _setChartMode(msg.centerSec != null ? "TRADE_FOCUS" : "LIVE_FOLLOW");
    } catch (err2) {
      if (_isAbort(err2) || stale()) { _loadInFlight = false; return; }
      console.warn("embedded candles error", err2);
      _resetPriceScale(priceJump);
      _applyOverlay(msg);
      if (!keepView) _applyViewCenter(msg.centerSec, focusHalfBars, undefined, ivSec);
      else restoreViewport(snap);
    }
    _loadInFlight = false;
    return;
  }
  lastAnalysis = a;
  _applySeriesData(a, { incremental: !opts?.full });
  if (tickerEl) tickerEl.textContent = a.ticker || msg.ticker || figi;
  const nm = $("symbol-name");
  if (nm) nm.textContent = a.name || "";
  updateEmLegend(null);
  _resetPriceScale(priceJump);
  _applyOverlay(msg);
  if (!keepView) _applyViewCenter(msg.centerSec, focusHalfBars, undefined, ivSec);
  else restoreViewport(snap);
  _setChartMode(msg.centerSec != null ? "TRADE_FOCUS" : (keepView && !snap.following ? "USER_EXPLORE" : "LIVE_FOLLOW"));
  _loadInFlight = false;
}

// Прайс-шкалу сбрасываем только при смене ИНСТРУМЕНТА: цены другой акции
// (например 2500₽ вместо 200₽) иначе остаются за пределами экрана. Переход по
// сделкам того же тикера шкалу не трогает. fitContent здесь НЕ вызываем:
// viewport в loadEmbedChart ставится сразу после (центр сделки либо дефолтные
// 6 часов), а второй fitContent давал двойной прыжок камеры.
function _resetPriceScale(resetPrice: boolean): void {
  if (!resetPrice || !emChart || !emSeries) return;
  const ps = emSeries.priceScale();
  ps.applyOptions({ autoScale: false });
  ps.applyOptions({ autoScale: true });
}

function syncIndicatorVisibility(): void {
  emSma?.applyOptions({ visible: indicators.sma });
  emEma?.applyOptions({ visible: indicators.ema });
  emBbU?.applyOptions({ visible: indicators.bb });
  emBbL?.applyOptions({ visible: indicators.bb });
}

function applyIndicators(): void {
  try { localStorage.setItem("deeptrading_indicators", JSON.stringify(indicators)); } catch { }
  const wantPane = indicators.macd || indicators.rsi;
  if (wantPane !== emOscPane) {
    rebuildEmbedChart();
  } else {
    syncIndicatorVisibility();
    if (wantPane && emChart) {
      try {
        const panes = emChart.panes();
        if (panes[1]) {
          panes[0].setStretchFactor(3);
          panes[1].setStretchFactor(2);
        }
      } catch { /* panes API может отсутствовать */ }
    }
  }
}

function rebuildEmbedChart(): void {
  if (!emChart) return;
  const logical = emChart.timeScale().getVisibleLogicalRange();
  const msg = lastFocus;
  emChart.remove();
  emChart = null;
  emSeries = null;
  emMarkers = null;
  emVolume = null; emSma = null; emEma = null; emBbU = null; emBbL = null;
  emMacdHist = null; emMacdLine = null; emSigLine = null; emRsi = null;
  _applied = null; // серии пересозданы — инкрементальное обновление недопустимо
  startChart();
  if (lastAnalysis) {
    _applySeriesData(lastAnalysis);
    updateEmLegend(null);
  }
  if (msg?.figi) _applyOverlay(msg);
  const ch = startChart();
  if (logical && ch) ch.timeScale().setVisibleLogicalRange(logical);
}

// Версия дефолтов графика. При её смене сохранённый ранее интервал сбрасывается
// на 1min — иначе старая настройка (например 5min из прошлой сессии) перебивала
// требование «по умолчанию минутные». Дальше пользовательский выбор уважается.
const CHART_DEFAULTS_VERSION = "v2-1min-6h";

function initChartToolbar(): void {
  if (chartToolbarInit) return;
  chartToolbarInit = true;
  try {
    if (localStorage.getItem("deeptrading_chart_defaults") !== CHART_DEFAULTS_VERSION) {
      localStorage.setItem("deeptrading_chart_interval", "1min");
      localStorage.setItem("deeptrading_chart_defaults", CHART_DEFAULTS_VERSION);
    }
    chartInterval = localStorage.getItem("deeptrading_chart_interval") || "1min";
  } catch { }
  if (chartInterval !== "1min") {
    chartInterval = "1min";
    try { localStorage.setItem("deeptrading_chart_interval", "1min"); } catch { }
  }
  document.querySelectorAll<HTMLButtonElement>("#ct-intervals .ct-int").forEach((b) => {
    const v = b.dataset.int;
    b.classList.toggle("active", v === chartInterval);
    b.addEventListener("click", () => {
      if (!v || v === chartInterval) return;
      chartInterval = v;
      try { localStorage.setItem("deeptrading_chart_interval", chartInterval); } catch { }
      document.querySelectorAll<HTMLButtonElement>("#ct-intervals .ct-int").forEach((x) => x.classList.toggle("active", x.dataset.int === v));
      if (lastFocus?.figi) void loadEmbedChart({ ...lastFocus, ...currentViewState() }, { full: true });
    });
  });
  document.querySelectorAll<HTMLInputElement>("#indicators-menu input[data-ind]").forEach((box) => {
    const k = box.dataset.ind as keyof typeof indicators;
    box.checked = indicators[k] ?? false;
    box.addEventListener("change", () => {
      (indicators as Record<string, boolean>)[k] = box.checked;
      applyIndicators();
    });
  });
  const btnInd = $("btn-indicators");
  const menu = $("indicators-menu");
  if (btnInd && menu) {
    btnInd.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.classList.toggle("hidden");
    });
    document.addEventListener("click", (e) => {
      if (!menu.contains(e.target as Node)) menu.classList.add("hidden");
    });
  }
  // «К текущему»: если юзер ушёл в историю сделки — снимаем якорь и возвращаем
  // живое окно; если уже в live — просто перезапрашиваем, сохраняя вьюпорт.
  $("btn-sync")?.addEventListener("click", () => {
    if (!lastFocus?.figi) return;
    if (chartMode === "TRADE_FOCUS") {
      void loadEmbedChart({ figi: lastFocus.figi, ticker: lastFocus.ticker ?? "", trade: lastFocus.trade ?? null, trades: lastFocus.trades ?? [] });
      return;
    }
    void loadEmbedChart({ ...lastFocus, ...currentViewState() }, { full: true });
  });
  const btnLog = $("btn-logscale") as HTMLButtonElement | null;
  if (btnLog) {
    const sync = (): void => {
      btnLog.classList.toggle("active", priceScaleLog);
      btnLog.textContent = priceScaleLog ? "лог" : "лин";
      emSeries?.priceScale().applyOptions({ mode: priceScaleLog ? PriceScaleMode.Logarithmic : PriceScaleMode.Normal });
    };
    btnLog.addEventListener("click", () => {
      priceScaleLog = !priceScaleLog;
      try { localStorage.setItem("deeptrading_price_scale_log", priceScaleLog ? "1" : "0"); } catch { }
      sync();
    });
    sync();
  }
}

function onEmbedMessage(e: MessageEvent): void {
  const d = e.data as FocusMsg & { type?: string };
  if (!d) return;
  // Только линии/маркеры (SL/TP/trailing) — свечи не перезапрашиваем.
  if (d.type === "overlay") { _applyOverlay(d); return; }
  if (d.type !== "focus") return;
  void loadEmbedChart(d);
}

// ——————————————————————————————————————————————————————————————————————
// Склад: каталог блоков + конструктор (перенос из оригинала Deeptrading).
// Рендер плашек стратегий, назначение ролей, расчёт сигналов выбранных
// стратегий на текущей акции/ТФ и отрисовка стрелок на графике.
// ——————————————————————————————————————————————————————————————————————

const SKLAD_TAG_BY_STRATEGY: Record<string, string> = {};
let skladCards: StrategyCardDto[] = [];
const skladActiveRuns = new Map<string, { runId: string; figi: string; signals: { ts: string; side: string }[]; params: Record<string, number> }>();

let skladExitPolicies: Array<{ id: string; label: string; params_schema: Record<string, { default: number; min?: number; max?: number }> }> = [];
let skladSelExit = { id: "fixed_sl_tp", params: { stop_pct: 1, target_pct: 2 } } as { id: string; params: Record<string, number> };
let skladSelectedSlot: string | null = null;

const SKLAD_ROLE_META: Record<string, { title: string; cls: string; hint: string }> = {
  bias: { title: "BIAS · контекст", cls: "blk-blue", hint: "направление старшего ТФ" },
  setup: { title: "SETUP · сетап", cls: "blk-purple", hint: "где ищем вход" },
  entry: { title: "ENTRY · триггер", cls: "blk-green", hint: "точка входа" },
};

let skladInit = false;

function skladStatus(text: string): void {
  const el = document.getElementById("sklad-status");
  if (el) el.textContent = text;
}

function skladFigi(): string {
  return lastFocus?.figi ?? _plottedFigi ?? "";
}

function skladParamsOf(card: StrategyCardDto): Record<string, number> {
  const params: Record<string, number> = {};
  document.querySelectorAll<HTMLInputElement>(`#params-${CSS.escape(card.id)} input[data-key]`).forEach((inp) => {
    params[inp.dataset.key!] = Number(inp.value);
  });
  return params;
}

function skladVisibleWindowSec(): { from: number; to: number } | null {
  if (!emChart) return null;
  const vr = emChart.timeScale().getVisibleRange();
  if (!vr) return null;
  const from = typeof vr.from === "number" ? vr.from : null;
  const to = typeof vr.to === "number" ? vr.to : null;
  if (from == null || to == null) return null;
  return { from, to };
}

function skladDrawSignals(): void {
  if (!emMarkersSig) return;
  const show = (document.getElementById("cb-markers") as HTMLInputElement | null)?.checked ?? true;
  if (!show) {
    emMarkersSig.setMarkers([]);
    return;
  }
  const figi = skladFigi();
  const markers: SeriesMarker<Time>[] = [];
  for (const [sid, run] of skladActiveRuns) {
    if (run.figi !== figi) continue;
    const tag = SKLAD_TAG_BY_STRATEGY[sid] ?? sid.slice(0, 3).toUpperCase();
    for (const s of run.signals) {
      const t = emEpoch(s.ts);
      if (t == null) continue;
      markers.push({
        time: t as UTCTimestamp,
        position: s.side === "BUY" ? "belowBar" : "aboveBar",
        color: s.side === "BUY" ? "#00d9a0" : "#ff5d6c",
        shape: s.side === "BUY" ? "arrowUp" : "arrowDown",
        text: tag,
      });
    }
  }
  markers.sort((a, b) => Number(a.time) - Number(b.time));
  emMarkersSig.setMarkers(markers.slice(-300));
}

function skladRenderConstructor(): void {
  const holder = document.getElementById("pipe-holder");
  if (!holder) return;
  const slotsHtml = ["bias", "setup", "entry"].map((role) => {
    const meta = SKLAD_ROLE_META[role];
    const members = [...skladActiveRuns.entries()].filter(([sid]) =>
      document.querySelector<HTMLSelectElement>(`#role-${CSS.escape(sid)}`)?.value === role);
    const list = members.map(([sid]) => {
      const card = skladCards.find((c) => c.id === sid);
      return `<div class="ps-item" data-sid="${sid}" data-role="${role}">
        <b>${card?.name ?? sid}</b>
      </div>`;
    }).join("");
    return `<div class="pipe-slot ${meta.cls}${skladSelectedSlot === role ? " sel" : ""}" data-slot="${role}">
      <div class="ps-title">${meta.title}<span class="badge">${members.length}</span></div>
      <div class="ps-sub">${meta.hint}</div>
      <div class="ps-items">${list || `<div class="mini-hint">пусто — отметьте стратегии в каталоге и назначьте роль</div>`}</div>
    </div><div class="pipe-arrow">↓</div>`;
  }).join("");

  const exitSchema = skladExitPolicies.find((e) => e.id === skladSelExit.id)?.params_schema ?? {};
  const exitParamsHtml = Object.entries(exitSchema).map(([k, spec]) =>
    `<label class="mini-hint">${k} <input type="number" data-exit-key="${k}" value="${skladSelExit.params[k] ?? spec.default}" step="any" style="width:64px"></label>`
  ).join(" ");
  const pipe = `${slotsHtml}
    <div class="pipe-slot blk-gray${skladSelectedSlot === "position" ? " sel" : ""}" data-slot="position">
      <div class="ps-title">POSITION <span class="qmark" title="Риск-модуль и мультипозиционность в движке не реализованы">?</span></div>
      <div class="ps-sub">qty=1 · одна позиция на FIGI · mode both</div>
    </div>
    <div class="pipe-slot blk-red${skladSelectedSlot === "exit" ? " sel" : ""}" data-slot="exit" style="margin-top:6px">
      <div class="ps-title">EXIT
        <select id="exit-sel">${skladExitPolicies.map((e) => `<option value="${e.id}"${e.id === skladSelExit.id ? " selected" : ""}>${e.label}</option>`).join("")}</select>
      </div>
      <div id="exit-params" style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px">${exitParamsHtml}</div>
    </div>
    <div class="pipe-slot blk-orange" style="margin-top:6px">
      <div class="ps-title">FILTERS <span class="qmark" title="Фильтры (VWAP side, ADX, ATR pct, volume ratio…) движком не исполняются">?</span></div>
      <div class="mini-hint">session time / cooldown / min hold уже работают (сессия, кулдаун, min_hold_bars)</div>
    </div>`;

  holder.innerHTML = pipe;

  holder.querySelectorAll<HTMLElement>(".pipe-slot").forEach((el) =>
    el.addEventListener("click", () => { skladSelectedSlot = el.dataset.slot!; skladRenderConstructor(); }));

  const exSel = holder.querySelector<HTMLSelectElement>("#exit-sel");
  exSel?.addEventListener("change", () => {
    const e = skladExitPolicies.find((x) => x.id === exSel.value);
    skladSelExit = { id: exSel.value, params: Object.fromEntries(Object.entries(e?.params_schema ?? {}).map(([k, sp]) => [k, sp.default])) };
    skladRenderConstructor();
  });
  holder.querySelectorAll<HTMLInputElement>("input[data-exit-key]").forEach((inp) =>
    inp.addEventListener("change", () => { skladSelExit.params[inp.dataset.exitKey!] = Number(inp.value); }));
}

let _ensLiveCfg: EnsembleConfig | null = null;

function skladRenderCatalog(cards: StrategyCardDto[], liveCfg: EnsembleConfig | null = null): void {
  const wrap = document.getElementById("strategy-cards");
  if (!wrap) return;
  wrap.innerHTML = "";
  const live = liveCfg != null;
  const orch = live && liveCfg._all_strategies ? new Set(liveCfg._all_strategies) : null;
  const setups = live ? (liveCfg.setups ?? []) : [];
  const setupOf = (id: string) => setups.find((s) => s.strategy_id === id);
  const shown = live && orch ? cards.filter((c) => orch.has(c.id)) : cards;
  for (const c of shown) {
    SKLAD_TAG_BY_STRATEGY[c.id] = `${c.name.slice(0, 3).toUpperCase()}`;
    const el = document.createElement("div");
    el.className = "scard" + (live ? " scard-live" : "");
    el.dataset.sid = c.id;
    const setup = setupOf(c.id);
    const paramsHtml = Object.entries(c.params_schema)
      .map(([key, spec]) => {
        const val = ((live && setup?.params?.[key] != null) ? setup.params[key] : spec.default);
        const ro = live ? " readonly" : "";
        return `<label>${key}<input type="number" data-key="${key}" value="${val}"` +
          ` min="${spec.min ?? ""}" max="${spec.max ?? ""}" step="any"${ro} /></label>`;
      })
      .join("");
    const roleHtml = live ? "" : `<label class="scard-role">Роль
        <select id="role-${CSS.escape(c.id)}">
          <option value="setup">SETUP</option>
          <option value="bias">BIAS</option>
          <option value="entry">ENTRY</option>
        </select></label>`;
    // live: галка = enabled в конфиге оркестра (без localStorage).
    let checked = false;
    if (live) {
      checked = !!setup?.enabled;
    } else {
      try { checked = localStorage.getItem(`sklad_check:${c.id}`) !== "0"; } catch { }
    }
    el.innerHTML = `
      <label class="scard-head">
        <input type="checkbox" data-sid="${c.id}" ${checked ? "checked" : ""} />
        <span class="scard-name">${c.name}</span>
        <span class="badge fam-${c.family}">${c.family}</span>
        <span class="wave-badge">W${c.wave}</span>
        ${live ? `<span class="badge live-badge" title="Функция живого оркестра">live</span>` : `<button class="scard-reset" data-reset="${c.id}" title="Сбросить параметры к дефолту">↺</button>`}
      </label>
      <div class="scard-rules">▲ ${c.long_rule}<br />▼ ${c.short_rule}</div>
      <div class="scard-params" id="params-${CSS.escape(c.id)}" data-figi="${skladFigi()}">${paramsHtml}</div>
      ${roleHtml}
    `;
    if (!live) {
      el.querySelectorAll<HTMLInputElement>(".scard-params input").forEach((inp) =>
        inp.addEventListener("change", () => {
          try { localStorage.setItem(`sklad_p:${c.id}:${inp.dataset.key!}`, inp.value); } catch { }
        }));
      el.querySelector<HTMLSelectElement>(`#role-${CSS.escape(c.id)}`)?.addEventListener("change", (ev) => {
        try { localStorage.setItem(`sklad_role:${c.id}`, (ev.target as HTMLSelectElement).value); } catch { }
        skladRenderConstructor();
      });
      el.querySelector<HTMLButtonElement>(".scard-reset")?.addEventListener("click", (ev) => {
        ev.stopPropagation();
        for (const key of Object.keys(c.params_schema)) {
          try { localStorage.removeItem(`sklad_p:${c.id}:${key}`); } catch { }
        }
        el.querySelectorAll<HTMLInputElement>(".scard-params input").forEach((inp) => {
          inp.value = String(c.params_schema[inp.dataset.key!]?.default ?? inp.value);
        });
      });
    }
    el.querySelector(".scard-head")?.addEventListener("click", (ev) => {
      const target = ev.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "SELECT" || target.tagName === "BUTTON") return;
      const holder = document.getElementById("block-params");
      if (holder) {
        holder.innerHTML = `<b>${c.name}</b> <span class="badge fam-${c.family}">${c.family}</span> <span class="wave-badge">W${c.wave}</span>
          <div class="mini-hint" style="margin:4px 0">▲ ${c.long_rule}<br />▼ ${c.short_rule}</div>
          ${live ? `<div class="mini-hint" style="margin:4px 0">оркестр: ${setup?.enabled ? "включена" : "выключена"} · tf ${setup?.tf ?? "—"}</div>` : ""}`;
      }
    });
    const cb = el.querySelector<HTMLInputElement>("input[type=checkbox]")!;
    cb.addEventListener("change", (ev) => {
      const on = (ev.target as HTMLInputElement).checked;
      el.classList.toggle("checked", on);
      if (live) {
        void skladToggleLive(c.id, on);
      } else {
        try { localStorage.setItem(`sklad_check:${c.id}`, on ? "1" : "0"); } catch { }
        if (!on) skladActiveRuns.delete(c.id);
        skladDrawSignals();
        skladRenderConstructor();
      }
    });
    if (checked) el.classList.add("checked");
    wrap.appendChild(el);
  }
}

async function skladToggleLive(sid: string, on: boolean): Promise<void> {
  const cfg = _ensLiveCfg;
  if (!cfg) { skladStatus("Конфиг ансамбля ещё не загружен — подождите"); return; }
  const card = skladCards.find((c) => c.id === sid);
  let setups = (cfg.setups ?? []).map((s) => s.strategy_id === sid ? { ...s, enabled: on } : s);
  if (!setups.some((s) => s.strategy_id === sid)) {
    const params: Record<string, number> = {};
    if (card) for (const [k, spec] of Object.entries(card.params_schema)) params[k] = Number(spec.default);
    setups.push({ strategy_id: sid, enabled: on, tf: cfg.entry_tf || "10min", params });
  }
  try {
    const r = await botEnsemblePatch({ setups });
    _ensLiveCfg = r.config;
    skladRenderCatalog(skladCards, r.config);
    skladRenderConstructor();
    skladStatus(`Оркестр: «${sid}» ${on ? "включена" : "выключена"} — применено стратегий: ${r.applied_strategies}`);
  } catch (e) {
    skladRenderCatalog(skladCards, cfg);
    skladStatus("Оркестр: не применить — " + (e instanceof Error ? e.message : String(e)));
  }
}

async function skladRunSelected(): Promise<void> {
  const btn = document.getElementById("btn-run-strategies") as HTMLButtonElement | null;
  const figi = skladFigi();
  if (!figi) {
    skladStatus("Нет акции на графике — откройте инструмент в каталоге");
    return;
  }
  const checked = [...document.querySelectorAll<HTMLInputElement>("#strategy-cards input:checked")];
  if (checked.length === 0) {
    skladStatus("Не выбрано ни одной стратегии");
    return;
  }
  if (btn) btn.classList.add("busy");
  let ok = 0;
  try {
    for (const box of checked) {
      const card = skladCards.find((c) => c.id === box.dataset.sid);
      if (!card) continue;
      const params = skladParamsOf(card);
      skladStatus(`Расчёт ${card.name} (${chartInterval})…`);
      const win = skladVisibleWindowSec();
      const res = await computeSignals(figi, chartInterval, card.id, params, win?.from, win?.to);
      skladActiveRuns.set(card.id, { runId: res.run_id, figi, signals: res.signals as { ts: string; side: string }[], params });
      ok += 1;
      skladStatus(`${card.name}: ${res.count} сигналов${res.cached ? " (из кеша)" : ""}`);
    }
    skladDrawSignals();
    skladRenderConstructor();
    if (ok > 0) skladStatus(`Готово: ${ok} стратегий рассчитано, стрелки на графике`);
  } catch (e) {
    skladStatus(e instanceof Error ? e.message : String(e));
  } finally {
    if (btn) btn.classList.remove("busy");
  }
}

function skladInitPanel(): void {
  if (skladInit) return;
  skladInit = true;
  document.getElementById("btn-run-strategies")?.addEventListener("click", () => void skladRunSelected());
  document.getElementById("cb-markers")?.addEventListener("change", skladDrawSignals);
  document.getElementById("btn-gates-refresh")?.addEventListener("click", () => void skladLoadGates());
  document.getElementById("btn-logic-refresh")?.addEventListener("click", () => void skladLoadLogic());
  document.getElementById("btn-sklad-clear")?.addEventListener("click", () => {
    skladCards.forEach((c) => {
      try { localStorage.setItem(`sklad_check:${c.id}`, "0"); } catch { }
      const el = document.querySelector<HTMLElement>(`.scard[data-sid="${CSS.escape(c.id)}"]`);
      const cb = el?.querySelector<HTMLInputElement>("input[type=checkbox]");
      if (cb) cb.checked = false;
      el?.classList.remove("checked");
    });
    skladActiveRuns.clear();
    skladDrawSignals();
    skladRenderConstructor();
    skladStatus("Каталог очищен");
  });
  fetchCatalog()
    .then((cat) => {
      skladCards = cat.strategies;
      return botEnsembleConfig().then((cfg) => {
        _ensLiveCfg = cfg;
        skladRenderCatalog(cat.strategies, cfg);
        skladExitPolicies = ((cat as unknown as { exit_policies?: typeof skladExitPolicies }).exit_policies ?? skladExitPolicies);
        skladRenderConstructor();
        const on = (cfg.setups ?? []).filter((s) => s.enabled).length;
        const total = (cfg._all_strategies ?? cfg.setups ?? []).length;
        skladStatus(`Live-ансамбль: ${on} из ${total} функций включено — галка = живой оркестр, клик применяет сразу`);
      });
    })
    .catch((e) => skladStatus(e instanceof Error ? e.message : String(e)));
  void skladLoadGates();
  void skladLoadLogic();
}

function skladOnChartFigiChange(figi: string): void {
  for (const [sid, run] of skladActiveRuns) {
    if (run.figi !== figi) skladActiveRuns.delete(sid);
  }
  skladDrawSignals();
}

// ===== Гейты входа (аудит конвейера бота) =====

const GATE_STAGE_TITLES: Record<string, string> = {
  signal: "Сигнал (внутри ансамбля)",
  pre_order: "До заявки (свеча)",
  time: "Время / сессии / риск дня",
  trend: "Тренд / таймфреймы",
  portfolio: "Портфель / позиции",
  sizing: "Сайзинг",
  approval: "AI-гейт",
};

function skladRenderGates(g: GatesReport): void {
  const cont = document.getElementById("gates-container");
  if (!cont) return;
  const order = (g.order ?? []).length ? g.order : Object.keys(g.by_stage ?? {});
  const stages = Object.keys(g.by_stage ?? {});
  const shown = order.filter((s) => stages.includes(s));
  for (const s of shown) {
    const items = (g.categories ?? {})[s];
    if (!items || !items.length) continue;
    const meta = g.by_stage[s];
    const chips = items.map((it) => {
      const le = !!it.logical_enabled;
      const okCls = le ? "ok" : "fail";
      const stateCh = le ? "✓" : "✗";
      const flag = it.flag_value !== null && it.flag_value !== undefined && it.flag_value !== ""
        ? ` <span class="mini-hint">=${escFlag(it.flag_value)}</span>` : "";
      const tools = it.toggleable
        ? `<label class="gate-switch" title="${le ? "Выключить" : "Включить"}">
            <input type="checkbox" class="g-toggle" data-key="${esc(it.key)}" ${le ? "checked" : ""}>
            <span class="g-switch-slider"></span></label>`
        : `<span class="g-fixed" title="Структурный — всегда на пути заявки">пост.</span>`;
      return `<div class="gate-chip ${okCls}" title="${esc(it.desc || "")}">
        <span class="g-state">${stateCh}</span>
        <div><span class="g-title">${esc(it.title)}</span>${flag}
        <div class="g-desc">${esc(it.key)}${it.rejects ? ` · отказов ${it.rejects}` : ""}</div></div>
        <div class="g-tools">${tools}</div>
      </div>`;
    }).join("");
    cont.insertAdjacentHTML("beforeend",
      `<div class="gate-stage">
        <div class="gate-stage-title">${esc(GATE_STAGE_TITLES[s] ?? s)}
          <span class="stage-count">${meta.enabled}/${meta.gates} · ${meta.rejects} отказов</span></div>
        <div class="gate-chips">${chips}</div>
      </div>`);
  }
  if (!shown.length) cont.innerHTML = `<div class="mini-hint">Гейты пока не собраны (бот не запущен)</div>`;
  cont.querySelectorAll<HTMLInputElement>("input[type=checkbox].g-toggle").forEach((cb) => {
    if (cb.dataset.bound) return;
    cb.dataset.bound = "1";
    cb.addEventListener("change", () => void skladToggleGate(String(cb.dataset.key ?? ""), !!cb.checked, cb));
  });
}

async function skladToggleGate(key: string, on: boolean, cb: HTMLInputElement): Promise<void> {
  const st = document.getElementById("gates-status");
  const cont = document.getElementById("gates-container");
  cb.disabled = true;
  try {
    const g = await botGatesToggle(key, on);
    if (cont) cont.innerHTML = "";
    skladRenderGates(g);
    if (st) st.textContent = `${g.count} гейтов${g.skip_counts && Object.keys(g.skip_counts).length
      ? ` · ${Object.keys(g.skip_counts).length} ключей отказов` : ""}`;
  } catch (e) {
    cb.disabled = false;
    cb.checked = !on;
    if (st) st.textContent = "ошибка: " + (e instanceof Error ? e.message : String(e));
  }
}

async function skladLoadGates(): Promise<void> {
  const cont = document.getElementById("gates-container");
  if (!cont) return;
  const st = document.getElementById("gates-status");
  if (st) st.textContent = "загрузка…";
  try {
    const g = await botGates();
    cont.innerHTML = "";
    skladRenderGates(g);
    if (st) st.textContent = `${g.count} гейтов` + (g.skip_counts && Object.keys(g.skip_counts).length
      ? ` · ${Object.keys(g.skip_counts).length} ключей отказов` : "");
  } catch (e) {
    cont.innerHTML = `<div class="mini-hint">Ошибка: ${esc(e instanceof Error ? e.message : String(e))}</div>`;
    if (st) st.textContent = "ошибка";
  }
}

// ===== Логика сигналов → ансамбль =====

function skladRenderLogic(cfg: EnsembleConfig, funnel?: { stats: Record<string, number>; ring: { stage: string; action: string; reason: string; n: number }[] }): void {
  const cont = document.getElementById("logic-container");
  if (!cont) return;
  const setups = (cfg.setups ?? []).filter((s) => s.enabled);
  const setupTags = setups.map((s) => `<span class="ls-tag on">${esc(s.strategy_id.replace(/_/g, " "))} <em>${s.tf}</em></span>`).join("");
  const offTags = (cfg.setups ?? []).filter((s) => !s.enabled).slice(0, 4)
    .map((s) => `<span class="ls-tag off">${esc(s.strategy_id.replace(/_/g, " "))}</span>`).join("");
  const bias = cfg.bias ?? { tf: "hour", period: 50 };
  const quorumCls = (funnel && (funnel.stats["signal:quorum_passed"] || 0) > 0) ? "hit" : "";
  const vol = Number(cfg.vol_thr ?? 0);
  const volOn = vol > 0;
  const biasMode = cfg.bias_mode ?? "info";
  const biasOn = biasMode === "veto";
  const reg = cfg.regime_setups_filter ?? {};
  const regOn = reg && Object.keys(reg).length > 0;
  const knobs = `<div class="ls-tags ens-knobs">
    <span class="ls-tag ${volOn ? "on" : "off"}" title="Объёмный фильтр входа/выхода (ensemble_config.vol_thr)">vol_thr=${vol > 0 ? vol : "выкл"}</span>
    <span class="ls-tag ${biasOn ? "on" : "off"}" title="Гейт «против bias»: veto — блокирует вход против направления">bias=${biasMode}${biasOn ? "" : " · выкл"}</span>
    <span class="ls-tag ${regOn ? "on" : "off"}" title="Режимный фильтр сетапов (regime_setups_filter)">regime=${regOn ? `${Object.keys(reg).length} режим(а)` : "выкл"}</span>
    <span class="ls-tag on">k=${cfg.quorum}</span>
    <span class="ls-tag ${cfg.neutral_mode === "all" ? "off" : "on"}" title="Нейтральный режим: semi_flip — флипы допингуют сигналы">${cfg.neutral_mode ?? "semi_flip"}</span>
  </div>`;
  const row = `
    <div class="logic-row">
      <div class="logic-step blk-blue" title="Bias ансамбля — ориентир направления">
        <div class="ls-title">BIAS</div>
        <div class="ls-sub">${bias.tf} / E${bias.period}</div>
        <div class="ls-tags"><span class="ls-tag on">MACD ${bias.tf}</span></div>
      </div>
      <div class="logic-arrow">→</div>
      <div class="logic-step blk-purple" title="Сетапы ансамбля (5m): голосуют за BUY/SELL">
        <div class="ls-title">SETUPS <span class="badge fam-momentum">${setups.length}</span></div>
        <div class="ls-sub">каждый независимо сигналит на ${cfg.entry_tf || "5min"}</div>
        <div class="ls-tags">${setupTags}${offTags ? `<span class="ls-tag off">…</span>` : ""}</div>
      </div>
      <div class="logic-arrow">→</div>
      <div class="logic-step blk-green" title="Кворум: сколько голосов нужно для входа">
        <div class="ls-title">QUORUM</div>
        <div class="ls-sub">k=${cfg.quorum} · ${cfg.neutral_mode ?? "semi_flip"}</div>
        <div class="ls-tags"><span class="ls-quorum ${quorumCls}">${funnel ? `⚑ входов ${(funnel.stats["signal:quorum_passed"] || 0)}` : "·"}</span></div>
      </div>
      <div class="logic-arrow">→</div>
      <div class="logic-step blk-orange" title="Заявка: после всех гейтов">
        <div class="ls-title">ORDER</div>
        <div class="ls-sub">entry ${cfg.entry_tf || "5min"} · горизонт сделки ~1 ч</div>
        <div class="ls-tags"><span class="ls-tag on">SL/TP</span><span class="ls-tag on">1 lot</span></div>
      </div>
    </div>`;
  let funnelHtml = "";
  if (funnel && funnel.stats && Object.keys(funnel.stats).length) {
    const names: Record<string, string> = {
      "signal:no_signal": "нет сигнала",
      "signal:quorum_passed": "кворум набран",
      "signal:rejected": "отклонено сигналом",
      "gate:rejected": "гейт отклонил",
      "order:submitted": "заявка выставлена",
      "order:filled": "исполнена",
      "exit:closed": "закрыта",
    };
    const total = Object.values(funnel.stats).reduce((a, b) => a + b, 0) || 1;
    const rows = Object.entries(funnel.stats).slice(0, 10).map(([k, v]) => {
      const nm = names[k] ?? k;
      const pct = (v / total) * 100;
      return `<div class="logic-funnel-row">
        <div class="lf-name" title="${esc(k)}">${esc(nm)}</div>
        <div class="lf-bar"><div class="lf-fill" style="width:${Math.max(2, Math.min(100, pct))}%"></div></div>
        <div class="lf-n">${v}</div>
      </div>`;
    }).join("");
    funnelHtml = `<div class="logic-funnel">${rows}</div>`;
  } else if (funnel) {
    funnelHtml = `<div class="mini-hint" style="margin-top:6px">Воронка пуста (бот не запущен или не видел сигналов)</div>`;
  }
  cont.innerHTML = knobs + row + funnelHtml;
}

async function skladLoadLogic(): Promise<void> {
  const cont = document.getElementById("logic-container");
  if (!cont) return;
  const st = document.getElementById("logic-status");
  if (st) st.textContent = "загрузка…";
  try {
    const [cfg, funnel] = await Promise.all([
      botEnsembleConfig(),
      botFunnel(undefined, undefined, 60).catch(() => null),
    ]);
    const figi = skladFigi();
    void figi;
    cont.innerHTML = "";
    skladRenderLogic(cfg, funnel ?? undefined);
    if (st) st.textContent = `${(cfg.setups ?? []).filter((s) => s.enabled).length} сетапов · k=${cfg.quorum} · фаннел ${funnel?.total ?? 0}`;
  } catch (e) {
    cont.innerHTML = `<div class="mini-hint">Ошибка: ${esc(e instanceof Error ? e.message : String(e))}</div>`;
    if (st) st.textContent = "ошибка";
  }
}

function startChart(): IChartApi | null {
  const host = document.getElementById("embedded-chart");
  if (!host || emChart) return emChart;
  host.classList.remove("hidden");
  emChart = createChart(host, {
    autoSize: true,
    layout: { background: { color: "#0a0e14" }, textColor: "#7e8999", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 11 },
    grid: { vertLines: { color: "rgba(255,255,255,0.04)" }, horzLines: { color: "rgba(255,255,255,0.04)" } },
    rightPriceScale: { borderColor: "#202939" },
    timeScale: { borderColor: "#202939", timeVisible: true, secondsVisible: false, tickMarkFormatter: emTickMark },
    crosshair: { mode: 0 },
    localization: { timeFormatter: emTimeLabel },
  });
  emSeries = emChart.addSeries(CandlestickSeries, {
    upColor: "#00d9a0", downColor: "#ff5d6c",
    borderVisible: false, wickUpColor: "#00d9a0", wickDownColor: "#ff5d6c",
  });
  emSeries.priceScale().applyOptions({ mode: priceScaleLog ? PriceScaleMode.Logarithmic : PriceScaleMode.Normal });
  emVolume = emChart.addSeries(HistogramSeries, {
    priceScaleId: "", priceLineVisible: false, lastValueVisible: false,
    priceFormat: { type: "volume" },
  }, 0);
  emVolume.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
  emSma = emChart.addSeries(LineSeries, {
    color: "#4a90e2", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
  }, 0);
  emEma = emChart.addSeries(LineSeries, {
    color: "#e6a23c", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    visible: indicators.ema,
  }, 0);
  emBbU = emChart.addSeries(LineSeries, {
    color: "rgba(155,89,182,0.8)", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    visible: indicators.bb,
  }, 0);
  emBbL = emChart.addSeries(LineSeries, {
    color: "rgba(155,89,182,0.8)", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    visible: indicators.bb,
  }, 0);
  emOscPane = indicators.macd || indicators.rsi;
  if (indicators.macd) {
    emMacdHist = emChart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, 1);
    emMacdLine = emChart.addSeries(LineSeries, {
      color: "#2962ff", lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
    }, 1);
    emSigLine = emChart.addSeries(LineSeries, {
      color: "#e6a23c", lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
    }, 1);
  }
  if (indicators.rsi) {
    emRsi = emChart.addSeries(LineSeries, {
      color: "#c39bd3", lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
    }, 1);
  }
  try {
    if (emOscPane) {
      const panes = emChart.panes();
      if (panes[1]) {
        panes[0].setStretchFactor(3);
        panes[1].setStretchFactor(2);
      }
    }
  } catch { /* panes API может отсутствовать в старых версиях */ }
  emMarkers = createSeriesMarkers(emSeries);
  emMarkersSig = createSeriesMarkers(emSeries);
  try {
    // Режим скролла: у правой кромки — LIVE_FOLLOW (refresh догоняет),
    // юзер ушёл влево — USER_EXPLORE (refresh не двигает вьюпорт).
    emChart.timeScale().subscribeVisibleLogicalRangeChange(() => {
      if (!emChart || chartMode === "TRADE_FOCUS") return;
      const len = lastAnalysis?.candles.length ?? 0;
      if (!len) return;
      const lg = emChart.timeScale().getVisibleLogicalRange();
      if (!lg) return;
      _setChartMode(lg.to >= len - 3 ? "LIVE_FOLLOW" : "USER_EXPLORE");
    });
  } catch { /* API может отсутствовать */ }
  setupEmTooltip(host);
  initChartToolbar();
  return emChart;
}

function setupEmTooltip(host: HTMLElement): void {
  if (!emChart) return;
  const area = host.parentElement;
  const tooltipEl = document.getElementById("tooltip");
  if (!area || !tooltipEl) return;
  const resetView = () => {
    try {
      emChart!.timeScale().resetTimeScale();
      emChart!.timeScale().scrollToRealTime();
    } catch { /* noop */ }
  };
  area.addEventListener("dblclick", resetView);
  const priceFmt = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const volFmt = new Intl.NumberFormat("ru-RU", { notation: "compact" });
  emChart.subscribeCrosshairMove((param) => {
    if (!param.point || !param.time || !lastAnalysis || !lastAnalysis.candles.length) {
      tooltipEl.classList.add("hidden");
      updateEmLegend(null);
      return;
    }
    const candle = param.seriesData.get(emSeries!) as CandlestickData | undefined;
    const vol = param.seriesData.get(emVolume!) as HistogramData | undefined;
    if (!candle) {
      tooltipEl.classList.add("hidden");
      updateEmLegend(null);
      return;
    }
    let idx: number | null = lastAnalysis.candles.findIndex((c) => Math.floor(new Date(c.ts).getTime() / 1000) === param.time);
    if (idx === -1) idx = null;
    updateEmLegend(param.time);
    const m = idx != null ? lastAnalysis.macd.macd[idx] : null;
    const s = idx != null ? lastAnalysis.macd.signal[idx] : null;
    const h = idx != null ? lastAnalysis.macd.hist[idx] : null;
    const ts = idx != null ? lastAnalysis.candles[idx].ts : "";
    const change =
      idx != null && idx > 0
        ? ((candle.close - lastAnalysis.candles[idx - 1].close) / lastAnalysis.candles[idx - 1].close) * 100
        : 0;
    tooltipEl.innerHTML = `
      <div class="t-date">${ts ? fmtEmDateTime(ts) : ""}</div>
      <div class="row"><span class="k">Откр.</span><span>${priceFmt.format(candle.open)} ₽</span></div>
      <div class="row"><span class="k">Макс.</span><span>${priceFmt.format(candle.high)} ₽</span></div>
      <div class="row"><span class="k">Мин.</span><span>${priceFmt.format(candle.low)} ₽</span></div>
      <div class="row"><span class="k">Закр.</span><span>${priceFmt.format(candle.close)} ₽</span></div>
      <div class="row"><span class="k">Изм.</span><span>${change >= 0 ? "+" : ""}${change.toFixed(2)}%</span></div>
      <div class="row"><span class="k">Объём</span><span>${vol != null ? volFmt.format(vol.value) : "—"}</span></div>
      ${
        m != null
          ? `<div class="t-date" style="margin-top:4px">MACD 12 26 9</div>
             <div class="row"><span class="k">MACD</span><span>${m.toFixed(3)}</span></div>
             <div class="row"><span class="k">Сигнал</span><span>${s?.toFixed(3) ?? "—"}</span></div>
             <div class="row"><span class="k">Гист.</span><span>${h?.toFixed(3) ?? "—"}</span></div>`
          : ""
      }
    `;
    const areaRect = area.getBoundingClientRect();
    let x = param.point.x + 18;
    let y = param.point.y + 14;
    const tw = tooltipEl.offsetWidth;
    const th = tooltipEl.offsetHeight;
    if (x + tw > areaRect.width - 8) x = Math.max(0, param.point.x - tw - 18);
    if (y + th > areaRect.height - 8) y = Math.max(0, param.point.y - th - 14);
    tooltipEl.style.left = `${x}px`;
    tooltipEl.style.top = `${y}px`;
    tooltipEl.classList.remove("hidden");
  });
}

// Тихий авто-рефреш (раз в 8с). Правила:
//  - TRADE_FOCUS (пользователь смотрит историческую сделку) — свечи НЕ трогаем,
//    обновляем только линии входа/SL/TP;
//  - снапшот вьюпорта снимается ДО setData, восстанавливается ПОСЛЕ;
//  - данные неизменились (по сигнатуре последней свечи) — не перерисовываем.
async function refreshEmbedChart(): Promise<void> {
  const lf = lastFocus;
  if (!lf?.figi || !emChart) return;
  if (document.hidden) return;
  if (_loadInFlight) return; // не мешаем недозагруженному фокусу
  if (chartMode === "TRADE_FOCUS") { _applyOverlay(lf); return; }
  const requestId = _chartReqId;
  const intervalAt = chartInterval;
  let nd: AnalysisDto | null = null;
  try {
    nd = await fetchAnalysis(lf.figi, intervalAt, 2000);
  } catch (err) {
    if (!_isAbort(err)) console.debug("[chart] refresh error", err);
    return;
  }
  if (requestId !== _chartReqId || chartInterval !== intervalAt) return; // фокус успел смениться
  if (!nd || !nd.candles || !nd.candles.length) return;
  if (nd.figi !== lf.figi) return;
  // Окно истории / тест: последний бар старый — живой привязки нет, не трогаем.
  const lastTs = new Date(nd.candles[nd.candles.length - 1].ts).getTime() / 1000;
  if (Date.now() / 1000 - lastTs > 1800) return;
  const prev = lastAnalysis;
  const ndTimes = emCandleTimes(nd);
  const prevFirst = prev ? new Date(prev.candles[0].ts).getTime() / 1000 : 0;
  const incremental = !!prev && ndTimes[0] === prevFirst;
  const changed = !prev
    || prev.candles.length !== nd.candles.length
    || _candleSig(prev.candles[prev.candles.length - 1]) !== _candleSig(nd.candles[nd.candles.length - 1]);
  if (!changed) { _applyOverlay(lf); return; }
  const snap = captureViewport();
  lastAnalysis = nd;
  _applySeriesData(nd, { incremental });
  updateEmLegend(null);
  _applyOverlay(lf);
  restoreViewport(snap);
  console.debug("[chart] refresh", "bars=" + nd.candles.length, "inc=" + incremental, "following=" + snap.following);
}

function initEmbedded(): void {
  document.body.classList.add("embedded");
  document.getElementById("page-chart")?.classList.add("active");
  startChart();
  window.addEventListener("message", onEmbedMessage);
  // Handshake: сообщаем родителю, что слушатель готов. До этого фокус не слали бы
  // вообще — иначе сообщение могло уйти в пустоту, а дефолт загрузился поверх него.
  try { window.parent?.postMessage({ type: "chart-ready" }, "*"); } catch { /* cross-origin */ }
  // Без focus-сообщения график был пустым всю страницу — грузим дефолт сразу
  // и авто-обновляем раз в 8с (как оригинал) либо последний показанный инструмент.
  void loadDefaultEmbedChart();
  window.setInterval(() => {
    void refreshEmbedChart();
  }, 8000);
}

async function loadDefaultEmbedChart(): Promise<void> {
  let figi = "";
  try { figi = localStorage.getItem("deeptrading_chart_figi") || ""; } catch { }
  if (!figi) figi = DEFAULT_CHART_FIGI;
  await loadEmbedChart({ figi, ticker: "", trade: null, trades: [] });
}

const DEFAULT_CHART_FIGI = "BBG004730N88";

function initAiModeSelect(): void {
  const sel = $("ai-mode-select") as HTMLSelectElement;
  if (!sel || sel.dataset.aiBound) return;
  sel.dataset.aiBound = "1";
  sel.addEventListener("change", () => {
    sel.dataset.aiUser = "1"; // не даём поллу перезаписать пока сохраняем
    void (async () => {
      const st = $("topbar-status");
      const before = st ? st.textContent : "";
      try {
        await botAiControlPut({ mode: sel.value });
        if (st) st.textContent = `AI-режим: ${sel.value || "не задан"} — сохранён`;
        window.setTimeout(() => { if (st && st.textContent && st.textContent.startsWith("AI-режим:")) st.textContent = before; }, 4000);
      } catch (e) {
        if (st) st.textContent = "AI: не сохранить — " + (e instanceof Error ? e.message : String(e));
      } finally {
        delete sel.dataset.aiUser;
      }
    })();
  });
}

async function initMainChart(): Promise<void> {
  startChart();
  if (!emChart) return;
  skladInitPanel();
  let figi = "";
  try { figi = localStorage.getItem("deeptrading_chart_figi") || ""; } catch { }
  if (!figi) { figi = DEFAULT_CHART_FIGI; try { localStorage.setItem("deeptrading_chart_figi", figi); } catch { } }
  await loadEmbedChart({ figi, ticker: "", trade: null, trades: [] });
}

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

  $("rep-refresh")?.addEventListener("click", () => { void loadRepRuns(); });
  document.getElementById("rep-kind")?.addEventListener("change", () => { void loadRepRuns(); });
  document.getElementById("rep-query")?.addEventListener("input", () => { void loadRepRuns(); });
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
    void loadRepTrades(true);
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


if (IS_EMBEDDED) {
  initEmbedded();
} else {
  initBotChartBridge();
  initAnalyticsTabs();
  initNav();
  initSidebarResize();
  initRightRailResize();
  initSidebarCollapse();
  initRightRail();
  initAIgate();
  initAiModeSelect();
  initBotChartResizer();
  startPolling();
  void initMainChart();
}