"""H-036 Phase-2 (v2): Russian macro-regime attribution с реальными факторами.

Устраняет DATA GAP Фазы 1: Brent/Gold/USD-RUB теперь скачаны (data/*_5m.csv, MOEX ISS, 1m->5m).
Атрибуция 517 июльских сделок (5b44f3b383df) к макро-режиму на баре входа.
Методология та же (B5): EMA-slope(A), ADX14(B), linreg-slope(C); strength_norm = raw/rolling vol.
Сектор-факторы (owner): AFLT+MVID->IMOEX(db); SNGSP->Brent(csv); NLMK+RUAL->USD-RUB(csv);
  дополнительно: NLMK+RUAL->Gold(csv) как сенситив металлов (отдельная группа metals_gold).
Артефакты: reports/macro_regime_attribution_v2.{json,md}
SHADOW/read-only (не policy).
"""
import sys, os, json, csv, statistics, bisect, threading, asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
DATA = os.path.join(BACKEND, "data")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")
MSK = ZoneInfo("Europe/Moscow")
IMOEX = "BBG00KDWPPW2"
T_FROM = datetime(2026, 7, 1, tzinfo=timezone.utc)
T_TO = datetime(2026, 8, 1, tzinfo=timezone.utc)

FIGI_SECTOR = {
    "BBG008F2T3T2": ("RUAL", "Metals/USD-RUB"), "BBG004S681B4": ("NLMK", "Metals/USD-RUB"),
    "BBG004S681M2": ("SNGSP", "OilGas/Brent"), "BBG004S683W7": ("AFLT", "IMOEX/risk"),
    "BBG004S68CP5": ("MVID", "IMOEX/risk"),
}
SECTOR_FACTOR = {"IMOEX/risk": "IMOEX", "OilGas/Brent": "BRENT", "Metals/USD-RUB": "USDRUB"}
CSV_FILE = {"BRENT": "brent_5m.csv", "GOLD": "gold_5m.csv", "USDRUB": "usdrub_5m.csv"}
# металлы дополнительно к USD-RUB: Gold-режим
METALS_GOLD = ("BBG008F2T3T2", "BBG004S681B4")


async def load_candles_db(figi: str):
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


def load_candles_csv(name: str):
    """data/{name}_5m.csv: ts_utc;high;low;open;close;volume (MOEX ISS 1m агрегированный в 5m)."""
    path = os.path.join(DATA, CSV_FILE[name])
    out = []
    with open(path) as f:
        for line in f:
            parts = line.strip().rstrip(";").split(";")
            if len(parts) < 6:
                continue
            ts = datetime.fromisoformat(parts[0].replace("Z", "+00:00")).astimezone(timezone.utc)
            if not (T_FROM <= ts < T_TO):
                continue
            h, l, o, c, v = (float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5]))
            out.append((ts, o, h, l, c, v))
    return out


def load_factor(name: str):
    if name == "IMOEX":
        res = {}
        def w():
            res["c"] = asyncio.new_event_loop().run_until_complete(load_candles_db(IMOEX))
        t = threading.Thread(target=w); t.start(); t.join()
        c1 = res["c"]
        # ресемпл 1m -> 5m
        out = []
        for ts, o, h, l, c, v in c1:
            minute = ts.minute - (ts.minute % 5)
            k = ts.replace(minute=minute, second=0, microsecond=0)
            if out and out[-1][0] == k:
                prev = out[-1]
                out[-1] = (k, prev[1], max(prev[2], h), min(prev[3], l), c, prev[5] + v)
            else:
                out.append((k, o, h, l, c, v))
        return out
    return load_candles_csv(name)


def ema(series, period):
    out = []; k = 2 / (period + 1); e = None
    for v in series:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def adx14(highs, lows, closes):
    n = len(closes); out = [None] * n
    if n < 30:
        return out
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    ups = [max(highs[i] - highs[i - 1], 0) for i in range(1, n)]
    dns = [max(lows[i - 1] - lows[i], 0) for i in range(1, n)]
    for i in range(14, n):
        tr14 = sum(trs[i - 13:i + 1])
        if tr14 == 0:
            out[i] = 50.0; continue
        up14, dn14 = sum(ups[i - 13:i + 1]), sum(dns[i - 13:i + 1])
        pdi, mdi = up14 / tr14 * 100, dn14 / tr14 * 100
        out[i] = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
    return out


