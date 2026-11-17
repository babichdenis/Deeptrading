import asyncio
from datetime import datetime, timedelta, timezone

from app.bot.universe.discovery import discover_broker_universe

NOW = datetime(2026, 11, 15, 23, tzinfo=timezone.utc)


class Database:
    def __init__(self):
        self.queries = []

    async def execute(self, query):
        self.queries.append(str(query))
        return self

    def all(self):
        return [('A', 'broken-name', 99, 10, 1000, 'energy'),
                ('B', 'B', 10, 20, 2000, None),
                ('INDEX', 'IMOEX', 10, 30, 3000, None)]


def catalog():
    base = dict(uid='uid', class_code='TQBR', lot=1, api_trade_available_flag=True,
                buy_available_flag=True, sell_available_flag=True, short_enabled_flag=False)
    return {figi: dict(base, figi=figi, ticker=figi) for figi in ('A', 'B')}


def discover(data=None, observed_at=NOW, side='long'):
    db = Database()
    snapshot, decisions = asyncio.run(discover_broker_universe(
        db, broker_instruments=catalog() if data is None else data,
        observed_at=observed_at, as_of=NOW, side=side))
    return snapshot, decisions, db


def test_all_watchlist_rows_reach_broker_filter_without_tiers():
    snapshot, decisions, db = discover()
    assert 'eligible_tier' not in db.queries[0]
    assert len(decisions) == 3
    assert [e.ref.ticker for e in snapshot.entries] == ['A', 'B']
    assert snapshot.as_of == NOW
    assert all(e.lot == 1 for e in snapshot.entries)
    assert decisions[-1].reasons == ('broker_identity_unknown',)


def test_missing_or_stale_catalog_never_admits_candidates():
    for data, observed in (({}, NOW), (catalog(), NOW - timedelta(hours=2))):
        snapshot, decisions, _ = discover(data, observed)
        assert not snapshot.entries
        assert len(decisions) == 3
        assert all(not decision.eligible for decision in decisions)


def test_long_and_short_candidate_populations_are_separate():
    data = catalog()
    data['A']['short_enabled_flag'] = True
    snapshot, decisions, _ = discover(data, side='short')
    assert [e.ref.figi for e in snapshot.entries] == ['A']
    assert 'short_enabled_not_confirmed' in decisions[1].reasons


def test_matching_dictionary_key_cannot_hide_wrong_instrument():
    data = catalog()
    data['A']['figi'] = 'OTHER'
    snapshot, decisions, _ = discover(data)
    assert [e.ref.figi for e in snapshot.entries] == ['B']
    assert 'broker_identity_mismatch' in decisions[0].reasons
