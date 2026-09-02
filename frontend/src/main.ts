import {
  ColorType,
  CandlestickSeries,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import "./style.css";
import {
  computeQuorum,
  computeSignals,
  fetchAnalysis,
  fetchCatalog,
  fetchMaxProfit,
  fetchRuns,
  syncCandles,
  type AnalysisDto,
  type StrategyCardDto,
  previewConfiguration,
} from "./api";
import { initBot, pollOnce } from "./bot";
import { initLab, refreshConfigs } from "./lab";
import { initTest } from "./test";
import { initEnsLab } from "./enslab";
import { OracleZones } from "./test";

declare global {
  interface Window {
    FIGI: string;
  }
}
window.FIGI = "BBG004730N88";

let FIGI = "BBG004730N88";
const $ = (id: string) => document.getElementById(id) as HTMLElement;
const money = (n: number | null | undefined): string =>
  n == null ? "—" : new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(n);

const ENSURE_DAYS: Record<string, number> = {
  "1min": 14,
  "5min": 60,
  "10min": 90,
  "15min": 90,
  hour: 120,
  "2h": 180,
  "4h": 180,
  day: 120,
  week: 260,
  month: 730,
};

const TIMEFRAMES: { label: string; interval: string; days: number }[] = [
  { label: "1М", interval: "1min", days: ENSURE_DAYS["1min"] },
  { label: "5М", interval: "5min", days: ENSURE_DAYS["5min"] },
  { label: "10М", interval: "10min", days: ENSURE_DAYS["10min"] },
  { label: "15М", interval: "15min", days: ENSURE_DAYS["15min"] },
  { label: "1Ч", interval: "hour", days: ENSURE_DAYS.hour },
  { label: "2Ч", interval: "2h", days: ENSURE_DAYS["2h"] },
  { label: "4Ч", interval: "4h", days: ENSURE_DAYS["4h"] },
  { label: "Д", interval: "day", days: ENSURE_DAYS.day },
  { label: "Н", interval: "week", days: ENSURE_DAYS.week },
  { label: "Мес", interval: "month", days: ENSURE_DAYS.month },
];

const COLORS = {
  bg: "#131722",
  panel: "#1b2030",
  grid: "#1e2433",
  text: "#787b86",
  up: "#26a69a",
  down: "#ef5350",
  sma: "#4a90e2",
  macdLine: "#2962ff",
  signalLine: "#ff9800",
};



const priceFmt = new Intl.NumberFormat("ru-RU", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const volFmt = new Intl.NumberFormat("ru-RU", { notation: "compact", maximumFractionDigits: 1 });

const PAGE_TITLES: Record<string, string> = {
  chart: "Склад — свечи, стратегии, конфигурации",
  test: "Тест — потолок торговли",
  lab: "Lab — тесты стратегий",
  bot: "Paper-бот — дашборд",
};

const IS_EMBEDDED = new URLSearchParams(window.location.search).get("embedded") === "1";
if (IS_EMBEDDED) document.body.classList.add("embedded");
const lk = (k: string) => (IS_EMBEDDED ? "bote_" : "") + k;

function initNav() {
  const $ = (id: string) => document.getElementById(id) as HTMLElement;
  const btns = document.querySelectorAll<HTMLButtonElement>(".nav-btn");
  btns.forEach((b) =>
    b.addEventListener("click", () => {
      btns.forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      const page = b.dataset.page!;
      document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
      document.getElementById(`page-${page}`)!.classList.add("active");
      $("page-title").textContent = PAGE_TITLES[page] ?? page;
      if (page === "lab") void refreshConfigs();
      if (page === "bot") void pollOnce();
    }),
  );
}

function toUnix(ts: string): UTCTimestamp {
  return Math.floor(new Date(ts).getTime() / 1000) as UTCTimestamp;
}

interface ChartRefs {
  chart: IChartApi;
  candles: ISeriesApi<"Candlestick">;
  volume: ISeriesApi<"Histogram">;
  sma: ISeriesApi<"Line">;
  emaLine: ISeriesApi<"Line">;
  bbUpper: ISeriesApi<"Line">;
  bbLower: ISeriesApi<"Line">;
  macdHist: ISeriesApi<"Histogram"> | null;
  macdLine: ISeriesApi<"Line"> | null;
  signalLine: ISeriesApi<"Line"> | null;
  rsiLine: ISeriesApi<"Line"> | null;
  markers: ISeriesMarkersPluginApi<Time>;
}

let refs: ChartRefs | null = null;
let oraclePrim: ReturnType<typeof makeOraclePrim> | null = null;
let _overlayLines: Array<{ remove: () => void }> = [];
let oracleBusy = false;
let analysis: AnalysisDto | null = null;
const savedTf = localStorage.getItem(lk("sklad_tf"));
const defTf = IS_EMBEDDED ? "1min" : "day";
let currentTf = TIMEFRAMES.find((t) => t.interval === savedTf) ?? TIMEFRAMES.find((t) => t.interval === defTf)!;
window.LAB_INTERVAL = currentTf.interval;
let ensuredFrom: UTCTimestamp | null = null;
let ensuredTo: UTCTimestamp | null = null;
let panTimer: ReturnType<typeof setTimeout> | null = null;
let panBusy = false;
let catalogCards: StrategyCardDto[] = [];
const activeRuns = new Map<string, { runId: string; signals: { ts: string; side: string }[]; params: Record<string, number> }>();
let quorumState: { runId: string; k: number; m: number; signals: { ts: string; side: string; features: Record<string, unknown> }[] } | null = null;

const TAG_BY_STRATEGY: Record<string, string> = {
  rsi_reversal: "RSI",
  bollinger_reclaim: "BB",
  pullback_ema: "PB",
  vwap_reclaim: "VWAP",
  range_compression_breakout: "SQZ",
  macd_cross: "MACD",
  donchian_breakout: "DON",
};

const STEP_SEC: Record<string, number> = {
  "1min": 60,
  "5min": 300,
  "10min": 600,
  "15min": 900,
  hour: 3600,
  "2h": 7200,
  "4h": 14400,
  day: 86400,
  week: 604800,
  month: 2592000,
};

interface IndicatorState {
  sma: boolean;
  ema: boolean;
  bb: boolean;
  rsi: boolean;
  macd: boolean;
}

function loadIndicatorState(): IndicatorState {
  const fallback: IndicatorState = { sma: true, ema: false, bb: false, rsi: false, macd: true };
  try {
    const raw = localStorage.getItem(lk("indicators"));
    return raw ? { ...fallback, ...JSON.parse(raw) } : fallback;
  } catch {
    return fallback;
  }
}

let indicators = loadIndicatorState();
let chartFlags = { oscPane: indicators.macd || indicators.rsi };

function buildChart(): ChartRefs {
  const container = document.getElementById("chart")!;
  const chart = createChart(container, {
    autoSize: true,
    layout: {
      background: { type: ColorType.Solid, color: COLORS.bg },
      textColor: COLORS.text,
      panes: { separatorColor: COLORS.grid, separatorHoverColor: "rgba(41,98,255,0.2)" },
    },
    grid: {
      vertLines: { color: COLORS.grid },
      horzLines: { color: COLORS.grid },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { labelBackgroundColor: "#363a45" },
      horzLine: { labelBackgroundColor: "#363a45" },
    },
    rightPriceScale: { borderColor: COLORS.grid },
    timeScale: { borderColor: COLORS.grid, timeVisible: true, secondsVisible: false },
    localization: {
      locale: "ru-RU",
      priceFormatter: (p: number) => priceFmt.format(p),
    },
  });

  const candleSeries = chart.addSeries(CandlestickSeries, {
    upColor: COLORS.up,
    downColor: COLORS.down,
    wickUpColor: COLORS.up,
    wickDownColor: COLORS.down,
    borderVisible: false,
  });

  const volumeSeries = chart.addSeries(
    HistogramSeries,
    {
      priceScaleId: "",
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat: { type: "volume" },
    },
    0,
  );
  volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

  const smaSeries = chart.addSeries(
    LineSeries,
    {
      color: COLORS.sma,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    },
    0,
  );

  const emaLine = chart.addSeries(
    LineSeries,
    {
      color: "#e6a23c",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      visible: indicators.ema,
    },
    0,
  );
  const bbUpper = chart.addSeries(
    LineSeries,
    {
      color: "rgba(155,89,182,0.8)",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      visible: indicators.bb,
    },
    0,
  );
  const bbLower = chart.addSeries(
    LineSeries,
    {
      color: "rgba(155,89,182,0.8)",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      visible: indicators.bb,
    },
    0,
  );

  const oscPane = indicators.macd || indicators.rsi;
  let macdHist: ISeriesApi<"Histogram"> | null = null;
  let macdLine: ISeriesApi<"Line"> | null = null;
  let signalLine: ISeriesApi<"Line"> | null = null;
  let rsiLine: ISeriesApi<"Line"> | null = null;

  if (indicators.macd) {
    macdHist = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, 1);
    macdLine = chart.addSeries(
      LineSeries,
      { color: COLORS.macdLine, lineWidth: 2, priceLineVisible: false, lastValueVisible: false },
      1,
    );
    signalLine = chart.addSeries(
      LineSeries,
      { color: COLORS.signalLine, lineWidth: 2, priceLineVisible: false, lastValueVisible: false },
      1,
    );
  }
  if (indicators.rsi) {
    rsiLine = chart.addSeries(
      LineSeries,
      { color: "#c39bd3", lineWidth: 2, priceLineVisible: false, lastValueVisible: false },
      1,
    );
  }

  try {
    const panes = chart.panes();
    if (panes[1] && oscPane) {
      panes[0].setStretchFactor(3);
      panes[1].setStretchFactor(2);
    }
  } catch {
    // panes API может отсутствовать в старых версиях
  }

  return {
    chart,
    candles: candleSeries,
    volume: volumeSeries,
    sma: smaSeries,
    emaLine,
    bbUpper,
    bbLower,
    macdHist,
    macdLine,
    signalLine,
    rsiLine,
    markers: createSeriesMarkers(candleSeries, []),
  };
}

function renderData(data: AnalysisDto, keepView = false) {
  if (!refs) refs = buildChart();
  const r = refs;
  const n = data.candles.length;

  const candlePoints: CandlestickData[] = [];
  const volumePoints: HistogramData[] = [];
  for (let i = 0; i < n; i++) {
    const c = data.candles[i];
    const t = toUnix(c.ts);
    candlePoints.push({ time: t, open: c.open, high: c.high, low: c.low, close: c.close });
    volumePoints.push({
      time: t,
      value: c.volume,
      color: c.close >= c.open ? "rgba(38,166,154,0.45)" : "rgba(239,83,80,0.45)",
    });
  }
  r.candles.setData(candlePoints);
  r.volume.setData(volumePoints);

  const smaPoints: LineData[] = [];
  const emaPoints: LineData[] = [];
  const bbUpperPoints: LineData[] = [];
  const bbLowerPoints: LineData[] = [];
  const rsiPoints: LineData[] = [];
  const macdPoints: LineData[] = [];
  const signalPoints: LineData[] = [];
  const histPoints: HistogramData[] = [];
  for (let i = 0; i < n; i++) {
    const t = toUnix(data.candles[i].ts);
    const s = data.sma20[i];
    if (s != null) smaPoints.push({ time: t, value: s });
    const e = data.ema50?.[i];
    if (e != null) emaPoints.push({ time: t, value: e });
    const bu = data.bb_upper?.[i];
    if (bu != null) bbUpperPoints.push({ time: t, value: bu });
    const bl = data.bb_lower?.[i];
    if (bl != null) bbLowerPoints.push({ time: t, value: bl });
    const rv = data.rsi?.[i];
    if (rv != null) rsiPoints.push({ time: t, value: rv });
    const m = data.macd.macd[i];
    if (m != null) macdPoints.push({ time: t, value: m });
    const sg = data.macd.signal[i];
    if (sg != null) signalPoints.push({ time: t, value: sg });
    const h = data.macd.hist[i];
    if (h != null) {
      histPoints.push({
        time: t,
        value: h,
        color: h >= 0 ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)",
      });
    }
  }
  r.sma.setData(smaPoints);
  r.emaLine.setData(emaPoints);
  r.bbUpper.setData(bbUpperPoints);
  r.bbLower.setData(bbLowerPoints);
  if (r.rsiLine) r.rsiLine.setData(rsiPoints);
  if (r.macdLine) r.macdLine.setData(macdPoints);
  if (r.signalLine) r.signalLine.setData(signalPoints);
  if (r.macdHist) r.macdHist.setData(histPoints);

  if (data.candles.length > 0) {
    ensuredFrom = toUnix(data.candles[0].ts);
    ensuredTo = toUnix(data.candles[n - 1].ts);
  }

  if (!keepView) {
    r.chart.timeScale().fitContent();
  }

  document.getElementById("symbol-ticker")!.textContent = data.ticker || FIGI;
  document.getElementById("symbol-name")!.textContent = data.name || "";
}

