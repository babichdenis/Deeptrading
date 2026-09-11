#!/usr/bin/env python3
"""Series 3, шаг 2: обучаемая калибровка уверенности (MTF_V4_2026.md §14).

Проверяет, даёт ли ЛОГИСТИЧЕСКАЯ модель на фичах входа калиброванный ranking
(в отличие от ручного additive-score из §23, который не монотонен).

Схема:
  train   — фитим LogisticRegression (+scaler) на фичах входа;
  valid   — последняя часть train по времени, для isotonic-калибровки;
  test    — независимый период: AUC, Brier, reliability bins, net/trade по бакетам.

Фичи (все доступны в момент входа, без look-ahead):
  votes, total, against_bias, vol_ratio, dryup, divergence_bear/bull,
  climax_long/short, volume_on_drop, upper_wick, lower_wick,
  side_is_long, regime (one-hot), hour MSK.

Usage:
  python scripts/confidence_calibration.py \
    --train-from 2026-06-01 --train-to 2026-06-30 \
    --test-from 2026-09-01  --test-to 2026-09-10
"""
import argparse, asyncio, os, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np
from sqlalchemy import text

from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

BASE_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
             "range_compression_breakout", "macd_cross", "donchian_breakout"]
V2P = {
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
    "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
    "donchian_breakout": {"period": 45},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
}
MSK = ZoneInfo("Europe/Moscow")
REGIMES = ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE"]


def optuna_params_dict(p):
    if not p:
        return {"sl_mult": 4.0, "rr": 4.0, "quorum": 2, "vol_thr": 0.0}
    return {
        "sl_mult": float(p.get("sl_mult", 4.0)), "rr": float(p.get("rr", 4.0)),
        "quorum": int(p.get("quorum", 2)), "vol_thr": float(p.get("vol_thr", 0.0) or 0.0),
        "strategy_params": {k: dict(v) for k, v in (p.get("strategy_params") or {}).items()},
    }


def feat_vector(t):
    vf = t.get("volume_features") or {}
    sc = t.get("score_components") or {}
    ts = datetime.fromisoformat(t["entry_ts"])
    hour = ts.astimezone(MSK).hour
    row = {
        "votes": float(sc.get("votes", 0) or 0),
        "total": float(sc.get("total", 0) or 0),
        "against_bias": 1.0 if sc.get("against_bias") else 0.0,
        "vol_ratio": float(vf.get("vol_ratio", 0) or 0),
        "dryup": 1.0 if vf.get("dryup") else 0.0,
        "divergence_bear": 1.0 if vf.get("divergence_bear") else 0.0,
        "divergence_bull": 1.0 if vf.get("divergence_bull") else 0.0,
        "climax_long": 1.0 if vf.get("climax_long") else 0.0,
        "climax_short": 1.0 if vf.get("climax_short") else 0.0,
        "volume_on_drop": 1.0 if vf.get("volume_on_drop") else 0.0,
        "upper_wick": float(vf.get("upper_wick", 0) or 0),
        "lower_wick": float(vf.get("lower_wick", 0) or 0),
        "side_is_long": 1.0 if t["side"] == "LONG" else 0.0,
        "hour": float(hour),
    }
    reg = sc.get("regime") or t.get("regime") or "NEUTRAL"
    for r in REGIMES:
        row[f"reg_{r}"] = 1.0 if reg == r else 0.0
    return row


FEAT_NAMES = ["votes", "total", "against_bias", "vol_ratio", "dryup", "divergence_bear",
              "divergence_bull", "climax_long", "climax_short", "volume_on_drop",
              "upper_wick", "lower_wick", "side_is_long", "hour"] + [f"reg_{r}" for r in REGIMES]


