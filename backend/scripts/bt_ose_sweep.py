#!/usr/bin/env python3
"""Общий харнесс OsEngine-портов: синтетика + реальные данные (общий для новых и существующих роботов).

Что делает:
  run     — гоняет все порты по сетке параметров × seed'ам детерминированной
            серии (ose.metrics.synthetic_series), сохраняет JSON в reports/.
            Числа воспроизводимы на любой машине: random.Random(seed)
            кроссплатформен, equity-кривая хешируется (sha256).
  compare — построчная сверка двух JSON (Mac vs .8): совпадение метрик с
            допуском --tol и дайджестов equity. Код версонируется sha256
            исходников app/engine/ose/* + app/engine/models.py.
  real    — прогон сеток параметров на РЕАЛЬНЫХ свечах из БД (как
            ose_exit_matrix.py): EngineRunner + CostModel (комиссия/слиппедж),
            внешние выходы x01..x14, период неделя/месяц, параллельно.
            Итог: markdown-таблица (стиль EXPERIMENTS.md, сразу сравнивать) +
            JSON с агрегатами и детальными прогонами по тикерам.

Единицы: run — пункты, 1 контракт, без издержек (эталон порта). real — ₽/шт,
издержки из конфига (дефолт 0.05% + 2 bps), как в бот-тестах и матрице.

Запуск (из backend/):
  python3 scripts/bt_ose_sweep.py                        # синтетика, дефолты
  python3 scripts/bt_ose_sweep.py compare reports/a.json reports/b.json
  python3 scripts/bt_ose_sweep.py real --from 2026-09-01 --to 2026-09-24 \
      --interval 10min --top-tickers 5 --robots envelop_trend,rsi_trade \
      --exits x07,x12,x13 --jobs 3
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
    BbPowerTrade,
    BollingerRevers,
    BollingerTrailing,
    CciTrade,
    EnvelopTrend,
    MacdRevers,
    MacdTrail,
    PriceChannelTrade,
    PriceChannelVolatility,
    Regime,
    RsiContrtrend,
    RsiTrade,
    RviTrade,
    SmaStochastic,
    SmaTrendSample,
    StrategyBollinger,
    TesterTab,
)
from app.engine.strategies import STRATEGY_REGISTRY

ROBOTS = {
    "price_channel": PriceChannelTrade,
    "sma_stoch": SmaStochastic,
    "envelop_trend": EnvelopTrend,
    "rsi_contrtrend": RsiContrtrend,
    "rsi_trade": RsiTrade,
    "bollinger": StrategyBollinger,
    # ---- Wave B (OnScriptIndicators) ----
    "cci_trade": CciTrade,
    "bb_power": BbPowerTrade,
    "rvi_trade": RviTrade,
    "macd_revers": MacdRevers,
    "macd_trail": MacdTrail,
    "bollinger_revers": BollingerRevers,
    "bollinger_trailing": BollingerTrailing,
    "sma_trend": SmaTrendSample,
    "pc_volatility": PriceChannelVolatility,
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
    # ---- Wave B: порты Robots/OnScriptIndicators ----
    "cci_trade": [
        {},
        {"cci_length": 14},
        {"up_line": 200.0, "down_line": -200.0},
        {"regime": Regime.ONLY_LONG},
    ],
    # Дефолт Step=100 оригинала инертен на синтетике (цена ~100):
    # добавляем малые шаги, иначе 0 сделок.
    "bb_power": [
        {},
        {"step": 0.5},
        {"step": 1.0},
        {"step": 2.0},
    ],
    "rvi_trade": [
        {},
        {"rvi_length": 9},
        {"rvi_length": 14},
        {"regime": Regime.ONLY_LONG},
    ],
    "macd_revers": [
        {},
        {"fast": 8, "slow": 21},
        {"fast": 12, "slow": 26, "signal": 5},
        {"regime": Regime.ONLY_SHORT},
    ],
    "macd_trail": [
        {},
        {"trail_stop": 0.5},
        {"trail_stop": 1.0},
        {"regime": Regime.ONLY_LONG},
    ],
    "bollinger_revers": [
        {},
        {"boll_length": 15},
        {"boll_deviation": 2.5},
        {"regime": Regime.ONLY_SHORT},
    ],
    # Расширенные сетки добавим после первого прогона.
    "bollinger_trailing": [{}],
    "sma_trend": [{}],
    "pc_volatility": [{}],
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


def _real_agg(raw: list[dict], labels: list[str], exits: list[str]) -> list[dict]:
    """Агрегаты (робот×параметры×выход) по всем тикерам."""
    out: list[dict] = []
    for label in labels:
        for xc in exits:
            rr = [r for r in raw
                  if r.get("strategy") == label and r.get("exit") == xc and "error" not in r]
            if not rr:
                errs = [r for r in raw if r.get("strategy") == label and r.get("exit") == xc]
                out.append({"label": label, "exit": xc, "trades": 0, "win_rate": 0.0,
                            "pf": None, "net": None, "max_dd_pct": None,
                            "error": errs[0]["error"] if errs else "no rows"})
                continue
            n = sum(r["trades"] for r in rr)
            wins = sum(r["wins"] for r in rr)
            gw, gl = sum(r["gw"] for r in rr), sum(r["gl"] for r in rr)
            net = sum(r["net"] for r in rr)
            _pf = round(gw / gl, 2) if gl > 0 else None
            status = ("CANDIDATE" if (net > 0 and (_pf is None or _pf >= 1.5))
                      else "WATCH" if net > 0 else "REJECT")
            out.append({
                "label": label, "exit": xc, "trades": n,
                "win_rate": round(wins / n * 100, 1) if n else 0.0,
                "pf": _pf, "net": round(net, 2),
                "expectancy": round(net / n, 3) if n else None,
                "max_dd_pct": max((r["max_dd_pct"] or 0) for r in rr),
                "by_ticker": {r["ticker"]: r["net"] for r in rr},
                "sec_total": round(sum(r["sec"] for r in rr), 1),
                "status": status,
            })
    return out


def _real_markdown(agg: list[dict], args, exp_ids: dict | None = None,
                   filters_map: dict | None = None) -> str:
    tick_str = (args.tickers or args.figis) or f"top-{args.top_tickers}"
    header: list[str] = []
    sep: list[str] = []
    if exp_ids:
        header.append("EXP")
        sep.append("---")
    header += ["Робот (params)", "Выход"]
    sep += ["---", "---"]
    header += ["Сделок", "WR", "NET ₽", "PF", "DD%", "Статус"]
    sep += ["---:"] * 5 + ["---"]
    if filters_map:
        header.append("Фильтр")
        sep.append("---")
    lines = [
        f"### REAL sweep {args.dfrom}..{args.dto} · {args.interval} · {tick_str}",
        "",
        f"Комиссия {args.commission:.4f} · слиппедж {args.slippage_bps} bps · qty={args.qty} · "
        f"роботов: {len({a['label'].split(' ', 1)[0] for a in agg})} · прогонов: {len(agg)}",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "|".join(sep) + "|",
    ]
    for a in sorted(agg, key=lambda x: -(x.get("net") or -1e18)):
        eid = (exp_ids or {}).get(a["label"], "")
        cells: list[str] = []
        if exp_ids:
            cells.append(eid)
        cells += [a["label"], a["exit"]]
        if a.get("net") is None:
            cells += ["—", "—", "—", "—", "—", a.get("error", "?")]
        else:
            cells += [str(a["trades"]), f"{a['win_rate']}%", f"{a['net']:,.2f}",
                      f"{a['pf'] if a['pf'] is not None else '∞'}", str(a["max_dd_pct"]),
                      a["status"]]
        if filters_map:
            fr = filters_map.get(a["label"])
            if fr is None:
                cells.append("")
            elif fr["passed"]:
                cells.append("PASS")
            else:
                cells.append("FAIL:" + ",".join(c["rule"] for c in fr["checks"] if not c["pass"]))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _load_spec(path: str) -> dict:
    """Spec-файл прогонов: единый источник и для харнесса, и для проект-теста."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    for k in ("period",):
        if k not in d:
            raise SystemExit(f"spec {path}: нет обязательного ключа {k!r}")
    if not (d.get("robot") or d.get("robots")):
        raise SystemExit(f"spec {path}: нет ни 'robot', ни 'robots'")
    return d