function fmtDateTime(ts: string): string {
  const d = new Date(ts);
  return d.toLocaleString("ru-RU", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function updateLegend(idx: number | null) {
  if (!analysis || analysis.candles.length === 0) return;
  const i = idx == null ? analysis.candles.length - 1 : Math.min(idx, analysis.candles.length - 1);
  const c = analysis.candles[i];
  const prevClose = i > 0 ? analysis.candles[i - 1].close : c.open;
  const changePct = ((c.close - prevClose) / prevClose) * 100;
  const dirClass = changePct >= 0 ? "up" : "down";
  const sign = changePct >= 0 ? "+" : "";

  document.getElementById("ohlc-legend")!.innerHTML = [
    `<span class="lbl">O</span> <span class="val">${priceFmt.format(c.open)}</span>`,
    `<span class="lbl">H</span> <span class="val">${priceFmt.format(c.high)}</span>`,
    `<span class="lbl">L</span> <span class="val">${priceFmt.format(c.low)}</span>`,
    `<span class="lbl">C</span> <span class="val ${dirClass}">${priceFmt.format(c.close)}</span>`,
    `<span class="val ${dirClass}">${sign}${changePct.toFixed(2)}%</span>`,
    `<span class="lbl">Vol</span> <span class="val">${volFmt.format(c.volume)}</span>`,
  ].join(" ");
}

function setupTooltip() {
  if (!refs) return;
  const tooltipEl = document.getElementById("tooltip")!;
  const wrap = document.querySelector(".chart-wrap")!;

  refs.chart.timeScale().subscribeVisibleLogicalRangeChange(onVisibleRangeChanged);

  refs.chart.subscribeCrosshairMove((param) => {
    if (!param.point || !param.time || !analysis) {
      tooltipEl.classList.add("hidden");
      updateLegend(null);
      return;
    }
    const candle = param.seriesData.get(refs!.candles) as CandlestickData | undefined;
    const vol = param.seriesData.get(refs!.volume) as HistogramData | undefined;
    if (!candle) {
      tooltipEl.classList.add("hidden");
      updateLegend(null);
      return;
    }

    let idx: number | null = analysis.candles.findIndex((c) => toUnix(c.ts) === param.time);
    if (idx === -1) idx = null;
    updateLegend(idx);

    const m = idx != null ? analysis.macd.macd[idx] : null;
    const s = idx != null ? analysis.macd.signal[idx] : null;
    const h = idx != null ? analysis.macd.hist[idx] : null;
    const ts = idx != null ? analysis.candles[idx].ts : "";

    const change =
      idx != null && idx > 0
        ? ((candle.close - analysis.candles[idx - 1].close) / analysis.candles[idx - 1].close) * 100
        : 0;

    tooltipEl.innerHTML = `
      <div class="t-date">${ts ? fmtDateTime(ts) : ""}</div>
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

    const wrapRect = wrap.getBoundingClientRect();
    let x = param.point.x + 18;
    let y = param.point.y + 14;
    const tw = tooltipEl.offsetWidth;
    const th = tooltipEl.offsetHeight;
    if (x + tw > wrapRect.width - 8) x = param.point.x - tw - 18;
    if (y + th > wrapRect.height - 8) y = param.point.y - th - 14;
    tooltipEl.style.left = `${x}px`;
    tooltipEl.style.top = `${y}px`;
    tooltipEl.classList.remove("hidden");
  });
}

function showBlockParams(c: StrategyCardDto) {
  document.querySelectorAll(".scard").forEach((el) => el.classList.remove("selected"));
  document.querySelector(`.scard[data-sid="${c.id}"]`)?.classList.add("selected");
  const holder = document.getElementById("block-params");
  if (!holder) return;
  const srcVals: Record<string, number> = {};
  document.querySelectorAll<HTMLInputElement>(`#params-${c.id} input`).forEach((inp) => {
    srcVals[inp.dataset.key!] = Number(inp.value);
  });
  const rows = Object.entries(c.params_schema).map(([key, spec]) => `
    <div class="bp-row">
      <span style="min-width:110px">${key}</span>
      <input type="number" data-bp-key="${key}" value="${srcVals[key] ?? spec.default}"
        min="${spec.min ?? ""}" max="${spec.max ?? ""}" step="any" />
      <span class="mini-hint">${spec.min ?? "−∞"}…${spec.max ?? "∞"}</span>
    </div>`).join("");
  const roleSel = document.querySelector<HTMLSelectElement>(`#role-${c.id}`);
  holder.innerHTML = `
    <b>${c.name}</b> <span class="badge fam-${c.family}">${c.family}</span> <span class="wave-badge">W${c.wave}</span>
    <div class="mini-hint" style="margin:4px 0">▲ ${c.long_rule}<br />▼ ${c.short_rule}</div>
    ${rows || `<span class="mini-hint">параметров нет</span>`}
    <label class="bp-row">Роль
      <select id="bp-role">
        <option value="setup"${roleSel?.value === "setup" ? " selected" : ""}>SETUP</option>
        <option value="bias"${roleSel?.value === "bias" ? " selected" : ""}>BIAS</option>
        <option value="entry"${roleSel?.value === "entry" ? " selected" : ""}>ENTRY</option>
      </select>
    </label>`;
  holder.querySelectorAll<HTMLInputElement>("input[data-bp-key]").forEach((inp) =>
    inp.addEventListener("input", () => {
      const key = inp.dataset.bpKey!;
      const orig = document.querySelector<HTMLInputElement>(`#params-${c.id} input[data-key="${key}"]`);
      if (orig) orig.value = inp.value;
      localStorage.setItem(`sklad_p:${c.id}:${key}`, inp.value);
    }));
  holder.querySelector<HTMLSelectElement>("#bp-role")?.addEventListener("change", (ev) => {
    const v = (ev.target as HTMLSelectElement).value;
    const rs = document.querySelector<HTMLSelectElement>(`#role-${c.id}`);
    if (rs) rs.value = v;
    renderConstructor();
  });
}

async function runPreview() {
  const btn = document.getElementById("btn-preview")!;
  const members = [...activeRuns.entries()]
    .filter(([, r]) => r.params)
    .map(([sid, r]) => ({
      strategy_id: sid,
      params: r.params!,
      role: document.querySelector<HTMLSelectElement>(`#role-${CSS.escape(sid)}`)?.value ?? "setup",
    }));
  if (members.length === 0) {
    setStatus("Для превью рассчитайте хотя бы одну стратегию");
    return;
  }
  const kInput = document.getElementById("quorum-k") as HTMLInputElement;
  const quorum = Math.max(1, Math.min(Number(kInput.value) || 1, members.length));
  const tabs = document.querySelectorAll<HTMLButtonElement>(".ptab");
  tabs.forEach((x) => x.classList.remove("active"));
  document.querySelector<HTMLButtonElement>('.ptab[data-tab="preview"]')?.classList.add("active");
  document.getElementById("tab-strategies")!.classList.add("hidden");
  document.getElementById("tab-reports")!.classList.add("hidden");
  document.getElementById("tab-preview")!.classList.remove("hidden");
  btn.classList.add("busy");
  try {
    setStatus("Превью: сохраняю конфигурацию…");
    const mod = await import("./lab");
    const cfgId = await mod.createFromActiveRuns(members, quorum, selExit);
    setStatus("Превью: считаю сигналы и сделки…");
    const pv = await previewConfiguration(cfgId, FIGI, 120);
    const trades = (pv.summary.trades_preview ?? []) as Array<Record<string, unknown>>;
    const sum = (pv.summary.summary ?? {}) as Record<string, unknown>;
    const net = Number(sum.net ?? 0);
    $("pv-summary")!.innerHTML = `
      <span>Сигналов после кворума: <b>${pv.merged_signals_count}</b></span>
      <span>Сделок: <b>${trades.length}</b></span>
      <span class="${net >= 0 ? "pos" : "neg"}">Net: <b>${money(net)} ₽</b></span>
      <span class="mini-hint">${pv.interval_name} · ${daysLabel(120)}</span>`;
    if (trades.length === 0) {
      const f = pv.funnel as Record<string, { BUY?: number; SELL?: number }>;
      const rawTotal = Object.entries(f).filter(([k]) => k !== "quorum")
        .reduce((acc, [, v]) => acc + (v.BUY ?? 0) + (v.SELL ?? 0), 0);
      $("pv-why")!.innerHTML = rawTotal === 0
        ? `<div class="empty-note">Why no entry? Стратегии не дали ни одного сигнала на этой акции/периоде — ослабьте параметры или смените ТФ.</div>`
        : `<div class="empty-note">Why no entry? Сырых сигналов ${rawTotal}, но кворум ${quorum} из ${members.length} не сходится (${pv.merged_signals_count} прошло). Понизьте кворум или добавьте согласованные стратегии.</div>`;
    } else {
      $("pv-why")!.innerHTML = "";
    }
    const fr = Object.entries(pv.funnel as Record<string, { BUY?: number; SELL?: number }>)
      .filter(([k]) => k !== "quorum")
      .map(([k, v]) => `<tr><td>${k.replace(":raw", "")}</td><td class="num">${v.BUY ?? 0}</td><td class="num">${v.SELL ?? 0}</td></tr>`).join("");
    const qRaw = (pv.funnel as Record<string, unknown>).quorum as { signals?: number } | undefined;
    $("pv-funnel")!.innerHTML = `<h3 class="pg-h3">Воронка</h3>
      <table class="runs-table"><thead><tr><th>Стратегия</th><th>BUY</th><th>SELL</th></tr></thead><tbody>${fr}</tbody></table>
      <div class="totals-line"><span>Кворум-сигналов: <b>${qRaw?.signals ?? pv.merged_signals_count}</b></span>
      <span class="mini-hint">Фильтры: каталог фильтров пока не реализован (backend поле filters готово)</span></div>`;
    redrawMarkers();
    for (const t of [...trades].slice(-40)) window.__chartOverlay?.(t);
    setStatus(`Превью готово: сделок ${trades.length}, net ${money(net)} ₽`);
  } catch (e) {
    setStatus(e instanceof Error ? e.message : String(e));
  } finally {
    btn.classList.remove("busy");
  }
}

function daysLabel(d: number): string { return `${d} дней`; }

async function sendActiveToLab() {
  const members = [...activeRuns.entries()]
    .filter(([, r]) => r.params)
    .map(([sid, r]) => ({
      strategy_id: sid,
      params: r.params!,
      role: document.querySelector<HTMLSelectElement>(`#role-${CSS.escape(sid)}`)?.value ?? "setup",
    }));
  const roleCounts = { bias: 0, setup: 0, entry: 0 } as Record<string, number>;
  for (const m of members) roleCounts[m.role] = (roleCounts[m.role] ?? 0) + 1;
  if (members.length === 0) {
    setStatus("Сначала рассчитайте стратегии кнопкой «Рассчитать выбранные»");
    return;
  }
  const kInput = document.getElementById("quorum-k") as HTMLInputElement;
  const quorum = Math.max(1, Math.min(Number(kInput.value) || 1, members.length));
  try {
    $("btn-send-lab").classList.add("busy");
    setStatus("Собираю конфигурацию и отправляю в Lab…");
    const mod = await import("./lab");
    await mod.createFromActiveRuns(members, quorum, selExit);
    setStatus(`Конфигурация создана (${members.length} стр.: bias ${roleCounts.bias ?? 0} / setup ${roleCounts.setup ?? 0} / entry ${roleCounts.entry ?? 0}, кворум ${quorum}) и готова в Lab`);
    document.querySelector<HTMLButtonElement>('.nav-btn[data-page="lab"]')?.click();
  } catch (e) {
    setStatus(e instanceof Error ? e.message : String(e));
  } finally {
    $("btn-send-lab").classList.remove("busy");
  }
}

function consumeTradeFocus() {
  const focus = window.__tradeFocus;
  if (!focus || !refs || !analysis) return;
  window.__chartOverlay?.(focus);
}

function setStatus(text: string) {
  document.getElementById("status-text")!.textContent = text;
}

let latestLogical: { from: number; to: number } | null = null;

function onVisibleRangeChanged(range: { from: number; to: number } | null) {
  if (!refs || !analysis || panBusy || !range) return;
  latestLogical = range;
  if (panTimer) clearTimeout(panTimer);
  panTimer = setTimeout(() => {
    void ensureForViewport();
    void loadOracle();
  }, 600);
}

function visibleWindowSec(): { from: number; to: number } | null {
  if (!refs) return null;
  const r = refs.chart.timeScale().getVisibleRange();
  if (!r) return null;
  const from = Math.floor(Number(r.from));
  const to = Math.ceil(Number(r.to));
  const pad = Math.max((to - from) * 0.05, 2);
  return { from: from - Math.ceil(pad), to: to + Math.ceil(pad) };
}

async function recalcActiveSignalsSilently() {
  if (activeRuns.size === 0 || !refs) return;
  const win = visibleWindowSec();
  if (!win) return;
  let updated = 0;
  for (const [sid, run] of activeRuns) {
    if (!run.params) continue;
    try {
      const res = await computeSignals(FIGI, currentTf.interval, sid, run.params, win.from, win.to);
      activeRuns.set(sid, { ...run, runId: res.run_id, signals: res.signals as { ts: string; side: string }[] });
      updated += 1;
    } catch {
      /* тихо */
    }
  }
  if (updated > 0) redrawMarkers();
}

function makeOraclePrim() {
  if (!refs) return null;
  const holder = { zones: [] as Array<{ from: number; to: number }> };
  const prim = {
    holder,
    paneViews: () => [new OracleZones(refs!.chart, holder.zones)],
    updateAllViews: () => {},
  };
  refs!.candles.attachPrimitive(prim);
  return prim;
}

async function loadOracle() {
  if (!refs || !analysis || oracleBusy) return;
  const active = (document.getElementById("cb-oracle") as HTMLInputElement)?.checked;
  if (!active) {
    if (oraclePrim) {
      refs.candles.detachPrimitive(oraclePrim);
      oraclePrim = null;
    }
    return;
  }
  const win = visibleWindowSec();
  if (!win) return;
  oracleBusy = true;
  try {
    const t0 = performance.now();
    const res = await fetchMaxProfit({
      figi: FIGI,
      interval_name: currentTf.interval,
      limit: 20000,
      threshold_pct: 0.5,
      fee_rate_pct: Number(localStorage.getItem(lk("oracle-fee")) || 0.05),
      capital: 100_000,
      from_ts: new Date(win.from * 1000).toISOString(),
      to_ts: new Date(win.to * 1000).toISOString(),
    });
    if (!refs) return;
    const zones = (res.trades ?? [])
      .map((t) => ({ from: toUnix(t.entry_ts), to: toUnix(t.exit_ts) }))
      .filter((z) => z.to != null);
    if (oraclePrim) {
      refs.candles.detachPrimitive(oraclePrim);
      oraclePrim = null;
    }
    oraclePrim = makeOraclePrim();
    if (oraclePrim) {
      oraclePrim.holder.zones = zones;
      refs.chart.timeScale().applyOptions({}); // триггер перерисовки примитива
    }
    setStatus(`Оракул: ${res.trades?.length ?? 0} сделок по видимому периоду (${((performance.now() - t0) / 1000).toFixed(1)}с)`);
  } catch (e) {
    setStatus(`Оракул: ${e instanceof Error ? e.message : String(e)}`);
  } finally {
    oracleBusy = false;
  }
}

async function ensureForViewport() {
  if (!refs || !analysis || panBusy) return;
  const logical = latestLogical;
  if (!logical) return;
  const bars = analysis.candles.length;
  if (bars === 0) return;

  const stepSec = STEP_SEC[currentTf.interval] ?? 300;
  let wantFromSec: number | null = null;
  let wantToSec: number | null = null;
  if (ensuredFrom !== null && logical.from < -2) {
    wantFromSec = Number(ensuredFrom) + Math.floor(logical.from) * stepSec;
  }
  if (ensuredTo !== null && logical.to > bars - 1) {
    wantToSec = Number(ensuredTo) + Math.ceil(logical.to - (bars - 1)) * stepSec;
  }
  if (wantFromSec === null && wantToSec === null) return;

  const marginBars = Math.ceil((logical.to - logical.from) * 0.5);
  const fromSec = (wantFromSec ?? Number(ensuredFrom)) - marginBars * stepSec;
  const toSec = (wantToSec ?? Number(ensuredTo)) + marginBars * stepSec;

  panBusy = true;
  const prevFirst = ensuredFrom;
  try {
    setStatus("Докачиваю свечи под видимую область…");
    await syncCandles(FIGI, currentTf.interval, currentTf.days, fromSec, toSec);
    analysis = await fetchAnalysis(FIGI, currentTf.interval);
    renderData(analysis, true);
    const addedLeft =
      prevFirst !== null && ensuredFrom !== null
        ? Math.round((Number(ensuredFrom) - Number(prevFirst)) / stepSec)
        : 0;
    if (addedLeft !== 0 && latestLogical) {
      refs!.chart.timeScale().setVisibleLogicalRange({
        from: latestLogical.from + addedLeft,
        to: latestLogical.to + addedLeft,
      });
    }
    redrawMarkers();
    updateLegend(null);
    setStatus(`${analysis.ticker} ${currentTf.label}: ${analysis.candles.length} свечей — диапазон расширен`);
    if (activeRuns.size > 0) await recalcActiveSignalsSilently();
  } catch (e) {
    setStatus(e instanceof Error ? e.message : String(e));
  } finally {
    panBusy = false;
  }
}

async function load(showLoader: boolean) {
  const loader = document.getElementById("loader");
  const btn = document.getElementById("btn-sync");
  try {
    if (showLoader) loader!.classList.remove("hidden");
    btn?.classList.add("busy");

    setStatus(`Проверка кеша ${currentTf.label}…`);
    const report = await syncCandles(FIGI, currentTf.interval, currentTf.days);
    if (report.downloaded > 0) {
      setStatus(`Докачано из биржи: ${report.downloaded} свечей`);
    }

    analysis = await fetchAnalysis(FIGI, currentTf.interval);
    if (analysis.candles.length === 0) {
      setStatus(`${currentTf.label}: данных нет ни в базе, ни на бирже`);
    }
    renderData(analysis);
    syncIndicatorVisibility();
    consumeTradeFocus();
    updateLegend(null);
    if (analysis.candles.length > 0) {
      setStatus(
        `${analysis.ticker} ${currentTf.label}: ${analysis.candles.length} свечей` +
          (report.downloaded > 0 ? ` (докачано ${report.downloaded})` : " (из кеша)"),
      );
    }
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (msg.includes("Failed to fetch") || msg.includes("NetworkError")) {
      setStatus("Нет связи с бэкендом — проверьте, что он запущен и BACKEND_URL указан верно");
    } else {
      setStatus(msg);
    }
  } finally {
    loader!.classList.add("hidden");
    btn?.classList.remove("busy");
  }
  void loadOracle();
}

function syncIndicatorVisibility() {
  if (!refs) return;
  refs.sma.applyOptions({ visible: indicators.sma });
  refs.emaLine.applyOptions({ visible: indicators.ema });
  refs.bbUpper.applyOptions({ visible: indicators.bb });
  refs.bbLower.applyOptions({ visible: indicators.bb });
}

function rebuildChartPreservingView() {
  if (!refs || !analysis) return;
  const logical = refs.chart.timeScale().getVisibleLogicalRange();
  const container = document.getElementById("chart")!;
  refs.chart.remove();
  refs = null;
  chartFlags = { oscPane: indicators.macd || indicators.rsi };
  renderData(analysis, true);
  redrawMarkers();
  updateLegend(null);
  if (logical) {
    refs!.chart.timeScale().setVisibleLogicalRange(logical);
  }
  void container;
}

function applyIndicators() {
  localStorage.setItem(lk("indicators"), JSON.stringify(indicators));
  const wantPane = indicators.macd || indicators.rsi;
  if (wantPane !== chartFlags.oscPane) {
    rebuildChartPreservingView();
  } else {
    syncIndicatorVisibility();
    if (chartFlags.oscPane && refs) {
      const panes = refs.chart.panes();
      if (panes[1]) {
        const on = indicators.macd || indicators.rsi;
        panes[1].setStretchFactor(on ? 2 : 0);
        panes[0].setStretchFactor(3);
      }
    }
  }
}

window.__setTradeLines = (trade: Record<string, unknown>) => {
  if (!refs) return;
  try {
    _overlayLines.forEach((l) => l.remove());
    _overlayLines = [];
    const mkLine = (price: number, color: string, title: string, dashed: boolean) => {
      const line = refs!.candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: dashed ? 2 : 0, axisLabelVisible: true, title });
      _overlayLines.push(line as { remove: () => void });
    };
    if (Number(trade.entry_price) > 0) mkLine(Number(trade.entry_price), "#e6a23c", "Вход", false);
    const stop = trade.initial_stop ?? trade.stop_loss ?? null;
    if (stop != null && Number(stop) > 0) mkLine(Number(stop), COLORS.down, "SL", true);
    const tp = trade.take_profit ?? null;
    if (tp != null && Number(tp) > 0) mkLine(Number(tp), COLORS.up, "TP", true);
  } catch { /* noop */ }
};

