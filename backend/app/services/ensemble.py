"""Ансамбль ролей: bias → setups+quorum → entry → exits → oracle.
MCP-контракт: запрос детерминирован (request_hash), setup-сигналы кэшируются,
выходы считаются каноническим движком. Никакого look-ahead.

Режимы: rule-based regime detector (app.services.regime) + адаптивные
конфигурации {режим → mode/exit/запрет торговли}. Сравнение static vs adaptive.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Sequence

import logging
from app.engine.costs import CostModel

logger = logging.getLogger(__name__)
from app.engine.exits import ExitPolicy, intrabar_exit

def _validate_candles(candles: list) -> tuple:
    """Validate candles for anomalies and broken data.

    Уровень 1 — битые ДНИ (мерцание цены): если в пределах торгового дня (МСК)
    цена между соседними барами прыгает >50% не менее FLICKER_JUMPS раз, весь
    день считается битым (сбой источника, напр. 2026-08-22: AFLT 32->85,
    MVID 46->4222, NLMK 70->317 мерцают весь день). Такие бары отбрасываются.

    Уровень 2 — битые бары: zero/negative, high<low, high<open/close,
    low>open/close, volume<0, и единичный ценовой прыжок >40% от prev close.

    Returns:
        (valid_candles, skipped_count)
    """
    from zoneinfo import ZoneInfo as _ZI
    _msk = _ZI("Europe/Moscow")

    FLICKER_JUMPS = 3
    FLICKER_THR = 0.50
    JUMP_THR = 0.40

    # --- Уровень 1: определить битые дни (мерцание) ---
    bad_days: set[str] = set()
    day_jumps: dict[str, int] = {}
    day_prev: dict[str, float] = {}
    for c in candles:
        d = c.ts.astimezone(_msk).date().isoformat()
        pv = day_prev.get(d)
        if pv is not None and pv > 0:
            j = abs(c.close - pv) / pv
            if j > FLICKER_THR:
                day_jumps[d] = day_jumps.get(d, 0) + 1
        day_prev[d] = c.close
    for d, n in day_jumps.items():
        if n >= FLICKER_JUMPS:
            bad_days.add(d)

    # --- Уровень 2: фильтрация баров ---
    valid = []
    skipped = 0
    prev_valid_close: float | None = None
    for c in candles:
        d = c.ts.astimezone(_msk).date().isoformat()
        if d in bad_days:
            skipped += 1
            continue
        # Check for zero/negative prices
        if c.open <= 0 or c.close <= 0 or c.high <= 0 or c.low <= 0:
            skipped += 1
            continue
        # Check high >= low
        if c.high < c.low:
            skipped += 1
            continue
        # Check high >= open and high >= close
        if c.high < c.open or c.high < c.close:
            skipped += 1
            continue
        # Check low <= open and low <= close
        if c.low > c.open or c.low > c.close:
            skipped += 1
            continue
        # Check volume >= 0
        if c.volume < 0:
            skipped += 1
            continue
        # Unit price jump vs previous valid close (gap between sessions allowed up to 40%)
        if prev_valid_close is not None and prev_valid_close > 0:
            if abs(c.close - prev_valid_close) / prev_valid_close > JUMP_THR:
                skipped += 1
                continue
        prev_valid_close = c.close
        valid.append(c)
    return valid, skipped

from app.engine.models import Candle as EngineCandle, PositionState, Side
from app.engine.policies import SignalPolicyConfig
from app.engine.quorum import merge_quorum
from app.engine.runner import EngineConfig, EngineRunner
from app.engine.sessions import SessionPolicyConfig
from app.engine.wave1 import ReplayStrategy
from app.services.ceiling import zigzag_swings
from app.services.experiments import build_exit_policy
from app.services.indicators import macd
from app.services.ml_ensemble_filter import MlEnsembleFilter, resample_to_5m
from app.services.regime import RegimeDetector, regime_at
from app.services.signals import generate_signals
from app.services.volume import volume_at, volume_features

ENGINE_VERSION = "ensemble_v2"
ALL_STRATEGY_IDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
                    "range_compression_breakout", "macd_cross", "donchian_breakout"]
# Volume-стратегии (VOLUME_EXHAUSTION_2026.md Шаг 3) — это continuation/context evidence,
# а не reversal-сигналы. `drop_useless` оценивает совпадение с oracle-разворотами и
# ошибочно удаляет их, поэтому они исключены из отсева.
VOLUME_STRATEGY_IDS = {"volume_drop", "volume_climax", "volume_divergence"}
TF_SECONDS = {"1min": 60, "5min": 300, "15min": 900, "hour": 3600}


def request_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def entry_pullback_deep_pass(c5: list[EngineCandle], atr5: list[float | None],
                              ts: datetime, side: str,
                              lookback: int = 20, thr_mult: float = 0.5) -> tuple[bool, str]:
    """B4-RUN: требовать глубокий pullback перед breakout на decision_ts.

    pullback_bps = откат цены входа (close 5m-бара, содержащего ts) от недавнего
    swing (max high / min low за lookback 5m-баров) в bps. Deep если
    pullback_bps >= thr_mult * ATR5m(decision) в bps. Без look-ahead: только
    прошлые бары. side: 'BUY' / 'SELL' (нотация micro_breakout).
    """
    c5ts = [c.ts for c in c5]
    j = bisect.bisect_right(c5ts, ts) - 1
    if j < lookback:
        return False, "PULLBACK_WARMUP"
    px = c5[j].close
    if px <= 0:
        return False, "PULLBACK_NA"
    lo = max(0, j - lookback)
    if side == "BUY":
        swing = max(c.high for c in c5[lo:j + 1])
        if swing <= 0:
            return False, "PULLBACK_NA"
        pb = (swing - px) / swing * 10000.0
    elif side == "SELL":
        swing = min(c.low for c in c5[lo:j + 1])
        if swing <= 0:
            return False, "PULLBACK_NA"
        pb = (px - swing) / swing * 10000.0
    else:
        return False, "PULLBACK_NA"
    atr = atr5[j] if j < len(atr5) else None
    if atr is None or atr <= 0:
        return False, "PULLBACK_NA"
    thr = thr_mult * atr / px * 10000.0
    if pb < thr:
        return False, "PULLBACK_SHALLOW"
    return True, ""


def resample(candles: list[EngineCandle], tf_seconds: int) -> list[EngineCandle]:
    out: list[EngineCandle] = []
    for c in candles:
        epoch = int(c.ts.timestamp())
        bucket = epoch - epoch % tf_seconds
        key = datetime.fromtimestamp(bucket, tz=timezone.utc)
        if out and out[-1].ts == key:
            prev = out[-1]
            out[-1] = EngineCandle(ts=key, open=prev.open, high=max(prev.high, c.high),
                                   low=min(prev.low, c.low), close=c.close,
                                   volume=prev.volume + c.volume)
        else:
            out.append(EngineCandle(ts=key, open=c.open, high=c.high, low=c.low,
                                    close=c.close, volume=c.volume))
    return out


_resample_cache: dict[int, list[EngineCandle]] = {}


def cached_resample(candles: list[EngineCandle], tf_seconds: int) -> list[EngineCandle]:
    """Resample with per-call cache. Within one compute_ensemble call,
    same tf_seconds returns cached result (avoids redundant O(N) passes)."""
    key = id(candles), tf_seconds
    if key not in _resample_cache:
        _resample_cache[key] = resample(candles, tf_seconds)
    return _resample_cache[key]


def clear_resample_cache():
    _resample_cache.clear()


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def compute_bias(bars: list[EngineCandle], period: int, tf_seconds: int = 3600) -> dict[int, int]:
    """Направление тренда по EMA(period) на барах tf ЗАКРЫТИЕМ ПРЕДЫДУЩЕГО бара."""
    closes = [c.close for c in bars]
    ema = _ema(closes, period)
    bias: dict[int, int] = {}
    for i in range(1, len(bars)):
        bucket = int(bars[i].ts.timestamp()) // tf_seconds
        bias[bucket] = 1 if closes[i - 1] >= ema[i - 1] else -1
    return bias


def micro_breakout(candles: list[EngineCandle], lookback: int) -> list[dict]:
    signals: list[dict] = []
    for i in range(lookback + 1, len(candles)):
        window = candles[i - lookback : i]
        highest = max(b.high for b in window)
        lowest = min(b.low for b in window)
        last = candles[i]
        if last.close > highest:
            signals.append({"ts": last.ts, "side": "BUY", "reason": "micro_breakout_up",
                            "features": {"breakout_level": highest}})
        elif last.close < lowest:
            signals.append({"ts": last.ts, "side": "SELL", "reason": "micro_breakout_down",
                            "features": {"breakout_level": lowest}})
    return signals


class RegimeExitPolicy(ExitPolicy):
    """Exit-политика, выбирающая подполитику по режиму в момент входа."""

    def __init__(self, default: ExitPolicy, per_regime: dict[str, ExitPolicy],
                 regime_bars: list[dict]):
        self.default = default
        self.per_regime = per_regime
        self.regime_bars = regime_bars
        self.policy_id = "regime_switch"
        self.version = "1.0.0"

    def plan_entry(self, side: Side, entry_price: float, bars: Sequence[EngineCandle]) -> object:
        regime = regime_at(self.regime_bars, bars[-1].ts) if bars else None
        policy = self.per_regime.get(regime["state"]) if regime else None
        return (policy or self.default).plan_entry(side, entry_price, bars)

    def update_stop(self, side, entry_price, current_stop, bars, qty=None, commission=None):
        """Трейлинг делегируется в подполитику режима текущего бара (как plan_entry)."""
        regime = regime_at(self.regime_bars, bars[-1].ts) if bars else None
        policy = self.per_regime.get(regime["state"]) if regime else None
        target = policy or self.default
        upd = getattr(target, "update_stop", None)
        if upd is None:
            return current_stop
        try:
            return upd(side, entry_price, current_stop, bars, qty=qty, commission=commission)
        except (TypeError, ValueError):
            return upd(side, entry_price, current_stop, bars)


def _oracle_fixed_qty(candles: list[EngineCandle], threshold_pct: float, fee_rate: float,
                      qty_shares: float) -> dict:
    cdicts = [c.__dict__ for c in candles]
    swings = zigzag_swings(cdicts, threshold_pct / 100.0)
    trades: list[dict] = []
    for k in range(len(swings) - 1):
        sw, nxt = swings[k], swings[k + 1]
        # low->high = лонг (купил в минимуме, продал в максимуме)
        # high->low = шорт (продал в максимуме, откупил в минимуме)
        if sw["kind"] == "low":
            side = "LONG"
            entry_px, exit_px = sw["price"], nxt["price"]
        else:
            side = "SHORT"
            entry_px, exit_px = sw["price"], nxt["price"]
        if exit_px == entry_px:
            continue
        gross = (exit_px - entry_px) * qty_shares if side == "LONG" else (entry_px - exit_px) * qty_shares
        commission = (entry_px + exit_px) * qty_shares * fee_rate
        trades.append({
            "side": side,
            "entry_ts": cdicts[sw["idx"]]["ts"].isoformat(),
            "exit_ts": cdicts[nxt["idx"]]["ts"].isoformat(),
            "entry_px": round(entry_px, 6), "exit_px": round(exit_px, 6),
            "gross": round(gross, 2), "commission": round(commission, 2),
            "net": round(gross - commission, 2),
        })
    n = len(trades)
    longs = [t for t in trades if t["side"] == "LONG"]
    shorts = [t for t in trades if t["side"] == "SHORT"]
    return {"trades": trades, "gross": round(sum(t["gross"] for t in trades), 2),
            "net": round(sum(t["net"] for t in trades), 2),
            "longs": len(longs), "shorts": len(shorts),
            "long_gross": round(sum(t["gross"] for t in longs), 2),
            "short_gross": round(sum(t["gross"] for t in shorts), 2)}


def _signal_quality(role: str, strategy_id: str, tf: str, signals: list[dict],
                    oracle_points: dict[str, list[datetime]], window_min: int) -> list[dict]:
    import bisect as _bisect
    window_sec = window_min * 60
    out = []
    for side in ("BUY", "SELL"):
        pts = oracle_points.get(side) or []
        pts_sorted = sorted(pts)
        np_ = len(pts_sorted)
        total = 0
        hits = 0
        lead = []
        covered = set()
        for s in signals:
            if s["side"] != side:
                continue
            total += 1
            if np_ == 0:
                continue
            ts = s["ts"]
            # Ближайшая oracle-точка через бинарный поиск (O(log n) вместо O(n)).
            i = _bisect.bisect_left(pts_sorted, ts)
            best = pts_sorted[i] if i < np_ else None
            if i > 0:
                p = pts_sorted[i - 1]
                if best is None or (ts - p) <= (best - ts):
                    best = p
            if best is None:
                continue
            delta = (ts - best).total_seconds()
            if abs(delta) <= window_sec:
                hits += 1
                lead.append(delta / 60)
                covered.add(best)
        false_pos = total - hits
        cov = len(covered) / max(np_, 1) * 100
        prec = hits / max(total, 1) * 100
        lead_med = sorted(lead)[len(lead) // 2] if lead else None
        out.append({
            "role": role, "strategy_id": strategy_id, "tf": tf, "side": side,
            "signals": total, "oracle_points": np_,
            "hits": hits, "coverage_pct": round(cov, 1), "precision_pct": round(prec, 1),
            "lead_min_median": round(lead_med, 1) if lead_med is not None else None,
            "false_positives": false_pos,
            "useless": total == 0 or cov == 0,
        })
    return out


def _mfe_mae(candles: list[EngineCandle], entry_idx: int, exit_idx: int,
             entry_px: float, side: str, r: float) -> dict:
    window = candles[entry_idx : exit_idx + 1]
    if not window:
        return {"mfe_r": 0.0, "mae_r": 0.0}
    if side == "LONG":
        mfe = max(b.high for b in window) - entry_px
        mae = entry_px - min(b.low for b in window)
    else:
        mfe = entry_px - min(b.low for b in window)
        mae = max(b.high for b in window) - entry_px
    r = r or entry_px * 0.01
    return {"mfe_r": round(mfe / r, 2), "mae_r": round(mae / r, 2)}


def _counterfactual_reentries(candles: list[EngineCandle], rejected: list[dict],
                              exit_obj: ExitPolicy, qty_shares: float,
                              fee_rate: float = 0.0005, slip_bps: float = 0.0002) -> dict:
    """Counterfactual отклонённых same-side re-entry: вход на следующем баре,
    выход той же exit-политикой. Показывает, отбрасывал ли cooldown плохие входы."""
    idx_by_ts = {c.ts.isoformat(): i for i, c in enumerate(candles)}
    results: list[dict] = []
    for r in rejected:
        i = idx_by_ts.get(r["signal_ts"])
        if i is None or i + 1 >= len(candles):
            continue
        side = Side.BUY if r["side"] == "BUY" else Side.SELL
        entry_bar = candles[i + 1]
        fill = entry_bar.open
        entry_notional = fill * qty_shares
        ec, es = entry_notional * fee_rate, entry_notional * slip_bps
        plan = exit_obj.plan_entry(side, fill, candles[: i + 2])
        state = PositionState.LONG if side is Side.BUY else PositionState.SHORT
        exit_price, exit_reason, exit_idx = None, None, None
        for j in range(i + 2, len(candles)):
            px, reason = intrabar_exit(candles[j], state, plan.stop_loss, plan.take_profit)
            if px is not None:
                exit_price, exit_reason, exit_idx = px, reason, j
                break
        if exit_price is None:
            exit_price, exit_reason, exit_idx = candles[-1].close, "end_of_data", len(candles) - 1
        exit_notional = exit_price * qty_shares
        xc, xs = exit_notional * fee_rate, exit_notional * slip_bps
        gross = (exit_price - fill) * qty_shares if side is Side.BUY else (fill - exit_price) * qty_shares
        results.append({
            "side": r["side"], "signal_ts": r["signal_ts"],
            "bars_since_exit": r.get("bars_since_exit"),
            "entry_ts": candles[i + 1].ts.isoformat(),
            "exit_ts": candles[exit_idx].ts.isoformat(),
            "entry_px": round(fill, 6), "exit_px": round(exit_price, 6),
            "exit_reason": exit_reason,
            "gross": round(gross, 2), "costs": round(ec + es + xc + xs, 2),
            "net": round(gross - ec - xc, 2),
        })
    if not results:
        return {"n": 0, "mean_net": None, "median_net": None, "wins_pct": None, "list": []}
    nets = sorted(r["net"] for r in results)
    return {
        "n": len(results),
        "mean_net": round(sum(nets) / len(nets), 2),
        "median_net": nets[len(nets) // 2],
        "wins_pct": round(sum(1 for r in results if r["net"] > 0) / len(results) * 100, 1),
        "list": results,
    }


def _counterfactual_hold(candles: list[EngineCandle], trades: list,
                         exit_obj: ExitPolicy, qty_shares: float,
                         fee_rate: float = 0.0005, slip_bps: float = 0.0002) -> dict:
    """Что было бы, если не выходить по противоположному сигналу, а держать
    позицию до срабатывания stop/target/trailing/session/конца данных."""
    rows: list[dict] = []
    for t in trades:
        if t["exit_reason"] != "signal_exit":
            continue
        side = Side.BUY if t["side"] == "LONG" else Side.SELL
        entry_i = t["entry_index"]
        fill = t["entry_px"]
        stop = t.get("initial_stop") or t.get("stop")
        target = t.get("target") or t.get("take_profit")
        update_stop = getattr(exit_obj, "update_stop", None)
        state = PositionState.LONG if side is Side.BUY else PositionState.SHORT
        exit_px, exit_reason, exit_i = None, None, None
        for j in range(min(entry_i + 1, len(candles)), len(candles)):
            if update_stop is not None:
                stop = update_stop(state, fill, stop, candles[entry_i : j + 1])
            px, reason = intrabar_exit(candles[j], state, stop, target)
            if px is not None:
                exit_px, exit_reason, exit_i = px, reason, j
                break
        if exit_px is None:
            exit_px, exit_reason, exit_i = candles[-1].close, "end_of_data", len(candles) - 1
        hold_net = (exit_px - fill) * qty_shares if side is Side.BUY else (fill - exit_px) * qty_shares
        entry_notional = fill * qty_shares
        exit_notional = exit_px * qty_shares
        hold_net -= entry_notional * fee_rate + exit_notional * fee_rate
        actual_net = t["net"]
        rows.append({
            "entry_ts": candles[entry_i].ts.isoformat(),
            "side": t["side"],
            "actual_exit_ts": t["exit_ts"],
            "actual_net": actual_net,
            "hold_exit_ts": candles[exit_i].ts.isoformat(),
            "hold_exit_reason": exit_reason,
            "hold_net": round(hold_net, 2),
            "delta": round(hold_net - actual_net, 2),
        })
    if not rows:
        return {"n": 0, "mean_delta": None, "actual_mean": None, "hold_mean": None,
                "hold_better": None, "list": []}
    deltas = sorted(r["delta"] for r in rows)
    return {
        "n": len(rows),
        "mean_delta": round(sum(r["delta"] for r in rows) / len(rows), 2),
        "median_delta": deltas[len(deltas) // 2],
        "actual_mean": round(sum(r["actual_net"] for r in rows) / len(rows), 2),
        "hold_mean": round(sum(r["hold_net"] for r in rows) / len(rows), 2),
        "hold_better": sum(1 for r in rows if r["delta"] > 0),
        "list": rows,
    }


def _oracle_coverage(oracle_swings_detail: list[dict], entries_raw: list[dict],
                     accepted: list[dict], rejected: list[dict],
                     window_min: int = 10,
                     executed_signals: set[tuple[str, str]] | None = None) -> dict:
    """Почему не заходим в сделки оракула, без look-ahead.

    oracle_swings_detail — [{side, point_ts, confirmation_ts, point_idx, conf_idx}].
    Две воронки:
      geometric — сигнал того же направления в окне ДО фактической точки экстремума;
      causal    — сигнал доступен к моменту ПОДТВЕРЖДЕНИЯ экстремума (confirmation_ts).
    Причины отсева считаются по каскаду гейтов (сигнал мог не пройти несколько),
    поэтому для каждого входа храним failed_gates + primary_reason.
    executed_signals — пары (signal_ts_iso, side), реально дошедшие до сделки движка.
    """
    exec_set = executed_signals or set()
    raw_acc = {(a["ts"], a["side"]): a for a in accepted}
    raw_rej = {(r["ts"], r["side"]): r["reason"] for r in rejected}
    window_sec = window_min * 60
    _key = lambda s: (s["ts"].isoformat(), s["side"]) if hasattr(s["ts"], "isoformat") else (s["ts"], s["side"])

    ge = {"tot": 0, "raw_before_point": 0, "accepted": 0, "executed": 0}
    ca = {"tot": 0, "raw_before_conf": 0, "accepted": 0, "executed": 0}
    rejected_by_gate = {"not_seen": 0, "quorum": 0, "bias": 0, "regime": 0,
                        "cooldown": 0, "price": 0, "ml": 0, "other": 0}
    points_detail: list[dict] = []

    def _gate_failure(reason: str) -> str:
        if reason.startswith("BIAS"):
            return "bias"
        if reason.startswith("QUORUM"):
            return "quorum"
        if reason.startswith("REGIME"):
            return "regime"
        if reason.startswith("ML_"):
            return "ml"
        if "REENTRY" in reason:
            return "cooldown"
        if reason == "REJECT_SHORT" or reason.startswith("SKIP_ENTRY") or reason.startswith("REJECT_SESSION"):
            return "price"
        return "other"

    # Предгруппировка entries_raw по направлению (O(log n) поиск окна вместо O(n) на swing).
    import bisect as _bisect
    from datetime import timedelta as _td
    _entries_by_side: dict[str, list[dict]] = {"BUY": [], "SELL": []}
    for _s in entries_raw:
        _entries_by_side.setdefault(_s["side"], []).append(_s)
    _side_ts: dict[str, list] = {}
    for _k, _v in _entries_by_side.items():
        _v.sort(key=lambda s: s["ts"])
        _side_ts[_k] = [s["ts"] for s in _v]

    def _window(side: str, t: datetime) -> list[dict]:
        lst = _entries_by_side.get(side) or []
        ts_list = _side_ts.get(side) or []
        if not lst:
            return []
        lo = _bisect.bisect_left(ts_list, t - _td(seconds=window_sec))
        hi = _bisect.bisect_right(ts_list, t)
        return lst[lo:hi]

    for sw in oracle_swings_detail:
        side = sw["side"]
        pt = datetime.fromisoformat(sw["point_ts"])
        conf_ts = datetime.fromisoformat(sw["confirmation_ts"])

        # все raw того же направления в окне вокруг точки (кроме сигналов позже окна)
        raw_window = _window(side, pt)
        raw_causal = _window(side, conf_ts)

        ge["tot"] += 1
        ca["tot"] += 1
        best_geo = max(raw_window, key=lambda s: s["ts"]) if raw_window else None
        best_cau = max(raw_causal, key=lambda s: s["ts"]) if raw_causal else None

        def _fate(best: dict | None, totals: dict, label: str, count_reject: bool):
            if best is None:
                if count_reject:
                    rejected_by_gate["not_seen"] += 1
                return "not_seen", []
            totals["raw_before_point" if label == "geo" else "raw_before_conf"] += 1
            key = _key(best)
            if key in raw_acc:
                totals["accepted"] += 1
                if key in exec_set:
                    totals["executed"] += 1
                return "accepted", []
            why = raw_rej.get(key, "unknown")
            gates = [_gate_failure(why)] if why != "unknown" else ["other"]
            if count_reject:
                rejected_by_gate[gates[0] if gates else "other"] += 1
            return f"REJECTED:{why}", gates

        geo_fate, geo_gates = _fate(best_geo, ge, "geo", count_reject=False)
        cau_fate, cau_gates = _fate(best_cau, ca, "cau", count_reject=False)

        # rejected_by_gate — только по каузальной ветке, со всеми причинами, не одна
        if best_cau is None:
            rejected_by_gate["not_seen"] += 1
        else:
            key = _key(best_cau)
            if key not in raw_acc:
                why = raw_rej.get(key, "unknown")
                gate = _gate_failure(why) if why != "unknown" else "other"
                rejected_by_gate[gate] += 1

        points_detail.append({
            "side": side,
            "oracle_point_ts": sw["point_ts"],
            "oracle_confirmation_ts": sw["confirmation_ts"],
            "raw_same_side_before_point": best_geo is not None,
            "raw_same_side_before_confirmation": best_cau is not None,
            "nearest_before_point_ts": best_geo["ts"].isoformat() if best_geo else None,
            "nearest_before_conf_ts": best_cau["ts"].isoformat() if best_cau else None,
            "causal": cau_fate == "accepted",
            "failed_gates": sorted(set(cau_gates)),
            "primary_reason": cau_fate.split(":")[-1] if cau_fate.startswith("REJECTED") else cau_fate,
        })

    n_ge = max(ge["tot"], 1)
    n_ca = max(ca["tot"], 1)
    lags = sorted(sw["conf_idx"] - sw["point_idx"] for sw in oracle_swings_detail)
    lag_stats = {"mean": round(sum(lags) / len(lags), 1), "median": lags[len(lags) // 2],
                 "max": lags[-1]} if lags else {"mean": None, "median": None, "max": None}
    return {
        "window_minutes": window_min,
        "point_total": ge["tot"],
        "confirmation_lag_bars": lag_stats,
        "geometric": {
            "raw_seen_before_point": ge["raw_before_point"],
            "accepted": ge["accepted"],
            "executed": ge["executed"],
            "coverage_accepted_pct": round(ge["accepted"] / n_ge * 100, 1),
        },
        "causal": {
            "raw_seen_before_confirmation": ca["raw_before_conf"],
            "accepted": ca["accepted"],
            "executed": ca["executed"],
            "coverage_accepted_pct": round(ca["accepted"] / n_ca * 100, 1),
        },
        "rejected_by_gate": rejected_by_gate,
        "points": points_detail,
    }


def _build_session_policy(req: dict) -> SessionPolicyConfig | None:
    """Сессионная политика: фильтрует ТОЛЬКО новые входы (через can_enter движка),
    уже открытая позиция управляется обычной exit-логикой (stop/target/сигнал).

    entry_session: "all" — входы в любое время торгов; "main" — только основная сессия.
    carry_overnight: переносить открытую позицию через ночь (не закрывать в конце дня).
    force_flat_at_session_end: принудительно закрывать после окончания основной сессии.
    """
    entry_session = req.get("entry_session", "all")
    carry = bool(req.get("carry_overnight", True))
    force_flat = bool(req.get("force_flat_at_session_end", False))
    if entry_session == "all":
        return None
    cfg = {"overnight": carry, "force_flat_at_session_end": force_flat}
    if req.get("entry_session_extended"):
        cfg["open_time"] = "09:30"
        cfg["close_time"] = "19:15"
    return SessionPolicyConfig(**cfg)


def _signal_score(quorum_ev: dict, e: dict, side: str, ts: datetime,
                  quorum_pool: list[dict], quorum_full: int,
                  regime_bars: list[dict] | None, vol_series: list[dict] | None,
                  macd_ok_fn, weights: dict | None = None) -> dict:
    """Score уверенности входа (Series 3, evidence — не gate).

    Собирается на момент входа из доступных фич без look-ahead:
    quorum (голоса/все) + за-bias + regime TREND-совпадение + volume-подтверждение
    + MACD 1m. Нормализуется в [-1, 1]. Возвращает score и компоненты.
    """
    w = weights or {}
    s = 0.0
    qf = quorum_ev.get("features", {}) if quorum_ev else {}
    mv = float(qf.get("votes", 1) or 1)
    mt = float(qf.get("total_members", 0) or 0) or float(len(quorum_pool) or 0) or float(quorum_full or 0)
    if not mt:
        mt = 1.0
    s += min(1.0, mv / mt) * w.get("quorum", 1.0)
    s += (0.0 if e.get("against_bias") else 1.0) * w.get("bias", 0.5)
    st = regime_at(regime_bars, ts) if regime_bars else None
    regime_name = st["state"] if st else "NEUTRAL"
    if regime_name in ("TREND_UP", "TREND_DOWN"):
        trend_ok = (regime_name == "TREND_UP" and side == "BUY") or \
                   (regime_name == "TREND_DOWN" and side == "SELL")
        s += (1.0 if trend_ok else -0.5) * w.get("regime", 0.5)
    vf = volume_at(vol_series, ts) if vol_series else None
    vol_drop = bool(vf and vf.get("volume_on_drop"))
    if vf:
        if side == "SELL" and vf.get("volume_on_drop"):
            s += 1.0 * w.get("volume", 0.5)
        elif side == "BUY" and vf.get("climax_short"):
            s += 1.0 * w.get("volume", 0.5)
    macd_against = bool(macd_ok_fn is not None and macd_ok_fn(ts, side) is False)
    if macd_against:
        s += -1.0 * w.get("macd", 0.5)
    w_tot = max(w.get("quorum", 1.0) + w.get("bias", 0.5) + w.get("regime", 0.5) +
                w.get("volume", 0.5) + w.get("macd", 0.5), 1e-9)
    return {
        "score": round(s / w_tot, 4),
        "components": {
            "votes": mv, "total": mt, "against_bias": bool(e.get("against_bias")),
            "regime": regime_name, "vol_drop": vol_drop, "macd_against": macd_against,
        },
    }


def _votes_last(setup_runs: list[tuple[str, list[dict]]]) -> dict:
    """Голоса стратегий на последнем 5m-баре (включая 1–2 голоса, до кворума)."""
    _vts = None
    for _, sigs in setup_runs:
        for s in sigs:
            if _vts is None or s["ts"] > _vts:
                _vts = s["ts"]
    if _vts is None:
        return {"ts": None, "buy": 0, "sell": 0, "buy_members": [], "sell_members": []}
    buy = [sid for sid, sigs in setup_runs
           if any(s["ts"] == _vts and s["side"] == "BUY" for s in sigs)]
    sell = [sid for sid, sigs in setup_runs
            if any(s["ts"] == _vts and s["side"] == "SELL" for s in sigs)]
    return {"ts": _vts.isoformat(), "buy": len(buy), "sell": len(sell),
            "buy_members": buy, "sell_members": sell}


def _stoch_map(bars, k_period: int, d_period: int) -> dict:
    """Stochastic %K/%D по барам → {ts: (k, d)}."""
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    ks: list = [None] * len(bars)
    for i in range(len(bars)):
        if i + 1 < k_period:
            continue
        hh = max(highs[i - k_period + 1:i + 1])
        ll = min(lows[i - k_period + 1:i + 1])
        rng = hh - ll
        ks[i] = 100.0 * (closes[i] - ll) / rng if rng > 0 else 50.0
    ds: list = [None] * len(bars)
    for i in range(len(bars)):
        w = [k for k in ks[max(0, i - d_period + 1):i + 1] if k is not None]
        if w:
            ds[i] = sum(w) / len(w)
    return {bars[i].ts: (ks[i], ds[i]) for i in range(len(bars))}


def _run_pipeline(candles: list[EngineCandle], req: dict, bias: dict[int, int],
                  setups_cfg: list[dict], quorum_k: int, entry_window_min: int,
                  entry_lookback: int, exit_obj: ExitPolicy, qty_shares: float,
                  regime_bars: list[dict] | None,
                  adaptive: dict[str, dict] | None, oracle: dict,
                  oracle_points: dict[str, list[datetime]], lot: int,
                  capital: float, label: str,
                  oracle_swings_detail: list[dict] | None = None,
                  bias_tf_sec: int = 3600,
                  ml_filter_obj=None) -> dict:
    """Один прогон (static или adaptive) через единый конвейер."""
    setup_runs: list[tuple[str, list[dict]]] = []
    setup_out: dict[str, dict] = {}
    _rsf = req.get("regime_setups_filter") or {}
    for s in setups_cfg:
        sid = s["strategy_id"]
        tf_sec = TF_SECONDS.get(s.get("tf", "5min"), 300)
        sigs = generate_signals(sid, s.get("params"), cached_resample(candles, tf_sec))
        # Фильтр по режиму: стратегия активна только в разрешённых режимах.
        _allowed = _rsf.get(sid)
        if _allowed is not None and regime_bars:
            sigs = [x for x in sigs
                    if ((regime_at(regime_bars, x["ts"]) or {}).get("state")) in _allowed]
        setup_runs.append((sid, sigs))
        setup_out[sid] = {"tf": s.get("tf", "5min"), "signals": len(sigs),
                          "BUY": sum(1 for x in sigs if x["side"] == "BUY"),
                          "SELL": sum(1 for x in sigs if x["side"] == "SELL")}

    # IMOEX: при высокой волатильности индекса — veto по направлению и/или отдельный голос.
    _imoex = req.get("imoex")
    _imoex_dir: dict = {}
    _imoex_hv: set = set()
    _imoex_veto = False
    _imoex_voice = False
    if _imoex:
        _imoex_dir = {str(k): int(v) for k, v in (_imoex.get("dir") or {}).items()}
        _imoex_hv = {str(x) for x in (_imoex.get("hv") or [])}
        _imoex_veto = bool(_imoex.get("veto"))
        _imoex_voice = bool(_imoex.get("voice"))
        if _imoex_voice:
            _isigs = [{"ts": datetime.fromisoformat(_t), "side": "BUY" if _d > 0 else "SELL",
                       "reason": "imoex_dir"}
                      for _t, _d in _imoex_dir.items() if _t in _imoex_hv]
            if _isigs:
                setup_runs.append(("imoex_direction", _isigs))
                setup_out["imoex_direction"] = {"tf": "5min", "signals": len(_isigs),
                                                "BUY": sum(1 for x in _isigs if x["side"] == "BUY"),
                                                "SELL": sum(1 for x in _isigs if x["side"] == "SELL")}

    quorum_sigs, funnel = merge_quorum(setup_runs, quorum_k)
    for idx, q in enumerate(quorum_sigs):
        q["event_id"] = f"Q{idx}"
    # entry_tf: "1min" (по умолчанию) — микро-брейкаут на 1м; "5min" — сигнал на 5м,
    # исполнение движком по open следующего 1м бара
    entry_tf = req.get("entry_tf", "1min")
    entry_candles = cached_resample(candles, TF_SECONDS.get(entry_tf, 60)) if entry_tf != "1min" else candles
    entries_raw = micro_breakout(entry_candles, entry_lookback)
    unique_raw_ts = len({s["ts"] for _, sigs in setup_runs for s in sigs})

    # Volume Exhaustion (VOLUME_EXHAUSTION_2026.md Шаг 1): серия фич по закрытым 5m-барам.
    # Торговлю не меняет — сигналы складываются в meta сделки и entry.volume_features.
    vol_series: list[dict] | None = None
    _vflow = req.get("volume_flow_filter")
    if req.get("volume_features") or req.get("volume_gate") or _vflow:
        from app.services.volume import VolumeParams as _VP
        _vp = _VP(price=str((_vflow or {}).get("price", "close")),
                  drop_ratio=float((_vflow or {}).get("ratio", 1.5)))
        vol_series = volume_features(cached_resample(candles, TF_SECONDS["5min"]), _vp)

    # Stochastic-фильтр (gate): не входить в BUY при перекупленности, в SELL — при перепроданности.
    stoch_map: dict | None = None
    if req.get("stoch_filter"):
        _scfg = req["stoch_filter"] or {}
        _cse = cached_resample(candles, TF_SECONDS.get(str(req.get("entry_tf", "5min")), 300))
        stoch_map = _stoch_map(_cse, int(_scfg.get("k_period", 14)), int(_scfg.get("d_period", 3)))

    accepted: list[dict] = []
    rejected: list[dict] = []
    regime_by_ts: dict[datetime, str] = {}
    entry_episodes: set[tuple[str, str]] = set()
    bias_mode = req.get("bias_mode", "veto")  # veto | info | strict_ct
    quorum_full = len(setup_runs)

    # EXP-002: entry_volatility_gate = rolling_atr_high_only
    # ATR_5m (Wilder 14, past-only) → per-day → rolling median 20 пред. торговых дней.
    # WARMUP: первые 20 завершённых торговых дней → входы запрещены.
    vol_gate = req.get("entry_volatility_gate")
    gate_info: dict | None = None
    if vol_gate == "rolling_atr_high_only":
        from zoneinfo import ZoneInfo as _ZI2
        _msk2 = _ZI2("Europe/Moscow")
        _c5 = cached_resample(candles, TF_SECONDS["5min"])
        # ATR Wilder 14 по 5m (строго прошлые бары: на каждый 5m бар)
        _atr = [None] * len(_c5)
        _trs: list[float] = []
        for i in range(1, len(_c5)):
            h, l, pc = _c5[i].high, _c5[i].low, _c5[i-1].close
            _trs.append(max(h-l, abs(h-pc), abs(l-pc)))
            if len(_trs) > 14:
                _trs.pop(0)
            if i >= 14:
                _atr[i] = sum(_trs) / 14
        # per-day: последний ATR дня
        _day_atr: dict[str, float] = {}
        _day_ts: dict[str, datetime] = {}
        for i, c in enumerate(_c5):
            d = c.ts.astimezone(_msk2).date().isoformat()
            if _atr[i] is not None:
                _day_atr[d] = _atr[i]
                _day_ts[d] = c.ts
        _day_ord = sorted(_day_atr)
        # rolling median предыдущих 20 дней (без текущего дня)
        _roll: dict[str, float | None] = {}
        for idx, d in enumerate(_day_ord):
            prev = _day_ord[max(0, idx-20):idx]
            _roll[d] = statistics.median([_day_atr[x] for x in prev]) if prev else None
        _warmup_days = set(_day_ord[:20])  # первые 20 завершённых дней окна
        gate_info = {"rolling_median_20d": {d: round(v,6) if v else None for d, v in _roll.items()},
                     "days_total": len(_day_ord), "warmup_days": sorted(_warmup_days)}
        _vol_day_ok: dict[str, bool] = {}
        for d in _day_ord:
            v = _roll.get(d)
            _vol_day_ok[d] = (v is not None and d not in _warmup_days and _day_atr[d] > v)

        def _gate_pass(ts_dt: datetime) -> tuple[bool, str]:
            d = ts_dt.astimezone(_msk2).date().isoformat()
            if d in _warmup_days:
                return False, "VOL_GATE_WARMUP"
            if not _vol_day_ok.get(d, False):
                return False, "VOL_GATE_LOW_VOL"
            return True, ""

    # B4-RUN: entry_pullback_depth = require_deep
    # pullback depth на decision_ts >= 0.5 * ATR5m(decision) в bps. Детерминированный
    # порог (без утечки из July shadow). Блокирует мелкие pullback'и перед breakout.
    pull_gate = req.get("entry_pullback_depth")
    _pull_pass = None
    if pull_gate in ("require_deep", "require_medium"):
        _thr_mult = {"require_deep": 0.5, "require_medium": 0.25}.get(pull_gate, 0.5)
        _c5 = cached_resample(candles, TF_SECONDS["5min"])
        _c5ts = [c.ts for c in _c5]
        _atr5 = [None] * len(_c5)
        _trs: list[float] = []
        for i in range(1, len(_c5)):
            h, l, pc = _c5[i].high, _c5[i].low, _c5[i-1].close
            _trs.append(max(h-l, abs(h-pc), abs(l-pc)))
            if len(_trs) > 14:
                _trs.pop(0)
            if i >= 14:
                _atr5[i] = sum(_trs) / 14

        def _pull_pass(ts_dt: datetime, side: str) -> tuple[bool, str]:
            return entry_pullback_deep_pass(_c5, _atr5, ts_dt, side, thr_mult=_thr_mult)

    # EXP-008: MACD-подтверждение на минутных свечах (entry_macd_1m).
    # Для каждого входа (5m/1m) требует, чтобы гистограмма MACD на 1m была
    # в сторону входа в момент сигнала: BUY -> histogram > 0, SELL -> histogram < 0.
    _macd_1m_hist: list[float | None] | None = None
    _macd_1m_ts: list[datetime] | None = None
    if req.get("entry_macd_1m"):
        try:
            _macd_fast = int(req.get("entry_macd_fast", 12))
            _macd_slow = int(req.get("entry_macd_slow", 26))
            _macd_sig = int(req.get("entry_macd_signal", 9))
            _macd_l, _macd_s, _macd_h = macd([c.close for c in candles],
                                             _macd_fast, _macd_slow, _macd_sig)
            _macd_1m_hist = list(_macd_h)
            _macd_1m_ts = list(c.ts for c in candles)
        except Exception:
            _macd_1m_hist = None
            _macd_1m_ts = None

    def _macd_1m_ok(ts_dt: datetime, side: str) -> bool | None:
        """True/False если направление известно, None если недостаточно данных."""
        if req.get("entry_macd_1m") is False or not _macd_1m_hist or not _macd_1m_ts:
            return None
        idx = None
        # последняя 1m свеча, закрывшаяся НЕ ПОЗЖЕ момента входа
        for i, c in enumerate(_macd_1m_ts):
            if c <= ts_dt:
                idx = i
            else:
                break
        if idx is None:
            return None
        h = _macd_1m_hist[idx]
        if h is None:
            return None
        return h > 0 if side == "BUY" else h < 0

    for e in entries_raw:
        ts, side = e["ts"], e["side"]
        bucket = int(ts.timestamp()) // bias_tf_sec
        bias_ok = (side == "BUY" and bias.get(bucket, 0) >= 0) or (side == "SELL" and bias.get(bucket, 0) <= 0)
        if not bias_ok:
            if bias_mode == "veto":
                rejected.append({**e, "ts": ts.isoformat(), "reason": "AGAINST_BIAS"})
                continue
            # info/strict_ct: пропускаем против bias дальше, но строже по кворуму
            e = {**e, "against_bias": True}
        # EXP-002: vol gate (rolling_atr_high_only) — блокирует входы в low-vol/WARMUP
        if vol_gate == "rolling_atr_high_only":
            _ok, _why = _gate_pass(ts)
            if not _ok:
                rejected.append({**e, "ts": ts.isoformat(), "reason": _why})
                continue
        # B4-RUN: pullback-depth gate — блокирует мелкие pullback'и перед breakout
        if pull_gate in ("require_deep", "require_medium"):
            _ok, _why = _pull_pass(ts, side)
            if not _ok:
                rejected.append({**e, "ts": ts.isoformat(), "reason": _why})
                continue
        # адаптивный режим: своя конфигурация (setups/quorum/mode/exit) для состояния
        mode = "both"
        quorum_pool = quorum_sigs
        state = None
        # Per-regime quorum: filter strategies by regime
        per_rq = req.get("per_regime_quorum")
        if per_rq is not None:
            _pr_state = regime_at(regime_bars, ts)
            _pr_name = _pr_state["state"] if _pr_state else "NEUTRAL"
            _pr_allowed = per_rq.get(_pr_name)
            if _pr_allowed is not None:
                _pr_member_runs = [(sid, sigs) for sid, sigs in setup_runs if sid in _pr_allowed]
                if _pr_member_runs:
                    _pr_quorum, _ = merge_quorum(_pr_member_runs, quorum_k)
                    for _pi, _pq in enumerate(_pr_quorum):
                        if "event_id" not in _pq:
                            _pq["event_id"] = f"PR_{_pr_name}_{_pi}"
                    quorum_pool = _pr_quorum
        if adaptive is not None:
            r = regime_at(regime_bars, ts)
            state = r["state"] if r else "NEUTRAL"
            cfg = adaptive.get(state)
            if cfg is None or cfg.get("no_trade"):
                rejected.append({**e, "ts": ts.isoformat(),
                                 "reason": f"REGIME_BLOCKED:{state}"})
                continue
            mode = cfg.get("mode", "both")
            if cfg.get("quorum_list") is not None:
                quorum_pool = cfg["quorum_list"]
        window_from = ts - timedelta(minutes=entry_window_min)
        need_votes = quorum_k
        if e.get("against_bias") and bias_mode == "strict_ct":
            need_votes = quorum_full  # counter-trend только при полном согласии
        quorum_ev = next((q for q in quorum_pool
                          if q["side"] == side and window_from <= q["ts"] <= ts
                          and int(q["features"].get("votes", 0)) >= need_votes), None)
        if quorum_ev is None:
            rejected.append({**e, "ts": ts.isoformat(), "reason": "SETUP_MISSING"})
            continue
        # IMOEX-veto: при HV индекса вход разрешён только по его направлению.
        if _imoex_veto and _imoex_hv:
            _tis = ts.isoformat()
            if _tis in _imoex_hv:
                _id = _imoex_dir.get(_tis)
                if _id is not None and ((_id > 0 and side == "SELL") or (_id < 0 and side == "BUY")):
                    rejected.append({**e, "ts": _tis, "reason": "IMOEX_VETO"})
                    continue
        # Stochastic-фильтр: BUY запрещён при перекупленности, SELL — при перепроданности.
        if stoch_map is not None:
            _sk, _sd = stoch_map.get(ts, (None, None))
            if _sk is not None:
                _sc = req.get("stoch_filter") or {}
                if (side == "BUY" and _sk > float(_sc.get("overbought", 80))) or \
                   (side == "SELL" and _sk < float(_sc.get("oversold", 20))):
                    rejected.append({**e, "ts": ts.isoformat(), "reason": "STOCH_FILTER"})
                    continue
        # Volume-flow фильтр: вход только при подтверждении объёмом.
        if _vflow is not None and vol_series is not None:
            _vfn = volume_at(vol_series, ts)
            if _vfn is not None:
                _vmode = str(_vflow.get("mode", "sell_only"))
                if side == "SELL" and not _vfn.get("volume_on_drop"):
                    rejected.append({**e, "ts": ts.isoformat(), "reason": "VOL_FLOW"})
                    continue
                if side == "BUY" and _vmode == "both" and not _vfn.get("volume_on_rise"):
                    rejected.append({**e, "ts": ts.isoformat(), "reason": "VOL_FLOW"})
                    continue
        if mode == "long" and side == "SELL":
            rejected.append({**e, "ts": ts.isoformat(), "reason": "REGIME_MODE:long"})
            continue
        if mode == "short" and side == "BUY":
            rejected.append({**e, "ts": ts.isoformat(), "reason": "REGIME_MODE:short"})
            continue
        if state is not None:
            regime_by_ts[ts] = state
        entry_episodes.add((side, quorum_ev["event_id"]))
        if ml_filter_obj is not None:
            ok_ml, why_ml = ml_filter_obj.accepts(ts)
            if not ok_ml:
                rejected.append({**e, "ts": ts.isoformat(), "reason": why_ml})
                continue
        # Volume filter: reject entries with low volume_ratio
        vol_filter_thr = req.get("volume_filter_threshold")
        if vol_filter_thr is not None:
            _vf_ts = ts
            _vf_c5 = cached_resample(candles, TF_SECONDS["5min"])
            _vf_idx = None
            for _i, _c in enumerate(_vf_c5):
                if _c.ts <= _vf_ts:
                    _vf_idx = _i
                else:
                    break
            if _vf_idx is not None and _vf_idx >= 1:
                _vf_vols = [v.volume for v in _vf_c5[:_vf_idx+1]]
                _vf_mean = sum(_vf_vols[max(0, _vf_idx-50):_vf_idx]) / max(1, min(_vf_idx, 50))
                _vf_ratio = _vf_c5[_vf_idx].volume / max(_vf_mean, 1e-9)
                if _vf_ratio < vol_filter_thr:
                    rejected.append({**e, "ts": ts.isoformat(), "reason": f"VOL_FILTER:{_vf_ratio:.2f}<{vol_filter_thr}"})
                    continue
        # Volume Exhaustion gate (VOLUME_EXHAUSTION_2026.md §12, Шаг 2):
        # пост-фильтр входов по сигналам объёма на закрытом 5m-баре. Ядро кворума не трогаем.
        # ФОРМАТ: {"require": {"volume_on_drop": ["SELL"]}, "block": {"dryup": ["BUY"]}}
        #   require — side без сигнала отбрасывается ("V4 подтверждает SHORT")
        #   block   — side при наличии сигнала отбрасывается   ("dryup блокирует LONG")
        _vol_gate = req.get("volume_gate")
        if _vol_gate and vol_series:
            _vf_now = volume_at(vol_series, ts)
            if _vf_now is not None:
                _gate_pass = True
                _req_map = _vol_gate.get("require") or {}
                for _sig, _sides in _req_map.items():
                    if side in _sides and not _vf_now.get(_sig):
                        rejected.append({**e, "ts": ts.isoformat(),
                                         "reason": f"VOL_GATE_REQUIRE:{_sig}:{side}"})
                        _gate_pass = False
                        break
                if _gate_pass:
                    _blk_map = _vol_gate.get("block") or {}
                    for _sig, _sides in _blk_map.items():
                        if side in _sides and _vf_now.get(_sig):
                            rejected.append({**e, "ts": ts.isoformat(),
                                             "reason": f"VOL_GATE_BLOCK:{_sig}:{side}"})
                            _gate_pass = False
                            break
                if _gate_pass:
                    accepted.append({**e, "ts": ts.isoformat(),
                                     "quorum_event_id": quorum_ev["event_id"],
                                     "volume_features": _vf_now})
                    continue
                else:
                    continue
            else:
                accepted.append({**e, "ts": ts.isoformat(), "quorum_event_id": quorum_ev["event_id"],
                                 "volume_features": None})
                continue
        # SIGNAL_SCORE (SIGNAL_SCORE_2026.md п.3): score уверенности входа.
        # Собираем на момент входа из доступных фич (без look-ahead) и либо
        # используем как meta, либо как пост-фильтр (по контракту — НЕ gate,
        # а evidence для sizing; gate оставлен только для обратной совместимости).
        # ФОРМАТ: {"threshold": 0.7, "weights": {...}} — weights опциональны.
        _score_cfg = req.get("score_gate")
        _score_raw = None
        if _score_cfg is not None or req.get("score_features"):
            _score_raw = _signal_score(
                quorum_ev, e, side, ts, quorum_pool, quorum_full,
                regime_bars, vol_series, _macd_1m_ok,
                (_score_cfg or {}).get("weights") or {},
            )
        if _score_cfg is not None and _score_raw is not None:
            _s_norm = _score_raw["score"]
            _thr = float(_score_cfg.get("threshold", 0.4))
            if _s_norm < _thr:
                rejected.append({**e, "ts": ts.isoformat(),
                                 "reason": f"SCORE:{_s_norm:.2f}<{_thr}",
                                 "score": _s_norm})
                continue
        _macd_ok = _macd_1m_ok(ts, side)
        if _macd_ok is False:
            rejected.append({**e, "ts": ts.isoformat(), "reason": "MACD_1M_AGAINST"})
            continue
        _acc_entry = {**e, "ts": ts.isoformat(), "quorum_event_id": quorum_ev["event_id"],
                      "volume_features": volume_at(vol_series, ts) if vol_series else None}
        if _score_raw is not None:
            _acc_entry["score"] = _score_raw["score"]
            _acc_entry["score_components"] = _score_raw.get("components", {})
        accepted.append(_acc_entry)

    if adaptive is not None:
        exit_obj = RegimeExitPolicy(exit_obj, adaptive["_exit_policies"], regime_bars)

    cfg_engine = EngineConfig(
        figi=str(req.get("figi", "")), qty=qty_shares, allow_short=True,
        cost_model=CostModel(
            commission_rate=float(req.get("commission_rate", 0.0005)),
            slippage_bps=float(req.get("slippage_bps", 2.0)),
        ),
        signal_policy=SignalPolicyConfig(min_hold_bars=int(req.get("min_hold_bars", 0)),
                                         same_side_reentry_cooldown_bars=int(req.get("same_side_reentry_cooldown_bars", 0)),
                                         exit_confirm_window_bars=int(req.get("exit_confirm_window_bars", 0)),
                                         entry_confirm_bars=int(req.get("entry_confirm_bars", 0)),
                                         opposite_hold=bool(req.get("opposite_hold", False)),
                                         confirm_flip=bool(req.get("confirm_flip", False))),
        session_policy=_build_session_policy(req),
        neutral_mode=req.get("neutral_mode"),
        regime_bars=regime_bars,
    )
    # Exit volume filter: remove exits with low volume
    vol_filter_thr = req.get("volume_filter_threshold")
    filtered_exits = entries_raw
    if vol_filter_thr is not None:
        _ef_c5 = cached_resample(candles, TF_SECONDS["5min"])
        _ef_pass = []
        for _ef_e in entries_raw:
            _ef_ts = _ef_e["ts"]
            _ef_idx = None
            for _i, _c in enumerate(_ef_c5):
                if _c.ts <= _ef_ts:
                    _ef_idx = _i
                else:
                    break
            if _ef_idx is not None and _ef_idx >= 1:
                _ef_vols = [v.volume for v in _ef_c5[:_ef_idx+1]]
                _ef_mean = sum(_ef_vols[max(0, _ef_idx-50):_ef_idx]) / max(1, min(_ef_idx, 50))
                _ef_ratio = _ef_c5[_ef_idx].volume / max(_ef_mean, 1e-9)
                if _ef_ratio >= vol_filter_thr:
                    _ef_pass.append(_ef_e)
            else:
                _ef_pass.append(_ef_e)
        filtered_exits = _ef_pass
    runner = EngineRunner(strategy=ReplayStrategy(
        [(a["ts"], a["side"]) for a in accepted],
        exits=[(e["ts"], e["side"]) for e in filtered_exits],
    ),
        exit_policy=exit_obj, config=cfg_engine)
    ledger = runner.run(candles)
    exit_coverage = dict(runner.exit_coverage)
    reentry_rejected: list[dict] = []
    for e in ledger.audit:
        if e.kind == "DECISION" and "REJECT_REENTRY" in e.detail:
            m = re.search(r"same-side (\w+) (\d+)b < cooldown (\d+)b", e.detail)
            reentry_rejected.append({
                "side": m.group(1) if m else "?",
                "signal_ts": e.time.isoformat(),
                "bars_since_exit": int(m.group(2)) if m else None,
                "cooldown_bars": int(m.group(3)) if m else None,
                "reason": "SAME_SIDE_REENTRY_COOLDOWN",
            })

    trades_out = []
    gross = commission = slippage = 0.0
    per_regime: dict[str, dict] = {}
    entry_by_ts = {a["ts"]: a for a in accepted}
    episode_map: dict[tuple[str, str], dict] = {}
    executed_signal_keys: set[tuple[str, str]] = set()
    for a in accepted:
        key = (a["side"], a.get("quorum_event_id", "N/A"))
        ep = episode_map.setdefault(key, {
            "episode_id": f"E{len(episode_map) + 1:03d}",
            "side": a["side"],
            "quorum_event_id": a.get("quorum_event_id", "N/A"),
            "first_ts": a["ts"], "last_ts": a["ts"],
            "status": "UNRESOLVED", "entry_px": None, "exit_px": None, "net": None,
        })
        if a["ts"] < ep["first_ts"]:
            ep["first_ts"] = a["ts"]
        if a["ts"] > ep["last_ts"]:
            ep["last_ts"] = a["ts"]
    fee_rate = 0.0005
    slip_bps = 0.0002
    gross_moves_pct: list[float] = []
    costs_list: list[float] = []
    break_even_list: list[float] = []
    break_even_by_regime: dict[str, list[float]] = {}
    break_even_by_side: dict[str, list[float]] = {}
    quorum_ts_by_id = {q["event_id"]: q["ts"] for q in quorum_sigs}
    for t in ledger.trades:
        r_dist = abs(t.entry_price - t.initial_stop) if t.initial_stop else t.entry_price * 0.01
        mm = _mfe_mae(candles, t.entry_index, t.exit_index, t.entry_price, t.side, r_dist)
        gross += t.gross_pnl
        commission += t.commission
        slippage += t.slippage
        state = regime_by_ts.get(t.entry_time, "NO_REGIME")
        bucket = per_regime.setdefault(state, {"trades": 0, "gross": 0.0, "net": 0.0, "wins": 0})
        bucket["trades"] += 1
        bucket["gross"] += t.gross_pnl
        bucket["net"] += t.net_pnl
        bucket["wins"] += 1 if t.net_pnl > 0 else 0

        entry_notional = t.entry_price * qty_shares
        exit_notional = t.exit_price * qty_shares
        entry_commission = entry_notional * fee_rate
        exit_commission = exit_notional * fee_rate
        entry_slip = entry_notional * slip_bps
        exit_slip = exit_notional * slip_bps
        costs_t = entry_commission + exit_commission + entry_slip + exit_slip
        move_pct = ((t.exit_price - t.entry_price) / t.entry_price * 100
                    if t.side == "LONG" else
                    (t.entry_price - t.exit_price) / t.entry_price * 100)
        gross_moves_pct.append(move_pct)
        costs_list.append(costs_t)
        avg_notional_t = (entry_notional + exit_notional) / 2
        break_even_t = costs_t / max(avg_notional_t, 1e-9) * 100
        break_even_list.append(break_even_t)
        break_even_by_regime.setdefault(state, []).append(break_even_t)
        break_even_by_side.setdefault(t.side, []).append(break_even_t)
        trades_out.append({
            "side": t.side, "regime": state,
            "entry_ts": t.entry_time.isoformat(), "exit_ts": t.exit_time.isoformat(),
            "entry_px": round(t.entry_price, 6), "exit_px": round(t.exit_price, 6),
            "entry_index": t.entry_index,
            "stop": round(t.initial_stop, 6) if t.initial_stop else None,
            "initial_stop": round(t.initial_stop, 6) if t.initial_stop else None,
            "target": round(t.take_profit, 6) if t.take_profit else None,
            "gross": round(t.gross_pnl, 2), "commission": round(t.commission, 2),
            "net": round(t.net_pnl, 2), "bars_held": t.bars_held,
            "exit_reason": t.exit_reason, "mfe_r": mm["mfe_r"], "mae_r": mm["mae_r"],
            "entry_notional": round(entry_notional, 2), "exit_notional": round(exit_notional, 2),
            "entry_commission": round(entry_commission, 2), "exit_commission": round(exit_commission, 2),
            "entry_slippage": round(entry_slip, 2), "exit_slippage": round(exit_slip, 2),
            "costs": round(costs_t, 2),
            "costs_pct_of_notional": round(costs_t / max(avg_notional_t, 1e-9) * 100, 3),
            "break_even_move_pct": round(break_even_t, 3),
            "gross_move_pct": round(move_pct, 3),
        })
        a = entry_by_ts.get(t.entry_time.isoformat())
        if a is None:
            # исполнение идёт по open следующего бара — ищем последний сигнал того же
            # направления, не позже момента входа
            want_side = "BUY" if t.side == "LONG" else "SELL"
            cand = [x for x in accepted if x["side"] == want_side
                    and x["ts"] <= t.entry_time.isoformat()]
            a = max(cand, key=lambda x: x["ts"]) if cand else None
        if a is not None and a.get("volume_features") is not None:
            trades_out[-1]["volume_features"] = a["volume_features"]
        if a is not None and a.get("score") is not None:
            trades_out[-1]["score"] = a["score"]
            trades_out[-1]["score_components"] = a.get("score_components", {})
        if a is not None:
            executed_signal_keys.add((a["ts"], a["side"]))
            ep = episode_map[(a["side"], a.get("quorum_event_id", "N/A"))]
            ep["status"] = "TRADED"
            ep["entry_px"] = round(t.entry_price, 6)
            ep["exit_px"] = round(t.exit_price, 6)
            ep["net"] = round(t.net_pnl, 2)
            ep["entry_time"] = t.entry_time.isoformat()
            ep["exit_time"] = t.exit_time.isoformat()
            ep["quorum_event_ts"] = quorum_ts_by_id.get(ep["quorum_event_id"]).isoformat() \
                if ep["quorum_event_id"] in quorum_ts_by_id else None
            ep["net_sum"] = round(ep.get("net_sum", 0.0) + t.net_pnl, 2)
            ep["cost_sum"] = round(ep.get("cost_sum", 0.0) + costs_t, 2)
            ep["trades_count"] = ep.get("trades_count", 0) + 1

    wins = [t for t in ledger.trades if t.net_pnl > 0]
    net = gross - commission
    pf = round(sum(t.gross_pnl for t in ledger.trades if t.gross_pnl > 0) /
               max(abs(sum(t.gross_pnl for t in ledger.trades if t.gross_pnl < 0)), 1e-9), 2)
    equity, cum = [], 0.0
    for t in ledger.trades:
        cum += t.net_pnl
        equity.append({"ts": t.exit_time.isoformat(), "equity": round(capital + cum, 2)})

    n_trades = max(len(trades_out), 1)
    costs_total = commission + slippage
    notional_avg = sum(t["entry_px"] for t in trades_out) / n_trades * qty_shares
    gross_neg = abs(sum(t.gross_pnl for t in ledger.trades if t.gross_pnl < 0))
    cost_per_trade = costs_total / n_trades
    break_even = cost_per_trade / max(notional_avg, 1e-9) * 100
    med_move = sorted(gross_moves_pct)[len(gross_moves_pct) // 2] if gross_moves_pct else 0.0
    above_be = sum(1 for m in gross_moves_pct if m >= break_even) if trades_out else 0
    episodes = sorted(episode_map.values(), key=lambda e: e["first_ts"])
    completed = sum(1 for e in episodes if e["status"] == "TRADED")
    traded_eps = [e for e in episodes if e["status"] == "TRADED"]
    ep_nets_sorted = sorted(e.get("net_sum", e.get("net") or 0.0) for e in traded_eps)
    ep_costs = [e.get("cost_sum", 0.0) for e in traded_eps]
    ep_trades = [e.get("trades_count", 1) for e in traded_eps]
    net_per_episode_mean = (sum(ep_nets_sorted) / len(ep_nets_sorted)) if ep_nets_sorted else 0.0
    cost_per_episode_mean = (sum(ep_costs) / len(ep_costs)) if ep_costs else 0.0
    net_per_episode_total = sum(ep_nets_sorted)

    def _q(vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        return s[min(len(s) - 1, int(len(s) * p))]

    def _be_agg(vals: list[float]) -> dict:
        return {"n": len(vals),
                "mean": round(sum(vals) / max(len(vals), 1), 3),
                "median": round(_q(vals, 0.5), 3),
                "p25": round(_q(vals, 0.25), 3),
                "p75": round(_q(vals, 0.75), 3)}

    _analytics = bool(req.get("analytics", False))
    logger.debug("pipeline analytics=%s setups=%d quorum=%d candles=%d",
                 _analytics, len(setup_runs), quorum_k, len(candles))
    quality = []
    if _analytics:
        for sid, sigs in setup_runs:
            quality.extend(_signal_quality("setup", sid, setup_out[sid]["tf"], sigs,
                                           oracle_points, entry_window_min))
        quality.extend(_signal_quality("entry", "micro_breakout", "1min", entries_raw,
                                       oracle_points, entry_window_min))
    useless = sorted({q["strategy_id"] for q in quality if q.get("useless")})

    mfe_all = [t["mfe_r"] for t in trades_out]
    mae_all = [t["mae_r"] for t in trades_out]
    hit_1r = sum(1 for t in trades_out if t["mfe_r"] >= 1.0 and t["mae_r"] < 1.0)
    hit_2r = sum(1 for t in trades_out if t["mfe_r"] >= 2.0 and t["mae_r"] < 1.0)

    return {
        "label": label,
        "setups": setup_out,
        "funnel": {
            "raw_signals": sum(v["signals"] for v in setup_out.values()),
            "unique_raw_ts": unique_raw_ts,
            "quorum_unique": len(quorum_sigs),
            "quorum_BUY": sum(1 for q in quorum_sigs if q["side"] == "BUY"),
            "quorum_SELL": sum(1 for q in quorum_sigs if q["side"] == "SELL"),
            "entries_raw": len(entries_raw),
            "accepted_decisions": len(accepted),
            "unique_entry_episodes": len(entry_episodes),
            "entries_rejected": len(rejected),
            "reentry_rejected": len(reentry_rejected),
            "reentry_rejected_list": reentry_rejected,
            "preview_trades": len(trades_out),
        },
        "funnel_tf": {
            "bias_states": {
                "LONG_ALLOWED": sum(1 for v in bias.values() if v >= 0),
                "SHORT_ALLOWED": sum(1 for v in bias.values() if v <= 0),
                "NEUTRAL": sum(1 for v in bias.values() if v == 0),
            },
            "setups": {
                "candidates": sum(v["signals"] for v in setup_out.values()),
                "quorum_passed": len(quorum_sigs),
                "BUY": sum(1 for q in quorum_sigs if q["side"] == "BUY"),
                "SELL": sum(1 for q in quorum_sigs if q["side"] == "SELL"),
            },
            "entries": {
                "candidates": len(entries_raw),
                "accepted": len(accepted),
                "rejected": len(rejected),
            },
            "rejected_by_reason": dict(__import__("collections").Counter(r["reason"].split(":")[0] for r in rejected)),
        },
        "why_no_entry": [{
            "ts": r["ts"], "side": r["side"],
            "code": r["reason"].split(":")[0],
            "detail": r["reason"],
        } for r in rejected[-500:]],
        "counterfactual_reentries": (_counterfactual_reentries(candles, reentry_rejected,
                                                               exit_obj, qty_shares)
                                     if _analytics else None),
        "counterfactual_hold": (_counterfactual_hold(candles, trades_out, exit_obj, qty_shares)
                                if _analytics else None),
        "exit_coverage": exit_coverage,
        "oracle_coverage": (_oracle_coverage(oracle_swings_detail, entries_raw, accepted,
                                             rejected, int(req.get("oracle_window_min", 10)),
                                             executed_signals=executed_signal_keys)
                            if (_analytics and oracle_swings_detail) else None),
        "episodes": {
            "quorum_points": len(quorum_sigs),
            "entry_decisions": len(accepted),
            "unique_episodes": len(episodes),
            "completed": completed,
            "unresolved": len(episodes) - completed,
            "list": episodes,
        },
        "quorum_list": [{"ts": q["ts"].isoformat(), "side": q["side"],
                         "votes": q["features"].get("votes"),
                         "buy_votes": q["features"].get("buy_votes", 0),
                         "sell_votes": q["features"].get("sell_votes", 0),
                         "members_for": q["features"].get("members_for", []),
                         "opposition": q["features"].get("opposition", []),
                         "total_members": len(setup_runs),
                         "quorum_k": quorum_k,
                         "event_id": q["event_id"]}
                        for q in quorum_sigs],
        "entries": accepted,
        "rejected": rejected,
        "votes_last": _votes_last(setup_runs),
        "trades": trades_out,
        "economic": {
            "trades": len(trades_out), "gross": round(gross, 2),
            "commission": round(commission, 2), "slippage": round(slippage, 2),
            "costs": round(costs_total, 2),
            "net": round(net, 2), "profit_factor": pf,
            "win_rate_pct": round(len(wins) / n_trades * 100, 1),
            "avg_hold_bars": round(sum(t["bars_held"] for t in trades_out) / n_trades, 1),
            "turnover": round((gross + gross_neg) / capital * 100, 1),
            "gross_per_trade": round(gross / n_trades, 2),
            "cost_per_trade": round(cost_per_trade, 2),
            "cost_gross_ratio": round(costs_total / max(abs(gross), 1e-9), 2),
            "break_even_move_pct": round(break_even, 3),
            "break_even_by_trade": _be_agg(break_even_list),
            "break_even_by_regime": {k: _be_agg(v) for k, v in sorted(break_even_by_regime.items())},
            "break_even_by_side": {k: _be_agg(v) for k, v in sorted(break_even_by_side.items())},
            "avg_gross_move_pct": round(sum(gross_moves_pct) / n_trades, 3) if trades_out else 0.0,
            "median_gross_move_pct": round(med_move, 3),
            "median_costs": round(sorted(costs_list)[len(costs_list) // 2], 2) if costs_list else 0.0,
            "trades_above_break_even": above_be,
            "net_per_episode": {
                "completed_episodes": completed,
                "total": round(net_per_episode_total, 2),
                "mean": round(net_per_episode_mean, 2),
                "median": round(_q(ep_nets_sorted, 0.5), 2),
                "p25": round(_q(ep_nets_sorted, 0.25), 2),
                "p75": round(_q(ep_nets_sorted, 0.75), 2),
            },
            "cost_per_episode_mean": round(cost_per_episode_mean, 2),
            "trades_per_episode_mean": round(sum(ep_trades) / len(ep_trades), 2) if ep_trades else 0.0,
            "median_net_per_trade": round(_q(sorted(t["net"] for t in trades_out), 0.5), 2),
            "equity": equity,
        },
        "capture_ratio": {
            "oracle_gross_potential": round(oracle["gross"], 2) if oracle else None,
            "causal_gross": round(gross, 2),
            "causal_costs": round(costs_total, 2),
            "causal_net": round(net, 2),
            "gross_capture_pct": round(gross / max(oracle["gross"], 1e-9) * 100, 1) if oracle else None,
            "net_capture_pct": round(net / max(oracle["gross"], 1e-9) * 100, 1) if oracle else None,
        },
        "session_stats": {
            "positions_carried_overnight": sum(
                1 for t in trades_out if t["entry_ts"][:10] != t["exit_ts"][:10]
            ),
            "session_close_forced": sum(
                1 for t in trades_out if t["exit_reason"] == "session_close"
            ),
            "open_positions_at_eod": sum(
                1 for t in trades_out if t["exit_reason"] == "end_of_data"
            ),
            "closed_by_signal": sum(
                1 for t in trades_out if t["exit_reason"] == "signal_exit"
            ),
            "closed_by_stop": sum(
                1 for t in trades_out if t["exit_reason"] == "stop_loss"
            ),
            "closed_by_target": sum(
                1 for t in trades_out if t["exit_reason"] == "target"
            ),
        },
        "quality": quality,
        "useless_strategies": useless,
        "per_regime": {k: {"trades": v["trades"], "gross": round(v["gross"], 2),
                           "net": round(v["net"], 2), "win_rate_pct": round(v["wins"] / max(v["trades"], 1) * 100, 1)}
                       for k, v in per_regime.items()},
        "movement_capture": {
            "mfe_r_median": round(sorted(mfe_all)[len(mfe_all) // 2], 2) if mfe_all else None,
            "mae_r_median": round(sorted(mae_all)[len(mae_all) // 2], 2) if mae_all else None,
            "hit_1r_before_minus1r": hit_1r, "hit_2r_before_minus1r": hit_2r,
            "of_trades": len(trades_out),
        },
    }


def compute_ensemble(candles_1m: list[EngineCandle], req: dict) -> dict:
    lot = int(req.get("lot", 10))
    capital = float(req.get("capital", 100_000))
    days = int(req.get("days", 3))
    bias_cfg = req.get("bias", {"tf": "hour", "period": 50})
    setups_cfg = list(req.get("setups", []))
    quorum_k = int(req.get("quorum", 2))
    entry_cfg = req.get("entry", {"tf": "1min", "lookback": 1})
    entry_window_min = int(req.get("entry_window_min", 15))
    exit_cfg = req.get("exit_policy", {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2}})
    oracle_cfg = req.get("oracle", {"threshold_pct": 0.5, "fee_rate_pct": 0.05})
    regime_cfg = req.get("regime", {"tf": "5min"})
    adaptive_cfg = req.get("adaptive", [])
    use_all = bool(req.get("use_all_setups", False))
    drop_useless = bool(req.get("drop_useless", False))

    # --- ML-фильтр: LightGBM gate качества сигналов (опционально) ---
    ml_filter_cfg = req.get("ml_filter")
    ml_filter_obj = None
    if ml_filter_cfg:
        ml_filter_obj = MlEnsembleFilter(
            threshold=float(ml_filter_cfg.get("threshold", 0.55)),
            ticker=ml_filter_cfg.get("ticker") or None,
            figi=req.get("figi") or None,
        )

    if req.get("from_ts") or req.get("to_ts"):
        _f = req.get("from_ts")
        _t = req.get("to_ts")
        if _f:
            candles = [c for c in candles_1m if c.ts >= datetime.fromisoformat(_f.replace("Z", "+00:00"))]
        else:
            candles = candles_1m
        if _t:
            candles = [c for c in candles if c.ts <= datetime.fromisoformat(_t.replace("Z", "+00:00"))]
    else:
        candles = [c for c in candles_1m if c.ts >= datetime.now(timezone.utc) - timedelta(days=days)]
    if len(candles) < 120:
        return {"error": "мало свечей", "bars": len(candles)}

    # --- Validate candles for anomalies ---
    candles, skipped_count = _validate_candles(candles)
    if skipped_count > 0:
        logger.warning("Skipped %d broken candles", skipped_count)
    if len(candles) < 120:
        return {"error": "мало свечей после валидации", "bars": len(candles)}

    # --- все функции, если запрошено ---
    if use_all or not setups_cfg:
        default_tf = "5min"
        setups_cfg = [{"strategy_id": sid, "tf": default_tf, "params": {}} for sid in ALL_STRATEGY_IDS]

    # --- bias ---
    bias_tf_sec = TF_SECONDS.get(str(bias_cfg.get("tf", "hour")), 3600)
    bias_bars = cached_resample(candles, bias_tf_sec)
    bias = compute_bias(bias_bars, int(bias_cfg.get("period", 50)), tf_seconds=bias_tf_sec)

    # --- regime timeline (на режимном ТФ) ---
    regime_tf_sec = TF_SECONDS.get(regime_cfg.get("tf", "5min"), 300)
    regime_bars = cached_resample(candles, regime_tf_sec)
    # REGIME-GATE: кастомный режим по дате (IMOEX daily range) вместо индикаторов
    gate = req.get("regime_gate")
    if gate and gate.get("high_vol_dates"):
        from zoneinfo import ZoneInfo as _ZI
        _msk = _ZI("Europe/Moscow")
        _hv = set(gate["high_vol_dates"])
        # regime_bars: заменяем ts на даты, состояние HIGH_VOL/LOW_VOL
        _bars_out = []
        for b in regime_bars:
            d = b.ts.astimezone(_msk).date().isoformat()
            _bars_out.append({"ts": b.ts, "state": "HIGH_VOL" if d in _hv else "LOW_VOL"})
        regime_bars = _bars_out
        timeline = regime_bars
        regime_row = regime_bars
        _regime_gated = True
    else:
        detector = RegimeDetector(**{k: v for k, v in regime_cfg.items()
                                     if k in ("slope_threshold", "adx_threshold",
                                              "atr_percentile_threshold", "range_mult")})
        regime_row = detector.compute(regime_bars)
        from app.services.regime import regime_timeline
        timeline = regime_timeline(regime_row)
        _regime_gated = False

    # --- размер позиции и оракул ---
    qty_shares = lot
    entry_px_sample = candles[0].open
    qty_shares = max(int(capital / (entry_px_sample * lot)) * lot, lot)
    _analytics = bool(req.get("analytics", False))
    _need_oracle = _analytics or (drop_useless and len(setups_cfg) > 1)
    oracle = (_oracle_fixed_qty(candles, oracle_cfg.get("threshold_pct", 0.5),
                                oracle_cfg.get("fee_rate_pct", 0.05) / 100.0, qty_shares)
              if _analytics else None)
    o_points: dict[str, list[datetime]] = {"BUY": [], "SELL": []}
    o_conf_points: dict[str, list[datetime]] = {"BUY": [], "SELL": []}
    o_swings_detail: list[dict] = []
    oracle_swings: list[dict] = []
    if _need_oracle:
        oracle_swings = zigzag_swings([c.__dict__ for c in candles],
                                      oracle_cfg.get("threshold_pct", 0.5) / 100.0)
        for sw in oracle_swings:
            side = "BUY" if sw["kind"] == "low" else "SELL"
            o_points[side].append(candles[sw["idx"]].ts)
            o_conf_points[side].append(candles[sw["conf"]].ts)
            if _analytics:
                o_swings_detail.append({
                    "side": side,
                    "point_ts": candles[sw["idx"]].ts.isoformat(),
                    "confirmation_ts": candles[sw["conf"]].ts.isoformat(),
                    "point_idx": sw["idx"],
                    "conf_idx": sw["conf"],
                })

    # --- отсев бесполезных (по предварительному прогону сигналов) ---
    if drop_useless and len(setups_cfg) > 1:
        keep: list[dict] = []
        for s in setups_cfg:
            if s["strategy_id"] in VOLUME_STRATEGY_IDS:
                keep.append(s)
                continue
            sigs = generate_signals(s["strategy_id"], s.get("params"),
                                    cached_resample(candles, TF_SECONDS.get(s.get("tf", "5min"), 300)))
            q = _signal_quality("setup", s["strategy_id"], s.get("tf", "5min"), sigs,
                                o_points, entry_window_min)
            if not any(x.get("useless") for x in q):
                keep.append(s)
        setups_cfg = keep or setups_cfg

    static_exit = build_exit_policy(exit_cfg["id"], exit_cfg.get("params"))

    # --- adaptive конфигурации {режим → mode/exit/setups/quorum/no_trade} ---
    adaptive_map: dict[str, dict] | None = None
    adaptive_exits: dict[str, ExitPolicy] = {}
    if adaptive_cfg:
        adaptive_map = {}
        for a in adaptive_cfg:
            state = a.get("name", "NEUTRAL")
            cfg = a.get("config") or {}
            if a.get("no_trade") or not cfg:
                adaptive_map[state] = {"no_trade": True}
                continue
            mode = cfg.get("mode", "both")
            entry_cfg_adapt = {"mode": mode}
            setups_sub = cfg.get("setups")
            q_k = int(cfg.get("quorum", quorum_k))
            if setups_sub:
                runs_sub = [
                    (s["strategy_id"],
                     generate_signals(s["strategy_id"], s.get("params"),
                                      cached_resample(candles, TF_SECONDS.get(s.get("tf", "5min"), 300))))
                    for s in setups_sub
                ]
                q_sub, _ = merge_quorum(runs_sub, q_k)
                for idx, q in enumerate(q_sub):
                    q["event_id"] = f"R{state[:2]}{idx}"
                entry_cfg_adapt["quorum_list"] = q_sub
                entry_cfg_adapt["setups"] = setups_sub
            ex = cfg.get("exit_policy")
            if ex:
                adaptive_exits[state] = build_exit_policy(ex["id"], ex.get("params"))
            adaptive_map[state] = entry_cfg_adapt
        adaptive_map["_exit_policies"] = adaptive_exits

    if ml_filter_obj is not None:
        c5_all = resample_to_5m(candles)
        ml_filter_obj.precompute(c5_all)

    static = _run_pipeline(candles, req, bias, setups_cfg, quorum_k, entry_window_min,
                           int(entry_cfg.get("lookback", 1)), static_exit, qty_shares,
                           regime_row,
                           adaptive_map if req.get("regime_gate") else None,
                           oracle, o_points, lot, capital, "static",
                           o_swings_detail, bias_tf_sec=bias_tf_sec,
                           ml_filter_obj=ml_filter_obj)

    if adaptive_map is not None:
        adaptive = _run_pipeline(candles, req, bias, setups_cfg, quorum_k, entry_window_min,
                                 int(entry_cfg.get("lookback", 1)), static_exit, qty_shares,
                                 regime_row, adaptive_map, oracle, o_points, lot, capital,
                                 "adaptive", bias_tf_sec=bias_tf_sec,
                                 ml_filter_obj=ml_filter_obj)
    else:
        adaptive = None

    return {
        "meta": {
            "engine_version": ENGINE_VERSION,
            "request_hash": request_hash(req),
            "figi": req.get("figi"), "interval": "1min", "days": days,
            "bars": len(candles),
            "from": candles[0].ts.isoformat(), "to": candles[-1].ts.isoformat(),
            "lot": lot, "capital": capital, "qty_shares": qty_shares,
            "params": {"bias": bias_cfg, "setups": setups_cfg, "quorum": quorum_k,
                       "entry": entry_cfg, "entry_window_min": entry_window_min,
                       "exit_policy": exit_cfg, "oracle": oracle_cfg,
                       "regime": regime_cfg, "adaptive": adaptive_cfg,
                       "use_all_setups": use_all, "drop_useless": drop_useless,
                       "ml_filter": bool(ml_filter_obj is not None)},
        },
        "regime": {"tf": regime_cfg.get("tf", "5min"), "timeline": timeline,
                   "bars": len(regime_row)},
        "oracle": ({"swings": len(oracle_swings), "trades": len(oracle["trades"]),
                    "gross": oracle["gross"], "net": oracle["net"],
                    "zones": [{"from": t["entry_ts"], "to": t["exit_ts"]} for t in oracle["trades"]]}
                   if oracle else {"swings": len(oracle_swings), "trades": None, "net": None}),
        "static": static,
        "adaptive": adaptive,
        "comparison": {
            "static_net": static["economic"]["net"],
            "adaptive_net": adaptive["economic"]["net"] if adaptive else None,
            "static_trades": static["economic"]["trades"],
            "adaptive_trades": adaptive["economic"]["trades"] if adaptive else None,
            "static_gross": static["economic"]["gross"],
            "adaptive_gross": adaptive["economic"]["gross"] if adaptive else None,
            "static_costs": static["economic"]["costs"],
            "adaptive_costs": adaptive["economic"]["costs"] if adaptive else None,
            "static_capture": static["capture_ratio"],
            "adaptive_capture": adaptive["capture_ratio"] if adaptive else None,
        },
        "config": {
            "strategy_id": "ensemble_main_v1",
            "config_hash": request_hash(req),
            "rule_based": True,
            "ml_enabled": False,
            "selection_period": "2026-06",
            "validation_period": "2026-07",
            "universe": (req.get("figis") or [req.get("figi")]),
            "params": {
                "bias": bias_cfg,
                "setups": [s["strategy_id"] for s in setups_cfg],
                "setups_tf": "5min",
                "quorum": quorum_k,
                "entry_tf": entry_cfg.get("tf", "1min"),
                "entry_lookback": entry_cfg.get("lookback", 1),
                "bias_mode": req.get("bias_mode", "veto"),
                "entry_session": req.get("entry_session", "all"),
                "carry_overnight": req.get("carry_overnight", True),
                "force_flat_at_session_end": req.get("force_flat_at_session_end", False),
                "same_side_reentry_cooldown_bars": req.get("same_side_reentry_cooldown_bars", 0),
                "exit_policy": exit_cfg,
                "cost_model": {"commission_rate": 0.0005, "slippage_bps": 2.0},
                "position_sizing": "capital_per_position",
                "oracle_threshold_pct": oracle_cfg.get("threshold_pct", 0.5),
            },
            "warning": "Backtest only — не торговый сигнал",
        },
    }
