const API = window.location.port === "5173" ? `http://${window.location.hostname}:8000` : "";

import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  createSeriesMarkers,
  LineSeries,
  type CandlestickData,
  type IChartApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { IPrimitivePaneView } from "lightweight-charts";
import { ZonesPrimitive, toUnix } from "./labchart";
import { fetchEnsemble, fetchEnsembleSweep, fetchMaxProfit, type CandleDto, type EnsembleResponse, type TestMaxProfitResponse } from "./api";

const $ = (id: string) => document.getElementById(id) as HTMLElement;

const money = (n: number | null | undefined): string =>
  n == null ? "—" : new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(n);

const pct = (n: number | null | undefined): string =>
  n == null ? "—" : `${n.toFixed(2)}%`;

interface TestHandle {
  destroy: () => void;
}

function renderTestChart(
  container: HTMLElement,
  candles: CandleDto[],
  swings: Array<{ kind: "low" | "high"; ts: string; price: number }>,
  trades: TestMaxProfitResponse["trades"],
): TestHandle {
  container.innerHTML = "";

  const chart = createChart(container, {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: "#131722" },
      textColor: "#787b86",
    },
    grid: { vertLines: { color: "#1e2433" }, horzLines: { color: "#1e2433" } },
    crosshair: { mode: CrosshairMode.Normal },
    rightPriceScale: { borderColor: "#2a2e39" },
    timeScale: { borderColor: "#2a2e39", timeVisible: true, secondsVisible: false },
  });

  const candleSeries = chart.addSeries(CandlestickSeries, {
    upColor: "#26a69a",
    downColor: "#ef5350",
    wickUpColor: "#26a69a",
    wickDownColor: "#ef5350",
    borderVisible: false,
  });

  const data: CandlestickData[] = candles
    .slice()
    .sort((a, b) => toUnix(a.ts) - toUnix(b.ts))
    .map((c) => ({ time: toUnix(c.ts), open: c.open, high: c.high, low: c.low, close: c.close }));
  candleSeries.setData(data);

  const markers: SeriesMarker<Time>[] = swings.map((s) => ({
    time: toUnix(s.ts),
    position: s.kind === "low" ? "belowBar" : "aboveBar",
    shape: s.kind === "low" ? "arrowUp" : "arrowDown",
    color: s.kind === "low" ? "#26a69a" : "#ef5350",
    text: s.kind === "low" ? "L" : "H",
  }));

  const zones: Array<{ timeFrom: UTCTimestamp; timeTo: UTCTimestamp; win: boolean }> = [];
  for (const t of trades) {
    zones.push({ timeFrom: toUnix(t.entry_ts), timeTo: toUnix(t.exit_ts), win: t.pnl >= 0 });
    markers.push({
      time: toUnix(t.entry_ts),
      position: "belowBar",
      shape: "arrowUp",
      color: "#b388ff",
      text: `▶${t.lots}`,
    });
    markers.push({
      time: toUnix(t.exit_ts),
      position: "aboveBar",
      shape: "circle",
      color: t.pnl >= 0 ? "#26a69a" : "#ef5350",
      text: money(t.pnl),
    });
  }
  markers.sort((a, b) => Number(a.time) - Number(b.time));
  const markersApi = createSeriesMarkers(candleSeries, []);
  markersApi.setMarkers(markers);

  if (zones.length > 0) {
    candleSeries.attachPrimitive(new ZonesPrimitive(chart, zones));
  }

  chart.timeScale().fitContent();

  return {
    destroy: () => {
      chart.remove();
      container.innerHTML = "";
    },
  };
}

function renderEquityChart(container: HTMLElement, equity: Array<{ ts: string; equity: number }>) {
  container.innerHTML = "";
  if (equity.length === 0) {
    container.innerHTML = `<span class="mini-hint">нет сделок</span>`;
    return;
  }
  const chart = createChart(container, {
    autoSize: true,
    height: 180,
    layout: {
      background: { type: ColorType.Solid, color: "#131722" },
      textColor: "#787b86",
    },
    grid: { vertLines: { color: "#1e2433" }, horzLines: { color: "#1e2433" } },
    rightPriceScale: { borderColor: "#2a2e39" },
    timeScale: { borderColor: "#2a2e39", timeVisible: true, secondsVisible: false },
  });
  const line = chart.addSeries(LineSeries, { color: "#2962ff", lineWidth: 2 });
  line.setData(
    equity.map((p) => ({ time: toUnix(p.ts), value: p.equity })),
  );
  chart.timeScale().fitContent();
}

