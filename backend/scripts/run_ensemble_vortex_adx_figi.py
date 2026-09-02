"""H-077 v3 ЧАСТЬ 2 (per-figi sharded): canonical ensemble + Vortex + ADX on OOS 2025.

Запуск: scripts/run_ensemble_vortex_adx_figi.py <FIGI>
Дробит OOS 2025 на месяцы, считает compute_ensemble для одного figi (последовательно),
размечает Vortex/ADX, пишет reports/_ens_partial_{sym}.json.
Конфиг canonical не меняется -> config_hash совпадает (HARD CONSTRAINT).
"""
import sys, json, math
from bisect import bisect_right
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "/Users/Denis/Dev/Deeptrading/backend")

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
import asyncio
from app.services.ensemble import compute_ensemble
from app.services.research_pack import _load_candles
from app.config import get_settings

BASE = dict(
    bias_mode="info", bias=dict(tf="hour", period=50),
    entry_tf="5min", entry=dict(tf="5min", lookback=1),
    entry_session="main", quorum=2, same_side_reentry_cooldown_bars=15,
    carry_overnight=True, opposite_hold=False,
    exit_policy=dict(id="atr_stop", params=dict(period=14, multiplier=2.0, risk_reward=2.0)),
    commission_rate=0.0005, slippage_bps=2.0, capital=10000.0, lot=10,
    use_all_setups=True, drop_useless=True,
)

def aggregate_5m(raw):
    buckets = {}
    for ts, o, h, l, c, v in raw:
        b = ts - timedelta(minutes=ts.minute % 5, seconds=ts.second, microseconds=ts.microsecond)
        buckets[b] = [o, h, l, c, v] if b not in buckets else [buckets[b][0], max(buckets[b][1], h), min(buckets[b][2], l), c, buckets[b][4] + v]
    return [(b,) + tuple(buckets[b]) for b in sorted(buckets)]

def load_stock(figi, a, b, eng_url):
    async def _do():
        eng = create_async_engine(eng_url)
        try:
            async with eng.connect() as c:
                r = await c.execute(text("SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=5 AND ts>=:a AND ts<:b ORDER BY ts"), {"f": figi, "a": a, "b": b})
                rows = r.fetchall()
                if len(rows) > 1000:
                    return [(x[0].replace(tzinfo=timezone.utc), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows]
                r = await c.execute(text("SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"), {"f": figi, "a": a, "b": b})
                raw = [(x[0].replace(tzinfo=timezone.utc), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in r.fetchall()]
                return aggregate_5m(raw)
        finally:
            await eng.dispose()
    return asyncio.new_event_loop().run_until_complete(_do())

def vortex(h, l, c, p=14):
    ph = np.array(h, float); pl = np.array(l, float); pc = np.array(c, float)
    tr = np.maximum.reduce([np.abs(ph[1:] - pc[:-1]), np.abs(pl[1:] - pc[:-1]), ph[1:] - pl[1:]])
    up = np.abs(ph[1:] - pl[:-1]); dn = np.abs(pl[1:] - ph[:-1])
    vip = np.full(len(pc), np.nan); vim = np.full(len(pc), np.nan)
    for i in range(p, len(pc)):
        trs = tr[i - p:i].sum(); ups = up[i - p:i].sum(); dns = dn[i - p:i].sum()
        if trs > 0:
            vip[i] = ups / trs; vim[i] = dns / trs
    return vip, vim

def adx14(h, l, c, p=14):
    ph = np.array(h, float); pl = np.array(l, float); pc = np.array(c, float)
    tr = np.maximum.reduce([np.abs(ph[1:] - pc[:-1]), np.abs(pl[1:] - pc[:-1]), ph[1:] - pl[1:]])
    up = ph[1:] - ph[:-1]; dn = pl[:-1] - pl[1:]
    pp = np.where((up > dn) & (up > 0), up, 0.0)
    pm = np.where((dn > up) & (dn > 0), dn, 0.0)
    dx = np.zeros(len(pc)); ad = np.full(len(pc), np.nan)
    for i in range(1, len(pc)):
        if tr[i - 1] > 0:
            dx[i] = abs(pp[i - 1] - pm[i - 1]) / tr[i - 1] * 100
    for i in range(p + 1, len(pc)):
        ad[i] = dx[i - p + 1:i + 1].mean()
    return ad

def months():
    ms = []; y, mo = 2025, 1
    while (y, mo) <= (2025, 12):
        a = datetime(y, mo, 1, tzinfo=timezone.utc)
        b = datetime(y + 1, 1, 1, tzinfo=timezone.utc) if mo == 12 else datetime(y, mo + 1, 1, tzinfo=timezone.utc)
        ms.append((a, b))
        if mo == 12: y, mo = y + 1, 1
        else: mo += 1
    return ms

def main():
    figi = sys.argv[1]
    sym = {"BBG008F2T3T2": "RUAL", "BBG004S681M2": "SNGP", "BBG004S683W7": "AFLT",
           "BBG004S68CP5": "MVID", "BBG004S681B4": "NLMK"}[figi]
    eng_url = get_settings().database_url
    S0 = []; S1b = []; S2 = []
    for a, b in months():
        req = dict(BASE); req["figi"] = figi
        req["from_ts"] = a.isoformat(); req["to_ts"] = b.isoformat()
        candles = _load_candles(figi, a, b)
        r = compute_ensemble(candles, req)
        trades = r.get("static", {}).get("trades", [])
        bars = load_stock(figi, a, b, eng_url)
        if len(bars) < 15:
            S0 += trades
            continue
        ts5 = [x[0] for x in bars]
        h = [x[1] for x in bars]; l = [x[2] for x in bars]; c = [x[3] for x in bars]
        vip, vim = vortex(h, l, c); ad = adx14(h, l, c)
        d = {}
        for j in range(14, len(c)):
            d[ts5[j] + timedelta(minutes=5)] = (vip[j], vim[j], ad[j])
        ts_list = sorted(d)
        for t in trades:
            dt = datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00"))
            jj = bisect_right(ts_list, dt - timedelta(minutes=5)) - 1
            if jj < 0:
                S0.append(t); continue
            vp, vm, a_ = d[ts_list[jj]]
            if vp is None or a_ is None:
                S0.append(t); continue
            # Vortex-confirm (информационно): ensemble контр-тренд vs Vortex тренд -> ~0 (противофаза)
            if jj >= 1:
                vp1, vm1, _ = d[ts_list[jj - 1]]
                if t["side"] == "BUY" and vp is not None and vm is not None and vp1 is not None and vm1 is not None:
                    if (vp > vm) and (vp1 <= vm1):
                        S1b.append(t)
                elif t["side"] == "SELL" and vp is not None and vm is not None and vp1 is not None and vm1 is not None:
                    if (vm > vp) and (vm1 <= vp1):
                        S1b.append(t)
            # ADX тренд-фильтр применяем НАПРЯМУЮ к S0 (независимо от Vortex) -> нетривиальная выборка
            if a_ > 25:
                S2.append(t)
            S0.append(t)
        print(f"  {sym} {a.date()}: +{len(trades)} tr (S1b={len(S1b)}, S2={len(S2)})", flush=True)
    with open(f"reports/_ens_partial_{sym}.json", "w") as f:
        json.dump({"figi": figi, "sym": sym, "S0": S0, "S1b": S1b, "S2": S2}, f)
    print(f"WROTE reports/_ens_partial_{sym}.json (S0={len(S0)}, S1b={len(S1b)}, S2={len(S2)})", flush=True)

if __name__ == "__main__":
    main()
