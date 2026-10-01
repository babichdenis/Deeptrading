"""Тесты CandleHub — свечной контур Фазы C (порт CandleManager из OsEngine).

Часть 1 (v1, базовая механика): конвенция close-time, арифметика бакетов,
склейка ТФ, стыки сессий (клиринг day→evening), гэпы, дубликаты/опоздавшие,
неполные кадры, события on_closed/on_updated, seed/finalize.

Часть 2 (v2, 2026-09-26 — «движок живёт с неидеальными данными»): валидатор
свечей (validate_candle), приоритеты источников (source_rank, replace),
опоздавшие минуты (insert + rebuild производных ТФ), батчи merge_1m/seed_1m,
события on_rejected/on_rebuilt, gap_report с календарём сессий, счётчики
stats.

Граница ролей (решение 2026-09-26): CandleHub — ЧИСТЫЙ движок свечей;
докачка новых тикеров, live-подписки, сигнальные фиды (IMOEX и др.) —
оркестратор ПОВЕРХ движка и здесь не тестируется. Без БД и сети — чистые
модули.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.engine.candlehub import (
    SOURCE_PRIORITY,
    CandleHub,
    CandleSeries,
    bucket_start,
    build_tf,
    source_rank,
    tf_to_seconds,
    validate_candle,
)
from app.engine.models import Candle

U = timezone.utc
T0 = datetime(2026, 9, 1, 10, 0, tzinfo=U)  # 13:00 MSK, внутри дневной сессии


# ============================================================
# Хелперы
# ============================================================
def _c(i, o=100.0, h=None, lo=None, c=100.0, v=10.0, t0=None):
    """Закрытая 1m-свеча с close = t0 + i минут (по умолчанию T0)."""
    ts = (t0 or T0) + timedelta(minutes=i)
    return Candle(
        ts=ts,
        open=o,
        high=h if h is not None else max(o, c),
        low=lo if lo is not None else min(o, c),
        close=c,
        volume=v,
    )


# ============================================================
# tf_to_seconds / bucket_start — арифметика бакетов (канон START/floor)
# ============================================================
class TestBucketMath:
    def test_tf_to_seconds(self):
        assert tf_to_seconds("5min") == 300
        assert tf_to_seconds("hour") == 3600
        assert tf_to_seconds(300) == 300

    def test_tf_to_seconds_rejects_unknown(self):
        with pytest.raises(ValueError):
            tf_to_seconds("7min")
        with pytest.raises(ValueError):
            tf_to_seconds(30)

    def test_boundary_is_own_bucket_start(self):
        # ts, кратный tf, — НАЧАЛО СВОЕГО бакета (канон START/floor)
        t = datetime(2026, 9, 1, 10, 5, tzinfo=U)
        assert bucket_start(t, 300) == t

    def test_non_boundary_rounds_down(self):
        t = datetime(2026, 9, 1, 10, 4, 59, tzinfo=U)
        assert bucket_start(t, 300) == datetime(2026, 9, 1, 10, 0, tzinfo=U)

    def test_naive_treated_as_utc(self):
        t = datetime(2026, 9, 1, 10, 4)  # без tzinfo
        assert bucket_start(t, 300) == datetime(2026, 9, 1, 10, 0, tzinfo=U)


# ============================================================
# v2: validate_candle — валидатор входа
# ============================================================
class TestValidateCandle:
    def test_healthy_candle_passes(self):
        assert validate_candle(_c(1, o=100, h=110, lo=95, c=105, v=7)) is None

    def test_zero_volume_and_flat_are_legal(self):
        # нулевой объём и плоская свеча — законные данные, не ошибки
        assert validate_candle(_c(1, o=100, h=100, lo=100, c=100, v=0)) is None

    def test_nan_rejected(self):
        bad = Candle(ts=T0, open=float("nan"), high=101, low=99, close=100, volume=1)
        assert validate_candle(bad) == "nan"
        bad2 = Candle(ts=T0, open=100, high=101, low=99, close=100,
                      volume=float("inf"))
        assert validate_candle(bad2) == "nan"

    def test_bad_price_rejected(self):
        assert validate_candle(_c(1, o=-1, h=100, lo=-2, c=50)) == "bad_price"
        assert validate_candle(_c(1, o=100, h=100, lo=0, c=50)) == "bad_price"

    def test_bad_volume_rejected(self):
        assert validate_candle(_c(1, v=-5)) == "bad_volume"

    def test_bad_range_rejected(self):
        # high ниже тела / low выше тела — противоречие OHLC
        assert validate_candle(_c(1, o=100, h=101, lo=99, c=110)) == "bad_range"
        assert validate_candle(_c(1, o=100, h=105, lo=99, c=90)) == "bad_range"

    def test_float_noise_not_rejected(self):
        # шум округления ~1e-12 не рождает ложный отказ (допуск rel_eps)
        ok = Candle(ts=T0, open=100.0, high=100.0 + 1e-12, low=100.0 - 1e-12,
                    close=100.0, volume=1.0)
        assert validate_candle(ok) is None


# ============================================================
# v2: source_rank — приоритет источников
# ============================================================
class TestSourceRank:
    def test_default_priority_live_best(self):
        assert source_rank("live") < source_rank("rest") < source_rank("db")

    def test_unknown_source_worst(self):
        assert source_rank("garbage") > source_rank("db")

    def test_custom_priority(self):
        # оркестратор может поставить, например, ISS (rest) выше live-стрима
        custom = {"rest": 0, "live": 1}
        assert source_rank("rest", custom) < source_rank("live", custom)

    def test_source_priority_constant(self):
        assert SOURCE_PRIORITY == {"live": 0, "rest": 1, "db": 2}


# ============================================================
# build_tf — пакетная сборка ТФ (БД / реплей / скрипты)
# ============================================================
class TestBuildTf:
    def test_basic_ohlcv(self):
        cs = [
            _c(1, o=100, h=101, lo=99, c=100.5, v=10),
            _c(2, o=100.5, h=102, lo=100, c=101.5, v=20),
            _c(3, o=101.5, h=103, lo=101, c=102.5, v=30),
            _c(4, o=102.5, h=104, lo=102, c=103.5, v=40),
            _c(5, o=103.5, h=105, lo=103, c=104.5, v=50),
        ]
        out = build_tf(cs, "5min")
        assert len(out) == 1
        b = out[0]
        assert b.ts == T0  # канон START: минуты 10:01–10:04 → бакет 10:00
        assert (b.open, b.high, b.low, b.close) == (100.0, 104.0, 99.0, 103.5)
        assert b.volume == 100.0

    def test_start_time_convention(self):
        # канон START: минута 10:05 открывает бакет 10:05; минуты 10:01–10:04 — бакет 10:00
        cs = [_c(i, c=float(i)) for i in range(1, 11)]  # close 10:01..10:10
        out = build_tf(cs, 300)
        assert [b.ts for b in out] == [T0, T0 + timedelta(minutes=5)]
        assert out[0].close == 4.0
        assert out[1].close == 9.0

    def test_last_partial_dropped_by_default(self):
        cs = [_c(i, c=float(i)) for i in range(1, 8)]  # 10:01..10:07
        out = build_tf(cs, "5min")
        # бакет 10:00 закрыт приходом минуты 10:05; бакет 10:05 из 3 минут — хвост
        assert len(out) == 1
        assert out[0].ts == T0

    def test_last_partial_included_on_demand(self):
        cs = [_c(i, c=float(i)) for i in range(1, 8)]
        out = build_tf(cs, "5min", include_partial=True)
        assert len(out) == 2
        assert out[1].ts == T0 + timedelta(minutes=5)
        assert out[1].close == 7.0
        assert out[1].volume == 30.0  # 3 минуты × v=10

    def test_duplicates_and_late_do_not_double_count(self):
        cs = [_c(1, v=10), _c(2, v=10), _c(2, v=10), _c(1, v=10)]
        cs += [_c(3, v=10), _c(4, v=10), _c(5, v=10)]
        out = build_tf(cs, "5min")
        assert len(out) == 1
        assert out[0].volume == 40.0  # 4 полные минуты бакета 10:00; дубли не задвоили

    def test_gap_makes_short_bar(self):
        # пропуск 10:03–10:05: бакет 10:00 собрал 2 минуты и закрыт
        # приходом первой свечи следующего бакета (10:06)
        cs = [_c(1, v=10), _c(2, v=10), _c(6, v=10), _c(7, v=10)]
        out = build_tf(cs, "5min")
        assert len(out) == 1
        assert out[0].ts == T0
        assert out[0].volume == 20.0  # честно короткий бар

    def test_session_junction_no_cross_merge(self):
        # дневная сессия MOEX: последний 1m закрывается 15:45 UTC (18:45 МСК);
        # вечерняя: первый 1m — 16:05 UTC (19:05 МСК). Между ними клиринг.
        day_t0 = datetime(2026, 9, 1, 15, 40, tzinfo=U)
        day = [_c(i, o=100, h=100 + i, lo=100, c=100 + i, v=1, t0=day_t0)
               for i in range(1, 6)]
        eve_t0 = datetime(2026, 9, 1, 16, 0, tzinfo=U)
        eve = [_c(i, o=200, h=200 + i, lo=200, c=200 + i, v=1, t0=eve_t0)
               for i in range(1, 6)]
        out = build_tf(day + eve, "5min")
        # бары: 15:40 (минуты 15:41–15:44), 15:45 (одна минута, закрыт приходом 16:01),
        # 16:00 (минуты 16:01–16:04); пустые бакеты не существуют
        assert [b.ts for b in out] == [
            datetime(2026, 9, 1, 15, 40, tzinfo=U),
            datetime(2026, 9, 1, 15, 45, tzinfo=U),
            datetime(2026, 9, 1, 16, 0, tzinfo=U),
        ]
        assert out[0].close == 104.0 and out[0].high == 104.0  # вечер не подмешался
        assert out[1].close == 105.0 and out[1].volume == 1.0
        assert out[2].open == 200.0

    def test_invalid_candles_skipped_by_validator(self):
        # v2: битые свечи бракуются валидатором и не портят бар
        cs = [_c(1, v=10), _c(2, v=float("nan")), _c(3, v=10),
              _c(4, v=10), _c(5, v=10)]
        out = build_tf(cs, "5min")
        assert len(out) == 1
        assert out[0].volume == 30.0  # 3 здоровые минуты полного бакета 10:00


# ============================================================
# CandleSeries — события, дедуп, maxlen (v1)
# ============================================================
class TestCandleSeries:
    def test_next_bucket_minute_closes_bar(self):
        s = CandleSeries("F", 300)
        closed = []
        s.on_closed.append(closed.append)
        for i in range(1, 6):
            s.push_1m(_c(i))
        # минута 10:05 открывает новый бакет — предыдущий [10:00,10:05) закрыт
        assert len(closed) == 1
        assert closed[0].ts == T0
        assert s.partial is not None and s.partial.ts == T0 + timedelta(minutes=5)
        assert len(s) == 1

    def test_on_updated_stream(self):
        s = CandleSeries("F", 300)
        updated = []
        s.on_updated.append(updated.append)
        s.push_1m(_c(1, c=1.0))
        s.push_1m(_c(2, c=2.0))
        assert len(updated) == 2
        assert s.partial.ts == T0
        assert updated[-1].close == 2.0
        assert updated[-1] == s.partial

    def test_skips_duplicates_and_late(self):
        s = CandleSeries("F", 300)
        s.push_1m(_c(1))
        s.push_1m(_c(2))
        s.push_1m(_c(2))  # дубль
        s.push_1m(_c(1))  # опоздавший
        s.push_1m(_c(3))
        assert s.skipped == 2
        assert s.partial.volume == 30.0  # 3 минуты, а не 5

    def test_maxlen_eviction(self):
        s = CandleSeries("F", 60, maxlen=3)
        for i in range(1, 6):
            s.push_1m(_c(i))
        assert len(s) == 3
        assert s.snapshot()[0].ts == T0 + timedelta(minutes=3)

    def test_snapshot_n(self):
        s = CandleSeries("F", 60)
        for i in range(1, 6):
            s.push_1m(_c(i))
        assert len(s.snapshot(2)) == 2
        assert s.snapshot(2)[-1].ts == T0 + timedelta(minutes=5)
        assert s.snapshot(0) == []

    def test_daily_bar_needs_finalize(self):
        # day-бакет метится НАЧАЛОМ суток (00:00 UTC, канон START) и закрывается
        # только явным finalize — торговые минуты заканчиваются раньше границы
        s = CandleSeries("F", 86400)
        t0 = datetime(2026, 9, 1, 20, 40, tzinfo=U)
        for i in range(1, 11):  # 20:41..20:50 UTC — хвост вечерней сессии
            s.push_1m(_c(i, t0=t0))
        assert len(s) == 0 and s.partial is not None
        s.finalize()
        assert len(s) == 1
        assert s.last.ts == datetime(2026, 9, 1, 0, 0, tzinfo=U)


# ============================================================
# v2: CandleSeries — reject / replace / insert
# ============================================================
class TestCandleSeriesV2:
    def test_invalid_candle_rejected_not_stored(self):
        s = CandleSeries("F", 60)
        rejected = []
        s.on_rejected.append(lambda c, src, reason: rejected.append((c, src, reason)))
        assert s.push_1m(_c(1, v=float("nan"))) == "reject"
        assert s.push_1m(_c(2, o=100, h=90, lo=99, c=110)) == "reject"
        assert len(s) == 0 and s.rejected == 2
        assert s.last is None
        assert rejected[0][2] == "nan" and rejected[0][1] == "live"
        assert rejected[1][2] == "bad_range"

    def test_reject_does_not_break_stream(self):
        s = CandleSeries("F", 60)
        s.push_1m(_c(1))
        assert s.push_1m(_c(2, v=-1)) == "reject"
        s.push_1m(_c(3))
        assert len(s) == 2
        assert [c.ts.minute for c in s.snapshot()] == [1, 3]

    def test_replace_by_better_source(self):
        s = CandleSeries("F", 60)
        s.push_1m(_c(1, c=100.0), source="db")
        rebuilt = []
        s.on_rebuilt.append(lambda: rebuilt.append(1))
        # та же минута от live (довереннее db) с ДРУГИМИ значениями — замена
        assert s.push_1m(_c(1, c=105.0), source="live") == "replace"
        assert len(s) == 1 and s.last.close == 105.0
        assert rebuilt == [1]

    def test_same_values_from_better_source_is_quiet_dup(self):
        s = CandleSeries("F", 60)
        s.push_1m(_c(1, c=100.0), source="db")
        rebuilt = []
        s.on_rebuilt.append(lambda: rebuilt.append(1))
        # значения совпали — пересборка не нужна, тихий дубликат
        assert s.push_1m(_c(1, c=100.0), source="live") == "dup"
        assert rebuilt == [] and s.skipped == 1

    def test_worse_source_cannot_replace(self):
        s = CandleSeries("F", 60)
        s.push_1m(_c(1, c=100.0), source="live")
        assert s.push_1m(_c(1, c=105.0), source="db") == "dup"
        assert s.last.close == 100.0 and s.skipped == 1

    def test_custom_priority_respected(self):
        s = CandleSeries("F", 60, source_priority={"iss": 0, "live": 1})
        s.push_1m(_c(1, c=100.0), source="live")
        assert s.push_1m(_c(1, c=103.0), source="iss") == "replace"
        assert s.last.close == 103.0

    def test_late_minute_inserted(self):
        s = CandleSeries("F", 60)
        for i in (1, 2, 4, 5):
            s.push_1m(_c(i))
        rebuilt = []
        s.on_rebuilt.append(lambda: rebuilt.append(1))
        # опоздавшая минута 3 — дыра закрыта вставкой на своё место
        assert s.push_1m(_c(3, c=77.0)) == "insert"
        assert [c.ts.minute for c in s.snapshot()] == [1, 2, 3, 4, 5]
        assert s.snapshot()[2].close == 77.0
        assert rebuilt == [1]

    def test_late_older_than_history_skipped_when_full(self):
        s = CandleSeries("F", 60, maxlen=3)
        for i in (3, 4, 5):
            s.push_1m(_c(i))
        # история полна, минута 1 старше всего хранимого — места нет
        assert s.push_1m(_c(1)) == "dup"
        assert len(s) == 3 and s.skipped == 1

    def test_late_in_derived_tf_is_dup_at_series_level(self):
        # на уровне СЕРИИ производный ТФ не вставляет опоздавших —
        # этим занимается CandleHub (rebuild из 1m-истории)
        s = CandleSeries("F", 300)
        s.push_1m(_c(1))
        s.push_1m(_c(2))
        assert s.push_1m(_c(1)) == "dup"
        assert s.skipped == 1


# ============================================================
# CandleHub — серии, события, seed, finalize (v1)
# ============================================================
class TestCandleHub:
    def test_events_and_history(self):
        hub = CandleHub()
        s5 = hub.series("FIGI", "5min")
        s1 = hub.series("FIGI", "1min")
        closed5, updated5, closed1 = [], [], []
        s5.on_closed.append(closed5.append)
        s5.on_updated.append(updated5.append)
        s1.on_closed.append(closed1.append)

        for i in range(1, 11):
            hub.ingest_1m("FIGI", _c(i, c=float(i)))

        # 1m-ряд: каждый бар закрыт сразу
        assert [c.ts for c in closed1] == [
            T0 + timedelta(minutes=i) for i in range(1, 11)
        ]
        # 5m-ряд: два закрытых бара (10:00, 10:05), по одному on_updated на минуту
        assert [c.ts for c in closed5] == [
            T0,
            T0 + timedelta(minutes=5),
        ]
        assert closed5[0].close == 4.0 and closed5[1].close == 9.0
        assert len(updated5) == 10
        assert s5.snapshot() == closed5
        assert s5.partial is not None and s5.partial.ts == T0 + timedelta(minutes=10)

    def test_auto_creates_1m_series(self):
        hub = CandleHub()
        hub.series("F", "5min")
        for i in range(1, 4):
            hub.ingest_1m("F", _c(i))
        s1 = hub.series("F", "1min")  # уже существует — создан hub-ом
        assert len(s1) == 3
        assert s1.last.ts == T0 + timedelta(minutes=3)
        assert hub.stats()[("F", 300)]["closed"] == 0

    def test_seed_is_quiet_then_live_fires(self):
        hub = CandleHub()
        s5 = hub.series("F", "5min")
        ev = []
        s5.on_closed.append(ev.append)
        hub.seed_1m("F", [_c(i, c=float(i)) for i in range(1, 6)])
        assert ev == []  # прогрев молчит
        assert len(s5) == 1 and s5.partial is not None  # бакет 10:00 закрыт; 10:05 формируется
        assert s5.partial.close == 5.0
        hub.ingest_1m("F", _c(6, c=6.0))
        hub.ingest_1m("F", _c(7, c=7.0))
        assert ev == []  # новый бакет ещё формируется
        assert s5.partial is not None and s5.partial.close == 7.0
        # v2: stats с пятью ключами; rebuilds=1 — один прогрев семенем
        assert hub.stats()[("F", 300)] == {
            "closed": 1, "partial": True, "skipped": 0,
            "rejected": 0, "rebuilds": 1,
        }

    def test_finalize_midframe(self):
        hub = CandleHub()
        s5 = hub.series("F", "5min")
        # поток оборвался на 3-й минуте бакета 10:05 (гэп данных/конец реплея)
        for i in (6, 7, 8):
            hub.ingest_1m("F", _c(i, v=10.0))
        assert len(s5) == 0 and s5.partial is not None
        closed = hub.finalize("F")
        assert len(s5) == 1
        assert s5.last.ts == T0 + timedelta(minutes=5)
        assert s5.last.volume == 30.0  # 3 минуты из 5 — бар короткий, но закрыт
        assert [c.ts for c in closed] == [T0 + timedelta(minutes=5)]

    def test_figis_are_independent(self):
        hub = CandleHub()
        a = hub.series("A", "5min")
        b = hub.series("B", "5min")
        hub.ingest_1m("A", _c(1, c=1.0))
        hub.ingest_1m("B", _c(1, c=99.0))
        assert a.partial.close == 1.0
        assert b.partial.close == 99.0
        assert hub.keys() == [("A", 60), ("A", 300), ("B", 60), ("B", 300)]

    def test_series_late_subscription_needs_seed(self):
        hub = CandleHub()
        for i in range(1, 6):
            hub.ingest_1m("F", _c(i, c=float(i)))
        s5 = hub.series("F", "5min")  # подписка ПОСЛЕ данных — ряд пуст
        assert len(s5) == 0
        # прогрев историей: старые ряды пропустят дубли, новый построится
        hub.seed_1m("F", [_c(i, c=float(i)) for i in range(1, 6)])
        assert len(s5) == 1 and s5.last.close == 4.0


# ============================================================
# v2: CandleHub — ingest replace/insert → rebuild производных ТФ
# ============================================================
class TestHubIngestV2:
    def test_replace_rebuilds_derived_tf(self):
        hub = CandleHub()
        s5 = hub.series("F", "5min")
        rebuilt = []
        s5.on_rebuilt.append(lambda: rebuilt.append(1))
        for i in range(1, 6):
            hub.ingest_1m("F", _c(i, o=float(i), c=float(i)), source="db")
        assert len(s5) == 1 and s5.last.close == 4.0 and s5.last.high == 4.0
        assert rebuilt == []  # обычный append не перестраивает историю
        # минута 3 от live (довереннее db) с другими значениями — замена
        assert hub.ingest_1m("F", _c(3, o=300.0, c=300.0), source="live") == "replace"
        assert s5.last.close == 4.0    # close бара не изменился
        assert s5.last.high == 300.0   # а high пересчитан
        assert rebuilt == [1] and s5.rebuilds == 1
        # 1m-ряд видит исправленную минуту
        assert hub.series("F", "1min").snapshot()[2].close == 300.0

    def test_insert_rebuilds_derived_tf(self):
        hub = CandleHub()
        s5 = hub.series("F", "5min")
        for i in (1, 2, 4, 5):
            hub.ingest_1m("F", _c(i, c=float(i)))
        # бакет 10:00 собран из 3 минут (дыра на 10:03); минута 10:05 — в partial
        assert len(s5) == 1 and s5.last.volume == 30.0
        # опоздавшая минута 3 закрывает дыру — производный ТФ перестроен
        assert hub.ingest_1m("F", _c(3, c=3.0)) == "insert"
        assert s5.last.volume == 40.0
        assert s5.rebuilds == 1

    def test_reject_does_not_touch_series(self):
        hub = CandleHub()
        hub.series("F", "5min")
        assert hub.ingest_1m("F", _c(1, v=float("nan"))) == "reject"
        assert hub.series("F", "1min").rejected == 1
        assert hub.stats()[("F", 60)]["rejected"] == 1
        assert hub.stats()[("F", 300)]["closed"] == 0


# ============================================================
# v2: merge_1m / seed_1m — батч-докачка и коррекция
# ============================================================
class TestMerge:
    def test_merge_fills_holes(self):
        s = CandleSeries("F", 60)
        for i in (1, 2, 5, 6):
            s.push_1m(_c(i))
        assert s.merge_1m([_c(3), _c(4)], source="db") is True
        assert [c.ts.minute for c in s.snapshot()] == [1, 2, 3, 4, 5, 6]

    def test_merge_batch_last_duplicate_wins(self):
        s = CandleSeries("F", 60)
        s.push_1m(_c(1, c=10.0))
        # внутри батча при дубле ts побеждает поздний
        s.merge_1m([_c(2, c=20.0), _c(2, c=25.0)])
        assert s.snapshot()[1].close == 25.0

    def test_merge_better_source_replaces(self):
        s = CandleSeries("F", 60)
        s.push_1m(_c(1, c=10.0), source="db")
        assert s.merge_1m([_c(1, c=11.0)], source="live") is True
        assert s.last.close == 11.0

    def test_merge_worse_source_ignored(self):
        s = CandleSeries("F", 60)
        s.push_1m(_c(1, c=10.0), source="live")
        assert s.merge_1m([_c(1, c=11.0)], source="db") is False
        assert s.last.close == 10.0 and s.skipped == 1

    def test_merge_identical_is_noop(self):
        s = CandleSeries("F", 60)
        s.push_1m(_c(1, c=10.0), source="live")
        rebuilt = []
        s.on_rebuilt.append(lambda: rebuilt.append(1))
        assert s.merge_1m([_c(1, c=10.0)], source="db") is False
        assert rebuilt == [] and s.skipped == 1

    def test_merge_rejects_invalid(self):
        s = CandleSeries("F", 60)
        assert s.merge_1m([_c(1, v=float("nan")), _c(2, v=-1)]) is False
        assert len(s) == 0 and s.rejected == 2

    def test_merge_derived_tf_forbidden(self):
        s = CandleSeries("F", 300)
        with pytest.raises(ValueError):
            s.merge_1m([_c(1)])

    def test_merge_respects_maxlen(self):
        s = CandleSeries("F", 60, maxlen=3)
        s.merge_1m([_c(i) for i in range(1, 7)])  # 6 минут, влезут 3 последних
        assert [c.ts.minute for c in s.snapshot()] == [4, 5, 6]

    def test_merge_fires_on_rebuilt_once(self):
        s = CandleSeries("F", 60)
        s.push_1m(_c(1))
        rebuilt = []
        s.on_rebuilt.append(lambda: rebuilt.append(1))
        s.merge_1m([_c(2), _c(3)])
        assert rebuilt == [1]  # один батч — одно событие, не по свече

    def test_seed_rebuilds_derived_after_hole_fill(self):
        hub = CandleHub()
        s5 = hub.series("F", "5min")
        for i in (1, 2, 4, 5):
            hub.ingest_1m("F", _c(i, c=float(i)))
        assert s5.last.volume == 30.0  # дыра на минуте 3
        hub.seed_1m("F", [_c(3, c=3.0)], source="db")
        assert s5.last.volume == 40.0
        assert s5.rebuilds == 1

    def test_seed_idempotent(self):
        hub = CandleHub()
        s5 = hub.series("F", "5min")
        candles = [_c(i, c=float(i)) for i in range(1, 6)]
        hub.seed_1m("F", candles, source="db")
        assert s5.rebuilds == 1 and len(s5) == 1
        hub.seed_1m("F", candles, source="db")  # повторный seed — no-op
        assert s5.rebuilds == 1 and len(s5) == 1


# ============================================================
# v2: gap_report — дыры 1m-истории
# ============================================================
class TestGapReport:
    def test_no_series_or_too_short(self):
        hub = CandleHub()
        assert hub.gap_report("F") == []
        hub.series("F", "1min")
        assert hub.gap_report("F") == []
        hub.ingest_1m("F", _c(1))
        assert hub.gap_report("F") == []  # одна свеча — интервала нет

    def test_raw_gap_without_calendar(self):
        hub = CandleHub()
        for i in (1, 2, 5, 8):
            hub.ingest_1m("F", _c(i))
        # дыры: 10:03–10:04 и 10:06–10:07 (close-time отсутствующих минут)
        assert hub.gap_report("F") == [
            (T0 + timedelta(minutes=3), T0 + timedelta(minutes=4)),
            (T0 + timedelta(minutes=6), T0 + timedelta(minutes=7)),
        ]

    def test_calendar_excludes_non_trading_minutes(self):
        hub = CandleHub()
        for i in (1, 2, 5):
            hub.ingest_1m("F", _c(i))

        def cal(m):  # минута 4 — неторговая (клиринг/ночь)
            return m.minute != 4

        assert hub.gap_report("F", is_trading_minute=cal) == [
            (T0 + timedelta(minutes=3), T0 + timedelta(minutes=3))
        ]

    def test_calendar_breaks_range_on_non_trading(self):
        hub = CandleHub()
        for i in (1, 6):
            hub.ingest_1m("F", _c(i))

        def cal(m):  # торгуемые только минуты 2 и 5 — два диапазона
            return m.minute in (2, 5)

        assert hub.gap_report("F", is_trading_minute=cal) == [
            (T0 + timedelta(minutes=2), T0 + timedelta(minutes=2)),
            (T0 + timedelta(minutes=5), T0 + timedelta(minutes=5)),
        ]


# ============================================================
# v2: stats — счётчики здоровья серий
# ============================================================
class TestStats:
    def test_stats_keys_and_counters(self):
        hub = CandleHub()
        hub.series("F", "5min")
        hub.ingest_1m("F", _c(1, v=float("nan")))  # reject
        hub.ingest_1m("F", _c(1))                   # append
        hub.ingest_1m("F", _c(1))                   # dup (значения те же)
        st = hub.stats()[("F", 60)]
        assert set(st) == {"closed", "partial", "skipped", "rejected", "rebuilds"}
        assert st["closed"] == 1 and st["rejected"] == 1 and st["skipped"] == 1
        assert st["rebuilds"] == 0 and st["partial"] is False
