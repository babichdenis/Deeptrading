from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.metrics import by_figi, equity_curve, full_report, halves, max_drawdown_pct, summarize
from app.engine.models import Trade

T0 = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)


def make_trade(idx: int, net: float, figi: str = "TEST") -> Trade:
    entry = T0 + timedelta(hours=idx)
    exit_ = entry + timedelta(minutes=30)
    return Trade(
        trade_id=f"T{idx:04d}",
        figi=figi,
        side="LONG",
        qty=1,
        entry_index=idx,
        entry_time=entry,
        entry_price=100.0,
        exit_index=idx + 1,
        exit_time=exit_,
        exit_price=100.0 + net,
        bars_held=1,
        gross_pnl=net,
        commission=0.5,
        slippage=0.02,
        net_pnl=net - 0.5,
        exit_reason="target",
    )


def test_summarize_basic():
    trades = [make_trade(1, 10), make_trade(2, -4), make_trade(3, 6)]
    s = summarize(trades)
    assert s["trades"] == 3
    assert s["wins"] == 2
    assert s["win_rate"] == 66.67
    assert s["net"] == round(9.5 + 5.5 - 4.5, 4)
    assert s["profit_factor"] > 1
    assert s["long_count"] == 3


def test_summarize_empty():
    s = summarize([])
    assert s["trades"] == 0
    assert s["win_rate"] == 0.0


def test_equity_and_drawdown():
    trades = [make_trade(1, 20), make_trade(2, -30), make_trade(3, 15)]
    curve = equity_curve(trades, start_capital=1000)
    values = [p["equity"] for p in curve]
    assert len(curve) == 3
    dd = max_drawdown_pct(values)
    assert 2 < dd < 3


def test_halves_split():
    trades = [make_trade(i, 5) for i in range(6)]
    h = halves(trades)
    assert h["h1"]["trades"] == 3
    assert h["h2"]["trades"] == 3


def test_by_figi_groups():
    trades = [make_trade(1, 5, "AAA"), make_trade(2, -3, "BBB"), make_trade(3, 4, "AAA")]
    grouped = by_figi(trades)
    assert set(grouped) == {"AAA", "BBB"}
    assert grouped["AAA"]["trades"] == 2


def test_full_report_shape():
    trades = [make_trade(1, 25), make_trade(2, -10), make_trade(3, 12)]
    report = full_report(trades, start_capital=1000)
    assert report["summary"]["trades"] == 3
    assert report["max_drawdown_pct"] >= 0
    assert report["final_equity"] == round(1000 + 24.5 + (-10.5) + 11.5, 2)
    assert "recovery" in report
