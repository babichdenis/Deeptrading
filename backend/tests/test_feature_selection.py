"""Phase 4-5: контракты Feature calculation и Selection.

Что здесь проверяется:
  Features  — паритет с каноническим ATR, формула ATR%, детерминизм,
              контракт на пустых/коротких данных и главное — look-ahead:
              будущая свеча не может изменить признаки для as_of;
  Selection — детерминированный tie-break, кросс-секционный percentile,
              Top-N на всех границах, отсутствие весов/ордеров/сигналов.

Live рантайм эти функции не вызывает: он остаётся на compat-пути
select_eligible_universe(top_n=9999). Тесты это фиксируют отдельно.
"""
from __future__ import annotations

import copy
import inspect
from dataclasses import fields
from datetime import datetime, timedelta

import pytest

from app.bot.universe import features, selection
from app.bot.universe.bars import resample_1m_to_5m
from app.bot.universe.domain import (
    FeatureSet,
    InstrumentRef,
    RankingMethod,
    ScreenedInstrument,
    ScreenResult,
    SelectionItem,
    SelectionResult,
)
from app.bot.universe.features import (
    ATR_PERIOD,
    FEATURE_WINDOW,
    atr_pct,
    compute_feature_set,
    compute_feature_sets,
)
from app.bot.universe.selection import rank_features, select
from app.engine.indicators import atr as engine_atr
from app.engine.models import Candle as EngineCandle
from app.engine.indicatorhub import _atr as hub_atr

AS_OF = datetime(2026, 9, 30, 18, 0, 0)
BAR_MINUTES = 5


def make_bars(closes, *, start=AS_OF, step_minutes=1, volumes=None) -> list[EngineCandle]:
    """Детерминированная серия баров. ts строго возрастает."""
    out = []
    for i, close in enumerate(closes):
        ts = start - timedelta(minutes=step_minutes * (len(closes) - i))
        volume = volumes[i] if volumes else 1000.0
        out.append(
            EngineCandle(
                ts=ts,
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=volume,
            )
        )
    return out


def series(n=120, *, base=100.0, wave=5.0, start=AS_OF) -> list[EngineCandle]:
    """Псевдослучайная, но полностью воспроизводимая серия для ATR-паритета."""
    closes = [base + wave * ((i * 7) % 11) - wave / 2 for i in range(n)]
    return make_bars(closes, start=start, step_minutes=BAR_MINUTES)


def ref(ticker, figi="") -> InstrumentRef:
    return InstrumentRef(ticker=ticker, figi=figi or f"figi_{ticker}")


def feature_set(ticker, atr_pct, *, as_of=AS_OF, atr=None) -> FeatureSet:
    return FeatureSet(
        instrument=ref(ticker),
        as_of=as_of,
        atr=atr if atr is not None else atr_pct,
        atr_pct=atr_pct,
        close=100.0,
        bars_used=44,
        valid=True,
    )


# ---------------------------------------------------------------- ATR parity


def test_feature_atr_equals_canonical_indicator():
    """Паритет с каноном на том же окне, которое использует Feature-слой.

    Сравнивать ATR по 44 последним барам с ATR по всей истории нельзя: ATR
    зависит от окна. Эталон поэтому режется той же границей FEATURE_WINDOW.
    """
    bars = series()
    window = bars[-FEATURE_WINDOW:]
    got = compute_feature_set(ref("ALRS"), bars, as_of=AS_OF)

    canonical = [v for v in hub_atr(window, ATR_PERIOD) if v is not None][-1]
    legacy_engine = [v for v in engine_atr(window, ATR_PERIOD) if v is not None][-1]

    assert got.valid is True
    assert got.bars_used == len(bars)
    assert got.atr == canonical
    assert got.atr == legacy_engine


def test_feature_atr_window_is_the_last_44_bars():
    """Сдвиг окна меняет ATR — значит канон считается именно по последним 44."""
    bars = series(90)
    got = compute_feature_set(ref("ALRS"), bars, as_of=AS_OF)
    wider = [v for v in hub_atr(bars[-60:], ATR_PERIOD) if v is not None][-1]
    assert got.atr != wider
    assert got.bars_used == len(bars)


def test_feature_atr_pct_formula():
    """atr_pct = atr / close * 100, округление до 3 знаков — как в legacy."""
    bars = series()
    got = compute_feature_set(ref("ALRS"), bars, as_of=AS_OF)

    assert got.close == bars[-1].close
    assert got.atr_pct == round(got.atr / got.close * 100, 3)
    assert got.atr_pct == atr_pct(bars, FEATURE_WINDOW)[0]


