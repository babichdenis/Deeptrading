#!/usr/bin/env python3
"""Свип-тест готовых OsEngine-портов (Wave A + RsiTrade) для межмашинной сверки.

Что делает:
  run     — гоняет все порты по сетке параметров × seed'ам детерминированной
            серии (ose.metrics.synthetic_series), сохраняет JSON в reports/.
            Числа воспроизводимы на любой машине: random.Random(seed)
            кроссплатформен, equity-кривая хешируется (sha256).
  compare — построчная сверка двух JSON (Mac vs .8): совпадение метрик с
            допуском --tol и дайджестов equity. Код версонируется sha256
            исходников app/engine/ose/* + app/engine/models.py.

Единицы — пункты (разности цен), 1 контракт, без комиссий (как в оригинале
TesterTab). Запись "inf" у profit_factor — gross_loss == 0 при наличии прибылей.

Запуск (из backend/):
  python3 scripts/bt_ose_sweep.py                        # run, дефолты
  python3 scripts/bt_ose_sweep.py --bars 600 --seeds 42,7,123,2026
  python3 scripts/bt_ose_sweep.py compare reports/a.json reports/b.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.ose.metrics import run_robot_with_metrics, synthetic_series
from app.engine.ose.robots import (
    EnvelopTrend,
    PriceChannelTrade,
    Regime,
    RsiContrtrend,
    RsiTrade,
    SmaStochastic,
    StrategyBollinger,
    TesterTab,
)

ROBOTS = {
    "price_channel": PriceChannelTrade,
    "sma_stoch": SmaStochastic,
    "envelop_trend": EnvelopTrend,
    "rsi_contrtrend": RsiContrtrend,
    "rsi_trade": RsiTrade,
    "bollinger": StrategyBollinger,
}

# Сетки параметров: {} = дефолты порта (как в OsEngine). step у sma_stoch
# задаётся в единицах цены (серия ~100), т.к. дефолт оригинала 500 пунктов
# на такой серии не даёт сделок (см. докстринг strategy.py).
GRIDS: dict[str, list[dict]] = {
    "price_channel": [
        {},
        {"length_up": 10, "length_down": 10},
        {"length_up": 34, "length_down": 34},
        {"regime": Regime.ONLY_LONG},
    ],
    "sma_stoch": [
        {"step": 1.0},
        {"step": 0.5},
        {"step": 2.0},
        {"step": 1.0, "regime": Regime.ONLY_SHORT},
    ],
    "envelop_trend": [
        {},
        {"deviation": 0.5},
        {"trail_stop": 0.5},
        {"deviation": 1.0, "trail_stop": 1.0},
    ],
    "rsi_contrtrend": [
        {},
        {"upline": 70.0, "downline": 30.0},
        {"rsi_length": 14},
        {"regime": Regime.ONLY_LONG},
    ],
    "rsi_trade": [
        {},
        {"upline": 70.0, "downline": 30.0},
        {"rsi_length": 14},
        {"upline": 60.0, "downline": 40.0},
    ],
    "bollinger": [
        {},
        {"boll_length": 15},
        {"boll_deviation": 2.5},
        {"regime": Regime.ONLY_SHORT},
    ],
}

# Исходники, чьё содержимое версиянируется в отчёте (сверка кода между машинами).
CODE_FILES = [
    "app/engine/ose/__init__.py",
    "app/engine/ose/indicators.py",
    "app/engine/ose/robots.py",
    "app/engine/ose/metrics.py",
    "app/engine/models.py",
]

DEFAULT_OUT = "reports/bt_ose_sweep.json"
DEFAULT_SEEDS = "42,7,123"
DEFAULT_BARS = 400


def _clean_params(params: dict) -> dict:
    """Regime -> строка-значение, чтобы JSON был сериализуемым и стабильным."""
    return {k: (v.value if isinstance(v, Regime) else v)
            for k, v in params.items()}


def _params_json(params: dict) -> str:
    return json.dumps(_clean_params(params), sort_keys=True, separators=(",", ":"))


def _code_sha256() -> str:
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for rel in CODE_FILES:
        h.update(rel.encode())
        h.update((root / rel).read_bytes())
    return h.hexdigest()[:16]


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        return out.stdout.strip() or "n/a"
    except Exception:
        return "n/a"


def _run_one(robot_name: str, params: dict, candles) -> dict:
    """Прогон одного конфига; float'ы округляются до 6 знаков, PF inf -> "inf",
    equity-кривая сворачивается в sha256-дайджест (округление фиксирует
    представление, hash ловит любое расхождение траектории)."""
    robot = ROBOTS[robot_name](TesterTab(), **params)
    rep = run_robot_with_metrics(robot, candles)
    curve = rep.pop("equity_curve")
    metrics: dict = {}
    for k, v in rep.items():
        if isinstance(v, float):
            metrics[k] = "inf" if math.isinf(v) else round(v, 6)
        else:
            metrics[k] = v
    metrics["equity_sha256"] = hashlib.sha256(
        ",".join(f"{v:.6f}" for v in curve).encode()
    ).hexdigest()
    return metrics


def run_sweep(args) -> int:
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    t0 = time.time()
    series_cache: dict[int, list] = {}
    series_info: dict[str, dict] = {}
    for seed in seeds:
        candles = synthetic_series(args.bars, seed=seed)
        series_cache[seed] = candles
        series_info[str(seed)] = {
            "first_close": round(candles[0].close, 6),
            "last_close": round(candles[-1].close, 6),
            "buy_hold": round(candles[-1].close - candles[0].close, 6),
        }

    runs: list[dict] = []
    n_cfg = sum(len(g) for g in GRIDS.values())
    print(f"sweep: {len(ROBOTS)} robots, {n_cfg} configs x {len(seeds)} seeds "
          f"= {n_cfg * len(seeds)} runs, bars={args.bars}")
    for robot_name, grid in GRIDS.items():
        for params in grid:
            pj = _params_json(params)
            for seed in seeds:
                rep = _run_one(robot_name, params, series_cache[seed])
                runs.append({
                    "robot": robot_name,
                    "params": _clean_params(params),
                    "params_json": pj,
                    "seed": seed,
                    "metrics": rep,
                })
                pf = rep["profit_factor"]
                print(f"  {robot_name:<15} {pj:<52} seed={seed:<5} "
                      f"tr={rep['trades']:<4} pnl={rep['pnl']:>9.2f} "
                      f"pf={pf} dd={rep['max_dd']:.2f}")

    report = {
        "meta": {
            "script": "scripts/bt_ose_sweep.py",
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "git_commit": _git_commit(),
            "code_sha256": _code_sha256(),
            "n_bars": args.bars,
            "seeds": seeds,
            "series_info": series_info,
        },
        "runs": runs,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\nsaved: {out} ({len(runs)} runs) in {time.time() - t0:.1f}s")
    print(f"code_sha256={report['meta']['code_sha256']} "
          f"git={report['meta']['git_commit']} host={report['meta']['hostname']}")
    return 0


def _key(run: dict) -> str:
    return f"{run['robot']}|{run['params_json']}|{run['seed']}"


def compare_reports(a_path: str, b_path: str, tol: float) -> int:
    a = json.loads(Path(a_path).read_text())
    b = json.loads(Path(b_path).read_text())
    ma_, mb_ = a["meta"], b["meta"]
    print(f"A: {a_path}  ({ma_.get('hostname')} {ma_.get('created_utc')} "
          f"code={ma_.get('code_sha256')})")
    print(f"B: {b_path}  ({mb_.get('hostname')} {mb_.get('created_utc')} "
          f"code={mb_.get('code_sha256')})")
    for f in ("code_sha256", "python", "n_bars", "seeds"):
        if ma_.get(f) != mb_.get(f):
            print(f"  WARNING meta.{f}: {ma_.get(f)} != {mb_.get(f)}")

    ia = {_key(r): r["metrics"] for r in a["runs"]}
    ib = {_key(r): r["metrics"] for r in b["runs"]}
    only_a = sorted(set(ia) - set(ib))
    only_b = sorted(set(ib) - set(ia))
    ok = bad = 0
    for k in sorted(set(ia) & set(ib)):
        ra, rb = ia[k], ib[k]
        diffs = []
        for f in sorted(set(ra) | set(rb)):
            va, vb = ra.get(f), rb.get(f)
            num = (isinstance(va, (int, float)) and isinstance(vb, (int, float))
                   and not isinstance(va, bool))
            if num and abs(va - vb) <= tol:
                continue
            if va != vb:
                diffs.append(f"{f}: {va} vs {vb}")
        if diffs:
            bad += 1
            print(f"MISMATCH {k}")
            for d in diffs:
                print(f"    {d}")
        else:
            ok += 1
    for k in only_a:
        print(f"ONLY IN A: {k}")
    for k in only_b:
        print(f"ONLY IN B: {k}")
    print(f"\ncompare: {ok} ok, {bad} mismatch, "
          f"{len(only_a)} only-A, {len(only_b)} only-B")
    return 0 if (bad == 0 and not only_a and not only_b) else 1


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["compare"]:
        ap = argparse.ArgumentParser(prog="bt_ose_sweep.py compare")
        ap.add_argument("a", help="первый JSON (напр. с Mac)")
        ap.add_argument("b", help="второй JSON (напр. с .8)")
        ap.add_argument("--tol", type=float, default=1e-9,
                        help="допуск для числовых метрик (дефолт 1e-9)")
        args = ap.parse_args(argv[1:])
        return compare_reports(args.a, args.b, args.tol)

    ap = argparse.ArgumentParser(
        description="Свип-тест OsEngine-портов с сохранением JSON для сверки")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"куда писать JSON (дефолт {DEFAULT_OUT})")
    ap.add_argument("--bars", type=int, default=DEFAULT_BARS)
    ap.add_argument("--seeds", default=DEFAULT_SEEDS, help="через запятую")
    args = ap.parse_args(argv)
    return run_sweep(args)


if __name__ == "__main__":
    raise SystemExit(main())