window.__chartOverlay = (trade: Record<string, unknown>) => {
  if (!refs) return;
  try {
    const entryTime = Math.floor(new Date(String(trade.entry_time)).getTime() / 1000) as UTCTimestamp;
    const rawExit = trade.exit_time;
    const exitTime = rawExit ? (Math.floor(new Date(String(rawExit)).getTime() / 1000) as UTCTimestamp) : null;
    const side = String(trade.side);
    const pnl = trade.net_pnl != null ? Number(trade.net_pnl) : null;
    const color = side === "LONG" ? COLORS.up : COLORS.down;
    const markers: SeriesMarker<Time>[] = [
      { time: entryTime, position: side === "LONG" ? "belowBar" : "aboveBar", color, shape: "arrowUp", text: `Вход` },
    ];
    if (exitTime != null) {
      markers.push({ time: exitTime, position: side === "LONG" ? "aboveBar" : "belowBar", color: pnl != null && pnl >= 0 ? COLORS.up : COLORS.down, shape: "circle", text: pnl != null ? `Выход ${money(pnl)} ₽` : "Выход" });
    }
    window.__setTradeLines?.(trade);
    refs.markers.setMarkers(markers);
    if (exitTime != null) {
      refs.chart.timeScale().setVisibleLogicalRange({ from: Number(entryTime), to: Number(exitTime) } as never);
    }
    setStatus(`Сделка ${side}${pnl != null ? " " + money(pnl) + " ₽" : ""} — отмечена на графике`);
  } catch (e) {
    console.warn("overlay error", e);
  }
};

