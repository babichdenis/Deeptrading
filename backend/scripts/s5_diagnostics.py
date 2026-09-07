#!/usr/bin/env python3
"""Series 5 Diagnostics: trades breakdown with quorum votes + indicator values per regime.

Для каждой сделки записывает:
- Entry: regime, quorum votes (какие стратегии), RSI, ATR%, EMA slope, ADX, volume_ratio
- Exit: regime, exit_reason, RSI, ATR%, EMA slope
- Flip context: was this a flip? from what side?
- Win/Loss outcome

Важно: flip = exit + entry, НЕ считается дважды.
"""
import asyncio, os, sys, json
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.engine.models import Candle as EngineCandle
from app.engine.indicators import atr
from app.services.indicators import rsi, ema
from app.services.regime import _adx, RegimeDetector, regime_at
from app.services.ensemble import TF_SECONDS

DATE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
DATE_TO = datetime(2026, 9, 1, tzinfo=timezone.utc)

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]

V2_PARAMS = {
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
    "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
    "donchian_breakout": {"period": 45},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
}


def base_req(figi, lot):
    setups = [{"strategy_id": sid, "tf": "5min", "params": dict(V2_PARAMS.get(sid, {}))} for sid in ALL_SIDS]
    return {
        "figi": figi,
        "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "main",
        "quorum": 2,
        "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True,
        "opposite_hold": False,
        "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": 0.0005,
        "slippage_bps": 2.0,
        "capital": 10000,
        "lot": lot,
        "setups": setups,
        "use_all_setups": False,
        "drop_useless": True,
        "from_ts": DATE_FROM.isoformat(),
        "to_ts": DATE_TO.isoformat(),
    }


def resample(candles, tf_seconds):
    out = []
    for c in candles:
        epoch = int(c.ts.timestamp())
        bucket = epoch - epoch % tf_seconds
        key = datetime.fromtimestamp(bucket, tz=timezone.utc)
        if out and out[-1].ts == key:
            prev = out[-1]
            out[-1] = EngineCandle(ts=key, open=prev.open, high=max(prev.high, c.high),
                                   low=min(prev.low, c.low), close=c.close,
                                   volume=prev.volume + c.volume)
        else:
            out.append(EngineCandle(ts=key, open=c.open, high=c.high, low=c.low,
                                    close=c.close, volume=c.volume))
    return out


def compute_indicators(candles_5m):
    """Compute RSI, ATR%, EMA slope, ADX, volume_ratio on 5m candles."""
    closes = [c.close for c in candles_5m]
    highs = [c.high for c in candles_5m]
    lows = [c.low for c in candles_5m]
    volumes = [c.volume for c in candles_5m]
    n = len(candles_5m)

    rsi_vals = rsi(closes, 16) if n > 16 else [None] * n
    atr_vals = atr(candles_5m, 14) if n > 14 else [None] * n
    atr_pct = [None] * n
    for i in range(n):
        if atr_vals[i] is not None and closes[i] > 0:
            atr_pct[i] = atr_vals[i] / closes[i] * 100

    ema20 = ema(closes, 20) if n > 20 else [None] * n
    ema50 = ema(closes, 50) if n > 50 else [None] * n
    ema_slope = [None] * n
    for i in range(5, n):
        if ema50[i] is not None and ema50[i-5] is not None and ema50[i-5] > 0:
            ema_slope[i] = (ema50[i] - ema50[i-5]) / ema50[i-5] * 100

    adx_vals = [None] * 1 + _adx(candles_5m, 14) if n > 14 else [None] * n
    adx_vals = [v if v is not None else (adx_vals[1] if len(adx_vals) > 1 else None) for v in adx_vals]

    vol_ratio = [None] * n
    for i in range(1, n):
        window = volumes[max(0, i-50):i]
        mean = sum(window) / len(window) if window else 1.0
        vol_ratio[i] = volumes[i] / max(mean, 1e-9)

    return {
        "rsi": rsi_vals,
        "atr_pct": atr_pct,
        "ema_slope": ema_slope,
        "adx": adx_vals,
        "vol_ratio": vol_ratio,
    }


