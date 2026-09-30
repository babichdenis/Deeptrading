#!/usr/bin/env python3
"""Быстрая матрица «робот × выход» на исторических свечах из БД (batch-движок).

Зачем: бот-тест в runtime идёт часами (гейты/логи/ансамбли на каждую минуту).
Здесь — чистый EngineRunner: роботы-ENTRY × ExitPolicy (x-коды из
docs/ROBOT_TEST_PROTOCOL.md §5) × тикеры × период (неделя/месяц) в
несколько минут (4 ядра, Pool). Это скрининг (E1/E2), не замена стенду S1.

Запуск (из backend/, на машине с БД):
  .venv/bin/python scripts/ose_exit_matrix.py --from 2026-09-18 --to 2026-09-24 \
      --interval 10min --tickers SBER,LKOH,TATN --exits x01,x02,x07

Скорость: ose_all тяжёлый (~30с/неделя/тикер — 6 роботов, инкрементальный
пересчёт ещё не сделан), одиночные роботы 1–10с. Для широких матриц начинай
с одиночных, ose_all — отдельным прогоном. --jobs N (по умолчанию 3).

Выход: пивот-матрица net по (робот × выход), топ-комбо, JSON в reports/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.engine.candlehub import build_tf  # noqa: E402
from app.engine.costs import CostModel  # noqa: E402
from app.engine.exits import (  # noqa: E402
    AtrStopPolicy,
    AtrTrailingPolicy,
    ExitPlan,
    ExitPolicy,
    FixedSlTpPolicy,
)
from app.engine.metrics import full_report, summarize  # noqa: E402
from app.engine.models import Candle, Signal  # noqa: E402
from app.engine.ose.robots import (  # noqa: E402
    EnvelopTrend,
    PriceChannelTrade,
    RsiContrtrend,
    RsiTrade,
    StrategyBollinger,
    TesterTab,
)
from app.engine.ose.strategy import _WARMUP as OSE_WARMUP  # noqa: E402
from app.engine.ose.strategy import _robot_vote  # noqa: E402
from app.engine.runner import EngineConfig, EngineRunner  # noqa: E402
from app.engine.strategies import STRATEGY_REGISTRY, build_strategy  # noqa: E402

TF_SECONDS = {"1min": 60, "5min": 300, "10min": 600, "15min": 900, "30min": 1800, "1h": 3600}


class NoExitPolicy(ExitPolicy):
    """x07/native: защитных выходов нет — только exit-голоса стратегии (как у OsEngine-native)."""
    policy_id = "signal_only"
    version = "1.0.0"

    def plan_entry(self, side, entry_price, bars):
        return ExitPlan(stop_loss=None, take_profit=None)


EXITS: dict[str, tuple[str, callable]] = {
    "x01": ("fixed 1%/2%", lambda: FixedSlTpPolicy(stop_pct=0.01, target_pct=0.02)),
    "x02": ("atr_stop 14×2 rr2", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0)),
    "x03": ("atr_trailing 14/2/1/2", lambda: AtrTrailingPolicy(period=14, initial_stop_atr=2.0,
                                                              activation_atr=1.0, trail_distance_atr=2.0)),
    "x04": ("fixed wide 2%/4%", lambda: FixedSlTpPolicy(stop_pct=0.02, target_pct=0.04)),
    "x05": ("atr_stop +BE/trail 1R", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0,
                                                           trail_activation_r=1.0, trail_distance_r=2.0)),
    "x07": ("signal-only (native)", NoExitPolicy),
    "x10": ("atr_stop rr2 + trail 1.5R", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0,
                                                               trail_activation_r=1.0, trail_distance_r=1.5)),
    # --- профили из анализа выходов (10m/q1: лучший по пикам/MAE) ---
    # Активация/дистанция задаются в долях risk (risk = multiplier×ATR):
    # mult=2.0 → 0.25 = 0.5×ATR, 0.375 = 0.75×ATR.
    "x12": ("trail 0.5/0.5×ATR", lambda: AtrStopPolicy(period=14, multiplier=2.0,
                                                       trail_activation_r=0.25, trail_distance_r=0.25)),
    "x13": ("TP 1.5×ATR (SL 2×ATR)", lambda: AtrStopPolicy(period=14, multiplier=2.0, risk_reward=0.75)),
    "x14": ("trail 0.75/0.5×ATR", lambda: AtrStopPolicy(period=14, multiplier=2.0,
                                                        trail_activation_r=0.375, trail_distance_r=0.25)),
}

# --- параметрические варианты OSE-роботов (в обход пустого OseRobotParams):
# чемпионы сетки Wave A из docs/osengine/EXPERIMENTS.md (синтетика).
RAW_ROBOTS: dict[str, type] = {
    "ose_envelop_trend": EnvelopTrend,
    "ose_price_channel": PriceChannelTrade,
    "ose_rsi_trade": RsiTrade,
    "ose_rsi_contrtrend": RsiContrtrend,
    "ose_bollinger": StrategyBollinger,
}


class RawOseSingle:
    """OSE-робот с параметрами конструктора. Поведение = `_OseSingleRobot`:
    голос робота (смена позиции за бар) → Signal(kind)."""

    strategy_id = "ose_raw"

    def __init__(self, sid: str, kwargs: dict | None = None, step_pct: float = 1.0):
        self._sid = sid
        self._kwargs = dict(kwargs or {})
        self._step_pct = step_pct
        self.reset()

    def reset(self) -> None:
        cls = RAW_ROBOTS[self._sid]
        self._tab = TesterTab()
        self._robot = cls(self._tab, **self._kwargs)

    def warmup_bars(self) -> int:
        return OSE_WARMUP.get(self._sid.removeprefix("ose_"), 30)

    def on_bar(self, candles):
        if not candles:
            return None
        vote = _robot_vote(self._tab, self._robot, candles, self._step_pct)
        if vote is None:
            return None
        side, action, kind = vote
        return Signal(strategy_id=f"{self._sid}:{action}", side=side,
                      time=candles[-1].ts, reason=f"raw:{action}", kind=kind)


def _parse_variants(spec: str) -> list[dict]:
    """'sid@k=v,k=v|sid2@k=v' → [{'label','sid','kwargs'}]."""
    out = []
    for part in spec.split("|"):
        part = part.strip()
        if not part or "@" not in part:
            continue
        sid, kv = part.split("@", 1)
        sid = sid.strip()
        if sid not in RAW_ROBOTS:
            print(f"⚠ вариант пропущен (нет RAW_ROBOTS): {sid}")
            continue
        kwargs: dict = {}
        for pair in kv.split(","):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k, v = k.strip(), v.strip()
            try:
                val: object = int(v)
            except ValueError:
                try:
                    val = float(v)
                except ValueError:
                    val = v
            kwargs[k] = val
        tag = ",".join(f"{k}={v}" for k, v in kwargs.items())
        out.append({"label": f"{sid}[{tag}]", "sid": sid, "kwargs": kwargs})
    return out

_CANDLE_CACHE: dict = {}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="dfrom", required=True, help="YYYY-MM-DD (UTC)")
    p.add_argument("--to", dest="dto", required=True, help="YYYY-MM-DD (UTC, включительно)")
    p.add_argument("--interval", default="10min", choices=sorted(TF_SECONDS))
    p.add_argument("--tickers", default="", help="через запятую: SBER,LKOH,...")
    p.add_argument("--figis", default="", help="если нет тикеров — figi через запятую")
    p.add_argument("--top-tickers", type=int, default=5, help="если ничего не задано — топ N по числу свечей")
    p.add_argument("--strategies", default="ose", help="ose | all | список ключей через запятую")
    p.add_argument("--variants", default="",
                   help="OSE-варианты с параметрами: 'sid@k=v,k=v|sid2@k=v' (чемпионы EXPERIMENTS.md)")
    p.add_argument("--exits", default="x01,x02,x03,x04,x07", help="коды из шапки файла")
    p.add_argument("--quorum", type=int, default=1, help="кворум для ose_all")
    p.add_argument("--commission", type=float, default=0.0005)
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--capital", type=float, default=100_000.0)
    p.add_argument("--jobs", type=int, default=3, help="параллельных процессов (машина: 4 ядра)")
    return p.parse_args()


def _sync_url() -> str:
    return get_settings().database_url.replace("+asyncpg", "")


def _engine_sync(url: str | None = None):
    """Синхронный движок с защитой от вечных сетевых ожиданий.

    connect_timeout — не висеть на TCP-коннекте (мёртвый хост/файрвол);
    keepalives — рвать полумёртвые коннекты; statement_timeout — страховка
    от бесконечных запросов. Иначе поток-загрузчик висит в poll() навечно,
    а потребитель (bt_ose_sweep real) deadlock'ится на локе пула.
    """
    return create_engine(
        url or _sync_url(),
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 10,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
            "options": "-c statement_timeout=120000",
        },
    )


def _resolve_figis(eng, args) -> list[tuple[str, str]]:
    """[(figi, ticker)] — по тикерам/figi или топ по числу свечей периода."""
    t0, t1 = f"{args.dfrom} 00:00:00+00", f"{args.dto} 23:59:59+00"
    with eng.connect() as c:
        if args.tickers:
            tks = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            rows = c.execute(text(
                "SELECT figi, ticker FROM instruments WHERE upper(ticker) = ANY(:t)"),
                {"t": tks}).fetchall()
            got = {r[1].upper(): r[0] for r in rows}
            miss = [t for t in tks if t not in got]
            if miss:
                print(f"⚠ нет в instruments: {', '.join(miss)}")
            return [(got[t], t) for t in tks if t in got]
        if args.figis:
            fl = [f.strip() for f in args.figis.split(",") if f.strip()]
            rows = c.execute(text("SELECT figi, ticker FROM instruments WHERE figi = ANY(:f)"),
                             {"f": fl}).fetchall()
            return [(r[0], r[1] or r[0][-6:]) for r in rows]
        rows = c.execute(text(
            "SELECT figi, count(*) n FROM candles "
            "WHERE interval = 1 AND ts >= :t0 AND ts <= :t1 "
            "GROUP BY figi ORDER BY n DESC LIMIT :k"), {"t0": t0, "t1": t1, "k": args.top_tickers}
        ).fetchall()
        figis = [r[0] for r in rows]
        tk = dict(c.execute(text("SELECT figi, ticker FROM instruments WHERE figi = ANY(:f)"),
                            {"f": figis}).fetchall())
        return [(f, tk.get(f) or f[-6:]) for f in figis]


class _RBar:
    """Мини-бар для канонического Resampler (ему нужен атрибут figi)."""
    __slots__ = ("figi", "ts", "open", "high", "low", "close", "volume")

    def __init__(self, figi=None, ts=None, open=None, high=None, low=None, close=None,
                 volume=None, **_kw):
        self.figi = figi
        self.ts = ts
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def _bars_tf_canonical(raw: list[Candle], tf_s: int) -> list[Candle]:
    """Старший ТФ через КАНОНИЧЕСКИЙ Resampler (та же семантика, что ReplayFeed/live).

    REF-001b: раньше здесь был candlehub.build_tf (сетка с меткой по ЗАКРЫТИЮ бакета) —
    расходилось с Resampler (метка по НАЧАЛУ бакета) у границ сессий: харнесс и replay
    торговали разные бары. Теперь единая агрегация у обоих контуров.
    """
    from app.marketdata.resampler import Resampler

    inv = {300: "5min", 600: "10min", 900: "15min", 1800: "30min", 3600: "hour"}
    rs = Resampler(inv[tf_s])
    out: list[Candle] = []
    for b in raw:
        o = rs.feed(_RBar(figi="x", ts=b.ts, open=b.open, high=b.high, low=b.low,
                          close=b.close, volume=b.volume or 0))
        if o is not None:
            out.append(Candle(ts=o.ts, open=o.open, high=o.high, low=o.low,
                              close=o.close, volume=o.volume or 0))
    for o in (rs.flush() or []):
        out.append(Candle(ts=o.ts, open=o.open, high=o.high, low=o.low,
                          close=o.close, volume=o.volume or 0))
    return out


def _load_tf_cached(figi: str, dfrom: str, dto: str, tf_s: int):
    """Свечи целевого TF для фиги (кэш на процесс-воркер: одна загрузка на фигу)."""
    key = (figi, dfrom, dto, tf_s)
    got = _CANDLE_CACHE.get(key)
    if got is not None:
        return got
    eng = _engine_sync()
    raw = _load_1m(eng, figi, f"{dfrom} 00:00:00+00", f"{dto} 23:59:59+00")
    bars = _bars_tf_canonical(raw, tf_s) if tf_s != 60 else raw
    _CANDLE_CACHE[key] = (len(raw), bars)
    return _CANDLE_CACHE[key]


def _load_1m(eng, figi: str, t0: str, t1: str) -> list[Candle]:
    q = ("SELECT ts, open, high, low, close, volume FROM candles "
         "WHERE figi = :f AND interval = 1 AND ts >= :t0 AND ts <= :t1 ORDER BY ts")
    with eng.connect() as c:
        rows = c.execute(text(q), {"f": figi, "t0": t0, "t1": t1}).fetchall()
    return [Candle(ts=r[0], open=float(r[1]), high=float(r[2]), low=float(r[3]),
                   close=float(r[4]), volume=float(r[5] or 0)) for r in rows]


def _strategy_ids(arg: str) -> list[str]:
    if arg == "ose":
        return [k for k in STRATEGY_REGISTRY if k.startswith("ose_")]
    if arg == "all":
        return list(STRATEGY_REGISTRY.keys())
    return [s.strip() for s in arg.split(",") if s.strip() in STRATEGY_REGISTRY]


def _data_hash(bars) -> str:
    """Хэш использованных свечей (MVP-1: data_hash для ExperimentResult/кэша)."""
    h = hashlib.sha256()
    for b in bars:
        h.update(f"{b.ts.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}".encode())
    return h.hexdigest()[:16]


CACHE_DIR = Path("reports/ose_cache")


def _run_cache_key(task: dict, bars) -> str:
    """Ключ кэша прогона: все входы + data_hash + code_sha (MVP-2)."""
    payload = {
        "sid": task.get("sid"), "robot_kwargs": task.get("robot_kwargs"),
        "params": task.get("params"), "xc": task.get("xc"),
        "figi": task.get("figi"), "dfrom": task.get("dfrom"), "dto": task.get("dto"),
        "tf_s": task.get("tf_s"), "qty": task.get("qty"),
        "commission": task.get("commission"), "slippage_bps": task.get("slippage_bps"),
        "capital": task.get("capital"), "artifacts": bool(task.get("artifacts")),
        "data_hash": _data_hash(bars), "code_sha": task.get("code_sha", ""),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _cache_read(key: str):
    p = CACHE_DIR / f"{key}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _cache_write(key: str, res: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_DIR / f".{key}.{__import__('os').getpid()}.tmp"
        tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_DIR / f"{key}.json")
    except Exception:
        pass


def _run_combo(task: dict) -> dict:
    """Один прогон (робот × выход × тикер). Живёт в воркере Pool."""
    t_start = time.time()
    try:
        _n1m, bars = _load_tf_cached(task["figi"], task["dfrom"], task["dto"], task["tf_s"])
        cache_key = None
        if task.get("cache"):
            cache_key = _run_cache_key(task, bars)
            cached = _cache_read(cache_key)
            if cached is not None:
                cached["sec"] = round(time.time() - t_start, 2)
                cached["cached"] = True
                return cached
        cost = CostModel(commission_rate=task["commission"], slippage_bps=task["slippage_bps"])
        if task.get("robot_kwargs") is not None:
            strat = RawOseSingle(task["sid"], task["robot_kwargs"])
        else:
            strat = build_strategy(task["sid"], task["params"])
        runner = EngineRunner(
            strategy=strat, exit_policy=EXITS[task["xc"]][1](),
            config=EngineConfig(figi=task["figi"], qty=task["qty"], cost_model=cost),
        )
        led = runner.run(bars)
        trades = led.trades
        nets = [t.net_pnl for t in trades]
        gw = sum(x for x in nets if x > 0)
        gl = -sum(x for x in nets if x <= 0)
        rep = full_report(trades, start_capital=task["capital"]) if trades else {}
        res = {
            "strategy": task.get("label") or task["sid"], "exit": task["xc"], "ticker": task["ticker"],
            "bars": len(bars), "trades": len(trades),
            "wins": sum(1 for x in nets if x > 0),
            "gw": round(gw, 4), "gl": round(gl, 4),
            "net": round(sum(nets), 4), "max_dd_pct": rep.get("max_drawdown_pct"),
            "commission": round(sum(t.commission for t in trades), 4),
            "slippage": round(sum(t.slippage for t in trades), 4),
            "sec": round(time.time() - t_start, 2),
        }
        if task.get("artifacts"):
            from app.engine.metrics import equity_curve as _eq
            res["data_hash"] = _data_hash(bars)
            res["turnover"] = round(sum(abs(t.entry_price * t.qty) for t in trades), 2)
            res["trades_detail"] = [
                {"side": t.side, "qty": t.qty,
                 "entry_time": t.entry_time.isoformat(), "entry_price": round(t.entry_price, 6),
                 "exit_time": t.exit_time.isoformat(), "exit_price": round(t.exit_price, 6),
                 "exit_reason": t.exit_reason, "bars_held": t.bars_held,
                 "net_pnl": round(t.net_pnl, 4)} for t in trades
            ]
            res["equity"] = _eq(trades, task["capital"])
        if cache_key:
            _cache_write(cache_key, res)
        return res
    except Exception as e:  # noqa: BLE001
        return {"strategy": task.get("label") or task["sid"], "exit": task["xc"], "ticker": task["ticker"],
                "error": f"{type(e).__name__}: {str(e)[:140]}", "sec": round(time.time() - t_start, 2)}


def _aggregate(rows: list[dict], sids: list[str], exits: list[str]) -> list[dict]:
    out = []
    for sid in sids:
        for xc in exits:
            rr = [r for r in rows if r.get("strategy") == sid and r.get("exit") == xc and "error" not in r]
            if not rr:
                errs = [r for r in rows if r.get("strategy") == sid and r.get("exit") == xc]
                out.append({"strategy": sid, "exit": xc, "trades": 0, "win_rate": 0.0,
                            "pf": None, "net": None, "expectancy": None, "max_dd_pct": None,
                            "error": errs[0]["error"] if errs else "no rows"})
                continue
            n = sum(r["trades"] for r in rr)
            wins = sum(r["wins"] for r in rr)
            gw, gl = sum(r["gw"] for r in rr), sum(r["gl"] for r in rr)
            net = sum(r["net"] for r in rr)
            out.append({
                "strategy": sid, "exit": xc, "exit_desc": EXITS[xc][0],
                "trades": n, "win_rate": round(wins / n * 100, 1) if n else 0.0,
                "pf": round(gw / gl, 2) if gl > 0 else None,
                "net": round(net, 2),
                "expectancy": round(net / n, 3) if n else None,
                "max_dd_pct": max((r["max_dd_pct"] or 0) for r in rr),
                "by_ticker": {r["ticker"]: r["net"] for r in rr},
                "sec_total": round(sum(r["sec"] for r in rr), 1),
            })
    return out


def main() -> int:
    args = parse_args()
    tf_s = TF_SECONDS[args.interval]
    sids = _strategy_ids(args.strategies)
    exits = [e.strip() for e in args.exits.split(",") if e.strip() in EXITS]
    if not sids or not exits:
        print("нет стратегий или выходов")
        return 1
    eng = _engine_sync()
    figis = _resolve_figis(eng, args)
    if not figis:
        print("нет тикеров для прогона")
        return 1
    tasks: list[dict] = []
    variants = _parse_variants(args.variants) if args.variants else []
    if variants:
        labels = [v["label"] for v in variants]
        for v in variants:
            for xc in exits:
                for f, tk in figis:
                    tasks.append({
                        "sid": v["sid"], "xc": xc, "figi": f, "ticker": tk,
                        "params": None, "robot_kwargs": v["kwargs"], "label": v["label"],
                        "dfrom": args.dfrom, "dto": args.dto, "tf_s": tf_s,
                        "qty": args.qty, "commission": args.commission,
                        "slippage_bps": args.slippage_bps, "capital": args.capital,
                    })
    else:
        labels = list(sids)
        for sid in sids:
            for xc in exits:
                for f, tk in figis:
                    tasks.append({
                        "sid": sid, "xc": xc, "figi": f, "ticker": tk,
                        "params": {"quorum": args.quorum} if sid == "ose_all" else None,
                        "robot_kwargs": None, "label": None,
                        "dfrom": args.dfrom, "dto": args.dto, "tf_s": tf_s,
                        "qty": args.qty, "commission": args.commission,
                        "slippage_bps": args.slippage_bps, "capital": args.capital,
                    })
    print(f"Период {args.dfrom}..{args.dto} · TF {args.interval} · "
          f"{len(labels)} стратегий × {len(exits)} выходов × {len(figis)} тикеров = {len(tasks)} прогонов "
          f"· jobs={args.jobs}")
    t_start = time.time()
    raw: list[dict] = []
    if args.jobs <= 1:
        for i, task in enumerate(tasks, 1):
            res = _run_combo(task)
            raw.append(res)
            print(f"  [{i}/{len(tasks)}] {res['strategy']} {res['exit']} {res['ticker']} "
                  f"net={res.get('net', '—')} {res['sec']}s {res.get('error', '')}", flush=True)
    else:
        with mp.Pool(args.jobs) as pool:
            for i, res in enumerate(pool.imap_unordered(_run_combo, tasks), 1):
                raw.append(res)
                print(f"  [{i}/{len(tasks)}] {res['strategy']} {res['exit']} {res['ticker']} "
                      f"net={res.get('net', '—')} {res['sec']}s {res.get('error', '')}", flush=True)
    dur = time.time() - t_start
    agg = _aggregate(raw, labels, exits)
    # --- пивот: net по (стратегия/вариант × выход) ---
    print(f"\n=== NET ₽ (qty={args.qty}, комиссия {args.commission:.4f}) · {dur:.0f} c wall ===")
    w = max(24, max(len(x) for x in labels) + 2)
    print("стратегия".ljust(w) + "".join(xc.rjust(11) for xc in exits))
    for label in labels:
        row = label.ljust(w)
        for xc in exits:
            v = next((a["net"] for a in agg if a["strategy"] == label and a["exit"] == xc), None)
            row += (f"{v:,.0f}".rjust(11) if v is not None else "—".rjust(11))
        print(row)
    print("\n=== ТОП по net ===")
    for a in sorted(agg, key=lambda x: -(x["net"] or -1e18))[:12]:
        print(f"  {a['strategy']:<20} {a['exit']} {a.get('exit_desc', ''):<24} "
              f"trades={a['trades']:<5} wr={a['win_rate']:<6} pf={str(a['pf']):<6} "
              f"net={a['net']:>10,.2f} dd={a['max_dd_pct']}")
    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    out = out_dir / f"ose_matrix_{stamp}.json"
    out.write_text(json.dumps({
        "period": [args.dfrom, args.dto], "interval": args.interval,
        "tickers": [t for _, t in figis], "qty": args.qty,
        "commission": args.commission, "slippage_bps": args.slippage_bps,
        "quorum": args.quorum, "jobs": args.jobs, "duration_sec": round(dur, 1),
        "results": agg, "raw": raw,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nJSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
