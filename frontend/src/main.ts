const API = window.location.port === "5173" ? `http://${window.location.hostname}:8000` : "";

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
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type SeriesMarker,
  type Time,
  TickMarkType,
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
import type { SyncReport } from "./api";
import { initEnsLab } from "./enslab";
import { OracleZones } from "./test";

declare global {
  interface Window {
    FIGI: string;
    __setTradeLines?: (trade: Record<string, unknown>) => void;
    __chartOverlay?: ((trade: Record<string, unknown>) => void) | null;
    __tradeFocus?: Record<string, unknown> | null;
  }
}
window.FIGI = "BBG004730N88";

let FIGI = "BBG004730N88";
let _renderedFigi = "";
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

// Сколько баров показывать по умолчанию (1min=1.5ч, 5min=5ч, 10min=10ч, …):
const WINDOW_BARS: Record<string, number> = {
  "1min": 90,
  "5min": 60,
  "10min": 60,
  "15min": 60,
  hour: 60,
  "2h": 60,
  "4h": 60,
  day: 60,
  week: 60,
  month: 60,
};

// Барам сверх окна (warmup) — чтобы индикаторы (SMA/EMA/MACD) были корректны у правого края
const TF_LIMIT: Record<string, number> = {
  "1min": 150,
  "5min": 120,
  "10min": 120,
  "15min": 120,
  hour: 120,
  "2h": 120,
  "4h": 120,
  day: 120,
  week: 120,
  month: 120,
};
function windowLimit(tf: string): number {
  return TF_LIMIT[tf] ?? (WINDOW_BARS[tf] ?? 60) + 40;
}

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
if (IS_EMBEDDED) {
  document.body.classList.add("embedded");
  const chartPage = document.getElementById("page-chart");
  const botPage = document.getElementById("page-bot");
  if (chartPage) chartPage.classList.add("active");
  if (botPage) botPage.classList.remove("active");
  const chartBtn = document.querySelector('.nav-btn[data-page="chart"]');
  const botBtn = document.querySelector('.nav-btn[data-page="bot"]');
  if (chartBtn) chartBtn.classList.add("active");
  if (botBtn) botBtn.classList.remove("active");
  const _pt = document.getElementById("page-title");
  if (_pt) _pt.textContent = "График";
}
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
      const _pt = $("page-title");
      if (_pt) _pt.textContent = PAGE_TITLES[page] ?? page;
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
let _overlayLines: IPriceLine[] = [];
let oracleBusy = false;
let analysis: AnalysisDto | null = null;

