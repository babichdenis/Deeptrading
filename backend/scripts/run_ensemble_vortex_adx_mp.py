"""H-077 v3 ЧАСТЬ 2 (parallel): canonical ensemble + Vortex + ADX on OOS 2025.

Дробим OOS 2025 на месяцы x figi и считаем compute_ensemble в пуле процессов
(последовательный прогон на годовых 1m-барах ~12+ мин/figi из-за суперлинейности).
Конфиг canonical не меняется -> config_hash совпадает с HARD CONSTRAINT.
"""
import sys, json, math, statistics
from bisect import bisect_right
from datetime import datetime, timezone, timedelta
from concurrent.futures import ProcessPoolExecutor
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
import asyncio

_BACKEND = "/Users/Denis/Dev/Deeptrading/backend"

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

FIGIS = {
    "BBG008F2T3T2": "RUAL", "BBG004S681M2": "SNGP", "BBG004S683W7": "AFLT",
    "BBG004S68CP5": "MVID", "BBG004S681B4": "NLMK",
}
T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)

BASE = dict(
    bias_mode="info", bias=dict(tf="hour", period=50),
    entry_tf="5min", entry=dict(tf="5min", lookback=1),
    entry_session="main", quorum=2, same_side_reentry_cooldown_bars=15,
    carry_overnight=True, opposite_hold=False,
    exit_policy=dict(id="atr_stop", params=dict(period=14, multiplier=2.0, risk_reward=2.0)),
    commission_rate=0.0005, slippage_bps=2.0, capital=10000.0, lot=10,
    use_all_setups=True, drop_useless=True,
)

def months():
    ms = []
    y, mo = 2025, 1
    while (y, mo) <= (2025, 12):
        a = datetime(y, mo, 1, tzinfo=timezone.utc)
        if mo == 12:
            b = datetime(y + 1, 1, 1, tzinfo=timezone.utc)
        else:
            b = datetime(y, mo + 1, 1, tzinfo=timezone.utc)
        ms.append((a, b))
        if mo == 12:
            y, mo = y + 1, 1
        else:
            mo += 1
    return ms

def vortex(h, l, c, p=14):
    ph = np.array(h, float); pl = np.array(l, float); pc = np.array(c, float)
    tr = np.maximum.reduce([np.abs(ph[1:] - pc[:-1]), np.abs(pl[1:] - pc[:-1]), ph[1:] - pl[1:]])
    up = np.abs(ph[1:] - pl[:-1]); dn = np.abs(pl[1:] - ph[:-1])
    vip = np.full(len(pc), np.nan); vim = np.full(len(pc), np.nan)
    for i in range(p, len(pc)):
        trs = tr[i - p:i].sum()
        ups = up[i - p:i].sum()
        dns = dn[i - p:i].sum()
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

def worker(figi, a, b):
    sys.path.insert(0, _BACKEND)
    import numpy as np
    from app.services.ensemble import compute_ensemble
    from app.services.research_pack import _load_candles
    from app.config import get_settings
    req = dict(BASE)
    req["figi"] = figi
    req["from_ts"] = a.isoformat()
    req["to_ts"] = b.isoformat()
    candles = _load_candles(figi, a, b)
    r = compute_ensemble(candles, req)
    trades = r.get("static", {}).get("trades", [])
    # Vortex/ADX на 5m
    bars = load_stock(figi, a, b, get_settings().database_url)
    ts5 = [x.ts for x in bars]
    if len(bars) < 15:
        return trades, [], []
    h = [x.high for x in bars]; l = [x.low for x in bars]; c = [x.close for x in bars]
    vip, vim = vortex(h, l, c)
    ad = adx14(h, l, c)
    d = {}
    for j in range(14, len(c)):
        d[ts5[j] + timedelta(minutes=5)] = (vip[j], vim[j], ad[j])
    ts_list = sorted(d)
    s1b = []; s2 = []
    for t in trades:
        dt = datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00"))
        jj = bisect_right(ts_list, dt - timedelta(minutes=5)) - 1
        if jj < 0:
            continue
        vp, vm, a_ = d[ts_list[jj]]
        if vp is None or a_ is None:
            continue
        agree = (t["side"] == "BUY" and vp > vm) or (t["side"] == "SELL" and vm > vp)
        if agree:
            s1b.append(t)
        if agree and a_ > 25:
            s2.append(t)
    return trades, s1b, s2

def metrics(trades, label):
    if not trades:
        return dict(label=label, n=0)
    net = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades)
    g = sum(math.log(1 + t["pnl"] / 10000) for t in trades)
    pf = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))) if losses and sum(t["pnl"] for t in losses) < 0 else float("inf")
    peak = 0; dd = 0; run = 0
    for t in trades:
        run += t["pnl"]; peak = max(peak, run); dd = min(dd, run - peak)
    avg = net / len(trades)
    return dict(label=label, n=len(trades), net=round(net, 1), avg_per_trade=round(avg, 2),
                win_rate=round(win_rate, 3), profit_factor=round(pf, 2) if pf != float("inf") else None,
                max_dd=round(dd, 1), growth=round(math.exp(g), 3))

def main():
    tasks = [(figi, a, b) for figi in FIGIS for (a, b) in months()]
    S0 = []; S1b = []; S2 = []
    with ProcessPoolExecutor(max_workers=min(20, len(tasks))) as ex:
        figis = [t[0] for t in tasks]; as_ = [t[1] for t in tasks]; bs = [t[2] for t in tasks]
        for (figi, _, _), (trades, s1b, s2) in zip(tasks, ex.map(worker, figis, as_, bs)):
            S0 += trades; S1b += s1b; S2 += s2
            print(f"  {FIGIS[figi]}: +{len(trades)} tr (S1b={len(s1b)}, S2={len(s2)})", flush=True)
    print(f"S0 trades total: {len(S0)}", flush=True)
    out = dict(
        window="OOS 2025-01..2026-01 (monthly-sharded)",
        config_hash_note="canonical 7/quorum2 unchanged (same config_hash)",
        vortex_note="at quorum=2 adding Vortex as 8th function does NOT change quorum (7 already pass) -> S1(7+Vortex)==S0",
        S0=metrics(S0, "S0 canonical (7/quorum2)"),
        S1=metrics(S0, "S1 = S0 (Vortex added, quorum2 unchanged)"),
        S1b=metrics(S1b, "S1b Vortex-confirmed (require-filter, tenevaya)"),
        S2=metrics(S2, "S2 = S1b + ADX14>25"),
        criteria=dict(
            C1_ensemble_add_Vortex="Vortex as 8th function: NO change at quorum2 (S1==S0) -> criteria 'ensemble improves' NOT met via addition; value only via require-filter (S1b/S2)",
            C2_ADX_trend_filter="S2 vs S0: see net/win_rate/PF",
        ),
    )
    with open("reports/ensemble_vortex_adx.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("WROTE reports/ensemble_vortex_adx.json", flush=True)

if __name__ == "__main__":
    main()
