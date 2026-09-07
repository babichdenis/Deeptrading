"""Detailed sandbox trading results."""
import asyncio, json
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import SessionLocal


async def main():
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT figi, ticker, side, entry_price, exit_price, net_pnl, "
            "entry_time, exit_reason, exit_meta "
            "FROM sandbox_trades ORDER BY entry_time"
        ))).fetchall()

    if not rows:
        print("NO TRADES")
        return

    total = len(rows)
    wins = [r for r in rows if r[5] and float(r[5]) > 0]
    losses = [r for r in rows if r[5] and float(r[5]) <= 0]
    gross_win = sum(float(r[5]) for r in wins)
    gross_loss = abs(sum(float(r[5]) for r in losses))
    net = gross_win - gross_loss
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr = len(wins) / total * 100 if total > 0 else 0

    from zoneinfo import ZoneInfo
    first = rows[0][6].astimezone(ZoneInfo("Europe/Moscow"))
    last = rows[-1][6].astimezone(ZoneInfo("Europe/Moscow"))

    # Per-day breakdown
    days = {}
    for r in rows:
        d = r[6].astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d")
        if d not in days:
            days[d] = {"count": 0, "wins": 0, "pnl": 0.0}
        days[d]["count"] += 1
        if r[5] and float(r[5]) > 0:
            days[d]["wins"] += 1
        days[d]["pnl"] += float(r[5]) if r[5] else 0

    # Per-ticker
    tickers = {}
    for r in rows:
        t = r[1] or r[0][-6:]
        if t not in tickers:
            tickers[t] = {"count": 0, "wins": 0, "pnl": 0.0}
        tickers[t]["count"] += 1
        if r[5] and float(r[5]) > 0:
            tickers[t]["wins"] += 1
        tickers[t]["pnl"] += float(r[5]) if r[5] else 0

    # Per-side
    sides = {}
    for r in rows:
        s = r[2]
        if s not in sides:
            sides[s] = {"count": 0, "wins": 0, "pnl": 0.0}
        sides[s]["count"] += 1
        if r[5] and float(r[5]) > 0:
            sides[s]["wins"] += 1
        sides[s]["pnl"] += float(r[5]) if r[5] else 0

    # Per-exit_reason
    reasons = {}
    for r in rows:
        reason = r[7] or "unknown"
        if reason not in reasons:
            reasons[reason] = {"count": 0, "wins": 0, "pnl": 0.0}
        reasons[reason]["count"] += 1
        if r[5] and float(r[5]) > 0:
            reasons[reason]["wins"] += 1
        reasons[reason]["pnl"] += float(r[5]) if r[5] else 0

    # Per-ticker per-day
    ticker_days = {}
    for r in rows:
        t = r[1] or r[0][-6:]
        d = r[6].astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d")
        key = (t, d)
        if key not in ticker_days:
            ticker_days[key] = {"count": 0, "wins": 0, "pnl": 0.0}
        ticker_days[key]["count"] += 1
        if r[5] and float(r[5]) > 0:
            ticker_days[key]["wins"] += 1
        ticker_days[key]["pnl"] += float(r[5]) if r[5] else 0

    days_count = len(days)
    print("=" * 80)
    print("SANDBOX TRADING RESULTS (T-Investments, 10k RUB)")
    print("=" * 80)
    print("Period: %s .. %s" % (first.strftime("%Y-%m-%d %H:%M"), last.strftime("%Y-%m-%d %H:%M")))
    print("Days with trades: %d" % days_count)
    print()
    print("--- SUMMARY ---")
    print("Total trades:  %d" % total)
    print("Wins:          %d (%.1f%%)" % (len(wins), wr))
    print("Losses:        %d (%.1f%%)" % (len(losses), 100 - wr))
    print("Gross Win:    +%s" % f"{gross_win:.2f}")
    print("Gross Loss:   -%s" % f"{gross_loss:.2f}")
    print("Net:           %s" % f"{net:+.2f}")
    print("PF:            %s" % f"{pf:.2f}")
    print()

    print("--- PER DAY ---")
    total_days = 0
    positive_days = 0
    negative_days = 0
    for d in sorted(days.keys()):
        v = days[d]
        w = v["wins"]
        c = v["count"]
        pnl = v["pnl"]
        day_wr = w / c * 100 if c > 0 else 0
        total_days += 1
        if pnl >= 0:
            positive_days += 1
        else:
            negative_days += 1
        print("  %s  trades=%3d  wins=%2d  WR=%5.1f%%  PnL=%+.2f" % (d, c, w, day_wr, pnl))
    print()
    print("  Positive days: %d / %d" % (positive_days, total_days))
    print("  Negative days: %d / %d" % (negative_days, total_days))
    print()

    print("--- PER TICKER ---")
    for t, v in sorted(tickers.items(), key=lambda x: -x[1]["pnl"]):
        w = v["wins"]
        c = v["count"]
        pnl = v["pnl"]
        t_wr = w / c * 100 if c > 0 else 0
        print("  %12s  trades=%3d  wins=%2d  WR=%5.1f%%  PnL=%+.2f" % (t, c, w, t_wr, pnl))
    print()

    print("--- PER SIDE ---")
    for s, v in sorted(sides.items(), key=lambda x: -x[1]["pnl"]):
        w = v["wins"]
        c = v["count"]
        pnl = v["pnl"]
        s_wr = w / c * 100 if c > 0 else 0
        print("  %6s  trades=%3d  wins=%2d  WR=%5.1f%%  PnL=%+.2f" % (s, c, w, s_wr, pnl))
    print()

    print("--- PER EXIT REASON ---")
    for reason, v in sorted(reasons.items(), key=lambda x: -x[1]["pnl"]):
        w = v["wins"]
        c = v["count"]
        pnl = v["pnl"]
        r_wr = w / c * 100 if c > 0 else 0
        print("  %20s  trades=%3d  wins=%2d  WR=%5.1f%%  PnL=%+.2f" % (reason, c, w, r_wr, pnl))
    print()

    # Avg win / avg loss
    avg_win = gross_win / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0
    print("--- ADDITIONAL ---")
    print("Avg win:   +%.2f" % avg_win)
    print("Avg loss:  -%.2f" % avg_loss)
    print("Win/Loss ratio: %.2f" % (avg_win / avg_loss if avg_loss > 0 else float("inf")))
    print("=" * 80)

asyncio.run(main())
