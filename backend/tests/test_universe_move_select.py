from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.bot.universe.move_select import (
    MoveSelectConfig,
    MoveSelectLogger,
    load_move_select_config,
    select_move_universe,
    try_select_move_universe,
)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeDB:
    def __init__(self, candles, margin):
        self.candles = candles
        self.margin = margin

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "instrument_margin" in sql:
            return FakeResult([(figi, short) for figi, short in self.margin.items()])
        iv = params["iv"]
        t0, t1, figis = params["t0"], params["t1"], set(params["figis"])
        rows = []
        for figi, series in self.candles.items():
            if figi not in figis:
                continue
            for ts, o, h, l, c in series:
                if iv == series_iv(series) and t0 <= ts < t1:
                    rows.append((figi, ts, o, h, l, c))
        return FakeResult(rows)


def series_iv(series):
    return getattr(series, "interval", 24)


class Series(list):
    interval = 24


def _day(base: datetime, offset: int) -> datetime:
    return base.replace(hour=12) - __import__("datetime").timedelta(days=offset)


def _candles(closes, base, interval=24):
    s = Series()
    s.interval = interval
    for i, close in enumerate(closes):
        ts = base - __import__("datetime").timedelta(days=len(closes) - 1 - i)
        s.append((ts, close, close * 1.001, close * 0.998, close))
    return s


