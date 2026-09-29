"""Triple-Barrier разметка и «идеальные сделки» (zigzag) с допусками.

Идея (López de Prado, «Advances in Financial ML»): у каждого бара три барьера —
хорошая точка (+take×ATR), плохая (−stop×ATR) и время (max_bars). Метка = какой
барьер задет первым: +1 GOOD / −1 BAD / 0 NEUTRAL (центр/время). Барьеры в ATR,
т.е. допуски адаптивны к волатильности; «идеала» не ищем.

Zigzag-«идеальные сделки»: альтернирующие экстремумы с минимальным ходом
min_move×ATR — карта трендов и референс для оценки сигналов/выходов
(capture ratio, entry lag, giveback).

Ядро — чистые функции по серии (как индикаторы hub); хранение — БД
(scripts/label_backfill.py), live-резолвер — отдельным слоем позже.
ATR — канон IndicatorHub (проверен бит-в-бит против engine.indicators.atr).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.engine.indicatorhub import _atr
from app.engine.models import Candle


@dataclass(frozen=True)
class BarrierConfig:
    take_atr: float = 1.5   # верхний барьер («хорошая точка») в ATR
    stop_atr: float = 1.5   # нижний барьер («плохая точка») в ATR (калибровка симметричная)
    max_bars: int = 24      # вертикальный барьер (центр/время) в барах
    atr_period: int = 14


@dataclass(frozen=True)
class ZigzagConfig:
    min_move_atr: float = 3.0  # минимальный размах колена зигзага в ATR (калибровка IS/OOS)
    atr_period: int = 14


def triple_barrier(candles: Sequence[Candle], cfg: BarrierConfig | None = None) -> list[dict]:
    """Метки по каждому бару: +1 / −1 / 0, какой барьер задет первым.

    Правило конфликта в одном баре — stop-first (как в движке). Метка 0
    «дозревает» только когда есть полный горизонт max_bars баров вперёд:
    у хвоста данных mature=False (live-резолвер досчитает позже).
    """
    cfg = cfg or BarrierConfig()
    bars = list(candles)
    n = len(bars)
    atr = _atr(bars, cfg.atr_period)
    rows: list[dict] = []
    for i in range(n):
        a = atr[i]
        if a is None or a <= 0:
            continue
        c = bars[i]
        upper = c.close + cfg.take_atr * a
        lower = c.close - cfg.stop_atr * a
        horizon = i + cfg.max_bars
        label = 0
        t1 = min(horizon, n - 1)
        exit_px = bars[t1].close
        hit = False
        for j in range(i + 1, min(horizon, n - 1) + 1):
            up_hit = bars[j].high >= upper
            dn_hit = bars[j].low <= lower
            if dn_hit and up_hit:
                label, t1, exit_px, hit = -1, j, lower, True  # stop-first
                break
            if dn_hit:
                label, t1, exit_px, hit = -1, j, lower, True
                break
            if up_hit:
                label, t1, exit_px, hit = 1, j, upper, True
                break
        mature = hit or horizon <= n - 1
        rows.append({
            "index": i,
            "ts": c.ts.isoformat(),
            "label": label,
            "mature": mature,
            "t1_ts": bars[t1].ts.isoformat(),
            "t1_bars": t1 - i,
            "upper": round(upper, 6),
            "lower": round(lower, 6),
            "entry_px": c.close,
            "ret_atr": round((exit_px - c.close) / a, 4),
        })
    return rows


def zigzag_pivots(candles: Sequence[Candle], cfg: ZigzagConfig | None = None) -> list[dict]:
    """Альтернирующие экстремумы с минимальным ходом min_move×ATR.

    Возврат: [{"index","ts","px","kind": "L"|"H"}] по возрастанию времени.
    Подтверждение экстремума — ход против него на порог (допуск), а не
    «идеальное» касание.
    """
    cfg = cfg or ZigzagConfig()
    bars = list(candles)
    atr = _atr(bars, cfg.atr_period)
    start = next((i for i, a in enumerate(atr) if a), None)
    if start is None or start >= len(bars) - 1:
        return []
    thr = lambda i: cfg.min_move_atr * atr[i]  # noqa: E731

    pivots: list[dict] = []
    lo_i = hi_i = start
    lo_px = bars[start].low
    hi_px = bars[start].high
    trend = 0  # 0 поиск первого экстремума, 1 ищем вершину, −1 ищем впадину
    cur_i = cur_px = None
    for i in range(start + 1, len(bars)):
        a = atr[i]
        if not a:
            continue
        t = thr(i)
        hi, lo = bars[i].high, bars[i].low
        if trend == 0:
            if hi > hi_px:
                hi_i, hi_px = i, hi
            if lo < lo_px:
                lo_i, lo_px = i, lo
            if hi_px - lo_px >= t:
                if lo_i < hi_i:  # сначала впадина, затем рост
                    pivots.append({"index": lo_i, "ts": bars[lo_i].ts.isoformat(),
                                   "px": lo_px, "kind": "L"})
                    trend, cur_i, cur_px = 1, hi_i, hi_px
                    lo_i, lo_px = i, lo
                else:
                    pivots.append({"index": hi_i, "ts": bars[hi_i].ts.isoformat(),
                                   "px": hi_px, "kind": "H"})
                    trend, cur_i, cur_px = -1, lo_i, lo_px
                    hi_i, hi_px = i, hi
        elif trend == 1:
            if hi > cur_px:
                cur_i, cur_px = i, hi
                lo_i, lo_px = i, lo  # контр-трек сбрасывается от нового максимума
            elif lo < lo_px:
                lo_i, lo_px = i, lo
            if cur_px - lo_px >= t:
                pivots.append({"index": cur_i, "ts": bars[cur_i].ts.isoformat(),
                               "px": cur_px, "kind": "H"})
                trend, cur_i, cur_px = -1, lo_i, lo_px
                hi_i, hi_px = i, hi  # контр-трек для поиска впадины — с текущего бара
        else:
            if lo < cur_px:
                cur_i, cur_px = i, lo
                hi_i, hi_px = i, hi
            elif hi > hi_px:
                hi_i, hi_px = i, hi
            if hi_px - cur_px >= t:
                pivots.append({"index": cur_i, "ts": bars[cur_i].ts.isoformat(),
                               "px": cur_px, "kind": "L"})
                trend, cur_i, cur_px = 1, hi_i, hi_px
                lo_i, lo_px = i, lo  # контр-трек для поиска вершины — с текущего бара
    return pivots


def zigzag_trades(candles: Sequence[Candle], cfg: ZigzagConfig | None = None) -> list[dict]:
    """«Идеальные» сделки по зигзагу: L→H = LONG, H→L = SHORT."""
    cfg = cfg or ZigzagConfig()
    bars = list(candles)
    atr = _atr(bars, cfg.atr_period)
    pivots = zigzag_pivots(bars, cfg)
    trades: list[dict] = []
    for a, b in zip(pivots, pivots[1:]):
        if a["kind"] == b["kind"]:
            continue
        if b["index"] <= a["index"]:
            continue  # вырожденное колено (широкий бар): H и L на одном баре — не сделка
        # пары хронологические: вход — первый пивот (L→вверх или H→вниз)
        side = "LONG" if a["kind"] == "L" else "SHORT"
        entry, exit_ = a, b
        a_atr = atr[entry["index"]] or 0.0
        move = (exit_["px"] - entry["px"]) * (1.0 if side == "LONG" else -1.0)
        trades.append({
            "side": side,
            "entry_index": entry["index"], "entry_ts": entry["ts"], "entry_px": entry["px"],
            "exit_index": exit_["index"], "exit_ts": exit_["ts"], "exit_px": exit_["px"],
            "bars": b["index"] - a["index"],
            "ret_pct": round(move / entry["px"] * 100, 4) if entry["px"] else None,
            "ret_atr": round(abs(move) / a_atr, 3) if a_atr else None,
        })
    return trades
