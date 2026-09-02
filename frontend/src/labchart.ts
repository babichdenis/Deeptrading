import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  type CandlestickData,
  type IChartApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { IPrimitivePaneView } from "lightweight-charts";

export interface LabTrade {
  side: string;
  mode?: string;
  entry_time: string;
  entry_price: number;
  exit_time: string;
  exit_price: number;
  net_pnl: number;
  gross_pnl?: number;
  commission?: number;
  exit_reason?: string;
  entry_votes?: number | null;
  entry_members?: string[] | null;
}

interface ZoneRect {
  timeFrom: UTCTimestamp;
  timeTo: UTCTimestamp;
  win: boolean | null;
}

class ZonesPaneView implements IPrimitivePaneView {
  private source: ZonesPrimitive;
  constructor(source: ZonesPrimitive) {
    this.source = source;
  }
  update() {}
  renderer() {
    const src = this.source;
    return {
      draw: (target: any) => {
        target.useMediaCoordinateSpace((scope: any) => {
          const ctx = scope.context;
          const ts = src.chart.timeScale();
          for (const z of src.zones) {
            const x1 = ts.timeToCoordinate(z.timeFrom as Time);
            const x2 = ts.timeToCoordinate(z.timeTo as Time);
            if (x1 === null && x2 === null) continue;
            const left = x1 ?? 0;
            const right = x2 ?? scope.mediaSize.width;
            // приглушённые: зелёный плюс, красный минус, жёлтый открытая
            ctx.fillStyle = z.win == null
              ? "rgba(255,193,7,0.10)"
              : z.win ? "rgba(38,166,154,0.10)" : "rgba(239,83,80,0.12)";
            ctx.fillRect(left, 0, Math.max(2, right - left), scope.mediaSize.height);
          }
        });
      },
    };
  }
}

export class ZonesPrimitive {
  zones: ZoneRect[] = [];
  chart: IChartApi;
  _views: IPrimitivePaneView[];
  constructor(chart: IChartApi, zones: ZoneRect[]) {
    this.chart = chart;
    this.zones = zones;
    this._views = [new ZonesPaneView(this)];
  }
  paneViews() {
    return this._views;
  }
  updateAllViews() {}
}

export function toUnix(iso: unknown): UTCTimestamp {
  return Math.floor(new Date(String(iso)).getTime() / 1000) as UTCTimestamp;
}

export interface LabChartHandle {
  destroy: () => void;
  focusRange: (fromTs: string, toTs: string) => void;
}

export interface LabChartOptions {
  analysis?: {
    sma20?: Array<number | null>;
    ema50?: Array<number | null>;
    bb_upper?: Array<number | null>;
    bb_lower?: Array<number | null>;
    rsi?: Array<number | null>;
    macd?: { macd: Array<number | null>; signal: Array<number | null>; hist: Array<number | null> };
  } | null;
}

