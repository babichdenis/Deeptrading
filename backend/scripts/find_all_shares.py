#!/usr/bin/env python3
"""Find all share FIGIs and test which return day candles."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.chdir(Path(__file__).resolve().parents[1])
from dotenv import load_dotenv
load_dotenv(Path(".env"))

from app.config import get_settings
from t_tech.invest.clients import Client
from t_tech.invest import CandleInterval
from datetime import datetime, timedelta, timezone

token = get_settings().tinkoff_token
NOW = datetime.now(timezone.utc)
WEEK = NOW - timedelta(days=7)

with Client(token=token) as svc:
    # Get all shares
    r = svc.instruments.shares()
    print(f"Total shares: {len(r.instruments)}")

    # Find SBER and similar
    candidates = [x for x in r.instruments
                  if any(k in x.ticker for k in ['SBER', 'GAZP', 'LKOH', 'ROSN', 'VTB', 'NVTK'])]
    print(f"\nCandidates ({len(candidates)}):")
    for x in candidates[:15]:
        print(f"  ticker={x.ticker:10s} figi={x.figi:20s} name={x.name[:50]}")

    # Test a few BBG-format FIGIs for candles
    print("\nTesting BBG-format FIGIs for day candles:")
    test_figis = [
        'BBG004730N88',  # from DB (1m data)
        'BBG00475KKY8',  # from DB
        'TCS019029540',  # TCS format for SBER
        'TCS907661625',  # TCS format for GAZP
    ]
    for figi in test_figis:
        try:
            resp = svc.market_data.get_candles(
                figi=figi,
                interval=CandleInterval.CANDLE_INTERVAL_DAY,
                from_=WEEK, to=NOW
            )
            print(f"  {figi}: {len(resp.candles)} candles")
        except Exception as e:
            print(f"  {figi}: FAIL — {e}")
