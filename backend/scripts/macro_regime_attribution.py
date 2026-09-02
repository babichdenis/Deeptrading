"""H-036 Russian macro-regime attribution (SHADOW, read-only). Фаза 1 (быстрый shadow).

Атрибуция 517 июльских сделок (5b44f3b383df) к IMOEX-режиму на баре входа
(up/down/flat; методология B5: EMA-slope, ADX, linreg; strength_norm = raw/rolling vol).
Плюс lead-lag: corr(factor[t-lag], stock[t]) lag 0/1/2/3/5 5m-баров.
Brent/Gold отсутствуют в БД — помечены DATA GAP (blocker Фазы 1).
Сектора (owner): AFLT+MVID→IMOEX; SNGSP→Brent(gap); NLMK+RUAL→USD-RUB(gap).
Артефакты: reports/macro_regime_attribution.{json,md}
Запуск: локально (с этой машины), НЕ на .54.
"""
import sys, os, json, csv, statistics, asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(BACKEND, "reports")
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
SECTOR_FACTOR = {"IMOEX/risk": IMOEX, "OilGas/Brent": None, "Metals/USD-RUB": None}


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
            out = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows.fetchall()]
            return out
    finally:
        await eng.dispose()


def resample_5m(c1):
    out = []
    for ts, o, h, l, c, v in c1:
        key = (ts - timezone.utc.utcoffset(ts)).replace(second=0, microsecond=0) if False else ts
        minute = ts.minute - (ts.minute % 5)
        k = ts.replace(minute=minute, second=0, microsecond=0)
        if out and out[-1][0] == k:
            prev = out[-1]
            out[-1] = (k, prev[1], max(prev[2], h), min(prev[3], l), c, prev[5] + v)
        else:
            out.append((k, o, h, l, c, v))
    return out


def ema(series, period):
    out = []
    k = 2 / (period + 1)
    e = None
    for v in series:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def adx14(highs, lows, closes):
    n = len(closes)
    out = [None] * n
    if n < 30:
        return out
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    ups = [max(highs[i] - highs[i - 1], 0) for i in range(1, n)]
    dns = [max(lows[i - 1] - lows[i], 0) for i in range(1, n)]
    for i in range(14, n):
        tr14 = sum(trs[i - 13:i + 1])
        if tr14 == 0:
            out[i] = 50.0
            continue
        up14, dn14 = sum(ups[i - 13:i + 1]), sum(dns[i - 13:i + 1])
        pdi, mdi = up14 / tr14 * 100, dn14 / tr14 * 100
        out[i] = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
    return out


