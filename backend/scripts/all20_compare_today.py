#!/usr/bin/env python3
"""Эталон compute_ensemble по всем 20 eligible тикерам за период.
Параметры из optuna_params_results.json: setups = ВСЕ 7 стратегий с
best_strat_params из файла, кворум фиксирован = 2, sl_mult/rr/vol_thr из файла.
Опция --bot: сравнение с sandbox_trades бота за тот же период.
НЕ меняет движок.

Пример: python scripts/all20_compare_today.py --from 2026-06-01 --to 2026-07-01
"""
import asyncio, json, os, argparse
from datetime import datetime, timezone, timedelta
from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

ALL = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
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
DAYS = 30  # прогрев по умолчанию

_script_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_script_dir, "optuna_params_results.json")) as _f:
    OPT = json.load(_f)  # ticker -> {best_params, best_strat_params, ...}


def build_req(figi, lot, opt, candles, entry_confirm=None):
    sp = {k: dict(v) for k, v in (opt.get("best_strat_params") or {}).items()}
    setups = [{"strategy_id": s, "tf": "5min",
               "params": dict(sp.get(s, V2P.get(s, {})))} for s in ALL]
    bp = opt.get("best_params") or {}
    sl_mult = float(bp.get("sl_mult", 4.0))
    rr = float(bp.get("rr", 4.0))
    quorum = 2
    vol_thr = float(bp.get("vol_thr", 0.0) or 0.0)
    if entry_confirm is None:
        entry_confirm = int(bp.get("entry_confirm_bars", 0) or 0)
    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "all", "quorum": quorum,
        "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop",
                        "params": {"period": 14, "multiplier": sl_mult, "risk_reward": rr}},
        "commission_rate": 0.0005, "slippage_bps": 2.0,
        "capital": 10000, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
        "neutral_mode": "semi_flip",
        "from_ts": candles[0].ts.isoformat(), "to_ts": candles[-1].ts.isoformat(),
    }
    if entry_confirm and entry_confirm > 0:
        req["entry_confirm_bars"] = entry_confirm
    if vol_thr and vol_thr > 0:
        req["volume_filter_threshold"] = vol_thr
    return req


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD (exclusive)")
    ap.add_argument("--date-only", help="YYYY-MM-DD: показывать сделки только этого дня")
    ap.add_argument("--entry-confirm", type=int, default=None,
                    help="Форсировать entry_confirm_bars=N (дефолт: из optuna-файла)")
    args = ap.parse_args()

    f_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    f_to = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    warmup_from = f_from - timedelta(days=DAYS)
    date_only = args.date_only or args.date_from
    bot_days = (f_to - f_from).days

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT i.ticker, i.figi, i.lot, i.optuna_params
            FROM instruments i
            JOIN universe u ON u.figi = i.figi
            WHERE u.eligible_tier = 'eligible'
            ORDER BY i.ticker
        """))).fetchall()

        bot_rows = (await db.execute(text("""
            SELECT ticker, side, entry_time, entry_price, exit_time, exit_price,
                   exit_reason, net_pnl
            FROM sandbox_trades WHERE entry_time >= :f AND entry_time < :t
            ORDER BY ticker, entry_time
        """), {"f": f_from, "t": f_to})).fetchall()
        bot_by_tk = {}
        for x in bot_rows:
            bot_by_tk.setdefault(x[0], []).append(x)

    print("=" * 100)
    print("ЭТАЛОН compute_ensemble %s..%s (все 7 стратегий, кворум 2, optuna-файл)" % (
        args.date_from, args.date_to))
    print("=" * 100)
    header = "%-7s | %8s | %5s | %5s | %8s | %-46s | %-8s" % (
        "ticker", "net", "trades", "wins", "PF", "сделки (entry exit side net)", "бот n/net")
    print(header)
    print("-" * 100)

    all_net = 0.0
    all_tr = 0
    for ticker, figi, lot, optraw in rows:
        opt = OPT.get(ticker, {})
        if not opt:
            print(f"{ticker}: нет в optuna_params_results.json — пропуск")
            continue
        try:
            candles = await _lc(db, figi, 1, date_from=warmup_from, date_to=f_to)
        except Exception as e:
            print(f"{ticker}: load err {e}")
            continue
        if not candles:
            print(f"{ticker}: no candles")
            continue
        req = build_req(figi, lot, opt, candles, args.entry_confirm)
        try:
            res = compute_ensemble(candles, req)
        except Exception as e:
            print(f"{ticker}: ENGINE ERR {str(e)[:80]}")
            continue
        if "error" in res:
            print(f"{ticker}: {res['error']}")
            continue
        st = res.get("static", {})
        trades = [t for t in st.get("trades", [])
                  if args.date_from <= t["entry_ts"][:10] < args.date_to]
        if date_only:
            trades = [t for t in trades if t["entry_ts"][:10] == date_only]
        net = sum(t["net"] for t in trades)
        wins = sum(1 for t in trades if t["net"] > 0)
        gwin = sum(t["net"] for t in trades if t["net"] > 0)
        glos = abs(sum(t["net"] for t in trades if t["net"] <= 0))
        pf = gwin / glos if glos > 0 else 0.0
        tdesc = []
        for t in sorted(trades, key=lambda x: x["entry_ts"]):
            tdesc.append(f"{t['side'][0]}{t['entry_ts'][5:16].replace('-', '/')}-{t['exit_ts'][11:16]}:{t['net']:.0f}")
        tstr = "; ".join(tdesc) if tdesc else "—"
        all_net += net
        all_tr += len(trades)
        bs = bot_by_tk.get(ticker, [])
        bnet = sum((x[7] or 0) for x in bs)
        bstr = f"{len(bs)}/{bnet:+.0f}" if bs and bot_days == 1 else (f"{len(bs)}/{bnet:+.0f}" if bs else "0")
        print("%-7s | %+8.0f | %5d | %5d | %7.2f | %-46s | %-8s" % (
            ticker, net, len(trades), wins, pf, tstr[:46], bstr))
        if date_only and bs and bot_days == 1:
            for x in bs:
                print("     бот: %-4s %s -> %s e=%s x=%s %-12s net=%s" % (
                    x[0], x[2].astimezone(timezone.utc).strftime("%H:%M"),
                    x[4].astimezone(timezone.utc).strftime("%H:%M") if x[4] else "--",
                    x[3], x[5] if x[5] else "--", x[6] or "OPEN",
                    round(x[7], 2) if x[7] is not None else None))
        print()
    print("-" * 100)
    print("ИТОГО: net=%+.0f  сделок=%d" % (all_net, all_tr))


if __name__ == "__main__":
    asyncio.run(main())