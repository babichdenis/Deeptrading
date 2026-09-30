#!/usr/bin/env python3
"""Stop Oracle: виртуальный этап анализа выходов (до подбора параметров).

Методология владельца: сначала ВИРТУАЛЬНО смотрим, где бы сработал стоп/трейл и
какая была просадка до максимальной PnL (что могли бы «забрать дальше»), и лишь
потом крутим параметры. Оракул — идеальные сделки зигзага (labeling.zigzag_trades).

Для каждого входа робота (native x07, trades_detail):
  - путь сделки [entry..native_exit]; MFE (в ATR) и его бар; MAE до пика (в ATR);
  - виртуальный жёсткий стоп k×ATR: убил бы сделку ДО пика или нет; сколько
    осталось бы (убит: −k ATR; выжил: capture нативной сделки);
  - виртуальный трейл (активация a×ATR, дистанция d×ATR, ratchet): capture и
    giveback от MFE;
  - оракул: попадает ли вход в колено зигзага, сколько от идеального хода забрали.

Запуск:
  .venv/bin/python scripts/stop_oracle.py --robot rsi_trade_hub \
      --from 2026-09-01 --to 2026-09-24 --interval 10min --jobs 3
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
from app.engine.indicatorhub import _atr  # noqa: E402
from app.engine.labeling import ZigzagConfig, zigzag_trades  # noqa: E402

import ose_exit_matrix as oem  # noqa: E402

DEFAULT_TICKERS = ("SBER,ROSN,T,GAZP,LKOH,NVTK,AFLT,SMLT,SNGSP,TRNFP,"
                   "MAGN,GMKN,RUAL,MTSS,NLMK,CHMF,ASTR,SFIN,YDEX,MVID,"
                   "VKCO,TATN,ALRS,PLZL,RNFT,OZON,VTBR,SIBN,AFKS,PHOR")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--robot", default="rsi_trade_hub")
    p.add_argument("--from", dest="dfrom", required=True)
    p.add_argument("--to", dest="dto", required=True)
    p.add_argument("--interval", default="10min", choices=sorted(oem.TF_SECONDS))
    p.add_argument("--tickers", default=DEFAULT_TICKERS)
    p.add_argument("--jobs", type=int, default=3)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--stops", default="0.5,1.0,1.5,2.0,3.0,4.0", help="жёсткие стопы, ×ATR")
    p.add_argument("--trails", default="0.5:0.5,1:0.5,1:1,2:1,2:1.5", help="трейлы a:d, ×ATR")
    p.add_argument("--zigzag-min", type=float, default=3.0, help="оракул: min ход зигзага ×ATR")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--commission", type=float, default=0.0005)
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--capital", type=float, default=100000.0)
    p.add_argument("--out", default="")
    return p.parse_args()


def main() -> int:
    a = parse_args()
    stops = [float(x) for x in a.stops.split(",") if x.strip()]
    trails = []
    for t in a.trails.split(","):
        if ":" in t:
            act, dist = t.split(":", 1)
            trails.append((float(act), float(dist)))
    tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
    with eng.connect() as c:
        fmap = {r[1]: r[0] for r in c.execute(text(
            "SELECT figi, upper(ticker) FROM instruments WHERE upper(ticker) = ANY(:t)"),
            {"t": tickers}).fetchall()}
    figis = [(fmap[t], t) for t in tickers if t in fmap]
    tf_s = oem.TF_SECONDS[a.interval]

    # 1) native-прогон с артефактами: входы/выходы робота
    import multiprocessing as mp
    tasks = [{
        "sid": a.robot, "xc": "x07", "figi": f, "ticker": tk,
        "params": None, "robot_kwargs": None, "label": f"{a.robot}@native",
        "artifacts": True, "cache": False, "code_sha": "",
        "dfrom": a.dfrom, "dto": a.dto, "tf_s": tf_s,
        "qty": a.qty, "commission": a.commission,
        "slippage_bps": a.slippage_bps, "capital": a.capital,
    } for f, tk in figis]
    t0 = time.time()
    raw: list[dict] = []
    with mp.Pool(a.jobs) as pool:
        for res in pool.imap_unordered(oem._run_combo, tasks):
            raw.append(res)

    # 2) виртуальный анализ по каждому тикеру
    stop_stats = {k: {"killed": 0, "survived": 0, "cap_atr": 0.0} for k in stops}
    trail_stats = {(act, d): {"hit": 0, "not_hit": 0, "cap_atr": 0.0, "giveback_atr": 0.0}
                   for act, d in trails}
    native_cap_atr = 0.0
    mfe_all: list[float] = []
    mae_peak_all: list[float] = []
    oracle_cover = 0
    oracle_capture: list[float] = []
    n_trades = 0

    for res in raw:
        if "error" in res or not res.get("trades_detail"):
            continue
        f = fmap.get(res.get("ticker") or "")
        if not f:
            continue
        try:
            _n1m, bars = oem._load_tf_cached(f, a.dfrom, a.dto, tf_s)
        except Exception:
            continue
        ts_idx = {b.ts: i for i, b in enumerate(bars)}
        atr_full = None
        # оракул по тикеру
        try:
            zig = zigzag_trades(bars, ZigzagConfig(min_move_atr=a.zigzag_min, atr_period=a.atr_period))
        except Exception:
            zig = []
        for t in res["trades_detail"]:
            try:
                ei = ts_idx.get(datetime.fromisoformat(t["entry_time"]))
                xi = ts_idx.get(datetime.fromisoformat(t["exit_time"]))
                if ei is None:
                    continue
                if xi is None or xi <= ei:
                    xi = min(ei + 1, len(bars) - 1)
            except Exception:
                continue
            if atr_full is None:
                atr_full = _atr(bars, a.atr_period)
            atr = atr_full[ei]
            if not atr or atr <= 0:
                continue
            long = str(t["side"]).upper() in ("LONG", "BUY")
            entry = float(t["entry_price"])
            # MFE/MAE до пика на пути [ei+1 .. xi]
            mfe_px = entry
            idx_mfe = ei
            mae_px = entry
            run_min, run_max = entry, entry
            for j in range(ei + 1, xi + 1):
                hi, lo = float(bars[j].high), float(bars[j].low)
                run_min = min(run_min, lo)
                run_max = max(run_max, hi)
                cur_fav = (run_max - entry) if long else (entry - run_min)
                if cur_fav > (mfe_px - entry if long else entry - mfe_px):
                    mfe_px = (run_max if long else run_min)
                    idx_mfe = j
                    mae_px = run_min if long else run_max
            mfe_atr = abs(mfe_px - entry) / atr
            mae_peak_atr = (entry - mae_px) / atr if long else (mae_px - entry) / atr
            mae_peak_atr = max(0.0, mae_peak_atr)
            mfe_all.append(mfe_atr)
            mae_peak_all.append(mae_peak_atr)
            # нативный capture (гросс, в ATR) по фактическому выходу
            exit_px = float(t["exit_price"])
            native = ((exit_px - entry) if long else (entry - exit_px)) / atr
            native_cap_atr += native
            n_trades += 1
            # виртуальные жёсткие стопы
            for k in stops:
                if mae_peak_atr >= k:
                    stop_stats[k]["killed"] += 1
                    stop_stats[k]["cap_atr"] += -k
                else:
                    stop_stats[k]["survived"] += 1
                    stop_stats[k]["cap_atr"] += native
            # виртуальные трейлы: ratchet от пика пути
            for (act, d) in trails:
                peak = entry
                stop_px = None
                captured = native  # если трейл так и не активировался/не сработал
                hit = False
                for j in range(ei + 1, xi + 1):
                    hi, lo = float(bars[j].high), float(bars[j].low)
                    peak = max(peak, hi) if long else min(peak, lo)
                    act_px = entry + act * atr if long else entry - act * atr
                    if (long and peak >= act_px) or (not long and peak <= act_px):
                        cand = peak - d * atr if long else peak + d * atr
                        stop_px = cand if stop_px is None else (max(stop_px, cand) if long else min(stop_px, cand))
                    if stop_px is not None and ((long and lo <= stop_px) or (not long and hi >= stop_px)):
                        captured = ((stop_px - entry) if long else (entry - stop_px)) / atr
                        hit = True
                        break
                st = trail_stats[(act, d)]
                st["hit" if hit else "not_hit"] += 1
                st["cap_atr"] += captured
                st["giveback_atr"] += max(0.0, mfe_atr - captured)
            # оракул: вход внутри колена зигзага
            for leg in zig:
                try:
                    if leg["entry_ts"] <= t["entry_time"] <= leg["exit_ts"]:
                        oracle_cover += 1
                        ideal = abs(leg["exit_px"] - leg["entry_px"]) / atr
                        if ideal > 0:
                            oracle_capture.append(max(0.0, min(1.5, native / ideal)))
                        break
                except Exception:
                    continue

    print(f"Stop Oracle: {a.robot} · {n_trades} сделок (native) · {a.dfrom}..{a.dto} · {a.interval} · "
          f"{time.time() - t0:.0f}s")
    if not n_trades:
        print("нет сделок")
        return 1
    import statistics as st
    print(f"MFE медиана {st.median(mfe_all):.2f} ATR · MAE-до-пика медиана {st.median(mae_peak_all):.2f} ATR "
          f"· нативный capture {native_cap_atr:+.1f} ATR (итого)")
    print(f"\n{'стоп k×ATR':>11} {'убил бы ДО пика':>15} {'выжил':>6} {'итог ATR':>10} {'Δ к native':>11}")
    for k in stops:
        v = stop_stats[k]
        print(f"{k:>11.1f} {v['killed']:>15} {v['survived']:>6} {v['cap_atr']:>10.1f} "
              f"{v['cap_atr'] - native_cap_atr:>+11.1f}")
    print(f"\n{'трейл a:d':>11} {'сработал':>9} {'не сраб.':>9} {'итог ATR':>10} {'giveback ATR':>13} {'Δ к native':>11}")
    for (act, d) in trails:
        v = trail_stats[(act, d)]
        print(f"{act:>5.1f}:{d:<5.1f} {v['hit']:>9} {v['not_hit']:>9} {v['cap_atr']:>10.1f} "
              f"{v['giveback_atr']:>13.1f} {v['cap_atr'] - native_cap_atr:>+11.1f}")
    if oracle_capture:
        print(f"\nОракул: {oracle_cover}/{n_trades} входов внутри колен зигзага; "
              f"средний capture от идеального хода {100 * st.mean(oracle_capture):.0f}%")
    out = a.out or f"reports/stop_oracle_{a.robot}_{datetime.now(timezone.utc):%Y%m%d_%H%M}.json"
    Path(out).write_text(json.dumps({
        "meta": {"robot": a.robot, "interval": a.interval, "period": [a.dfrom, a.dto],
                 "tickers": sorted(t for _, t in figis), "stops_xatr": stops,
                 "trails": trails, "created_utc": datetime.now(timezone.utc).isoformat()},
        "summary": {
            "trades": n_trades, "native_cap_atr": round(native_cap_atr, 1),
            "mfe_median_atr": round(st.median(mfe_all), 2),
            "mae_peak_median_atr": round(st.median(mae_peak_all), 2),
            "stops": {str(k): stop_stats[k] for k in stops},
            "trails": {f"{act}:{d}": trail_stats[(act, d)] for (act, d) in trails},
            "oracle_cover": oracle_cover,
            "oracle_capture_mean": (round(st.mean(oracle_capture), 3) if oracle_capture else None),
        },
    }, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
