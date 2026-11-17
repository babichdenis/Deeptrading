"""Проверка пригодности ряда: разрывы масштаба.

Живой случай, ради которого модуль появился: у SNGS блок 2026-07-16 … 08-24
записан в масштабе ×100 при соседних значениях ~15. Ряд проходит проверку OHLC
и при этом даёт ATR 310 % против медианы 2.7 %, вставая первым в отборе.

Здесь проверяется только поведение проверки: что она находит разрыв, что не
ругается на законные движения, что не меняет данные и что причина читаема.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.services.series_sanity import (
    DEFAULT_MAX_RATIO,
    ScaleBreak,
    detect_scale_breaks,
    series_usable,
)

T0 = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)


@dataclass
class _Bar:
    ts: datetime
    close: float


def _series(closes: list[float]) -> list[_Bar]:
    return [_Bar(ts=T0 + timedelta(days=i), close=c) for i, c in enumerate(closes)]


def test_clean_series_is_usable() -> None:
    verdict = series_usable(_series([15.0, 15.2, 14.9, 15.4, 15.1]))
    assert verdict.usable is True
    assert verdict.reason is None
    assert verdict.breaks == ()


def test_detects_hundredfold_jump_up() -> None:
    """Ровно случай SNGS: 15.245 -> 5118.9."""
    verdict = series_usable(_series([15.245, 5118.93, 4948.11, 15.2]))
    assert verdict.usable is False
    assert len(verdict.breaks) == 2, "вход в другой масштаб и возврат — два разрыва"
    first = verdict.breaks[0]
    assert first.prev_close == pytest.approx(15.245)
    assert first.close == pytest.approx(5118.93)
    assert first.ratio > 300
    assert "scale_break" in (verdict.reason or "")


def test_detects_jump_down() -> None:
    verdict = series_usable(_series([5631.39, 15.2]))
    assert verdict.usable is False
    assert verdict.breaks[0].ratio < 0.01


def test_ordinary_moves_are_not_breaks() -> None:
    """Дивидендный гэп и обычная волатильность — не повод браковать ряд."""
    verdict = series_usable(_series([100.0, 70.0, 74.0, 68.5, 71.2]))
    assert verdict.usable is True


def test_threshold_is_configurable_and_checked() -> None:
    bars = _series([100.0, 250.0])
    assert series_usable(bars, max_ratio=3.0).usable is True, "×2.5 меньше порога"
    assert series_usable(bars, max_ratio=2.0).usable is False
    with pytest.raises(ValueError):
        series_usable(bars, max_ratio=1.0)


def test_bad_price_is_skipped_not_reported_as_scale_break() -> None:
    """Нулевая цена — дело контракта приёма, а не проверки масштаба."""
    bars = [_Bar(T0, 15.0), _Bar(T0 + timedelta(days=1), 0.0),
            _Bar(T0 + timedelta(days=2), 15.2)]
    assert detect_scale_breaks(bars) == ()


def test_default_threshold_is_sane() -> None:
    assert DEFAULT_MAX_RATIO == 3.0


def test_break_reports_facts_not_corrections() -> None:
    """Проверка описывает разрыв и НЕ предлагает исправленную цену."""
    br = ScaleBreak(ts=T0, prev_close=15.245, close=5118.93)
    text = str(br)
    assert "15.245" in text and "5118.93" in text
    assert "scale_break" not in text, "это описание, а не причина отказа"
