"""Optuna: подбор параметров MOEX-follow (сила сигнала входа/выхода, hold, K, EMA).

Train/valid split по времени (70/30) — чтобы не подгоняться на всей истории.
Objective: net на train при MaxDD <= 25%; затем проверяем лучших на valid.

Запуск: python scripts/optuna_moex_follow.py [--trials 150] [--days 120]
"""
from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from scripts.test_moex_follow import load, simulate


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=150)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--equity", type=float, default=50000.0)
    ap.add_argument("--train-frac", type=float, default=0.7)
    args = ap.parse_args()

    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    picked, data = await load(args.days)
    idx = data.get("BBG00KDWPPW2") or []
    n_bars = len(idx)
    split = int(n_bars * args.train_frac)
    print(f"баров {n_bars} | train до {idx[split]['ts']} | valid после | тикеров {len(picked)}", flush=True)

    def _run(p, start_i, end_i):
        return simulate(picked, data, entry_thr=p["entry_thr"], exit_thr=p["exit_thr"],
                        hold=p["hold"], k=p["k"], fast=p["fast"], slow=p["slow"],
                        pos_pct=p["pos_pct"], lev=p["lev"], equity0=args.equity,
                        start_i=start_i, end_i=end_i)

    def objective(trial):
        fast = trial.suggest_int("fast", 5, 40)
        slow = trial.suggest_int("slow", fast + 10, 120)
        p = {
            "entry_thr": trial.suggest_float("entry_thr", 0.0, 0.30, step=0.01),
            "exit_thr": trial.suggest_float("exit_thr", -0.25, 0.05, step=0.01),
            "hold": trial.suggest_int("hold", 1, 24),
            "k": trial.suggest_int("k", 5, 20),
            "fast": fast, "slow": slow,
            "pos_pct": 0.5, "lev": 2.0,
        }
        r = _run(p, 0, split)
        if r["n"] < 50 or r["maxdd"] < -25.0:
            return -1e6
        trial.set_user_attr("valid_net", None)
        return r["net"]

    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=7))
    study.optimize(objective, n_trials=args.trials, show_progress_bar=False)

    print("\nТоп-5 на train (net) + проверка на valid:")
    print(f"{'#':>2} {'net_tr':>9}{'DD_tr':>7} {'net_val':>9}{'DD_val':>7}{'N_val':>7}"
          f"  thr_in/out hold  K  fast/slow")
    done = sorted([t for t in study.trials if t.value and t.value > -1e5],
                  key=lambda t: -t.value)[:5]
    for i, t in enumerate(done, 1):
        rv = _run({**t.params, "pos_pct": 0.5, "lev": 2.0}, split, n_bars)
        print(f"{i:>2} {t.value:>9.0f}{t.user_attrs.get('dd', 0):>7.1f} "
              f"{rv['net']:>9.0f}{rv['maxdd']:>7.1f}{rv['n']:>7}  "
              f"{t.params['entry_thr']:.2f}/{t.params['exit_thr']:+.2f} "
              f"{t.params['hold']:>4} {t.params['k']:>3} {t.params['fast']}/{t.params['slow']}")
    # baseline на valid для сравнения
    base = {"entry_thr": 0.05, "exit_thr": 0.0, "hold": 6, "k": 10, "fast": 20, "slow": 50,
            "pos_pct": 0.5, "lev": 2.0}
    rb = _run(base, split, n_bars)
    rt = _run(base, 0, split)
    print(f"\nbaseline (0.05/0.0, hold 6, K10, 20/50): train {rt['net']:+.0f}₽ DD {rt['maxdd']}% | "
          f"valid {rb['net']:+.0f}₽ DD {rb['maxdd']}% N {rb['n']}")


if __name__ == "__main__":
    asyncio.run(main())
