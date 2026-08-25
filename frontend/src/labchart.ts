import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  createSeriesMarkers,
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
  win: boolean;
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
            ctx.fillStyle = z.win ? "rgba(38,166,154,0.14)" : "rgba(239,83,80,0.16)";
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
}

export function createLabChart(
  container: HTMLElement,
  candlesJson: Array<{ ts: string; open: number; high: number; low: number; close: number; volume: number }>,
  trades: LabTrade[],
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

  const zones: ZoneRect[] = [];
  const markers: SeriesMarker<Time>[] = [];

  for (const t of trades) {
    const tf = toUnix(t.entry_time);
    const tt = toUnix(t.exit_time);
    zones.push({ timeFrom: tf, timeTo: tt, win: Number(t.net_pnl) >= 0 });

    const win = Number(t.net_pnl) >= 0;
    const long = t.side === "LONG";
    const votes = t.entry_votes != null ? `${t.entry_votes}✓` : "";
    markers.push({
      time: tf,
      position: long ? "belowBar" : "aboveBar",
      shape: long ? "arrowUp" : "arrowDown",
      color: long ? "#26a69a" : "#ef5350",
      text: `${long ? "L" : "S"} ${votes}`,
    });
    markers.push({
      time: tt,
      position: long ? "aboveBar" : "belowBar",
      shape: "circle",
      color: win ? "#26a69a" : "#ef5350",
      text: String(t.exit_reason ?? "").slice(0, 8) || money(t.net_pnl),
    });
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
  };
}