function renderStats(d: TestMaxProfitResponse) {
  const cap = d.params.capital;
  const row = (label: string, value: string, sub: string, cls = "") => `
    <div class="stat"><span class="stat-k">${label}</span><span class="stat-v ${cls}">${value}</span><span class="mini-hint">${sub}</span></div>`;

  $("test-meta").textContent =
    `${d.ticker} · ${d.name} · ${d.interval} · ${d.bars} бар · ` +
    `${d.from.slice(0, 10)} → ${d.to.slice(0, 10)} · лот ${d.lot} · слип ${d.params.slippage_tick}%`;

  $("test-stats").innerHTML = [
    row("Потолок (1 лот, каждый бар)", money(d.ceiling_1lot.pnl) + " ₽",
      `${d.ceiling_1lot.profitable_bars}/${d.ceiling_1lot.bars} баров прибыльных`),
    row("Идеальный свинг, внутридневной", money(d.perfect_intraday.pnl) + " ₽",
      `${pct(d.perfect_intraday.pnl / cap * 100)} · ${d.perfect_intraday.trades} сделок`,
      d.perfect_intraday.pnl >= 0 ? "pos" : "neg"),
    row("Идеальный свинг, мультиднев", money(d.perfect_multiday.pnl) + " ₽",
      `${pct(d.perfect_multiday.pnl / cap * 100)} · ${d.perfect_multiday.trades} сделок`,
      d.perfect_multiday.pnl >= 0 ? "pos" : "neg"),
    row("Buy & Hold", money(d.buy_hold.pnl) + " ₽",
      `${pct(d.buy_hold.pct)} · ${d.buy_hold.lots} лотов`,
      d.buy_hold.pnl >= 0 ? "pos" : "neg"),
  ].join("");

  const p = d.predictability;
  const rulesHtml = p.rules
    .map(
      (r) =>
        `<tr><td>${r.name}</td><td class="num">${r.signals}</td>` +
        `<td class="num">${r.precision.toFixed(1)}%</td>` +
        `<td class="num">${r.coverage.toFixed(1)}%</td><td class="num">${r.base.toFixed(1)}%</td></tr>`,
    )
    .join("");
  const wf = (x: { precision: number | null; coverage: number | null; base: number }, label: string) =>
    x.precision == null || x.coverage == null
      ? `<tr><td>${label}</td><td colspan="3" class="num">мало данных</td></tr>`
      : `<tr><td>${label}</td><td class="num">${x.precision.toFixed(1)}%</td>` +
        `<td class="num">${x.coverage.toFixed(1)}%</td><td class="num">${x.base.toFixed(1)}%</td></tr>`;

  $("test-predict").innerHTML = `
    <div class="mini-hint" style="margin:6px 0">
      ${p.swings} точек зигзага (${p.lows} лоу / ${p.highs} хай) ·
      подтверждение через ${p.confirm_lag_min_median} мин (ср. ${p.confirm_lag_min_mean}) ·
      к подтверждению уже прошло ${p.captured_median_pct}% движения
    </div>
    <table class="runs-table">
      <thead><tr><th>Правило</th><th>Сигналов</th><th>Точность</th><th>Покрытие</th><th>База</th></tr></thead>
      <tbody>${rulesHtml}
        ${wf(p.walkforward.entry, "Логрегрессия: вход")}
        ${wf(p.walkforward.exit, "Логрегрессия: выход")}
      </tbody>
    </table>`;
}

