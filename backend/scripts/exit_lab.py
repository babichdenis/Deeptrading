#!/usr/bin/env python3
"""Exit Lab: доскональный тест политик выхода с параметрами.

Метрики — по методологии владельца (не только PnL): количество сделок,
победы/поражения в ШТУКАХ и в РУБЛЯХ (gross win/loss), expectancy.
Выходные данные — отчёт reports/exit_lab_<robot>_<ts>.json (agg + raw по тикерам).

Запуск:
  .venv/bin/python scripts/exit_lab.py --robot rsi_trade_hub \
      --from 2026-09-01 --to 2026-09-24 --interval 10min --tickers SBER,... --jobs 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.engine.exits import (  # noqa: E402
    AtrStopPolicy,
    AtrTrailingPolicy,
    FixedSlTpPolicy,
)

import ose_exit_matrix as oem  # noqa: E402

NoExitPolicy = oem.NoExitPolicy

DEFAULT_TICKERS = ("SBER,ROSN,T,GAZP,LKOH,NVTK,AFLT,SMLT,SNGSP,TRNFP,"
                   "MAGN,GMKN,RUAL,MTSS,NLMK,CHMF,ASTR,SFIN,YDEX,MVID,"
                   "VKCO,TATN,ALRS,PLZL,RNFT,OZON,VTBR,SIBN,AFKS,PHOR")

# name -> (label, factory). Комиссионный трейл: dist в ЧИСТЫХ ATR (см. AtrStopPolicy).
GRID: dict[str, tuple[str, object]] = {
    "native":        ("signal-only (native)", lambda: NoExitPolicy()),
    "fixed_100_200": ("fixed 1%/2%", lambda: FixedSlTpPolicy(stop_pct=0.01, target_pct=0.02)),
    "fixed_050_100": ("fixed 0.5%/1%", lambda: FixedSlTpPolicy(stop_pct=0.005, target_pct=0.01)),
    "fixed_150_300": ("fixed 1.5%/3%", lambda: FixedSlTpPolicy(stop_pct=0.015, target_pct=0.03)),
    "fixed_050_150": ("fixed 0.5%/1.5%", lambda: FixedSlTpPolicy(stop_pct=0.005, target_pct=0.015)),
    # ATR-стоп без трейла
    "atr_m15_rr2":  ("atr 1.5 rr2", lambda: AtrStopPolicy(period=14, multiplier=1.5, risk_reward=2.0)),
    "atr_m20_rr2":  ("atr 2 rr2", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0)),
    "atr_m25_rr2":  ("atr 2.5 rr2", lambda: AtrStopPolicy(period=14, multiplier=2.5, risk_reward=2.0)),
    "atr_m30_rr2":  ("atr 3 rr2", lambda: AtrStopPolicy(period=14, multiplier=3.0, risk_reward=2.0)),
    "atr_m20_rr3":  ("atr 2 rr3", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=3.0)),
    "atr_m30_rr3":  ("atr 3 rr3", lambda: AtrStopPolicy(period=14, multiplier=3.0, risk_reward=3.0)),
    "atr_m20_noRR": ("atr 2 stop-only", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=None)),
    "atr_m30_noRR": ("atr 3 stop-only", lambda: AtrStopPolicy(period=14, multiplier=3.0, risk_reward=None)),
    # ATR-стоп + трейл (dist в долях risk = mult×ATR)
    "tr_a05_d05":   ("atr2 rr2 trail act0.5 dist0.5xAtr", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0, trail_activation_r=0.5, trail_distance_r=0.25)),
    "tr_a10_d05":   ("atr2 rr2 trail act1 dist0.5xAtr", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0, trail_activation_r=1.0, trail_distance_r=0.25)),
    "tr_a10_d10":   ("atr2 rr2 trail act1 dist1xAtr", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0, trail_activation_r=1.0, trail_distance_r=0.5)),
    "tr_a10_d15":   ("atr2 rr2 trail act1 dist1.5xAtr", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0, trail_activation_r=1.0, trail_distance_r=0.75)),
    "tr_a20_d10":   ("atr2 rr2 trail act2 dist1xAtr", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0, trail_activation_r=2.0, trail_distance_r=0.5)),
    # комиссионный трейл (как в лайв-боте), dist в ATR
    "cm4_d10":      ("comm x4 trail dist1xAtr", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0, trail_activation_comm_mult=4.0, trail_distance_r=1.0, trail_min_atr=1.0)),
    "cm4_d15":      ("comm x4 trail dist1.5xAtr", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0, trail_activation_comm_mult=4.0, trail_distance_r=1.5, trail_min_atr=1.0)),
    "cm8_d15":      ("comm x8 trail dist1.5xAtr", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0, trail_activation_comm_mult=8.0, trail_distance_r=1.5, trail_min_atr=1.0)),
    "cm4_dyn":      ("comm x4 dynamic (compress+min+vol)", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0, trail_activation_comm_mult=4.0, trail_distance_r=2.5, trail_compress_r=1.0, trail_min_factor=0.6, trail_min_atr=1.5, trail_vol_boost=0.3)),
    # AtrTrailingPolicy (профиль x03)
    "trail_2_10_15": ("AtrTrailing 2/act1/dist1.5", lambda: AtrTrailingPolicy(period=14, initial_stop_atr=2.0, activation_atr=1.0, trail_distance_atr=1.5)),
    "trail_3_05_20": ("AtrTrailing 3/act0.5/dist2", lambda: AtrTrailingPolicy(period=14, initial_stop_atr=3.0, activation_atr=0.5, trail_distance_atr=2.0)),
    "trail_2_05_10": ("AtrTrailing 2/act0.5/dist1", lambda: AtrTrailingPolicy(period=14, initial_stop_atr=2.0, activation_atr=0.5, trail_distance_atr=1.0)),
}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--robot", default="rsi_trade_hub")
    p.add_argument("--from", dest="dfrom", required=True)
    p.add_argument("--to", dest="dto", required=True)
    p.add_argument("--interval", default="10min", choices=sorted(oem.TF_SECONDS))
    p.add_argument("--tickers", default=DEFAULT_TICKERS)
    p.add_argument("--jobs", type=int, default=3)
    p.add_argument("--commission", type=float, default=0.0005)
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--capital", type=float, default=100000.0)
    p.add_argument("--out", default="")
    return p.parse_args()


def main() -> int:
    a = parse_args()
    # регистрируем сетку выходов в реестре матрицы (под префиксом el_)
    codes: list[str] = []
    for name, (label, factory) in GRID.items():
        code = f"el_{name}"
        oem.EXITS[code] = (label, factory)
        codes.append(code)
    tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
    with eng.connect() as c:
        fmap = {r[1]: r[0] for r in c.execute(text(
            "SELECT figi, upper(ticker) FROM instruments WHERE upper(ticker) = ANY(:t)"),
            {"t": tickers}).fetchall()}
    figis = [(fmap[t], t) for t in tickers if t in fmap]
    tf_s = oem.TF_SECONDS[a.interval]
    tasks: list[dict] = []
    for code in codes:
        for f, tk in figis:
            tasks.append({
                "sid": a.robot, "xc": code, "figi": f, "ticker": tk,
                "params": None, "robot_kwargs": None,
                "label": f"{a.robot}@{code}", "artifacts": False, "cache": True, "code_sha": "",
                "dfrom": a.dfrom, "dto": a.dto, "tf_s": tf_s,
                "qty": a.qty, "commission": a.commission,
                "slippage_bps": a.slippage_bps, "capital": a.capital,
            })
    print(f"Exit Lab: робот {a.robot} · выходов {len(codes)} · тикеров {len(figis)} · "
          f"прогонов {len(tasks)} · TF {a.interval} · {a.dfrom}..{a.dto}")
    import multiprocessing as mp
    raw: list[dict] = []
    t0 = time.time()
    # fork: дочерние воркеры наследуют регистрацию el_* в oem.EXITS
    # (spawn-дети пере-импортируют oem без нашей сетки → «unknown exit»).
    _ctx = mp.get_context("fork")
    with _ctx.Pool(a.jobs) as pool:
        for res in pool.imap_unordered(oem._run_combo, tasks):
            raw.append(res)
    print(f"готово за {time.time() - t0:.0f}с")
    # агрегация по выходам: сделки, победы/поражения в ШТУКАХ и РУБЛЯХ
    agg: dict[str, dict] = {}
    for code in codes:
        rows = [r for r in raw if r.get("exit") == code and "error" not in r]
        n = sum(r["trades"] for r in rows)
        w = sum(r["wins"] for r in rows)
        gw = sum(r["gw"] for r in rows)
        gl = sum(r["gl"] for r in rows)
        net = sum(r["net"] for r in rows)
        agg[code] = {
            "label": GRID[code[3:]][0],
            "trades": n, "wins": w, "losses": n - w,
            "wr": round(100 * w / n, 1) if n else 0.0,
            "gross_win_rub": round(gw, 1), "gross_loss_rub": round(-gl, 1),
            "net": round(net, 1),
            "avg_win_rub": round(gw / w, 2) if w else 0.0,
            "avg_loss_rub": round(-gl / max(1, n - w), 2),
            "expectancy": round(net / n, 3) if n else 0.0,
            "errors": sum(1 for r in raw if r.get("exit") == code and "error" in r),
        }
    print(f"\n{'выход':<44} {'сд':>5} {'W шт':>5} {'L шт':>5} {'WR%':>5} "
          f"{'gW ₽':>9} {'gL ₽':>9} {'net':>9} {'ср.W':>7} {'ср.L':>7} {'err':>4}")
    for code in codes:
        v = agg[code]
        print(f"{code[3:]:<44} {v['trades']:>5} {v['wins']:>5} {v['losses']:>5} {v['wr']:>5} "
              f"{v['gross_win_rub']:>9} {v['gross_loss_rub']:>9} {v['net']:>9} "
              f"{v['avg_win_rub']:>7} {v['avg_loss_rub']:>7} {v['errors']:>4}")
    out = a.out or f"reports/exit_lab_{a.robot}_{datetime.now(timezone.utc):%Y%m%d_%H%M}.json"
    Path(out).write_text(json.dumps({
        "meta": {"robot": a.robot, "interval": a.interval, "period": [a.dfrom, a.dto],
                 "tickers": sorted(t for _, t in figis), "qty": a.qty,
                 "commission": a.commission, "slippage_bps": a.slippage_bps,
                 "created_utc": datetime.now(timezone.utc).isoformat()},
        "agg": agg, "raw": raw,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
