#!/usr/bin/env python3
"""Find BBG FIGIs for major MOEX stocks."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.chdir(Path(__file__).resolve().parents[1])
from dotenv import load_dotenv
load_dotenv(Path(".env"))

from app.config import get_settings
from t_tech.invest.clients import Client

token = get_settings().tinkoff_token
with Client(token=token) as svc:
    for q in ['SBER', 'SBERP', 'GAZP', 'VTB']:
        r = svc.instruments.find_instrument(query=q)
        if r.instruments:
            print(f"{q}:")
            for x in r.instruments[:3]:
                print(f"  ticker={x.ticker} figi={x.figi} uid={x.uid} name={x.name} exchange={getattr(x,'exchange',None)}")
        else:
            print(f"{q}: NOT FOUND")