function renderTrades(d: TestMaxProfitResponse) {
  const tbody = $("test-trades-tbody");
  tbody.innerHTML = "";
  const fmt = (ts: string) =>
    new Date(ts).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  const hold = (a: string, b: string) => {
    const h = (new Date(b).getTime() - new Date(a).getTime()) / 3600e3;
    return h < 24 ? `${h.toFixed(1)} ч` : `${(h / 24).toFixed(1)} дн`;
  };
  for (const t of d.trades) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmt(t.entry_ts)}</td><td>${fmt(t.exit_ts)}</td>
      <td class="num">${t.entry_px.toFixed(3)}</td><td class="num">${t.exit_px.toFixed(3)}</td>
      <td class="num">${t.lots}</td>
      <td class="num ${t.pnl >= 0 ? "pos" : "neg"}">${money(t.pnl)} ₽</td>
      <td class="num">${hold(t.entry_ts, t.exit_ts)}</td>`;
    tbody.appendChild(tr);
  }
}

let chartHandle: TestHandle | null = null;
let equityChart: IChartApi | null = null;

async function loadTest() {
  const btn = $("btn-test-run");
  btn.classList.add("busy");
  try {
    const ticker = ($("test-ticker") as HTMLInputElement).value.trim().toUpperCase();
    const figi = await resolveFigi(ticker);
    if (!figi) {
      throw new Error(`Тикер ${ticker} не найден на бирже`);
    }
    const body = {
      figi,
      interval_name: ($("test-interval") as HTMLSelectElement).value,
      limit: Number(($("test-limit") as HTMLSelectElement).value),
      threshold_pct: Number(($("test-threshold") as HTMLInputElement).value),
      fee_rate_pct: Number(($("test-fee") as HTMLInputElement).value),
      capital: Number(($("test-capital") as HTMLInputElement).value),
    };
    const d = await fetchMaxProfit(body);

    const swings = [
      ...d.trades.map((t) => ({ kind: "low" as const, ts: t.entry_ts, price: t.entry_px })),
      ...d.trades.map((t) => ({ kind: "high" as const, ts: t.exit_ts, price: t.exit_px })),
    ];
    chartHandle?.destroy();
    chartHandle = renderTestChart($("test-chart"), d.candles, swings, d.trades);

    equityChart?.remove();
    renderEquityChart($("test-equity"), d.equity);

    renderStats(d);
    renderTrades(d);
  } catch (e) {
    const box = $("test-stats");
    box.innerHTML = `<span class="mini-hint">${e instanceof Error ? e.message : String(e)}</span>`;
  } finally {
    btn.classList.remove("busy");
  }
}

async function resolveFigi(ticker: string): Promise<string | null> {
  if (ticker.length > 12 && ticker.startsWith("BBG")) return ticker;
  const res = await fetch(`${API}/api/instruments?search=${encodeURIComponent(ticker)}&limit=10`);
  if (!res.ok) return null;
  const d = await res.json();
  const hit = d.items.find((i: { ticker: string }) => i.ticker.toUpperCase() === ticker);
  return hit?.figi ?? null;
}

export async function initTest() {
  $("btn-test-run").addEventListener("click", () => void loadTest());
  $("btn-ens-run").addEventListener("click", () => void loadEnsemble());
  $("btn-sweep-run").addEventListener("click", () => void loadSweep());
  $("btn-diag-run").addEventListener("click", () => void loadDiagnostics());
  const dl = $("test-tickers") as HTMLDataListElement;
  try {
    const res = await fetch(`${API}/api/instruments?limit=50`);
    const d = await res.json();
    for (const i of d.items as Array<{ ticker: string; name: string }>) {
      const opt = document.createElement("option");
      opt.value = i.ticker;
      opt.label = i.name;
      dl.appendChild(opt);
    }
  } catch {
    /* datalist необязателен */
  }
}

// ============ Ансамбль ============

const REGIME_COLORS: Record<string, string> = {
  RANGE: "rgba(74,144,226,0.18)",
  TREND_UP: "rgba(38,166,154,0.18)",
  TREND_DOWN: "rgba(239,83,80,0.18)",
  HIGH_VOLATILITY: "rgba(255,152,0,0.18)",
  NEUTRAL: "rgba(120,123,134,0.10)",
};

class RegimeStrip implements IPrimitivePaneView {
  private segments: Array<{ from: number; to: number; state: string }>;
  private chart: IChartApi;
  constructor(chart: IChartApi, segments: Array<{ from: number; to: number; state: string }>) {
    this.chart = chart;
    this.segments = segments;
  }
  renderer() {
    return {
      draw: (target: unknown) => {
        const scope = target as {
          useMediaCoordinateSpace: (cb: (s: { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } }) => void) => void;
        };
        scope.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
          const ts = this.chart.timeScale();
          const h = Math.min(26, mediaSize.height * 0.12);
          for (const s of this.segments) {
            const x1 = ts.timeToCoordinate(s.from as Time);
            const x2 = s.to != null ? ts.timeToCoordinate(s.to as Time) : mediaSize.width;
            if (x1 === null) continue;
            ctx.fillStyle = REGIME_COLORS[s.state] ?? "rgba(120,123,134,0.1)";
            ctx.fillRect(x1, 0, Math.max(2, (x2 ?? mediaSize.width) - x1), h);
          }
          ctx.fillStyle = "rgba(120,123,134,0.6)";
          ctx.font = "10px sans-serif";
          ctx.fillText("Режим:", 4, h - 4);
        });
      },
    };
  }
}

export class OracleZones implements IPrimitivePaneView {
  private zones: Array<{ from: number; to: number }>;
  private chart: IChartApi;
  constructor(chart: IChartApi, zones: Array<{ from: number; to: number }>) {
    this.chart = chart;
    this.zones = zones;
  }
  renderer() {
    return {
      draw: (target: unknown) => {
        const scope = target as {
          useMediaCoordinateSpace: (cb: (s: { context: CanvasRenderingContext2D; mediaSize: { width: number; height: number } }) => void) => void;
        };
        scope.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
          const ts = this.chart.timeScale();
          ctx.strokeStyle = "rgba(255,255,255,0.35)";
          ctx.setLineDash([3, 3]);
          ctx.lineWidth = 1;
          for (const z of this.zones) {
            const x1 = ts.timeToCoordinate(z.from as Time);
            const x2 = z.to != null ? ts.timeToCoordinate(z.to as Time) : mediaSize.width;
            if (x1 === null) continue;
            ctx.beginPath();
            ctx.moveTo(x1, 0);
            ctx.lineTo(x1, mediaSize.height);
            ctx.stroke();
            if (x2 != null) {
              ctx.beginPath();
              ctx.moveTo(x2, 0);
              ctx.lineTo(x2, mediaSize.height);
              ctx.stroke();
              ctx.fillStyle = "rgba(255,255,255,0.04)";
              ctx.fillRect(x1, 0, Math.max(1, x2 - x1), mediaSize.height);
            }
          }
          ctx.setLineDash([]);
        });
      },
    };
  }
}

function renderEnsembleChart(
  container: HTMLElement,
  candles: CandleDto[],
  d: EnsembleResponse,
) {
  container.innerHTML = "";
  const chart = createChart(container, {
    autoSize: true,
    layout: { background: { type: ColorType.Solid, color: "#131722" }, textColor: "#787b86" },
    grid: { vertLines: { color: "#1e2433" }, horzLines: { color: "#1e2433" } },
    crosshair: { mode: CrosshairMode.Normal },
    rightPriceScale: { borderColor: "#2a2e39" },
    timeScale: { borderColor: "#2a2e39", timeVisible: true, secondsVisible: false },
  });
  const candleSeries = chart.addSeries(CandlestickSeries, {
    upColor: "#26a69a", downColor: "#ef5350", wickUpColor: "#26a69a", wickDownColor: "#ef5350", borderVisible: false,
  });
  candleSeries.setData(
    candles.slice().sort((a, b) => toUnix(a.ts) - toUnix(b.ts)).map((c) => ({
      time: toUnix(c.ts), open: c.open, high: c.high, low: c.low, close: c.close,
    })),
  );

  const markers: SeriesMarker<Time>[] = [];
  const zones: Array<{ timeFrom: UTCTimestamp; timeTo: UTCTimestamp; win: boolean }> = [];
  const active = d.adaptive ?? d.static;

  for (const e of active.entries) {
    markers.push({
      time: toUnix(e.ts), position: e.side === "BUY" ? "belowBar" : "aboveBar",
      shape: e.side === "BUY" ? "arrowUp" : "arrowDown",
      color: e.side === "BUY" ? "#26a69a" : "#ef5350", text: "A",
    });
  }
  for (const r of active.rejected.slice(0, 400)) {
    markers.push({
      time: toUnix(r.ts), position: r.side === "BUY" ? "belowBar" : "aboveBar",
      shape: "square", color: "rgba(120,123,134,0.5)",
      text: r.reason === "QUORUM_REJECT" ? "Q" : r.reason.startsWith("REGIME") ? "R" : "B",
    });
  }
  for (const t of active.trades) {
    zones.push({ timeFrom: toUnix(t.entry_ts), timeTo: toUnix(t.exit_ts), win: t.net >= 0 });
    markers.push({
      time: toUnix(t.entry_ts), position: "belowBar", shape: "arrowUp",
      color: "#b388ff", text: `▶${t.regime.slice(0, 2)}`,
    });
    markers.push({
      time: toUnix(t.exit_ts), position: "aboveBar", shape: "circle",
      color: t.net >= 0 ? "#26a69a" : "#ef5350", text: String(Math.round(t.net)),
    });
  }
  markers.sort((a, b) => Number(a.time) - Number(b.time));
  const markersApi = createSeriesMarkers(candleSeries, []);
  markersApi.setMarkers(markers);

  if (zones.length > 0) candleSeries.attachPrimitive(new ZonesPrimitive(chart, zones));

  const segments = d.regime.timeline
    .filter((t) => t.from)
    .map((t) => ({ from: toUnix(t.from), to: t.to ? toUnix(t.to) : null, state: t.state }))
    .filter((s) => s.to !== null) as Array<{ from: number; to: number; state: string }>;
  if (segments.length > 0) {
    candleSeries.attachPrimitive({ paneViews: () => [new RegimeStrip(chart, segments)], updateAllViews: () => {} });
  }
  const oracleZones = d.oracle.zones
    .map((z) => ({ from: toUnix(z.from), to: toUnix(z.to) }))
    .filter((z) => z.to != null) as Array<{ from: number; to: number }>;
  if (oracleZones.length > 0) {
    candleSeries.attachPrimitive({ paneViews: () => [new OracleZones(chart, oracleZones)], updateAllViews: () => {} });
  }

  // легенда: оракул vs реальные сделки
  const legend = document.createElement("div");
  legend.className = "ens-legend";
  legend.innerHTML = `<span class="lg-oracle">┅ ┅ оракул (идеал)</span> <span class="lg-real">■ сделки ансамбля (зел=плюс, красн=минус)</span>`;
  container.appendChild(legend);

  chart.timeScale().fitContent();
}

function fmtDT(ts: string): string {
  return new Date(ts).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function renderEnsembleTables(d: EnsembleResponse) {
  const st = d.static;
  const active = d.adaptive ?? d.static;
  $("ens-meta").textContent =
    `${d.meta.figi} · ${d.meta.bars} бар · лот ${d.meta.lot} · ${d.meta.qty_shares} акций · hash ${d.meta.request_hash}`;

  // === Потолок vs Реальность ===
  {
    const o = d.oracle;
    const trs = active.trades;
    const w = trs.filter((t) => t.net > 0);
    const l = trs.filter((t) => t.net < 0);
    const cap = active.capture_ratio;
    $("ens-ideal-real").textContent =
      `период ${d.meta.from.slice(0, 10)} → ${d.meta.to.slice(0, 10)} · ` +
      `лотируется по капиталу ${money(d.meta.capital)} ₽ · оракул = идеальный свинг-трейдер с тем же размером позиции`;
    $("ens-ideal-real-body").innerHTML = `
      <tr><td>Сделок</td><td class="num">${o.trades}</td><td class="num">${trs.length}</td></tr>
      <tr><td>Gross</td><td class="num">${money(o.gross)} ₽</td><td class="num">${money(active.economic.gross)} ₽</td></tr>
      <tr><td>Costs</td><td class="num">${money(o.gross - o.net)} ₽</td><td class="num">${money(active.economic.costs)} ₽</td></tr>
      <tr><td>NET</td><td class="num ${o.net >= 0 ? "pos" : "neg"}">${money(o.net)} ₽</td>
        <td class="num ${active.economic.net >= 0 ? "pos" : "neg"}">${money(active.economic.net)} ₽</td></tr>
      <tr><td>Плюсовых сделок</td><td class="num">—</td><td class="num">${w.length} (сумма +${money(w.reduce((s, t) => s + t.net, 0))} ₽)</td></tr>
      <tr><td>Минусовых сделок</td><td class="num">—</td><td class="num">${l.length} (сумма ${money(l.reduce((s, t) => s + t.net, 0))} ₽)</td></tr>
      <tr><td>Capture (net vs оракул)</td><td colspan="2" class="num">${cap.net_capture_pct}% (gross ${cap.gross_capture_pct}%)</td></tr>
    `;
  }

  $("ens-funnel").innerHTML = [
    ["raw_signals", "Raw сигналы функций"],
    ["unique_raw_ts", "Уникальные ts сигналов"],
    ["quorum_unique", "Точки кворума (same-bar)"],
    ["accepted_decisions", "Принятые решения о входе"],
    ["unique_entry_episodes", "Уникальные эпизоды входа"],
    ["entries_rejected", "Отклонено (bias/quorum/режим)"],
    ["reentry_rejected", "Повторный вход той же стороны (cooldown)"],
    ["preview_trades", "Preview-сделки"],
  ].map(([k, label]) => `<tr><td>${label}</td><td class="num">${st.funnel[k as keyof typeof st.funnel]}</td></tr>`).join("")
    + `<tr><td class="dim">кворум BUY / SELL</td><td class="num">${st.funnel.quorum_BUY} / ${st.funnel.quorum_SELL}</td></tr>`;

  const useless = new Set(st.useless_strategies);
  $("ens-quality").innerHTML = st.quality
    .map((q) => `<tr>
      <td>${q.strategy_id}${q.useless ? " <b class=\"neg\">⚠</b>" : ""}</td>
      <td>${q.side}</td><td class="num">${q.signals}</td>
      <td class="num">${q.coverage_pct.toFixed(1)}</td><td class="num">${q.precision_pct.toFixed(1)}</td>
      <td class="num">${q.lead_min_median ?? "—"}</td><td class="num">${q.false_positives}</td>
      <td class="num ${q.useless ? "neg" : "pos"}">${q.useless ? "drop" : "ok"}</td>
    </tr>`).join("");
  if (useless.size > 0) {
    $("ens-quality").innerHTML += `<tr><td colspan="8" class="num">Бесполезные (coverage=0): ${[...useless].join(", ")}</td></tr>`;
  }

  const run = (r: EnsembleResponse["static"] | null) =>
    r ? { trades: r.economic.trades, gross: r.economic.gross, net: r.economic.net,
          pf: r.economic.profit_factor,
          cap: r.capture_ratio.net_capture_pct,
          costs: r.economic.costs,
          break_even: r.economic.break_even_move_pct }
      : null;
  const s = run(d.static);
  const a = d.adaptive ? run(d.adaptive) : null;
  $("ens-compare").innerHTML = `
    <thead><tr><th></th><th>Static</th><th>Adaptive</th></tr></thead>
    <tbody>
      <tr><td>Сделок</td><td class="num">${s?.trades ?? "—"}</td><td class="num">${a?.trades ?? "—"}</td></tr>
      <tr><td>Gross</td><td class="num">${money(s?.gross)}</td><td class="num">${money(a?.gross)}</td></tr>
      <tr><td>Costs (комиссия+слип)</td><td class="num">${money(s?.costs)}</td><td class="num">${money(a?.costs)}</td></tr>
      <tr><td>Net</td><td class="num ${(s?.net ?? 0) >= 0 ? "pos" : "neg"}">${money(s?.net)} ₽</td>
        <td class="num ${(a?.net ?? 0) >= 0 ? "pos" : "neg"}">${money(a?.net)} ₽</td></tr>
      <tr><td>PF</td><td class="num">${s?.pf ?? "—"}</td><td class="num">${a?.pf ?? "—"}</td></tr>
      <tr><td>Net capture (vs оракул), %</td><td class="num">${s?.cap ?? "—"}</td><td class="num">${a?.cap ?? "—"}</td></tr>
      <tr><td>Break-even ход, %</td><td class="num">${s?.break_even ?? "—"}</td><td class="num">${a?.break_even ?? "—"}</td></tr>
    </tbody>`;

  const cap = d.static.capture_ratio;
  $("ens-compare").innerHTML += `
    <div class="mini-hint" style="margin-top:6px">Оракул (потенциал): ${money(cap.oracle_gross_potential)} ₽ · каузальный gross ${money(cap.causal_gross)} ₽ · costs ${money(cap.causal_costs)} ₽ · net ${money(cap.causal_net)} ₽ · gross capture ${cap.gross_capture_pct}%</div>`;

  const ep = st.episodes;
  $("ens-episodes").innerHTML = `
    <div class="mini-hint" style="margin:6px 0">Точки кворума ${ep.quorum_points} · решений ${ep.entry_decisions} · эпизодов ${ep.unique_episodes} · завершено ${ep.completed} · нерешённых ${ep.unresolved}</div>
    <table class="runs-table">
      <thead><tr><th>Эпизод</th><th>Side</th><th>Кворум</th><th>Первый→последний сигнал</th><th>Вход→Выход</th><th>Net</th><th>Статус</th></tr></thead>
      <tbody>${ep.list.map((e) => `<tr>
        <td>${e.episode_id}</td><td>${e.side}</td><td>${e.quorum_event_id}</td>
        <td>${fmtDT(e.first_ts)} → ${fmtDT(e.last_ts)}</td>
        <td class="num">${e.entry_px != null ? `${e.entry_px.toFixed(2)} → ${e.exit_px?.toFixed(2)}` : "—"}</td>
        <td class="num ${(e.net ?? 0) >= 0 ? "pos" : "neg"}">${e.net != null ? `${money(e.net)} ₽` : "—"}</td>
        <td class="${e.status === "TRADED" ? "pos" : ""}">${e.status}</td>
      </tr>`).join("")}</tbody>
    </table>`;

  const ec = st.economic;
  const be = ec.break_even_by_trade ?? null;
  $("ens-costs").innerHTML = `
    <table class="runs-table">
      <thead><tr><th>Показатель</th><th>Значение</th></tr></thead>
      <tbody>
        <tr><td>Ср. gross ход за сделку</td><td class="num">${ec.avg_gross_move_pct.toFixed(3)}%</td></tr>
        <tr><td>Медианный gross ход</td><td class="num">${ec.median_gross_move_pct.toFixed(3)}%</td></tr>
        <tr><td>Ср. costs за сделку</td><td class="num">${money(ec.cost_per_trade)} ₽</td></tr>
        <tr><td>Break-even: ср./медиана</td><td class="num">${ec.break_even_move_pct.toFixed(3)}% / ${be?.median?.toFixed(3) ?? "—"}%</td></tr>
        <tr><td>Break-even P25 / P75</td><td class="num">${be?.p25?.toFixed(3) ?? "—"}% / ${be?.p75?.toFixed(3) ?? "—"}%</td></tr>
        <tr><td>Сделок выше break-even</td><td class="num">${ec.trades_above_break_even} / ${ec.trades}</td></tr>
        <tr><td>Cost/gross ratio</td><td class="num">${ec.cost_gross_ratio}</td></tr>
      </tbody>
    </table>`;

  $("ens-per-regime").innerHTML = Object.entries(active.per_regime)
    .map(([state, r]) => `<tr><td>${state}</td><td class="num">${r.trades}</td>
      <td class="num ${r.net >= 0 ? "pos" : "neg"}">${money(r.net)} ₽</td>
      <td class="num">${r.win_rate_pct}%</td></tr>`)
    .join("") || `<tr><td colspan="4" class="num">нет сделок по режимам</td></tr>`;

  const tbody = $("ens-trades-tbody");
  tbody.innerHTML = "";
  const hold = (a2: string, b2: string) => {
    const h = (new Date(b2).getTime() - new Date(a2).getTime()) / 36e5;
    return h < 24 ? `${h.toFixed(1)} ч` : `${(h / 24).toFixed(1)} дн`;
  };
  for (const t of active.trades) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${t.regime}</td><td>${t.side}</td>
      <td>${fmtDT(t.entry_ts)} → ${fmtDT(t.exit_ts)}</td>
      <td class="num">${t.entry_px.toFixed(2)} → ${t.exit_px.toFixed(2)}</td>
      <td class="num">${t.stop?.toFixed(2) ?? "—"}/${t.target?.toFixed(2) ?? "—"}</td>
      <td class="num ${t.net >= 0 ? "pos" : "neg"}">${money(t.net)} ₽</td>
      <td class="num">${t.mfe_r}R/${t.mae_r}R · ход ${t.gross_move_pct.toFixed(2)}%</td>
      <td class="num">${money(t.costs)} ₽ (${t.costs_pct_of_notional.toFixed(2)}%)</td>
      <td>${t.exit_reason} · ${hold(t.entry_ts, t.exit_ts)}</td>`;
    tbody.appendChild(tr);
  }
}