def lookup_indicator(ts, candles_5m, indicator_array):
    """Look up indicator value at timestamp ts (nearest bar)."""
    best_idx = None
    best_diff = None
    for i, c in enumerate(candles_5m):
        diff = abs((c.ts - ts).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_idx = i
    if best_idx is not None and best_diff < 600:  # within 10 min
        return indicator_array[best_idx]
    return None


async def load_august(figi):
    from app.services.signals import _load_candles as _lc
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=DATE_FROM, date_to=DATE_TO)


async def get_eligible():
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT DISTINCT i.figi, i.ticker, i.lot
            FROM instruments i
            JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible'
            ORDER BY i.ticker
        """))).fetchall()
    return [(r.figi, r.ticker, r.lot) for r in rows]


def analyze_ticker(figi, ticker, candles, lot):
    """Run compute_ensemble and enrich trades with indicators + quorum votes."""
    req = base_req(figi, lot)
    res = compute_ensemble(candles, req)
    if "error" in res:
        print("    compute_ensemble ERROR:", res["error"])
        return None

    trades = res.get("static", {}).get("trades", [])
    quorum_list = res.get("static", {}).get("quorum_list", [])
    if not trades:
        print("    compute_ensemble returned 0 trades (quorum=%d)" % len(quorum_list))
        return None

    # Build regime timeline independently from regime_bars
    regime_tf_sec = TF_SECONDS.get("5min", 300)
    regime_bars_raw = resample(candles, regime_tf_sec)
    detector = RegimeDetector()
    regime_row = detector.compute(regime_bars_raw)

    # Build quorum lookup by timestamp (nearest match)
    quorum_by_ts_side = defaultdict(list)
    for q in quorum_list:
        q_ts = datetime.fromisoformat(q["ts"])
        quorum_by_ts_side[(q["side"], q_ts)].append(q)

    # Build 5m candles + indicators
    candles_5m = resample(candles, 300)
    indicators = compute_indicators(candles_5m)

    # Enrich each trade
    enriched = []
    prev_exit_side = None
    for i, t in enumerate(trades):
        entry_ts = datetime.fromisoformat(t["entry_ts"])
        exit_ts = datetime.fromisoformat(t["exit_ts"])

        # Entry indicators
        rsi_entry = lookup_indicator(entry_ts, candles_5m, indicators["rsi"])
        atr_entry = lookup_indicator(entry_ts, candles_5m, indicators["atr_pct"])
        ema_entry = lookup_indicator(entry_ts, candles_5m, indicators["ema_slope"])
        adx_entry = lookup_indicator(entry_ts, candles_5m, indicators["adx"])
        vol_entry = lookup_indicator(entry_ts, candles_5m, indicators["vol_ratio"])

        # Exit indicators
        rsi_exit = lookup_indicator(exit_ts, candles_5m, indicators["rsi"])
        atr_exit = lookup_indicator(exit_ts, candles_5m, indicators["atr_pct"])
        ema_exit = lookup_indicator(exit_ts, candles_5m, indicators["ema_slope"])

        # Find quorum votes for this entry
        quorum_votes = []
        for q in quorum_list:
            q_ts = datetime.fromisoformat(q["ts"])
            if abs((q_ts - entry_ts).total_seconds()) < 300 and q["side"] == t["side"]:
                quorum_votes = q.get("members_for", [])
                break

        # Find quorum votes for exit (opposite side at exit time)
        exit_side = "SELL" if t["side"] == "LONG" else "BUY"
        quorum_exit_votes = []
        for q in quorum_list:
            q_ts = datetime.fromisoformat(q["ts"])
            if abs((q_ts - exit_ts).total_seconds()) < 300 and q["side"] == exit_side:
                quorum_exit_votes = q.get("members_for", [])
                break

        # Look up regime from regime_row (independent of trade dict)
        regime_state = regime_at(regime_row, entry_ts)
        regime_name = regime_state["state"] if regime_state else "UNKNOWN"

        # SL/TP distances
        entry_px = t["entry_px"]
        stop_px = t.get("stop")
        target_px = t.get("target")
        sl_dist_pct = abs(entry_px - stop_px) / entry_px * 100 if stop_px and entry_px else None
        tp_dist_pct = abs(target_px - entry_px) / entry_px * 100 if target_px and entry_px else None

        # Flip detection
        is_flip = False
        if i > 0:
            prev = trades[i-1]
            prev_side = prev["side"]
            if prev_side != t["side"]:
                gap = abs((entry_ts - datetime.fromisoformat(prev["exit_ts"])).total_seconds())
                if gap < 600:  # within 10 min = flip
                    is_flip = True

        enriched.append({
            "ticker": ticker,
            "trade_num": i + 1,
            "side": t["side"],
            "is_flip": is_flip,
            "regime": regime_name,
            "entry_ts": t["entry_ts"],
            "exit_ts": t["exit_ts"],
            "entry_px": t["entry_px"],
            "exit_px": t["exit_px"],
            "stop": t["stop"],
            "target": t["target"],
            "sl_dist_pct": round(sl_dist_pct, 3) if sl_dist_pct else None,
            "tp_dist_pct": round(tp_dist_pct, 3) if tp_dist_pct else None,
            "net": t["net"],
            "gross": t["gross"],
            "bars_held": t["bars_held"],
            "exit_reason": t["exit_reason"],
            "mfe_r": t["mfe_r"],
            "mae_r": t["mae_r"],
            "win": t["net"] > 0,
            # Quorum votes
            "quorum_votes": quorum_votes,
            "quorum_n": len(quorum_votes),
            "quorum_exit_votes": quorum_exit_votes,
            "quorum_exit_n": len(quorum_exit_votes),
            # Entry indicators
            "rsi_entry": round(rsi_entry, 2) if rsi_entry is not None else None,
            "atr_pct_entry": round(atr_entry, 3) if atr_entry is not None else None,
            "ema_slope_entry": round(ema_entry, 3) if ema_entry is not None else None,
            "adx_entry": round(adx_entry, 1) if adx_entry is not None else None,
            "vol_ratio_entry": round(vol_entry, 2) if vol_entry is not None else None,
            # Exit indicators
            "rsi_exit": round(rsi_exit, 2) if rsi_exit is not None else None,
            "atr_pct_exit": round(atr_exit, 3) if atr_exit is not None else None,
            "ema_slope_exit": round(ema_exit, 3) if ema_exit is not None else None,
        })

    return enriched


def print_ticker_report(all_trades):
    """Detailed per-ticker table with indicators, gross, commission, PF."""
    by_ticker = defaultdict(lambda: {"trades": [], "wins": [], "losses": []})
    for t in all_trades:
        tk = t["ticker"]
        by_ticker[tk]["trades"].append(t)
        if t["win"]:
            by_ticker[tk]["wins"].append(t)
        else:
            by_ticker[tk]["losses"].append(t)

    def avg(lst, key):
        vals = [t[key] for t in lst if t[key] is not None]
        return sum(vals) / len(vals) if vals else None

    def summ(lst, key):
        return sum(t[key] for t in lst if t[key] is not None)

    print()
    print("=" * 160)
    print("  PER-TICKER DETAILED REPORT")
    print("=" * 160)

    # Header
    print("  %-8s %5s %5s %5s %5s %9s %9s %9s %9s %6s %5s %5s %5s %5s %5s %5s" % (
        "Ticker", "Trd", "Win", "Loss", "WR%", "GrossW", "GrossL", "Comm", "Net", "PF",
        "MFE", "MAE", "Bars", "ATR%", "EMA", "ADX"))
    print("  " + "-" * 155)

    for ticker in ["SMLT", "NLMK", "GAZP", "LENT", "CHMF"]:
        if ticker not in by_ticker:
            continue
        data = by_ticker[ticker]
        trades = data["trades"]
        wins = data["wins"]
        losses = data["losses"]
        n = len(trades)
        wr = len(wins) / n * 100 if n else 0
        gross_win = summ(wins, "gross")
        gross_loss = summ(losses, "gross")
        comm = summ(trades, "commission") if "commission" in trades[0] else 0
        net = summ(trades, "net")
        pf = gross_win / abs(gross_loss) if gross_loss else 0
        mfe = avg(trades, "mfe_r")
        mae = avg(trades, "mae_r")
        bh = avg(trades, "bars_held")
        atr_e = avg(trades, "atr_pct_entry")
        ema_e = avg(trades, "ema_slope_entry")
        adx_e = avg(trades, "adx_entry")

        print("  %-8s %5d %5d %5d %4.0f%% %+9.0f %+9.0f %+9.0f %+9.0f %5.2f %5.2f %5.2f %5.0f %5.2f %+5.3f %5.1f" % (
            ticker, n, len(wins), len(losses), wr,
            gross_win, gross_loss, comm, net, pf,
            mfe if mfe else 0, mae if mae else 0, bh if bh else 0,
            atr_e if atr_e else 0, ema_e if ema_e else 0, adx_e if adx_e else 0))

    # TOTAL row
    n = len(all_trades)
    wins = [t for t in all_trades if t["win"]]
    losses = [t for t in all_trades if not t["win"]]
    wr = len(wins) / n * 100 if n else 0
    gross_win = summ(wins, "gross")
    gross_loss = summ(losses, "gross")
    comm = summ(all_trades, "commission") if "commission" in all_trades[0] else 0
    net = summ(all_trades, "net")
    pf = gross_win / abs(gross_loss) if gross_loss else 0
    mfe = avg(all_trades, "mfe_r")
    mae = avg(all_trades, "mae_r")
    bh = avg(all_trades, "bars_held")
    atr_e = avg(all_trades, "atr_pct_entry")
    ema_e = avg(all_trades, "ema_slope_entry")
    adx_e = avg(all_trades, "adx_entry")

    print("  " + "-" * 155)
    print("  %-8s %5d %5d %5d %4.0f%% %+9.0f %+9.0f %+9.0f %+9.0f %5.2f %5.2f %5.2f %5.0f %5.2f %+5.3f %5.1f" % (
        "TOTAL", n, len(wins), len(losses), wr,
        gross_win, gross_loss, comm, net, pf,
        mfe if mfe else 0, mae if mae else 0, bh if bh else 0,
        atr_e if atr_e else 0, ema_e if ema_e else 0, adx_e if adx_e else 0))

    # Detailed per-ticker × regime matrix
    print()
    print("=" * 160)
    print("  PER-TICKER × REGIME MATRIX")
    print("=" * 160)

    regimes = ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN"]
    header = "  %-8s" % "Ticker"
    for r in regimes:
        header += " | %-35s" % r
    print(header)

    sub_header = "  %-8s" % ""
    for r in regimes:
        sub_header += " | %5s %5s %5s %5s %9s" % ("N", "WR%", "Net", "G/PF", "Gross")
    print(sub_header)
    print("  " + "-" * 155)

    for ticker in ["SMLT", "NLMK", "GAZP", "LENT", "CHMF"]:
        if ticker not in by_ticker:
            continue
        trades = by_ticker[ticker]["trades"]
        row = "  %-8s" % ticker
        for r in regimes:
            subset = [t for t in trades if t["regime"] == r]
            n = len(subset)
            if n == 0:
                row += " | %5s %5s %5s %5s %9s" % ("-", "-", "-", "-", "-")
                continue
            wins_r = [t for t in subset if t["win"]]
            wr_r = len(wins_r) / n * 100
            net_r = sum(t["net"] for t in subset)
            gross_w = sum(t["gross"] for t in wins_r)
            gross_l = sum(t["gross"] for t in subset if not t["win"])
            pf_r = gross_w / abs(gross_l) if gross_l else 0
            row += " | %5d %4.0f%% %+5.0f %5.2f %+9.0f" % (n, wr_r, net_r, pf_r, gross_w + gross_l)
        print(row)

    # RSI per ticker (для выигрышных/проигрышных)
    print()
    print("=" * 160)
    print("  RSI ENTRY: WINS vs LOSSES per ticker")
    print("=" * 160)
    print("  %-8s %5s %6s %6s %6s %6s %6s %6s" % (
        "Ticker", "N", "RSI_W", "RSI_L", "ATR_W", "ATR_L", "EMA_W", "EMA_L"))
    print("  " + "-" * 70)

    for ticker in ["SMLT", "NLMK", "GAZP", "LENT", "CHMF"]:
        if ticker not in by_ticker:
            continue
        trades = by_ticker[ticker]["trades"]
        wins = [t for t in trades if t["win"]]
        losses = [t for t in trades if not t["win"]]
        rsi_w = avg(wins, "rsi_entry")
        rsi_l = avg(losses, "rsi_entry")
        atr_w = avg(wins, "atr_pct_entry")
        atr_l = avg(losses, "atr_pct_entry")
        ema_w = avg(wins, "ema_slope_entry")
        ema_l = avg(losses, "ema_slope_entry")
        print("  %-8s %5d %6.1f %6.1f %6.2f %6.2f %+6.3f %+6.3f" % (
            ticker, len(trades),
            rsi_w if rsi_w else 0, rsi_l if rsi_l else 0,
            atr_w if atr_w else 0, atr_l if atr_l else 0,
            ema_w if ema_w else 0, ema_l if ema_l else 0))

    # Vol per ticker
    print()
    print("=" * 160)
    print("  VOLUME RATIO: WINS vs LOSSES per ticker")
    print("=" * 160)
    print("  %-8s %5s %6s %6s %6s %6s" % (
        "Ticker", "N", "Vol_W", "Vol_L", "ADX_W", "ADX_L"))
    print("  " + "-" * 50)

    for ticker in ["SMLT", "NLMK", "GAZP", "LENT", "CHMF"]:
        if ticker not in by_ticker:
            continue
        trades = by_ticker[ticker]["trades"]
        wins = [t for t in trades if t["win"]]
        losses = [t for t in trades if not t["win"]]
        vol_w = avg(wins, "vol_ratio_entry")
        vol_l = avg(losses, "vol_ratio_entry")
        adx_w = avg(wins, "adx_entry")
        adx_l = avg(losses, "adx_entry")
        print("  %-8s %5d %6.2f %6.2f %6.1f %6.1f" % (
            ticker, len(trades),
            vol_w if vol_w else 0, vol_l if vol_l else 0,
            adx_w if adx_w else 0, adx_l if adx_l else 0))

    # SL/TP per ticker
    print()
    print("=" * 160)
    print("  SL/TP BREAKDOWN per ticker")
    print("=" * 160)
    print("  %-8s %5s %5s %5s %9s %7s %9s %7s %7s %7s" % (
        "Ticker", "SL_n", "TP_n", "SG_n", "SL_net", "SL_dist", "TP_net", "TP_dist", "SL_WR%", "TP_WR%"))
    print("  " + "-" * 90)

    for ticker in ["SMLT", "NLMK", "GAZP", "LENT", "CHMF"]:
        if ticker not in by_ticker:
            continue
        trades = by_ticker[ticker]["trades"]
        sl = [t for t in trades if t["exit_reason"] == "stop_loss"]
        tp = [t for t in trades if t["exit_reason"] == "target"]
        sg = [t for t in trades if t["exit_reason"] == "signal_exit"]
        sl_n = len(sl)
        tp_n = len(tp)
        sg_n = len(sg)
        sl_net = sum(t["net"] for t in sl)
        tp_net = sum(t["net"] for t in tp)
        sl_dist = avg(sl, "sl_dist_pct")
        tp_dist = avg(tp, "tp_dist_pct")
        sl_wr = sum(1 for t in sl if t["win"]) / sl_n * 100 if sl_n else 0
        tp_wr = sum(1 for t in tp if t["win"]) / tp_n * 100 if tp_n else 0
        print("  %-8s %5d %5d %5d %+9.0f %6.2f%% %+9.0f %6.2f%% %6.1f%% %6.1f%%" % (
            ticker, sl_n, tp_n, sg_n,
            sl_net, sl_dist if sl_dist else 0,
            tp_net, tp_dist if tp_dist else 0,
            sl_wr, tp_wr))

    # Exit quorum votes per ticker
    print()
    print("=" * 160)
    print("  EXIT QUORUM VOTES per ticker")
    print("=" * 160)
    for ticker in ["SMLT", "NLMK", "GAZP", "LENT", "CHMF"]:
        if ticker not in by_ticker:
            continue
        trades = by_ticker[ticker]["trades"]
        exit_votes = defaultdict(int)
        for t in trades:
            for v in t.get("quorum_exit_votes", []):
                exit_votes[v] += 1
        if exit_votes:
            top3 = sorted(exit_votes.items(), key=lambda x: -x[1])[:5]
            vote_str = " ".join("%s:%d" % (s.split("_")[0], c) for s, c in top3)
            print("  %-8s %s" % (ticker, vote_str))
        else:
            print("  %-8s (no exit votes)" % ticker)


def print_regime_report(all_trades):
    """Print detailed regime × win/loss × indicators report."""
    by_regime = defaultdict(lambda: {"trades": [], "wins": [], "losses": []})
    for t in all_trades:
        r = t["regime"]
        by_regime[r]["trades"].append(t)
        if t["win"]:
            by_regime[r]["wins"].append(t)
        else:
            by_regime[r]["losses"].append(t)

    def avg(lst, key):
        vals = [t[key] for t in lst if t[key] is not None]
        return sum(vals) / len(vals) if vals else None

    print()
    print("=" * 140)
    print("  REGIME BREAKDOWN: indicators + quorum votes + win/loss")
    print("=" * 140)

    for regime in ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE"]:
        if regime not in by_regime:
            continue
        data = by_regime[regime]
        trades = data["trades"]
        wins = data["wins"]
        losses = data["losses"]
        n = len(trades)
        wr = len(wins) / n * 100 if n else 0
        net = sum(t["net"] for t in trades)
        net_t = net / n if n else 0

        print()
        print("  --- %s (%d trades, WR %.1f%%, net %+.0f, net/t %+.0f) ---" % (
            regime, n, wr, net, net_t))

        # Indicator averages: all / wins / losses
        for label, subset in [("ALL", trades), ("WINS", wins), ("LOSSES", losses)]:
            if not subset:
                continue
            rsi_e = avg(subset, "rsi_entry")
            atr_e = avg(subset, "atr_pct_entry")
            ema_e = avg(subset, "ema_slope_entry")
            adx_e = avg(subset, "adx_entry")
            vol_e = avg(subset, "vol_ratio_entry")
            rsi_x = avg(subset, "rsi_exit")
            mfe = avg(subset, "mfe_r")
            mae = avg(subset, "mae_r")
            bh = avg(subset, "bars_held")
            print("    %-8s RSI_entry=%6.1f  ATR%%=%5.2f  EMA_slope=%+6.3f  ADX=%5.1f  Vol=%5.2f  |  RSI_exit=%6.1f  MFE=%.2f  MAE=%.2f  bars=%.0f" % (
                label,
                rsi_e if rsi_e else 0,
                atr_e if atr_e else 0,
                ema_e if ema_e else 0,
                adx_e if adx_e else 0,
                vol_e if vol_e else 0,
                rsi_x if rsi_x else 0,
                mfe if mfe else 0,
                mae if mae else 0,
                bh if bh else 0,
            ))

        # Quorum vote distribution
        vote_counts = defaultdict(int)
        for t in trades:
            for v in t["quorum_votes"]:
                vote_counts[v] += 1
        if vote_counts:
            print("    Quorum votes (strategy → count):")
            for sid, cnt in sorted(vote_counts.items(), key=lambda x: -x[1]):
                pct = cnt / n * 100
                win_cnt = sum(1 for t in wins if sid in t["quorum_votes"])
                lose_cnt = sum(1 for t in losses if sid in t["quorum_votes"])
                win_wr = win_cnt / cnt * 100 if cnt else 0
                print("      %-30s %4d (%5.1f%%)  WR_of_votes=%5.1f%%" % (
                    sid, cnt, pct, win_wr))

        # Flip vs non-flip
        flips = [t for t in trades if t["is_flip"]]
        non_flips = [t for t in trades if not t["is_flip"]]
        if flips:
            fl_wr = sum(1 for t in flips if t["win"]) / len(flips) * 100
            fl_net = sum(t["net"] for t in flips)
            print("    Flips: %d trades, WR %.1f%%, net %+.0f" % (len(flips), fl_wr, fl_net))
        if non_flips:
            nf_wr = sum(1 for t in non_flips if t["win"]) / len(non_flips) * 100
            nf_net = sum(t["net"] for t in non_flips)
            print("    Non-flips: %d trades, WR %.1f%%, net %+.0f" % (len(non_flips), nf_wr, nf_net))

        # Exit reasons + SL/TP breakdown
        by_reason = defaultdict(lambda: {"n": 0, "net": 0.0, "wins": 0, "sl_dist": [], "tp_dist": []})
        for t in trades:
            r = t["exit_reason"]
            by_reason[r]["n"] += 1
            by_reason[r]["net"] += t["net"]
            if t["win"]:
                by_reason[r]["wins"] += 1
            if t["sl_dist_pct"] is not None:
                by_reason[r]["sl_dist"].append(t["sl_dist_pct"])
            if t["tp_dist_pct"] is not None:
                by_reason[r]["tp_dist"].append(t["tp_dist_pct"])
        print("    Exit reasons:")
        for reason, rd in sorted(by_reason.items(), key=lambda x: -x[1]["n"]):
            rwr = rd["wins"] / rd["n"] * 100 if rd["n"] else 0
            sl_avg = sum(rd["sl_dist"]) / len(rd["sl_dist"]) if rd["sl_dist"] else 0
            tp_avg = sum(rd["tp_dist"]) / len(rd["tp_dist"]) if rd["tp_dist"] else 0
            print("      %-25s %4d  WR=%5.1f%%  net=%+10.0f  SL_dist=%5.2f%%  TP_dist=%5.2f%%" % (
                reason, rd["n"], rwr, rd["net"], sl_avg, tp_avg))

        # Exit quorum votes
        exit_vote_counts = defaultdict(int)
        for t in trades:
            for v in t.get("quorum_exit_votes", []):
                exit_vote_counts[v] += 1
        if exit_vote_counts:
            print("    Exit quorum votes (strategy → count):")
            for sid, cnt in sorted(exit_vote_counts.items(), key=lambda x: -x[1]):
                print("      %-30s %4d" % (sid, cnt))

        # SL/TP summary
        sl_trades = [t for t in trades if t["exit_reason"] == "stop_loss"]
        tp_trades = [t for t in trades if t["exit_reason"] == "target"]
        print("    SL/TP summary:")
        if sl_trades:
            sl_avg_dist = sum(t["sl_dist_pct"] for t in sl_trades if t["sl_dist_pct"]) / len(sl_trades)
            sl_net = sum(t["net"] for t in sl_trades)
            print("      SL: %d trades, avg_dist=%.2f%%, net=%+.0f" % (len(sl_trades), sl_avg_dist, sl_net))
        if tp_trades:
            tp_avg_dist = sum(t["tp_dist_pct"] for t in tp_trades if t["tp_dist_pct"]) / len(tp_trades)
            tp_net = sum(t["net"] for t in tp_trades)
            print("      TP: %d trades, avg_dist=%.2f%%, net=%+.0f" % (len(tp_trades), tp_avg_dist, tp_net))


def print_flip_analysis(all_trades):
    """Analyze flip events specifically."""
    flips = [t for t in all_trades if t["is_flip"]]
    non_flips = [t for t in all_trades if not t["is_flip"]]

    print()
    print("=" * 140)
    print("  FLIP ANALYSIS: %d flips vs %d non-flips" % (len(flips), len(non_flips)))
    print("=" * 140)

    if not flips:
        print("  No flips found."); return

    # Flip indicators at entry
    for label, subset in [("FLIPS", flips), ("NON-FLIPS", non_flips)]:
        if not subset:
            continue
        n = len(subset)
        wr = sum(1 for t in subset if t["win"]) / n * 100
        net = sum(t["net"] for t in subset)
        rsi_e = sum(t["rsi_entry"] for t in subset if t["rsi_entry"] is not None) / max(1, sum(1 for t in subset if t["rsi_entry"] is not None))
        atr_e = sum(t["atr_pct_entry"] for t in subset if t["atr_pct_entry"] is not None) / max(1, sum(1 for t in subset if t["atr_pct_entry"] is not None))
        ema_e = sum(t["ema_slope_entry"] for t in subset if t["ema_slope_entry"] is not None) / max(1, sum(1 for t in subset if t["ema_slope_entry"] is not None))
        print("  %-10s %4d trades  WR=%5.1f%%  net=%+10.0f  |  RSI=%5.1f  ATR%%=%5.2f  EMA=%+6.3f" % (
            label, n, wr, net, rsi_e, atr_e, ema_e))

    # Flip by regime
    print()
    print("  Flips by regime:")
    by_regime = defaultdict(list)
    for t in flips:
        by_regime[t["regime"]].append(t)
    for regime in ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN"]:
        if regime in by_regime:
            subset = by_regime[regime]
            n = len(subset)
            wr = sum(1 for t in subset if t["win"]) / n * 100
            net = sum(t["net"] for t in subset)
            print("    %-18s %4d  WR=%5.1f%%  net=%+10.0f" % (regime, n, wr, net))

    # Flip quorum votes
    print()
    print("  Flip quorum vote distribution:")
    vote_counts = defaultdict(int)
    for t in flips:
        for v in t["quorum_votes"]:
            vote_counts[v] += 1
    for sid, cnt in sorted(vote_counts.items(), key=lambda x: -x[1]):
        print("    %-30s %4d" % (sid, cnt))


def print_indicator_distributions(all_trades):
    """Print RSI/ATR/EMA distributions for win vs loss."""
    print()
    print("=" * 140)
    print("  INDICATOR DISTRIBUTIONS: win vs loss (entry values)")
    print("=" * 140)

    wins = [t for t in all_trades if t["win"]]
    losses = [t for t in all_trades if not t["win"]]

    def histogram(vals, bins):
        counts = [0] * len(bins)
        for v in vals:
            for i in range(len(bins) - 1):
                if bins[i] <= v < bins[i+1]:
                    counts[i] += 1
                    break
            else:
                if v >= bins[-1]:
                    counts[-1] += 1
        return counts

    # RSI histogram
    rsi_bins = list(range(10, 91, 10))
    rsi_w = [t["rsi_entry"] for t in wins if t["rsi_entry"] is not None]
    rsi_l = [t["rsi_entry"] for t in losses if t["rsi_entry"] is not None]
    w_counts = histogram(rsi_w, rsi_bins)
    l_counts = histogram(rsi_l, rsi_bins)
    print()
    print("  RSI entry distribution:")
    print("  RSI_range    Wins   Losses   W/L ratio")
    for i in range(len(rsi_bins) - 1):
        w, l = w_counts[i], l_counts[i]
        ratio = w / l if l else float('inf')
        print("  %3d-%3d     %5d  %6d    %5.2f" % (rsi_bins[i], rsi_bins[i+1], w, l, ratio))

    # ATR% histogram
    atr_bins = [0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 5.0, 100]
    atr_w = [t["atr_pct_entry"] for t in wins if t["atr_pct_entry"] is not None]
    atr_l = [t["atr_pct_entry"] for t in losses if t["atr_pct_entry"] is not None]
    w_counts = histogram(atr_w, atr_bins)
    l_counts = histogram(atr_l, atr_bins)
    print()
    print("  ATR% entry distribution:")
    print("  ATR%_range   Wins   Losses   W/L ratio")
    for i in range(len(atr_bins) - 1):
        w, l = w_counts[i], l_counts[i]
        ratio = w / l if l else float('inf')
        print("  %5.1f-%5.1f   %5d  %6d    %5.2f" % (atr_bins[i], atr_bins[i+1], w, l, ratio))


async def main():
    tickers = sys.argv[1].split(",") if len(sys.argv) > 1 else ["SMLT", "NLMK", "GAZP", "LENT", "CHMF"]
    eligible = await get_eligible()
    by_ticker = {t: (f, l) for f, t, l in eligible}

    all_trades = []
    for ticker in tickers:
        if ticker not in by_ticker:
            print("  %s: NOT in eligible" % ticker); continue
        figi, lot = by_ticker[ticker]
        print("Loading %s..." % ticker, end=" ", flush=True)
        candles = await load_august(figi)
        print("%d candles" % len(candles))

        enriched = analyze_ticker(figi, ticker, candles, lot)
        if enriched:
            all_trades.extend(enriched)
            print("  → %d trades" % len(enriched))

    if not all_trades:
        print("No trades!"); return

    # Overall stats
    n = len(all_trades)
    wins = sum(1 for t in all_trades if t["win"])
    net = sum(t["net"] for t in all_trades)
    print()
    print("=" * 140)
    print("  TOTAL: %d trades, WR %.1f%%, net %+.0f" % (n, wins/n*100, net))

    # Print reports
    print_ticker_report(all_trades)
    print_regime_report(all_trades)
    print_flip_analysis(all_trades)
    print_indicator_distributions(all_trades)

    # Save to JSON
    out_path = os.path.join(os.path.dirname(__file__), "..", "s5_diagnostics.json")
    with open(out_path, "w") as f:
        json.dump(all_trades, f, indent=2, default=str)
    print()
    print("  Saved to %s" % out_path)


if __name__ == "__main__":
    asyncio.run(main())
