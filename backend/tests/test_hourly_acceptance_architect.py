from datetime import datetime, timedelta, timezone

import pytest

from app.bot.hourly_runtime import HourlyContextService, HourlyJournal, MinuteWindow, permits_new_entry
from app.engine.models import Candle


AT = datetime(2026, 8, 3, 10, tzinfo=timezone.utc)


class MinuteFixture:
    def __init__(self, absent=()):
        self.absent = set(absent)

    async def load(self, db, figis, *, as_of, lookback_minutes):
        bars = tuple(Candle(as_of - timedelta(minutes=61-i), 100, 100, 100, 100, 1) for i in range(61))
        return [MinuteWindow(figi, () if figi in self.absent else bars, 0 if figi in self.absent else len(bars)) for figi in figis]


def service(path, absent=()):
    return HourlyContextService(market_id='INDEX', journal=HourlyJournal(path), loader=MinuteFixture(absent), clock=lambda: AT)


@pytest.mark.asyncio
async def test_missing_market_cannot_admit_entry_from_fresh_snapshot(tmp_path):
    instance = service(tmp_path / 'context.jsonl', absent=('INDEX',))
    snapshot = await instance.ensure(db=None, now=AT, figis=('A', 'INDEX'))
    assert snapshot.context.features['A'].status == 'MARKET_UNKNOWN'
    assert not permits_new_entry(overlay=instance.emergency, snapshot=snapshot, now=AT, market_id='INDEX', target_figi='A', block_key='A')
    assert instance.status(now=AT)['state'] != 'KNOWN'


@pytest.mark.asyncio
async def test_restart_restores_usable_snapshot_without_recomputation(tmp_path):
    path = tmp_path / 'context.jsonl'
    before = service(path)
    expected = await before.ensure(db=None, now=AT, figis=('A', 'INDEX'))
    after = service(path)
    after.restore(now=AT + timedelta(minutes=5))
    restored = after.current(now=AT + timedelta(minutes=5))
    assert restored is not None
    assert restored.context.input_hash == expected.context.input_hash
    assert restored.context.features == expected.context.features
    assert after.current(now=AT + timedelta(hours=1)) is None


@pytest.mark.asyncio
async def test_journal_cannot_restore_future_snapshot_into_earlier_clock(tmp_path):
    path = tmp_path / 'context.jsonl'
    before = service(path)
    await before.ensure(db=None, now=AT, figis=('A', 'INDEX'))
    after = service(path)
    assert after.restore(now=AT - timedelta(minutes=1)) is None
    assert after.current(now=AT - timedelta(minutes=1)) is None


@pytest.mark.asyncio
async def test_requested_instrument_without_bars_remains_explicitly_unknown(tmp_path):
    instance = service(tmp_path / 'context.jsonl', absent=('MISSING',))
    snapshot = await instance.ensure(db=None, now=AT, figis=('A', 'INDEX', 'MISSING'))
    assert 'MISSING' in snapshot.context.features
    assert snapshot.context.features['MISSING'].status == 'ENDPOINT_UNKNOWN'
    assert 'MISSING' in instance.status(now=AT)['unknown_figis']


@pytest.mark.asyncio
async def test_entry_requires_known_target_not_only_known_market(tmp_path):
    instance = service(tmp_path / 'context.jsonl', absent=('MISSING',))
    snapshot = await instance.ensure(db=None, now=AT, figis=('A', 'INDEX', 'MISSING'))
    assert permits_new_entry(overlay=instance.emergency, snapshot=snapshot, now=AT, market_id='INDEX', target_figi='A')
    assert not permits_new_entry(overlay=instance.emergency, snapshot=snapshot, now=AT, market_id='INDEX', target_figi='MISSING')
    assert not permits_new_entry(overlay=instance.emergency, snapshot=snapshot, now=AT, market_id='INDEX', target_figi='UNREQUESTED')