# Поля параметров роботов, попадающие в resolved config (union OseRobotParams).
_RESOLVED_FIELDS = ("length", "deviation", "trail_stop", "length_up", "length_down",
                    "boll_length", "boll_deviation", "sma_length", "rsi_length",
                    "upline", "downline")


def _spec_normalize(spec: dict) -> dict:
    """v0 (`robots`+`tickers`+`interval`) и v1 (`experiment`+`robot`+`universe`+`timeframe`)
    к общему виду."""
    exp = spec.get("experiment") or {}
    out = {
        "name": exp.get("name") or spec.get("name", "spec"),
        "explicit_id": exp.get("id", ""),
        "period": spec["period"],
        "interval": spec.get("timeframe") or spec.get("interval", "10min"),
        "tickers": list(spec.get("universe") or spec.get("tickers") or []),
        "costs": spec.get("costs", {}) or {},
        "execute": spec.get("execution") or spec.get("execute", {}) or {},
        "filters": spec.get("filters", {}) or {},
        "wf": spec.get("wf", {}) or {},
        "exits": spec.get("exits") or [],
        "outputs": spec.get("outputs", {}) or {},
        "robots": [],
    }
    _src = ([spec["robot"]] if spec.get("robot") else []) + list(spec.get("robots") or [])
    for r in _src:
        name = str(r.get("name") or r.get("robot") or "").strip()
        if name.startswith("ose_") and name[4:] in ROBOTS:
            name = name[4:]
        out["robots"].append({
            "robot": name,
            "params": dict(r.get("params") or {}),
            "sweep": dict(r.get("sweep") or {}),
        })
    return out


def _exp_next_number() -> int:
    """Следующий номер EXP по уже записанным reports/experiments/EXP-*.json."""
    d = Path("reports/experiments")
    mx = 0
    if d.exists():
        for p in d.glob("EXP-*.json"):
            try:
                mx = max(mx, int(p.stem.split("-", 1)[1]))
            except Exception:
                continue
    return mx + 1


def _resolved_params(robot: str, kwargs: dict) -> dict:
    """Эффективные параметры робота: из kwargs, иначе дефолт конструктора."""
    import inspect
    cls = ROBOTS[robot]
    sig = inspect.signature(cls.__init__).parameters
    res: dict = {}
    for f in _RESOLVED_FIELDS:
        if f not in sig:
            continue
        if f in kwargs:
            res[f] = kwargs[f]
        else:
            d = sig[f].default
            if d is not inspect.Parameter.empty and not isinstance(d, Regime):
                res[f] = d
    return res


