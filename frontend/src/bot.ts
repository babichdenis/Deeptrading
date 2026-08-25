import {
  botCancelPending,
  botCloseAll,
  botEvents,
  type BotEventRow,
  type BotOrderRow,
  botOrders,
  botPause,
  botPositions,
  botReset,
  botStart,
  botStatus,
  botStop,
  botTrades,
  fetchCatalog,
  type BotPositionRow,
  type BotTradeRow,
  type StrategyCardDto,
} from "./api";

const $ = (id: string) => document.getElementById(id) as HTMLElement;

let catalog: StrategyCardDto[] = [];
let lastRunning: boolean | null = null;
let entriesPaused = false;

function money(n: number | null | undefined): string {
  if (n == null) return "—";
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(n);
}

export async function initBot(onStateChange?: (running: boolean) => void) {
  const cat = await fetchCatalog();
  catalog = cat.strategies.filter((c) => c.status === "AVAILABLE");
  ($("bot-strategy") as HTMLSelectElement).innerHTML = catalog
    .map((c) => `<option value="${c.id}">${c.name}</option>`)
    .join("");
  $("bot-strategy").addEventListener("change", renderStrategyParams);
  renderStrategyParams();

  $("btn-bot-start").addEventListener("click", () => void doStart());
  $("btn-bot-stopui").addEventListener("click", () => void doStop());
  $("btn-bot-reset").addEventListener("click", () => void doReset());
  $("btn-bot-pause").addEventListener("click", () => void doPause());
  $("btn-bot-cancelpending").addEventListener("click", () => void doCancelPending());
  $("btn-bot-closeall").addEventListener("click", () => void doCloseAll());

  void pollOnce();
  setInterval(() => void pollOnce(onStateChange), 8000);
  setInterval(() => void pollEvents(), 3000);
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
      strategy_id: ($("bot-strategy") as HTMLSelectElement).value,
      params: readParams(),
      interval_name: ($("bot-interval") as HTMLSelectElement).value,
      top_n: Number(($("bot-topn") as HTMLInputElement).value),
      qty_per_trade: Number(($("bot-qty") as HTMLInputElement).value),
      stop_pct: Number(($("bot-stop") as HTMLInputElement).value) / 100,
      target_pct: Number(($("bot-target") as HTMLInputElement).value) / 100,
      allow_short: ($("bot-short") as HTMLInputElement).checked,
      initial_cash: Number(($("bot-cash") as HTMLInputElement).value),
    });
    await pollOnce();
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    $("btn-bot-start").classList.remove("busy");
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
  const cash = Number(($("bot-cash") as HTMLInputElement).value) || 100000;
  try {
    await botReset(cash);
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    await pollOnce();
  }
}

async function pollOnce(onStateChange?: (running: boolean) => void) {
  let st;
  try {
    st = await botStatus();
  } catch {
    return;
  }

  const pill = $("bot-pill");
  const pillTop = $("bot-pill-top");
  for (const el of [pill, pillTop]) {
    el.classList.toggle("on", st.running);
    el.classList.toggle("off", !st.running);
  }
  $("bot-state-short").textContent = st.running ? "BOT ON" : "OFF";
  $("bot-state-text").textContent = st.running ? `Бот работает · ${st.mode}` : "Бот выключен";

  const toggle = $("btn-bot-toggle");
  toggle.textContent = st.running ? "■ Стоп" : "▶ Старт";
  toggle.classList.toggle("stop", st.running);
  toggle.classList.toggle("start", !st.running);

  const modeChip = $("bot-mode");
  if (st.running && st.mode !== "—") {
    modeChip.textContent = st.mode;
    modeChip.classList.remove("hidden");
  } else {
    modeChip.classList.add("hidden");
  }

  renderStatusStrip(st);

  if (st.error) st.error && ($("bs-mode").textContent = "ошибка");

  $("bs-mode").textContent = st.running ? st.mode : "—";
  $("bs-candles").textContent = String(st.candles_seen);
  $("bs-signals").textContent = String(st.signals_seen);

  $("equity-chip").textContent = `${money(st.portfolio.equity)} ₽`;
  $("bs-equity").textContent = `${money(st.portfolio.equity)} ₽`;
  const pnlEl = $("bs-pnl");
  pnlEl.textContent = `${st.portfolio.pnl >= 0 ? "+" : ""}${money(st.portfolio.pnl)} ₽`;
  pnlEl.className = "stat-v " + (st.portfolio.pnl >= 0 ? "pos" : "neg");

  renderUniverse(st.universe);

  if (lastRunning !== st.running) {
    lastRunning = st.running;
    onStateChange?.(st.running);
  }

  if ($("page-bot").classList.contains("active")) {
    $("bs-positions").textContent = String(st.portfolio.positions_open);
    const positions: BotPositionRow[] = await botPositions().catch(() => []);
    document.querySelector("#bot-positions-table tbody")!.innerHTML =
      positions
        .map(
          (p) =>
            `<tr><td><b>${p.ticker}</b></td><td>${p.side}</td><td class="num">${p.qty}</td>` +
            `<td class="num">${money(p.entry_price)}</td><td class="num">${p.stop_loss ? money(p.stop_loss) : "—"}</td>` +
            `<td class="num">${p.take_profit ? money(p.take_profit) : "—"}</td></tr>`
        )
        .join("") || `<tr><td colspan=6 style="color:var(--text-dim)">нет открытых позиций</td></tr>`;

    const trades: BotTradeRow[] = await botTrades(200).catch(() => []);
    renderTrades(trades);
    renderPortfolioSummary(st, trades);
    void renderOrders();
  }
}