function buildAdaptiveConfig() {
  const mode = (id: string) => ($(id) as HTMLSelectElement).value;
  const ranges = [
    { name: "RANGE", mode: mode("ens-mode-range") },
    { name: "TREND_UP", mode: mode("ens-mode-trend-up") },
    { name: "TREND_DOWN", mode: mode("ens-mode-trend-down") },
    { name: "HIGH_VOLATILITY", mode: mode("ens-mode-hv") },
  ];
  return ranges
    .filter((r) => r.mode !== "both")
    .map((r) => (r.mode === "no_trade" ? { name: r.name, no_trade: true } : { name: r.name, config: { mode: r.mode } }));
}

const EXIT_DEFAULTS: Record<string, Record<string, number>> = {
  atr_stop: { period: 14, multiplier: 2, risk_reward: 2 },
  fixed_sl_tp: { stop_pct: 1.0, target_pct: 2.0 },
  atr_trailing: { period: 14, initial_stop_atr: 2, activation_atr: 1, trail_distance_atr: 2 },
};

async function loadEnsemble() {
  const btn = $("btn-ens-run");
  btn.classList.add("busy");
  try {
    const ticker = ($("test-ticker") as HTMLInputElement).value.trim().toUpperCase();
    const figi = await resolveFigi(ticker);
    if (!figi) throw new Error(`Тикер ${ticker} не найден`);
    const capital = Number(($("test-capital") as HTMLInputElement).value);
    const body: Record<string, unknown> = {
      figi,
      days: Number(($("ens-days") as HTMLInputElement).value) || 7,
      capital,
      lot: 10,
      use_all_setups: ($("ens-all") as HTMLInputElement).checked,
      drop_useless: ($("ens-drop") as HTMLInputElement).checked,
      quorum: Number(($("ens-quorum") as HTMLInputElement).value),
      same_side_reentry_cooldown_bars: Number(($("ens-cooldown") as HTMLInputElement).value),
      exit_confirm_window_bars: Number(($("ens-exit-confirm") as HTMLInputElement).value),
      exit_policy: { id: ($("ens-exit") as HTMLSelectElement).value, params: EXIT_DEFAULTS[($("ens-exit") as HTMLSelectElement).value] },
      oracle: { threshold_pct: Number(($("ens-oracle") as HTMLInputElement).value), fee_rate_pct: 0.05 },
      adaptive: ($("ens-adaptive") as HTMLInputElement).checked ? buildAdaptiveConfig() : [],
    };
    const d = await fetchEnsemble(body);
    const mp = await fetchMaxProfit({ figi, interval_name: "1min", limit: 5000, threshold_pct: 0.5, fee_rate_pct: 0.05, capital });
    renderEnsembleChart($("ens-chart"), mp.candles, d);
    renderEnsembleTables(d);
  } catch (e) {
    $("ens-funnel").innerHTML = `<tr><td>${e instanceof Error ? e.message : String(e)}</td></tr>`;
  } finally {
    btn.classList.remove("busy");
  }
}

