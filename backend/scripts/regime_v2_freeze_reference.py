"""Stage C.6 — заморозка reference labels Regime v2 (read-only, один прогон).

Находит в реальных данных характерные переходы (RANGE→TREND, blip в тренде,
TREND_UP↔TREND_DOWN, HIGH_VOL→TREND), вырезает окна с контекстом и сохраняет
бары + полный вывод пайплайна (measurements → classifier → hysteresis mild 2/1)
в `tests/fixtures/regime_v2_reference.json` для герметичного эталонного теста.

Запуск:
  ~/.venvs/deeptrading/bin/python scripts/regime_v2_freeze_reference.py \
    --dsn postgresql://deeptrading:deeptrading@192.168.1.7:5432/deeptrading \
    --days 93 --to 2026-10-02T08:00:00Z
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.engine.models import Candle
from app.services.ensemble import resample
from app.services.regime_v2.hysteresis import RegimeV2State
from app.services.regime_v2.measurements import RegimeV2Params, compute_measurements

import regime_calibration as rcal

TREND = ("TREND_UP", "TREND_DOWN")
CONTEXT_BARS = 260
FORWARD_BARS = 40

def _blip_in_trend(raw: list[str], i: int) -> bool:
    if i >= len(raw) or raw[i] not in TREND:
        return False
    for k in (1, 2, 3):
        j = i + 1 + k
        if j >= len(raw):
            break
        if all(raw[i + m] == "NEUTRAL" for m in range(1, k + 1)) and raw[j] == raw[i]:
            return True
    return False


PATTERNS = {
    "range_to_trend": lambda raw, i: raw[i] == "RANGE" and any(
        raw[j] in TREND for j in range(i + 1, min(i + 6, len(raw)))),
    "blip_in_trend": _blip_in_trend,
    "trend_flip": lambda raw, i: (
        i + 1 < len(raw) and raw[i] in TREND and raw[i + 1] in TREND
        and raw[i] != raw[i + 1]),
    "hv_to_trend": lambda raw, i: (
        i + 1 < len(raw) and raw[i] == "HIGH_VOLATILITY" and raw[i + 1] in TREND),
}


def run_pipeline(bars: list[Candle], tf_sec: int, window: int):
    ms = compute_measurements(bars, RegimeV2Params(window=window))
    state = RegimeV2State()
    out = []
    for m in ms:
        obs, raw, hyst = state.update(m["ts"], tf_sec, m)
        out.append({"ts": m["ts"], "structure": obs.structure, "direction": obs.direction,
                    "volatility": obs.volatility, "raw": raw, "hyst": hyst,
                    "confidence": obs.confidence})
    return out


def find_pattern(raw: list[str], matcher) -> int | None:
    for i in range(1, len(raw)):
        if matcher(raw, i):
            return i
    return None


async def run(args):
    import os
    dsn = args.dsn or os.environ.get("REGIME_CAL_DSN")
    if not dsn:
        from app.config import get_settings
        dsn = get_settings().database_url.replace("+asyncpg", "")
    t_to = datetime.fromisoformat(args.to.replace("Z", "+00:00")) if args.to else datetime.now(UTC)
    if t_to.tzinfo is None:
        t_to = t_to.replace(tzinfo=UTC)
    t_from = t_to - timedelta(days=args.days)
    tfs = [int(x) for x in args.tfs.split(",") if x.strip()]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    conn = await rcal.open_readonly(dsn)
    cases: dict[str, dict] = {}
    try:
        instruments = await rcal.select_universe(conn, t_from, t_to, args)
        for inst in instruments:
            candles = await rcal.load_1m(conn, inst["figi"], t_from, t_to)
            for tf in tfs:
                bars = resample(candles, tf)
                full_out = run_pipeline(bars, tf, args.window)
                raw = [r["raw"] for r in full_out]
                for name, matcher in PATTERNS.items():
                    if name in cases:
                        continue
                    idx = find_pattern(raw, matcher)
                    if idx is None:
                        continue
                    start = max(0, idx - CONTEXT_BARS)
                    end = min(len(bars), idx + FORWARD_BARS)
                    slice_bars = bars[start:end]
                    slice_out = run_pipeline(slice_bars, tf, args.window)
                    slice_raw = [r["raw"] for r in slice_out]
                    if find_pattern(slice_raw, matcher) is None:
                        continue
                    cases[name] = {
                        "name": name,
                        "ticker": inst["ticker"],
                        "figi": inst["figi"],
                        "tf": tf,
                        "bars": [{"ts": b.ts.isoformat(), "open": float(b.open),
                                  "high": float(b.high), "low": float(b.low),
                                  "close": float(b.close), "volume": float(b.volume or 0)}
                                 for b in slice_bars],
                        "expected": [dict(r, ts=r["ts"].isoformat()) for r in slice_out],
                    }
    finally:
        await conn.close()

    payload = {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(),
            "window": args.window,
            "hysteresis": {"confirm_bars": 2, "min_tenure": 1},
            "days": args.days, "t_to": t_to.isoformat(),
            "note": "expected = вывод пайплайна на этом же срезе (frozen reference)",
        },
        "cases": cases,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path, sorted(cases)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=None)
    p.add_argument("--tickers", default="SBER,ROSN,NVTK,SMLT")
    p.add_argument("--days", type=int, default=93)
    p.add_argument("--to", default="2026-10-02T08:00:00Z")
    p.add_argument("--tfs", default="3600,300")
    p.add_argument("--window", type=int, default=12)
    p.add_argument("--top", type=int, default=6)
    p.add_argument("--min-bars", type=int, default=50000)
    p.add_argument("--out", default="tests/fixtures/regime_v2_reference.json")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    path, found = asyncio.run(run(args))
    print(f"fixture: {path}; patterns: {found}")


if __name__ == "__main__":
    main()
