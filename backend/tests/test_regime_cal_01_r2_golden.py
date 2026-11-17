"""Golden fixtures для REGIME-CAL-01 r2: границы исполнения и отчёта.

Фикстуры сдаются ДО нового прогона (требование ARCH-74). Каждая ловит конкретный
дефект r1: гэп через стоп, конфликт стоп/цель, конец данных, притязания на капитал.
"""
from __future__ import annotations

import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts" / "research"))

import pytest

from app.engine.models import Candle as EngineCandle
from app.engine.research_execution import walk_exit
import regime_cal_01_r2 as r2

BASE = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def bar(minutes: int, o: float, h: float, l: float, c: float) -> EngineCandle:
    return EngineCandle(ts=BASE + timedelta(minutes=minutes), open=o, high=h, low=l,
                        close=c, volume=100.0)


def test_gap_through_stop_long_exits_at_open_not_at_stop() -> None:
    """Фикстура ревью: entry 103, stop 98.5714, бар open 80 → выход 80, не 98.5714."""
    bars = [bar(0, 103.0, 103.0, 102.9, 103.0), bar(60, 80.0, 81.0, 79.0, 80.5)]
    out = r2.walk_exit_hourly(bars, 1, 1, 98.5714, 110.0)
    assert out[0] == "exit"
    assert out[1] == pytest.approx(80.0), "long при гэпе выходит по open, а не по цене стопа"
    assert out[3] == "sl_gap_open"


def test_gap_through_stop_short_exits_at_open() -> None:
    bars = [bar(0, 100.0, 100.1, 99.9, 100.0), bar(60, 120.0, 121.0, 119.0, 120.0)]
    out = r2.walk_exit_hourly(bars, 1, -1, 104.5, 90.0)
    assert out[0] == "exit"
    assert out[1] == pytest.approx(120.0)
    assert out[3] == "sl_gap_open"


def test_stop_wins_over_target_inside_one_bar() -> None:
    bars = [bar(0, 100.0, 100.0, 100.0, 100.0), bar(60, 100.0, 103.0, 97.0, 101.0)]
    out = r2.walk_exit_hourly(bars, 1, 1, 98.0, 102.0)
    assert out[3] == "sl_touch"
    assert out[1] == pytest.approx(98.0)


def test_target_reachable_without_stop_touch() -> None:
    bars = [bar(0, 100.0, 100.0, 100.0, 100.0), bar(60, 100.5, 103.0, 100.2, 102.5)]
    out = r2.walk_exit_hourly(bars, 1, 1, 98.0, 102.0)
    assert out[3] == "tp_touch"
    assert out[1] == pytest.approx(102.0)


def test_data_end_leaves_position_incomplete_not_closed_at_last_price() -> None:
    bars = [bar(0, 100.0, 100.0, 100.0, 100.0), bar(60, 100.0, 101.0, 99.5, 100.8)]
    out = r2.walk_exit_hourly(bars, 1, 1, 90.0, 120.0)
    assert out[0] == "incomplete"
    assert out[1] == "data_ended_open_position"


def test_gap_rule_agrees_with_canonical_walk_exit() -> None:
    """Канон на минутах даёт ту же семантику: гэп через стоп → open, не стоп."""

    class M:
        def __init__(self, ts, o, h, l):
            self.ts, self.open, self.high, self.low = ts, o, h, l

    entry = BASE
    minutes = [M(BASE, 103.0, 103.0, 102.9),
               M(BASE + timedelta(minutes=1), 80.0, 81.0, 79.0)]
    by_ts = {m.ts: m for m in minutes}
    canon = walk_exit(minutes, by_ts, day=BASE.date(), entry_ts=entry, side=1,
                      sl=98.5714, tp=110.0, full_18_20=False, has_18_30=False)
    assert canon[0] == "exit"
    assert canon[1] == pytest.approx(80.0)
    assert canon[3] == "sl_gap_open"


def test_atr_uses_registry_not_own_copy() -> None:
    from app.engine.indicatorhub import INDICATORS

    bars = [bar(i * 60, 100 + i * 0.1, 100.4 + i * 0.1, 99.6 + i * 0.1, 100.2 + i * 0.1)
            for i in range(30)]
    mine = r2.atr_series(bars)
    canon = INDICATORS["atr"].calculate(bars, {"length": r2.ATR_PERIOD})["value"]
    assert [None if v is None else round(float(v), 10) for v in mine] == \
           [None if v is None else round(float(v), 10) for v in canon]


def test_label_at_does_not_use_bar_before_its_close() -> None:
    labels = [(BASE, "RANGE"), (BASE + timedelta(hours=1), "TREND_UP")]
    inside_first = BASE + timedelta(minutes=30)
    assert r2.label_at(labels, inside_first) is None, "бар ещё не закрыт — метки нет"
    at_close = BASE + timedelta(hours=1)
    assert r2.label_at(labels, at_close) == "RANGE"


def test_overnight_detected_on_msk_date_change() -> None:
    evening_msk = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
    next_morning_msk = datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)
    same_evening_msk = datetime(2026, 9, 1, 19, 0, tzinfo=timezone.utc)
    assert r2.crossed_day(evening_msk, next_morning_msk) is True
    assert r2.crossed_day(evening_msk, same_evening_msk) is False


def test_summary_makes_no_capital_or_drawdown_claims() -> None:
    trades = [
        {"status": "closed", "reason": "sl_touch", "overnight": False,
         "net_rub": -50.0, "net_pct": -0.005, "gross_pct": -0.004, "costs_pct": 0.0007},
        {"status": "incomplete", "reason": "data_ended_open_position", "overnight": None,
         "net_rub": None},
    ]
    s = r2.summarize(trades)
    for forbidden in ("equity", "dd", "drawdown", "max_drawdown"):
        assert forbidden not in s
    assert s["diagnostic_kind"] == "normalized_per_trade_diagnostic_not_portfolio_capital"
    assert s["trades_closed"] == 1
    assert s["trades_open_at_data_end"] == 1
    assert s["overnight_costs"] == "unknown"


def test_empty_summary_reports_unknown_not_zero() -> None:
    s = r2.summarize([])
    assert s["net_rub_sum"] is None, "неизвестное не маскируется нулём"
    assert s["win_rate"] is None


def test_no_embedded_credentials_in_source() -> None:
    src = inspect.getsource(r2)
    assert "deeptrading:deeptrading@" not in src
    assert "PGPASSWORD" in src


def test_universe_is_frozen_before_evaluation(tmp_path: Path) -> None:
    import asyncio

    class FakeConn:
        async def fetch(self, sql, *args):
            self.last_args = args
            return [{"figi": "AAA", "n": 10}, {"figi": "BBB", "n": 9}]

    conn = FakeConn()
    tickers, mode = asyncio.run(r2.pick_tickers_fixed(conn, tmp_path))
    assert tickers == ["AAA", "BBB"]
    assert mode == "frozen_from_august_only"
    frozen = tmp_path / "universe.json"
    assert frozen.exists()
    again, mode2 = asyncio.run(r2.pick_tickers_fixed(conn, tmp_path))
    assert again == ["AAA", "BBB"] and mode2 == "frozen_from_file"
