"""Unit-тесты контракта записи свечей (B1): чистая логика, без БД/сети.

Покрывает валидацию (все коды причин), законность нулевого volume и плоской свечи,
принадлежность окну, обработку дублей (identical/conflicting) и форму UpsertResult.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import candle_ingestion_contract as cic

_UTC = timezone.utc
_TS = datetime(2026, 10, 5, 12, 0, tzinfo=_UTC)


def _row(ts=_TS, *, o=100.0, h=100.01, l=99.99, c=100.0, v=1000,
         figi="FIGI1", interval=1) -> dict:
    return {"figi": figi, "interval": interval, "ts": ts,
            "open": o, "high": h, "low": l, "close": c, "volume": v}


# ------------------------------------------------------------------ validate_row
def test_valid_row_passes():
    assert cic.validate_row(_row()) is None


@pytest.mark.parametrize("field,value,reason", [
    ("close", float("nan"), cic.NAN),
    ("open", float("inf"), cic.NAN),
    ("high", float("-inf"), cic.NAN),
    ("volume", float("nan"), cic.NAN),
    ("open", -1.0, cic.BAD_PRICE),
    ("close", 0.0, cic.BAD_PRICE),       # цена 0 недопустима (нужна positive)
    ("low", 0.0, cic.BAD_PRICE),
    ("volume", -1, cic.BAD_VOLUME),
])
def test_invalid_values(field, value, reason):
    r = _row()
    r[field] = value  # field — dict-ключ (open/high/low/close/volume)
    rej = cic.validate_row(r)
    assert rej is not None and rej.reason == reason


def test_bad_range_high_below_low():
    rej = cic.validate_row(_row(l=200.0))  # low > high
    assert rej.reason == cic.BAD_RANGE


def test_bad_range_close_above_high():
    rej = cic.validate_row(_row(c=105.0))  # close > high
    assert rej.reason == cic.BAD_RANGE


def test_bad_range_open_below_low():
    rej = cic.validate_row(_row(o=50.0))  # open < low
    assert rej.reason == cic.BAD_RANGE


def test_price_one_is_not_auto_corruption():
    """Цена 1 сама по себе НЕ доказывает порчу — структурно валидна."""
    assert cic.validate_row(_row(o=1.0, h=1.0, l=1.0, c=1.0)) is None


def test_zero_volume_legal():
    assert cic.validate_row(_row(v=0)) is None


def test_flat_candle_legal():
    """Плоская свеча (o==h==l==c) законна."""
    assert cic.validate_row(_row(o=100.0, h=100.0, l=100.0, c=100.0)) is None


def test_naive_ts_rejected():
    rej = cic.validate_row(_row().copy() | {"ts": _TS.replace(tzinfo=None)})
    assert rej.reason == cic.NAIVE_TS


def test_non_datetime_ts_rejected():
    rej = cic.validate_row(_row() | {"ts": "2026-10-05"})
    assert rej.reason == cic.NOT_DATETIME


def test_missing_figi_rejected():
    rej = cic.validate_row(_row(figi=""))
    assert rej.reason == cic.MISSING_FIGI


def test_bad_interval_rejected():
    rej = cic.validate_row(_row(interval=0))
    assert rej.reason == cic.BAD_INTERVAL


def test_missing_field_rejected():
    r = _row()
    del r["close"]
    rej = cic.validate_row(r)
    assert rej.reason == cic.MISSING_FIELD


def test_window_membership():
    w = (_TS, _TS + timedelta(minutes=5))
    assert cic.validate_row(_row(_TS), window=w) is None
    assert cic.validate_row(_row(_TS + timedelta(minutes=5)), window=w) is None
    outside = cic.validate_row(_row(_TS + timedelta(minutes=6)), window=w)
    assert outside.reason == cic.OUTSIDE_WINDOW


def test_window_tz_compared_in_utc():
    """Окно и ts сравниваются в UTC независимо от исходного tz."""
    from zoneinfo import ZoneInfo
    msk = ZoneInfo("Europe/Moscow")
    ts_msk = _TS.astimezone(msk)  # тот же момент, другой tz
    w = (_TS, _TS)
    assert cic.validate_row(_row(ts_msk), window=w) is None


# ------------------------------------------------------------------ duplicates
def test_identical_duplicates_deduped():
    rows = [_row(), dict(_row())]
    deduped, conflicts = cic.split_duplicates(rows)
    assert len(deduped) == 1 and conflicts == []


def test_conflicting_duplicate_detected():
    a = _row()
    b = dict(_row(), close=100.005)  # валидна, иное содержимое, тот же ключ
    deduped, conflicts = cic.split_duplicates([a, b])
    assert len(conflicts) == 1
    assert len(deduped) == 1


def test_same_ts_different_figi_not_duplicate():
    rows = [_row(figi="A"), _row(figi="B")]
    deduped, conflicts = cic.split_duplicates(rows)
    assert len(deduped) == 2 and conflicts == []


def test_float_int_volume_content_equal():
    """volume 1000 (int) и 1000.0 (float) — одинаковое содержимое."""
    a = _row(v=1000)
    b = _row(v=1000.0)
    _, conflicts = cic.split_duplicates([a, b])
    assert conflicts == []


# ------------------------------------------------------------------ assert_valid_batch
def test_assert_valid_batch_raises_on_invalid():
    with pytest.raises(cic.CandleValidationError) as ei:
        cic.assert_valid_batch([_row(), _row(c=float("nan"))])
    assert ei.value.rejections and ei.value.rejections[0].reason == cic.NAN


def test_assert_valid_batch_raises_on_conflict():
    with pytest.raises(cic.ConflictingDuplicateError):
        cic.assert_valid_batch([_row(), dict(_row(), close=100.005)])


def test_assert_valid_batch_ok_returns_deduped():
    out = cic.assert_valid_batch([_row(), dict(_row()), _row(_TS + timedelta(minutes=1))])
    assert len(out) == 2  # один дубль удалён


def test_contract_error_is_valueerror_not_dbapi():
    """Контрактный отказ — ValueError, не сырая ошибка драйвера/БД."""
    from sqlalchemy.exc import DBAPIError
    try:
        cic.assert_valid_batch([_row(c=float("nan"))])
    except Exception as e:  # noqa: BLE001
        assert isinstance(e, cic.CandleContractError)
        assert isinstance(e, ValueError)
        assert not isinstance(e, DBAPIError)


# ------------------------------------------------------------------ UpsertResult
def test_upsert_result_fields():
    r = cic.UpsertResult(accepted=5, inserted=3, unchanged=1, corrected=1, written=4)
    assert r.accepted == 5 and r.inserted == 3 and r.unchanged == 1
    assert r.corrected == 1 and r.written == 4 and r.rejected == 0
    assert r.written == r.inserted + r.corrected