def test_legacy_atr_pct_wrapper_unchanged():
    """Legacy-обёртка и новый контракт дают одно и то же число."""
    for closes in ([], [100.0], [100.0] * 13, list(range(50, 150))):
        bars = make_bars(closes)
        legacy_value, legacy_valid = atr_pct(bars, FEATURE_WINDOW)
        new = compute_feature_set(ref("T"), bars, as_of=AS_OF)
        if legacy_valid:
            assert new.atr_pct == legacy_value
            assert new.valid is True
        else:
            assert new.valid is False


def test_feature_close_zero_is_invalid():
    bars = series()
    bars[-1] = EngineCandle(
        ts=bars[-1].ts, open=0.0, high=0.0, low=0.0, close=0.0, volume=1.0
    )
    got = compute_feature_set(ref("ZERO"), bars, as_of=AS_OF)
    assert got.valid is False
    assert got.atr is None
    assert got.atr_pct is None


# ------------------------------------------------------- детерминизм и вход


def test_feature_is_deterministic():
    bars = series()
    first = compute_feature_set(ref("ALRS"), bars, as_of=AS_OF)
    second = compute_feature_set(ref("ALRS"), bars, as_of=AS_OF)
    assert first == second
    assert (first.atr, first.atr_pct) == (second.atr, second.atr_pct)


def test_feature_does_not_mutate_input_bars():
    bars = series()
    before = [copy.deepcopy(b) for b in bars]
    compute_feature_set(ref("ALRS"), bars, as_of=AS_OF)
    assert bars == before


def test_compute_feature_sets_covers_every_instrument():
    bars = series()
    refs = (ref("AAA"), ref("BBB"), ref("CCC"))
    result = compute_feature_sets(
        refs, {refs[0].figi: bars, refs[1].figi: bars}, as_of=AS_OF
    )
    assert [fs.instrument for fs in result] == list(refs)
    assert [fs.valid for fs in result] == [True, True, False]
    assert result[2].atr is None


# --------------------------------------------------------------- look-ahead


def test_lookahead_future_bar_does_not_change_features():
    """Главный regression test: будущая свеча не влияет на признаки для as_of."""
    bars = series(60, start=AS_OF - timedelta(minutes=5 * 59))
    assert all(b.ts <= AS_OF for b in bars)

    baseline = compute_feature_set(ref("ALRS"), bars, as_of=AS_OF)

    with_future = bars + [
        EngineCandle(
            ts=AS_OF + timedelta(minutes=5),
            open=baseline.close,
            high=baseline.close * 1.5,
            low=baseline.close * 0.5,
            close=baseline.close * 1.4,
            volume=999_999.0,
        )
    ]
    polluted = compute_feature_set(ref("ALRS"), with_future, as_of=AS_OF)

    assert polluted == baseline


def test_lookahead_ignores_bars_inside_future_window():
    """Будущий бар не попадает в окно ATR, даже если её длина достаточна."""
    bars = series(30, start=AS_OF - timedelta(minutes=5 * 29))
    baseline = compute_feature_set(ref("ALRS"), bars, as_of=AS_OF)
    assert baseline.valid is True

    future_only = bars + [
        EngineCandle(
            ts=AS_OF + timedelta(minutes=5),
            open=1.0, high=500.0, low=1.0, close=400.0, volume=1e9,
        )
    ]
    assert compute_feature_set(ref("ALRS"), future_only, as_of=AS_OF) == baseline


def test_lookahead_as_of_in_past_excludes_newer_bars():
    """as_of в середине истории — видны только бары до него."""
    bars = series(60, start=AS_OF - timedelta(minutes=5 * 59))
    mid = bars[29].ts

    at_mid = compute_feature_set(ref("ALRS"), bars, as_of=mid)
    assert at_mid.bars_used == 30
    assert at_mid.as_of == mid

    full = compute_feature_set(ref("ALRS"), bars, as_of=AS_OF)
    assert full.bars_used == 60
    assert full.atr_pct != at_mid.atr_pct or full.valid != at_mid.valid


# ------------------------------------------------------- ranking / tie-break

FIXTURE = (
    ("AAA", 0.10),
    ("BBB", 0.30),
    ("CCC", 0.20),
    ("DDD", 0.30),
)


def fixture_features():
    return [feature_set(t, p) for t, p in FIXTURE]


def test_ranking_order_and_tiebreak():
    result = select(fixture_features(), top_n=4)
    assert [i.instrument.ticker for i in result.items] == ["BBB", "DDD", "CCC", "AAA"]
    assert [i.rank for i in result.items] == [1, 2, 3, 4]
    assert result.items[0].score == 0.30
    assert result.items[1].score == 0.30


