"""Parity harness: реплей дня из БД через runtime (paper) vs фактические сделки бота.

Сравниваем РЕШЕНИЯ (ticker, side, ts входа) — как близко реплей воспроизводит
реальное поведение бота за тот же день.

Usage:
  .venv/bin/python3 scripts/parity_replay.py --date 2026-09-10 --mode live
  .venv/bin/python3 scripts/parity_replay.py --date 2026-09-08 --mode sandbox --top_n 20
"""
import argparse
import asyncio
import os
import sys
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.database import SessionLocal

_W = sys.stdout
_MSK = "Europe/Moscow"


async def _fetch_real(db, mode, broker_account_id, t0, t1):
    r = await db.execute(text(
        "SELECT ticker, side, entry_time, entry_price, exit_time, net_pnl "
        "FROM sandbox_trades WHERE mode = :m AND broker_account_id = :account "
        "AND entry_time >= :t0 AND entry_time < :t1 "
        "ORDER BY entry_time"),
        {"m": mode, "account": broker_account_id, "t0": t0, "t1": t1})
    return [dict(zip(("ticker", "side", "entry_ts", "entry_price", "exit_ts", "net"),
                     (row[0], row[1], row[2], float(row[3]), row[4], float(row[5] or 0))))
            for row in r.fetchall()]


async def _fetch_paper(db, t0, t1, test_name):
    r = await db.execute(text(
        "SELECT ticker, side, entry_time, entry_price, exit_time, net_pnl "
        "FROM sandbox_trades WHERE mode = 'paper' AND test_name = :test_name AND entry_time >= :t0 AND entry_time < :t1 "
        "ORDER BY entry_time"),
        {"t0": t0, "t1": t1, "test_name": test_name})
    return [dict(zip(("ticker", "side", "entry_ts", "entry_price", "exit_ts", "net"),
                     (row[0], row[1], row[2], float(row[3]), row[4], float(row[5] or 0))))
            for row in r.fetchall()]


def _match(real, paper, tol_s):
    paper_idx = {}
    for i, t in enumerate(paper):
        paper_idx.setdefault((t["ticker"], t["side"]), []).append((t["entry_ts"], i))
    matched_paper = set()
    hits = 0
    for t in real:
        cands = paper_idx.get((t["ticker"], t["side"])) or []
        for ets, i in cands:
            if i in matched_paper:
                continue
            if abs((ets - t["entry_ts"]).total_seconds()) <= tol_s:
                matched_paper.add(i)
                hits += 1
                break
    return hits, matched_paper


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--mode", default="sandbox", choices=["sandbox", "live"])
    ap.add_argument("--t0", default="06:00")
    ap.add_argument("--t1", default="18:30")
    ap.add_argument("--top_n", type=int, default=20)
    ap.add_argument("--cash", type=float, default=10000.0)
    ap.add_argument("--strategy", default="ensemble_v4")
    ap.add_argument("--interval", default="1min", choices=["1min", "5min", "10min", "15min"])
    ap.add_argument("--quantize", action="store_true", help="округлять время входа до минут (бар)")
    args = ap.parse_args()

    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    t0_utc = datetime.fromisoformat(f"{args.date}T{args.t0}:00+03:00").astimezone(timezone.utc)
    t1_utc = datetime.fromisoformat(f"{args.date}T{args.t1}:00+03:00").astimezone(timezone.utc)

    from app.config import get_settings

    account = get_settings().get_account(args.mode)
    if not account:
        ap.error(f"Не настроен брокерский счёт для режима {args.mode}")
    async with SessionLocal() as db:
        real = await _fetch_real(db, args.mode, account, t0_utc, t1_utc)
    print(f"REAL {args.mode} {args.date}: {len(real)} сделок в окне {t0_utc.isoformat()}..{t1_utc.isoformat()}")

    from app.bot.runtime import BotConfig, runtime

    test_name = f"parity_{args.date}_{uuid4().hex}"
    cfg = BotConfig(
        strategy_id=args.strategy,
        interval_name=args.interval,
        top_n=args.top_n,
        use_ensemble=args.strategy == "ensemble_v4",
        mode="paper",
        feed="replay",
        test_name=test_name,
        replay_start=t0_utc.isoformat(),
        replay_end=t1_utc.isoformat(),
        replay_pace="fast",
        sessions=["morning", "day", "evening"],
        long_allowed=True,
        short_allowed=True,
        ensemble_session="all",
        atr_period=14,
        atr_multiplier=4.0,
        atr_risk_reward=4.0,
        leverage=1.0,
        commission_rate=0.0005,
        slippage_bps=2.0,
        confirm_flip=2,
        reentry_cooldown_bars=15,
        overnight=False,
        initial_cash=args.cash,
    )

    await runtime.start(cfg)
    deadline = asyncio.get_event_loop().time() + 2400
    while (runtime.starting or runtime.running) and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(2)
    if runtime.starting or runtime.running:
        await runtime.stop()
        raise SystemExit("Реплей остановлен по таймауту")
    if runtime.error:
        raise SystemExit(f"Реплей завершился с ошибкой: {runtime.error}")

    from app.services.loghub import hub as _hub
    logs = [r.line for r in _hub.tail(400)]
    print("=== ПОЛНЫЙ ЛОГ РЕПЛЕЯ (первые 40 + последние 25) ===")
    for l in logs[:40]:
        print(l)
    if len(logs) > 65:
        print("... <пропущено %d строк> ..." % (len(logs) - 65))
    for l in logs[-25:]:
        print(l)
    st = runtime.status if hasattr(runtime, "status") else {}
    print(f"\nstatus.error={st.get('error')!r}")
    print(f"status.running={st.get('running')}  health={st.get('health')}")
    print(f"runtime._replay_seen={getattr(runtime, '_replay_seen', None)}")
    print(f"runtime._replay_pos_peek={getattr(runtime, '_replay_pos_peek', None)}")

    async with SessionLocal() as db:
        paper = await _fetch_paper(db, t0_utc, t1_utc, test_name)
    print(f"REPLAY (paper): {len(paper)} сделок")

    tol_s = 0 if args.quantize else 180  # ±3 мин от бара входа
    hits, matched = _match(real, paper, tol_s)
    print(f"\n=== PARITY {args.date} [{args.mode}] top_n={args.top_n} cash={args.cash:.0f} ===")
    print(f"real={len(real)}  replay={len(paper)}  real matched by replay={hits} ({hits / max(len(real),1) * 100:.1f}%)")
    print(f"replay trades matched by real={len(matched)} ({len(matched) / max(len(paper),1) * 100:.1f}%)")

    from collections import Counter
    rc, pc = Counter(t["ticker"] for t in real), Counter(t["ticker"] for t in paper)
    allt = sorted(set(rc) | set(pc))
    print(f"{'ticker':6s} {'real':>5s} {'paper':>5s}")
    for t in allt:
        print(f"{t:6s} {rc[t]:5d} {pc[t]:5d}")


if __name__ == "__main__":
    asyncio.run(main())
