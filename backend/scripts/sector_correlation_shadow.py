"""SECTOR CORRELATION SHADOW (IMOEX-only, read-only). Быстрый анализ на 5 FIGI.

Для каждой бумаги (RUAL, SNGSP, AFLT, MVID, NLMK):
  corr(stock_return_5m, IMOEX_return_5m), corr(stock_return_1m, IMOEX_return_1m)
  lead-lag corr на лагах 0/1/2/3/5 минут + directional accuracy (sign factor == sign stock+lag).
Сектора: SNGSP(oil), NLMK+RUAL(metals), AFLT+MVID(consumer→IMOEX).
Артефакты: reports/5b44f3b383df/sector_correlation_imoex.json + sector_correlation_methodology.md
Запуск: локально.
"""
import sys, os, json, csv, statistics, asyncio, threading
from datetime import datetime, timezone

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")
IMOEX = "BBG00KDWPPW2"
T_FROM = datetime(2026, 7, 1, tzinfo=timezone.utc)
T_TO = datetime(2026, 8, 1, tzinfo=timezone.utc)
FIGIS = {"BBG008F2T3T2": ("RUAL", "Metals"), "BBG004S681B4": ("NLMK", "Metals"),
         "BBG004S681M2": ("SNGSP", "Oil&Gas"), "BBG004S683W7": ("AFLT", "Consumer"),
         "BBG004S68CP5": ("MVID", "Consumer")}


async def load_candles(figi: str):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    eng = create_async_engine("postgresql+asyncpg://deeptrading:deeptrading@192.168.1.54:5432/deeptrading")
    try:
        async with eng.connect() as c:
            rows = await c.execute(text(
                "SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=1 "
                "AND ts>=:a AND ts<:b ORDER BY ts"),
                {"f": figi, "a": T_FROM, "b": T_TO})
            return [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows.fetchall()]
    finally:
        await eng.dispose()


def load(figi):
    res = {}
    def w():
        res["c"] = asyncio.new_event_loop().run_until_complete(load_candles(figi))
    t = threading.Thread(target=w); t.start(); t.join()
    return res["c"]


def main():
    im = load(IMOEX)
    print(f"IMOEX 1m: {len(im)}", flush=True)
    im1 = [(x[0], x[4]) for x in im]
    ir = [None] * len(im1)
    for i in range(1, len(im1)):
        if im1[i - 1][1]:
            ir[i] = im1[i][1] / im1[i - 1][1] - 1

    result = {"schema": "sector_correlation_imoex_v1", "run_id": "5b44f3b383df",
              "config_hash": "1c7f75dc44c2aa67", "period": "2026-07-01..2026-07-31",
              "note": "SHADOW/read-only; IMOEX-only (Brent/Gold DATA GAP); point-in-time; no oracle",
              "per_stock": {}, "per_sector": {}, "generated_at": datetime.now(timezone.utc).isoformat()}

    sector_corr = {}
    for figi, (ticker, sector) in FIGIS.items():
        s1 = load(figi)
        sr = [None] * len(s1)
        for i in range(1, len(s1)):
            if s1[i - 1][4]:
                sr[i] = s1[i][4] / s1[i - 1][4] - 1
        # маппинг по 1m времени
        pairs = {}
        j = 0
        ts1 = [x[0] for x in s1]
        for i in range(1, len(ts1)):
            while j + 1 < len(im1) and im1[j + 1][0] <= ts1[i]:
                j += 1
            if sr[i] is not None and ir[j] is not None:
                pairs[i] = (ir[j], sr[i])
        # lead-lag corr (лаг в минутах)
        lead = {}
        for lag in (0, 1, 2, 3, 5):
            xs, ys = [], []
            for i, (fv, sv) in pairs.items():
                fi = i - lag
                if fi <= 0 or fi not in pairs:
                    continue
                xs.append(pairs[fi][0]); ys.append(sv)
            if len(xs) > 30:
                mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
                sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys)
                lead[f"lag_{lag}"] = round(sxy / (sxx ** 0.5 * syy ** 0.5), 3) if sxx and syy else None
                # directional accuracy: sign(factor[t]) == sign(stock[t+lag])
                acc = sum(1 for i, (fv, sv) in pairs.items() if (i - lag) in pairs and
                          (fv > 0) == (pairs[i][1] > 0) and fv != 0) / len(xs)
                lead[f"lag_{lag}_acc"] = round(acc, 3)
            else:
                lead[f"lag_{lag}"] = None; lead[f"lag_{lag}_acc"] = None
        result["per_stock"][ticker] = {"sector": sector, "figi": figi, "lead_lag": lead}
        sector_corr.setdefault(sector, []).append(lead.get("lag_0"))
        print(f"  {ticker} ({sector}): lag0={lead.get('lag_0')} acc={lead.get('lag_0_acc')}", flush=True)

    for sector, vals in sector_corr.items():
        present = [v for v in vals if v is not None]
        result["per_sector"][sector] = {"n_stocks": len(vals),
                                        "avg_lag0_corr": round(statistics.mean(present), 3) if present else None}

    with open(os.path.join(RUN_DIR, "sector_correlation_imoex.json"), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    md = ["# SECTOR CORRELATION with IMOEX (SHADOW, IMOEX-only)", "",
          "5 FIGI, July 2026. Brent/Gold/USD-RUB — DATA GAP (не доступны), поэтому только IMOEX-фактор.",
          "Precursor к полному H-036.", "", "## Per-stock lead-lag corr (1m)", "",
          "| stock | sector | lag0 | lag1 | lag2 | lag3 | lag5 | acc@lag0 |", "|---|---|---|---|---|---|---|---|"]
    for ticker, v in result["per_stock"].items():
        ll = v["lead_lag"]
        md.append(f"| {ticker} | {v['sector']} | {ll['lag_0']} | {ll['lag_1']} | {ll['lag_2']} | {ll['lag_3']} | "
                  f"{ll['lag_5']} | {ll['lag_0_acc']} |")
    md += ["", "## Per-sector avg lag0 corr", "", "| sector | avg lag0 |", "|---|---|"]
    for sector, v in result["per_sector"].items():
        md.append(f"| {sector} | {v['avg_lag0_corr']} |")
    md += ["", "## Вывод", "Проверять устойчивость на disjoint 2026-окне. Множественное тестирование "
           "(5 акций × 5 лагов) — требуется Deflated Sharpe."]
    with open(os.path.join(RUN_DIR, "sector_correlation_methodology.md"), "w") as f:
        f.write("\n".join(md))
    print(json.dumps({"per_stock": {k: v["lead_lag"]["lag_0"] for k, v in result["per_stock"].items()},
                      "per_sector": result["per_sector"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
