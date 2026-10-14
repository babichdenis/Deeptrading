#!/usr/bin/env python3
"""Extended-horizon screen: is H=60 the best holding period?

Follow-up to the owner's question "why hold exactly one hour?". H=60 was
inherited from the daypack engine, not chosen by grid. This screen expands
HORIZONS to {30,60,90,120,180,240} over the same full m1 history and the
same dip(-3%)+down3 base rule, so we can see whether 60min is optimal or
merely adequate.

Reuse of b2_reversal_longrun timing/costs; only HORIZONS differ.
Run: nohup .venv/bin/python b2_horizon_screen.py --out ... &
"""
from __future__ import annotations

import argparse
import b2_reversal_longrun as R

R.HORIZONS = (30, 60, 90, 120, 180, 240)
R.WINDOWS = (30,)
R.THRESHOLDS = (-0.03,)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    args = p.parse_args()
    R.run(__import__("pathlib").Path(args.out))


if __name__ == "__main__":
    main()
