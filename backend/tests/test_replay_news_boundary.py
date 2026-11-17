import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import news


@pytest.mark.asyncio
@pytest.mark.parametrize('feed,enabled,expected_calls', [('replay', True, 0), ('live', True, 1), ('live', False, 0)])
async def test_runtime_news_block_uses_live_fetch_only_for_live_clock(monkeypatch, feed, enabled, expected_calls):
    source = Path('app/bot/runtime.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    blocks = [node for node in ast.walk(tree) if isinstance(node, ast.If)
              and 'entry_news_blackout' in ast.unparse(node.test)]
    assert len(blocks) == 1
    wrapper = ast.parse('async def run(cfg, ticker):\n    _nb_reason = ""\n    return _nb_reason\n')
    wrapper.body[0].body.insert(1, blocks[0])
    scope = {}
    exec(compile(ast.fix_missing_locations(wrapper), '<runtime-news-block>', 'exec'), scope)
    calls = []
    def fetch():
        calls.append('fetch')
        return []
    monkeypatch.setattr(news, 'fetch_news', fetch)
    monkeypatch.setattr(news, 'blackout_reason', lambda items, ticker, minutes: 'negative_news')
    cfg = SimpleNamespace(feed=feed, entry_news_blackout=enabled, entry_news_blackout_min=60)
    result = await scope['run'](cfg, 'SBER')
    assert len(calls) == expected_calls
    assert result == ('negative_news' if expected_calls else '')