// ============ Матрица ============

function renderSweep(d: import("./api").SweepResponse) {
  const note = $("sweep-note");
  note.textContent = d.note + (d.filtered_out.length
    ? ` · отброшено по цене: ${d.filtered_out.map((f) => `${f.ticker}(${f.price ?? "?"}₽)`).join(", ")}`
    : "");
  const cds = [...new Set(d.cells.map((c) => c.cooldown))].sort((a, b) => a - b);
  const cfs = [...new Set(d.cells.map((c) => c.exit_confirm))].sort((a, b) => a - b);
  const tickers = [...new Set(d.cells.map((c) => c.ticker))];
  const th = `<th>Акция</th><th>Цена</th>${cfs.map((cf) => cds.map((cd) => `<th>cd${cd}·cf${cf}</th>`).join("")).join("")}`;
  const rows = tickers.map((tk) => {
    const cellsOf = d.cells.filter((c) => c.ticker === tk);
    const first = cellsOf[0];
    const td = cfs.map((cf) => cds.map((cd) => {
      const c = cellsOf.find((x) => x.cooldown === cd && x.exit_confirm === cf);
      if (!c) return `<td class="num">—</td>`;
      const cfTxt = c.counterfactual?.mean_net != null
        ? `<span class="mini-hint">rej=${c.reentry_rejected} CF=${c.counterfactual.mean_net.toFixed(0)}</span>` : "";
      return `<td class="num ${c.net >= 0 ? "pos" : "neg"}">${c.net.toFixed(0)}<br/>${cfTxt}</td>`;
    }).join("")).join("");
    return `<tr><td>${tk}</td><td class="num">${first.price}</td>${td}</tr>`;
  }).join("");
  $("sweep-table").innerHTML = `<thead><tr>${th}</tr></thead><tbody>${rows}</tbody>`;
}

