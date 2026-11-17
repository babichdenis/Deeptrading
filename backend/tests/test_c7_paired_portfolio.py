"""C7 paired portfolio integration: admission at T+5, slots, cash, protection.

Synthetic data only. These cases map one-to-one onto the gates ARCH422 asked
for: a signal that loses Universe membership before it resolves, a trend that
turns before execution, a slot taken by a competing ticker, a confirmation that
never arrives, a stop hit between 5m boundaries, and a duplicated boundary that
must not duplicate an entry. Fees, lots and slippage come from the shared
``Portfolio``/``CostModel``, not from a local re-implementation.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKER = Path(__file__).resolve().parents[1] / "reports" / "connected_week_20261109_13_c1" / "worker_c7_r1"
sys.path.insert(0, str(WORKER))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import c7_timing as t7  # noqa: E402
import paired_portfolio as pp  # noqa: E402
from app.bot.universe.connected import Candidate, Series  # noqa: E402
from app.engine.costs import CostModel  # noqa: E402

UTC = timezone.utc
T = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
FIGI = "BBG004730N88"
TICKER = "SBER"


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 1000.0


def _bars(end_close: datetime, minutes: int, count: int, *,
          last_high=None, last_low=None, last_close=None):
    step = timedelta(minutes=minutes)
    last_label = end_close - step
    out = []
    for i in reversed(range(count)):
        idx = count - 1 - i
        label = last_label - step * i
        base = 100.0 + idx * 0.01
        high, low, close = base + 1.0, base - 1.0, base + 0.005
        if i == 0:
            if last_high is not None:
                high, low = last_high, last_low
            if last_close is not None:
                close = last_close
        out.append(Bar(label, base, high, low, close))
    return out


def _series(figi=FIGI, *, trend_up=True):
    """130 15m, 130 5m and 25 60m buckets, all closing at or before T."""
    s15 = _bars(T, 15, 130, last_high=102.0, last_low=99.0, last_close=101.5)
    s5 = _bars(T, 5, 130)
    s60 = _bars(T, 60, 25)
    if not trend_up:
        s15 = [
            Bar(b.ts, 200.0 - i, 201.0 - i, 199.0 - i, 200.0 - i - 0.5)
            for i, b in enumerate(s15)
        ]
        s60 = [
            Bar(b.ts, 200.0 - i, 201.0 - i, 199.0 - i, 200.0 - i - 0.5)
            for i, b in enumerate(s60)
        ]
    return {5: Series(s5), 15: Series(s15), 60: Series(s60)}


def _minutes(figi=FIGI, *, last_open=200.0, drop_at=None, drop_low=None):
    """1m bars from T to T+14; the T+5 open is where C7 would enter."""
    out = []
    for i in range(15):
        open_ = last_open + i
        low = open_ - 1.0
        if drop_at is not None and i == drop_at:
            low = drop_low
        out.append(Bar(T + timedelta(minutes=i), open_, open_ + 1.0, low, open_ + 0.5))
    return out


SETUPS = [{"strategy_id": "setup0"}, {"strategy_id": "setup1"}]


def _maps(figi=FIGI, *, buy=True, confirmation_close=102.0):
    side = "BUY" if buy else "SELL"
    signal_label = T - timedelta(minutes=15)
    per = {"setup0": {signal_label: {side}}, "setup1": {signal_label: {side}}}
    return {"15": {figi: per}, "5": {figi: {"setup0": {}, "setup1": {}}}}


def _snaps(figis=(FIGI,), *, members=None, hour=T):
    selected = [
        Candidate(ticker=TICKER, figi=f, lot=1, reasons=[])
        for f in (members if members is not None else figis)
    ]
    return {hour: {"selected": selected, "all": selected, "admitted": selected}}


def _exec_cfg(cash=20000.0, slots=4, budget=4000.0):
    return {"initial_cash": cash, "max_positions": slots, "entry_budget_rub": budget}


def _as_tf_map(series, figis):
    """Accept both ``{figi: {tf: Series}}`` and ``{tf: {figi: Series}}``."""
    keys = set(series)
    if keys <= {5, 15, 60}:
        per_tf = {tf: dict(cells) for tf, cells in series.items()}
    else:
        per_tf = {5: {}, 15: {}, 60: {}}
        for figi, cells in series.items():
            for tf, cell in cells.items():
                per_tf[tf][figi] = cell
    return {tf: per_tf.get(tf, {}) for tf in (5, 15, 60)}


def _run(*, series=None, minutes=None, snaps=None, maps=None, by_figi=None,
         exec_cfg=None, figis=(FIGI,), add_confirmation=True):
    series = _as_tf_map(series or {FIGI: _series()}, figis)
    minutes = minutes or {FIGI: _minutes()}
    series5 = series[5]
    if add_confirmation:
        for figi in figis:
            if not any(b.ts == T for b in series5[figi].bars):
                series5[figi] = Series(
                    list(series5[figi].bars)
                    + [Bar(T, 100.0, 103.0, 99.5, 102.5)]
                )
    snaps = snaps if snaps is not None else _snaps()
    maps = maps or _maps()
    by_figi = by_figi or {FIGI: {"ticker": TICKER, "lot": 1}}
    cost = CostModel(commission_rate=0.0005, slippage_bps=2.0, tick_size=0.0)
    return pp.run_paired_portfolio(
        figis=tuple(figis), series=series, by_figi=by_figi, minutes_1m=minutes,
        snaps=snaps, maps=maps, setups=SETUPS,
        cost=cost, exec_cfg=exec_cfg or _exec_cfg(),
        eval_start=T - timedelta(days=1), eval_end=T + timedelta(hours=1),
    )


def _arm(results, arm):
    return results[arm]


# --- 1. happy path: control at T, C7 at T+5 ---------------------------------


def test_control_enters_at_t_and_c7_at_t_plus_five():
    results = _run()
    control = _arm(results, pp.CONTROL_ARM).portfolio
    c7 = _arm(results, pp.C7_ARM).portfolio
    assert len(control.positions) == 1 and len(c7.positions) == 1
    assert control.positions[FIGI].entry_ts == T
    assert control.positions[FIGI].entry_raw == 200.0
    assert c7.positions[FIGI].entry_ts == T + timedelta(minutes=5)
    assert c7.positions[FIGI].entry_raw == 205.0
    assert c7.positions[FIGI].entry_signal_ts == T - timedelta(minutes=15)


# --- 2. lost Universe membership between T and T+5 --------------------------


def test_signal_that_leaves_the_universe_at_t_plus_five_does_not_enter():
    results = _run(snaps=_snaps(members=()))
    c7 = _arm(results, pp.C7_ARM)
    assert c7.portfolio.positions == {}
    assert any(c["reason"] == "not_member" for c in c7.cancelled)


# --- 3. trend turns before execution ---------------------------------------


def test_trend_turning_before_execution_cancels_the_confirmation():
    results = _run(series={FIGI: _series(trend_up=False)})
    c7 = _arm(results, pp.C7_ARM)
    assert c7.portfolio.positions == {}
    reasons = {c["reason"] for c in c7.cancelled}
    assert reasons & {"trend_gate", "trend_missing_or_stale"}


# --- 4. confirmation never appears -----------------------------------------


def test_missing_confirmation_cancels_and_invents_no_price():
    minutes = {FIGI: _minutes()}
    results = _run(minutes=minutes, add_confirmation=False)
    c7 = _arm(results, pp.C7_ARM)
    assert c7.portfolio.positions == {}
    assert any("CANCEL" in c["reason"] or c["reason"] == "no_decision" for c in c7.cancelled)


# --- 5. slot taken by a competing ticker -----------------------------------


def test_slot_taken_by_another_ticker_blocks_the_second_entry():
    other = "BBG004S681W1"
    series = {FIGI: _series(), other: _series(other)}
    minutes = {FIGI: _minutes(), other: _minutes()}
    series5 = {FIGI: series[FIGI][5], other: series[other][5]}
    maps = {
        "15": {
            FIGI: {"setup0": {T - timedelta(minutes=15): {"BUY"}},
                   "setup1": {T - timedelta(minutes=15): {"BUY"}}},
            other: {"setup0": {T - timedelta(minutes=15): {"BUY"}},
                    "setup1": {T - timedelta(minutes=15): {"BUY"}}},
        },
        "5": {FIGI: {"setup0": {}, "setup1": {}},
              other: {"setup0": {}, "setup1": {}}},
    }
    by_figi = {FIGI: {"ticker": TICKER, "lot": 1}, other: {"ticker": "GAZP", "lot": 1}}
    results = _run(
        series={5: series5, 15: {FIGI: series[FIGI][15], other: series[other][15]},
                60: {FIGI: series[FIGI][60], other: series[other][60]}},
        minutes=minutes,
        snaps=_snaps(figis=(FIGI, other), members=(FIGI, other)),
        maps=maps, by_figi=by_figi, exec_cfg=_exec_cfg(slots=1),
        figis=(FIGI, other),
    )
    c7 = _arm(results, pp.C7_ARM)
    assert len(c7.portfolio.positions) == 1
    assert any(c["reason"] == "max_positions" for c in c7.cancelled)


# --- 6. stop hit between 5m boundaries -------------------------------------


def test_stop_is_hit_between_five_minute_boundaries():
    series = {FIGI: _series()}
    results = _run(series=series, minutes={FIGI: _minutes(drop_at=8, drop_low=150.0)})
    c7 = _arm(results, pp.C7_ARM).portfolio
    assert c7.positions == {}
    assert len(c7.trades) == 1
    assert "stop" in c7.trades[0]["reason"].lower()


# --- 7. duplicated boundary cannot duplicate an entry ----------------------


def test_repeated_signal_cannot_enter_twice():
    results = _run()
    c7 = _arm(results, pp.C7_ARM).portfolio
    entries = [e for e in c7.events if e["kind"] == "ENTRY"]
    assert len(entries) == 1


# --- 8. accounting comes from the shared contract --------------------------


def test_fees_lots_and_slippage_match_the_shared_portfolio():
    series = {FIGI: _series()}
    results = _run(series=series, minutes={FIGI: _minutes(drop_at=8, drop_low=150.0)})
    pf = _arm(results, pp.C7_ARM).portfolio
    trade = pf.trades[0]
    assert trade["net"] == trade["fill_gross"] - trade["fees"]
    assert abs(trade["identity_gap"]) < 1e-9
    assert trade["entry_commission"] > 0
    notional = trade["entry_fill"] * trade["qty"]
    assert notional <= 4000.0 + 1e-9


# --- 9. cash that cannot cover the fee-inclusive budget ---------------------


def test_cash_shortage_never_becomes_a_partial_entry():
    results = _run(exec_cfg=_exec_cfg(cash=100.0, budget=4000.0))
    c7 = _arm(results, pp.C7_ARM)
    assert c7.portfolio.positions == {}
    assert c7.portfolio.cash == 100.0
    assert c7.portfolio.rejected or any(
        c["reason"] in {"insufficient_cash", "zero_lots", "entry_budget_exceeded"}
        for c in c7.cancelled
    )


def test_missing_confirmation_minute_expires_at_first_later_event():
    minutes = [bar for bar in _minutes() if bar.ts <= T + timedelta(minutes=6)
               and bar.ts != T + timedelta(minutes=5)]
    result = _arm(_run(minutes={FIGI: minutes}), pp.C7_ARM)
    assert result.intents_expired == 1
    assert not result.resolved and not result.portfolio.positions


def test_end_of_data_accounts_for_every_unresolved_signal():
    minutes = [bar for bar in _minutes() if bar.ts < T + timedelta(minutes=5)]
    result = _arm(_run(minutes={FIGI: minutes}), pp.C7_ARM)
    assert len(result.signals) == 1
    assert len(result.resolved) + len(result.cancelled) + result.intents_expired == len(result.signals)
    assert result.cancelled[0]['reason'] == 'END_OF_DATA_UNRESOLVED'
    assert result.cancelled[0]['at'] == (T + timedelta(minutes=4)).isoformat()
    assert not result.portfolio.trades and not result.portfolio.positions


def _missing_sell_run(policy, next_open=201.0, include_next=True):
    minutes = [Bar(T + timedelta(minutes=i), 200, 201, 199, 200) for i in range(15)]
    if include_next:
        minutes.append(Bar(T + timedelta(minutes=16), next_open, next_open+1, next_open-1, next_open))
    maps = _maps()
    for setup in SETUPS:
        maps['15'][FIGI][setup['strategy_id']][T] = {'SELL'}
    cfg = {**_exec_cfg(), 'signal_exit_policy': policy}
    clock = [Bar(T + timedelta(minutes=15), 100, 101, 99, 100)]
    return _run(minutes={FIGI: minutes, 'CLOCK_ONLY': clock}, maps=maps, exec_cfg=cfg)


def test_paired_persistent_exit_closes_next_real_open_in_both_arms():
    old = _missing_sell_run('exact_open_v1')
    new = _missing_sell_run('next_available_open_v1')
    for arm in (pp.CONTROL_ARM, pp.C7_ARM):
        assert FIGI in old[arm].portfolio.positions
        assert not old[arm].portfolio.trades
        pf = new[arm].portfolio
        assert not pf.positions
        assert len(pf.trades) == 1
        trade = pf.trades[0]
        assert trade['exit_ts'] == (T + timedelta(minutes=16)).isoformat()
        assert trade['exit_raw'] == 201
        assert trade['reason'] == 'ENSEMBLE_SELL'
        assert abs(trade['identity_gap']) < 1e-9
        assert FIGI in pf.path_uncertain_figis


def test_paired_gap_stop_precedes_persistent_exit():
    result = _missing_sell_run('next_available_open_v1', next_open=150)
    for arm in (pp.CONTROL_ARM, pp.C7_ARM):
        trades = result[arm].portfolio.trades
        assert len(trades) == 1
        assert trades[0]['reason'] == 'stop_loss'
        assert trades[0]['exit_raw'] == 150


def test_paired_end_records_unresolved_exit_without_fabricated_fill():
    result = _missing_sell_run('next_available_open_v1', include_next=False)
    for arm in (pp.CONTROL_ARM, pp.C7_ARM):
        pf = result[arm].portfolio
        assert FIGI in pf.positions
        assert not pf.trades
        events = [e for e in pf.events if e['kind'] == 'EXIT_INTENT_UNRESOLVED']
        assert len(events) == 1
