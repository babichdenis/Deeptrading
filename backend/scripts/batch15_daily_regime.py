#!/usr/bin/env python3
"""BATCH-15 / RESEARCH-CORE-V2 (B2): дневной regime-overlay для 15 внутридневных сигналов.

Фиксированные признаки считаются НА ПРЕДЫДУЩЕМ завершённом торговом дне (as_of) —
никогда на будущем или текущем незакрытом дне. Сигналы15 не меняются; это overlay,
который разрешает/запрещает входы внутридневных кандидатов по дневному режиму.

Признаки (на as_of = предыдущий завершённый день):
  returns N сессий:   ret[N] = C[as_of] / C[as_of-N] - 1      (N = 20,60,120,250)
  SMA N:              sma[N] = mean(close[as_of-N+1 .. as_of]) (N = 20,60,120,250)
  realized vol N:     vol[N] = population stdev дневных доходностей за N дней (N=20,60)
  ER60:               abs(C[t]-C[t-60]) / sum|ΔC| (i=t-59..t); знаменатель — 60 изменений
                       => нужно 61 полная цена; при нуле знаменателя ER=0;
                       недостаточная история => er60_unknown=True.

Недостаточная/невалидная история для конкретного признака => признак UNKNOWN
(sufficient[имя]=False). Режим UP/DOWN/MIXED требует SMA60 и ret60; иначе UNKNOWN.
Corporate-action разрывы: флаг ca_gap (описательный, НЕ корректируем догадками).

Режим (fixed): UP если close_prev>SMA60 и ret60>0; DOWN если close_prev<SMA60 и
ret60<0; иначе MIXED. Длительности 120/250 — описательные, не доп. фильтры.

Comparison arms (для всех15): A — без regime-фильтра; B — long только UP / short только
DOWN, MIXED->no_trade, UNKNOWN->unknown_filter; C — как B, но среди eligible акций
ежедневно top10 по ER60 (ties по ticker, ranking только на prev close); <10 — брать
доступные, не добирать по будущему PnL.

Это исследуемая гипотеза, не утверждение предсказуемости. Окна/topN/thresholds не
оптимизируются по увиденной прибыли.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

FLOAT_EPS = 1e-12
CA_GAP_RETURN = 0.30  # описательный порог разрыва CA (флаг, не корректируем)
SMA_WINDOWS = (20, 60, 120, 250)
RET_WINDOWS = (20, 60, 120, 250)
VOL_WINDOWS = (20, 60)


@dataclass
class DailyBar:
    d: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class DailyFeatures:
    as_of: date
    close_prev: Optional[float] = None
    sma: Dict[int, float] = field(default_factory=dict)
    ret: Dict[int, float] = field(default_factory=dict)
    vol: Dict[int, float] = field(default_factory=dict)
    er60: Optional[float] = None
    er60_unknown: bool = False
    ca_gap: bool = False
    sufficient: Dict[str, bool] = field(default_factory=dict)


def _stdev(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def compute_features(bars: List[DailyBar], as_of: date) -> Tuple[DailyFeatures, int]:
    """Фичи на as_of (должен быть последним баром в bars). idx — позиция as_of.

    Возвращает (DailyFeatures, idx). Бары — хронологически. close as_of = последний
    закрытый день; будущие/незакрытые не используются.
    """
    closes = [b.close for b in bars]
    idx = None
    for i, b in enumerate(bars):
        if b.d == as_of:
            idx = i
    if idx is None:
        raise ValueError(f"as_of {as_of.isoformat()} отсутствует в bars")
    feats = DailyFeatures(as_of=as_of)
    feats.close_prev = closes[idx]

    for N in RET_WINDOWS:
        ok = idx >= N
        feats.sufficient[f"ret{N}"] = ok
        if ok:
            feats.ret[N] = closes[idx] / closes[idx - N] - 1.0

    for N in SMA_WINDOWS:
        ok = idx >= N - 1
        feats.sufficient[f"sma{N}"] = ok
        if ok:
            feats.sma[N] = sum(closes[idx - N + 1:idx + 1]) / N

    for N in VOL_WINDOWS:
        ok = idx >= N
        feats.sufficient[f"vol{N}"] = ok
        if ok:
            chg = [closes[i] / closes[i - 1] - 1.0
                   for i in range(idx - N + 1, idx + 1)]
            feats.vol[N] = _stdev(chg)

    if idx >= 60:
        denom = sum(abs(closes[i] - closes[i - 1])
                     for i in range(idx - 59, idx + 1))
        num = abs(closes[idx] - closes[idx - 60])
        feats.er60 = 0.0 if denom <= 0 else num / denom
        feats.er60_unknown = denom <= 0
        feats.sufficient["er60"] = denom > 0
    else:
        feats.er60_unknown = True
        feats.sufficient["er60"] = False

    if idx >= 1:
        r = abs(closes[idx] / closes[idx - 1] - 1.0)
        feats.ca_gap = r > CA_GAP_RETURN
    return feats, idx


def regime_for(feats: DailyFeatures) -> str:
    """UP / DOWN / MIXED / UNKNOWN (fixed правило)."""
    if not feats.sufficient.get("sma60") or not feats.sufficient.get("ret60"):
        return "UNKNOWN"
    sma60 = feats.sma[60]
    r60 = feats.ret[60]
    cp = feats.close_prev
    if cp > sma60 + FLOAT_EPS and r60 > 0:
        return "UP"
    if cp < sma60 - FLOAT_EPS and r60 < 0:
        return "DOWN"
    return "MIXED"


def apply_arm(side: str, regime: str, arm: str, er60_rank_ok: bool = True) -> bool:
    """Разрешён ли вход со стороной side при regime и arm.

    arm A — без фильтра (всегда True). arm B — long только UP, short только DOWN,
    MIXED/UNKNOWN -> False. arm C — как B плюс er60_rank_ok (top10 по ER60).
    side: 'long' | 'short'.
    """
    if arm not in ("A", "B", "C"):
        raise ValueError(f"unknown arm {arm!r}")
    if arm == "A":
        return True
    if regime == "UNKNOWN":
        return False
    if arm == "B":
        if side == "long" and regime == "UP":
            return True
        if side == "short" and regime == "DOWN":
            return True
        return False
    # arm C
    if side == "long" and regime == "UP" and er60_rank_ok:
        return True
    if side == "short" and regime == "DOWN" and er60_rank_ok:
        return True
    return False


def select_arm_c(regimes: Dict[str, str], er60: Dict[str, float],
                 eligible: Dict[str, bool], top_n: int = 10) -> Dict[str, bool]:
    """Для arm C: среди eligible (coverage/liquidity) тикеров отобрать top_n по ER60
    (ranking только на prev close), ties по ticker. Возвращает {ticker: selected}.

    Тикеры вне top_n среди eligible => False; если eligible < top_n — берём доступные
    (не добираем по будущему PnL). UNKNOWN-режимные исключаются (как в arm B/C)."""
    pool = [t for t in eligible if eligible[t] and regimes.get(t) in ("UP", "DOWN")]
    ranked = sorted(pool, key=lambda t: (-(er60.get(t, 0.0) or 0.0), t))
    chosen = set(ranked[:top_n])
    return {t: (t in chosen) for t in eligible}
