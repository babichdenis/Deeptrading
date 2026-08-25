from __future__ import annotations

from datetime import datetime, timezone

from app.bot.events import EventLog
from app.bot.risk import RiskSnapshot
from app.bot.runtime import BotOrder
from app.bot.session import session_state


def test_session_state_boundaries():
    cases = {
        "2026-08-25T06:00:00+00:00": "PRE_MARKET",
        "2026-08-25T06:55:00+00:00": "OPENING",
        "2026-08-25T12:00:00+00:00": "TRADING",
        "2026-08-25T15:50:00+00:00": "CLEARING",
        "2026-08-25T17:00:00+00:00": "EVENING",
        "2026-08-25T20:55:00+00:00": "POST_MARKET",
    }
    for iso, want in cases.items():
        assert session_state(datetime.fromisoformat(iso)) == want
    assert session_state(datetime(2026, 8, 22, 12, tzinfo=timezone.utc)) == "WEEKEND"


def test_risk_snapshot_states():
    assert RiskSnapshot(-1200, 1000, False).state == "LOSS_LIMIT"
    assert RiskSnapshot(-500, 1000, True).state == "PAUSED"
    assert RiskSnapshot(300, 1000, False).state == "NORMAL"
    assert RiskSnapshot(-1000, 0, False).state == "NORMAL"
    assert not RiskSnapshot(-1500, 1000, False).entries_allowed()
    assert RiskSnapshot(50, 1000, True).entries_allowed() is False


def test_event_log_latest_and_cap():
    log = EventLog(maxlen=3)
    for i in range(5):
        log.log("SIGNAL_CREATED", figi=f"F{i}", ticker=f"T{i}", seq=i)
    assert len(log) == 3
    evs = log.latest(10)
    assert [e["payload"]["seq"] for e in evs] == [4, 3, 2]
    top = log.latest(1)[0]
    assert top["type"] == "SIGNAL_CREATED"
    assert top["figi"] == "F4"
    assert top["ts"].endswith("+00:00")


def test_bot_order_lifecycle():
    o = BotOrder(id="paper-x", figi="F", ticker="T", action="open", side="BUY", qty=2)
    assert o.status == "SUBMITTED"
    d = o.to_dict()
    assert d["id"] == "paper-x" and d["status"] == "SUBMITTED" and d["price"] is None
    o.status = "CANCELLED"
    assert BotOrder(id="y", figi="F", ticker="T", action="open", side="BUY",
                    qty=1, status="CANCELLED").to_dict()["status"] == "CANCELLED"
    o.status = "FILLED"
    o.filled_at = datetime.now(timezone.utc)
    o.price = 123.45
    assert o.to_dict()["filled_at"] and o.to_dict()["price"] == 123.45