async function loadSweep() {
  const btn = $("btn-sweep-run");
  btn.classList.add("busy");
  try {
    const body: Record<string, unknown> = {
      figis: ($("sweep-figis") as HTMLInputElement).value.split(",").map((s) => s.trim()).filter(Boolean),
      days: Number(($("sweep-days") as HTMLInputElement).value),
      capital: Number(($("test-capital") as HTMLInputElement).value),
      lot: 10,
      price_min: Number(($("sweep-pmin") as HTMLInputElement).value),
      price_max: Number(($("sweep-pmax") as HTMLInputElement).value),
      cooldowns: ($("sweep-cds") as HTMLInputElement).value.split(",").map((s) => Number(s.trim())),
      exit_confirms: ($("sweep-cfs") as HTMLInputElement).value.split(",").map((s) => Number(s.trim())),
    };
    const d = await fetchEnsembleSweep(body);
    renderSweep(d);
  } catch (e) {
    $("sweep-note").textContent = e instanceof Error ? e.message : String(e);
  } finally {
    btn.classList.remove("busy");
  }
}

// ============ Диагностика (таб на Складе) ============

const REASON_RU: Record<string, string> = {
  AGAINST_BIAS: "против 1h bias",
  SETUP_MISSING: "нет кворума в окне",
  REGIME_BLOCKED: "режим запретил",
  REGIME_MODE: "режим-направление",
  COOLDOWN: "повторный вход той же стороны",
  SAME_SIDE_REENTRY_COOLDOWN: "повторный вход той же стороны",
  BIAS_NEUTRAL: "bias нейтрален",
  BIAS_CONFLICT: "bias конфликтует",
  FILTER_REJECTED: "фильтр отклонил",
};

