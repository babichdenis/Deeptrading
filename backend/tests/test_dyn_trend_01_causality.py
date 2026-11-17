"""DYN-TREND-01: точечные проверки причинности (ARCH-94: только focused, не full suite).

Проверяем границы: снапшот не видит будущее, требование покрытия относится к
КАЖДОМУ дню окна, неполные бакеты отбрасываются, мягкий вариант не гейтит
направление, строгий не содержит условия по MOEX.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts" / "research"))

import pytest  # noqa: E402

from app.engine.models import Candle  # noqa: E402
import dyn_trend_01 as dyn  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")


def day_bars(day: datetime, count: int, price: float = 100.0) -> list[Candle]:
    """Полные 5m бары сессии, начиная с 10:00 MSK."""
    out = []
    start = datetime(day.year, day.month, day.day, 10, 0, tzinfo=MSK)
    for i in range(count):
        ts = start + timedelta(minutes=5 * i)
        p = price + i * 0.01
        out.append(Candle(ts=ts.astimezone(dyn.timezone.utc), open=p, high=p + 0.1,
                          low=p - 0.1, close=p, volume=10.0))
    return out


def build_days(count_days: int, bars_per_day: int = dyn.FULL_DAY_BARS,
               base: datetime | None = None) -> tuple[dict, dict]:
    base = base or datetime(2026, 9, 7, tzinfo=MSK)
    stock, moex = {}, {}
    d = base
    made = 0
    while made < count_days:
        if d.weekday() < 5:
            rows = day_bars(d, bars_per_day)
            stock[d.date()] = rows
            moex[d.date()] = rows
            made += 1
        d += timedelta(days=1)
    return stock, moex


def test_snapshot_ignores_future_days() -> None:
    """Снапшот на дату решения не видит саму дату и последующие дни."""
    stock, moex = build_days(6)
    dates = sorted(set(stock) & set(moex))
    decision = dates[3]
    snap = dyn.build_snapshot("TEST", "FIGI", stock, moex, dates, decision)
    assert snap["window_dates"] == [d.isoformat() for d in dates[:3]]
    assert decision.isoformat() not in snap["window_dates"]
    assert snap["valid"] is True


def test_future_mutation_does_not_change_past_snapshot() -> None:
    """Мутация данных после даты решения не меняет прошлый снапшот."""
    stock, moex = build_days(6)
    dates = sorted(set(stock) & set(moex))
    decision = dates[3]
    before = dyn.build_snapshot("TEST", "FIGI", stock, moex, dates, decision)
    for d in dates[3:]:
        stock[d] = [Candle(ts=b.ts, open=999, high=999, low=999, close=999,
                           volume=b.volume) for b in stock[d]]
    after = dyn.build_snapshot("TEST", "FIGI", stock, moex, dates, decision)
    assert before == after


def test_coverage_required_per_day_not_summed() -> None:
    """ARCH-101 п.1: 95 баров требуется в КАЖДОМ дне, а не в сумме."""
    stock, moex = build_days(6)
    dates = sorted(set(stock) & set(moex))
    # последний день окна урезан: два первых полные, суммарно всё равно много
    short_day = dates[2]
    stock[short_day] = stock[short_day][:50]
    moex[short_day] = moex[short_day][:50]
    snap = dyn.build_snapshot("TEST", "FIGI", stock, moex, dates, dates[3])
    assert snap["valid"] is False
    assert snap["reason"].startswith("coverage_")
    total = sum(snap["coverage"].values())
    assert total > dyn.MIN_BARS_PER_DAY, (
        f"суммарно {total} баров — прежняя логика «95 суммарно» пропустила бы вход")


def test_insufficient_window_days_is_unknown() -> None:
    stock, moex = build_days(2)
    dates = sorted(set(stock) & set(moex))
    snap = dyn.build_snapshot("TEST", "FIGI", stock, moex, dates, dates[1])
    assert snap["valid"] is False
    assert snap["reason"] == "insufficient_window_days"


def test_resample_drops_incomplete_buckets() -> None:
    """Бакет без всех минут отбрасывается, а не добивается догадкой."""
    start = datetime(2026, 9, 7, 7, 0, tzinfo=dyn.timezone.utc)
    minute_bars = [Candle(ts=start + timedelta(minutes=i), open=1, high=1, low=1,
                          close=1, volume=1) for i in range(10)]
    del minute_bars[3]  # дырка внутри первого бакета
    out = dyn.resample_complete(minute_bars, seconds=300)
    assert len(out) == 1, "проверяются только два полных бакета, первый отброшен"
    assert out[0].ts == start + timedelta(minutes=5)


def test_soft_variant_does_not_gate_direction() -> None:
    """ARCH-101 п.2: в dyn_soft направление трендом НЕ ограничивается."""
    src = (BACKEND / "scripts" / "research" / "dyn_trend_01.py").read_text(encoding="utf-8")
    block = src[src.index('if self.mode == "dyn_soft"'):
                src.index('else:  # dyn_strict')]
    for forbidden in ("trend_direction", "TrendDirection", "relative_return"):
        assert forbidden not in block, f"в мягком варианте не должно быть {forbidden}"


def test_strict_variant_has_no_moex_condition() -> None:
    """ARCH-101 п.3: в строгом варианте нет условия side*MOEX <= 0."""
    src = (BACKEND / "scripts" / "research" / "dyn_trend_01.py").read_text(encoding="utf-8")
    block = src[src.index('else:  # dyn_strict'):src.index("return super().decide")]
    assert "moex_return" not in block
    assert "relative_return" in block, "относительная сила должна участвовать"


def test_excluding_ticker_keeps_other_snapshots() -> None:
    stock, moex = build_days(6)
    dates = sorted(set(stock) & set(moex))
    full = dyn.build_snapshot("AAA", "F1", stock, moex, dates, dates[3])
    other = dyn.build_snapshot("BBB", "F2", stock, moex, dates, dates[3])
    assert {k: v for k, v in full.items() if k != "ticker"} == \
           {k: v for k, v in other.items() if k != "ticker"}, \
        "одинаковые данные дают одинаковый снапшот независимо от тикера"
    assert full["ticker"] == "AAA" and other["ticker"] == "BBB"


def test_rank_is_deterministic_and_capped() -> None:
    rows = [{"ticker": f"T{i:02d}", "valid": True, "atr_pct": 0.01 * (i + 1)}
            for i in range(15)]
    rows.append({"ticker": "TV", "valid": False, "atr_pct": None})
    dyn.rank_top10(rows)
    top = [r["ticker"] for r in rows if r.get("top10")]
    assert len(top) == dyn.TOP_N
    best = [r for r in rows if r.get("rank_atr") == 1][0]
    assert best["ticker"] == "T14", "первый ранг — наибольший ATR%"
    assert rows[-1].get("top10") is None, "невалидный кандидат не получает ранг"