def _apply_filters(metrics: dict, filters: dict) -> dict:
    """Фильтры из spec: PASS/FAIL с причинами. FAIL не удаляет эксперимент."""
    checks: list[dict] = []
    for key, mkey in (("min_trades", "trades"),
                      ("min_profit_factor", "profit_factor"),
                      ("min_net_pnl", "net_pnl"),
                      ("max_drawdown_pct", "max_drawdown_pct"),
                      ("min_win_rate", "win_rate")):
        if key not in filters:
            continue
        thr = filters[key]
        val = metrics.get(mkey)
        if key == "min_profit_factor":
            # PF=None = убытков не было: PASS, если эксперимент не в минусе
            ok = (val is None and (metrics.get("net_pnl") or 0) > 0) or \
                 (val is not None and val >= thr)
        elif key == "max_drawdown_pct":
            ok = val is not None and val <= thr
        else:
            ok = val is not None and val >= thr
        checks.append({"rule": key, "threshold": thr, "value": val, "pass": bool(ok)})
    return {"passed": all(c["pass"] for c in checks), "checks": checks}


def compare_experiments(a) -> int:
    """compare-exp: метрики рядом + что изменилось в конфиге (первый EXP — база)."""
    def _load(eid: str) -> dict:
        p = Path(a.dir) / f"{eid}.json"
        if not p.exists():
            raise SystemExit(f"нет файла эксперимента: {p}")
        return json.loads(p.read_text(encoding="utf-8"))

    exps = [_load(e) for e in a.ids]
    base = exps[0]
    print(f"=== compare-exp · база {base['experiment_id']} ({base.get('name', '')}) ===")
    keys = [("net_pnl", "NET"), ("profit_factor", "PF"), ("win_rate", "WR%"),
            ("trades", "Сделок"), ("expectancy", "Exp"), ("max_drawdown_pct", "DD%"),
            ("commission", "Комис."), ("slippage", "Слип.")]
    hdr = "метрика".ljust(10) + "".join(e["experiment_id"].rjust(14) for e in exps)
    if len(exps) > 1:
        hdr += "Δ(хвост−база)".rjust(16)
    print(hdr)
    for mk, title in keys:
        row = title.ljust(10)
        for e in exps:
            v = (e.get("metrics") or {}).get(mk)
            row += (f"{v:,.2f}" if isinstance(v, (int, float)) else "—").rjust(14)
        if len(exps) > 1:
            v0 = (base.get("metrics") or {}).get(mk)
            vn = (exps[-1].get("metrics") or {}).get(mk)
            dv = (vn - v0) if isinstance(v0, (int, float)) and isinstance(vn, (int, float)) else None
            row += (f"{dv:+,.2f}" if dv is not None else "—").rjust(16)
        print(row)
    for e in exps:
        f = e.get("filters") or {}
        if f and f.get("checks"):
            fails = [c["rule"] for c in f.get("checks", []) if not c["pass"]]
            print(f"{e['experiment_id']}: фильтр " +
                  ("PASS" if f.get("passed") else "FAIL:" + ",".join(fails)))
    dh = {e["experiment_id"]: e.get("data_hash") for e in exps}
    if len(set(dh.values())) > 1:
        print(f"⚠ data_hash различается: {dh}")
    bcfg = base.get("config") or {}
    for e in exps[1:]:
        cfg = e.get("config") or {}
        print(f"\n--- {e['experiment_id']} vs {base['experiment_id']} ---")
        bv, cv = bcfg.get("params") or {}, cfg.get("params") or {}
        for k in sorted(set(bv) | set(cv)):
            if bv.get(k) != cv.get(k):
                print(f"  params.{k}: {bv.get(k)} → {cv.get(k)}")
        for sec in ("robot", "exits", "timeframe", "universe", "period", "costs"):
            if bcfg.get(sec) != cfg.get(sec):
                print(f"  {sec}: {bcfg.get(sec)} → {cfg.get(sec)}")
    return 0


def _spec_configs(spec: dict) -> list[tuple[str, str, dict]]:
    """[(label, robot, kwargs)] из spec; sweep = декартово произведение значений."""
    import itertools
    out: list[tuple[str, str, dict]] = []
    for r in spec.get("robots", []):
        robot = str(r.get("robot", "")).strip()
        if robot not in ROBOTS and robot not in STRATEGY_REGISTRY:
            print(f"⚠ spec: неизвестный робот/стратегия {robot!r} — пропущен")
            continue
        base = dict(r.get("params") or {})
        sweep = r.get("sweep") or {}
        if sweep:
            keys = list(sweep.keys())
            for combo in itertools.product(*(sweep[k] for k in keys)):
                p = dict(base)
                p.update(dict(zip(keys, combo)))
                out.append((f"{robot} {json.dumps(p, sort_keys=True, separators=(',', ':'))}",
                            robot, p))
        else:
            out.append((f"{robot} {json.dumps(base, sort_keys=True, separators=(',', ':'))}",
                        robot, base))
    return out


