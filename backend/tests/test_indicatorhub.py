"""Тесты IndicatorHub — кэш индикаторов поверх CandleHub."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.engine.indicatorhub import IndicatorHub, INDICATORS
from app.engine.models import Candle


def test_envelops_reference_vector():
    """Канон конвертов: SMA ± deviation% от уровня (reference-вектор вручную)."""
    from app.engine.indicatorhub import _envelops
    candles = [
        Candle(ts=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i),
               open=float(v), high=float(v), low=float(v), close=float(v))
        for i, v in enumerate((1, 2, 3, 4))
    ]
    out = _envelops(candles, 2, 10.0)
    assert out["center"] == [None, 1.5, 2.5, 3.5]
    assert out["up"][3] == pytest.approx(3.85)
    assert out["down"][3] == pytest.approx(3.15)
    assert out["up"][0] is None and out["down"][0] is None


def _candles(n: int = 100, start: datetime | None = None) -> list[Candle]:
    if start is None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    import math

    out = []
    for i in range(n):
        ts = start + timedelta(minutes=i)
        price = 100.0 + 10.0 * math.sin(i / 10.0)
        out.append(
            Candle(
                ts=ts,
                open=price - 0.25,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=1000.0 + i * 10,
            )
        )
    return out


def test_registry_categories():
    categories = {d.category for d in INDICATORS.values()}
    assert "trend" in categories
    assert "momentum" in categories
    assert "volatility" in categories
    assert "volume" in categories
    assert "price_structure" in categories


def test_subscribe_and_update():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "sma", {"length": 20})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    val = hub.get("SBER", 60, "sma", {"length": 20})
    assert val is not None
    assert val > 0


def test_unsubscribe():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "sma", {"length": 20})
    hub.unsubscribe("SBER", 60, "sma", {"length": 20})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    assert hub.get("SBER", 60, "sma", {"length": 20}) is None


def test_preview_does_not_commit():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "sma", {"length": 20})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    before = hub.get("SBER", 60, "sma", {"length": 20})
    hub.update("SBER", 60, candles, is_closed=False)
    after = hub.get("SBER", 60, "sma", {"length": 20})
    assert before == after


def test_get_series():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "sma", {"length": 20})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    series = hub.get_series("SBER", 60, "sma", {"length": 20}, n=10)
    assert len(series) == 10
    assert series[0][0] < series[-1][0]


def test_warmup():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "rsi", {"length": 14})
    candles = _candles(200)
    hub.warmup("SBER", 60, candles)
    val = hub.get("SBER", 60, "rsi", {"length": 14})
    assert val is not None
    assert 0 <= val <= 100


def test_multi_row_indicator():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "bollinger", {"length": 20, "mult": 2.0})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    up = hub.get_row("SBER", 60, "bollinger", "up", {"length": 20, "mult": 2.0})
    center = hub.get_row("SBER", 60, "bollinger", "center", {"length": 20, "mult": 2.0})
    down = hub.get_row("SBER", 60, "bollinger", "down", {"length": 20, "mult": 2.0})
    assert up is not None
    assert center is not None
    assert down is not None
    assert up > center > down


def test_adx_structure():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "adx", {"length": 14})
    candles = _candles(100)
    hub.update("SBER", 60, candles, is_closed=True)
    adx = hub.get("SBER", 60, "adx", {"length": 14})
    assert adx is not None
    assert adx >= 0


def test_macd_structure():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "macd", {"fast": 12, "slow": 26, "signal": 9})
    candles = _candles(100)
    hub.update("SBER", 60, candles, is_closed=True)
    macd = hub.get("SBER", 60, "macd", {"fast": 12, "slow": 26, "signal": 9})
    assert macd is not None


def test_donchian():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "donchian", {"length": 20})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    upper = hub.get_row("SBER", 60, "donchian", "upper", {"length": 20})
    lower = hub.get_row("SBER", 60, "donchian", "lower", {"length": 20})
    assert upper is not None
    assert lower is not None
    assert upper > lower


def test_returns():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "returns", {"length": 1})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    val = hub.get("SBER", 60, "returns", {"length": 1})
    assert val is not None


def test_range_atr():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "range_atr", {"length": 14})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    val = hub.get("SBER", 60, "range_atr", {"length": 14})
    assert val is not None
    assert val > 0


def test_volume_sma():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "volume_sma", {"length": 20})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    val = hub.get("SBER", 60, "volume_sma", {"length": 20})
    assert val is not None
    assert val > 0


def test_relative_volume():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "relative_volume", {"length": 20})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    val = hub.get("SBER", 60, "relative_volume", {"length": 20})
    assert val is not None
    assert val > 0


def test_rolling_high_low():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "rolling_high_low", {"length": 20})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    high = hub.get_row("SBER", 60, "rolling_high_low", "high", {"length": 20})
    low = hub.get_row("SBER", 60, "rolling_high_low", "low", {"length": 20})
    assert high is not None
    assert low is not None
    assert high > low


def test_candle_body():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "candle_body", {})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    val = hub.get("SBER", 60, "candle_body", {})
    assert val is not None


def test_wicks():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "wicks", {})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    upper = hub.get_row("SBER", 60, "wicks", "upper", {})
    lower = hub.get_row("SBER", 60, "wicks", "lower", {})
    assert upper is not None
    assert lower is not None


def test_range():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "range", {})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    val = hub.get("SBER", 60, "range", {})
    assert val is not None
    assert val >= 0


def test_empty_candles():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "sma", {"length": 20})
    hub.update("SBER", 60, [], is_closed=True)
    assert hub.get("SBER", 60, "sma", {"length": 20}) is None


def test_unknown_indicator():
    hub = IndicatorHub()
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    assert hub.get("SBER", 60, "unknown") is None


def test_different_figi_isolated():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "sma", {"length": 20})
    hub.subscribe("GAZP", 60, "sma", {"length": 20})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    hub.update("GAZP", 60, candles, is_closed=True)
    assert hub.get("SBER", 60, "sma", {"length": 20}) is not None
    assert hub.get("GAZP", 60, "sma", {"length": 20}) is not None


def test_different_tf_isolated():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "sma", {"length": 20})
    hub.subscribe("SBER", 300, "sma", {"length": 20})
    candles = _candles(50)
    hub.update("SBER", 60, candles, is_closed=True)
    hub.update("SBER", 300, candles, is_closed=True)
    assert hub.get("SBER", 60, "sma", {"length": 20}) is not None
    assert hub.get("SBER", 300, "sma", {"length": 20}) is not None


def test_rsi_independent_timeframes():
    """Acceptance test: RSI(SBER, 1m/5m/15m) — три независимых состояния.

    Каждое значение обязано совпасть с прямым расчётом по ЗАКРЫТЫМ барам
    своего TF (build_tf из 1m). Если бы IndicatorHub считал 5m и 15m из
    1m-ряда, значения совпали бы с 1m и тест упал бы.
    """
    from app.engine.candlehub import build_tf
    from app.engine.indicatorhub import _rsi

    hub = IndicatorHub()
    params = {"length": 14}
    for tf in (60, 300, 900):
        hub.subscribe("SBER", tf, "rsi", params)

    candles_1m = _candles(1500)
    candles_5m = build_tf(candles_1m, "5min")
    candles_15m = build_tf(candles_1m, "15min")
    assert len(candles_5m) > 50 and len(candles_15m) > 20

    hub.update("SBER", 60, candles_1m, is_closed=True)
    hub.update("SBER", 300, candles_5m, is_closed=True)
    hub.update("SBER", 900, candles_15m, is_closed=True)

    for tf, series in ((60, candles_1m), (300, candles_5m), (900, candles_15m)):
        expected = _rsi(series, 14)[-1]
        assert hub.get_at("SBER", tf, "rsi", params, ts=series[-1].ts) == expected
        assert hub.get("SBER", tf, "rsi", params) == expected

    rsi_1m = hub.get("SBER", 60, "rsi", params)
    rsi_5m = hub.get("SBER", 300, "rsi", params)
    rsi_15m = hub.get("SBER", 900, "rsi", params)
    assert len({rsi_1m, rsi_5m, rsi_15m}) == 3


def test_required_bars_depend_on_params():
    assert INDICATORS["sma"].required_bars({"length": 5}) == 5
    assert INDICATORS["sma"].required_bars({"length": 20}) == 20
    assert INDICATORS["rsi"].required_bars({"length": 14}) == 15
    assert INDICATORS["adx"].required_bars({"length": 14}) == 28
    assert INDICATORS["donchian"].required_bars({"length": 20}) == 21
    assert INDICATORS["macd"].required_bars() == 34
    assert INDICATORS["stochastic"].required_bars({"k": 14, "d": 3}) == 16
    assert INDICATORS["ema"].required_bars({"length": 200}) == 200
    assert INDICATORS["range"].required_bars() == 1


def test_registry_metadata_is_declared():
    for name, definition in INDICATORS.items():
        assert definition.name == name
        assert definition.category
        assert definition.primary
        assert isinstance(definition.stateful, bool)
        assert isinstance(definition.supports_preview, bool)
        assert definition.required_bars() >= 1
        for dep in definition.dependencies:
            assert dep in INDICATORS, (name, dep)
        for spec in definition.parameters:
            assert spec.name and spec.type in (int, float)
            assert definition.default_params()[spec.name] is not None


def test_supports_preview_false_is_skipped_in_preview():
    from app.engine.indicatorhub import _sma, IndicatorDefinition

    INDICATORS["no_preview"] = IndicatorDefinition(
        name="no_preview",
        category="test",
        parameters=(_spec_length(),),
        calculate=lambda c, p: {"value": _sma([x.close for x in c], 1)},
        warmup=lambda p: 1,
        supports_preview=False,
    )
    try:
        hub = IndicatorHub()
        hub.subscribe("SBER", 60, "no_preview")
        candles = _candles(5)
        hub.commit("SBER", 60, candles)
        assert hub.get("SBER", 60, "no_preview") is not None
        assert hub.preview("SBER", 60, candles) == {}
    finally:
        del INDICATORS["no_preview"]


def test_preview_labels_distinguish_params():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "sma", {"length": 5})
    hub.subscribe("SBER", 60, "sma", {"length": 20})
    out = hub.preview("SBER", 60, _candles(50))
    assert len(out) == 2
    assert all(key.startswith("sma:") for key in out)


def _spec_length():
    from app.engine.indicatorhub import _spec

    return _spec("length", int, 1, minimum=1)


def test_required_bars_first_value_is_not_none():
    for name, params in (
        ("sma", {"length": 20}),
        ("ema", {"length": 20}),
        ("rsi", {"length": 14}),
        ("atr", {"length": 14}),
        ("adx", {"length": 14}),
        ("macd", {}),
        ("bollinger", {"length": 20, "mult": 2.0}),
        ("stochastic", {"k": 14, "d": 3}),
        ("donchian", {"length": 20}),
        ("vwap", {"length": 20}),
        ("rolling_high_low", {"length": 20}),
        ("returns", {"length": 1}),
        ("range_atr", {"length": 14}),
        ("volume_sma", {"length": 20}),
        ("relative_volume", {"length": 20}),
        ("candle_body", {}),
        ("wicks", {}),
        ("range", {}),
    ):
        definition = INDICATORS[name]
        resolved = definition.resolve(params)
        need = definition.required_bars(params)
        rows = definition.calculate(_candles(need), resolved)
        firsts = {row: values[need - 1] for row, values in rows.items()}
        assert all(v is not None for v in firsts.values()), (name, firsts)


def test_custom_params_are_independent():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "sma", {"length": 5})
    hub.subscribe("SBER", 60, "sma", {"length": 20})
    hub.update("SBER", 60, _candles(50), is_closed=True)
    short = hub.get("SBER", 60, "sma", {"length": 5})
    long = hub.get("SBER", 60, "sma", {"length": 20})
    assert short is not None and long is not None
    assert short != long
    assert len(hub.get_series("SBER", 60, "sma", {"length": 5})) == 46
    assert len(hub.get_series("SBER", 60, "sma", {"length": 20})) == 31


def test_partial_params_resolve_to_defaults():
    hub = IndicatorHub()
    hub.subscribe("SBER", 60, "bollinger", {"length": 10})
    hub.update("SBER", 60, _candles(50), is_closed=True)
    with_default_mult = hub.get("SBER", 60, "bollinger", {"length": 10, "mult": 2.0})
    assert hub.get("SBER", 60, "bollinger", {"length": 10}) == with_default_mult


def test_unknown_param_rejected():
    hub = IndicatorHub()
    with pytest.raises(ValueError):
        hub.subscribe("SBER", 60, "sma", {"lenght": 20})
    with pytest.raises(ValueError):
        hub.subscribe("SBER", 60, "sma", {"length": 0})


def test_unknown_indicator_rejected():
    hub = IndicatorHub()
    with pytest.raises(KeyError):
        hub.subscribe("SBER", 60, "no_such_indicator")


def test_warmup_below_requirement_is_not_ready():
    hub = IndicatorHub()
    params = {"length": 20}
    hub.subscribe("SBER", 60, "sma", params)
    hub.warmup("SBER", 60, _candles(19))
    assert not hub.is_ready("SBER", 60, "sma", params)
    hub.warmup("SBER", 60, _candles(20))
    assert hub.is_ready("SBER", 60, "sma", params)


def test_warmup_is_deterministic():
    params = {"length": 14}
    candles = _candles(300)
    first = IndicatorHub()
    first.subscribe("SBER", 60, "rsi", params)
    first.warmup("SBER", 60, candles)
    second = IndicatorHub()
    second.subscribe("SBER", 60, "rsi", params)
    second.warmup("SBER", 60, candles)
    assert first.get_series("SBER", 60, "rsi", params) == \
        second.get_series("SBER", 60, "rsi", params)


def test_preview_includes_forming_without_committing():
    hub = IndicatorHub()
    params = {"length": 3}
    hub.subscribe("SBER", 60, "sma", params)
    candles = _candles(10)
    hub.commit("SBER", 60, candles)
    closed_value = hub.get("SBER", 60, "sma", params)

    forming = candles[-1]
    forming = Candle(
        ts=forming.ts + timedelta(minutes=1), open=forming.close,
        high=forming.close + 10.0, low=forming.close - 10.0,
        close=forming.close + 5.0, volume=999.0,
    )
    preview = hub.preview("SBER", 60, candles, forming)
    assert preview["sma"]["value"] is not None
    assert preview["sma"]["value"] != closed_value
    assert hub.get("SBER", 60, "sma", params) == closed_value


def test_market_data_hub():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    md.indicators.subscribe("SBER", 60, "sma", {"length": 20})
    candles = _candles(100)
    for c in candles:
        md.ingest_1m("SBER", c)
    val = md.indicators.get("SBER", 60, "sma", {"length": 20})
    assert val is not None
    assert val > 0


def test_market_data_hub_seed():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    md.indicators.subscribe("SBER", 60, "rsi", {"length": 14})
    candles = _candles(200)
    md.seed_1m("SBER", candles)
    val = md.indicators.get("SBER", 60, "rsi", {"length": 14})
    assert val is not None
    assert 0 <= val <= 100


def test_market_data_hub_multi_tf():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    md.indicators.subscribe("SBER", 60, "sma", {"length": 20})
    md.indicators.subscribe("SBER", 300, "sma", {"length": 1})
    candles = _candles(100)
    for c in candles:
        md.ingest_1m("SBER", c)
    assert md.indicators.get("SBER", 60, "sma", {"length": 20}) is not None
    assert md.indicators.get("SBER", 300, "sma", {"length": 1}) is not None


def test_market_data_hub_commits_only_on_closed_bar():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    params = {"length": 1}
    md.subscribe("SBER", "5min", "sma", params)
    start = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    candles = _candles(5, start=start)
    for candle in candles[:4]:
        md.ingest_1m("SBER", candle)
    series = md.candles.series("SBER", "5min")
    assert len(series) == 0
    assert series.partial is not None
    assert md.get("SBER", "5min", "sma", params) is None
    assert md.preview("SBER", "5min")["sma"]["value"] is not None

    md.ingest_1m("SBER", candles[4])
    assert len(series) == 1
    assert series.last.ts == candles[4].ts
    assert md.get("SBER", "5min", "sma", params) == candles[4].close


def test_market_data_hub_late_subscribe_builds_timeframe_from_history():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    md.subscribe("SBER", "1min", "rsi", {"length": 14})
    md.seed_1m("SBER", _candles(300))
    assert md.bound_timeframes("SBER") == [60]

    md.subscribe("SBER", "5min", "rsi", {"length": 14})
    series = md.candles.series("SBER", "5min")
    assert len(series) >= 55
    assert md.get("SBER", "5min", "rsi", {"length": 14}) is not None
    assert md.describe("SBER")[-1]["ready"] is True


def test_market_data_hub_unbind_keeps_foreign_handlers():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    params = {"length": 14}
    md.subscribe("SBER", "5min", "rsi", params)
    seen = []
    foreign = lambda candle: seen.append(candle)
    md.candles.series("SBER", "5min").on_closed.append(foreign)

    assert md.unsubscribe("SBER", "5min", "rsi", params)
    series = md.candles.series("SBER", "5min")
    assert foreign in series.on_closed
    assert len(series.on_closed) == 1

    start = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    for candle in _candles(5, start=start):
        md.ingest_1m("SBER", candle)
    assert seen


def test_market_data_hub_resubscribe_rebinds():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    params = {"length": 14}
    md.subscribe("SBER", "5min", "rsi", params)
    md.seed_1m("SBER", _candles(300))
    assert md.unsubscribe("SBER", "5min", "rsi", params)
    assert md.get("SBER", "5min", "rsi", params) is None

    md.subscribe("SBER", "5min", "rsi", params)
    assert md.bound_timeframes("SBER") == [300]
    assert md.get("SBER", "5min", "rsi", params) is not None


def test_market_data_hub_finalize_commits_forming_bar():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    params = {"length": 1}
    md.subscribe("SBER", "5min", "sma", params)
    start = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    candles = _candles(3, start=start)
    for candle in candles:
        md.ingest_1m("SBER", candle)
    assert md.get("SBER", "5min", "sma", params) is None

    md.finalize("SBER", "5min")
    assert md.get("SBER", "5min", "sma", params) == candles[-1].close


def test_market_data_hub_only_binds_subscribed_timeframes():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    md.subscribe("SBER", "1min", "rsi", {"length": 14})
    md.subscribe("SBER", "5min", "rsi", {"length": 14})
    assert md.candles.keys() == [("SBER", 60), ("SBER", 300)]
    assert md.bound_timeframes("SBER") == [60, 300]

    md.ingest_1m("SBER", _candles(200)[0])
    assert md.candles.keys() == [("SBER", 60), ("SBER", 300)]


def test_market_data_hub_unbind_after_unsubscribe():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    params = {"length": 14}
    md.subscribe("SBER", "5min", "rsi", params)
    assert md.bound_timeframes("SBER") == [300]
    assert md.unsubscribe("SBER", "5min", "rsi", params)
    assert md.bound_timeframes("SBER") == []


def test_market_data_hub_required_1m_bars():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    md.subscribe("SBER", "hour", "ema", {"length": 200})
    assert md.required_1m_bars("SBER", "hour") == 200 * 60
    md.subscribe("SBER", "1min", "rsi", {"length": 14})
    assert md.required_1m_bars("SBER", "1min") == 15


def test_market_data_hub_warms_every_timeframe():
    from app.engine.markethub import MarketDataHub
    from app.engine.indicatorhub import _rsi

    md = MarketDataHub()
    params = {"length": 14}
    for tf in ("1min", "5min", "15min"):
        md.subscribe("SBER", tf, "rsi", params)
    md.seed_1m("SBER", _candles(900))
    for tf in ("1min", "5min", "15min"):
        series = md.candles.series("SBER", tf)
        expected = _rsi(series.snapshot(), 14)[-1]
        assert md.get_at("SBER", tf, "rsi", series.snapshot()[-1].ts, params) == expected


def test_market_data_hub_late_candle_rebuilds_indicators():
    from app.engine.markethub import MarketDataHub
    from app.engine.indicatorhub import _sma

    md = MarketDataHub()
    params = {"length": 3}
    md.subscribe("SBER", "1min", "sma", params)
    candles = _candles(10)
    for candle in candles[:5] + candles[6:]:
        md.ingest_1m("SBER", candle)
    assert md.ingest_1m("SBER", candles[5]) == "insert"

    history = md.candles.series("SBER", "1min").snapshot()
    assert len(history) == 10
    assert history[5].ts == candles[5].ts
    assert md.get("SBER", "1min", "sma", params) == _sma(
        [c.close for c in history], 3)[-1]


def test_market_data_hub_describe_readiness():
    from app.engine.markethub import MarketDataHub

    md = MarketDataHub()
    md.subscribe("SBER", "1min", "rsi", {"length": 14})
    manifest = md.describe("SBER")
    assert len(manifest) == 1
    assert manifest[0]["indicator"] == "rsi"
    assert manifest[0]["required_bars"] == 15
    assert manifest[0]["ready"] is False
    md.seed_1m("SBER", _candles(200))
    assert md.describe("SBER")[0]["ready"] is True