export function createLabChart(
  container: HTMLElement,
  candlesJson: Array<{ ts: string; open: number; high: number; low: number; close: number; volume: number }>,
  trades: LabTrade[],
  opts: LabChartOptions = {},
): LabChartHandle {
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

  const sortedCandles = [...candlesJson].sort((a, b) => toUnix(a.ts) - toUnix(b.ts));
  const data: CandlestickData[] = sortedCandles.map((c) => ({
    time: toUnix(c.ts),
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
  }));
  candleSeries.setData(data);

  // индикаторы (из анализа акции) — SMA/EMA/BB как на Складе
  const a = opts.analysis;
  if (a) {
    const line = (color: string, arr?: Array<number | null>, width = 1) => {
      if (!arr) return;
      const s = chart.addSeries(LineSeries, { color, lineWidth: width as never, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      const pts = sortedCandles
        .map((c, i) => ({ time: toUnix(c.ts), value: arr[i] }))
        .filter((p) => p.value != null && Number.isFinite(p.value));
      if (pts.length) s.setData(pts as never[]);
    };
    line("#4a90e2", a.sma20, 2);
    line("#e6a23c", a.ema50, 2);
    line("rgba(155,89,182,0.8)", a.bb_upper, 1);
    line("rgba(155,89,182,0.8)", a.bb_lower, 1);
    // нижняя панель: MACD и/или RSI
    const hasMacd = !!a.macd?.macd?.some((v) => v != null);
    const hasRsi = !!a.rsi?.some((v) => v != null);
    if (hasMacd || hasRsi) {
      const oscPts = sortedCandles.map((c) => toUnix(c.ts));
      if (hasMacd && a.macd) {
        const hist = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, 1);
        hist.setData(a.macd.hist.map((v, i) => ({
          time: oscPts[i], value: v ?? 0,
          color: (v ?? 0) >= 0 ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)",
        })).filter((_p, i) => a.macd!.macd[i] != null) as never[]);
        const m = chart.addSeries(LineSeries, { color: "#2962ff", lineWidth: 2, priceLineVisible: false, lastValueVisible: false }, 1);
        m.setData(a.macd.macd.map((v, i) => ({ time: oscPts[i], value: v })).filter((p) => p.value != null) as never[]);
        const s = chart.addSeries(LineSeries, { color: "#ff9800", lineWidth: 2, priceLineVisible: false, lastValueVisible: false }, 1);
        s.setData(a.macd.signal.map((v, i) => ({ time: oscPts[i], value: v })).filter((p) => p.value != null) as never[]);
      }
      if (hasRsi && a.rsi) {
        const r = chart.addSeries(LineSeries, { color: "#c39bd3", lineWidth: 2, priceLineVisible: false, lastValueVisible: false }, 1);
        r.setData(a.rsi.map((v, i) => ({ time: oscPts[i], value: v })).filter((p) => p.value != null) as never[]);
      }
      try {
        const panes = chart.panes();
        if (panes[1]) { panes[0].setStretchFactor(3); panes[1].setStretchFactor(2); }
      } catch { /* старые версии */ }
    }
  }

  const zones: ZoneRect[] = [];
  const markers: SeriesMarker<Time>[] = [];

  for (const t of trades) {
    const tf = toUnix(t.entry_time);
    const tt = toUnix(t.exit_time);
    const isOpen = !t.exit_time;
    const win = isOpen ? null : Number(t.net_pnl) >= 0;
    zones.push({ timeFrom: tf, timeTo: tt, win: isOpen ? null : Number(t.net_pnl) >= 0 });

    const long = t.side === "LONG";
    const votes = t.entry_votes != null ? `${t.entry_votes}✓` : "";
    markers.push({
      time: tf,
      position: long ? "belowBar" : "aboveBar",
      shape: long ? "arrowUp" : "arrowDown",
      color: long ? "#26a69a" : "#ef5350",
      text: `${long ? "L" : "S"} ${votes}`,
    });
    if (!isOpen) {
      markers.push({
        time: tt,
        position: long ? "aboveBar" : "belowBar",
        shape: "circle",
        color: win ? "#26a69a" : "#ef5350",
        text: String(t.exit_reason ?? "").slice(0, 8) || money(t.net_pnl),
      });
    }
    // стоп и тейк линиями на входе
    const entryPrice = Number(t.entry_price);
    const stop = (t as any).initial_stop != null ? Number((t as any).initial_stop) : null;
    const target = (t as any).take_profit != null ? Number((t as any).take_profit) : null;
    const addLine = (price: number, color: string, title: string) => {
      if (!Number.isFinite(price)) return;
      candleSeries.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title });
    };
    if (stop != null) addLine(stop, "#ef5350", "SL");
    if (target != null) addLine(target, "#26a69a", "TP");
    void entryPrice;
  }

  if (zones.length > 0) {
    candleSeries.attachPrimitive(new ZonesPrimitive(chart, zones));
  }

  const markersApi: ISeriesMarkersPluginApi<Time> = createSeriesMarkers(candleSeries, []);
  markers.sort((a, b) => Number(a.time) - Number(b.time));
  markersApi.setMarkers(markers);

  chart.timeScale().fitContent();

  // панель деталей при наведении — ближайшая сделка по времени
  const infoPanel = document.createElement("div");
  infoPanel.className = "lab-info-panel";
  infoPanel.innerHTML = `<span class="mini-hint">Наведите курсор на сделку…</span>`;
  container.appendChild(infoPanel);

  chart.subscribeCrosshairMove((param) => {
    if (!param.time) return;
    const tSec = Number(param.time);
    let best: LabTrade | null = null;
    let bestDist = Infinity;
    for (const t of trades) {
      const mid = (toUnix(t.entry_time) + toUnix(t.exit_time)) / 2;
      const d = Math.abs(mid - tSec);
      if (d < bestDist) {
        bestDist = d;
        best = t;
      }
    }
    if (!best) return;
    const votes =
      best.entry_votes != null
        ? `Голоса кворума: <b>${best.entry_votes}</b> (${(best.entry_members ?? []).join(", ")})`
        : "";
    infoPanel.innerHTML = `
      <div class="totals-line" style="margin:8px 0">
        <span><b>${best.side}</b> [${best.mode ?? ""}]</span>
        ${votes ? `<span>${votes}</span>` : ""}
        <span>Вход: ${fmt(best.entry_time)} @ ${best.entry_price}</span>
        <span>Выход: ${fmt(best.exit_time)} @ ${best.exit_price} · ${best.exit_reason}</span>
        <span>PnL: <b class="${Number(best.net_pnl) >= 0 ? "pos" : "neg"}">${money(best.net_pnl)} ₽</b></span>
      </div>`;
  });

  function fmt(iso: unknown): string {
    const d = new Date(String(iso));
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  function money(n: number): string {
    return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(n);
  }

  return {
    destroy: () => {
      chart.remove();
      container.innerHTML = "";
    },
    focusRange: (fromTs: string, toTs: string) => {
      const a = toUnix(fromTs);
      const b = toUnix(toTs) || a + 3600;
      const pad = Math.max(60, (b - a) * 0.15);
      try {
        chart.timeScale().setVisibleLogicalRange({ from: a - pad, to: b + pad } as never);
      } catch {
        chart.timeScale().setVisibleRange({ from: a - pad as never, to: b + pad as never });
      }
    },
  };
}
