from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from app.engine.models import Candle
from app.services.ensemble import _oracle_coverage, compute_ensemble

T0 = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)


def iso(minute: int) -> str:
    return (T0 + timedelta(minutes=minute)).isoformat()


def ts(minute: int) -> datetime:
    return T0 + timedelta(minutes=minute)


def buy_swing() -> dict:
    return {"side": "BUY", "point_ts": iso(10), "confirmation_ts": iso(30),
            "point_idx": 10, "conf_idx": 30}


def sell_swing() -> dict:
    return {"side": "SELL", "point_ts": iso(50), "confirmation_ts": iso(70),
            "point_idx": 50, "conf_idx": 70}


def test_geometric_sees_signal_before_point_causal_does_not():
    entries_raw = [{"ts": ts(3), "side": "BUY", "reason": "micro_breakout_up"}]
    res = _oracle_coverage([buy_swing()], entries_raw, [], [])
    assert res["geometric"]["raw_seen_before_point"] == 1
    assert res["causal"]["raw_seen_before_confirmation"] == 0
    assert res["causal"]["accepted"] == 0
    assert res["rejected_by_gate"]["not_seen"] == 1
    p = res["points"][0]
    assert p["causal"] is False
    assert p["primary_reason"] == "not_seen"
    assert p["nearest_before_point_ts"] == iso(3)
    assert p["nearest_before_conf_ts"] is None


def test_causal_accepted_and_executed_counting():
    entries_raw = [
        {"ts": ts(25), "side": "BUY"},
        {"ts": ts(65), "side": "SELL"},
    ]
    accepted = [{"ts": iso(25), "side": "BUY"}, {"ts": iso(65), "side": "SELL"}]
    executed = {(iso(25), "BUY")}
    res = _oracle_coverage([buy_swing(), sell_swing()], entries_raw, accepted, [],
                           executed_signals=executed)
    assert res["causal"]["raw_seen_before_confirmation"] == 2
    assert res["causal"]["accepted"] == 2
    assert res["causal"]["executed"] == 1
    assert res["geometric"]["executed"] == 0
    assert [p["causal"] for p in res["points"]] == [True, True]
    assert res["points"][0]["primary_reason"] == "accepted"


def test_rejected_signal_maps_to_gate():
    entries_raw = [{"ts": ts(25), "side": "BUY"}, {"ts": ts(65), "side": "SELL"}]
    rejected = [
        {"ts": iso(25), "side": "BUY", "reason": "QUORUM_REJECT"},
        {"ts": iso(65), "side": "SELL", "reason": "BIAS_REJECT"},
    ]
    res = _oracle_coverage([buy_swing(), sell_swing()], entries_raw, [], rejected)
    assert res["rejected_by_gate"]["quorum"] == 1
    assert res["rejected_by_gate"]["bias"] == 1
    p_buy = res["points"][0]
    assert p_buy["failed_gates"] == ["quorum"]
    assert p_buy["primary_reason"] == "QUORUM_REJECT"
    p_sell = res["points"][1]
    assert p_sell["primary_reason"] == "BIAS_REJECT"


def test_confirmation_lag_stats_and_side_filtering():
    entries_raw = [{"ts": ts(3), "side": "SELL"}, {"ts": ts(45), "side": "SELL"}]
    res = _oracle_coverage([buy_swing(), sell_swing()], entries_raw, [], [])
    assert res["point_total"] == 2
    assert res["confirmation_lag_bars"] == {"mean": 20.0, "median": 20, "max": 20}
    buy_pt = res["points"][0]
    sell_pt = res["points"][1]
    assert buy_pt["raw_same_side_before_point"] is False
    assert sell_pt["raw_same_side_before_point"] is True
    assert sell_pt["nearest_before_point_ts"] == iso(45)
    assert res["geometric"]["raw_seen_before_point"] == 1


def compute_smoke_candles(n: int = 300) -> list[Candle]:
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    out = []
    for i in range(n):
        px = 100 + 2 * math.sin(i / 12)
        out.append(Candle(ts=end - timedelta(minutes=n - 1 - i),
                          open=px, high=px + 0.4, low=px - 0.4, close=px, volume=1000))
    return out


def test_ensemble_static_contains_oracle_coverage():
    res = compute_ensemble(compute_smoke_candles(), {"days": 30})
    assert "error" not in res, res.get("error")
    oc = res["static"]["oracle_coverage"]
    assert oc is not None
    assert oc["point_total"] > 0
    for key in ("window_minutes", "geometric", "causal", "rejected_by_gate",
                "confirmation_lag_bars", "points"):
        assert key in oc
    for p in oc["points"]:
        assert set(p) >= {"side", "oracle_point_ts", "oracle_confirmation_ts",
                          "causal", "failed_gates", "primary_reason"}