function initToolbar() {
  const nav = document.getElementById("timeframes")!;
  for (const tf of TIMEFRAMES) {
    const b = document.createElement("button");
    b.className = "tf-btn" + (tf.interval === currentTf.interval ? " active" : "");
    b.textContent = tf.label;
    b.addEventListener("click", () => {
      if (tf.interval === currentTf.interval) return;
      nav.querySelectorAll(".tf-btn").forEach((el) => el.classList.remove("active"));
      b.classList.add("active");
      currentTf = tf;
      localStorage.setItem(lk("sklad_tf"), tf.interval);
      window.LAB_INTERVAL = tf.interval;
      activeRuns.clear();
      quorumState = null;
      document.getElementById("quorum-m")!.textContent = "0";
      if (refs) refs.markers.setMarkers([]);
      load(true);
    });
    nav.appendChild(b);
  }

  const cbm = document.getElementById("cb-markers") as HTMLInputElement;
  cbm.checked = localStorage.getItem(lk("sklad_markers")) !== "0";
  cbm.addEventListener("change", () => localStorage.setItem(lk("sklad_markers"), cbm.checked ? "1" : "0"));
  const qk = document.getElementById("quorum-k") as HTMLInputElement;
  const savedQ = Number(localStorage.getItem(lk("sklad_quorum")));
  if (savedQ >= 1) qk.value = String(savedQ);
  qk.addEventListener("change", () => localStorage.setItem(lk("sklad_quorum"), qk.value));

  document.querySelectorAll<HTMLInputElement>("#indicators-menu input[data-ind]").forEach((box) => {
    box.checked = (indicators as unknown as Record<string, boolean>)[box.dataset.ind!] ?? false;
    box.addEventListener("change", () => {
      (indicators as unknown as Record<string, boolean>)[box.dataset.ind!] = box.checked;
      applyIndicators();
    });
  });

  const btnInd = document.getElementById("btn-indicators")!;
  const menu = document.getElementById("indicators-menu")!;
  btnInd.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.classList.toggle("hidden");
  });
  document.addEventListener("click", (e) => {
    if (!menu.contains(e.target as Node)) menu.classList.add("hidden");
  });

  document.getElementById("btn-sync")!.addEventListener("click", () => load(true));
  document.getElementById("cb-oracle")?.addEventListener("change", () => void loadOracle());
}

