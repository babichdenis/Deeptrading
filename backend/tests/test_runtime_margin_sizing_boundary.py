import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.paper_broker import PaperBroker


@pytest.fixture
def sizing_block():
    from app.bot.live_broker import LiveBroker

    source = (Path(__file__).resolve().parents[1] / 'app/bot/runtime.py').read_text(encoding='utf-8')
    begin = source.index('            lev = max(1.0, float(cfg.leverage or 1.0))') if '            lev = max(1.0, float(cfg.leverage or 1.0))' in source else source.index('            lev = 1.0\n            lot_cost = price * lot')
    end = source.index('            _used_lev = lev', begin) + len('            _used_lev = lev')
    body = textwrap.dedent(source[begin:end])
    arguments = 'self, cfg, side, _sizing, _is_priority, _is_ai, _is_momentum, ticker, figi, budget, price, lot'
    code = 'async def calculate(' + arguments + '):\n' + textwrap.indent(body, '    ') + '\n    return qty, _used_lev\n'
    scope = {'PaperBroker': PaperBroker, 'LiveBroker': LiveBroker, 'action': 'open'}
    exec(compile(code, '<runtime sizing boundary>', 'exec'), scope)
    return scope['calculate']


@pytest.mark.asyncio
@pytest.mark.parametrize('side', ['BUY', 'SELL'])
@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('inside_session', [False, True])
@pytest.mark.parametrize('sizing', ['divide', 'multiply'])
@pytest.mark.parametrize('broker_failure', [False, True])
async def test_actual_sizing_respects_margin_permission(monkeypatch, sizing_block, side, enabled, inside_session, sizing, broker_failure):
    import app.bot.session as sessions

    monkeypatch.setattr(sessions, 'session_state', lambda **kwargs: 'TRADING')
    monkeypatch.setattr(sessions, 'trading_session', lambda **kwargs: 'day' if inside_session else 'evening')
    broker = object.__new__(PaperBroker)
    limits = SimpleNamespace(buy_cash=100, sell_cash=100, buy_margin=600, sell_margin=600, leverage=6.0)
    broker.get_max_lots = AsyncMock(side_effect=RuntimeError('unavailable') if broker_failure else None, return_value=limits)
    rejected = []
    context = SimpleNamespace(broker=broker, _sector_meta={}, _bot_now=lambda: None,
                              _log=lambda *args, **kwargs: None, _trace_order_reject=lambda *args: rejected.append(args))
    cfg = SimpleNamespace(leverage=3.0, use_margin=enabled, margin_sessions=['day'], margin_sizing=sizing, margin_leverage=0.0)
    result = await sizing_block(context, cfg, side, sizing, False, False, False, 'TEST', 'TEST', 10000.0, 100.0, 10)
    if broker_failure:
        assert result is None
        assert rejected and rejected[0][4] == 'sizing_margin_unknown'
        return
    assert not rejected
    expected_leverage = 6.0 if enabled and inside_session else 1.0
    expected_lots = 60 if expected_leverage == 6.0 and sizing == 'multiply' else 10
    assert result == (expected_lots, expected_leverage)
