"""Виртуальный (info) трейл в карточке сделки + MAE до пика в %.

Регрессии (владелец, 2026-10-02), PLZL 2026-09-04:
  1. «Трейл (инфо): вкл, закрыл бы 995.80 в 11:40, забрал бы +0.00₽» — трейл
     срабатывал НА БАРЕ СВОЕГО РОЖДЕНИЯ: стоп, рождённый из high/low этого же
     бара, лежит внутри диапазона бара, и open <= stop выполнялось всегда.
     Теперь проверка нового стопа пропускает бар рождения (как для _prev).
  2. Утечка состояния: _trail_info жил до _st_close, а часть путей закрытия его
     не вычищала → новая позиция по тому же figi наследовала activated/hit_ts
     ЧУЖОЙ сделки. Теперь трейл обнуляется на открытии позиции.
  3. MAE до пика показывался только в ATR — добавили % от цены входа.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot.runtime import PaperBotRuntime  # noqa: E402


class _Bar:
    def __init__(self, ts, o, h, l, c):
        self.ts = ts
        self.open = o
        self.high = h
        self.low = l
        self.close = c


def _rt(trail_info_mult=0.5, distance_atr=1.0, atr=1.0) -> PaperBotRuntime:
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt._trail_info = {}
    rt._trail_active = {}
    rt._trail_stop = {}
    rt._peak_pnl = {}
    rt._entry_regime = {}
    rt.logs = []
    rt._log = lambda msg: rt.logs.append(msg)
    rt.atr_now = lambda figi: atr
    rt.config = SimpleNamespace(
        trail_info_activation_comm_mult=trail_info_mult,
        trail_activation_comm_mult=None,
        atr_period=14,
        initial_sl_atr=4.0,
        trail_distance_atr=distance_atr,
        trail_compress_r=0.0,
        trail_min_factor=0.3,
        trail_min_atr=0.0,
        trail_vol_boost=0.0,
        commission_rate=0.0005,
    )
    return rt


def _step(rt, bar, *, side="BUY", entry=100.0, qty=1.0, comm=0.1, stop=None):
    """Прогоняет кусок _step_exit: пик P&L + блок виртуального трейла."""
    from app.bot.runtime import PositionState

    rt._trail_active.setdefault("BBG1", False)
    rt._trail_stop.setdefault("BBG1", stop if stop is not None else entry - 4 * rt.atr_now("BBG1"))
    rt._peak_pnl.setdefault("BBG1", {"pnl": 0.0, "ts": bar.ts, "price": entry,
                                     "atr_abs": None, "atr_pct": None, "mae": 0.0})
    state = PositionState.LONG if side == "BUY" else PositionState.SHORT

    peak_px = bar.high if state == PositionState.LONG else bar.low
    cur_pnl = (peak_px - entry) * qty if state == PositionState.LONG else (entry - peak_px) * qty
    atr_v = rt.atr_now("BBG1")
    adv_px = (entry - bar.low) if state == PositionState.LONG else (bar.high - entry)
    mae_cur = (adv_px / atr_v) if (adv_px > 0 and atr_v) else 0.0
    peak = rt._peak_pnl.get("BBG1")
    peak["mae_cur"] = max(float(peak.get("mae_cur", 0.0)), mae_cur)
    peak["mae_cur_pct"] = max(float(peak.get("mae_cur_pct", 0.0)),
                              (adv_px / entry * 100.0) if (entry and adv_px > 0) else 0.0)
    if adv_px > float(peak.get("mae_dist", 0.0)):
        peak["mae_dist"] = float(adv_px)
        peak["mae_px"] = float(bar.low if state == PositionState.LONG else bar.high)
        peak["mae_ts"] = bar.ts.isoformat()
    if cur_pnl > float(peak.get("pnl", -1e18)):
        peak.update({"pnl": round(cur_pnl, 2), "ts": bar.ts.isoformat(), "price": float(peak_px),
                     "atr_pct": round(float(atr_v / entry * 100), 2),
                     "atr_abs": round(float(atr_v), 4),
                     "mae_atr": round(float(peak["mae_cur"]), 2),
                     "mae_pct": round(float(peak["mae_cur_pct"]), 2)})
    return state


def _buffer(n=20, start=99.0, rng=1.0):
    """Буфер с устойчивым ATR ≈ rng — policy считает ATR по нему сам."""
    out = []
    ts = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    for i in range(n):
        mid = start + 0.3 * i
        out.append(_Bar(ts + timedelta(minutes=10 * i), mid, mid + rng / 2, mid - rng / 2, mid))
    return out


def _trail_block(rt, bar, state, entry=100.0, qty=1.0, comm=0.1, stop=None, buf=None):
    """Ровно та логика info-трейла, что в runtime (с фиксом бара рождения)."""
    from app.engine.exits import AtrStopPolicy
    from app.engine.exits import intrabar_exit as _ibe
    from app.engine.models import Side

    figi = "BBG1"
    ti = rt._trail_info.get(figi)
    if ti is None:
        ti = {"active": False, "act_track": rt._trail_stop.get(figi)}
        rt._trail_info[figi] = ti
    ti_cfg = rt.config.trail_info_activation_comm_mult or rt.config.trail_activation_comm_mult
    if ti_cfg is None:
        return ti
    pol = AtrStopPolicy(period=14, multiplier=4.0, trail_activation_comm_mult=ti_cfg,
                        trail_distance_r=rt.config.trail_distance_atr, trail_compress_r=0.0,
                        trail_min_factor=0.3, trail_min_atr=0.0, trail_vol_boost=0.0)
    side = Side.BUY if state.name == "LONG" else Side.SELL
    bars = list(buf) if buf else [bar]
    born = False
    if not ti.get("active"):
        if pol.trailing_activated(side, entry, qty, comm, bars):
            ti["active"] = True
            ti["act_track"] = rt._trail_stop.get(figi)
            born = True
    if ti.get("active"):
        prev = ti.get("act_track")
        if prev is not None and not born and not ti.get("hit_ts"):
            px, reason = _ibe(bar, state, float(prev), None, close_based=True)
            if px is not None:
                ti["hit_ts"] = bar.ts.isoformat()
                ti["hit_price"] = float(px)
                ti["hit_reason"] = reason or "TRAIL"
        pk = float((rt._peak_pnl.get(figi) or {}).get("price")) or None
        new_stop = pol.update_stop(side, entry, prev, bars, qty=qty, commission=comm, peak_price=pk)
        if new_stop is not None:
            ti["act_track"] = float(new_stop)
            if not born:  # ФИКС: на баре рождения новый стоп не проверяем
                px, reason = _ibe(bar, state, float(new_stop), None, close_based=True)
                if px is not None and not ti.get("hit_ts"):
                    ti["hit_ts"] = bar.ts.isoformat()
                    ti["hit_price"] = float(px)
                    ti["hit_reason"] = reason or "TRAIL"
    return ti


def test_no_trail_hit_on_activation_bar():
    """PLZL 04.09.2026: стоп рождается выше open бара входа → раньше «срабатывал» сразу.

    Бар входа: open = вход, high = open + 1.5 → стоп = high − 1×ATR(1.0) = open + 0.5,
    то есть ВЫШЕ открытия бара. Старая проверка в этом же баре давала
    hit_price = open = цена входа и hit_pnl = 0.00₽.
    """
    rt = _rt(trail_info_mult=0.1, distance_atr=1.0, atr=1.0)
    buf = _buffer(20, start=99.0, rng=1.0)
    bar = _Bar(datetime(2026, 9, 4, 11, 40, tzinfo=timezone.utc), 100.0, 101.5, 99.8, 101.2)
    state = _step(rt, bar)
    assert float(rt._peak_pnl["BBG1"]["price"]) == 101.5  # пик = high текущего бара
    ti = _trail_block(rt, bar, state, buf=buf + [bar])
    assert ti["active"] is True, "трейл должен активироваться на баре входа"
    assert ti["act_track"] > 100.0, "стоп рождается выше открытия — это и вызывало баг"
    assert not ti.get("hit_ts"), "трейл не должен срабатывать на баре рождения"

    # На СЛЕДУЮЩЕМ баре проверка работает: закрытие за стопом = срабатывание.
    bar2 = _Bar(datetime(2026, 9, 4, 11, 50, tzinfo=timezone.utc), 100.6, 100.7, 98.9, 99.0)
    state2 = _step(rt, bar2)
    ti = _trail_block(rt, bar2, state2, buf=buf + [bar, bar2])
    assert ti.get("hit_ts"), "на следующем баре закрытие за стопом должно сработать"
    assert ti["hit_price"] == pytest.approx(99.0)


def test_trail_state_cleared_on_new_position():
    """Утечка: hit_ts чужой сделки не должен переезжать в новую позицию."""
    rt = _rt()
    rt._trail_info["BBG1"] = {"active": True, "act_track": 105.0,
                              "hit_ts": "2026-09-01T10:00:00+00:00",
                              "hit_price": 103.0, "hit_reason": "stop_loss"}
    bar = _Bar(datetime(2026, 9, 4, 11, 40, tzinfo=timezone.utc), 100.0, 100.5, 99.9, 100.4)
    # Шаг открытия позиции в runtime обнуляет трейл (и пик P&L).
    rt._peak_pnl["BBG1"] = {"pnl": 0.0, "ts": bar.ts, "price": 100.0,
                            "atr_abs": None, "atr_pct": None, "mae": 0.0}
    rt._trail_info.pop("BBG1", None)
    assert "BBG1" not in rt._trail_info


def test_peak_stores_mae_pct_and_atr():
    """MAE до пика: в ATR и в % от входа, ATR пика — в ₽ и в %."""
    rt = _rt()
    bar1 = _Bar(datetime(2026, 9, 4, 11, 40, tzinfo=timezone.utc), 100.0, 100.2, 99.5, 100.0)
    _step(rt, bar1)  # просадка 0.5 = 0.5 ATR = 0.5%
    bar2 = _Bar(datetime(2026, 9, 4, 11, 50, tzinfo=timezone.utc), 100.0, 101.5, 99.9, 101.2)
    _step(rt, bar2)  # новый пик — MAE фиксируется на этот момент
    peak = rt._peak_pnl["BBG1"]
    assert peak["atr_abs"] == pytest.approx(1.0)
    assert peak["atr_pct"] == pytest.approx(1.0)
    assert peak["mae_atr"] == pytest.approx(0.5)
    assert peak["mae_pct"] == pytest.approx(0.5)
    # json-сериализуемо (пишется в exit_meta)
    assert json.loads(json.dumps(peak, default=str))["mae_pct"] == pytest.approx(0.5)
