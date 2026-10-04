#!/usr/bin/env python
"""Signal Lab runner (M1: фаза signals).

Примеры:
  # проверить конфиг без записи
  python scripts/signal_lab_run.py --config configs/signal_lab/discovery_2026-09.json \
      --phase signals --dry-run

  # полный месяц: все движки, все тикеры, 3 ТФ
  python scripts/signal_lab_run.py --config configs/signal_lab/discovery_2026-09.json \
      --phase signals --jobs 3

  # смоук: один тикер, два движка, три дня
  python scripts/signal_lab_run.py --config configs/signal_lab/discovery_2026-09.json \
      --phase signals --tickers SBER --engines macd_cross,rsi_reversal \
      --date-from 2026-09-01T00:00:00+00:00 --date-to 2026-09-03T23:59:59+00:00
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.lab.config import load_config  # noqa: E402
from app.lab.runner import run_phase  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Signal Lab runner")
    ap.add_argument("--config", required=True, help="путь к JSON-конфигу прогона")
    ap.add_argument("--phase", default="signals",
                    choices=["signals", "outcomes", "fixed", "trailing", "events", "regime", "all"])
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--tickers", default="", help="override: список тикеров через запятую")
    ap.add_argument("--engines", default="", help="override: список strategy_id через запятую")
    ap.add_argument("--date-from", default="", help="override period_from (ISO)")
    ap.add_argument("--date-to", default="", help="override period_to (ISO)")
    ap.add_argument("--run-id", default="", help="для фаз outcomes/fixed/trailing: id прогона")
    ap.add_argument("--db-url", default="", help="override DSN (без +asyncpg)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
    engines = [e.strip() for e in args.engines.split(",") if e.strip()] or None
    out = run_phase(
        cfg, args.phase,
        db_url=(args.db_url or None), jobs=args.jobs,
        engines=engines, tickers=tickers,
        date_from=(args.date_from or None), date_to=(args.date_to or None),
        run_id=(args.run_id or None),
        dry_run=args.dry_run,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