def test_ranking_is_independent_of_input_order():
    forward = rank_features(fixture_features())
    backward = rank_features(list(reversed(fixture_features())))
    assert forward == backward

    shuffled = rank_features(
        [feature_set("CCC", 0.20), feature_set("AAA", 0.10),
         feature_set("DDD", 0.30), feature_set("BBB", 0.30)]
    )
    assert [i.instrument.ticker for i in shuffled] == ["BBB", "DDD", "CCC", "AAA"]


def test_ranking_is_deterministic_on_repeat():
    first = select(fixture_features(), top_n=3)
    second = select(fixture_features(), top_n=3)
    assert first == second


# ---------------------------------------------------------------- percentile


def test_percentile_documented_convention():
    """percentile = 100 * count(x <= value) / n; равные значения делят percentile."""
    result = select(fixture_features(), top_n=4)
    pct = {i.instrument.ticker: i.atr_pct_percentile for i in result.items}
    assert pct == {"BBB": 100.0, "DDD": 100.0, "CCC": 50.0, "AAA": 25.0}


def test_percentile_preserves_raw_atr_pct():
    result = select(fixture_features(), top_n=4)
    assert {i.instrument.ticker: i.atr_pct for i in result.items} == {
        "AAA": 0.10, "BBB": 0.30, "CCC": 0.20, "DDD": 0.30,
    }


def test_percentile_edge_cases():
    assert select([], top_n=3).items == ()
    assert select([], top_n=3).as_of is None

    single = select([feature_set("ONLY", 0.42)], top_n=1)
    assert single.items[0].atr_pct_percentile == 100.0

    same = [feature_set("AAA", 0.25), feature_set("BBB", 0.25), feature_set("CCC", 0.25)]
    equal = select(same, top_n=3)
    assert {i.atr_pct_percentile for i in equal.items} == {100.0}
    assert [i.instrument.ticker for i in equal.items] == ["AAA", "BBB", "CCC"]


def test_percentile_ignores_invalid_features_without_value():
    mixed = [feature_set("AAA", 0.10), feature_set("BBB", None, atr=None)]
    result = select(mixed, top_n=2)
    scores = {i.instrument.ticker: (i.score, i.atr_pct_percentile) for i in result.items}
    assert scores["AAA"] == (0.10, 100.0)
    assert scores["BBB"] == (0.0, None)


# -------------------------------------------------------------------- Top-N


@pytest.mark.parametrize(
    ("top_n", "expected"),
    [
        (0, []),
        (1, ["BBB"]),
        (2, ["BBB", "DDD"]),
        (3, ["BBB", "DDD", "CCC"]),
        (5, ["BBB", "DDD", "CCC", "AAA"]),
        (99, ["BBB", "DDD", "CCC", "AAA"]),
    ],
)
def test_top_n_boundaries(top_n, expected):
    result = select(fixture_features(), top_n=top_n)
    assert [i.instrument.ticker for i in result.selected] == expected
    assert len(result.items) == 4


def test_all_eligible_baseline_returns_everything():
    result = select(fixture_features(), top_n=1, method=RankingMethod.ALL_ELIGIBLE)
    assert [i.instrument.ticker for i in result.selected] == ["BBB", "DDD", "CCC", "AAA"]
    assert result.ranking_method == "all_eligible"
    assert result.top_n == 1


def test_selection_result_contract():
    result = select(fixture_features(), top_n=2)
    assert isinstance(result, SelectionResult)
    assert result.as_of == AS_OF
    assert result.ranking_method == "top_n_atr"
    assert result.top_n == 2
    assert all(isinstance(i, SelectionItem) for i in result.items)
    assert [i.rank for i in result.selected] == [1, 2]


def test_custom_selection_strategy():
    class ByVolumeScore:
        name = "by_volume_score"

        def score(self, fs: FeatureSet) -> float:
            return float(fs.bars_used)

    result = select(fixture_features(), top_n=1, strategy=ByVolumeScore())
    assert result.ranking_method == "by_volume_score"
    assert [i.instrument.ticker for i in result.selected] == ["AAA"]


def test_selection_does_not_mutate_input():
    fset = fixture_features()
    before = copy.deepcopy(fset)
    select(fset, top_n=2)
    assert fset == before


# ------------------------------------------------- Selection != Allocation


def test_selection_result_has_no_weight_fields():
    forbidden = {
        "target_weight", "weight", "notional", "quantity", "lots",
        "capital", "allocation", "position_size",
    }
    for contract in (SelectionItem, SelectionResult):
        names = {f.name for f in fields(contract)}
        assert names.isdisjoint(forbidden), f"{contract.__name__} содержит поля Allocation"


