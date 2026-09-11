#!/usr/bin/env python3
"""Conformal sizing: неопределённость → РАЗМЕР позиции (не gate).

Series 3 / MTF_V4_2026.md §14 + внешний план (conformal prediction).
Строим логит P(win) на фичах входа, оборачиваем в split-conformal,
сравниваем 3 политики на OOS:
  baseline        — все сделки, размер 1.0
  conformal_gate  — торгуем только singleton-{win} (p_win >= 1-q), иначе пропуск
  conformal_size  — размер ∝ уверенности: clip(1 + (p-0.5)*k, 0.5, 2.0)

Split: train (proper) → calib (conformal quantile) → test (OOS).
Все периоды — по времени, без перемешивания.

Usage:
  python scripts/conformal_sizing.py \
    --train-from 2026-06-01 --train-to 2026-06-30 \
    --test-from 2026-09-01  --test-to 2026-09-10
"""
import argparse, asyncio, os, sys
from datetime import datetime, timedelta, timezone
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


def max_dd(trades):
    tr = sorted(trades, key=lambda t: t["exit_ts"])
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    for t in tr:
        eq += t["_sized_net"]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return mdd


def policy_stats(trades, size_fn, name):
    sized = []
    for t in trades:
        s = size_fn(t)
        if s <= 0:
            continue
        tt = dict(t)
        tt["_sized_net"] = t["net"] * s
        tt["_size"] = s
        sized.append(tt)
    net = sum(x["_sized_net"] for x in sized)
    wins = sum(1 for x in sized if x["_sized_net"] > 0)
    gw = sum(x["_sized_net"] for x in sized if x["_sized_net"] > 0)
    gl = -sum(x["_sized_net"] for x in sized if x["_sized_net"] <= 0)
    pf = gw / gl if gl > 1e-9 else float("inf")
    mdd = max_dd(sized) if sized else 0.0
    print(f"{name:<16} | {len(sized):>5} | {wins/max(len(sized),1)*100:>5.1f} | {net:>10.2f} | "
          f"{pf:>6.2f} | {mdd:>9.2f} | {net/max(len(sized),1):>8.2f}")
    return net, pf, mdd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-from", required=True)
    ap.add_argument("--train-to", required=True)
    ap.add_argument("--test-from", required=True)
    ap.add_argument("--test-to", required=True)
    ap.add_argument("--alpha", type=float, default=0.2, help="1-coverage для conformal")
    ap.add_argument("--size-k", type=float, default=2.0, help="крутизна sizing")
    ap.add_argument("--warmup-days", type=int, default=60)
    ap.add_argument("--tickers", default="SMLT,GAZP,CHMF,NLMK,MAGN,LENT,NVTK,SNGSP,GMKN,RUAL")
    args = ap.parse_args()
    tks = [t.strip() for t in args.tickers.split(",") if t.strip()]

    tr_f = datetime.fromisoformat(args.train_from).replace(tzinfo=timezone.utc)
    tr_t = datetime.fromisoformat(args.train_to).replace(tzinfo=timezone.utc)
    te_f = datetime.fromisoformat(args.test_from).replace(tzinfo=timezone.utc)
    te_t = datetime.fromisoformat(args.test_to).replace(tzinfo=timezone.utc)

    print("=== TRAIN+CALIB ===")
    te_f_warm = te_f - timedelta(days=args.warmup_days)

    async def _run():
        tr = await collect(tks, tr_f, tr_t, tr_f - timedelta(days=args.warmup_days))
        print("=== TEST ===")
        te = await collect(tks, te_f, te_t, te_f_warm)
        return tr, te

    tr, te = asyncio.run(_run())
    if len(tr) < 100 or len(te) < 30:
        print(f"мало данных: train={len(tr)} test={len(te)}")
        return

    tr.sort(key=lambda t: t["entry_ts"])
    te.sort(key=lambda t: t["entry_ts"])
    Xtr = np.array([[feat_vector(t)[f] for f in FEAT_NAMES] for t in tr])
    ytr = np.array([1 if t["net"] > 0 else 0 for t in tr])
    Xte = np.array([[feat_vector(t)[f] for f in FEAT_NAMES] for t in te])
    yte = np.array([1 if t["net"] > 0 else 0 for t in te])

    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import roc_auc_score, brier_score_loss

    n_prop = int(len(Xtr) * 0.7)
    Xp, yp = Xtr[:n_prop], ytr[:n_prop]
    Xc, yc = Xtr[n_prop:], ytr[n_prop:]
    scaler = StandardScaler().fit(Xp)
    clf = LogisticRegression(max_iter=2000, C=1.0).fit(scaler.transform(Xp), yp)

    p_cal = clf.predict_proba(scaler.transform(Xc))[:, 1]
    # nonconformity для истинного класса
    s_cal = np.where(yc == 1, 1 - p_cal, p_cal)
    n = len(s_cal)
    q_level = min(1.0, np.ceil((n + 1) * (1 - args.alpha)) / n)
    q = float(np.quantile(s_cal, q_level))

    p_te = clf.predict_proba(scaler.transform(Xte))[:, 1]
    auc = roc_auc_score(yte, p_te)
    brier = brier_score_loss(yte, p_te)
    print(f"\ntrain={len(tr)} (prop={n_prop}, cal={len(s_cal)}) test={len(te)}")
    print(f"conformal q({1-args.alpha:.0%})={q:.3f}  singleton-win threshold p>={1-q:.3f}")
    print(f"AUC(test)={auc:.3f}  Brier(test)={brier:.3f}")

    print(f"\n=== Reliability (test) ===")
    print(f"{'Bucket':<12} | {'N':>5} | {'pred%':>6} | {'fact%':>6} | {'Net/tr':>8}")
    for lo, hi in [(0, .4), (.4, .5), (.5, .6), (.6, .7), (.7, .8), (.8, 1.01)]:
        m = (p_te >= lo) & (p_te < hi)
        if m.sum() == 0:
            continue
        net = sum(t["net"] for t, k in zip(te, m) if k)
        print(f"[{lo:.2f},{hi:.2f})  | {int(m.sum()):>5} | {p_te[m].mean()*100:>6.1f} | "
              f"{yte[m].mean()*100:>6.1f} | {net/m.sum():>8.2f}")

    for t, p in zip(te, p_te):
        t["_p"] = float(p)

    print(f"\n=== Политики (test) ===")
    print(f"{'Policy':<16} | {'N':>5} | {'WR%':>5} | {'Net':>10} | {'PF':>6} | {'MaxDD':>9} | {'Net/tr':>8}")
    policy_stats(te, lambda t: 1.0, "baseline")
    policy_stats(te, lambda t: 1.0 if t["_p"] >= (1 - q) else 0.0, "conformal_gate")
    policy_stats(te, lambda t: float(np.clip(1 + (t["_p"] - 0.5) * args.size_k, 0.5, 2.0)), "conformal_size")

    print(f"\n=== Sizing по квартилям p (test) ===")
    order = np.argsort(p_te)
    qn = len(p_te) // 4
    for i in range(4):
        idx = order[i * qn:(i + 1) * qn] if i < 3 else order[3 * qn:]
        if len(idx) == 0:
            continue
        net = sum(te[j]["net"] for j in idx)
        print(f"Q{i+1}: N={len(idx):>4} p={p_te[idx].mean()*100:>5.1f}% "
              f"WR={yte[idx].mean()*100:>5.1f}% Net/tr={net/len(idx):>7.2f}")


if __name__ == "__main__":
    main()