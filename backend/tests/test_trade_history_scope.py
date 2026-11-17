from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, insert, text
from app.bot.runtime import PaperBotRuntime
from app.models.sandbox_trade import SandboxTrade

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


@pytest.fixture
def history_runtime(monkeypatch):
    engine = create_engine('sqlite://')
    connection = engine.connect()
    SandboxTrade.__table__.create(connection)
    connection.execute(text('CREATE TABLE universe (ticker TEXT, avg_daily_turnover REAL)'))
    rows = []
    variants = [
        ('paper', None, 'testA', -1, 10), ('paper', None, 'testA', 1, 20),
        ('paper', None, 'testB', -1, 30), ('live', 'A', None, -1, 40),
        ('live', 'B', None, -1, 50), ('sandbox', 'A', None, -1, 60),
        ('live', None, None, -1, 70), ('live', 'A', 'old-label', -1, 80)]
    for ident, (mode, account, test, offset, pnl) in enumerate(variants, 1):
        rows.append(dict(id=ident, ticker='SBER', figi='FIGI', mode=mode,
            broker_account_id=account, test_name=test, entry_time=NOW-timedelta(days=1),
            entry_price=100, exit_time=NOW+timedelta(minutes=offset), net_pnl=pnl))
    connection.execute(insert(SandboxTrade), rows)
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def execute(self, query): return connection.execute(query)
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt._database_session = Session
    rt.config = SimpleNamespace(feed='replay', test_name='testA')
    rt.broker_mode = 'paper'
    rt.broker = SimpleNamespace(_account=None)
    rt._bot_now = lambda: NOW
    rt._hist_cache = {}
    rt._hist_ts = 0
    rt.quote_probe = Mock(return_value={})
    monkeypatch.setattr('app.api.routes.screener.fetch_tqbr_market', rt.quote_probe)
    yield rt
    connection.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_replay_history_is_test_scoped_and_visible_as_of(history_runtime):
    rt = history_runtime
    assert (await rt.trade_history())['SBER']['net'] == 10
    rt._bot_now = lambda: NOW+timedelta(minutes=2)
    assert (await rt.trade_history())['SBER']['net'] == 30
    rt.config.test_name = 'testB'
    assert (await rt.trade_history())['SBER']['net'] == 30
    assert (await rt.trade_history())['SBER']['n'] == 1
    rt.quote_probe.assert_not_called()


@pytest.mark.asyncio
async def test_live_history_never_reuses_other_account_or_mode_cache(history_runtime):
    rt = history_runtime
    await rt.trade_history()
    rt.config.feed = 'live'
    rt.broker_mode = 'live'
    rt.broker._account = 'A'
    assert (await rt.trade_history())['SBER']['net'] == 120
    rt.broker._account = 'B'
    assert (await rt.trade_history())['SBER']['net'] == 50
    rt.broker_mode = 'sandbox'
    rt.broker._account = 'A'
    assert (await rt.trade_history())['SBER']['net'] == 60


@pytest.mark.asyncio
async def test_unknown_live_account_is_an_error_not_empty_history(history_runtime):
    rt = history_runtime
    rt._hist_cache = {'OLD': {'net': 999}}
    rt.broker_mode = 'live'
    with pytest.raises(ValueError):
        await rt.trade_history()

    assert rt._hist_cache == {}
