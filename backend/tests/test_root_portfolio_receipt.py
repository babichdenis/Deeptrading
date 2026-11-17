from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from reports.connected_week_20261109_13_c1.ROOT.portfolio_receipt import portfolio_receipt


@dataclass
class Position:
    qty: int = 10
    path_uncertainty: bool = False


T = datetime(2026, 8, 24, 21, tzinfo=timezone.utc)


def run(bars, equity=None):
    pf = SimpleNamespace(positions={'F': Position()}, cash=8999, events=[], trades=[], rejected=[], equity=equity or [])
    return portfolio_receipt(pf, {'F': bars}, T, 10000)


def test_mark_includes_paid_entry_fee_without_fabricating_exit():
    result = run([SimpleNamespace(ts=T-timedelta(minutes=1), close=101)])
    assert result['final_equity_marked'] == 10009
    assert result['net_change'] == 9
    assert result['trades'] == []
    assert result['future_exit_costs_included'] is False


def test_future_close_is_invisible_and_stale_mark_is_unknown():
    result = run([SimpleNamespace(ts=T, close=200), SimpleNamespace(ts=T-timedelta(minutes=7), close=101)])
    assert result['net_change'] is None
    assert result['final_equity_marked'] is None
    assert result['missing_final_marks'] == ['F']


def test_known_terminal_mark_does_not_erase_unknown_path():
    bars = [SimpleNamespace(ts=T-timedelta(minutes=1), close=101)]
    result = run(bars, [{'equity': None}])
    assert result['conditional_marked_change'] == 9
    assert result['net_change'] is None
    assert result['ledger_sha256'] == run(bars, [{'equity': None}])['ledger_sha256']
    assert result['ledger_sha256'] != run(bars)['ledger_sha256']


def test_closed_position_does_not_erase_execution_uncertainty():
    pf = SimpleNamespace(positions={}, cash=10020, events=[], trades=[], rejected=[],
                         equity=[{'equity': 10020}], path_uncertain_figis={'F'})
    result = portfolio_receipt(pf, {}, T, 10000)
    assert result['final_equity_marked'] == 10020
    assert result['conditional_marked_change'] == 20
    assert result['net_change'] is None
    assert result['path_uncertain_figis'] == ['F']
    pf.path_uncertain_figis = set()
    assert result['ledger_sha256'] != portfolio_receipt(pf, {}, T, 10000)['ledger_sha256']