function initResizer() {
  const wrap = document.getElementById("chart-wrap")!;
  const resizer = document.getElementById("resizer")!;

  const saved = Number(localStorage.getItem(lk("chartHeight")));
  const initial =
    saved > 0 ? saved : Math.round(window.innerHeight * 0.5);
  wrap.style.height = `${clampHeight(initial)}px`;

  let startY = 0;
  let startH = 0;

  const onMove = (e: PointerEvent) => {
    const h = clampHeight(startH + (e.clientY - startY));
    wrap.style.height = `${h}px`;
  };
  const onUp = () => {
    resizer.classList.remove("dragging");
    localStorage.setItem(lk("chartHeight"), String(wrap.clientHeight));
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

function clampHeight(h: number): number {
  return Math.max(240, Math.min(h, window.innerHeight - 160));
}

function redrawMarkers() {
  if (!refs) return;
  const show = (document.getElementById("cb-markers") as HTMLInputElement).checked;
  if (!show) {
    refs.markers.setMarkers([]);
    return;
  }
  const markers: SeriesMarker<Time>[] = [];
  for (const [sid, run] of activeRuns) {
    const tag = TAG_BY_STRATEGY[sid] ?? sid.slice(0, 3).toUpperCase();
    for (const s of run.signals) {
      markers.push({
        time: toUnix(s.ts),
        position: s.side === "BUY" ? "belowBar" : "aboveBar",
        color: s.side === "BUY" ? COLORS.up : COLORS.down,
        shape: s.side === "BUY" ? "arrowUp" : "arrowDown",
        text: tag,
      });
    }
  }
  if (quorumState) {
    for (const s of quorumState.signals) {
      const votes = Number(s.features.votes ?? quorumState.k);
      markers.push({
        time: toUnix(s.ts),
        position: s.side === "BUY" ? "belowBar" : "aboveBar",
        color: "#b388ff",
        shape: s.side === "BUY" ? "arrowUp" : "arrowDown",
        text: `Q${votes}/${quorumState.m}`,
      });
    }
  }
  markers.sort((a, b) => Number(a.time) - Number(b.time));
  refs.markers.setMarkers(markers);
}

async function runQuorum() {
  const btn = document.getElementById("btn-quorum")!;
  const members = [...activeRuns.entries()];
  if (members.length < 2) {
    setStatus("Кворуму нужно минимум 2 рассчитанные стратегии");
    return;
  }
  const kInput = document.getElementById("quorum-k") as HTMLInputElement;
  let k = Math.max(1, Math.min(Number(kInput.value) || 2, members.length));
  kInput.value = String(k);
  btn.classList.add("busy");
  try {
    setStatus(`Кворум ${k}-of-${members.length}…`);
    const res = await computeQuorum(
      members.map(([, r]) => r.runId),
      k,
    );
    quorumState = { runId: res.run_id, k, m: members.length, signals: res.signals };
    redrawMarkers();
    setStatus(`Кворум ${k}/${members.length}: ${res.count} сигналов${res.cached ? " (из кеша)" : ""}`);
  } catch (e) {
    setStatus(e instanceof Error ? e.message : String(e));
  } finally {
    btn.classList.remove("busy");
  }
}

async function runSelectedStrategies() {
  const btn = document.getElementById("btn-run-strategies")!;
  const checked = [...document.querySelectorAll<HTMLInputElement>("#strategy-cards input:checked")];
  if (checked.length === 0) {
    setStatus("Не выбрано ни одной стратегии");
    return;
  }
  btn.classList.add("busy");
  let ok = 0;
  try {
    for (const box of checked) {
      const card = catalogCards.find((c) => c.id === box.dataset.sid);
      if (!card) continue;
      const params: Record<string, number> = {};
      document
        .querySelectorAll<HTMLInputElement>(`#params-${card.id} input`)
        .forEach((inp) => {
          params[inp.dataset.key!] = Number(inp.value);
        });
      setStatus(`Расчёт ${card.name} (${currentTf.label})…`);
      const win = visibleWindowSec();
      const res = await computeSignals(FIGI, currentTf.interval, card.id, params, win?.from, win?.to);
      activeRuns.set(card.id, { runId: res.run_id, signals: res.signals as { ts: string; side: string }[], params });
      ok += 1;
      setStatus(
        `${card.name}: ${res.count} сигналов${res.cached ? " (из кеша)" : ""}`,
      );
    }
    redrawMarkers();
      setStatus(`Готово: ${ok} стратегий рассчитано, всего маркеров на графике`);
      document.getElementById("quorum-m")!.textContent = String(activeRuns.size);
      renderConstructor();
      await refreshRunsTable();
  } catch (e) {
    setStatus(e instanceof Error ? e.message : String(e));
  } finally {
    btn.classList.remove("busy");
  }
}

let exitPolicies: Array<{ id: string; label: string; params_schema: Record<string, { default: number; min?: number; max?: number }> }> = [];
let selExit = { id: "fixed_sl_tp", params: { stop_pct: 1, target_pct: 2 } } as { id: string; params: Record<string, number> };
let selectedSlot: string | null = null;

const ROLE_META: Record<string, { title: string; cls: string; hint: string }> = {
  bias: { title: "BIAS · контекст", cls: "blk-blue", hint: "направление старшего ТФ" },
  setup: { title: "SETUP · сетап", cls: "blk-purple", hint: "где ищем вход" },
  entry: { title: "ENTRY · триггер", cls: "blk-green", hint: "точка входа" },
};

function renderConstructor() {
  const holder = document.getElementById("pipe-holder");
  if (!holder) return;
  const slotsHtml = ["bias", "setup", "entry"].map((role) => {
    const meta = ROLE_META[role];
    const members = [...activeRuns.entries()].filter(([sid]) =>
      document.querySelector<HTMLSelectElement>(`#role-${CSS.escape(sid)}`)?.value === role);
    const list = members.map(([sid, r]) => {
      const card = catalogCards.find((c) => c.id === sid);
      return `<div class="ps-item" data-sid="${sid}" data-role="${role}">
        <b>${card?.name ?? sid}</b>
        <span class="mini-hint">${Object.entries(r.params ?? {}).map(([k, v]) => `${k}=${v}`).join(", ")}</span>
      </div>`;
    }).join("");
    return `<div class="pipe-slot ${meta.cls}${selectedSlot === role ? " sel" : ""}" data-slot="${role}">
      <div class="ps-title">${meta.title}<span class="badge">${members.length}</span></div>
      <div class="ps-sub">${meta.hint}</div>
      ${list || `<div class="mini-hint">пусто — отметьте стратегии в каталоге и назначьте роль</div>`}
    </div><div class="pipe-arrow">↓</div>`;
  }).join("");

  const exitSchema = exitPolicies.find((e) => e.id === selExit.id)?.params_schema ?? {};
  const exitParamsHtml = Object.entries(exitSchema).map(([k, spec]) =>
    `<label class="mini-hint">${k} <input type="number" data-exit-key="${k}" value="${selExit.params[k] ?? spec.default}" step="any" style="width:64px"></label>`
  ).join(" ");
  const pipe = `${slotsHtml}
    <div class="pipe-slot blk-gray${selectedSlot === "position" ? " sel" : ""}" data-slot="position">
      <div class="ps-title">POSITION <span class="qmark" title="Риск-модуль и мультипозиционность в движке не реализованы — параметры не применяются">?</span></div>
      <div class="ps-sub">qty=1 · одна позиция на FIGI · mode both</div>
    </div>
    <div class="pipe-side">
      <div class="pipe-slot blk-red${selectedSlot === "exit" ? " sel" : ""}" data-slot="exit" style="flex:1">
        <div class="ps-title">EXIT
          <select id="exit-sel">${exitPolicies.map((e) => `<option value="${e.id}"${e.id === selExit.id ? " selected" : ""}>${e.label}</option>`).join("")}</select>
        </div>
        <div id="exit-params" style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px">${exitParamsHtml}</div>
      </div>
      <div class="pipe-slot blk-gray" style="flex:1">
        <div class="ps-title">RISK <span class="qmark" title="Модуль Risk в движке не реализован">?</span></div>
        <div class="mini-hint">fixed qty=1 · max daily loss — ?</div>
      </div>
    </div>
    <div class="pipe-slot blk-orange" style="margin-top:6px">
      <div class="ps-title">FILTERS <span class="qmark" title="Фильтры (VWAP side, ADX, ATR pct, volume ratio…) движком не исполняются">?</span></div>
      <div class="mini-hint">session time / cooldown / min hold уже работают (сессия, кулдаун, min_hold_bars)</div>
    </div>`;

  holder.innerHTML = pipe;

  holder.querySelectorAll<HTMLElement>(".pipe-slot").forEach((el) =>
    el.addEventListener("click", () => { selectedSlot = el.dataset.slot!; renderConstructor(); }));

  const exSel = holder.querySelector<HTMLSelectElement>("#exit-sel");
  exSel?.addEventListener("change", () => {
    const e = exitPolicies.find((x) => x.id === exSel.value);
    selExit = { id: exSel.value, params: Object.fromEntries(Object.entries(e?.params_schema ?? {}).map(([k, sp]) => [k, sp.default])) };
    renderConstructor();
  });
  holder.querySelectorAll<HTMLInputElement>("input[data-exit-key]").forEach((inp) =>
    inp.addEventListener("change", () => { selExit.params[inp.dataset.exitKey!] = Number(inp.value); }));
}

function renderCatalog(cards: StrategyCardDto[]) {
  const wrap = document.getElementById("strategy-cards")!;
  wrap.innerHTML = "";
  for (const c of cards) {
    const el = document.createElement("div");
    el.className = "scard";
    el.dataset.sid = c.id;
    const paramsHtml = Object.entries(c.params_schema)
      .map(
        ([key, spec]) => {
          const saved = Number(localStorage.getItem(`sklad_p:${c.id}:${key}`));
          const val = Number.isFinite(saved) && localStorage.getItem(`sklad_p:${c.id}:${key}`) !== null ? saved : spec.default;
          return `<label>${key}<input type="number" data-key="${key}" value="${val}"` +
            ` min="${spec.min ?? ""}" max="${spec.max ?? ""}" step="any" /></label>`;
        },
      )
      .join("");
    el.innerHTML = `
      <label class="scard-head">
        <input type="checkbox" data-sid="${c.id}" />
        <span class="scard-name">${c.name}</span>
        <span class="badge fam-${c.family}">${c.family}</span>
        <span class="wave-badge">W${c.wave}</span>
        <button class="scard-reset" data-reset="${c.id}" title="Сбросить параметры к дефолту">↺</button>
      </label>
      <div class="scard-rules">▲ ${c.long_rule}<br />▼ ${c.short_rule}</div>
      <div class="scard-params" id="params-${c.id}">${paramsHtml}</div>
      <label class="scard-role">Роль
        <select id="role-${c.id}">
          <option value="setup"${localStorage.getItem(`sklad_role:${c.id}`) === "bias" ? "" : " selected"}>SETUP</option>
          <option value="bias"${localStorage.getItem(`sklad_role:${c.id}`) === "bias" ? " selected" : ""}>BIAS</option>
          <option value="entry"${localStorage.getItem(`sklad_role:${c.id}`) === "entry" ? " selected" : ""}>ENTRY</option>
        </select>
      </label>
    `;
    const cb = el.querySelector<HTMLInputElement>("input[type=checkbox]")!;
    cb.checked = localStorage.getItem(`sklad_check:${c.id}`) !== "0";
    el.addEventListener("click", (ev) => {
      const target = ev.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "SELECT") return;
      if ((target as HTMLElement).classList.contains("scard-reset")) return;
      showBlockParams(c);
    });
    el.querySelector<HTMLButtonElement>(".scard-reset")!.addEventListener("click", (ev) => {
      ev.stopPropagation();
      for (const key of Object.keys(c.params_schema)) localStorage.removeItem(`sklad_p:${c.id}:${key}`);
      el.querySelectorAll<HTMLInputElement>(".scard-params input").forEach((inp) => {
        inp.value = String(c.params_schema[inp.dataset.key!]?.default ?? inp.value);
      });
      showBlockParams(c);
    });
    el.querySelectorAll<HTMLInputElement>(".scard-params input").forEach((inp) =>
      inp.addEventListener("change", () =>
        localStorage.setItem(`sklad_p:${c.id}:${inp.dataset.key!}`, inp.value)));
    el.querySelector("input[type=checkbox]")!.addEventListener("change", (ev) => {
      const on = (ev.target as HTMLInputElement).checked;
      localStorage.setItem(`sklad_check:${c.id}`, on ? "1" : "0");
      el.classList.toggle("checked", on);
      if (!on) {
        activeRuns.delete(c.id);
        redrawMarkers();
      }
      renderConstructor();
    });
    el.querySelector<HTMLSelectElement>(`#role-${c.id}`)!.addEventListener("change", (ev) => {
      localStorage.setItem(`sklad_role:${c.id}`, (ev.target as HTMLSelectElement).value);
      renderConstructor();
    });
    wrap.appendChild(el);
  }
}

async function refreshRunsTable() {
  try {
    const runs = await fetchRuns(FIGI);
    const tbody = document.getElementById("runs-tbody")!;
    tbody.innerHTML = "";
    for (const r of runs) {
      const tr = document.createElement("tr");
      const created = r.created_at ? new Date(r.created_at).toLocaleString("ru-RU") : "—";
      const paramsStr = Object.entries(r.params)
        .map(([k, v]) => `${k}=${v}`)
        .join(", ");
      tr.innerHTML = `
        <td>${TAG_BY_STRATEGY[r.strategy_id] ?? r.strategy_id}</td>
        <td>${r.interval_name}</td>
        <td class="num">${activeRuns.get(r.strategy_id)?.runId === r.run_id ? activeRuns.get(r.strategy_id)!.signals.length : "—"}</td>
        <td class="num">${r.bars}</td>
        <td>${paramsStr}</td>
        <td>${created}</td>
      `;
      tbody.appendChild(tr);
    }
  } catch (e) {
    setStatus(e instanceof Error ? e.message : String(e));
  }
}

function initPanel() {
  const tabs = document.querySelectorAll<HTMLButtonElement>(".ptab");
  tabs.forEach((t) =>
    t.addEventListener("click", () => {
      tabs.forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      document.getElementById("tab-strategies")!.classList.toggle("hidden", t.dataset.tab !== "strategies");
      document.getElementById("tab-reports")!.classList.toggle("hidden", t.dataset.tab !== "reports");
      document.getElementById("tab-preview")!.classList.toggle("hidden", t.dataset.tab !== "preview");
      document.getElementById("tab-diag")?.classList.toggle("hidden", t.dataset.tab !== "diag");
      if (t.dataset.tab === "reports") void refreshRunsTable();
    }),
  );

  document.getElementById("btn-run-strategies")!.addEventListener("click", () => void runSelectedStrategies());
  $("btn-send-lab")!.addEventListener("click", () => void sendActiveToLab());
  document.getElementById("btn-preview")!.addEventListener("click", () => void runPreview());
  document.getElementById("btn-quorum")!.addEventListener("click", () => void runQuorum());
  document.getElementById("cb-markers")!.addEventListener("change", redrawMarkers);

  fetchCatalog()
    .then((cat) => {
      catalogCards = cat.strategies;
      renderCatalog(cat.strategies);
      exitPolicies = ((cat as any).exit_policies ?? []) as typeof exitPolicies;
      renderConstructor();
    })
    .catch((e) => setStatus(e instanceof Error ? e.message : String(e)));
}

initToolbar();
initResizer();
initPanel();
load(true);
setupTooltipAfterFirstRender();

function setupTooltipAfterFirstRender() {
  const observer = new MutationObserver(() => {
    if (refs) {
      setupTooltip();
      observer.disconnect();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}


function initSidebarResize() {
  const sb = document.getElementById("sidebar");
  if (!sb) return;
  const KEY = "deeptrading_sidebar_w";
  try {
    const saved = localStorage.getItem(KEY);
    if (saved) {
      const w = Math.max(190, Math.min(560, Number(saved)));
      if (w > 0) sb.style.width = w + "px";
    }
  } catch {
    /* noop */
  }
  window.addEventListener("mouseup", () => {
    try {
      localStorage.setItem(KEY, String(Math.round(sb.getBoundingClientRect().width)));
    } catch {
      /* noop */
    }
  });
}

initSidebarResize();
if (IS_EMBEDDED) {
  let curFigi = FIGI;
  window.addEventListener("message", (ev) => {
    const d = ev.data as { type?: string; figi?: string; ticker?: string; trade?: Record<string, unknown> };
    if (!d || d.type !== "focus") return;
    void (async () => {
      try {
        const f = String(d.figi || "");
        if (f && f !== curFigi) {
          curFigi = f;
          FIGI = f;
          window.FIGI = f;
          let data = await fetchAnalysis(f, currentTf.interval, 2000);
          if (!(data && data.candles && data.candles.length)) {
            try {
              await syncCandles(f, currentTf.interval, ENSURE_DAYS[currentTf.interval] ?? 7);
            } catch { /* noop */ }
            data = await fetchAnalysis(f, currentTf.interval, 2000);
          }
          if (data && data.candles && data.candles.length) {
            renderData(data);
          } else {
            setStatus("нет свечей в базе для " + (d.ticker || f));
          }
        }
        if (refs) {
          const hist = (d.trades || []) as Record<string, unknown>[];
          const mks: SeriesMarker<Time>[] = [];
          for (const tr of hist) {
            const et = tr.entry_time ? toUnix(String(tr.entry_time)) : null;
            const xt = tr.ts ? toUnix(String(tr.ts)) : null;
            const isL = String(tr.side) === "LONG";
            if (et != null && !isNaN(Number(et))) mks.push({ time: et, position: isL ? "belowBar" : "aboveBar", color: COLORS.up, shape: "arrowUp", text: String(tr.qty ?? "") });
            if (xt != null && tr.exit_price != null && !isNaN(Number(xt))) mks.push({ time: xt, position: isL ? "aboveBar" : "belowBar", color: COLORS.down, shape: "arrowDown", text: "" });
          }
          if (mks.length) refs.markers.setMarkers(mks);
          if (d.trade) window.__setTradeLines?.(d.trade);
        }
      } catch (e) {
        console.warn("embed focus error", e);
      }
    })();
  });
}

initNav();
void initLab();
void initBot();
void initTest();
void initEnsLab();