def main():
    print("loading IMOEX ...", flush=True)
    import threading
    res = {}
    def worker():
        res["c"] = asyncio.new_event_loop().run_until_complete(load_candles(IMOEX))
    t = threading.Thread(target=worker); t.start(); t.join()
    c1 = res["c"]
    print(f"IMOEX 1m: {len(c1)}", flush=True)
    c5 = resample_5m(c1)
    closes = [x[4] for x in c5]; highs = [x[2] for x in c5]; lows = [x[3] for x in c5]
    ts5 = [x[0] for x in c5]
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
    vol30 = [None] * len(closes)
    for i in range(30, len(closes)):
        vol30[i] = statistics.pstdev(rets[i - 29:i + 1])
    ema_f, ema_s = ema(closes, 20), ema(closes, 50)
    adx = adx14(highs, lows, closes)
    # linreg slope 40
    slopes = [None] * len(closes)
    for i in range(40, len(closes)):
        seg = closes[i - 39:i + 1]
        xs = list(range(40)); mx = 19.5; my = sum(seg) / 40
        num = sum((x - mx) * (y - my) for x, y in zip(xs, seg))
        den = sum((x - mx) ** 2 for x in xs)
        slopes[i] = num / den if den else None

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

    trades = list(csv.DictReader(open(os.path.join(RUN_DIR, "trades.csv"))))
    import bisect
    rows = []
    for t in trades:
        figi = t["figi"]
        if figi not in FIGI_SECTOR:
            continue
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        j = bisect.bisect_right(ts5, dt) - 1
        if j < 50:
            continue
        ticker, sector = FIGI_SECTOR[figi]
        for m in ("A", "B", "C"):
            rg, rs = regime(j, m)
            rows.append({"trade_id": t["trade_id"], "ticker": ticker, "sector": sector,
                         "method": m, "regime": rg,
                         "strength_norm": round(rs / vol30[j], 3) if (rs is not None and vol30[j]) else None,
                         "net": float(t["net_rub"]), "gross": float(t["gross_rub"]),
                         "win": float(t["net_rub"]) > 0,
                         "hold_bars": float(t["hold_bars"]) if t["hold_bars"] else None,
                         "mae_r": float(t["mae_r"]) if t["mae_r"] else None,
                         "exit_reason": t["exit_reason"]})
    print(f"rows: {len(rows)}", flush=True)

    def econ(sub):
        n = len(sub)
        if n == 0:
            return {"count": 0}
        net = sum(r["net"] for r in sub)
        gp = sum(max(r["gross"], 0) for r in sub); gl = sum(max(-r["gross"], 0) for r in sub)
        return {"count": n, "net": round(net, 2), "net_per_trade": round(net / n, 2),
                "pf": round(gp / gl, 3) if gl > 0 else None,
                "win_rate": round(sum(1 for r in sub if r["win"]) / n, 4)}

    groups = {}
    for m in ("A", "B", "C"):
        g = {}
        for reg in ("up_trend", "down_trend", "flat"):
            g[f"all_{reg}"] = econ([r for r in rows if r["method"] == m and r["regime"] == reg])
            for sector in ("IMOEX/risk", "OilGas/Brent", "Metals/USD-RUB"):
                g[f"{sector}_{reg}"] = econ([r for r in rows if r["method"] == m and r["sector"] == sector and r["regime"] == reg])
        groups[m] = g

    # lead-lag: corr(factor[t-lag], stock[t]) для 5 акций (stock 5m rets vs IMOEX 5m rets)
    lead = {}
    for figi, (ticker, sector) in FIGI_SECTOR.items():
        if SECTOR_FACTOR[sector] is None:
            lead[ticker] = {"factor": f"{sector} (DATA GAP)"}
            continue
        import threading as _th
        res2 = {}
        def w2():
            res2["c"] = asyncio.new_event_loop().run_until_complete(load_candles(figi))
        tt = _th.Thread(target=w2); tt.start(); tt.join()
        s5 = resample_5m(res2["c"])
        scl = [x[4] for x in s5]; sts = [x[0] for x in s5]
        # выравнивание по 5m барам (общие индексы)
        sr = [None] * len(sts)
        for i in range(1, len(sts)):
            if scl[i - 1]:
                sr[i] = scl[i] / scl[i - 1] - 1
        ir = [None] * len(ts5)
        for i in range(1, len(ts5)):
            ir[i] = closes[i] / closes[i - 1] - 1
        # маппинг по времени: для каждого бара акции ищем IMOEX бар с ts <= sts[i]
        pairs = {}
        j = 0
        for i in range(1, len(sts)):
            while j + 1 < len(ts5) and ts5[j + 1] <= sts[i]:
                j += 1
            if sr[i] is None or ir[j] is None:
                continue
            pairs[i] = (ir[j], sr[i])
        lead[ticker] = {}
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
                corr = sxy / (sxx ** 0.5 * syy ** 0.5) if sxx and syy else None
                lead[ticker][f"lag_{lag}"] = round(corr, 3)
            else:
                lead[ticker][f"lag_{lag}"] = None

    report = {"schema": "macro_regime_attribution_v1", "run_id": "5b44f3b383df",
              "config_hash": "1c7f75dc44c2aa67", "period": "2026-07-01..2026-07-31",
              "note": "SHADOW/read-only; IMOEX-regime на баре входа (B5 методология); "
                      "Brent/Gold/USD-RUB отсутствуют в БД -> DATA GAP (blocker Фазы 1); multiple-testing не коррекция",
              "data_gaps": {"Brent": "нет в БД (blocker для SNGSP)", "Gold": "нет в БД (blocker для металлов)",
                            "USD_RUB": "нет в БД (proxy для NLMK/RUAL)"},
              "sector_factors": FIGI_SECTOR, "groups": groups, "lead_lag": lead,
              "generated_at": datetime.now(timezone.utc).isoformat()}
    with open(os.path.join(REPORTS, "macro_regime_attribution.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    md = ["# RUSSIAN MACRO-REGIME ATTRIBUTION (H-036, SHADOW)", "",
          "Фаза 1 (быстрый shadow): IMOEX-режим на баре входа для 517 сделок July 2026.", "",
          "## Regime attribution (метод B = ADX, preferred из B5)", "",
          "| группа | n | net | net/t | PF | win |", "|---|---|---|---|---|---|"]
    for reg in ("up_trend", "down_trend", "flat"):
        g = groups["B"][f"all_{reg}"]
        md.append(f"| все {reg} | {g['count']} | {g['net']} | {g['net_per_trade']} | {g['pf']} | {g['win_rate']} |")
    md += ["", "## По секторам (IMOEX/risk: AFLT, MVID)", "", "| regime | n | net/t | win |", "|---|---|---|---|"]
    for reg in ("up_trend", "down_trend", "flat"):
        g = groups["B"][f"IMOEX/risk_{reg}"]
        md.append(f"| {reg} | {g['count']} | {g['net_per_trade']} | {g['win_rate']} |")
    md += ["", "## Lead-lag corr (factor[t-lag] vs stock[t], 5m)", "", "| ticker | factor | lag0 | lag1 | lag2 | lag3 | lag5 |", "|---|---|---|---|---|---|---|"]
    for ticker, v in lead.items():
        md.append(f"| {ticker} | {v.get('factor', 'IMOEX')} | {v.get('lag_0')} | {v.get('lag_1')} | "
                  f"{v.get('lag_2')} | {v.get('lag_3')} | {v.get('lag_5')} |")
    md += ["", "## DATA GAP / blockers", "Brent, Gold, USD-RUB отсутствуют в БД (см. JSON data_gaps).",
           "Полная Фаза 1 (Brent для SNGSP, Gold/USD-RUB для металлов) — блокирована до скачивания данных.",
           "", "## Множественное тестирование", "10 имён × факторы × лаги × режимы — требуются Deflated Sharpe "
           "(H-026) + purging/embargo (H-027). Здесь только July, выводы ограничены."]
    with open(os.path.join(REPORTS, "macro_regime_attribution.md"), "w") as f:
        f.write("\n".join(md))
    print(json.dumps({"rows": len(rows), "lead": {k: v for k, v in lead.items()},
                      "B_all": {r: groups["B"][f"all_{r}"]["net_per_trade"] for r in ("up_trend", "down_trend", "flat")}},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