def build_factor_series(name):
    """Возвращает (ts5, closes, highs, lows, adx, ema_f, ema_s, slopes, vol30)."""
    c5 = load_factor(name)
    closes = [x[4] for x in c5]; highs = [x[2] for x in c5]; lows = [x[3] for x in c5]
    ts5 = [x[0] for x in c5]
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
    vol30 = [None] * len(closes)
    for i in range(30, len(closes)):
        vol30[i] = statistics.pstdev(rets[i - 29:i + 1])
    ema_f, ema_s = ema(closes, 20), ema(closes, 50)
    adx = adx14(highs, lows, closes)
    slopes = [None] * len(closes)
    for i in range(40, len(closes)):
        seg = closes[i - 39:i + 1]
        xs = list(range(40)); mx = 19.5; my = sum(seg) / 40
        num = sum((x - mx) * (y - my) for x, y in zip(xs, seg))
        den = sum((x - mx) ** 2 for x in xs)
        slopes[i] = num / den if den else None
    return ts5, closes, highs, lows, adx, ema_f, ema_s, slopes, vol30


def make_regime_fn(ts5, closes, highs, lows, adx, ema_f, ema_s, slopes, vol30):
    TAU_A, TAU_C = 0.0015, 0.00075
    def regime(j, method):
        if j < 50:
            return None, None
        if method == "A":
            dn = (ema_f[j] - ema_s[j]) / closes[j]
            return ("up_trend" if dn > TAU_A else ("down_trend" if dn < -TAU_A else "flat")), dn
        if method == "B":
            a = adx[j]
            if a is None:
                return None, None
            if a <= 25:
                return "flat", a
            return ("up_trend" if closes[j] > ema_s[j] else "down_trend"), a
        if method == "C":
            s = slopes[j]
            if s is None:
                return None, None
            sn = s / closes[j]
            return ("up_trend" if sn > TAU_C else ("down_trend" if sn < -TAU_C else "flat")), sn
    return regime


def econ(sub):
    n = len(sub)
    if n == 0:
        return {"count": 0}
    net = sum(r["net"] for r in sub)
    gp = sum(max(r["gross"], 0) for r in sub); gl = sum(max(-r["gross"], 0) for r in sub)
    return {"count": n, "net": round(net, 2), "net_per_trade": round(net / n, 2),
            "pf": round(gp / gl, 3) if gl > 0 else None,
            "win_rate": round(sum(1 for r in sub if r["win"]) / n, 4)}


def lead_lag_corr(factor_bars, stock_bars, lags=(0, 1, 2, 3, 5)):
    """factor_bars: (ts, close) пары 5m; stock_bars: (ts, o, h, l, c, v) 5m.
    corr(factor[t-lag], stock[t])."""
    sts = [x[0] for x in stock_bars]; scl = [x[4] for x in stock_bars]
    ts5 = [x[0] for x in factor_bars]; fcl = [x[1] for x in factor_bars]
    sr = [None] * len(sts)
    for i in range(1, len(sts)):
        if scl[i - 1]:
            sr[i] = scl[i] / scl[i - 1] - 1
    fr = [None] * len(ts5)
    for i in range(1, len(ts5)):
        fr[i] = fcl[i] / fcl[i - 1] - 1
    pairs = {}
    j = 0
    for i in range(1, len(sts)):
        while j + 1 < len(ts5) and ts5[j + 1] <= sts[i]:
            j += 1
        if sr[i] is None or fr[j] is None:
            continue
        pairs[i] = (fr[j], sr[i])
    out = {}
    for lag in lags:
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
            out[f"lag_{lag}"] = round(sxy / (sxx ** 0.5 * syy ** 0.5), 3) if sxx and syy else None
        else:
            out[f"lag_{lag}"] = None
    return out


