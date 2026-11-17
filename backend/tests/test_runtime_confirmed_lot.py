import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed,stored', [(1000, 1), (10, 1000), (1, 10)])
async def test_confirmed_broker_lot_overrides_old_universe_and_db(confirmed, stored):
    source = (Path(__file__).resolve().parents[1] / 'app/bot/runtime.py').read_text(encoding="utf-8")
    begin = source.index('            if confirmed_broker_lot is not None:', source.index('    async def _submit_order('))
    end = source.index('            import math', begin)
    body = textwrap.dedent(source[begin:end])
    namespace = {}
    exec('async def resolve(self, confirmed_broker_lot, figi):\n' + textwrap.indent(body, '    ') + '\n    return lot', namespace)
    rt = SimpleNamespace(universe=[dict(figi='A', lot=stored, lot_size=stored)])
    assert await namespace['resolve'](rt, confirmed, 'A') == confirmed
