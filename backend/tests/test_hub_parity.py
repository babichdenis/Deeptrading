"""Parity двух CandleHub на одной синтетической ленте.

app.engine.candlehub принимает уже закрытые 1m и сам собирает 5min
(ts бара = close бакета). app.marketdata.hub принимает бары таймфрейма
подписки и держит последний FORMING, пока не придёт следующий ts.

Лента одна и та же (ts + OHLCV). Сверяем контракт, который уже должен
совпадать: набор ts закрытых 5m-свечей, число CLOSED, forming-обновления
не закрывают бар, дубликат не двигает sequence и не даёт второй CLOSED.

Расхождения, которые этот прогон не выравнивает (engine не трогаем):
- повтор того же ts: engine считает dup (первые значения остаются),
  marketdata обновляет forming (последний close, high=max, low=min,
  volume=max). CLOSED нет ни там, ни там;
- опоздавшая 1m с новым ts: engine вставляет её в 1m-ряд и пересобирает
  5m на месте (тот же ts бакета, OHLCV может измениться, нового CLOSED нет).
  marketdata такой бар дропает (out_of_order) и закрытые бары не переписывает;
- engine закрывает 1m сразу, marketdata держит последний бар forming;
- forming.ts у engine — правая граница бакета (10:30), у marketdata — ts
  события (10:26);
- дыру engine отдаёт только через gap_report(), marketdata шлёт CANDLE_GAP.
  Пустые бакеты не создаёт ни один.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.engine.candlehub import CandleHub as EngineHub
from app.engine.models import Candle as EngineCandle
from app.marketdata import Candle as MdCandle
from app.marketdata import CandleHub as MdHub
from app.marketdata import CandleState

FIGI = "BBGTESTHUB"
T0 = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
TF = "5min"


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _ohlc(close: float, volume: float = 10.0) -> dict[str, float]:
    return {
        "open": 100.0,
        "high": max(101.0, close),
        "low": min(99.0, close),
        "close": close,
        "volume": volume,
    }


def test_hub_parity_on_shared_tape():
    eng = EngineHub()
    series_5 = eng.series(FIGI, TF)
    eng_closed: list[EngineCandle] = []
    series_5.on_closed.append(eng_closed.append)

    md = MdHub()
    md_closed: list = []
    md.on(CandleState.CLOSED, md_closed.append)
    asyncio.run(md.subscribe(FIGI, TF))
    state = md.get_subscription(FIGI, TF)
    assert state is not None

    def push(minute: int, close: float, volume: float = 10.0) -> str:
        values = _ohlc(close, volume)
        action = eng.ingest_1m(FIGI, EngineCandle(ts=_at(minute), **values))
        md._process_candle(
            MdCandle(ts=_at(minute), figi=FIGI, timeframe=TF, source="stream", **values),
            state,
        )
        return action

    # Нормальный ряд: три закрытых 5m-бакета по границе (10:05, 10:10, 10:15).
    assert push(5, 100.0) == "append"
    assert push(10, 101.0) == "append"
    assert push(15, 102.0) == "append"

    # Дубликат уже закрытого ts. sequence и число CLOSED стоят на месте.
    seq_before = state.sequence
    closed_before = len(eng_closed)
    md_closed_before = len(md_closed)
    bars_before = len(series_5)
    assert push(10, 101.0) == "dup"
    assert state.sequence == seq_before
    assert len(eng_closed) == closed_before
    assert len(md_closed) == md_closed_before
    assert len(series_5) == bars_before
    assert sum(1 for event in md_closed if event.ts == _at(10)) == 1

    # Out-of-order: минута 10:07 внутри уже закрытого бакета 10:10.
    # 5m-набор ts не растёт ни у одного хаба.
    assert push(7, 103.0, volume=7.0) == "insert"
    assert [c.ts for c in series_5.snapshot()] == [_at(5), _at(10), _at(15)]
    assert [event.ts for event in md_closed] == [_at(5), _at(10)]

    # Gap: 10:25, бакета 10:20 нет.
    assert push(25, 104.0) == "append"

    # Первая свеча следующего бакета закрывает 10:25 у marketdata
    # (engine закрыл 10:25 сразу, потому что ts на границе) и открывает forming.
    assert push(26, 105.0, volume=3.0) == "append"
    expected = [_at(5), _at(10), _at(15), _at(25)]
    assert [c.ts for c in eng_closed] == expected
    assert [c.ts for c in series_5.snapshot()] == expected
    assert [event.ts for event in md_closed] == expected
    store = md.get_store(FIGI, TF)
    assert store is not None
    assert [c.ts for c in store.get()] == expected

    # 20 обновлений forming внутри бакета (10:25, 10:30]. CLOSED не растёт.
    seq_at_forming = state.sequence
    for i in range(20):
        assert push(26, 110.0 + i, volume=20.0 + i) == "dup"
    assert len(eng_closed) == len(expected)
    assert len(md_closed) == len(expected)
    assert [c.ts for c in series_5.snapshot()] == expected
    assert [c.ts for c in store.get()] == expected
    assert state.sequence == seq_at_forming
    assert series_5.partial is not None and series_5.partial.ts == _at(30)
    assert store.forming() is not None and store.forming().ts == _at(26)
    # engine оставил close первого тика 10:26, marketdata — close последнего апдейта
    assert series_5.partial.close == 105.0
    assert store.forming().close == 129.0

    reasons = md.stats["reasons"]
    assert reasons == {
        "received": 27,
        "duplicate": 1,
        "out_of_order": 1,
        "gap": 1,
        "corrected": 20,
        "closed": 4,
    }
    # опоздавшая 1m живёт только в engine-ряде 1min
    assert any(c.ts == _at(7) for c in eng.series(FIGI, "1min").snapshot())
    assert all(c.ts != _at(7) for c in store.get())