async def collect(tks, from_ts, to_ts, warmup):
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT ticker, figi, lot, optuna_params FROM instruments
            WHERE ticker = ANY(:tk) AND NOT (ticker='T' AND figi='BBG000BSJK37')
            ORDER BY ticker"""), {"tk": tks})).fetchall()
    meta = {r.ticker: (r.figi, r.lot, r.optuna_params) for r in rows}
    out = []
    for tkr in tks:
        if tkr not in meta:
            continue
        figi, lot, opt = meta[tkr]
        p = optuna_params_dict(opt)
        async with SessionLocal() as db:
            candles = await _lc(db, figi, 1, date_from=warmup, date_to=to_ts)
        if not candles:
            continue
        setups = [{"strategy_id": s, "tf": "5min",
                   "params": dict(p["strategy_params"].get(s, V2P.get(s, {})))}
                  for s in BASE_SIDS]
        req = {
            "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
            "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1}, "entry_session": "main",
            "quorum": p["quorum"], "same_side_reentry_cooldown_bars": 15,
            "carry_overnight": True, "opposite_hold": False, "confirm_flip": 2,
            "neutral_mode": "semi_flip",
            "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": p["sl_mult"], "risk_reward": p["rr"]}},
            "commission_rate": 0.0005, "slippage_bps": 2.0,
            "capital": 10000, "lot": lot, "setups": setups,
            "use_all_setups": False, "drop_useless": False,
            "from_ts": from_ts.isoformat(), "to_ts": to_ts.isoformat(),
            "volume_features": True, "score_features": True,
        }
        if p["vol_thr"] and p["vol_thr"] > 0:
            req["volume_filter_threshold"] = p["vol_thr"]
        res = compute_ensemble(candles, req)
        if "error" in res:
            print(f"{tkr}: ERROR {res['error']}")
            continue
        for t in res.get("static", {}).get("trades", []):
            t["_ticker"] = tkr
            out.append(t)
        print(f"{tkr}: {len(res.get('static', {}).get('trades', []))} trades")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-from", required=True)
    ap.add_argument("--train-to", required=True)
    ap.add_argument("--test-from", required=True)
    ap.add_argument("--test-to", required=True)
    ap.add_argument("--warmup-days", type=int, default=60)
    ap.add_argument("--tickers", default="SMLT,GAZP,CHMF,NLMK,MAGN,LENT,NVTK,SNGSP,GMKN,RUAL")
    args = ap.parse_args()
    tks = [t.strip() for t in args.tickers.split(",") if t.strip()]

    tr_f = datetime.fromisoformat(args.train_from).replace(tzinfo=timezone.utc)
    tr_t = datetime.fromisoformat(args.train_to).replace(tzinfo=timezone.utc)
    te_f = datetime.fromisoformat(args.test_from).replace(tzinfo=timezone.utc)
    te_t = datetime.fromisoformat(args.test_to).replace(tzinfo=timezone.utc)
    from datetime import timedelta
    warm = tr_f - timedelta(days=args.warmup_days)

    print("=== TRAIN ===")

    async def _run():
        tr = await collect(tks, tr_f, tr_t, warm)
        print("=== TEST ===")
        te = await collect(tks, te_f, te_t, te_f - timedelta(days=args.warmup_days))
        return tr, te

    tr, te = asyncio.run(_run())
    if len(tr) < 50 or len(te) < 20:
        print(f"мало данных: train={len(tr)} test={len(te)}")
        return

    tr.sort(key=lambda t: t["entry_ts"])
    te.sort(key=lambda t: t["entry_ts"])
    Xtr = np.array([[feat_vector(t)[f] for f in FEAT_NAMES] for t in tr])
    ytr = np.array([1 if t["net"] > 0 else 0 for t in tr])
    Xte = np.array([[feat_vector(t)[f] for f in FEAT_NAMES] for t in te])
    yte = np.array([1 if t["net"] > 0 else 0 for t in te])
    net_te = np.array([t["net"] for t in te])

    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import roc_auc_score, brier_score_loss
    from sklearn.isotonic import IsotonicRegression

    scaler = StandardScaler().fit(Xtr)
    clf = LogisticRegression(max_iter=2000, C=1.0).fit(scaler.transform(Xtr), ytr)

    n_val = max(int(len(Xtr) * 0.2), 1)
    Xva, yva = Xtr[-n_val:], ytr[-n_val:]
    p_va = clf.predict_proba(scaler.transform(Xva))[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip").fit(p_va, yva)

    p_te_raw = clf.predict_proba(scaler.transform(Xte))[:, 1]
    p_te = iso.predict(p_te_raw)

    auc = roc_auc_score(yte, p_te_raw)
    brier = brier_score_loss(yte, p_te)
    print(f"\ntrain={len(tr)} test={len(te)}  AUC(test)={auc:.3f}  Brier(test)={brier:.3f}")

    print(f"\n=== Reliability / net по бакетам калиброванной вероятности (test) ===")
    print(f"{'Bucket':<12} | {'N':>5} | {'pred%':>6} | {'fact%':>6} | {'Net':>10} | {'Net/tr':>8}")
    edges = [0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 1.01]
    for lo, hi in zip(edges, edges[1:]):
        m = (p_te >= lo) & (p_te < hi)
        n = int(m.sum())
        if n == 0:
            continue
        pred = p_te[m].mean() * 100
        fact = yte[m].mean() * 100
        net = net_te[m].sum()
        print(f"[{lo:.2f},{hi:.2f})  | {n:>5} | {pred:>6.1f} | {fact:>6.1f} | {net:>10.2f} | {net/n:>8.2f}")

    order = np.argsort(p_te)
    q = len(p_te) // 4
    print(f"\n=== Квартили (test) ===")
    for i in range(4):
        idx = order[i * q:(i + 1) * q] if i < 3 else order[3 * q:]
        if len(idx) == 0:
            continue
        print(f"Q{i+1}: N={len(idx):>4} pred={p_te[idx].mean()*100:>5.1f}% fact={yte[idx].mean()*100:>5.1f}% "
              f"Net/tr={net_te[idx].mean():>7.2f}")


if __name__ == "__main__":
    main()