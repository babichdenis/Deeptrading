"""Карточка входа сделки: причина, голоса и режим — для ЛЮБОГО движка.

Регрессия (владелец, 2026-10-02): в таблице «Последние сделки» у прогонов на
rsi_trade_hub колонка входа была пустой — не видно, кто голосовал и в каком
режиме входили. Причина: одиночные движки отдавали в meta голые фичи
({"rsi": 37.09}), карточку entry писали только ансамбль и OSE-стратегии.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot.runtime import PaperBotRuntime  # noqa: E402


class _Cfg:
    strategy_id = ""


def _rt(strategy_id: str = "", robot_name: str = "") -> PaperBotRuntime:
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.strategies = {}
    rt._entry_regime = {}
    rt.config = SimpleNamespace(strategy_id=strategy_id)
    if strategy_id:
        rt.strategies["BBG1"] = SimpleNamespace(strategy_id=strategy_id,
                                                 robot_name=robot_name)
    return rt


def test_single_engine_card_has_reason_vote_and_regime():
    rt = _rt("rsi_trade_hub")
    meta = rt._entry_card(
        "BBG1",
        {
            "rsi": 37.09, "rsi_5m": 40.1, "gate_path": ["time", "market"],
            "_sig": {"strategy_id": "rsi_trade_hub", "side": "BUY",
                     "reason": "rsi_up_down", "kind": "entry",
                     "ts": "2026-09-04T14:30:00+00:00"},
        },
        "NEUTRAL",
    )
    e = meta["entry"]
    assert e["reason"] == "rsi_trade_hub:rsi_up_down" or e["reason"] == "rsi_up_down"
    assert e["strategy_id"] == "rsi_trade_hub"
    assert e["votes"] == 1 and e["buy_votes"] == 1 and e["sell_votes"] == 0
    assert e["rsi"] == 37.09                       # RSI виден в строке таблицы
    assert e["features"]["rsi"] == 37.09           # …и в карточке деталей
    assert meta["quorum_event"]["members_for"] == ["rsi_trade_hub"]
    assert meta["quorum_event"]["total_members"] == 1
    assert meta["regime"] == "NEUTRAL"
    assert meta["engine"] == "single"
    assert "_sig" not in meta                       # служебная подсказка не пишется в БД
    assert meta["gate_path"] == ["time", "market"]  # путь гейтов не теряем


def test_short_side_votes():
    rt = _rt("rsi_trade_hub")
    meta = rt._entry_card(
        "BBG1",
        {"_sig": {"strategy_id": "rsi_trade_hub", "side": "SELL",
                  "reason": "rsi_dn_up", "ts": "2026-09-04T14:30:00+00:00"}},
        "TREND_DOWN",
    )
    e = meta["entry"]
    assert e["sell_votes"] == 1 and e["buy_votes"] == 0
    assert meta["quorum_event"]["opposition"] == ["rsi_trade_hub"]
    assert meta["regime"] == "TREND_DOWN"


def test_no_signal_hints_falls_back_to_strategy_instance():
    """Моментум/очередь/AI приходят без _sig — голос всё равно пишется (один движок)."""
    rt = _rt("macd_cross", robot_name="macd")
    meta = rt._entry_card("BBG1", {"momentum": True}, "RANGE")
    e = meta["entry"]
    assert e["strategy_id"] == "macd_cross"
    assert e["robot"] == "macd"
    assert e["votes"] == 1
    assert e["reason"].startswith("macd_cross")
    assert meta["regime"] == "RANGE"


def test_ensemble_card_is_not_overwritten():
    """Ансамбль/OSE приносят карточку сами — только добираем режим."""
    rt = _rt("ensemble_v4")
    rich = {
        "entry": {"reason": "ensemble_neutral_semi_flip:fl", "buy_votes": 2,
                  "sell_votes": 1, "features": {"rsi": 50.0}},
        "quorum_event": {"votes": 3, "members_for": ["a", "b"]},
        "setups": {"fl": {"enabled": True}},
    }
    meta = rt._entry_card("BBG1", rich, "TREND_UP")
    assert meta["entry"]["reason"] == "ensemble_neutral_semi_flip:fl"
    assert meta["quorum_event"]["votes"] == 3
    assert meta["setups"] == {"fl": {"enabled": True}}
    assert meta["regime"] == "TREND_UP"


@pytest.mark.parametrize("reg", ["NEUTRAL", "TREND_UP", "TREND_DOWN", "RANGE", "NO_REGIME"])
def test_regime_always_present(reg):
    rt = _rt("rsi_trade_hub")
    meta = rt._entry_card("BBG1", {"rsi": 1.0}, reg)
    assert meta["regime"] == reg
    assert meta["entry"]["strategy_id"] == "rsi_trade_hub"


def test_serializer_reads_engine_and_regime_from_meta():
    """_entry_sid_reg: в строке сделки — реальный движок, а не жёсткий v4_enhanced."""
    from app.api.routes.sandbox import _entry_sid_reg
    sid, reg = _entry_sid_reg('{"strategy_id": "rsi_trade_hub", "regime": "NEUTRAL"}')
    assert (sid, reg) == ("rsi_trade_hub", "NEUTRAL")
    sid, reg = _entry_sid_reg('{"entry": {"strategy_id": "macd_cross"}, "entry_regime": "TREND_UP"}')
    assert (sid, reg) == ("macd_cross", "TREND_UP")
    assert _entry_sid_reg(None) == (None, None)
    assert _entry_sid_reg("не json") == (None, None)
    assert _entry_sid_reg('{"rsi": 37.09}') == (None, None)