// ---- CDBG: диагностика графика (временный харнесс) ----
const __CDBG: string[] = [];
let _cdbgEl: HTMLElement | null = null;
let _cdbgCount = 0;
function cdbg(...args: unknown[]) {
  const line = `[${new Date().toISOString().slice(11, 23)}] ${args.map((a) => (typeof a === "string" ? a : JSON.stringify(a))).join(" ")}`;
  __CDBG.push(line);
  if (__CDBG.length > 1500) __CDBG.splice(0, __CDBG.length - 1500);
  _cdbgCount++;
  try { console.log(line); } catch { /* noop */ }
  let el = _cdbgEl;
  if (!el) {
    el = document.getElementById("cdbg") as HTMLElement | null;
    if (!el) {
      el = document.createElement("div");
      el.id = "cdbg";
      el.style.cssText = "display:none;white-space:pre;font:10px monospace;";
      document.body.appendChild(el);
    }
    _cdbgEl = el;
  }
  el.textContent = __CDBG.join("\n");
  try {
    const lines = __CDBG.slice(-6);
    document.title = "G|" + lines.join("§").replace(/"/g, "'").slice(-170);
  } catch { /* noop */ }
}
(window as any).__CDBG = __CDBG;
(window as any).cdbg = cdbg;
function cdbgView(tag: string) {
  if (!refs) { cdbg(tag, "no-refs"); return; }
  try {
    const lg = refs.chart.timeScale().getVisibleLogicalRange();
    const vr = refs.chart.timeScale().getVisibleRange();
    cdbg(tag, "lg=" + (lg ? `${lg.from.toFixed(1)}..${lg.to.toFixed(1)}` : "null"), "vr=" + (vr ? `${Math.floor(Number(vr.from))}..${Math.floor(Number(vr.to))}` : "null"));
  } catch (e) { cdbg(tag, "view-err", String(e)); }
}
// ---------------------------------------------
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
    timeScale: {
      borderColor: COLORS.grid,
      timeVisible: true,
      secondsVisible: false,
      tickMarkFormatter: (t: Time, type: TickMarkType) => axisTickLabel(t, type),
    },
    localization: {
      locale: "ru-RU",
      priceFormatter: (p: number) => priceFmt.format(p),
      timeFormatter: (t: Time) => axisTimeLabel(t as never),
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
  cdbg("renderData", "keepView=" + keepView, "n=" + data.candles.length, "first=" + (data.candles[0]?.ts ?? "-"), "last=" + (data.candles[data.candles.length - 1]?.ts ?? "-"));
  if (!refs) refs = buildChart();
  const r = refs;
  const n = data.candles.length;
  const dataFigi = data.figi ?? FIGI;
  const figiChanged = dataFigi !== _renderedFigi && _renderedFigi !== "";
  if (figiChanged && keepView) {
    cdbg("renderData", "figi сменился (" + _renderedFigi + "→" + dataFigi + ") — принудительный reset вместо keepView");
    keepView = false;
  }
  if (!keepView && refs) { try { refs.markers.setMarkers([]); } catch { /* noop */ } }

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
  _renderedFigi = dataFigi;

  if (!keepView) {
    const want = WINDOW_BARS[currentTf.interval] ?? 60;
    try {
      const ps = r.chart.priceScale('right');
      ps.applyOptions({ autoScale: false });
      ps.applyOptions({ autoScale: true });
    } catch { /* noop */ }
    try {
      r.chart.timeScale().applyOptions({ rightOffset: 0 });
      const lastIdx = Math.max(0, candlePoints.length - 1);
      const fromIdx = Math.max(0, lastIdx - want + 1);
      cdbg("renderData-reset", "want=" + want, "lastIdx=" + lastIdx, "fromIdx=" + fromIdx);
      r.chart.timeScale().setVisibleLogicalRange({
        from: Math.min(fromIdx, lastIdx),
        to: Math.max(fromIdx, lastIdx),
      });
    } catch { /* noop */ }
  }
  cdbgView("renderData-end keepView=" + keepView);

  document.getElementById("symbol-ticker")!.textContent = data.ticker || FIGI;
  document.getElementById("symbol-name")!.textContent = data.name || "";
}

const TZ = "Europe/Moscow";
const _dtMSK = new Intl.DateTimeFormat("ru-RU", { timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const _dtMSKFull = new Intl.DateTimeFormat("ru-RU", { timeZone: TZ, day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });

function fmtDateTime(ts: string): string {
  return _dtMSKFull.format(new Date(ts));
}

function fmtUTC(sec: number): string {
  return _dtMSKFull.format(new Date(sec * 1000));
}

function axisTimeLabel(t: unknown): string {
  try {
    const num = typeof t === "number" ? t : Number(t);
    return Number.isFinite(num) ? _dtMSK.format(new Date(num * 1000)) : String(t ?? "");
  } catch { return String(t ?? ""); }
}

const _dtMSKYear = new Intl.DateTimeFormat("ru-RU", { timeZone: TZ, year: "numeric" });
const _dtMSKMonth = new Intl.DateTimeFormat("ru-RU", { timeZone: TZ, month: "short", year: "numeric" });
const _dtMSKDay = new Intl.DateTimeFormat("ru-RU", { timeZone: TZ, day: "2-digit", month: "2-digit" });
const _dtMSKSec = new Intl.DateTimeFormat("ru-RU", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

function _mskDateOf(t: Time): Date | null {
  if (typeof t === "number") return new Date(t * 1000);
  if (typeof t === "string") {
    const ms = Date.parse(t);
    return Number.isFinite(ms) ? new Date(ms) : null;
  }
  if (t && typeof t === "object" && "year" in t && "month" in t && "day" in t) {
    const b = t as { year: number; month: number; day: number };
    return new Date(Date.UTC(b.year, b.month - 1, b.day));
  }
  return null;
}

function axisTickLabel(t: Time, type: TickMarkType): string {
  try {
    const d = _mskDateOf(t);
    if (!d) return String(t);
    switch (type) {
      case TickMarkType.Year: return _dtMSKYear.format(d);
      case TickMarkType.Month: return _dtMSKMonth.format(d);
      case TickMarkType.DayOfMonth: return _dtMSKDay.format(d);
      case TickMarkType.TimeWithSeconds: return _dtMSKSec.format(d);
      case TickMarkType.Time:
      default: return _dtMSK.format(d);
    }
  } catch { return String(t); }
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

  const resetView = () => {
    try {
      cdbg("resetView", "dblclick");
      refs!.chart.timeScale().resetTimeScale();
      refs!.chart.timeScale().scrollToRealTime();
      cdbgView("resetView-done");
    } catch { /* noop */ }
  };
  wrap.addEventListener("dblclick", resetView);

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
  cdbg("visrange", "from=" + range.from.toFixed(1), "to=" + range.to.toFixed(1), "bars=" + analysis.candles.length, "lastDel=" + (analysis.candles.length - 1 - range.to).toFixed(1));
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
  cdbg("ensure", "logical=" + logical.from.toFixed(1) + ".." + logical.to.toFixed(1), "bars=" + bars, "ensured=" + String(ensuredFrom) + ".." + String(ensuredTo));

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
      const nf = latestLogical.from + addedLeft;
      const nt = latestLogical.to + addedLeft;
      refs!.chart.timeScale().setVisibleLogicalRange({
        from: Math.min(nf, nt),
        to: Math.max(nf, nt),
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

    let report: SyncReport | null = null;
    analysis = await fetchAnalysis(FIGI, currentTf.interval, windowLimit(currentTf.interval));
    if (analysis.candles.length === 0) {
      setStatus(`Докачиваю свежие ${currentTf.label}…`);
      report = await syncCandles(FIGI, currentTf.interval, currentTf.days);
      analysis = await fetchAnalysis(FIGI, currentTf.interval, windowLimit(currentTf.interval));
    }
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
          (report?.downloaded ? ` (докачано ${report.downloaded})` : " (из кеша)"),
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

function _voteText(metaRaw: unknown): string {
  try {
    const m = typeof metaRaw === "string" ? JSON.parse(metaRaw) : metaRaw;
    const q = m && m.quorum_event;
    if (q && Array.isArray(q.members_for) && q.members_for.length) {
      return (q.members_for as string[]).join("+");
    }
    if (q && q.reason) return String(q.reason);
  } catch { /* noop */ }
  return "";
}

function showVoteNote(text: string) {
  let el = document.getElementById("embed-note");
  if (!el) {
    el = document.createElement("div");
    el.id = "embed-note";
    const wrap = document.querySelector(".chart-wrap");
    if (wrap) wrap.appendChild(el);
  }
  if (!el) return;
  el.textContent = text;
  (el as HTMLElement).style.display = text ? "block" : "none";
}

window.__setTradeLines = (trade: Record<string, unknown>) => {
  if (!refs) return;
  try {
    for (const l of _overlayLines) { try { refs.candles.removePriceLine(l); } catch { /* noop */ } }
    _overlayLines = [];
    const mkLine = (price: number, color: string, title: string, dashed: boolean) => {
      const line = refs!.candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: dashed ? 2 : 0, axisLabelVisible: true, title });
      _overlayLines.push(line);
    };
    if (Number(trade.entry_price) > 0) mkLine(Number(trade.entry_price), "#e6a23c", "Вход", false);
    const stop = trade.initial_stop ?? trade.stop_loss ?? null;
    if (stop != null && Number(stop) > 0) mkLine(Number(stop), COLORS.down, "SL", true);
    const tp = trade.take_profit ?? null;
    if (tp != null && Number(tp) > 0) mkLine(Number(tp), COLORS.up, "TP", true);
    const parts: string[] = [];
    const vin = _voteText(trade.meta);
    const vout = _voteText(trade.exit_meta);
    if (vin) parts.push("ВХОД: " + vin);
    if (vout) parts.push("ВЫХОД: " + vout);
    showVoteNote(parts.join("   |   "));
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
    cdbg("chartOverlay", "entry=" + fmtUTC(entryTime), "exit=" + (exitTime != null ? fmtUTC(exitTime) : "-"), "side=" + side);
    if (exitTime != null) {
      const ef = Number(entryTime), et = Number(exitTime);
      try {
        const all = refs.chart.timeScale().getVisibleRange();
        cdbg("chartOverlay-range", "before=" + (all ? `${Math.floor(Number(all.from))}..${Math.floor(Number(all.to))}` : "null"));
      } catch { /* noop */ }
      refs.chart.timeScale().setVisibleRange({ from: ef as UTCTimestamp, to: et as UTCTimestamp });
      cdbgView("chartOverlay-range after");
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
      const created = r.created_at ? new Date(r.created_at).toLocaleString("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
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
  } catch { /* noop */ }

  let dragging = false;
  let startX = 0;
  let startW = 0;

  const onMove = (e: PointerEvent) => {
    if (!dragging) return;
    const w = Math.max(190, Math.min(560, startW + (e.clientX - startX)));
    sb.style.width = `${w}px`;
    sb.style.flexBasis = `${w}px`;
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
  const grip = document.querySelector(".sidebar-grip") as HTMLElement | null;
  const target = grip ?? sb;
  target.addEventListener("pointerdown", (e) => {
    const rect = sb.getBoundingClientRect();
    if (!grip && e.clientX < rect.right - 8) return;
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    startW = rect.width;
    document.body.classList.add("sb-resizing");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
}

initSidebarResize();
if (IS_EMBEDDED) {
  try {
    const ov = document.createElement("div");
    ov.id = "cdbg-ov";
    ov.style.cssText = "position:fixed;left:8px;bottom:8px;z-index:99999;background:rgba(10,12,20,0.82);color:#7fd1a5;font:10px/1.35 'Menlo',monospace;padding:4px 6px;border-radius:4px;max-width:46vw;pointer-events:none;white-space:pre-wrap;";
    document.body.appendChild(ov);
    const upd = () => {
      ov.textContent = "#" + _cdbgCount + " • " + (__CDBG.length ? __CDBG.slice(-4).join("\n") : "(пусто)");
    };
    if ((window as any).__CDBG) {
      const iv = setInterval(upd, 1000);
      (window as any).__cdbgUpd = upd;
      void iv;
    }
  } catch { /* noop */ }
  let curFigi = FIGI;
  let _liveFigi = "";
  let _lastFocusMks: SeriesMarker<Time>[] = [];
  let _lastFocusTrade: Record<string, unknown> | null = null;
  let _lastTradeSig = "";
  const _tradeSigOf = (t: Record<string, unknown>) => `${String(t.entry_time ?? "")}|${String(t.entry_price ?? "")}|${String(t.stop_loss ?? "")}|${String(t.take_profit ?? "")}`;
  const syncBtn = document.getElementById("btn-sync");
  if (syncBtn) {
    syncBtn.addEventListener("click", () => {
      void (async () => {
        try {
          syncBtn.classList.add("busy");
          syncBtn.textContent = "⟳ Докачка…";
          const r = await fetch(`${API}/api/candles/${encodeURIComponent(FIGI)}/backfill?days=35&workers=4`, { method: "POST" });
          const rep = r.ok ? await r.json() : null;
          const nd = await fetchAnalysis(FIGI, currentTf.interval, 2000);
          if (nd && nd.candles && nd.candles.length) renderData(nd, true);
          setStatus(rep ? `докачка: +${rep.total_added ?? 0} свечей` : "докачка: ошибка");
        } catch { /* noop */ } finally {
          syncBtn.classList.remove("busy");
          syncBtn.textContent = "⟳ Данные";
        }
      })();
    });
  }

  // auto-refresh: свежие свечи каждые 8с, правая кромка прилипает к now
  window.setInterval(() => {
    void (async () => {
      if (!_liveFigi) return;
      const wantFigi = _liveFigi;
      try {
        const nd = await fetchAnalysis(wantFigi, currentTf.interval, windowLimit(currentTf.interval));
        if (!nd || nd.figi !== wantFigi) {
          cdbg("auto", "figi mismatch — SKIP (" + (nd?.figi ?? "none") + ")");
          return;
        }
        if (nd && nd.candles && nd.candles.length) {
          analysis = nd;
          renderData(nd, true);
          if (_lastFocusMks.length) refs?.markers.setMarkers(_lastFocusMks);
          if (_lastFocusTrade && _lastTradeSig !== _tradeSigOf(_lastFocusTrade)) {
            _lastTradeSig = _tradeSigOf(_lastFocusTrade);
            window.__setTradeLines?.(_lastFocusTrade);
          }
          try {
            const lastT = nd.candles[nd.candles.length - 1].ts;
            const lastSec = new Date(lastT).getTime() / 1000;
            const age = Date.now() / 1000 - lastSec;
            const lg = refs?.chart.timeScale().getVisibleLogicalRange();
            const bars = nd.candles.length;
            const pinned = lg != null && lg.to >= bars - 5 && lg.to >= 0;
            const broken = lg == null || lg.from < -2 || lg.to < -2 || lg.to >= bars + 50 || lg.from >= bars + 50;
            cdbg("auto", "age=" + Math.round(age) + "s", "pinned=" + pinned, "lg.to=" + (lg ? lg.to.toFixed(1) : "null"), "bars=" + bars, "broken=" + broken);
            if (broken) {
              cdbg("auto", "broken view — reset к последним " + (WINDOW_BARS[currentTf.interval] ?? 60) + " барам");
              const want = WINDOW_BARS[currentTf.interval] ?? 60;
              const lastIdx = Math.max(0, bars - 1);
              const fromIdx = Math.max(0, lastIdx - want + 1);
              refs?.chart.timeScale().setVisibleLogicalRange({ from: fromIdx, to: lastIdx });
              cdbgView("auto-broken-reset");
            } else if (age < 180 && pinned) {
              const before = refs?.chart.timeScale().getVisibleLogicalRange();
              refs?.chart.timeScale().scrollToRealTime();
              const after = refs?.chart.timeScale().getVisibleLogicalRange();
              cdbg("auto", "scrollToRealTime", "before=" + (before ? `${before.from.toFixed(1)}..${before.to.toFixed(1)}` : "null"), "after=" + (after ? `${after.from.toFixed(1)}..${after.to.toFixed(1)}` : "null"));
            }
          } catch { /* noop */ }
          cdbgView("auto-end");
        } else {
          cdbg("auto", "пустые данные — НЕ перерисовываю (сохраняю текущий график)");
        }
      } catch { /* noop */ }
    })();
  }, 8000);
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
          _liveFigi = f;
          let data = await fetchAnalysis(f, currentTf.interval, windowLimit(currentTf.interval));
          if (!(data && data.candles && data.candles.length)) {
            try {
              await syncCandles(f, currentTf.interval, currentTf.days);
            } catch { /* noop */ }
            data = await fetchAnalysis(f, currentTf.interval, windowLimit(currentTf.interval));
          }
          if (data && data.candles && data.candles.length) {
            renderData(data);
            setStatus(`${data.ticker ?? d.ticker ?? f} — ${data.candles.length} свечей`);
          } else {
            setStatus("нет свечей в базе для " + (d.ticker || f));
          }
        } else if (f === curFigi) {
          // тот же инструмент — тихо обновляем свечи, не трогая масштаб
          try {
            const nd = await fetchAnalysis(f, currentTf.interval, windowLimit(currentTf.interval));
            if (nd && nd.candles && nd.candles.length) {
              analysis = nd;
              renderData(nd, true);
            }
          } catch { /* noop */ }
        }
        if (refs) {
          const hist = ((d as { trades?: unknown[] }).trades || []) as Record<string, unknown>[];
          const mks: SeriesMarker<Time>[] = [];
          for (const tr of hist) {
            const et = tr.entry_time ? toUnix(String(tr.entry_time)) : null;
            const xt = tr.ts ? toUnix(String(tr.ts)) : null;
            const isL = String(tr.side) === "LONG";
            if (et != null && !isNaN(Number(et))) mks.push({ time: et, position: isL ? "belowBar" : "aboveBar", color: COLORS.up, shape: "arrowUp", text: String(tr.qty ?? "") });
            if (xt != null && tr.exit_price != null && !isNaN(Number(xt))) mks.push({ time: xt, position: isL ? "aboveBar" : "belowBar", color: COLORS.down, shape: "arrowDown", text: "" });
          }
          _lastFocusMks = mks;
          refs.markers.setMarkers(mks);
          const fHist = (d as unknown as { trades?: unknown[] }).trades ?? [];
          cdbg("focus", "figi=" + f, "curFigi=" + curFigi, "trades=" + fHist.length, "mks=" + mks.length, "hasTrade=" + (d.trade ? "yes" : "no"));
          if (d.trade) {
            _lastFocusTrade = d.trade as Record<string, unknown>;
            _lastTradeSig = _tradeSigOf(_lastFocusTrade);
            window.__setTradeLines?.(d.trade);
            cdbgView("focus-trade");
          }
        }
      } catch (e) {
        console.warn("embed focus error", e);
      }
    })();
  });
  const autof = new URLSearchParams(window.location.search).get("autofocus");
  if (autof) {
    cdbg("AUTOFOCUS", autof);
    const send = (msg: Record<string, unknown>) => window.dispatchEvent(new MessageEvent("message", { data: msg }));
    send({ type: "focus", figi: autof, ticker: autof, trade: { side: "LONG", entry_time: "2026-09-08T03:00:00+00:00", entry_price: 100, stop_loss: 98, take_profit: 104 }, trades: [
      { side: "LONG", entry_time: "2026-09-08T03:00:00+00:00", ts: "2026-09-08T03:30:00+00:00", entry_price: 100, exit_price: 103 },
    ] });
    setTimeout(() => send({ type: "focus", figi: "BBG004730RP0", ticker: "GAZP", trade: null, trades: [] }), 6000);
    setTimeout(() => send({ type: "focus", figi: "BBG004730N88", ticker: "SBER", trade: { side: "SHORT", entry_time: "2026-09-08T03:10:00+00:00", entry_price: 250, stop_loss: 255, take_profit: 240 }, trades: [
      { side: "SHORT", entry_time: "2026-09-08T03:10:00+00:00", ts: "2026-09-08T03:40:00+00:00", entry_price: 250, exit_price: 244 },
    ] }), 12000);
    let snap = 0;
    const iv = setInterval(() => {
      snap += 1;
      try {
        const lg = refs?.chart.timeScale().getVisibleLogicalRange();
        const vr = refs?.chart.timeScale().getVisibleRange();
        const pr = refs?.chart.priceScale?.(COLORS.down ? "right" : "right")?.getVisibleRange?.();
        cdbg("SNAP", `#${snap}`, "lg=" + (lg ? `${lg.from.toFixed(1)}..${lg.to.toFixed(1)}` : "null"),
          "vr=" + (vr ? fmtUTC(Math.floor(vr.from as number)) + ".." + fmtUTC(Math.floor(vr.to as number)) : "null"),
          "pr=" + (pr ? pr.from.toFixed(2) + ".." + pr.to.toFixed(2) : "null"),
          "bars=" + (refs?.chart.timeScale().getVisibleLogicalRange() ? analysis?.candles?.length ?? 0 : 0));
      } catch { cdbg("SNAP", `#${snap}`, "err"); }
    }, 3000);
    setTimeout(() => clearInterval(iv), 45000);
  }
}

initNav();
void initLab();
if (!IS_EMBEDDED) void initBot();
void initTest();
void initEnsLab();
