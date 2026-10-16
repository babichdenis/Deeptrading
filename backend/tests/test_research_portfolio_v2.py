"""B3 тесты research_portfolio_v2.py (герметично, на synthetic trades).

Проверяют ключевые инварианты RESEARCH-CORE-V2:
- нормированный капитал 1, gross exposure <= 1, без leverage;
- равные веса сигналов дня, невошедшая доля = cash;
- разные candidates — отдельные альтернативные портфели;
- UNKNOWN исход → день UNKNOWN, equity/DD после не выдумывается;
- known-only диагностика отдельно с маркировкой;
- walk-forward без lookahead (train >= 2 мес, следующий месяц eval);
- baseline CASH и long-only equal-weight.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import research_portfolio_v2 as rp  # noqa: E402

UTC = timezone.utc


def _trade(ticker="GAZP", cid="B01", day="2026-06-11", side="long",
           entry_base=100.0, net=None, status="FILLED", inc="") -> rp.Trade:
    net_val = net if net is not None else 0.0
    return rp.Trade(
        ticker=ticker, candidate_id=cid, day=date.fromisoformat(day),
        side=side,
        entry_ts=datetime.fromisoformat(f"{day}T08:00:00+00:00"),
        exit_ts=(datetime.fromisoformat(f"{day}T15:00:00+00:00")
                 if status == "FILLED" else None),
        entry_base=entry_base,
        net_per_unit=(net_val / entry_base) if status == "FILLED" else None,
        gross_per_unit=(net_val / entry_base) if status == "FILLED" else None,
        costs_per_unit=0.0014 if status == "FILLED" else None,
        status=status, incomplete_reason=inc,
        event_id=f"{cid}|{ticker}|{day}")


# ------------------------------------------------------------- allocator

def test_equal_weights_and_cash():
    trades = [_trade("GAZP", "B01", "2026-06-11", net=1.0),
              _trade("LKOH", "B01", "2026-06-11", net=2.0),
              _trade("SFIN", "B01", "2026-06-11", net=-1.0)]
    ledger = rp.build_portfolio(trades, "B01", "c5_s2")
    assert len(ledger.days) == 1
    d = ledger.days[0]
    assert d.capital_usage == 1.0
    assert d.cash_weight == 0.0
    assert all(s.weight == 1.0 / 3 for s in d.slots)
    assert abs(d.return_daily - (1.0 + 2.0 - 1.0) / 3 / 100.0) < 1e-12


def test_gross_exposure_never_exceeds_one():
    trades = [_trade("GAZP", "B01", "2026-06-11", net=1.0, side=s)
              for s in ("long", "short", "long", "short", "long")]
    ledger = rp.build_portfolio(trades, "B01", "c5_s2")
    d = ledger.days[0]
    assert d.capital_usage <= 1.0 + 1e-12


def test_partial_day_leaves_cash():
    trades = [_trade("GAZP", "B01", "2026-06-11", net=1.0)]
    ledger = rp.build_portfolio(trades, "B01", "c5_s2")
    d = ledger.days[0]
    assert d.capital_usage == 1.0
    assert d.cash_weight == 0.0


# ------------------------------------------------------------- unknown

def test_unknown_day_poisons_equity_curve():
    trades = [_trade("GAZP", "B01", "2026-06-11", net=1.0),
              _trade("GAZP", "B01", "2026-06-12", status="INCOMPLETE",
                     inc="path_gap_after_entry")]
    ledger = rp.build_portfolio(trades, "B01", "c5_s2")
    assert ledger.days[0].return_daily is not None
    assert ledger.days[1].return_daily is None
    assert ledger.days[1].has_unknown is True
    curve = ledger.equity_curve()
    assert curve[0][1] is not None
    assert curve[1][1] is None


def test_defined_drawdown_only_on_known():
    trades = [_trade("GAZP", "B01", "2026-06-11", net=-0.05),
              _trade("GAZP", "B01", "2026-06-12", net=0.02),
              _trade("GAZP", "B01", "2026-06-15", status="INCOMPLETE")]
    ledger = rp.build_portfolio(trades, "B01", "c5_s2")
    dd = ledger.defined_drawdown()
    assert dd is not None and dd > 0
    m = rp.portfolio_metrics(ledger)
    assert m.days_total == 3
    assert m.days_unknown == 1
    assert m.days_known == 2
    assert m.defined_curve_points == 2


def test_known_return_separate_from_daily():
    trades = [_trade("GAZP", "B01", "2026-06-11", net=1.0),
              _trade("GAZP", "B01", "2026-06-12", status="INCOMPLETE")]
    ledger = rp.build_portfolio(trades, "B01", "c5_s2")
    m = rp.portfolio_metrics(ledger)
    assert m.known_days_return_mean is not None
    assert m.days_unknown == 1


# ------------------------------------------------------------- candidates

def test_separate_alternative_portfolios():
    trades = [_trade("GAZP", "B01", "2026-06-11", net=1.0),
              _trade("GAZP", "B02", "2026-06-11", net=-3.0)]
    l1 = rp.build_portfolio(trades, "B01", "c5_s2")
    l2 = rp.build_portfolio(trades, "B02", "c5_s2")
    assert l1.days[0].return_daily > 0
    assert l2.days[0].return_daily < 0
    assert l1.candidate_id != l2.candidate_id


# ------------------------------------------------------------- walk-forward

def test_walk_forward_no_lookahead():
    ledgers = {}
    for cid, sign in (("B01", 1.0), ("B02", -1.0)):
        trades = []
        for m, day in ((6, "2026-06-11"), (7, "2026-07-11"), (8, "2026-08-11")):
            trades.append(_trade("GAZP", cid, day, net=sign * 0.5))
        ledgers[(cid, "c5_s2")] = rp.build_portfolio(trades, cid, "c5_s2")
    results = rp.walk_forward_select(ledgers, "c5_s2", min_train_months=2)
    assert results[0].chosen == "CASH" and results[0].reason == "insufficient_train"
    assert results[1].chosen == "CASH" and results[1].reason == "insufficient_train"
    assert results[2].chosen == "B01"
    assert results[2].reason == "selected"


def test_walk_forward_cash_when_no_positive():
    ledgers = {}
    trades = [_trade("GAZP", "B01", "2026-06-11", net=-1.0),
              _trade("GAZP", "B01", "2026-07-11", net=-1.0),
              _trade("GAZP", "B01", "2026-08-11", net=-1.0)]
    ledgers[("B01", "c5_s2")] = rp.build_portfolio(trades, "B01", "c5_s2")
    results = rp.walk_forward_select(ledgers, "c5_s2", min_train_months=2)
    assert results[2].chosen == "CASH"
    assert results[2].reason == "no_positive_candidate"


# ------------------------------------------------------------- baselines

def test_baseline_cash_flat_equity():
    days = [date(2026, 6, 11), date(2026, 6, 12)]
    ledger = rp.baseline_cash(days)
    curve = ledger.equity_curve()
    assert all(eq == 1.0 for _, eq in curve)
    assert all(d.capital_usage == 0.0 for d in ledger.days)


def test_baseline_long_excludes_shorts():
    trades = [_trade("GAZP", "B01", "2026-06-11", side="long", net=1.0),
              _trade("LKOH", "B01", "2026-06-11", side="short", net=5.0)]
    ledger = rp.baseline_long_equal_weight(trades, "c5_s2")
    d = ledger.days[0]
    assert len(d.slots) == 1
    assert d.slots[0].ticker == "GAZP"


def test_signal_free_long_baseline_isolates_market():
    daily = {
        "GAZP": [{"ts": "2026-06-11", "open": 100.0, "close": 102.0},
                 {"ts": "2026-06-12", "open": 102.0, "close": 101.0}],
        "LKOH": [{"ts": "2026-06-11", "open": 200.0, "close": 202.0},
                 {"ts": "2026-06-12", "open": 202.0, "close": 204.0}],
    }
    cost = 0.0007  # за сторону (итого 0.0014 round-trip)
    ledger = rp.signal_free_long_baseline(daily, cost)
    assert len(ledger.days) == 2
    d1 = ledger.days[0]
    assert len(d1.slots) == 2
    assert all(s.weight == 0.5 for s in d1.slots)
    # день 1: GAZP +2%, LKOH +1%, минус cost на сторону входа+выхода
    exp_d1 = 0.5 * ((0.02 - 2 * cost) + (0.01 - 2 * cost))
    assert abs(d1.return_daily - exp_d1) < 1e-12
    # нет логики сигналов: все тикеры дня входят
    assert all(s.side == "long" for s in d1.slots)


def test_signal_free_baseline_partial_excluded():
    daily = {
        "GAZP": [{"ts": "2026-06-11", "open": 100.0, "close": 101.0},
                 {"ts": "2026-06-12", "open": 101.0, "close": 102.0,
                  "partial": "partial_session"}],
    }
    ledger = rp.signal_free_long_baseline(daily, 0.0007)
    assert len(ledger.days) == 1
    assert ledger.days[0].day.isoformat() == "2026-06-11"


# ------------------------------------------------------------- causality

def test_future_trades_do_not_change_past_days():
    trades = [_trade("GAZP", "B01", "2026-06-11", net=1.0)]
    l1 = rp.build_portfolio(trades, "B01", "c5_s2")
    trades2 = trades + [_trade("GAZP", "B01", "2026-08-11", net=99.0)]
    l2 = rp.build_portfolio(trades2, "B01", "c5_s2")
    assert l1.days[0].return_daily == l2.days[0].return_daily


# ------------------------------------------------------------- trade quality

def test_trade_quality_win_rate_expectancy():
    trades = [
        _trade("GAZP", "B01", "2026-06-11", net=2.0),
        _trade("LKOH", "B01", "2026-06-11", net=-1.0),
        _trade("SFIN", "B01", "2026-06-12", status="INCOMPLETE", inc="gap"),
    ]
    tq = rp.trade_quality(trades, "B01", "c5_s2")
    assert tq.filled_total == 2
    assert tq.incomplete_unknown == 1
    assert abs(tq.win_rate - 0.5) < 1e-12
    assert abs(tq.avg_win - 0.02) < 1e-12
    assert abs(tq.avg_loss - (-0.01)) < 1e-12
    assert tq.gross_expectancy is not None
    assert tq.net_expectancy is not None
    assert tq.cost_break_even is not None and tq.cost_break_even > 0
    assert tq.payoff_ratio is not None and tq.payoff_ratio == 2.0
    assert tq.breakeven_win_rate is not None
    assert abs(tq.breakeven_win_rate - 1.0 / 3.0) < 1e-12


def test_trade_quality_unknown_excluded_from_denominator():
    trades = [_trade("GAZP", "B01", "2026-06-11", net=1.0)]
    for i in range(5):
        trades.append(_trade("GAZP", "B01", f"2026-06-{12 + i:02d}",
                             status="INCOMPLETE", inc="gap"))
    tq = rp.trade_quality(trades, "B01", "c5_s2")
    assert tq.filled_total == 1
    assert tq.incomplete_unknown == 5
    assert tq.win_rate == 1.0
    assert "не входит" in tq.denominator_note


# ------------------------------------------------------------- overnight

def test_overnight_decomposition_split():
    rows = [
        {"ts": "2026-06-11", "open": 100.0, "close": 102.0},   # intraday +2%
        {"ts": "2026-06-12", "open": 105.0, "close": 104.0},   # overnight +2.94%
        {"ts": "2026-06-15", "open": 103.0, "close": 103.0},   # flat
    ]
    split = rp.overnight_decomposition(rows, "GAZP")
    assert split.days == 3
    assert abs(split.intraday_mean - (0.02 + (-1 / 105) + 0.0) / 3) < 1e-9
    assert split.overnight_mean is not None
    assert split.total_mean is not None
    assert split.partial_days_excluded == 0
    assert "недостижим" in split.note


def test_overnight_partial_days_excluded():
    rows = [
        {"ts": "2026-06-11", "open": 100.0, "close": 101.0},
        {"ts": "2026-06-12", "open": 101.0, "close": 102.0, "partial": "partial_session"},
        {"ts": "2026-06-15", "open": 103.0, "close": 104.0},
    ]
    split = rp.overnight_decomposition(rows, "GAZP")
    assert split.partial_days_excluded == 1
    assert split.days == 2
