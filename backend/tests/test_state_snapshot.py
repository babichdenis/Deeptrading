"""State Contract (Этап B): сериализация и стабильность fingerprint снимка движка."""
from __future__ import annotations

from datetime import datetime, timezone

from app.engine.state import EngineStateSnapshot, IndicatorStateSnapshot, StrategyStateSnapshot


def _snap() -> EngineStateSnapshot:
    return EngineStateSnapshot(
        instrument="BBG004730N88",
        timeframe="10min",
        as_of=datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc),
        data_version="dh16",
        resampler_version="start-v1",
        indicator_version="hub-1",
        strategy_version="rsi_trade_hub-1.0.0",
        indicators=(
            IndicatorStateSnapshot("rsi", "len=20", {"ag": 0.41, "al": 0.63, "n": 421}),
            IndicatorStateSnapshot("ema", "len=50", {"last": 273.11}),
        ),
        strategy=StrategyStateSnapshot("rsi_trade_hub", "1.0.0", {"prev_rsi": 62.28}),
    )


def test_roundtrip_and_fingerprint_stable():
    snap = _snap()
    raw = snap.to_json()
    back = EngineStateSnapshot.from_json(raw)
    assert back == snap
    assert back.fingerprint() == snap.fingerprint()
    assert snap.to_json() == back.to_json()


def test_fingerprint_changes_with_state():
    a = _snap()
    b = EngineStateSnapshot(**{**a._payload(), "as_of": datetime(2026, 9, 24, 4, 10, tzinfo=timezone.utc)})
    assert a.fingerprint() != b.fingerprint()


def test_json_safe():
    import json
    json.loads(_snap().to_json())  # не должно бросать