function renderStatusStrip(st: Awaited<ReturnType<typeof botStatus>>) {
  const statusChip = $("strip-status");
  statusChip.classList.toggle("on", st.running);
  statusChip.classList.toggle("off", !st.running);
  statusChip.querySelector("span")!.textContent = st.running ? "RUNNING" : "OFF";

  const modeChip = $("strip-mode");
  modeChip.classList.toggle("warn", st.running && st.mode !== "PAPER" && st.mode !== "—");
  modeChip.querySelector("span")!.textContent = st.mode || "—";

  const sessionChip = $("strip-session");
  const session = st.session ?? "—";
  sessionChip.classList.toggle("on", session === "TRADING" || session === "EVENING");
  sessionChip.classList.toggle("warn", session === "OPENING" || session === "CLEARING");
  sessionChip.classList.toggle("off",
    session === "PRE_MARKET" || session === "POST_MARKET" || session === "WEEKEND" || session === "—");
  sessionChip.querySelector("span")!.textContent = session;

  const dataChip = $("strip-data");
  const health = st.data?.health ?? (st.candles_seen > 0 ? "HEALTHY" : "NO_DATA");
  dataChip.classList.toggle("on", health === "HEALTHY");
  dataChip.classList.toggle("warn", health === "STALE");
  dataChip.classList.toggle("off", health === "NO_DATA");
  dataChip.querySelector("span")!.textContent =
    `${health.toLowerCase()} · ${st.data?.source ?? st.mode}`;

  const sigChip = $("strip-signals");
  sigChip.querySelector("span")!.textContent = String(st.signals_seen);

  const risk = st.risk;
  const riskChip = $("strip-risk");
  const riskState = risk?.state ?? "NORMAL";
  riskChip.classList.toggle("err", riskState === "LOSS_LIMIT");
  riskChip.classList.toggle("warn", riskState === "PAUSED");
  riskChip.classList.toggle("on", riskState === "NORMAL");
  riskChip.querySelector("span")!.textContent =
    `${riskState} · ${risk ? `${risk.daily_pnl >= 0 ? "+" : ""}${risk.daily_pnl} ₽` : "—"}`;

  entriesPaused = risk?.entries_paused ?? entriesPaused;
  updatePauseButton();
  $("pending-count").textContent = String(st.pending_orders ?? 0);

  const errChip = $("strip-error");
  if (st.error) {
    errChip.classList.remove("hidden");
    errChip.querySelector("span")!.textContent = st.error.slice(0, 80);
  } else {
    errChip.classList.add("hidden");
  }
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

async function renderOrders() {
  let orders: BotOrderRow[];
  try {
    orders = await botOrders(30);
  } catch {
    return;
  }
  document.querySelector("#bot-orders-table tbody")!.innerHTML =
    orders
      .map(
        (o) =>
          `<tr><td class="num">${new Date(o.created_at).toLocaleTimeString("ru-RU")}</td>` +
          `<td><b>${o.ticker}</b></td><td>${o.action}</td><td>${o.side}</td><td class="num">${o.qty}</td>` +
          `<td class="${o.status === "FILLED" ? "pos" : o.status === "CANCELLED" ? "neg" : ""}">${o.status}</td>` +
          `<td class="num">${o.price != null ? money(o.price) : "—"}</td>` +
          `<td style="color:var(--text-dim);font-size:10px">${o.id}</td></tr>`
      )
      .join("") || `<tr><td colspan=8 style="color:var(--text-dim)">заявок пока нет</td></tr>`;
}

function renderTrades(trades: BotTradeRow[]) {
  document.querySelector("#bot-trades-table tbody")!.innerHTML =
    trades
      .slice(0, 20)
      .map(
        (t) =>
          `<tr><td><b>${t.ticker}</b></td><td>${t.side} ${t.qty}</td>` +
          `<td class="num">${money(t.entry_price)} → ${money(t.exit_price)}</td>` +
          `<td class="num ${t.net_pnl >= 0 ? "pos" : "neg"}">${money(t.net_pnl)}</td><td>${t.exit_reason}</td></tr>`
      )
      .join("") || `<tr><td colspan=5 style="color:var(--text-dim)">пока нет сделок</td></tr>`;
  document.querySelector("#pf-trades-table tbody")!.innerHTML =
    trades
      .map(
        (t) =>
          `<tr><td><b>${t.ticker}</b></td><td>${t.side}</td><td class="num">${t.qty}</td>` +
          `<td class="num">${money(t.entry_price)}</td><td class="num">${money(t.exit_price)}</td>` +
          `<td class="num ${t.net_pnl >= 0 ? "pos" : "neg"}">${money(t.net_pnl)}</td><td>${t.exit_reason}</td></tr>`
      )
      .join("") || `<tr><td colspan=7 style="color:var(--text-dim)">сделок пока нет</td></tr>`;
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

async function renderUniverse(universe: Array<{ ticker: string; atr_pct: number }>) {
  $("bot-universe").innerHTML = universe.length
    ? universe.map((u) => `<span class="u-chip">${u.ticker} · ATR ${u.atr_pct}%</span>`).join("")
    : "";
}
