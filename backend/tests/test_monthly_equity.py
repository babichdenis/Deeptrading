import pytest

from research.monthly_equity import summarize_months


WINDOW = {"start": "2025-12-31T21:00:00+00:00", "end": "2026-02-28T21:00:00+00:00"}


def arm(rows, uncertain=()):
    return {"initial_cash": 20000, "equity_path": rows, "execution_uncertain_figis": uncertain}


def point(ts, equity):
    return {"ts": ts, "equity": equity}


def test_monthly_returns_use_previous_equity_and_moscow_boundaries():
    result = summarize_months(arm([
        point("2026-01-31T21:00:00+00:00", 26000),
        point("2026-02-28T21:00:00+00:00", 33800),
    ]), WINDOW)
    assert [r["month_msk"] for r in result] == ["2026-01", "2026-02"]
    assert [r["marked_return_pct"] for r in result] == pytest.approx([30, 30])
    assert all(r["full_calendar_month"] for r in result)
    assert all(r["certified_full_path_drawdown_pct"] is None for r in result)


def test_missing_boundary_is_not_replaced_with_nearby_quote():
    result = summarize_months(arm([
        point("2026-01-31T20:59:00+00:00", 26000),
        point("2026-02-28T21:00:00+00:00", 33800),
    ]), WINDOW)
    assert all(r["marked_return_pct"] is None for r in result)


def test_uncertain_execution_cannot_become_verified_return():
    result = summarize_months(arm([
        point("2026-01-31T21:00:00+00:00", 26000),
        point("2026-02-28T21:00:00+00:00", 33800),
    ], ["FIGI"]), WINDOW)
    assert all(r["marked_return_pct"] is None for r in result)


def test_unknown_valuation_keeps_sampled_risk_explicit():
    result = summarize_months(arm([
        point("2026-01-05T12:00:00+00:00", None),
        point("2026-01-06T12:00:00+00:00", 10000),
        point("2026-01-31T21:00:00+00:00", 26000),
    ]), WINDOW)
    assert result[0]["unknown_equity_points"] == 1
    assert result[0]["sampled_drawdown_pct"] == 50
    assert result[0]["certified_full_path_drawdown_pct"] is None


@pytest.mark.parametrize("stamps", [
    ["2026-01-05T12:00:00", "2026-01-06T12:00:00+00:00"],
    ["2026-01-05T12:00:00+00:00"] * 2,
    ["2026-01-06T12:00:00+00:00", "2026-01-05T12:00:00+00:00"],
])
def test_invalid_timestamp_paths_rejected(stamps):
    with pytest.raises(ValueError):
        summarize_months(arm([point(ts, 20000) for ts in stamps]), WINDOW)
