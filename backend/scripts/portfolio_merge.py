#!/usr/bin/env python3
"""Портфельная сборка: объединить per-ticker сделки на общем пуле.

Правила:
- Общий капитал POOL (10K). Каждая позиция может занять до POS_PCT*equity.
- Сделки сортируются по entry_ts. На входе: если cash >= стоимость -> открыть,
  иначе сделка ПРОПУСКАЕТСЯ (сигнал был, денег нет).
- qty = floor(budget / (entry_px*lot)) * lot, минимум 1 лот.
- На exit_ts закрываем по exit_px (реализация по сигналу/стопу из compute_ensemble).
- Позиции одного тикера не пересекаются (compute_ensemble их уже исключил),
  но при пропуске входа сделка отбрасывается целиком.
- Режим semi_flip учитывается на уровне сигналов (уже в дампе).

Вывод: полный отчёт (per-ticker, per-regime, daily equity, макс. одновременные,
пропущенные входы из-за нехватки cash).
"""
import argparse, json, os, sys
from collections import defaultdict
from datetime import datetime, timezone, date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

MSK_OFF = timezone.utc  # даты в UTC корректны для группировки; для МСК - см. ниже
from zoneinfo import ZoneInfo
MSK = ZoneInfo("Europe/Moscow")


def load_trades(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_margin_map():
    """Загрузить per-ticker плечо из instrument_margin (figi -> (long_lev, short_lev))."""
    import asyncio
    from sqlalchemy import text
    from app.database import SessionLocal

    out = {}
    async def _q():
        async with SessionLocal() as db:
            rows = (await db.execute(text(
                "SELECT figi, long_lev, short_lev FROM instrument_margin"))).fetchall()
            for r in rows:
                out[r.figi] = (float(r[1] or 1.0), float(r[2] or 1.0))
    asyncio.run(_q())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True, help="jsonl от per_ticker_dump.py")
    ap.add_argument("--pool", type=float, default=10_000.0)
    ap.add_argument("--pos-pct", type=float, default=0.20)
    ap.add_argument("--comm", type=float, default=0.0005)
    ap.add_argument("--slip-bps", type=float, default=2.0)
    ap.add_argument("--lev", type=float, default=1.0, help="фикс. плечо (1 = без маржи)")
    ap.add_argument("--margin", action="store_true",
                    help="реальная маржа: плечо per-ticker из instrument_margin (long/short)")
    args = ap.parse_args()

    trades = load_trades(args.trades)
    print("Loaded %d trades from %s" % (len(trades), args.trades))
    if not trades:
        return

    margin_map = load_margin_map() if args.margin else {}
    if args.margin:
        print("Margin mode: per-ticker leverage from instrument_margin (%d figis)" % len(margin_map))

    # нормализация времени
    for t in trades:
        t["ets"] = datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00"))
        t["xts"] = datetime.fromisoformat(t["exit_ts"].replace("Z", "+00:00"))

    # сортировка входов по времени
    trades.sort(key=lambda t: t["ets"])

    cash = args.pool
    equity = args.pool
    positions = {}  # ticker -> {trade, qty_shares, entry_cost}
    skipped = {"no_cash": 0, "no_lot": 0}
    realized = []  # закрытые с полным net
    per_ticker = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0.0, "gw": 0.0, "gl": 0.0})
    entry_log = []

    def mark_result(tk, net, side):
        d = per_ticker[tk]
        d["n"] += 1
        d["net"] += net
        if net > 0:
            d["wins"] += 1
            d["gw"] += net
        else:
            d["gl"] += net

    # проходим по событиям: сначала все входы, потом выходы в порядке времени.
    # Используем единую очередь событий.
    events = []
    for idx, t in enumerate(trades):
        events.append((t["ets"], "entry", idx, t))
        events.append((t["xts"], "exit", idx, t))
    events.sort(key=lambda e: (e[0], 0 if e[1] == "exit" else 1))  # exit раньше при равенстве

    for ts, kind, idx, t in events:
        tk = t["ticker"]
        lot = int(t["lot"])
        if kind == "entry":
            if tk in positions:
                continue  # уже открыта (не должно быть, но защита)
            # equity = pool + сумма реализованного net (текущий капитал)
            equity = args.pool + sum(r["net"] for r in realized)
            if args.margin:
                _ll, _sl = margin_map.get(t.get("figi", t["ticker"]), (1.0, 1.0))
                lev = _ll if t["side"] == "LONG" else _sl
            else:
                lev = args.lev
            # Лимит позиции: notional <= equity * pos_pct (доля портфеля на 1 акцию)
            max_notional = equity * args.pos_pct
            cost_per_lot = t["entry_px"] * lot
            if cost_per_lot <= 0:
                skipped["no_lot"] += 1
                continue
            # Сколько лотов можем по лимиту позиции (не по бюджету с плечом)
            lots_limit = int(max_notional / cost_per_lot)
            if lots_limit < 1:
                skipped["no_lot"] += 1
                entry_log.append((tk, t["ets"], 0, "no_lot"))
                continue
            lots = lots_limit
            qty_shares = lots * lot
            notional = qty_shares * t["entry_px"]
            slip = qty_shares * t["entry_px"] * args.slip_bps / 1e4
            comm = qty_shares * t["entry_px"] * args.comm
            is_short = t["side"] == "SHORT"
            # Обеспечение: сколько cash замораживаем под позицию (и LONG и SHORT)
            margin_req = notional / lev
            # Свободный cash = пул + реализованная прибыль - Σ(обеспечения открытых)
            used = sum(p["margin_req"] for p in positions.values())
            realized_sum = sum(r["net"] for r in realized)
            free_cash = args.pool + realized_sum - used
            if margin_req + comm + slip > free_cash:
                # уменьшить лоты до доступного обеспечения
                avail_lots = int((free_cash - comm - slip) * lev / cost_per_lot) if lev > 0 else 0
                if avail_lots < 1:
                    skipped["no_cash"] += 1
                    entry_log.append((tk, t["ets"], 0, "no_cash"))
                    continue
                lots = avail_lots
                qty_shares = lots * lot
                notional = qty_shares * t["entry_px"]
                slip = qty_shares * t["entry_px"] * args.slip_bps / 1e4
                comm = qty_shares * t["entry_px"] * args.comm
                margin_req = notional / lev
            positions[tk] = {"t": t, "qty": qty_shares, "margin_req": margin_req,
                             "notional": notional, "is_short": is_short,
                             "entry_cash": margin_req, "lev": lev}
            entry_log.append((tk, t["ets"], qty_shares, "ok"))
        else:
            pos = positions.pop(tk, None)
            if pos is None:
                continue
            t = pos["t"]
            qty = pos["qty"]
            is_short = pos.get("is_short", t["side"] == "SHORT")
            side_mult = -1 if is_short else 1
            gross = (t["exit_px"] - t["entry_px"]) * qty * side_mult
            slip_entry = qty * t["entry_px"] * args.slip_bps / 1e4
            slip_exit = qty * t["exit_px"] * args.slip_bps / 1e4
            comm_entry = qty * t["entry_px"] * args.comm
            comm_exit = qty * t["exit_px"] * args.comm
            net = gross - comm_entry - comm_exit - slip_entry - slip_exit
            # Освобождаем обеспечение + добавляем PnL
            # (обеспечение вернулось, PnL в equity через realized)
            realized.append({**t, "qty_shares": qty, "gross": gross, "net": net,
                             "comm": comm_entry + comm_exit, "slip": slip_entry + slip_exit,
                             "margin_req": pos["margin_req"]})
            mark_result(tk, net, t["side"])

    # report
    total_net = sum(r["net"] for r in realized)
    print("=" * 110)
    _lev_label = "margin(per-ticker)" if args.margin else "%.1fx" % args.lev
    print("PORTFOLIO MERGE  | pool=%s | pos=%.0f%% | lev=%s | comm=%.2f%% | slip=%dbps" % (
        format(args.pool, ",.0f"), args.pos_pct * 100, _lev_label, args.comm * 100, args.slip_bps))
    print("=" * 110)
    print("Trades executed: %d / %d signals (%.0f%%)" % (
        len(realized), len(trades), len(realized) / len(trades) * 100 if trades else 0))
    print("Skipped: no_cash=%d no_lot=%d" % (skipped["no_cash"], skipped["no_lot"]))
    wins = [r for r in realized if r["net"] > 0]
    losses = [r for r in realized if r["net"] <= 0]
    gw = sum(r["net"] for r in wins)
    gl = sum(r["net"] for r in losses)
    print("Net: %+.2f | Final equity: %.2f | Return: %+.1f%%" % (total_net, args.pool + total_net, total_net / args.pool * 100))
    print("WR: %.1f%% | PF: %.2f" % (len(wins) / len(realized) * 100 if realized else 0,
                                     gw / abs(gl) if gl else 0))
    print("Gross win: %+.2f (avg %+.2f) | Gross loss: %+.2f (avg %+.2f)" % (
        gw, gw / len(wins) if wins else 0, gl, gl / len(losses) if losses else 0))

    # per ticker
    print()
    print("Per-ticker:")
    print("  %-7s %6s %5s %5s %6s %10s %10s %10s %6s %8s" % (
        "Ticker", "Exec", "Wins", "Skip", "WR%", "GrossWin", "GrossLoss", "Net", "PF", "Avg"))
    all_tk = sorted(per_ticker.keys(), key=lambda x: -per_ticker[x]["net"])
    for tk in all_tk:
        d = per_ticker[tk]
        skip = sum(1 for e in entry_log if e[0] == tk and e[2] == 0)
        d["skip"] = skip
    for tk in all_tk:
        d = per_ticker[tk]
        skip = d.get("skip", 0)
        wr = d["wins"] / d["n"] * 100 if d["n"] else 0
        pf = d["gw"] / abs(d["gl"]) if d["gl"] else 0
        print("  %-7s %6d %5d %5d %5.1f%% %+10.2f %+10.2f %+10.2f %6.2f %+8.2f" % (
            tk, d["n"], d["wins"], skip, wr, d["gw"], d["gl"], d["net"], pf,
            d["net"] / d["n"] if d["n"] else 0))

    # per regime
    print()
    print("Per-regime (вход):")
    by_reg = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0.0})
    for r in realized:
        rg = r.get("regime", "UNKNOWN")
        by_reg[rg]["n"] += 1
        by_reg[rg]["net"] += r["net"]
        if r["net"] > 0:
            by_reg[rg]["wins"] += 1
    for rg in ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE", "UNKNOWN"]:
        if rg not in by_reg:
            continue
        d = by_reg[rg]
        print("  %-18s %5d WR=%.0f%% net=%+9.2f (%.2f/trade)" % (
            rg, d["n"], d["wins"] / d["n"] * 100 if d["n"] else 0, d["net"],
            d["net"] / d["n"] if d["n"] else 0))

    # daily equity
    print()
    print("Daily (МСК):")
    day_net = defaultdict(float)
    for r in realized:
        d = r["xts"].astimezone(MSK).date().isoformat()
        day_net[d] += r["net"]
    cum = args.pool
    print("  %-12s %10s %10s" % ("Date", "DayNet", "Equity"))
    for d in sorted(day_net.keys()):
        cum += day_net[d]
        print("  %-12s %+10.2f %10.2f" % (d, day_net[d], cum))

    # concurrent positions
    evs = []
    for r in realized:
        evs.append((r["xts"], -1))
        evs.append((r["ets"], 1))
    evs.sort(key=lambda e: e[0])
    cur = 0
    mx = 0
    for _, d in evs:
        cur += d
        mx = max(mx, cur)
    print()
    print("Max concurrent positions: %d" % mx)

    # save executed
    out = args.trades.replace(".jsonl", "_exec.jsonl")
    with open(out, "w") as f:
        for r in realized:
            f.write(json.dumps({k: r[k] for k in [
                "ticker", "side", "entry_ts", "exit_ts", "entry_px", "exit_px",
                "exit_reason", "regime", "qty_shares", "gross", "net", "comm", "slip"]}) + "\n")
    print()
    print("Executed trades saved -> %s" % out)


if __name__ == "__main__":
    main()
