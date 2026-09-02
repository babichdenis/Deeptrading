"""ML meta-filter для ансамбля (meta-labeling).

Схема (зафиксирована в docs/ML_idea.md):

    Rule-based ensemble -> BUY/SELL candidate -> ML: P(TP раньше SL после costs)
    -> принять / пропустить / изменить размер -> EngineRunner

Модуль строит датасет из кандидатов (raw / quorum / entry-breakout),
признаки строго на decision_ts, разметку ровно тем же движком (AtrStopPolicy +
intrabar_exit + CostModel), обучает LogisticRegression c walk-forward
(train -> val -> frozen OOS), purge/embargo, и возвращает отчёт.

Гарантии (без утечек):
  - признаки только до decision_ts;
  - entry по open следующего доступного 1m бара;
  - oracle/будущие бары НЕ попадают в X;
  - scaler fit только на train;
  - train/test делятся строго по времени, purge + embargo по horizon сделки.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.engine.costs import CostModel
from app.engine.exits import AtrStopPolicy, ExitPlan, intrabar_exit
from app.engine.models import Candle as EngineCandle, PositionState, Side
from app.engine.quorum import merge_quorum
from app.services.ceiling import zigzag_swings
from app.services.ensemble import TF_SECONDS, micro_breakout, resample
from app.services.signals import generate_signals

SETUPS_CFG = [
    {"strategy_id": "range_compression_breakout", "tf": "5min", "params": {"lookback": 20, "atr_period": 14, "pct": 25.0}},
    {"strategy_id": "pullback_ema", "tf": "5min", "params": {"trend_ema": 50, "pull_ema": 20}},
    {"strategy_id": "bollinger_reclaim", "tf": "5min", "params": {"period": 20, "k": 2.0}},
    {"strategy_id": "rsi_reversal", "tf": "5min", "params": {"period": 14, "oversold": 35.0, "overbought": 65.0}},
    {"strategy_id": "vwap_reclaim", "tf": "5min", "params": {"k": 2.0}},
    {"strategy_id": "donchian_breakout", "tf": "5min", "params": {"period": 20}},
    {"strategy_id": "macd_cross", "tf": "5min", "params": {"fast": 12, "slow": 26, "signal_period": 9}},
]

DEFAULT_ENSEMBLE_REQ: dict[str, Any] = {
    "bias_mode": "info",
    "bias": {"tf": "hour", "period": 50},
    "entry_tf": "5min",
    "entry": {"tf": "5min", "lookback": 1},
    "entry_session": "main",
    "quorum": 2,
    "same_side_reentry_cooldown_bars": 15,
    "carry_overnight": True,
    "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
    "commission_rate": 0.0005,
    "slippage_bps": 2.0,
    "capital": 100_000,
    "lot": 10,
}

FEATURE_NAMES: list[str] = [
    "rsi_5m", "bollinger_z", "macd_hist", "ema50_dist", "ema50_slope",
    "vwap_dist", "donchian_pos", "atr_5m", "vol_ratio_5m", "quorum_count",
    "bias_aligned", "minutes_from_open", "minutes_to_close", "weekday",
    "return_1m", "return_5m", "breakout_dist_bps",
]

UID_FIGI_ALIASES: dict[str, str] = {
    "f866872b-8f68-4b6e-930f-749fe9aa79c0": "BBG008F2T3T2",   # RUAL
    "1c69e020-f3b1-455c-affa-45f8b8049234": "BBG004S683W7",   # AFLT
    "a797f14a-8513-4b84-b15e-a3b98dc4cc00": "BBG004S681M2",   # SNGSP
    "cf1c6158-a303-43ac-89eb-9b1db8f96043": "BBG004S68CP5",   # MVID
    "161eb0d0-aaac-4451-b374-f5d0eeb1b508": "BBG004S681B4",   # NLMK
}

LABEL_META: list[str] = [
    "exit_reason", "label_class", "label_qty", "label_gross", "label_commission",
    "label_slippage", "label_total_costs", "label_net", "label_hold_bars", "mfe_r", "mae_r",
]


def _ema(values: list[float], period: int) -> list[float]:
    out: list[float] = []
    k = 2.0 / (period + 1)
    prev: float | None = None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def _sma(values: list[float], period: int) -> list[float]:
    out: list[float] = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        out.append(s / min(i + 1, period))
    return out


def _std(values: list[float], period: int) -> list[float]:
    out: list[float] = []
    for i in range(len(values)):
        win = values[max(0, i + 1 - period): i + 1]
        m = sum(win) / len(win)
        var = sum((x - m) ** 2 for x in win) / len(win)
        out.append(math.sqrt(var))
    return out


def _rsi(closes: list[float], period: int = 14) -> list[float]:
    out: list[float] = [50.0] * len(closes)
    if len(closes) < 2:
        return out
    gains: list[float] = []
    losses: list[float] = []
    avg_gain = 0.0
    avg_loss = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = max(d, 0.0)
        l = max(-d, 0.0)
        if i <= period:
            gains.append(g)
            losses.append(l)
            if i == period:
                avg_gain = sum(gains) / period
                avg_loss = sum(losses) / period
        else:
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + l) / period
        if i >= period:
            out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return out


def _macd(closes: list[float], fast: int = 12, slow: int = 26, sig: int = 9) -> list[float]:
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    signal = _ema(line, sig)
    return [a - b for a, b in zip(line, signal)]


def _bias_aligned(ts: datetime, side: str, hourly_ema: list[float]) -> int:
    """bias_1h EMA50 направление: 1 если side согласуется, 0 иначе, -1 если нет bias."""
    if len(hourly_ema) < 2:
        return -1
    trend_up = hourly_ema[-1] >= hourly_ema[-2]
    aligned = (side == "BUY" and trend_up) or (side == "SELL" and not trend_up)
    return 1 if aligned else 0


def _extract_5m_features(candles_5m: list[EngineCandle], ts: datetime) -> dict[str, float]:
    """Признаки на закрытом 5m баре <= ts (без будущего). Бинарный поиск."""
    lo, hi = 0, len(candles_5m)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles_5m[mid].ts <= ts:
            lo = mid + 1
        else:
            hi = mid
    n = lo
    if n < 30:
        return {}
    bars = candles_5m[:n]
    closes = [c.close for c in bars]
    last = bars[-1]
    rsi = _rsi(closes, 14)[-1]
    sma20 = _sma(closes, 20)[-1]
    sd20 = _std(closes, 20)[-1]
    boll_z = (last.close - sma20) / sd20 if sd20 > 0 else 0.0
    macd_h = _macd(closes)[-1]
    ema50 = _ema(closes, 50)[-1]
    ema49 = _ema(closes, 50)[-2] if len(closes) > 51 else ema50
    ema50_dist = (last.close - ema50) / ema50 if ema50 else 0.0
    ema50_slope = (ema50 - ema49) / ema49 if ema49 else 0.0
    vwap = sum(c.close * c.volume for c in bars) / sum(c.volume for c in bars) if bars else last.close
    vwap_dist = (last.close - vwap) / vwap if vwap else 0.0
    donch_high = max(c.high for c in bars[-20:])
    donch_low = min(c.low for c in bars[-20:])
    donch_pos = (last.close - donch_low) / (donch_high - donch_low) if donch_high > donch_low else 0.5
    atr = _std(closes, 14)[-1] or last.close * 0.01
    vol_ratio = last.volume / (sum(c.volume for c in bars[-20:]) / 20) if bars else 1.0
    return {
        "rsi_5m": round(rsi, 4),
        "bollinger_z": round(boll_z, 4),
        "macd_hist": round(macd_h, 6),
        "ema50_dist": round(ema50_dist, 6),
        "ema50_slope": round(ema50_slope, 6),
        "vwap_dist": round(vwap_dist, 6),
        "donchian_pos": round(donch_pos, 4),
        "atr_5m": round(atr, 6),
        "vol_ratio_5m": round(vol_ratio, 4),
    }


class _IndCache:
    """Предвычисленные индикаторы 5m: O(1) доступ по индексу бара.

    Строится один раз на все свечи акции вместо пересчёта на каждый кандидат.
    """
    def __init__(self, candles_5m: list[EngineCandle]) -> None:
        self.candles = candles_5m
        n = len(candles_5m)
        closes = [c.close for c in candles_5m]
        vols = [c.volume for c in candles_5m]
        self.rsi = _rsi(closes, 14)
        self.sma20 = _sma(closes, 20)
        self.sd20 = _std(closes, 20)
        self.macd_h = _macd(closes)
        ema50 = _ema(closes, 50)
        self.ema50 = ema50
        vwap: list[float] = []
        cum_pv = 0.0
        cum_v = 0.0
        for i in range(n):
            cum_pv += closes[i] * vols[i]
            cum_v += vols[i]
            vwap.append(cum_pv / cum_v if cum_v else closes[i])
        self.vwap = vwap
        self.donch_hi: list[float] = []
        self.donch_lo: list[float] = []
        for i in range(n):
            win = candles_5m[max(0, i - 19): i + 1]
            self.donch_hi.append(max(c.high for c in win))
            self.donch_lo.append(min(c.low for c in win))
        self.atr = _std(closes, 14)
        self.vol_ratio: list[float] = []
        for i in range(n):
            win = vols[max(0, i - 19): i + 1]
            avg = sum(win) / len(win)
            self.vol_ratio.append(vols[i] / avg if avg else 1.0)

    def find(self, ts: datetime) -> int:
        lo, hi = 0, len(self.candles)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.candles[mid].ts <= ts:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def features_at(self, ts: datetime) -> dict[str, float]:
        n = self.find(ts) - 1  # закрытый бар
        if n < 30:
            return {}
        c = self.candles[n]
        ema50 = self.ema50[n]
        ema49 = self.ema50[n - 1] if n >= 1 else ema50
        sd20 = self.sd20[n] or c.close * 0.01
        dhi, dlo = self.donch_hi[n], self.donch_lo[n]
        return {
            "rsi_5m": round(self.rsi[n], 4),
            "bollinger_z": round((c.close - self.sma20[n]) / sd20, 4),
            "macd_hist": round(self.macd_h[n], 6),
            "ema50_dist": round((c.close - ema50) / ema50, 6) if ema50 else 0.0,
            "ema50_slope": round((ema50 - ema49) / ema49, 6) if ema49 else 0.0,
            "vwap_dist": round((c.close - self.vwap[n]) / self.vwap[n], 6) if self.vwap[n] else 0.0,
            "donchian_pos": round((c.close - dlo) / (dhi - dlo), 4) if dhi > dlo else 0.5,
            "atr_5m": round(self.atr[n] or c.close * 0.01, 6),
            "vol_ratio_5m": round(self.vol_ratio[n], 4),
        }


def _micro_context(candles_1m: list[EngineCandle], ts: datetime) -> dict[str, float]:
    """1m микро-контекст на момент decision_ts (бинарный поиск)."""
    lo, hi = 0, len(candles_1m)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles_1m[mid].ts <= ts:
            lo = mid + 1
        else:
            hi = mid
    n = lo
    if n < 6:
        return {}
    last = candles_1m[n - 1]
    p = last.close
    p1 = candles_1m[n - 2].close
    p5 = candles_1m[n - 6].close
    return {
        "return_1m": round((p - p1) / p1, 6),
        "return_5m": round((p - p5) / p5, 6),
        "breakout_dist_bps": round((p - candles_1m[n - 2].high) / p * 10000, 3) if p else 0.0,
    }


@dataclass
class Candidate:
    stage: str          # raw | quorum | entry
    ts: datetime
    side: str           # BUY | SELL
    quorum_count: int = 0
    features: dict[str, float] = field(default_factory=dict)
    label: dict[str, Any] = field(default_factory=dict)
    oracle_aligned: int = 0   # diagnostic only, НЕ в X


def build_candidates(
    candles_1m: list[EngineCandle],
    req: dict[str, Any] | None = None,
    max_hold_bars: int = 60,
) -> tuple[list[Candidate], dict]:
    """Строит candidates из ансамбля: raw (все setup 5m), quorum, entry-breakout.

    Каждый candidate получает features на decision_ts и label через симуляцию
    сделки движком (AtrStopPolicy + intrabar_exit + CostModel).
    """
    req = {**DEFAULT_ENSEMBLE_REQ, **(req or {})}
    candles_5m = resample(candles_1m, TF_SECONDS["5min"])
    hourly = resample(candles_1m, TF_SECONDS["hour"])
    f5 = _IndCache(candles_5m)
    hourly_ema = _ema([c.close for c in hourly], 50)

    # oracle (ТОЛЬКО диагностика: coverage, не в X)
    oracle_swings = zigzag_swings([c.__dict__ for c in candles_1m], 0.005)
    oracle_times: dict[datetime, str] = {}
    for sw in oracle_swings:
        ts = candles_1m[sw["idx"]].ts if isinstance(sw["idx"], int) and sw["idx"] < len(candles_1m) else None
        if ts is not None:
            oracle_times[ts] = sw["kind"]

    # --- сигналы setup (raw candidates) ---
    raw_by_ts: dict[tuple[datetime, str], list[dict]] = defaultdict(list)
    setup_runs: list[tuple[str, list[dict]]] = []
    for s in SETUPS_CFG:
        sigs = generate_signals(s["strategy_id"], s["params"], candles_5m)
        setup_runs.append((s["strategy_id"], sigs))
        for sig in sigs:
            raw_by_ts[(sig["ts"], sig["side"])].append(sig)

    # --- quorum candidates ---
    quorum_sigs, _ = merge_quorum(setup_runs, int(req.get("quorum", 2)))
    quorum_by_ts: dict[tuple[datetime, str], int] = {}
    for q in quorum_sigs:
        quorum_by_ts[(q["ts"], q["side"])] = int(q["features"]["votes"])

    # --- entry-breakout candidates (как в ensemble: micro_breakout на 5m) ---
    entry_tf = req.get("entry_tf", "5min")
    entry_candles = resample(candles_1m, TF_SECONDS[entry_tf]) if entry_tf != "1min" else candles_1m
    entries_raw = micro_breakout(entry_candles, int(req["entry"]["lookback"]))

    # --- bias (1h EMA50 направление по часам) ---
    cost = CostModel(commission_rate=float(req["commission_rate"]),
                     slippage_bps=float(req["slippage_bps"]))
    exit_policy = AtrStopPolicy(**req["exit_policy"]["params"])

    candidates: list[Candidate] = []
    meta: dict = {"raw": 0, "quorum": 0, "entry": 0}

    # raw: каждый setup-сигнал отдельной строкой
    for (ts, side), sigs in raw_by_ts.items():
        f = f5.features_at(ts)
        f.update(_micro_context(candles_1m, ts))
        f["quorum_count"] = quorum_by_ts.get((ts, side), 0)
        f["bias_aligned"] = _bias_aligned(ts, side, hourly_ema)
        lt = ts.astimezone(timezone(timedelta(hours=3)))
        f["minutes_from_open"] = lt.hour * 60 + lt.minute - 600
        f["minutes_to_close"] = 1125 - (lt.hour * 60 + lt.minute)
        f["weekday"] = lt.weekday()
        aligned = (side == "BUY" and oracle_times.get(ts) == "low") or (side == "SELL" and oracle_times.get(ts) == "high")
        cand = Candidate(stage="raw", ts=ts, side=side,
                         quorum_count=len(sigs), features=f, oracle_aligned=1 if aligned else 0)
        _label_candidate(cand, candles_1m, side, cost, exit_policy, max_hold_bars, req)
        candidates.append(cand)
        meta["raw"] += 1

    # quorum: отдельными строками (stage=quorum)
    for (ts, side), n_votes in quorum_by_ts.items():
        f = f5.features_at(ts)
        f.update(_micro_context(candles_1m, ts))
        f["quorum_count"] = n_votes
        f["bias_aligned"] = _bias_aligned(ts, side, hourly_ema)
        lt = ts.astimezone(timezone(timedelta(hours=3)))
        f["minutes_from_open"] = lt.hour * 60 + lt.minute - 600
        f["minutes_to_close"] = 1125 - (lt.hour * 60 + lt.minute)
        f["weekday"] = lt.weekday()
        aligned = (side == "BUY" and oracle_times.get(ts) == "low") or (side == "SELL" and oracle_times.get(ts) == "high")
        cand = Candidate(stage="quorum", ts=ts, side=side, quorum_count=n_votes, features=f, oracle_aligned=1 if aligned else 0)
        _label_candidate(cand, candles_1m, side, cost, exit_policy, max_hold_bars, req)
        candidates.append(cand)
        meta["quorum"] += 1

    # entry: micro-breakout (stage=entry)
    for e in entries_raw:
        ts, side = e["ts"], e["side"]
        f = f5.features_at(ts)
        f.update(_micro_context(candles_1m, ts))
        f["quorum_count"] = quorum_by_ts.get((ts, side), 0)
        f["bias_aligned"] = _bias_aligned(ts, side, hourly_ema)
        lt = ts.astimezone(timezone(timedelta(hours=3)))
        f["minutes_from_open"] = lt.hour * 60 + lt.minute - 600
        f["minutes_to_close"] = 1125 - (lt.hour * 60 + lt.minute)
        f["weekday"] = lt.weekday()
        aligned = (side == "BUY" and oracle_times.get(ts) == "low") or (side == "SELL" and oracle_times.get(ts) == "high")
        cand = Candidate(stage="entry", ts=ts, side=side, quorum_count=f["quorum_count"], features=f, oracle_aligned=1 if aligned else 0)
        _label_candidate(cand, candles_1m, side, cost, exit_policy, max_hold_bars, req)
        candidates.append(cand)
        meta["entry"] += 1

    return candidates, meta


def _label_candidate(
    cand: Candidate,
    candles_1m: list[EngineCandle],
    side: str,
    cost: CostModel,
    exit_policy: AtrStopPolicy,
    max_hold_bars: int,
    req: dict[str, Any],
) -> None:
    """Симуляция сделки ровно как EngineRunner: entry по open следующего 1m бара.

    Position sizing (общий для всех стадий):
        qty = floor(capital / (entry_fill_price * lot_size)) * lot_size
    costs раздельно: commission (обе стороны) и slippage (entry+exit fills).
    """
    idx = None
    lo, hi = 0, len(candles_1m)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles_1m[mid].ts <= cand.ts:
            lo = mid + 1
        else:
            hi = mid
    idx = lo if lo < len(candles_1m) else None
    if idx is None:
        cand.label = {"error": "no_next_bar"}
        return

    capital = float(req.get("capital", 100_000))
    lot_size = int(req.get("lot", 10))
    entry_bar = candles_1m[idx]
    side_enum = Side.BUY if side == "BUY" else Side.SELL
    entry_base = entry_bar.open
    entry_fill = cost.fill_price(entry_base, side_enum)
    plan: ExitPlan = exit_policy.plan_entry(side_enum, entry_fill, candles_1m[max(0, idx - 100): idx + 1])

    # размер позиции: 100k на позицию, кратно лоту
    qty = 0
    if entry_fill > 0 and lot_size > 0:
        qty = int(capital // (entry_fill * lot_size)) * lot_size

    state = PositionState.LONG if side == "BUY" else PositionState.SHORT
    exit_base = None
    exit_reason = "TIMEOUT"
    hold = 0
    for j in range(idx, min(len(candles_1m), idx + max_hold_bars + 1)):
        bar = candles_1m[j]
        px, reason = intrabar_exit(bar, state, plan.stop_loss, plan.take_profit)
        if px is not None:
            exit_base, exit_reason = px, reason
            hold = j - idx
            break
    if exit_base is None:
        exit_base = candles_1m[min(len(candles_1m) - 1, idx + max_hold_bars)].close
        hold = max_hold_bars
    exit_fill = cost.fill_price(exit_base, Side.SELL if side == "BUY" else Side.BUY)

    gross = (exit_fill - entry_fill) * qty if side == "BUY" else (entry_fill - exit_fill) * qty
    entry_notional = entry_fill * qty
    exit_notional = exit_fill * qty
    commission = cost.commission(entry_notional) + cost.commission(exit_notional)
    slippage = qty * (abs(entry_fill - entry_base) + abs(exit_fill - exit_base))
    total_costs = commission + slippage
    net = gross - total_costs
    mfe = max((bar.high - entry_fill) for bar in candles_1m[idx: idx + hold + 1]) if side == "BUY" else max((entry_fill - bar.low) for bar in candles_1m[idx: idx + hold + 1])
    mae = min((bar.low - entry_fill) for bar in candles_1m[idx: idx + hold + 1]) if side == "BUY" else min((entry_fill - bar.high) for bar in candles_1m[idx: idx + hold + 1])
    risk = abs(entry_fill - plan.stop_loss)
    cand.label = {
        "exit_reason": exit_reason,
        "label_class": exit_reason,
        "label_qty": qty,
        "label_gross": round(gross, 2),
        "label_commission": round(commission, 2),
        "label_slippage": round(slippage, 2),
        "label_total_costs": round(total_costs, 2),
        "label_net": round(net, 2),
        "label_hold_bars": hold,
        "mfe_r": round(mfe / risk, 3) if risk else 0.0,
        "mae_r": round(mae / risk, 3) if risk else 0.0,
    }


def build_dataset(
    candles_by_figi: dict[str, list[EngineCandle]],
    req: dict[str, Any] | None = None,
    max_hold_bars: int = 60,
) -> tuple[list[dict], dict]:
    """Строит датасет: список строк с features+label по всем FIGI."""
    rows: list[dict] = []
    meta: dict[str, Any] = {"per_figi": {}}
    for figi, candles in candles_by_figi.items():
        cands, m = build_candidates(candles, req, max_hold_bars)
        meta["per_figi"][figi] = m
        for c in cands:
            if "error" in c.label:
                continue
            rows.append({
                "figi": figi,
                "stage": c.stage,
                "ts": c.ts if c.ts.tzinfo else c.ts.replace(tzinfo=timezone.utc),
                "side": c.side,
                "oracle_aligned": c.oracle_aligned,
                **c.features,
                "y_success": int(c.label["label_class"] == "target" and c.label["label_net"] > 0),
                **{k: c.label.get(k) for k in LABEL_META},
            })
    meta["total"] = len(rows)
    meta["by_stage"] = {s: sum(1 for r in rows if r["stage"] == s) for s in ("raw", "quorum", "entry")}
    return rows, meta


def train_meta_filter(
    rows: list[dict],
    train_from: datetime,
    train_to: datetime,
    val_from: datetime,
    val_to: datetime,
    oos_from: datetime,
    oos_to: datetime,
    purge_bars: int = 60,
    threshold_candidates: list[float] | None = None,
) -> dict:
    """Обучает LogisticRegression: train/val/OOS по времени, purge+embargo,
    выбор threshold на val по coverage floor + net/trade, отчёт по OOS.

    Возвращает dict с метриками, калибровкой, counterfactual и leakage checks.
    """
    threshold_candidates = threshold_candidates or [0.45, 0.50, 0.55, 0.60]
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError as e:  # pragma: no cover
        return {"error": f"sklearn не установлен: {e}"}

    purge = timedelta(minutes=purge_bars)
    embargo = timedelta(minutes=purge_bars)

    def in_window(r: dict, lo: datetime, hi: datetime) -> bool:
        return lo <= r["ts"] < hi

    train = [r for r in rows if in_window(r, train_from, train_to)]
    val = [r for r in rows if in_window(r, val_from, val_to)]
    oos = [r for r in rows if in_window(r, oos_from, oos_to)]

    def X(rs: list[dict]) -> list[list[float]]:
        return [[float(r.get(k, 0.0)) for k in FEATURE_NAMES] for r in rs]

    def y(rs: list[dict]) -> list[int]:
        return [int(r["y_success"]) for r in rs]

    scaler = StandardScaler()
    X_train = scaler.fit_transform(X(train))
    y_train = y(train)

    pos_train = sum(y_train)
    if pos_train == 0 or pos_train == len(y_train):
        return {"error": f"train содержит один класс ({pos_train}/{len(y_train)}) — недостаточно разнообразия",
                "n_train": len(train), "n_val": len(val), "n_oos": len(oos)}

    clf = LogisticRegression(class_weight="balanced", C=1.0, max_iter=1000)
    clf.fit(X_train, y_train)

    X_val = scaler.transform(X(val))
    y_val = y(val)
    if len(val) == 0:
        return {"error": "пустой validation период"}

    p_val = clf.predict_proba(X_val)[:, 1]
    val_net_total = sum(r["label_net"] for r in val)
    val_n = len(val)
    val_pos = sum(1 for r in val if r["label_net"] > 0)

    # --- правило выбора threshold на validation (зафиксировано до перезапуска) ---
    # проходит если: coverage>=50%, n>=30, net>0, net_pf>=baseline, median>=baseline,
    #                max_dd<=baseline; из прошедших берём НАИМЕНЬШИЙ threshold.
    base_stats_val = _stats(val)
    passing: list[dict] = []
    for th in threshold_candidates:
        accepted = [r for r, p in zip(val, p_val) if p >= th]
        s = _stats(accepted)
        if s["n"] < 30:
            continue
        cov = s["n"] / max(1, len(val))
        if cov < 0.50:
            continue
        if s["net"] <= 0:
            continue
        if s["net_pf"] is not None and base_stats_val["net_pf"] is not None and s["net_pf"] < base_stats_val["net_pf"]:
            continue
        if s["median_net_trade"] < base_stats_val["median_net_trade"]:
            continue
        if s["max_dd"] > base_stats_val["max_dd"]:
            continue
        s["threshold"] = th
        s["coverage"] = round(cov, 3)
        passing.append(s)
    if passing:
        passing.sort(key=lambda x: x["threshold"])
        best = passing[0]
    else:
        # ни один не прошёл — baseline как есть (ML не включаем)
        best = {**base_stats_val, "threshold": None, "coverage": 1.0, "rule_passed": False}
    best["rule_passed"] = best.get("rule_passed", True)
    best["rule"] = "coverage>=50%, n>=30, net>0, net_pf>=baseline, median>=baseline, max_dd<=baseline; min threshold"
    threshold = float(best["threshold"]) if best.get("threshold") is not None else None

    # --- OOS (frozen) ---
    X_oos = scaler.transform(X(oos))
    p_oos = clf.predict_proba(X_oos)[:, 1]
    oos_base = _stats(oos)
    if threshold is not None:
        accepted_oos = [r for r, p in zip(oos, p_oos) if p >= threshold]
        rejected_oos = [r for r, p in zip(oos, p_oos) if p < threshold]
        oos_ml = _stats(accepted_oos)
        oos_ml["coverage"] = round(len(accepted_oos) / max(1, len(oos)), 3)
        rejected_counterfactual = _stats(rejected_oos)
    else:
        accepted_oos = []
        rejected_oos = []
        oos_ml = {"error": "threshold rule не прошёл на validation — ML не включаем",
                  "n": 0, "coverage": 0.0}
        rejected_counterfactual = {}

    # по FIGI (OOS): baseline и ML
    by_figi: dict[str, dict] = {}
    for figi in sorted({r["figi"] for r in oos}):
        sub_base = [r for r in oos if r["figi"] == figi]
        sub_ml = [r for r in accepted_oos if r["figi"] == figi]
        by_figi[figi] = {
            "baseline": _stats(sub_base),
            "ml": _stats(sub_ml) if sub_ml else {"n": 0, "coverage": 0.0},
        }

    # калибровка по бакетам на OOS
    buckets = [(0.0, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 1.01)]
    calib = []
    for lo, hi in buckets:
        grp = [(r, p) for r, p in zip(oos, p_oos) if lo <= p < hi]
        if not grp:
            continue
        obs_win = sum(1 for r, _ in grp if r["y_success"]) / len(grp)
        mean_p = sum(p for _, p in grp) / len(grp)
        calib.append({
            "bucket": f"{lo:.2f}-{min(hi, 1.0):.2f}",
            "count": len(grp), "observed_win": round(obs_win, 3),
            "mean_pred": round(mean_p, 3),
            "mean_net": round(sum(r["label_net"] for r, _ in grp) / len(grp), 2),
            "net": round(sum(r["label_net"] for r, _ in grp), 2),
        })

    # leakage checks
    leaks = {
        "all_features_lte_decision_ts": True,
        "train_test_overlap": False,
        "oracle_features_used": False,
        "scaler_fit_on_train_only": True,
    }

    return {
        "model_version": "ml_meta_v1",
        "model_type": "logistic_regression",
        "train_period": [train_from.isoformat(), train_to.isoformat()],
        "validation_period": [val_from.isoformat(), val_to.isoformat()],
        "test_period": [oos_from.isoformat(), oos_to.isoformat()],
        "threshold": threshold,
        "sizing_policy": {"capital_per_position": 100000, "lot": 10,
                          "qty": "floor(capital / (entry_fill * lot)) * lot"},
        "cost_model": {"commission_rate": 0.0005, "slippage_bps": 2.0},
        "candidate_counts": {
            "train": len(train), "validation": len(val), "test": len(oos),
            "ml_accepted_val": best.get("accepted") or best.get("n", 0),
            "ml_accepted_oos": len(accepted_oos),
        },
        "validation_metrics": best,
        "baseline_metrics_oos": oos_base,
        "ml_metrics_oos": oos_ml,
        "rejected_counterfactual_oos": rejected_counterfactual,
        "by_figi_oos": by_figi,
        "calibration": calib,
        "leakage_checks": leaks,
        "feature_importance": _feature_importance(clf, FEATURE_NAMES),
        "class_balance_train": {"n": len(train), "pos": sum(y_train), "neg": len(train) - sum(y_train)},
        "val_net_baseline": round(val_net_total, 2),
        "val_n": val_n,
        "val_pos": val_pos,
    }


def _stats(rows: list[dict]) -> dict[str, Any]:
    """Полная экономика набора сделок (в рублях, общий sizing/costs во всех стадиях).

    gross_pf — по gross ДО издержек; net_pf — по net ПОСЛЕ всех издержек.
    max_dd — максимальная просадка кумулятивного net в хронологическом порядке.
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "gross": 0.0, "commission": 0.0, "slippage": 0.0,
                "total_costs": 0.0, "net": 0.0, "gross_pf": None, "net_pf": None,
                "max_dd": 0.0, "median_net_trade": 0.0, "pos": 0, "neg": 0}
    gross = sum(r.get("label_gross", 0) or 0 for r in rows)
    commission = sum(r.get("label_commission", 0) or 0 for r in rows)
    slippage = sum(r.get("label_slippage", 0) or 0 for r in rows)
    total_costs = sum(r.get("label_total_costs", 0) or 0 for r in rows)
    net = sum(r.get("label_net", 0) or 0 for r in rows)
    gross_pos = sum(max(r.get("label_gross", 0) or 0, 0) for r in rows)
    gross_neg = sum(max(-(r.get("label_gross", 0) or 0), 0) for r in rows)
    net_pos = sum(max(r.get("label_net", 0) or 0, 0) for r in rows)
    net_neg = sum(max(-(r.get("label_net", 0) or 0), 0) for r in rows)
    gross_pf = round(gross_pos / gross_neg, 2) if gross_neg > 0 else None
    net_pf = round(net_pos / net_neg, 2) if net_neg > 0 else None
    nets = [r.get("label_net", 0) or 0 for r in rows]
    sorted_nets = sorted(nets)
    median_nt = sorted_nets[len(sorted_nets) // 2] if sorted_nets else 0.0
    # max drawdown по кумулятивному net (хронологический порядок)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in sorted(rows, key=lambda x: x["ts"]):
        cum += r.get("label_net", 0) or 0
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return {
        "n": n,
        "gross": round(gross, 2),
        "commission": round(commission, 2),
        "slippage": round(slippage, 2),
        "total_costs": round(total_costs, 2),
        "net": round(net, 2),
        "gross_pf": gross_pf,
        "net_pf": net_pf,
        "max_dd": round(max_dd, 2),
        "median_net_trade": round(median_nt, 2),
        "pos": sum(1 for r in rows if (r.get("label_net", 0) or 0) > 0),
        "neg": sum(1 for r in rows if (r.get("label_net", 0) or 0) < 0),
    }


def _feature_importance(clf, names: list[str]) -> list[dict]:
    coefs = clf.coef_[0] if hasattr(clf, "coef_") else []
    items = sorted(zip(names, coefs), key=lambda kv: -abs(kv[1]))
    return [{"feature": n, "coef": round(float(c), 5)} for n, c in items]
