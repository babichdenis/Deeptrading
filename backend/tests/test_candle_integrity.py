"""Watchdog свечей: каноническая строка, md5-пара, композиция, статусы (hermetic)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.candle_integrity import (
    aggregate_rows,
    canonical_line,
    classify_status,
    is_trading_minute,
    md5_pair,
)

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def _row(i, close=None):
    c = close if close is not None else 100.0 + i
    return (T0 + timedelta(minutes=i), 100.0, c + 0.5, c - 0.5, c, 10.0 + i)


def test_canonical_line_golden():
    line = canonical_line(T0, 252.88, 253.0, 252.5, 252.9, 1000)
    assert line == "1788256800|252.88000000|253.00000000|252.50000000|252.90000000|1000.00000000"


def test_md5_pair_signed_and_stable():
    x1, s1 = md5_pair("abc")
    x2, s2 = md5_pair("abc")
    assert (x1, s1) == (x2, s2)
    assert -(2 ** 63) <= x1 < 2 ** 63 and -(2 ** 63) <= s1 < 2 ** 63


def test_aggregate_composition_and_sensitivity():
    rows_a = [_row(0), _row(1)]
    rows_b = [_row(2), _row(3)]
    a, b = aggregate_rows(rows_a), aggregate_rows(rows_b)
    whole = aggregate_rows(rows_a + rows_b)
    assert (a["hash_xor"] ^ b["hash_xor"]) == whole["hash_xor"]
    assert (a["hash_sum"] + b["hash_sum"]) == whole["hash_sum"]
    assert whole["min_ts"] == rows_a[0][0] and whole["max_ts"] == rows_b[-1][0]
    # пропуск минуты
    assert aggregate_rows([_row(0), _row(2)])["hash_xor"] != aggregate_rows([_row(0), _row(1), _row(2)])["hash_xor"]
    # правка значения
    assert aggregate_rows([_row(0), _row(1, close=999.0)])["hash_sum"] != a["hash_sum"] + b["hash_sum"]
    # сдвиг минут
    shifted = [(ts + timedelta(minutes=1), o, h, low, c, v) for ts, o, h, low, c, v in rows_a]
    assert aggregate_rows(shifted)["hash_xor"] != a["hash_xor"]


def test_is_trading_minute_boundaries():
    def msk(h, m=0):
        return datetime(2026, 9, 1, h, m, tzinfo=timezone(timedelta(hours=3)))

    assert not is_trading_minute(msk(6, 49))
    assert is_trading_minute(msk(6, 50))
    assert not is_trading_minute(msk(18, 45))
    assert is_trading_minute(msk(19, 5))
    assert is_trading_minute(msk(23, 49))
    assert not is_trading_minute(msk(23, 50))


def test_classify_status():
    assert classify_status(None, None, 100, 0) == "DEFICIT_DAY"
    assert classify_status({"rows": 40}, None, 100, 0) == "DEFICIT_DAY"
    assert classify_status({"rows": 100}, None, 100, 3) == "DEFICIT_INTRADAY"
    assert classify_status({"rows": 100}, None, 100, 0) == "NO_BASELINE"
    base = {"rows": 100, "hash_xor": "1", "hash_sum": "2", "volume_sum": "3",
            "min_ts": T0, "max_ts": T0}
    cur = {"rows": 100, "hash_xor": "1", "hash_sum": "2", "volume_sum": "3",
           "min_ts": T0, "max_ts": T0}
    assert classify_status(cur, base, 100, 0) == "OK"
    cur2 = dict(cur, hash_sum="9")
    assert classify_status(cur2, base, 100, 0) == "MISMATCH"
    assert isinstance(aggregate_rows([])["volume_sum"], Decimal)
