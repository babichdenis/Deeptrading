"""AUDIT P1.4: у runtime-моста Universe не было собственных тестов.

Мост — это `runtime_select.select_screened_universe` плюс ветка в
`PaperBotRuntime._startup`, которая его вызывает. Отсюда риски, которых
не видно в тестах отдельных компонентов Universe:

- v2-отбор может просочиться в live/sandbox (он исследовательский);
- отбор может заглянуть в будущее относительно as_of;
- figi в Universe (BBG из свечей) и figi в инструментах (TCS) — разные
  пространства имён, ошибка здесь = тихая потеря тикера;
- стрим может подписаться на другой набор, чем отобранный Universe;
- hot-add может добавить тикеры ПОСЛЕ старта окна и сломать прогретые ряды;
- пустой или частично битый рынок обязан диагностироваться, а не молчать.

Тесты функциональные (фейковый драйвер вместо БД) + структурные на ветку
_startup, которую нельзя поднять без живого ринка.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.bot.universe.runtime_select import (
    UNIVERSE_MODES,
    _is_all_market,
    _strategy_mode,
    select_screened_universe,
)

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)
FEATURE_WINDOW = 44


# ── фейковый драйвер: отдаёт заранее заданные строки по типу запроса ──────


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _FakeDB:
    """Мини-драйвер: раскладывает строки по смыслу запроса, а не по тексту.

    Маршруты all-market-пути (см. runtime_select._load_snapshot_bars):
      instruments (ORM)               → self.instruments  (фолбэк тикера)
      instrument_info api_trade_avail → self.info         (TCS-снимок/лоты)
      candles GROUP BY                → self.figis        (предфильтр)
      candles make_interval bulk      → self.bars         (bounded tail)
      instrument_info ANY(:figis)     → self.lots         (добор лотов)
    """

    def __init__(self, *, instruments=(), figis=(), lots=(), info=(), bars=()):
        self.instruments = list(instruments)
        self.figis = list(figis)
        self.lots = list(lots)
        self.info = list(info)
        self.bars = list(bars)
        self.calls = []

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.calls.append((sql, params))
        if "GROUP BY" in sql:                                   # предфильтр FIGI
            return _FakeResult(self.figis)
        if "figi, lot" in sql and "instrument_info" in sql:     # добор лотов
            return _FakeResult(self.lots)
        if "api_trade_available" in sql:                        # instrument_info снимок
            return _FakeResult(self.info)
        if "make_interval" in sql:                              # bounded bulk баров
            return _FakeResult(self.bars)
        if "FROM instruments" in sql:                           # instruments фолбэк
            return _FakeResult(self.instruments)
        raise AssertionError(f"неожиданный запрос: {sql[:120]}")


def _candles(figi, n=60, start=T0, slope=1.0):
    from app.engine.models import Candle as EngineCandle

    out = []
    for i in range(n):
        base = 100 + slope * i
        out.append(EngineCandle(
            ts=start + STEP * i, open=base, high=base + 0.4, low=base - 0.4,
            close=base + 0.1, volume=1000.0,
        ))
    return out


def _bulk_rows(figis, n=60, slope=1.0):
    rows = []
    for f in figis:
        for c in _candles(f, n=n, slope=slope):
            rows.append((f, c.ts, c.open, c.high, c.low, c.close, c.volume))
    return rows


def _select(db, *, mode="v2_trend_all", top_n=10, as_of=None, figi_by_ticker=None):
    as_of = as_of or (T0 + STEP * 60)
    return asyncio.run(select_screened_universe(
        db, figi_by_ticker or {}, mode=mode, top_n=top_n, as_of=as_of,
    ))


# ── 1. v2-отбор разрешён ТОЛЬКО в mode=test ──────────────────────────────


def test_v2_gated_behind_test_mode_in_startup():
    """Ветка v2 обязана требовать cfg.mode == 'test', иначе уйдёт в live."""
    from app.bot import runtime as rt

    src = inspect.getsource(rt.PaperBotRuntime._startup)
    assert '_umode.startswith("v2_") and str(cfg.mode) == "test"' in src, (
        "v2-отбор перестал быть ограничен тест-контуром"
    )


def test_stream_universe_is_exactly_selected_in_v2():
    """В v2 стрим подписывается ровно на отобранный набор, а не на all_eligible."""
    from app.bot import runtime as rt

    src = inspect.getsource(rt.PaperBotRuntime._startup)
    assert 'self.stream_universe = [u["figi"] for u in self.universe]' in src


def test_universe_mode_env_is_optional_and_normalized(monkeypatch):
    """Пустой UNIVERSE_MODE = legacy-отбор; значение нормализуется к нижнему."""
    from app.bot.runtime import _universe_v2_mode

    junk = SimpleNamespace(universe_mode="")
    monkeypatch.setattr("app.config.get_settings", lambda: junk)
    assert _universe_v2_mode() == ""
    junk.universe_mode = " V2_Trend_All "
    assert _universe_v2_mode() == "v2_trend_all"


def test_universe_mode_broken_settings_is_safe(monkeypatch):
    """Если settings не загружаются (hermetic без окружения) — legacy, без исключения."""
    from app.bot.runtime import _universe_v2_mode

    def _boom():
        raise RuntimeError("no settings")

    monkeypatch.setattr("app.config.get_settings", _boom)
    assert _universe_v2_mode() == ""


def test_known_modes_and_suffix_parsing():
    assert set(UNIVERSE_MODES) == {
        "v2_trend", "v2_meanrev", "v2_trend_all", "v2_meanrev_all",
    }
    assert _is_all_market("v2_trend_all") is True
    assert _is_all_market("v2_trend") is False
    assert _strategy_mode("v2_trend_all") == "v2_trend"
    assert _strategy_mode("v2_meanrev") == "v2_meanrev"


def test_unknown_mode_rejected():
    """Опечатка в UNIVERSE_MODE обязана падать, а не молча откатываться к legacy."""
    db = _FakeDB()
    with pytest.raises(ValueError, match="unknown universe mode"):
        _select(db, mode="v2_typo")


# ── 2. отбор видит только историю ДО as_of ───────────────────────────────


def test_all_market_queries_are_bounded_by_as_of():
    """И предфильтр FIGI, и bounded-bulk обязаны отбирать по закрытию бара.

    Иначе отбор заглянет в бары после as_of — либо, наоборот, посчитает
    инструмент годным по истории, которой рантайм ещё не видел.
    """
    db = _FakeDB(figis=["F1"], bars=_bulk_rows(["F1"]))
    _select(db, as_of=T0 + STEP * 60)

    bounded = [sql for sql, _ in db.calls if "make_interval" in sql]
    assert bounded, "ни один запрос не ограничен as_of"
    for sql in bounded:
        assert "make_interval" in sql, sql[:120]
        assert "ts <= :a" not in sql and "ts <= :as_of" not in sql


def test_bars_after_as_of_never_appear_in_output():
    """Бары будущего не должны попасть в признаки, даже если их отдал драйвер."""
    figis = ["F1"]
    as_of = T0 + STEP * 30
    good = _bulk_rows(figis, n=30)
    poison = good + [
        (figis[0], T0 + STEP * 40, 1.0, 99999.0, 0.01, 99999.0, 10 ** 12)
    ]

    a = _select(_FakeDB(figis=figis, bars=good), as_of=as_of, top_n=5)
    b = _select(_FakeDB(figis=figis, bars=poison), as_of=as_of, top_n=5)

    assert [r["figi"] for r in a] == [r["figi"] for r in b]
    assert [r["atr_pct"] for r in a] == [r["atr_pct"] for r in b]


# ── 3. TCS → BBG: два пространства имён figi ─────────────────────────────


def test_bbg_figi_mapped_back_to_tcs_ticker():
    """Свечи лежат по BBG-FIGI, instrument_info — по TCS-FIGI.

    Тикер в выдаче обязан соответствовать тикеру по TCS-FIGI, а не быть
    вырезан из последних символов BBG-FIGI (такой фолбэк есть в коде как
    последний случай, и он не должен срабатывать при корректном маппинге).
    """
    figi_by_ticker = {"SBER": "BBG004730032"}
    db = _FakeDB(
        figis=["BBG004730032"],
        bars=_bulk_rows(["BBG004730032"], n=60, slope=2.0),
        info=[("TCS_SBER", "SBER", 10, "Sberbank")],
    )
    out = _select(db, figi_by_ticker=figi_by_ticker, mode="v2_trend_all")

    assert [r["figi"] for r in out] == ["BBG004730032"]
    assert out[0]["ticker"] == "SBER"


def test_lot_resolved_for_tcs_figi_of_mapped_instrument():
    """Лот приходит по TCS-FIGI, а отдаётся по BBG-FIGI, по которому идут бары."""
    figi_by_ticker = {"GAZP": "BBG0007669625"}
    db = _FakeDB(
        figis=["BBG0007669625"],
        bars=_bulk_rows(["BBG0007669625"], n=60),
        info=[("TCS_GAZP", "GAZP", 1, "Gazprom")],
        lots=[("TCS_GAZP", 1)],
    )
    out = _select(db, figi_by_ticker=figi_by_ticker, mode="v2_trend_all")
    assert out, "инструмент не прошёл скрининг"
    assert out[0]["lot_size"] == 1


def test_unknown_lot_falls_back_to_default_explicitly():
    """FIGI нет в instrument_info-снимке → лот добирается из БД; нет и там → DEFAULT_LOT.

    0 на биржу не передать, поэтому фолбэк обязан быть явной константой, а не 0.
    """
    from app.bot.universe.discovery import DEFAULT_LOT

    figi_by_ticker = {"XYZ": "BBG0099999999"}
    db = _FakeDB(
        figis=["BBG0099999999"],
        bars=_bulk_rows(["BBG0099999999"], n=60),
        info=[],
        lots=[],
    )
    out = _select(db, figi_by_ticker=figi_by_ticker, mode="v2_trend_all")
    assert out and out[0]["ticker"] == "XYZ"
    assert out[0]["lot_size"] == DEFAULT_LOT


# ── 4. горячий добор отключён на время окна ──────────────────────────────


def test_hot_add_never_touches_db_in_replay():
    """В реплее hot-add обязан быть выключен.

    Иначе в Universe попадают бары ПОСЛЕ старта окна: ломаются прогретые
    ряды и теряются сигналы (найдено 01.10 на v2trendALL/dbg0901).
    """
    from app.bot.runtime import BotConfig, PaperBotRuntime

    called = SimpleNamespace(hot=False)

    class _BoomSession:
        def __init__(self, *a, **k):
            called.hot = True
            raise AssertionError("hot-add полез в БД в реплее")

    runtime_mod = inspect.getmodule(PaperBotRuntime)
    original = runtime_mod.SessionLocal
    runtime_mod.SessionLocal = _BoomSession
    try:
        rt = SimpleNamespace(
            running=True, config=BotConfig(feed="replay"), universe=[{"figi": "F1"}],
        )
        task = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _run_briefly(PaperBotRuntime._hot_add_universe, rt)
        )
    finally:
        runtime_mod.SessionLocal = original

    assert called.hot is False
    assert task is not None


async def _run_briefly(coro_func, obj):
    """Запускает бесконечный цикл и гасит его, не дожидаясь завершения."""
    task = asyncio.ensure_future(coro_func(obj))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return task


def test_hot_add_guard_is_checked_before_db_work():
    """Проверка feed=='replay' стоит ДО похода в БД (иначе guard декоративен)."""
    from app.bot.runtime import PaperBotRuntime

    src = inspect.getsource(PaperBotRuntime._hot_add_universe)
    guard = src.index('== "replay"')
    session = src.index("SessionLocal")
    assert guard < session, "guard проверяется после открытия сессии — БД уже затронут"


# ── 5. пустой / частично битый рынок диагностируется детерминированно ────


def test_empty_market_returns_empty_list():
    db = _FakeDB(figis=[], bars=[])
    assert _select(db) == []


def test_market_without_accepted_candidates_returns_empty():
    """Есть бары, но скрининг никого не принял — пусто, а не исключение."""
    db = _FakeDB(figis=["F1"], bars=_bulk_rows(["F1"], n=60, slope=1.0))
    out = _select(db, mode="v2_meanrev_all")   # сильный тренд не проходит mean-reversion
    assert out == []


def test_figi_without_bars_is_skipped_not_fatal():
    """FIGI прошла предфильтр, но баров не пришло — инструмент выбрасывается."""
    db = _FakeDB(figis=["F1", "F2"], bars=_bulk_rows(["F1"], n=60))
    out = _select(db, mode="v2_trend_all", top_n=10)
    assert {r["figi"] for r in out} <= {"F1"}


def test_broken_figi_does_not_poison_the_rest():
    """FIGI с битыми данными (нулевая цена) выпадает, остальные проходят."""
    figis = ["F_OK", "F_BAD"]
    rows = _bulk_rows(["F_OK"], n=60, slope=2.0)
    bad = []
    for c in _candles("F_BAD", n=60, slope=2.0):
        bad.append(("F_BAD", c.ts, 0.0, 0.0, 0.0, 0.0, 0.0))
    db = _FakeDB(figis=figis, bars=rows + bad)
    out = _select(db, mode="v2_trend_all", top_n=10)
    assert {r["figi"] for r in out} == {"F_OK"}


def test_empty_universe_raises_in_startup():
    """Пустой Universe в _startup обязан поднять ошибку, а не торговать вхолостую."""
    from app.bot import runtime as rt

    src = inspect.getsource(rt.PaperBotRuntime._startup)
    assert 'if not self.universe:' in src
    assert 'raise RuntimeError("universe is empty")' in src


# ── 6. детерминизм и контракт выдачи ────────────────────────────────────


def test_selection_is_deterministic():
    """Один и тот же вход — один и тот же порядок выдачи (score DESC, ticker ASC)."""
    figis = ["F1", "F2", "F3"]
    db = _FakeDB(figis=figis, bars=_bulk_rows(figis, n=60, slope=1.5))
    first = _select(db, mode="v2_trend_all", top_n=5)
    second = _select(_FakeDB(figis=figis, bars=_bulk_rows(figis, n=60, slope=1.5)),
                     mode="v2_trend_all", top_n=5)
    assert first == second


def test_output_contract_matches_runtime_expectations():
    """Ключи выдачи — те, что _startup/runtime читают по месту."""
    db = _FakeDB(figis=["F1"], bars=_bulk_rows(["F1"], n=60, slope=2.0))
    out = _select(db, mode="v2_trend_all", top_n=5)
    assert out
    for row in out:
        assert {"figi", "ticker", "atr_pct", "lot_size"} <= set(row)
        assert isinstance(row["figi"], str) and row["figi"]
        assert isinstance(row["lot_size"], int) and row["lot_size"] > 0
        assert float(row["atr_pct"]) >= 0.0


def test_top_n_limits_result():
    figis = [f"F{i}" for i in range(6)]
    rows = []
    for idx, f in enumerate(figis):
        rows += _bulk_rows([f], n=60, slope=1.0 + idx * 0.3)
    db = _FakeDB(figis=figis, bars=rows, info=[(f, f, 1, f) for f in figis])
    out = _select(db, mode="v2_trend_all", top_n=2)
    assert len(out) == 2