AS_OF = datetime(2026, 6, 30, 20, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_config_defaults_disabled():
    cfg = MoveSelectConfig()
    assert cfg.enabled is False
    assert cfg.replay_only is True
    assert cfg.direction == "fall"
    assert cfg.n == 3
    assert cfg.lookback_days == 1
    assert cfg.metric == "cum_ret"


def test_config_validation_rejects_bad_values():
    with pytest.raises(ValueError):
        MoveSelectConfig(direction="sideways").validate()
    with pytest.raises(ValueError):
        MoveSelectConfig(metric="momentum").validate()
    with pytest.raises(ValueError):
        MoveSelectConfig(tf="week").validate()
    with pytest.raises(ValueError):
        MoveSelectConfig(n=0).validate()
    with pytest.raises(ValueError):
        MoveSelectConfig(lookback_days=0).validate()


def test_load_config_from_json(tmp_path):
    path = tmp_path / "move.json"
    path.write_text(json.dumps({"direction": "rise", "n": 5, "lookback_days": 3,
                                "metric": "rsi", "tf": "hour",
                                "exclude_tickers": "gmkn, smlt"}), encoding="utf-8")
    cfg = load_move_select_config(path)
    assert cfg.direction == "rise"
    assert cfg.n == 5
    assert cfg.lookback_days == 3
    assert cfg.metric == "rsi"
    assert cfg.tf == "hour"
    assert cfg.exclude_tickers == ("GMKN", "SMLT")
    assert cfg.config_source.endswith("move.json")


def test_try_returns_empty_when_disabled():
    assert _run(try_select_move_universe(FakeDB({}, {}), {"AAA": "f1"}, feed="replay",
                                        as_of=AS_OF)) == []


def test_try_returns_empty_outside_replay(tmp_path):
    path = tmp_path / "move.json"
    path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    got = _run(try_select_move_universe(FakeDB({}, {}), {"AAA": "f1"}, feed="live",
                                        as_of=AS_OF, config_path=path))
    assert got == []


def test_selects_n_fallers_by_cum_ret(tmp_path):
    base = datetime(2026, 6, 29, tzinfo=timezone.utc)
    candles = {
        "f1": _candles([100, 90], base),
        "f2": _candles([100, 95], base),
        "f3": _candles([100, 85], base),
        "f4": _candles([100, 110], base),
    }
    cfg = MoveSelectConfig(enabled=True, direction="fall", n=2, lookback_days=1,
                           metric="cum_ret", tf="day", shortable_only=False,
                           log_dir=str(tmp_path))
    got = _run(select_move_universe(FakeDB(candles, {}), {"A": "f1", "B": "f2", "C": "f3",
                                                          "D": "f4"}, cfg, AS_OF))
    assert [u["ticker"] for u in got] == ["C", "A"]
    assert got[0]["move_cum_ret"] == pytest.approx(-0.15)


def test_selects_n_risers():
    base = datetime(2026, 6, 29, tzinfo=timezone.utc)
    candles = {"f1": _candles([100, 90], base), "f2": _candles([100, 130], base),
               "f3": _candles([100, 120], base)}
    cfg = MoveSelectConfig(enabled=True, direction="rise", n=2, lookback_days=1,
                           metric="cum_ret", tf="day", shortable_only=False,
                           log_dir="/tmp/move_test")
    got = _run(select_move_universe(FakeDB(candles, {}), {"A": "f1", "B": "f2", "C": "f3"},
                                    cfg, AS_OF))
    assert [u["ticker"] for u in got] == ["B", "C"]


def test_direction_fall_excludes_risers():
    base = datetime(2026, 6, 29, tzinfo=timezone.utc)
    candles = {"f1": _candles([100, 140], base), "f2": _candles([100, 120], base)}
    cfg = MoveSelectConfig(enabled=True, direction="fall", n=5, lookback_days=1,
                           shortable_only=False, log_dir="/tmp/move_test")
    assert _run(select_move_universe(FakeDB(candles, {}), {"A": "f1", "B": "f2"}, cfg,
                                     AS_OF)) == []


def test_shortable_only_filters_by_margin(tmp_path):
    base = datetime(2026, 6, 29, tzinfo=timezone.utc)
    candles = {"f1": _candles([100, 80], base), "f2": _candles([100, 70], base)}
    cfg = MoveSelectConfig(enabled=True, direction="fall", n=5, lookback_days=1,
                           shortable_only=True, log_dir=str(tmp_path))
    got = _run(select_move_universe(FakeDB(candles, {"f1": True, "f2": False}),
                                    {"A": "f1", "B": "f2"}, cfg, AS_OF))
    assert [u["ticker"] for u in got] == ["A"]
    log = (tmp_path / f"{AS_OF.strftime('%Y%m%dT%H%M%SZ')}.jsonl").read_text(encoding="utf-8")
    assert '"action": "rejected_prefilter"' in log
    assert "not_shortable" in log


def test_exclude_tickers(tmp_path):
    base = datetime(2026, 6, 29, tzinfo=timezone.utc)
    candles = {"f1": _candles([100, 80], base), "f2": _candles([100, 70], base)}
    cfg = MoveSelectConfig(enabled=True, direction="fall", n=5, lookback_days=1,
                           shortable_only=False, exclude_tickers=("B",),
                           log_dir=str(tmp_path))
    got = _run(select_move_universe(FakeDB(candles, {}), {"A": "f1", "B": "f2"}, cfg, AS_OF))
    assert [u["ticker"] for u in got] == ["A"]


def test_lookback_window_limits_history(tmp_path):
    base = datetime(2026, 6, 29, tzinfo=timezone.utc)
    long_history = _candles([200, 100, 100, 100, 100, 100, 100, 100, 100, 50], base)
    short_history = _candles([200, 100, 50], base)
    cfg = MoveSelectConfig(enabled=True, direction="fall", n=5, lookback_days=1,
                           shortable_only=False, log_dir=str(tmp_path))
    got = _run(select_move_universe(FakeDB({"f1": long_history, "f2": short_history},
                                           {}), {"A": "f1", "B": "f2"}, cfg, AS_OF))
    by = {u["ticker"]: u for u in got}
    assert by["A"]["move_cum_ret"] == pytest.approx(-0.5)
    assert by["B"]["move_cum_ret"] == pytest.approx(-0.75)


def test_rsi_metric_uses_canonical_hub(tmp_path):
    base = datetime(2026, 6, 29, tzinfo=timezone.utc)
    down = _candles([100 - i for i in range(20)], base)
    up = _candles([80 + i for i in range(20)], base)
    cfg = MoveSelectConfig(enabled=True, direction="any", n=5, lookback_days=25,
                           metric="rsi", tf="day", rsi_period=14, shortable_only=False,
                           log_dir=str(tmp_path))
    got = _run(select_move_universe(FakeDB({"f1": down, "f2": up}, {}), {"A": "f1", "B": "f2"},
                                    cfg, AS_OF))
    by = {u["ticker"]: u for u in got}
    assert by["A"]["move_rsi"] < by["B"]["move_rsi"]
    assert [u["ticker"] for u in got] == ["A", "B"]


def test_log_records_every_decision(tmp_path):
    base = datetime(2026, 6, 29, tzinfo=timezone.utc)
    candles = {"f1": _candles([100, 80], base), "f2": _candles([100, 140], base),
               "f3": _candles([100, 90], base)}
    cfg = MoveSelectConfig(enabled=True, direction="fall", n=1, lookback_days=1,
                           shortable_only=False, log_dir=str(tmp_path))
    _run(select_move_universe(FakeDB(candles, {}), {"A": "f1", "B": "f2", "C": "f3"}, cfg,
                              AS_OF))
    path = tmp_path / f"{AS_OF.strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    actions = [r["action"] for r in rows]
    assert "picked" in actions
    assert "not_picked" in actions
    assert "rejected_sign" in actions
    picked = [r for r in rows if r["action"] == "picked"]
    assert len(picked) == 1
    assert picked[0]["ticker"] == "A"
    assert picked[0]["rank"] == 1
    assert picked[0]["cum_ret"] == pytest.approx(-0.2)


def test_deterministic_on_same_input(tmp_path):
    base = datetime(2026, 6, 29, tzinfo=timezone.utc)
    candles = {"f1": _candles([100, 90], base), "f2": _candles([100, 90], base),
               "f3": _candles([100, 85], base)}
    cfg = MoveSelectConfig(enabled=True, direction="fall", n=2, lookback_days=1,
                           shortable_only=False, log_dir=str(tmp_path))
    tickers = {"A": "f1", "B": "f2", "C": "f3"}
    first = _run(select_move_universe(FakeDB(candles, {}), tickers, cfg, AS_OF))
    second = _run(select_move_universe(FakeDB(candles, {}), tickers, cfg, AS_OF))
    assert [u["ticker"] for u in first] == [u["ticker"] for u in second]
    assert [u["move_score"] for u in first] == [u["move_score"] for u in second]


def test_logger_writes_jsonl(tmp_path):
    log = MoveSelectLogger(tmp_path, "run1")
    log.record("picked", "AAA", rank=1, score=-0.1)
    log.note("hello")
    log.close()
    rows = [json.loads(line) for line in (tmp_path / "run1.jsonl").read_text().splitlines()]
    assert rows[0]["action"] == "picked"
    assert rows[0]["ticker"] == "AAA"
    assert rows[1]["action"] == "note"


def test_try_never_raises_on_bad_config(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert _run(try_select_move_universe(FakeDB({}, {}), {"A": "f1"}, feed="replay",
                                         as_of=AS_OF, config_path=path)) == []