def test_selection_api_has_no_execution_surface():
    """В Selection нет ни брокера, ни ордеров, ни позиций (спецификация, п.18).

    Проверяем публичные имена модуля, а не подстроки в исходнике: иначе
    срабатывает ложное совпадение на `ordered`/`sort order`.
    """
    public = {n for n in dir(selection) if not n.startswith("_")}
    forbidden = {
        "buy", "sell", "close", "close_position", "open_position",
        "order", "orders", "broker", "portfolio", "positions",
        "rebalance", "execute", "execution", "allocate", "allocation",
    }
    assert public.isdisjoint(forbidden), f"лишние имена: {public & forbidden}"

    source = inspect.getsource(selection).lower()
    for banned in (
        "app.bot.portfolio", "app.bot.execution", "app.services.broker",
        "app.bot.broker", "from app.bot.portfolio", "order(1", "order(2",
    ):
        assert banned not in source


def test_ranking_change_does_not_produce_exit():
    """Смена ранкинга — не сигнал на выход (спецификация, п.19).

    GAZP был в позиции и выпал из Top-2. Selection возвращает ранкинг и срез,
    и НИЧЕГО, что выглядело бы как распоряжение закрыться.
    """
    held = "GAZP"
    fset = fixture_features() + [
        FeatureSet(instrument=ref(held), as_of=AS_OF, atr=9.0, atr_pct=0.01, valid=True)
    ]

    before = select([f for f in fset if f.instrument.ticker != held] + [
        f for f in fset if f.instrument.ticker == held
    ], top_n=2)
    after = select(fset, top_n=2)

    assert held not in [i.instrument.ticker for i in after.selected]
    assert held in [i.instrument.ticker for i in after.items]
    assert after.selected == after.selected[: len(after.selected)]

    payload = {f.name for f in fields(SelectionResult)} | {
        f.name for f in fields(SelectionItem)
    }
    assert payload.isdisjoint({"side", "action", "close", "exit", "sell", "orders"})


# ----------------------------------------- наблюдения без изменения семантики


def test_min_source_bars_never_binds_below_resampled_floor():
    """Наблюдение (спецификация, п.24): min_source_bars=5 недостижим.

    Профиль ELIGIBLE требует 15 баров 5m, а 15 таких баров нельзя получить из
    5 минутных: floor-бакет 5m наполняется пятью 1m-барами. Значение НЕ меняем —
    фиксируем фактическую семантику.
    """
    from app.bot.universe.screener import ELIGIBLE

    assert ELIGIBLE.min_source_bars == 5
    assert ELIGIBLE.min_resampled_bars == 15

    few = make_bars([100.0 + i for i in range(10)])
    assert len(resample_1m_to_5m(few, "figi")) < ELIGIBLE.min_resampled_bars
    assert len(few) >= ELIGIBLE.min_source_bars


def test_250_source_bars_suffice_for_window_44():
    """Наблюдение (спецификация, п.25): 250 минутных баров хватает для окна 44.

    Production-лимит в этой задаче НЕ меняется; проверка лишь показывает, что
    предложенный лимит сохраняет ATR. Запас: 49-50 полных 5m-бакетов на 44.
    """
    base = datetime(2026, 9, 30, 12, 0, 0)
    closes = [100.0 + ((i * 7) % 11) for i in range(1000)]
    bars_1m = make_bars(closes, start=base, step_minutes=1)

    wide = resample_1m_to_5m(bars_1m, "figi")
    narrow = resample_1m_to_5m(bars_1m[-250:], "figi")

    assert len(narrow) >= FEATURE_WINDOW
    assert atr_pct(wide[-FEATURE_WINDOW:], FEATURE_WINDOW)[0] == \
           atr_pct(narrow[-FEATURE_WINDOW:], FEATURE_WINDOW)[0]


# ------------------------------------------------------------- live-изоляция


def test_runtime_still_uses_compat_path_not_selection():
    """Live-решение не переведено на Selection: это запрещено до Phase 6."""
    runtime = inspect.getsource(
        __import__("app.bot.runtime", fromlist=["x"])
    )
    assert "select_eligible_universe(db, top_n=9999)" in runtime
    for banned in ("rank_features", "SelectionResult", "compute_feature_set"):
        assert f"universe.selection.{banned}" not in runtime
        assert f"universe.features.{banned}" not in runtime


def test_vol_carousel_ranking_not_migrated():
    """vol_carousel сохраняет собственный ranking (спецификация, п.21)."""
    from app.services import vol_carousel

    assert hasattr(vol_carousel, "rank_volatile")
    assert "app.bot.universe.selection" not in inspect.getsource(vol_carousel)


def test_screened_instrument_carries_no_features():
    """ScreenedInstrument не содержит ATR — это контракт Phase 1-3."""
    names = {f.name for f in fields(ScreenedInstrument)}
    assert "atr" not in names
    assert "atr_pct" not in names
    assert ScreenResult(eligible=True).eligible is True