def main():
    print("loading factors ...", flush=True)
    factors = {}
    for name in ("IMOEX", "BRENT", "GOLD", "USDRUB"):
        factors[name] = build_factor_series(name)
        print(f"  {name}: {len(factors[name][0])} 5m баров", flush=True)
    reg_fns = {name: make_regime_fn(*factors[name]) for name in factors}
    idx = {name: factors[name][0] for name in factors}  # ts5

    trades = list(csv.DictReader(open(os.path.join(RUN_DIR, "trades.csv"))))
    rows = []
    for t in trades:
        figi = t["figi"]
        if figi not in FIGI_SECTOR:
            continue
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        ticker, sector = FIGI_SECTOR[figi]
        fname = SECTOR_FACTOR[sector]
        j = bisect.bisect_right(idx[fname], dt) - 1
        for m in ("A", "B", "C"):
            rg, rs = reg_fns[fname](j, m)
            vol30 = factors[fname][8]
            rows.append({"trade_id": t["trade_id"], "ticker": ticker, "sector": sector,
                         "factor": fname, "method": m, "regime": rg,
                         "strength_norm": round(rs / vol30[j], 3) if (rs is not None and vol30[j]) else None,
                         "net": float(t["net_rub"]), "gross": float(t["gross_rub"]),
                         "win": float(t["net_rub"]) > 0,
                         "hold_bars": float(t["hold_bars"]) if t["hold_bars"] else None,
                         "mae_r": float(t["mae_r"]) if t["mae_r"] else None,
                         "exit_reason": t["exit_reason"]})
    # металлы + Gold (доп. сенситив)
    for t in trades:
        figi = t["figi"]
        if figi not in METALS_GOLD:
            continue
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        ticker, sector = FIGI_SECTOR[figi]
        j = bisect.bisect_right(idx["GOLD"], dt) - 1
        for m in ("A", "B", "C"):
            rg, rs = reg_fns["GOLD"](j, m)
            vol30 = factors["GOLD"][8]
            rows.append({"trade_id": t["trade_id"], "ticker": ticker, "sector": "Metals/Gold",
                         "factor": "GOLD", "method": m, "regime": rg,
                         "strength_norm": round(rs / vol30[j], 3) if (rs is not None and vol30[j]) else None,
                         "net": float(t["net_rub"]), "gross": float(t["gross_rub"]),
                         "win": float(t["net_rub"]) > 0, "hold_bars": None, "mae_r": None,
                         "exit_reason": t["exit_reason"]})
    print(f"rows: {len(rows)}", flush=True)

    groups = {}
    for m in ("A", "B", "C"):
        g = {}
        for reg in ("up_trend", "down_trend", "flat"):
            g[f"all_{reg}"] = econ([r for r in rows if r["method"] == m and r["regime"] == reg])
            for sector in ("IMOEX/risk", "OilGas/Brent", "Metals/USD-RUB", "Metals/Gold"):
                g[f"{sector}_{reg}"] = econ([r for r in rows if r["method"] == m and r["sector"] == sector and r["regime"] == reg])
        groups[m] = g

    lead = {}
    stock5 = {}
    for figi, (ticker, sector) in FIGI_SECTOR.items():
        res = {}
        def w():
            res["c"] = asyncio.new_event_loop().run_until_complete(load_candles_db(figi))
        t = threading.Thread(target=w); t.start(); t.join()
        c1 = res["c"]
        out = []
        for ts, o, h, l, c, v in c1:
            minute = ts.minute - (ts.minute % 5)
            k = ts.replace(minute=minute, second=0, microsecond=0)
            if out and out[-1][0] == k:
                prev = out[-1]
                out[-1] = (k, prev[1], max(prev[2], h), min(prev[3], l), c, prev[5] + v)
            else:
                out.append((k, o, h, l, c, v))
        stock5[figi] = out
        fname2 = SECTOR_FACTOR[sector]
        fac_ts = factors[fname2][0]; fac_cl = factors[fname2][1]
        lead[ticker] = {"factor": fname2}
        lead[ticker].update(lead_lag_corr([(fac_ts[i], fac_cl[i]) for i in range(len(fac_ts))], out))
    # металлы + Gold lead-lag
    for figi, (ticker, sector) in FIGI_SECTOR.items():
        if figi in METALS_GOLD:
            fg_ts = factors["GOLD"][0]; fg_cl = factors["GOLD"][1]
            lead[f"{ticker}_gold"] = {"factor": "GOLD"}
            lead[f"{ticker}_gold"].update(lead_lag_corr([(fg_ts[i], fg_cl[i]) for i in range(len(fg_ts))], stock5[figi]))

    report = {"schema": "macro_regime_attribution_v2", "run_id": "5b44f3b383df",
              "config_hash": "1c7f75dc44c2aa67", "period": "2026-07-01..2026-07-31",
              "note": "SHADOW/read-only; real factors (Brent/Gold/USD-RUB из data/*_5m.csv, MOEX ISS 1m->5m); "
                      "устранён DATA GAP Фазы 1; multiple-testing не скорректирован",
              "data_gaps": {},
              "sector_factors": FIGI_SECTOR, "groups": groups, "lead_lag": lead,
              "generated_at": datetime.now(timezone.utc).isoformat()}
    with open(os.path.join(REPORTS, "macro_regime_attribution_v2.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    md = ["# RUSSIAN MACRO-REGIME ATTRIBUTION v2 (H-036, SHADOW, реальные факторы)", "",
          "Фаза 2: Brent/Gold/USD-RUB скачаны (MOEX ISS, 5m) — DATA GAP устранён. 517 сделок July 2026.",
          "Метод B = ADX (preferred из B5).", "",
          "## Regime attribution (метод B)", "",
          "| группа | фактор | n | net | net/t | PF | win |", "|---|---|---|---|---|---|---|"]
    for reg in ("up_trend", "down_trend", "flat"):
        g = groups["B"][f"all_{reg}"]
        md.append(f"| все | {g['count']} | {g['net']} | {g['net_per_trade']} | {g['pf']} | {g['win_rate']} |")
    for sector, fac in (("IMOEX/risk", "IMOEX"), ("OilGas/Brent", "BRENT"), ("Metals/USD-RUB", "USDRUB"), ("Metals/Gold", "GOLD")):
        md += ["", f"## {sector} (фактор {fac})", "", "| regime | n | net/t | PF | win |", "|---|---|---|---|---|"]
        for reg in ("up_trend", "down_trend", "flat"):
            g = groups["B"][f"{sector}_{reg}"]
            md.append(f"| {reg} | {g['count']} | {g['net_per_trade']} | {g['pf']} | {g['win_rate']} |")
    md += ["", "## Lead-lag corr (factor[t-lag] vs stock[t], 5m)", "", "| ticker | factor | lag0 | lag1 | lag2 | lag3 | lag5 |", "|---|---|---|---|---|---|---|"]
    for ticker, v in lead.items():
        md.append(f"| {ticker} | {v.get('factor', '?')} | {v.get('lag_0')} | {v.get('lag_1')} | "
                  f"{v.get('lag_2')} | {v.get('lag_3')} | {v.get('lag_5')} |")
    md += ["", "## Множественное тестирование", "не скорректировано; только July; выводы ограничены "
           "(нужны Deflated Sharpe H-026 + purging/embargo H-027 на расширении окна)."]
    with open(os.path.join(REPORTS, "macro_regime_attribution_v2.md"), "w") as f:
        f.write("\n".join(md))
    print(json.dumps({"rows": len(rows),
                      "B_all": {r: groups["B"][f"all_{r}"]["net_per_trade"] for r in ("up_trend", "down_trend", "flat")},
                      "B_sector": {s: {r: groups["B"][f"{s}_{r}"]["net_per_trade"] for r in ("up_trend", "down_trend", "flat")}
                                   for s in ("IMOEX/risk", "OilGas/Brent", "Metals/USD-RUB", "Metals/Gold")}},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