def bot_config(a) -> int:
    """Превратить конфиг из spec в нагрузку /bot/mode (проект-тест) и, по флагу, запустить."""
    spec = _spec_normalize(_load_spec(a.spec))
    configs = _spec_configs(spec)
    if not configs:
        print("spec: нет конфигов")
        return 1
    idx = max(0, min(a.robot_index, len(configs) - 1))
    label, robot, kwargs = configs[idx]
    name = (a.name or f"{spec.get('name', 'spec')}:{label}")
    # нормализация как в /bot/mode: :/" и пр. заменяются на _
    _bad = set("\\/?%*:|\"<>")
    name = "".join(c if c not in _bad else "_" for c in name).strip()[:48]
    payload = {
        "mode": "test",
        "test_name": name,
        "replay_start": f"{spec['period'][0]}T04:00:00Z",
        "replay_end": f"{spec['period'][1]}T21:59:00Z",
        "replay_pace": "fast",
        "test_engine": (f"ose_{robot}" if robot in ROBOTS else robot),
        "test_interval": spec.get("interval", "10min"),
        "test_params": kwargs,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    if a.start:
        import urllib.request
        req = urllib.request.Request(
            f"{a.url}/api/v1/bot/mode",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("started:", resp.read().decode("utf-8")[:300])
    print(f"\nПаритет: харнесс реальные данные — bt_ose_sweep.py real --spec {a.spec}\n"
          f"Проект-тест после финиша: {a.url}/api/v1/bot/test_stats?test_name={name}")
    return 0


def _metrics_from_rows(rows: list[dict]) -> dict:
    """Сводные метрики конфига по тикерам (общий расчёт для EXP и WF)."""
    n = sum(r["trades"] for r in rows)
    wins = sum(r["wins"] for r in rows)
    gw, gl = sum(r["gw"] for r in rows), sum(r["gl"] for r in rows)
    net = sum(r["net"] for r in rows)
    return {
        "trades": n, "wins": wins,
        "win_rate": round(wins / n * 100, 2) if n else 0.0,
        "gross_win": round(gw, 4), "gross_loss": round(gl, 4),
        "net_pnl": round(net, 4),
        "profit_factor": round(gw / gl, 2) if gl > 0 else None,
        "expectancy": round(net / n, 3) if n else None,
        "max_drawdown_pct": max((r.get("max_dd_pct") or 0) for r in rows) if rows else None,
        "commission": round(sum(r.get("commission", 0) or 0 for r in rows), 4),
        "slippage": round(sum(r.get("slippage", 0) or 0 for r in rows), 4),
        "turnover": round(sum(r.get("turnover", 0) or 0 for r in rows), 2),
    }


def _wf_phases(period: list[str], iters: int, pct_oos: float, last_in_sample: bool = False):
    """Нарезка InSample/OutOfSample (x = Y + Y/P*C, см. OsEngine optimizer §3).

    В отличие от оригинала — без «дырки в сутки»: OOS начинается на следующий
    день после IS и заканчивается перед следующим IS."""
    from datetime import date as _d
    from datetime import timedelta as _td
    d0, d1 = _d.fromisoformat(str(period[0])), _d.fromisoformat(str(period[1]))
    total = (d1 - d0).days + 1
    y = max(1, int(total / max(1, iters) / (1 + max(0.0, pct_oos) / 100.0)))
    f = max(1, int(round(y * max(0.0, pct_oos) / 100.0)))
    out: list[tuple[str, str, str]] = []
    cur = d0
    for _ in range(iters):
        is_end = cur + _td(days=y - 1)
        if is_end > d1:
            break
        out.append(("IS", cur.isoformat(), is_end.isoformat()))
        oos_start = is_end + _td(days=1)
        if oos_start > d1:
            break
        oos_end = min(oos_start + _td(days=f - 1), d1)
        out.append(("OOS", oos_start.isoformat(), oos_end.isoformat()))
        cur = oos_end + _td(days=1)
    if last_in_sample and cur <= d1:
        out.append(("IS", cur.isoformat(), d1.isoformat()))
    return out


def _run_grid(configs, exits, figis, period, args, *, jobs: int, artifacts: bool = False,
              cache: bool = True, code_sha: str = "") -> list[dict]:
    """Прогон (конфиги × выходы × тикеры) на одном отрезке через общий воркер."""
    import multiprocessing as mp
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ose_exit_matrix as oem
    tf_s = oem.TF_SECONDS[args.interval]
    tasks: list[dict] = []
    for label, rn, kwargs in configs:
        _raw = rn in ROBOTS
        _sid = f"ose_{rn}" if _raw else rn
        _rkw = kwargs if _raw else None
        _params = None if _raw else dict(kwargs)
        for xc in exits:
            for f, tk in figis:
                tasks.append({
                    "sid": _sid, "xc": xc, "figi": f, "ticker": tk,
                    "params": _params, "robot_kwargs": _rkw, "label": label,
                    "artifacts": artifacts, "cache": cache, "code_sha": code_sha,
                    "dfrom": period[0], "dto": period[1], "tf_s": tf_s,
                    "qty": args.qty, "commission": args.commission,
                    "slippage_bps": args.slippage_bps, "capital": args.capital,
                })
    raw: list[dict] = []
    if jobs <= 1:
        for t in tasks:
            raw.append(oem._run_combo(t))
    else:
        with mp.Pool(jobs) as pool:
            for res in pool.imap_unordered(oem._run_combo, tasks):
                raw.append(res)
    return raw


def _wf_metrics(raw: list[dict], configs) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for label, _rn, _kw in configs:
        rows = [r for r in raw if r.get("strategy") == label and "error" not in r]
        out[label] = _metrics_from_rows(rows)
    return out


def run_wf(args) -> int:
    """Walk-forward: IS (сетка → фильтр → top-K) → OOS → матрица выживаемости → robustness."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ose_exit_matrix as oem
    spec = _spec_normalize(_load_spec(args.spec))
    wf = spec.get("wf") or {}
    iters = int(wf.get("iterations", 3))
    pct = float(wf.get("percent_oos", 25))
    top_k = int(wf.get("top_k", 3))
    rank_by = str(wf.get("rank_by", "net_pnl"))
    last_is = bool(wf.get("last_in_sample", False))
    rob_cfg = wf.get("robustness") or {}
    configs = _spec_configs(spec)
    if not configs:
        print("нет конфигов")
        return 1
    exits = [e for e in (spec.get("exits") or []) if e in oem.EXITS] or ["x07"]
    args.dfrom, args.dto = (args.dfrom, args.dto) if (getattr(args, "dfrom", "") and getattr(args, "dto", "")) \
        else (spec["period"][0], spec["period"][1])
    args.interval = getattr(args, "interval", "") or spec["interval"]
    costs = spec.get("costs") or {}
    args.commission = float(costs.get("commission", getattr(args, "commission", 0.0005)))
    args.slippage_bps = float(costs.get("slippage_bps", getattr(args, "slippage_bps", 2.0)))
    args.qty = int(costs.get("qty", getattr(args, "qty", 1)))
    args.capital = float(costs.get("capital", getattr(args, "capital", 100_000.0)))
    args.jobs = int(getattr(args, "jobs", 0) or (spec.get("execute") or {}).get("jobs", 3))
    if not (getattr(args, "tickers", "") or getattr(args, "figis", "")):
        if spec.get("tickers"):
            args.tickers = ",".join(str(t) for t in spec["tickers"])
            args.figis = ""
    eng = oem._engine_sync()
    figis = oem._resolve_figis(eng, args)
    if not figis:
        print("нет тикеров")
        return 1
    filters = dict(spec.get("filters") or {})
    cache_on = bool((spec.get("execute") or {}).get("cache", True)) and not getattr(args, "no_cache", False)
    code_sha = _code_sha256()
    phases = _wf_phases([args.dfrom, args.dto], iters, pct, last_is)
    print(f"WF {spec['name']}: {len(phases)} фаз · {len(configs)} конфигов × {len(exits)} выходов × "
          f"{len(figis)} тикеров · фильтры {filters or '—'} · jobs={args.jobs}")
    for typ, a, b in phases:
        print(f"  {typ}: {a}..{b}")
    t0 = time.time()
    phase_results: list[dict] = []
    selected_per_iter: list[dict] = []
    for typ, a, b in phases:
        raw = _run_grid(configs, exits, figis, [a, b], args, jobs=args.jobs,
                        artifacts=False, cache=cache_on, code_sha=code_sha)
        errs = [r for r in raw if "error" in r]
        if errs:
            print(f"  ⚠ {len(errs)} прогонов с ошибкой (в метриках учтены как 0), "
                  f"напр.: {errs[0].get('error', '')[:120]}")
        by_label = _wf_metrics(raw, configs)
        passed = [l for l in by_label
                  if (not filters or _apply_filters(by_label[l], filters)["passed"])]
        phase_results.append({"type": typ, "period": [a, b], "by_label": by_label, "passed": passed})
        if typ == "IS":
            ranked = sorted(passed or list(by_label),
                            key=lambda l: -(by_label[l].get(rank_by) or -1e18))
            sel = ranked[:top_k]
            selected_per_iter.append({"iter": len(selected_per_iter) + 1, "is_period": [a, b],
                                      "selected": sel, "is_metrics": {l: by_label[l] for l in sel}})
            note = "" if passed else "  (фильтр не прошёл никто — берём топ без фильтра)"
            print(f"  IS #{len(selected_per_iter)} ({a}..{b}): выбрано {sel}{note}")
        else:
            if selected_per_iter:
                sel = selected_per_iter[-1]["selected"]
                selected_per_iter[-1].setdefault("oos", []).append(
                    {"period": [a, b], "metrics": {l: by_label[l] for l in sel}})
                print(f"  OOS ({a}..{b}): " + ", ".join(
                    f"{l.split(' ')[0]} net={by_label[l]['net_pnl']:+.1f}" for l in sel))
    # --- сводка IS→OOS + OOS-матрица ---
    print("\n=== WF: IS → OOS ===")
    sel_stats: dict[str, dict] = {}
    for it in selected_per_iter:
        print(f"IS #{it['iter']} {it['is_period'][0]}..{it['is_period'][1]}")
        for l in it["selected"]:
            im = it["is_metrics"][l]
            st = sel_stats.setdefault(l, {"oos_nets": [], "is_nets": []})
            st["is_nets"].append(im["net_pnl"])
            chunks = []
            for oos in it.get("oos", []):
                om = oos["metrics"][l]
                st["oos_nets"].append(om["net_pnl"])
                chunks.append(f"{oos['period'][0]}..{oos['period'][1]}: {om['net_pnl']:+.1f}")
            print(f"  {l:<44} IS={im['net_pnl']:+9.1f} → OOS " + ("; ".join(chunks) or "—"))
    print("\n=== OOS-матрица (выживаемость) ===")
    for l, st in sorted(sel_stats.items(),
                        key=lambda kv: -(sum(kv[1]["oos_nets"]) if kv[1]["oos_nets"] else -1e18)):
        nets = sorted(st["oos_nets"])
        if not nets:
            continue
        pos = sum(1 for x in nets if x > 0)
        med = nets[len(nets) // 2]
        print(f"  {l:<44} OOS-периодов={len(nets)} положительных={pos} median={med:+.1f} "
              f"worst={min(nets):+.1f} dispersion={max(nets) - min(nets):.1f} "
              f"IS_sum={sum(st['is_nets']):+.1f} OOS_sum={sum(nets):+.1f}")
    # --- robustness: соседи лучшего по OOS конфига на полном периоде ---
    robustness: list[dict] = []
    if rob_cfg and sel_stats:
        best_label = max(sel_stats, key=lambda l: (sum(sel_stats[l]["oos_nets"])
                                                   if sel_stats[l]["oos_nets"] else -1e18))
        best = next((c for c in configs if c[0] == best_label), None)
        delta_pct = float(rob_cfg.get("delta_pct", 20))
        if best is not None:
            _lbl, _rn, kwargs = best
            base_resolved = (_resolved_params(_rn, kwargs) if _rn in ROBOTS else dict(kwargs))
            print(f"\n=== Robustness: соседи ±{delta_pct:g}% вокруг {best_label} (полный период) ===")
            neighbors: list[tuple[str, dict]] = [("base", dict(kwargs))]
            for k, v in base_resolved.items():
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v == 0:
                    continue
                if isinstance(v, int):
                    lo, hi = max(1, int(round(v * (1 - delta_pct / 100.0)))), \
                        max(1, int(round(v * (1 + delta_pct / 100.0))))
                else:
                    lo = round(v * (1 - delta_pct / 100.0), 4)
                    hi = round(v * (1 + delta_pct / 100.0), 4)
                nb = dict(kwargs); nb[k] = lo; neighbors.append((f"{k}={lo}", nb))
                nb = dict(kwargs); nb[k] = hi; neighbors.append((f"{k}={hi}", nb))
            nconfigs = [(f"{_rn} [{tag}]", _rn, kw) for tag, kw in neighbors]
            # Окно robustness: period_days от конца периода (по умолчанию 10 дней) —
            # соседи на полном 3-месячном периоде для тяжёлых роботов нерентабельны.
            from datetime import date as _d
            from datetime import timedelta as _td
            _days = int(rob_cfg.get("period_days", 10) or 10)
            _d1 = _d.fromisoformat(str(spec["period"][1]))
            _d0 = max(_d.fromisoformat(str(spec["period"][0])), _d1 - _td(days=_days - 1))
            rob_period = [_d0.isoformat(), _d1.isoformat()]
            print(f"  (окно robustness: {rob_period[0]}..{rob_period[1]})")
            raw = _run_grid(nconfigs, exits, figis, rob_period, args, jobs=args.jobs,
                            artifacts=False, cache=cache_on, code_sha=code_sha)
            for label, _rn2, _kw in nconfigs:
                rows = [r for r in raw if r.get("strategy") == label and "error" not in r]
                m = _metrics_from_rows(rows)
                tag = label.split("[", 1)[1].rstrip("]")
                robustness.append({"neighbor": tag, "net_pnl": m["net_pnl"], "trades": m["trades"],
                                   "pf": m["profit_factor"], "win_rate": m["win_rate"]})
                print(f"  {tag:<22} net={m['net_pnl']:+9.1f} trades={m['trades']:<4} "
                      f"pf={m['profit_factor']} wr={m['win_rate']}%")
    dur = time.time() - t0
    summary = {
        "meta": {
            "script": "scripts/bt_ose_sweep.py wf",
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "name": spec["name"], "period": spec["period"], "interval": args.interval,
            "wf": wf, "filters": filters, "rank_by": rank_by, "top_k": top_k,
            "costs": {"commission": args.commission, "slippage_bps": args.slippage_bps,
                      "qty": args.qty, "capital": args.capital},
            "universe": [t for _, t in figis], "code_sha": code_sha,
            "duration_sec": round(dur, 1),
        },
        "phases": phase_results,
        "selected": selected_per_iter,
        "oos_matrix": [{"config": l, "oos_nets": st["oos_nets"], "is_nets": st["is_nets"]}
                       for l, st in sel_stats.items()],
        "robustness": robustness,
    }
    out_dir = Path((spec.get("outputs") or {}).get("dir") or "reports/experiments")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"WF_{spec['name']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nsaved: {out} · {dur:.0f}s")
    return 0


def run_real(args) -> int:
    """Реальные свечи из БД + издержки: сетки параметров × выходы × тикеры."""
    import multiprocessing as mp

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ose_exit_matrix as oem  # общий движок real-прогонов (кэш, пул, политики)

    spec = _load_spec(args.spec) if getattr(args, "spec", "") else None
    if spec is None and not (args.dfrom and args.dto):
        print("нужны --from/--to или --spec")
        return 1
    if spec is not None:
        spec = _spec_normalize(spec)
        args.dfrom = spec["period"][0]
        args.dto = spec["period"][1]
        args.interval = spec.get("interval", args.interval)
        if spec.get("tickers"):
            args.tickers = ",".join(str(t) for t in spec["tickers"])
            args.figis = ""
        costs = spec.get("costs", {}) or {}
        args.commission = float(costs.get("commission", args.commission))
        args.slippage_bps = float(costs.get("slippage_bps", args.slippage_bps))
        args.qty = int(costs.get("qty", args.qty))
        args.capital = float(costs.get("capital", args.capital))
        args.jobs = int((spec.get("execute", {}) or {}).get("jobs", args.jobs))
    _filters = dict(spec.get("filters") or {}) if spec is not None else {}
    _cache_on = bool(spec is not None
                     and (spec.get("execute", {}) or {}).get("cache", True)
                     and not getattr(args, "no_cache", False))
    _code_sha = _code_sha256()

    tf_s = oem.TF_SECONDS[args.interval]
    eng = oem._engine_sync()
    figis = oem._resolve_figis(eng, args)
    if not figis:
        print("нет тикеров для прогона")
        return 1
    exits = ([e for e in (spec.get("exits") or []) if e in oem.EXITS] if spec is not None else []) \
        or [e.strip() for e in args.exits.split(",") if e.strip() in oem.EXITS]
    if spec is not None:
        configs = _spec_configs(spec)  # [(label, robot, kwargs)]
    else:
        robots = ([r.strip() for r in args.robots.split(",")
                   if r.strip() in ROBOTS or r.strip() in STRATEGY_REGISTRY]
                  if args.robots else list(ROBOTS.keys()))
        configs = []
        for rn in robots:
            if rn in ROBOTS:
                for params in GRIDS.get(rn, [{}]):
                    configs.append((f"{rn} {_params_json(params)}", rn, dict(params)))
            else:
                configs.append((f"{rn} {{}}", rn, {}))
    if not configs or not exits:
        print("нет конфигов или выходов")
        return 1

    # MVP-1: каждому конфигу — immutable EXPERIMENT_ID (или явный из spec)
    exp_ids: dict[str, str] = {}
    if spec is not None:
        nxt = _exp_next_number()
        for label, _rn, _kw in configs:
            if spec["explicit_id"] and len(configs) == 1:
                exp_ids[label] = spec["explicit_id"]
            else:
                exp_ids[label] = f"EXP-{nxt:06d}"
                nxt += 1

    tasks: list[dict] = []
    labels: list[str] = []
    for label, rn, kwargs in configs:
        labels.append(label)
        _raw = rn in ROBOTS
        _sid = f"ose_{rn}" if _raw else rn
        _rkw = kwargs if _raw else None
        _params = None if _raw else dict(kwargs)
        for xc in exits:
            for f, tk in figis:
                tasks.append({
                    "sid": _sid, "xc": xc, "figi": f, "ticker": tk,
                    "params": _params, "robot_kwargs": _rkw, "label": label,
                    "artifacts": spec is not None,
                    "cache": _cache_on, "code_sha": _code_sha,
                    "dfrom": args.dfrom, "dto": args.dto, "tf_s": tf_s,
                    "qty": args.qty, "commission": args.commission,
                    "slippage_bps": args.slippage_bps, "capital": args.capital,
                })
    print(f"REAL sweep {args.dfrom}..{args.dto} · {args.interval} · "
          f"{len(labels)} конфигов × {len(exits)} выходов × {len(figis)} тикеров "
          f"= {len(tasks)} прогонов · комиссия {args.commission:.4f} · слиппедж {args.slippage_bps} bps "
          f"· jobs={args.jobs}")
    t0 = time.time()
    raw: list[dict] = []
    if args.jobs <= 1:
        for i, task in enumerate(tasks, 1):
            res = oem._run_combo(task)
            raw.append(res)
            print(f"  [{i}/{len(tasks)}] {res['strategy']} {res['exit']} {res['ticker']} "
                  f"net={res.get('net', '—')} {res['sec']}s{' (cached)' if res.get('cached') else ''} "
                  f"{res.get('error', '')}", flush=True)
    else:
        with mp.Pool(args.jobs) as pool:
            for i, res in enumerate(pool.imap_unordered(oem._run_combo, tasks), 1):
                raw.append(res)
                print(f"  [{i}/{len(tasks)}] {res['strategy']} {res['exit']} {res['ticker']} "
                      f"net={res.get('net', '—')} {res['sec']}s{' (cached)' if res.get('cached') else ''} "
                      f"{res.get('error', '')}", flush=True)
    dur = time.time() - t0
    agg = _real_agg(raw, labels, exits)
    exp_metrics: dict[str, dict] = {}
    if spec is not None:
        for label in labels:
            rows = [r for r in raw if r.get("strategy") == label and "error" not in r]
            n = sum(r["trades"] for r in rows)
            wins = sum(r["wins"] for r in rows)
            gw, gl = sum(r["gw"] for r in rows), sum(r["gl"] for r in rows)
            net = sum(r["net"] for r in rows)
            exp_metrics[label] = {
                "trades": n, "wins": wins,
                "win_rate": round(wins / n * 100, 2) if n else 0.0,
                "gross_win": round(gw, 4), "gross_loss": round(gl, 4),
                "net_pnl": round(net, 4),
                "profit_factor": round(gw / gl, 2) if gl > 0 else None,
                "expectancy": round(net / n, 3) if n else None,
                "max_drawdown_pct": max((r.get("max_dd_pct") or 0) for r in rows) if rows else None,
                "commission": round(sum(r.get("commission", 0) or 0 for r in rows), 4),
                "slippage": round(sum(r.get("slippage", 0) or 0 for r in rows), 4),
                "turnover": round(sum(r.get("turnover", 0) or 0 for r in rows), 2),
            }
    filters_map = ({lbl: _apply_filters(exp_metrics[lbl], _filters) for lbl in labels}
                   if _filters else {})
    md = _real_markdown(agg, args, exp_ids if spec is not None else None,
                        filters_map if filters_map else None)
    print("\n" + md)
    report = {
        "meta": {
            "script": "scripts/bt_ose_sweep.py real",
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "git_commit": _git_commit(),
            "code_sha256": _code_sha256(),
            "period": [args.dfrom, args.dto],
            "interval": args.interval,
            "tickers": [t for _, t in figis],
            "commission": args.commission,
            "slippage_bps": args.slippage_bps,
            "qty": args.qty,
            "jobs": args.jobs,
            "duration_sec": round(dur, 1),
        },
        "aggregate": agg,
        "raw": raw,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    out_md = out.with_suffix(".md")
    out_md.write_text(md, encoding="utf-8")
    print(f"saved: {out} · {out_md} ({len(raw)} runs) in {dur:.0f}s")

    # MVP-1: ExperimentResult на каждый конфиг spec (reports/experiments/EXP-*.json)
    if spec is not None:
        exp_dir = Path(spec.get("outputs", {}).get("dir") or "reports/experiments")
        exp_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for label, rn, kwargs in configs:
            eid = exp_ids[label]
            rows = [r for r in raw if r.get("strategy") == label and "error" not in r]
            cfg = {
                "robot": (f"ose_{rn}" if rn in ROBOTS else rn),
                "params": (_resolved_params(rn, kwargs) if rn in ROBOTS else dict(kwargs)),
                "exits": exits,
                "timeframe": args.interval,
                "universe": [t for _, t in figis],
                "period": [args.dfrom, args.dto],
                "costs": {"commission": args.commission, "slippage_bps": args.slippage_bps,
                          "qty": args.qty, "capital": args.capital},
            }
            cfg_hash = hashlib.sha256(
                json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str).encode()
            ).hexdigest()[:16]
            dhash = hashlib.sha256(
                "|".join(sorted(f"{r['ticker']}:{r.get('data_hash', '')}" for r in rows)).encode()
            ).hexdigest()[:16]
            payload = {
                "experiment_id": eid,
                "name": f"{spec['name']}:{label}",
                "status": "completed" if rows else "failed",
                "config": cfg,
                "config_hash": cfg_hash,
                "code_sha": _code_sha256(),
                "data_hash": dhash,
                "metrics": exp_metrics.get(label, {}),
                "filters": filters_map.get(label),
                "artifacts": {
                    "per_ticker": {r["ticker"]: {
                        "data_hash": r.get("data_hash"),
                        "trades": r.get("trades_detail", []),
                        "equity": r.get("equity", []),
                    } for r in rows},
                },
                "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source_spec": str(Path(args.spec)),
            }
            (exp_dir / f"{eid}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            written.append(f"{eid}({label.split(' ')[0]})")
        print(f"experiments: {', '.join(written)} → {exp_dir}")
    return 0


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

    if argv[:1] == ["wf"]:
        ap = argparse.ArgumentParser(
            prog="bt_ose_sweep.py wf",
            description="Walk-forward (MVP-3): IS→filter→topK→OOS + OOS-матрица + robustness")
        ap.add_argument("--spec", required=True)
        ap.add_argument("--from", dest="dfrom", default="", help="YYYY-MM-DD; перекрывает период spec")
        ap.add_argument("--to", dest="dto", default="")
        ap.add_argument("--jobs", type=int, default=0, help="0 = из spec.execution.jobs")
        ap.add_argument("--commission", type=float, default=0.0005)
        ap.add_argument("--slippage-bps", type=float, default=2.0)
        ap.add_argument("--qty", type=int, default=1)
        ap.add_argument("--capital", type=float, default=100_000.0)
        ap.add_argument("--tickers", default="")
        ap.add_argument("--figis", default="")
        ap.add_argument("--top-tickers", type=int, default=5)
        ap.add_argument("--interval", default="",
                        choices=["", "1min", "5min", "10min", "15min", "30min", "1h"],
                        help="пусто = из spec")
        ap.add_argument("--no-cache", action="store_true")
        a2 = ap.parse_args(argv[1:])
        return run_wf(a2)

    if argv[:1] == ["compare-exp"]:
        ap = argparse.ArgumentParser(
            prog="bt_ose_sweep.py compare-exp",
            description="Сравнить эксперименты (EXP-*.json): метрики рядом + что изменилось")
        ap.add_argument("ids", nargs="+", help="EXP-000001 EXP-000002 ... (первый — база)")
        ap.add_argument("--dir", default="reports/experiments")
        a2 = ap.parse_args(argv[1:])
        return compare_experiments(a2)

    if argv[:1] == ["bot-config"]:
        ap = argparse.ArgumentParser(
            prog="bt_ose_sweep.py bot-config",
            description="Сгенерировать (и опционально запустить) проект-тест /bot/mode "
                        "из того же spec-файла, что и харнесс")
        ap.add_argument("--spec", required=True)
        ap.add_argument("--robot-index", type=int, default=0,
                        help="индекс конфига в spec (порядок как в файле)")
        ap.add_argument("--name", default="", help="test_name (дефолт <spec.name>:<label>, до 48 симв.)")
        ap.add_argument("--url", default="http://192.168.1.7:8000")
        ap.add_argument("--start", action="store_true", help="сразу POST /bot/mode")
        a2 = ap.parse_args(argv[1:])
        return bot_config(a2)

    if argv[:1] == ["real"]:
        ap = argparse.ArgumentParser(
            prog="bt_ose_sweep.py real",
            description="Прогон сеток GRIDS на реальных свечах из БД с издержками "
                        "(общий движок с ose_exit_matrix.py)")
        ap.add_argument("--from", dest="dfrom", default="", help="YYYY-MM-DD (UTC); при --spec не нужен")
        ap.add_argument("--to", dest="dto", default="", help="YYYY-MM-DD (UTC, включительно)")
        ap.add_argument("--spec", default="", help="JSON-файл прогонов (перекрывает --robots/--tickers/период)")
        ap.add_argument("--interval", default="10min",
                        choices=["1min", "5min", "10min", "15min", "30min", "1h"])
        ap.add_argument("--tickers", default="", help="SBER,LKOH,...")
        ap.add_argument("--figis", default="")
        ap.add_argument("--top-tickers", type=int, default=5,
                        help="если тикеры не заданы — топ N по объёму данных за период")
        ap.add_argument("--robots", default="", help="пусто = все из ROBOTS (сетки GRIDS)")
        ap.add_argument("--exits", default="x07,x12,x13",
                        help="выходы из реестра ose_exit_matrix (x01..x14)")
        ap.add_argument("--commission", type=float, default=0.0005)
        ap.add_argument("--slippage-bps", type=float, default=2.0)
        ap.add_argument("--qty", type=int, default=1)
        ap.add_argument("--capital", type=float, default=100_000.0)
        ap.add_argument("--jobs", type=int, default=3)
        ap.add_argument("--no-cache", action="store_true",
                        help="отключить дисковый кэш прогонов (reports/ose_cache)")
        ap.add_argument("--out", default="", help="дефолт reports/bt_ose_real_<stamp>.json")
        args = ap.parse_args(argv[1:])
        if not args.out:
            args.out = ("reports/bt_ose_real_"
                        + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M") + ".json")
        return run_real(args)

    ap = argparse.ArgumentParser(
        description="Свип-тест OsEngine-портов с сохранением JSON для сверки")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"куда писать JSON (дефолт {DEFAULT_OUT})")
    ap.add_argument("--bars", type=int, default=DEFAULT_BARS)
    ap.add_argument("--seeds", default=DEFAULT_SEEDS, help="через запятую")
    args = ap.parse_args(argv)
    return run_sweep(args)


if __name__ == "__main__":
    raise SystemExit(main())
