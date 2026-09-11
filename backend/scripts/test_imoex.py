"""Бэктест IMOEX: направление индекса как veto + голос при высокой волатильности.

Запуск: cd backend && PYTHONPATH=. .venv/bin/python3 scripts/test_imoex.py [DAYS]
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.database import SessionLocal
from app.services.signals import _load_candles as _lc
from app.services.ensemble import compute_ensemble, resample, TF_SECONDS
from app.bot.ensemble_strategy import V2_SETUPS

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 40
IMOEX_FIGI = "BBG00KDWPPW2"
V2P = {s["strategy_id"]: s["params"] for s in V2_SETUPS}


def setups(ids):
    return [{"strategy_id": s, "tf": "5min", "params": dict(V2P.get(s, {}))} for s in ids]


BASE5 = ["rsi_reversal", "bollinger_reclaim", "vwap_reclaim", "macd_cross", "donchian_breakout"]
SETUPS = setups(BASE5 + ["volume_drop"])

VARIANTS = [
    ("1. baseline (5+vol_drop)", False, False, "hv"),
    ("2. IMOEX veto (HV)", True, False, "hv"),
    ("3. IMOEX голос (HV)", False, True, "hv"),
    ("4. IMOEX veto+голос (HV)", True, True, "hv"),
    ("5. IMOEX veto (все режимы)", True, False, "all"),
    ("6. IMOEX голос (все режимы)", False, True, "all"),
    ("7. IMOEX veto+голос (все)", True, True, "all"),
]


def _ema(vals, span):
    if not vals:
        return []
    a = 2 / (span + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def _atr(candles, period=14):
    out = []
    for i in range(1, len(candles)):
        p, c = candles[i - 1], candles[i]
        out.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    sma = []
    for i in range(len(out)):
        w = out[max(0, i - period + 1):i + 1]
        sma.append(sum(w) / len(w))
    return [None] + sma


def imoex_maps(imoex_1m, hv_pct=90.0):
    """Возвращает (dir: {ts_iso: +1/-1}, hv: set(ts_iso)) на 5m."""
    c5 = resample(imoex_1m, TF_SECONDS["5min"])
    closes = [float(c.close) for c in c5]
    ema = _ema(closes, 50)
    atr = _atr(c5, 14)
    atr_pct = [a / max(cl, 1e-9) * 100 if a is not None else None for a, cl in zip(atr, closes)]
    vals = [v for v in atr_pct if v is not None]
    thr = None
    if vals:
        vals_sorted = sorted(vals)
        thr = vals_sorted[min(len(vals_sorted) - 1, int(len(vals_sorted) * hv_pct / 100))]
    dir_map = {}
    hv_set = set()
    for i, c in enumerate(c5):
        t = c.ts.isoformat()
        dir_map[t] = 1 if closes[i] >= ema[i] else -1
        if thr is not None and atr_pct[i] is not None and atr_pct[i] >= thr:
            hv_set.add(t)
    return dir_map, hv_set, thr


async def get_eligible():
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT DISTINCT i.figi, i.ticker, i.lot FROM instruments i "
            "JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible' ORDER BY i.ticker"
        ))).fetchall()
    return [(r[0], r[1], int(r[2]) if r[2] else 1) for r in rows]


def base_req(figi, lot, imoex=None):
    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "all", "quorum": 2,
        "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": 10000,
        "lot": lot, "setups": SETUPS, "use_all_setups": False, "drop_useless": True,
        "neutral_mode": "semi_flip",
    }
    if imoex:
        req["imoex"] = imoex
    return req


async def main():
    elig = await get_eligible()
    dfrom = datetime.now(timezone.utc) - timedelta(days=DAYS)
    async with SessionLocal() as db:
        imoex_1m = await _lc(db, IMOEX_FIGI, 1, date_from=dfrom)
        cmap = {}
        for figi, ticker, lot in elig:
            c = await _lc(db, figi, 1, date_from=dfrom)
            if len(c) >= 200:
                cmap[ticker] = (figi, c, lot)
    dir_map, hv_set, thr = imoex_maps(imoex_1m)
    print(f"eligible {len(elig)} | с данными {len(cmap)} | период {DAYS}д")
    print(f"IMOEX: 5m баров {len(dir_map)} | HV-порог ATR%={thr:.3f} | HV-баров {len(hv_set)}")

    print("\n" + "=" * 108)
    print("  %-30s %7s %6s %8s %10s %12s %12s %7s" % ("Variant", "Trades", "Wins", "WR%", "GrossWin", "GrossLoss", "Net", "PF"))
    print("=" * 108)
    for label, use_veto, use_voice, mode in VARIANTS:
        tr = []
        for ticker, (figi, c, lot) in cmap.items():
            imoex = None
            if use_veto or use_voice:
                hv = list(hv_set) if mode == "hv" else list(dir_map.keys())
                imoex = {"dir": dir_map, "hv": hv}
            try:
                res = compute_ensemble(c, base_req(figi, lot, imoex))
                if "error" not in res:
                    tr.extend(res.get("static", {}).get("trades", []))
            except Exception as e:
                print("  err", ticker, type(e).__name__, str(e)[:60])
        w = [t for t in tr if t.get("net", 0) > 0]
        l = [t for t in tr if t.get("net", 0) < 0]
        gw = sum(t["net"] for t in w)
        gl = sum(t["net"] for t in l)
        net = sum(t.get("net", 0) for t in tr)
        wr = len(w) / len(tr) * 100 if tr else 0
        pf = gw / abs(gl) if gl else 0
        print("  %-30s %7d %6d %7.1f%% %+11.0f %+11.0f %+11.0f %7.2f" % (
            label, len(tr), len(w), wr, gw, gl, net, pf))


asyncio.run(main())
