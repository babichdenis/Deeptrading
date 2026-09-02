import {
  botCancelPending,
  botCloseAll,
  botEvents,
  type BotEventRow,
  type BotOrderRow,
  botPause,
  botReset,
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
  type SandboxPositionRow,
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

function fmtTime(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function sideIcon(side: string): string {
  const s = side.toUpperCase();
  if (s === "LONG" || s === "BUY") return `<span class="side-up">▲</span>`;
  if (s === "SHORT" || s === "SELL") return `<span class="side-down">▼</span>`;
  return side;
}

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
    const resp = await fetch(`/api/v1/bot/positions/close?figi=${encodeURIComponent(pos.figi)}`, { method: "POST" });
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

export async function initBot(onStateChange?: (running: boolean) => void) {
  $("btn-bot-start").addEventListener("click", () => void doStart());
  $("btn-bot-stop").addEventListener("click", () => void doStop());

  document.querySelector("#bot-positions-table tbody")?.addEventListener("click", (e) => {
    const tr = (e.target as HTMLElement).closest("tr") as HTMLElement | null;
    if (!tr) return;
    const idx = Array.from(tr.parentElement!.children).indexOf(tr);
    const p = _lastPositions[idx];
    if (p) sendEmbedFocus(p.figi, p.ticker, positionAsTrade(p));
  });
  document.querySelector("#bot-trades-table tbody")?.addEventListener("click", (e) => {
    const tr = (e.target as HTMLElement).closest("tr") as HTMLElement | null;
    if (!tr) return;
    const idx = Array.from(tr.parentElement!.children).indexOf(tr);
    const t = _lastTrades.slice(0, 30)[idx];
    if (t) sendEmbedFocus(t.figi, t.ticker, tradeAsTrade(t));
  });

  void pollOnce();
  setInterval(() => void pollOnce(onStateChange), 8000);
  setInterval(() => void pollEvents(), 3000);
  setInterval(() => void pollLogs(), 1000);
  setupLogFilters();
  initBotResizer();
}

const LGF = { candles: true, signals: true, trades: true, events: true };
let _allLogs: string[] = [];
let _lastPositions: SandboxPositionRow[] = [];
let _lastTrades: BotTradeRow[] = [];
let _chartInit = false;

function botEmbedFrame(): HTMLIFrameElement | null {
  return document.getElementById("bot-chart-frame") as HTMLIFrameElement | null;
}
function botIso(s?: string | null): string | undefined {
  if (!s) return undefined;
  return s.includes("T") ? s : s.replace(" ", "T");
}
function sendEmbedFocus(figi: string, ticker: string, trade: Record<string, unknown>) {
  const f = botEmbedFrame();
  if (!f || !f.contentWindow) return;
  f.contentWindow.postMessage({ type: "focus", figi, ticker, trade }, "*");
}
function positionAsTrade(p: SandboxPositionRow): Record<string, unknown> {
  return {
    side: p.side, entry_time: botIso(p.entry_time), entry_price: p.entry_price,
    stop_loss: p.stop_loss, take_profit: p.take_profit,
  };
}
function tradeAsTrade(t: BotTradeRow): Record<string, unknown> {
  return {
    side: t.side,
    entry_time: botIso((t as { entry_time?: string }).entry_time ?? t.ts),
    exit_time: t.ts ? botIso(t.ts) : undefined,
    entry_price: (t as { entry_price?: number }).entry_price ?? t.price,
    exit_price: (t as { exit_price?: number }).exit_price,
    stop_loss: (t as { stop_loss?: number | null }).stop_loss,
    take_profit: (t as { take_profit?: number | null }).take_profit,
    net_pnl: t.net_pnl,
  };
}


function logKind(l: string): string {
  if (l.includes("СВЕЧА")) return "candles";
  if (l.includes("СИГНАЛ")) return "signals";
  if (l.includes("СДЕЛКА") || l.includes("ВЫХОД")) return "trades";
  return "events";
}

function renderLogs() {
  const el = $("bot-live-logs");
  if (!el) return;
  const rows = _allLogs.filter((l) => LGF[logKind(l) as keyof typeof LGF]).slice(-250);
  el.innerHTML = rows.length
    ? rows.map((l) => {
        let cls = "";
        if (l.includes("ПРОПУСК") || l.includes("ошибк") || l.includes("ERROR")) cls = ' class="lg-err"';
        else if (l.includes("СИГНАЛ")) cls = ' class="lg-sig"';
        else if (l.includes("ВЫХОД") || l.includes("ЗАКРЫТИЕ")) cls = ' class="lg-exit"';
        else if (l.includes("СДЕЛКА")) cls = ' class="lg-fill"';
        else if (l.includes("СВЕЧА")) cls = ' class="lg-cnd"';
        return `<div${cls}>${l}</div>`;
      }).join("")
    : '<span style="color:#666">нет записей под фильтр</span>';
  el.scrollTop = el.scrollHeight;
}

async function pollLogs() {
  const el = $("bot-live-logs");
  if (!el) return;
  try {
    const r = await fetch("/api/v1/bot/logs?limit=400");
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
      renderLogs();
      if (key === "candles") {
        void fetch("/api/v1/bot/logconfig", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ log_candles: cb.checked }),
        });
      }
    });
  };
  setLG("lg-candles", "candles");
  setLG("lg-signals", "signals");
  setLG("lg-trades", "trades");
  setLG("lg-events", "events");
  const clear = $("lg-clear");
  if (clear) clear.addEventListener("click", () => {
    _allLogs = [];
    renderLogs();
    void fetch("/api/v1/bot/logs/clear", { method: "POST" });
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
  btn.textContent = entriesPaused ? "▶ Снять паузу входов" : "⏸ Пауза новых входов";
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

async function doStart() {
  try {
    $("btn-bot-start").classList.add("busy");
    await botStart({
      strategy_id: "ensemble_v4",
      params: {},
      interval_name: "1min",
      top_n: 6,
      qty_per_trade: 1,
      stop_pct: 0.008,
      target_pct: 0.02,
      allow_short: false,
      initial_cash: 10000,
      mode: "sandbox",
      use_ensemble: true,
      ensemble_capital: 0,
      ensemble_quorum: 2,
      ensemble_session: "main",
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
      const r = await fetch("/api/v1/bot/status");
      return r.ok ? await r.json() : null;
    } catch {
      return null;
    }
  })();

  const backendAlive = !!bst;
  const engineRunning = bst ? !!bst.running : false;
  const botErr = bst && bst.error ? bst.error : null;

  if (pillTop) {
    pillTop.classList.remove("warn");
    pillTop.classList.toggle("on", engineRunning);
    pillTop.classList.toggle("off", !backendAlive);
  }
  $("bot-state-text").textContent =
    botErr ? `Бот ошибка: ${botErr.slice(0, 60)}`
      : engineRunning ? `Бот работает · ${bst.mode}`
      : backendAlive ? "Бот остановлен" : "Бот недоступен";

  $("btn-bot-start").classList.toggle("hidden", engineRunning);
  $("btn-bot-stop").classList.toggle("hidden", !engineRunning);

  const modeChip = $("bot-mode");
  if (bst && bst.running && bst.mode !== "—") {
    modeChip.textContent = bst.mode;
    modeChip.classList.remove("hidden");
  } else {
    modeChip.classList.add("hidden");
  }

  if (bst && bst.error) $("bs-mode").textContent = "ошибка";
  $("bs-mode").textContent = bst && bst.running ? bst.mode : "—";
  $("bs-candles").textContent = bst && bst.candles_seen != null ? String(bst.candles_seen) : "—";
  $("bs-signals").textContent = bst && bst.signals_seen != null ? String(bst.signals_seen) : "—";
  const sessEl = $("bs-session");
  if (sessEl) {
    const s = bst && bst.session ? bst.session : "—";
    sessEl.textContent = s;
    sessEl.classList.remove("pos", "neg");
    if (s === "TRADING") sessEl.classList.add("pos");
    else if (s === "OPENING" || s === "EVENING") sessEl.classList.add("pos");
  }

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
  const bsEq = $("bs-equity");
  if (bsEq) bsEq.textContent = `${money(st.portfolio.equity)} ₽`;
  const pnlEl = $("bs-pnl");
  if (pnlEl) {
    pnlEl.textContent = `${st.portfolio.pnl >= 0 ? "+" : ""}${money(st.portfolio.pnl)} ₽`;
    pnlEl.className = "stat-v " + (st.portfolio.pnl >= 0 ? "pos" : "neg");
  }
  const bsPos = $("bs-positions");
  if (bsPos) bsPos.textContent = String(st.portfolio.positions_open);

  if (!$("page-bot")) return;

  try {
  const positions: SandboxPositionRow[] = await sandboxPositions().catch(() => []);
  _lastPositions = positions;
  document.querySelector("#bot-positions-table tbody")!.innerHTML =
    positions
      .map((p) => {
        const trend = priceTrend(p.current_price, p.prev_close);
        const closeAction = p.side.toUpperCase() === "LONG" ? "Продать" : "Купить";
        const closeClass = p.side.toUpperCase() === "LONG" ? "btn-sell" : "btn-buy";
        return `<tr>` +
          `<td class="num" style="font-size:10px;color:var(--text-dim)">${fmtTime(p.entry_time)}</td>` +
          `<td><b>${p.ticker}</b></td>` +
          `<td>${sideIcon(p.side)}</td>` +
          `<td class="num">${p.qty}</td>` +
          `<td class="num">${price(p.entry_price)} <span class="dim">(${money(p.entry_price * p.qty)}₽)</span></td>` +
          `<td class="num${trend}">${price(p.current_price)}</td>` +
          `<td class="num ${p.unrealized_pnl >= 0 ? "pos" : "neg"}">${p.unrealized_pnl >= 0 ? "+" : ""}${money(p.unrealized_pnl)}</td>` +
          `<td class="num">${p.stop_loss != null ? price(p.stop_loss) : "—"}</td>` +
          `<td class="num">${p.take_profit != null ? price(p.take_profit) : "—"}</td>` +
          `<td><button class="btn-sm ${closeClass}" onclick="window.__closePosition('${p.ticker}','${p.side}',${p.qty})">${closeAction}</button></td>` +
          `</tr>`;
      })
      .join("") || `<tr><td colspan=10 style="color:var(--text-dim)">нет открытых позиций</td></tr>`;

  const trades: BotTradeRow[] = await sandboxTrades(200).catch(() => []);
  _lastTrades = trades;
  renderTrades(trades);
  renderPortfolioSummary(st, trades);
  if (!_chartInit && positions.length > 0) {
    _chartInit = true;
    const first = positions[0];
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
      const t = new Date(e.ts).toLocaleTimeString("ru-RU");
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
    if (el.id === "portfolio-summary" || el.id === "bot-events" || el.id === "cfg-editor") return;
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
      .slice(0, 30)
      .map(
        (t) => {
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
          return `<tr>` +
            `<td style="font-size:10px;color:var(--text-dim)">${timeCell}</td>` +
            `<td><b>${t.ticker}</b></td>` +
            `<td>${sideIcon(t.side)}</td>` +
            `<td class="num">${t.qty}</td>` +
            `<td class="num">${pxCell}</td>` +
            `<td class="num">${pnlCell}</td>` +
            `<td class="num" style="color:var(--text-dim)">${!isOpen && t.commission != null ? money(t.commission) : "—"}</td>` +
            `<td>${isOpen ? "открыта · ждём выхода" : t.exit_reason}</td>` +
            `</tr>`;
        }
      )
      .join("") || `<tr><td colspan=8 style="color:var(--text-dim)">пока нет сделок</td></tr>`;
  document.querySelector("#pf-trades-table tbody")!.innerHTML =
    trades
      .map(
        (t) =>
          `<tr><td style="font-size:10px;color:var(--text-dim)">${fmtTime(t.ts)}</td>` +
          `<td><b>${t.ticker}</b></td><td>${sideIcon(t.side)}</td><td class="num">${t.qty}</td>` +
          `<td class="num">${price(t.price != null ? t.price : t.entry_price)}</td><td class="num">${price(t.exit_price)}</td>` +
          `<td class="num ${t.net_pnl >= 0 ? "pos" : "neg"}">${t.net_pnl >= 0 ? "+" : ""}${money(t.net_pnl)}</td>` +
          `<td class="num" style="color:var(--text-dim)">${t.commission != null ? money(t.commission) : "—"}</td>` +
          `<td>${t.exit_reason}</td></tr>`
      )
      .join("") || `<tr><td colspan=9 style="color:var(--text-dim)">сделок пока нет</td></tr>`;
}

function renderPortfolioSummary(
  st: { portfolio: { cash: number; equity: number; pnl: number; initial_cash: number } },
  trades: BotTradeRow[],
) {
  const p = st.portfolio;
  const wins = trades.filter((t) => t.net_pnl > 0).length;
  $("portfolio-summary").innerHTML = `
    <div class="metrics-grid">
      <div class="metric"><span class="k">Equity</span><span class="v">${money(p.equity)} ₽</span></div>
      <div class="metric"><span class="k">Кеш</span><span class="v">${money(p.cash)} ₽</span></div>
      <div class="metric"><span class="k">P&L всего</span><span class="v ${p.pnl >= 0 ? "pos" : "neg"}">${p.pnl >= 0 ? "+" : ""}${money(p.pnl)} ₽</span></div>
      <div class="metric"><span class="k">Сделок</span><span class="v">${trades.length}</span></div>
      <div class="metric"><span class="k">Прибыльных</span><span class="v">${wins}</span></div>
      <div class="metric"><span class="k">Win rate</span><span class="v">${trades.length ? Math.round(wins / trades.length * 100) : 0}%</span></div>
    </div>`;
}
