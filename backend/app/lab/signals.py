"""M1: генерация сырых сигналов Signal Lab (без гейтов, все kind).

Один проход на (тикер, ТФ, движок): сигналы по закрытым барам, вход в анализ —
next-open (используется на M2+). Здесь только фиксация «что сказал движок».
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from app.lab.data import LabBar, build_tf, engine_sync, iso, load_1m, to_engine_candles
from app.services.signals import generate_signals


def params_hash(params: dict | None) -> str:
    raw = json.dumps(params or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def signal_uid(dataset_key: str, figi: str, strategy_id: str, phash: str,
               tf: str, bar_ts: str, side: str, kind: str) -> str:
    raw = f"{dataset_key}|{figi}|{strategy_id}|{phash}|{tf}|{bar_ts}|{side}|{kind}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def signals_for_stream(strategy_id: str, params: dict, tf: str, tf_seconds: int,
                       bars: list[LabBar], dataset_key: str, window: int | None,
                       period_from: datetime, period_to: datetime,
                       strategy_version: str = "") -> list[dict]:
    """Сырые сигналы одного (движок, ТФ) на готовых барах ТФ."""
    candles = to_engine_candles(bars)
    sigs = generate_signals(strategy_id, params, candles, window=window)
    phash = params_hash(params)
    out: list[dict] = []
    for s in sigs:
        ts = s.get("ts")
        if not isinstance(ts, datetime):
            continue
        tsu = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        if tsu < period_from or tsu > period_to:
            continue  # прогрев не пишем: сигналы только за отчётный период
        side = str(s.get("side") or "")
        kind = str(s.get("kind") or "entry")
        bar_ts = iso(tsu)
        out.append({
            "figi": "",  # заполняет runner (знает figi)
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "params_hash": phash,
            "tf": tf,
            "tf_seconds": tf_seconds,
            "bar_ts": bar_ts,
            "bar_close_ts": iso(tsu.replace(tzinfo=tsu.tzinfo) + _tf_delta(tf_seconds)),
            "side": side,
            "kind": kind,
            "reason": str(s.get("reason") or "")[:256],
            "features": s.get("features") or {},
            "signal_uid": signal_uid(dataset_key, "", strategy_id, phash, tf, bar_ts, side, kind),
        })
    return out


def _tf_delta(tf_seconds: int):
    from datetime import timedelta
    return timedelta(seconds=tf_seconds)


def signals_task(args: dict) -> dict:
    """Worker mp.Pool: один тикер → 1m один раз → все ТФ → все движки.

    Возвращает {'ticker', 'figi', 'rows': [...], 'counts': {tf: n}, 'error': str|None}.
    """
    figi = args["figi"]
    ticker = args["ticker"]
    result: dict = {"ticker": ticker, "figi": figi, "rows": [], "counts": {}, "error": None}
    try:
        eng = engine_sync(args["db_url"])
        bars_1m = load_1m(eng, figi, args["warmup_from"], args["period_to"])
        if not bars_1m:
            result["error"] = "no_1m_bars"
            return result
        period_from = _parse(args["period_from"])
        period_to = _parse(args["period_to"])
        for tf in args["tfs"]:
            tsec = int(args["tf_seconds"][tf])
            bars = build_tf(bars_1m, tf)
            n_tf = 0
            for strategy_id in args["engines"]:
                params = (args["engine_params"] or {}).get(strategy_id) or {}
                rows = signals_for_stream(
                    strategy_id, params, tf, tsec, bars, args["dataset_key"],
                    args["window"], period_from, period_to)
                for r in rows:
                    r["figi"] = figi
                    r["ticker"] = ticker
                    r["signal_uid"] = signal_uid(
                        args["dataset_key"], figi, strategy_id, r["params_hash"],
                        tf, r["bar_ts"], r["side"], r["kind"])
                result["rows"].extend(rows)
                n_tf += len(rows)
            result["counts"][tf] = n_tf
    except Exception as e:  # воркер не должен ронять пул
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return result


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Кэш баров на процесс-воркер: (figi, warmup, to, tfs) → {tf: bars}.
# Задачи сортируются по тикеру, воркер обрабатывает соседние — попадания частые.
_BARS_CACHE: dict = {}
_BARS_CACHE_MAX = 3
# Кэш объявленного прогрева движка (warmup_bars) на процесс-воркер.
_WARMUP_CACHE: dict = {}


def _get_tf_bars(args: dict, figi: str) -> dict:
    key = (args["db_url"], figi, args["warmup_from"], args["period_to"], tuple(args["tfs"]))
    hit = _BARS_CACHE.get(key)
    if hit is not None:
        return hit
    eng = engine_sync(args["db_url"])
    bars_1m = load_1m(eng, figi, args["warmup_from"], args["period_to"])
    tfbars = {tf: build_tf(bars_1m, tf) for tf in args["tfs"]}
    if len(_BARS_CACHE) >= _BARS_CACHE_MAX:
        _BARS_CACHE.pop(next(iter(_BARS_CACHE)))
    _BARS_CACHE[key] = tfbars
    return tfbars


def _engine_warmup(strategy_id: str, params: dict) -> int:
    hit = _WARMUP_CACHE.get(strategy_id)
    if hit is None:
        from app.engine.strategies import build_strategy
        try:
            hit = int(build_strategy(strategy_id, params).warmup_bars() or 0)
        except Exception:
            hit = 400
        _WARMUP_CACHE[strategy_id] = hit
    return hit


def _slice_for_engine(bars: list, period_from: datetime, keep: int) -> list:
    """Оставить весь период + keep баров прогрева (движку больше не видно).

    generate_signals кормит стратегию от начала списка; всё, что старше
    keep относительно начала периода, стратегия не увидит (окно ≤ 400,
    объявленный warmup_bars — минимально достаточный прогрев).
    """
    if not bars:
        return bars
    from bisect import bisect_left
    try:
        idx = bisect_left(bars, period_from, key=lambda b: b.ts)
    except TypeError:  # старый питон без key
        idx = next((i for i, b in enumerate(bars) if b.ts >= period_from), len(bars))
    lo = max(0, idx - max(int(keep), 400))
    return bars[lo:]


def engine_task(args: dict) -> dict:
    """Worker mp.Pool: один (тикер, движок) → все ТФ → строки сигналов.

    Печатает время по движку (видно, кто тяжёлый) и возвращает строки.
    """
    import time as _t
    figi = args["figi"]
    ticker = args["ticker"]
    strategy_id = args["strategy_id"]
    result: dict = {"ticker": ticker, "figi": figi, "strategy_id": strategy_id,
                    "rows": [], "counts": {}, "sec": 0.0, "error": None}
    t0 = _t.perf_counter()
    try:
        tfbars = _get_tf_bars(args, figi)
        if not tfbars.get("1min"):
            result["error"] = "no_1m_bars"
            return result
        period_from = _parse(args["period_from"])
        period_to = _parse(args["period_to"])
        params = (args["engine_params"] or {}).get(strategy_id) or {}
        keep = 2 * _engine_warmup(strategy_id, params)
        for tf in args["tfs"]:
            tsec = int(args["tf_seconds"][tf])
            bars_tf = _slice_for_engine(tfbars.get(tf) or [], period_from, keep)
            rows = signals_for_stream(
                strategy_id, params, tf, tsec, bars_tf, args["dataset_key"],
                args["window"], period_from, period_to)
            for r in rows:
                r["figi"] = figi
                r["ticker"] = ticker
                r["signal_uid"] = signal_uid(
                    args["dataset_key"], figi, strategy_id, r["params_hash"],
                    tf, r["bar_ts"], r["side"], r["kind"])
            result["rows"].extend(rows)
            result["counts"][tf] = len(rows)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    result["sec"] = round(_t.perf_counter() - t0, 2)
    print(f"[{ticker} {strategy_id}] signals={len(result['rows'])} "
          f"{result['counts']} t={result['sec']}s {result['error'] or ''}", flush=True)
    return result