async function loadDiagnostics() {
  const btn = $("btn-diag-run");
  btn.classList.add("busy");
  try {
    const ticker = ($("symbol-ticker")?.textContent || "RUAL").trim();
    const figi = await resolveFigi(ticker);
    if (!figi) throw new Error(`Тикер ${ticker} не найден`);
    const body: Record<string, unknown> = {
      figi,
      days: 7,
      capital: 100000,
      lot: 10,
      use_all_setups: true,
      drop_useless: true,
      quorum: 2,
      same_side_reentry_cooldown_bars: 15,
    };
    const d = await fetchEnsemble(body);
    const st = d.static;
    $("diag-meta").textContent =
      `${ticker} · ${d.meta.bars} бар · ${d.meta.from.slice(0, 10)} → ${d.meta.to.slice(0, 10)} · hash ${d.meta.request_hash}`;

    const ft = st.funnel_tf;
    $("diag-funnel").innerHTML = [
      ["Bias LONG_ALLOWED", ft.bias_states.LONG_ALLOWED],
      ["Bias SHORT_ALLOWED", ft.bias_states.SHORT_ALLOWED],
      ["Bias NEUTRAL", ft.bias_states.NEUTRAL],
      ["Setup кандидаты", ft.setups.candidates],
      ["Setup прошли кворум", ft.setups.quorum_passed],
      ["Entry кандидаты (1m)", ft.entries.candidates],
      ["Entry принято", ft.entries.accepted],
      ["Entry отсеяно", ft.entries.rejected],
      ["Сделок preview", st.funnel.preview_trades],
    ].map(([k, v]) => `<tr><td>${k}</td><td class="num">${v}</td></tr>`).join("");

    $("diag-reasons").innerHTML = Object.entries(ft.rejected_by_reason)
      .map(([code, n]) => `<tr><td>${code}</td><td>${REASON_RU[code] ?? ""}</td><td class="num">${n}</td></tr>`)
      .join("") || `<tr><td colspan="3" class="num">нет отсевов</td></tr>`;

    const oc = st.oracle_coverage;
    if (oc) {
      $("diag-oracle").innerHTML = [
        ["Точек оракула", oc.point_total],
        ["Raw видно (до подтверждения)", oc.causal.raw_seen_before_confirmation],
        ["Принято (каузально)", oc.causal.accepted],
        ["Исполнено", oc.causal.executed],
        ["Покрытие принятых, %", oc.causal.coverage_accepted_pct],
      ].map(([k, v]) => `<tr><td>${k}</td><td class="num">${v}</td></tr>`).join("")
        + `<tr><td colspan="2" class="dim">Причины: ${JSON.stringify(oc.rejected_by_gate)}</td></tr>`;
    } else {
      $("diag-oracle").innerHTML = `<tr><td class="num">нет данных oracle_coverage</td></tr>`;
    }

    const why = $("diag-why");
    why.innerHTML = st.why_no_entry.slice(-100).map((r) => `
      <tr><td>${fmtDT(r.ts)}</td><td>${r.side}</td><td>${r.code}</td><td>${REASON_RU[r.code] ?? r.detail}</td></tr>`).join("");
  } catch (e) {
    $("diag-meta").textContent = e instanceof Error ? e.message : String(e);
  } finally {
    btn.classList.remove("busy");
  }
}
