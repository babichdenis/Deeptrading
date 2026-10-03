#!/usr/bin/env python
"""Профилировщик Signal Lab: где тормозит движок на минутках.

Пример:
  python scripts/signal_lab_profile.py --engine canon_ensemble \
      --ticker SBER --date 2026-09-02 --bars 0
  python scripts/signal_lab_profile.py --engine ose_all \
      --ticker SBER --date 2026-09-02 --bars 600 --tf 1min
"""
from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.lab.data import build_tf, engine_sync, load_1m, resolve_universe, to_engine_candles  # noqa: E402
from app.services.signals import generate_signals  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True)
    ap.add_argument("--ticker", default="SBER")
    ap.add_argument("--date", default="2026-09-02")
    ap.add_argument("--bars", type=int, default=0, help="0 = весь день")
    ap.add_argument("--tf", default="1min")
    ap.add_argument("--window", type=int, default=400)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    eng = engine_sync()
    u = resolve_universe(eng, [args.ticker])[0]
    # прогрева хватит 2 недель — как в конфиге прогона
    bars_1m = load_1m(eng, u["figi"], f"{args.date}T00:00:00+00:00",
                      f"{args.date}T23:59:59+00:00")
    if not bars_1m:
        print("нет 1m-баров")
        return 2
    bars = build_tf(bars_1m, args.tf)
    candles = to_engine_candles(bars)
    if args.bars and len(candles) > args.bars:
        candles = candles[:args.bars]
    print(f"engine={args.engine} ticker={args.ticker} tf={args.tf} "
          f"bars={len(candles)} window={args.window}", flush=True)

    prof = cProfile.Profile()
    prof.enable()
    sigs = generate_signals(args.engine, None, candles, window=args.window)
    prof.disable()
    print(f"signals={len(sigs)}")

    buf = io.StringIO()
    st = pstats.Stats(prof, stream=buf).sort_stats("cumulative")
    st.print_stats(args.top)
    out = buf.getvalue()
    # оставляем только значимые строки
    for line in out.splitlines():
        if any(k in line for k in ("cumtime", "ncalls", "{", "app/", "site-packages")):
            print(line[:180])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
