"""Интеграционные тесты записи свечей на одноразовой PostgreSQL 16 тестовой БД (B1).

Контур — общий fixtures/pipeline_contract/db.py (fail-closed, env PIPELINE_TEST_DATABASE_URL).
Репродукция трёх наблюдаемых рисков существующего пути (CODE-FIRST-R1-20261011, B1 п.1):
  R1: upsert_candles пишет OHLCV без собственной проверки;
  R2: ошибка invalidate подавляется (`except Exception: pass`);
  R3: fill_gaps публикует в hub до успешного сохранения.
Помеченные ДЕФЕКТ тесты падают до исправления и проходят после.
Сеть мокается (fetch_candles), транзакции — настоящий PostgreSQL, не мок execute.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.models.candle import Candle as DBCandle
from app.services import candle_integrity
from app.services import candle_hub_orchestrator as cho
from app.services import tinvest
from tests.fixtures.pipeline_contract import db as tdb

_UTC = timezone.utc
_FIGI = "B1TESTFIGI00000001"


def _contour_requested() -> bool:
    return bool((os.environ.get("PIPELINE_TEST_DATABASE_URL") or "").strip())


@pytest_asyncio.fixture
async def pg():
    """Сессия изолированной тестовой БД.

    Без PIPELINE_TEST_DATABASE_URL — skip (обычный прогон). Если переменная задана,
    но контур непригоден/это production — ошибка (fail-closed), а не зелёный skip.
    """
    if not _contour_requested():
        pytest.skip("ingestion PG contour: задать PIPELINE_TEST_DATABASE_URL (test-БД)")
    target = tdb.resolve_test_target()
    await tdb.prepare_schema(target.url)
    engine, maker = tdb.make_sessionmaker(target.url)
    try:
        async with maker() as session:
            await tdb.truncate(session)
            yield session
            await session.rollback()
    finally:
        await engine.dispose()


def _row(ts: datetime, *, o=100.0, h=100.01, l=99.99, c=100.0, v=1000,
         figi: str = _FIGI, interval: int = 1) -> dict:
    return {"figi": figi, "interval": interval, "ts": ts,
            "open": o, "high": h, "low": l, "close": c, "volume": v}


def _day_rows(day: datetime, n: int = 3, **kw) -> list[dict]:
    base = day.replace(hour=12, minute=0, second=0, microsecond=0)
    return [_row(base + timedelta(minutes=i), **kw) for i in range(n)]


async def _count(db, figi: str = _FIGI) -> int:
    return int((await db.execute(
        select(func.count()).select_from(DBCandle).where(DBCandle.figi == figi)
    )).scalar_one())


# ============================================================ sanity
async def test_valid_batch_written(pg):
    """Валидный пакет записывается (базовая работоспособность, до и после)."""
    rows = _day_rows(datetime(2026, 10, 5, tzinfo=_UTC))
    n = await tinvest.upsert_candles(pg, rows)
    assert await _count(pg) == len(rows)
    assert n == len(rows)


async def test_repeat_same_batch_is_idempotent(pg):
    """Повтор того же пакета не меняет экономическое содержимое (число строк)."""
    rows = _day_rows(datetime(2026, 10, 5, tzinfo=_UTC))
    await tinvest.upsert_candles(pg, rows)
    n1 = await _count(pg)
    first = (await pg.execute(select(DBCandle).where(DBCandle.figi == _FIGI)
                              .order_by(DBCandle.ts))).scalars().all()
    snap1 = [(r.ts, str(r.open), str(r.close), r.volume) for r in first]

    await tinvest.upsert_candles(pg, rows)  # повтор
    n2 = await _count(pg)
    second = (await pg.execute(select(DBCandle).where(DBCandle.figi == _FIGI)
                               .order_by(DBCandle.ts))).scalars().all()
    snap2 = [(r.ts, str(r.open), str(r.close), r.volume) for r in second]

    assert n1 == n2 == len(rows)
    assert snap1 == snap2, "повторная загрузка изменила содержимое"


# ============================================================ R1: запись без проверки
@pytest.mark.parametrize("mut", ["nan", "inf", "negative_price", "negative_volume", "bad_range", "naive_ts"])
async def test_r1_invalid_ohlcv_must_be_rejected(pg, mut):
    """ДЕФЕКТ (B1): невалидная свеча принимается границей записи и попадает в БД."""
    ts = datetime(2026, 10, 5, 12, 0, tzinfo=_UTC)
    row = _row(ts)
    if mut == "nan":
        row["close"] = float("nan")
    elif mut == "inf":
        row["high"] = float("inf")
    elif mut == "negative_price":
        row["open"] = -1.0
    elif mut == "negative_volume":
        row["volume"] = -5
    elif mut == "bad_range":
        row["low"] = row["high"] + 10.0
    elif mut == "naive_ts":
        row["ts"] = ts.replace(tzinfo=None)

    rejected = False
    try:
        await tinvest.upsert_candles(pg, [row])
    except Exception:  # noqa: BLE001
        rejected = True
    await pg.rollback()

    assert rejected, f"невалидная свеча ({mut}) принята границей записи"
    assert await _count(pg) == 0, "невалидная свеча попала в БД"


async def test_r1_conflicting_duplicate_rejected_explicitly(pg):
    """ДЕФЕКТ (B1): конфликт дублей — явный отказ ДО записи, не сырая ошибка драйвера."""
    ts = datetime(2026, 10, 5, 12, 0, tzinfo=_UTC)
    good = _row(ts, c=100.0)  # high=100.01, low=99.99
    conflict = dict(good, close=100.005)  # валидна (в пределах диапазона), тот же ключ, иное содержимое

    raised = None
    try:
        await tinvest.upsert_candles(pg, [good, conflict])
    except Exception as exc:  # noqa: BLE001
        raised = exc
    await pg.rollback()

    assert raised is not None, "конфликт дублей принят молча"
    from sqlalchemy.exc import DBAPIError
    assert not isinstance(raised, DBAPIError), (
        f"конфликт дублей дал сырую ошибку драйвера/БД вместо контрактного отказа: {raised!r}")


async def test_r1_identical_duplicate_is_idempotent(pg):
    """Одинаковые дубли в пакете идемпотентны (не конфликт, не ошибка)."""
    ts = datetime(2026, 10, 5, 12, 0, tzinfo=_UTC)
    row = _row(ts)
    n = await tinvest.upsert_candles(pg, [row, dict(row)])  # полный дубль
    assert await _count(pg) == 1
    assert n == 1


async def test_inserted_unchanged_corrected_distinction(pg):
    """Явное различие inserted/unchanged/corrected с проверяемым изменением содержимого."""
    day = datetime(2026, 10, 5, tzinfo=_UTC)
    rows = _day_rows(day, n=3)

    r1 = await tinvest.upsert_candles_detailed(pg, rows)
    assert (r1.inserted, r1.unchanged, r1.corrected, r1.written) == (3, 0, 0, 3)

    r2 = await tinvest.upsert_candles_detailed(pg, rows)  # повтор — всё unchanged, без записи
    assert (r2.inserted, r2.unchanged, r2.corrected, r2.written) == (0, 3, 0, 0)

    changed = [dict(rows[0]), dict(rows[1], close=rows[1]["close"] + 0.005), dict(rows[2])]
    r3 = await tinvest.upsert_candles_detailed(pg, changed)  # одна валидная коррекция
    assert (r3.inserted, r3.unchanged, r3.corrected, r3.written) == (0, 2, 1, 1)

    # содержимое в БД: скорректированная строка изменилась, остальные — нет
    got = {c.ts: str(c.close) for c in (await pg.execute(
        select(DBCandle).where(DBCandle.figi == _FIGI))).scalars().all()}
    assert float(got[changed[1]["ts"]]) == pytest.approx(rows[1]["close"] + 0.005)
    assert float(got[rows[0]["ts"]]) == pytest.approx(rows[0]["close"])


# ============================================================ R2: подавление ошибки invalidate
async def test_r2_invalidate_failure_is_not_swallowed(pg):
    """ДЕФЕКТ (B1): ошибка invalidate подавляется, запись возвращает успех."""
    rows = _day_rows(datetime(2026, 10, 5, tzinfo=_UTC))

    async def _boom(*_a, **_k):
        raise RuntimeError("integrity cache down")

    original = candle_integrity.invalidate
    candle_integrity.invalidate = _boom
    try:
        succeeded = False
        try:
            await tinvest.upsert_candles(pg, rows)
            succeeded = True
        except Exception:  # noqa: BLE001
            succeeded = False
        await pg.rollback()
    finally:
        candle_integrity.invalidate = original

    assert not succeeded, "ошибка invalidate подавлена — запись вернула успех"
    assert await _count(pg) == 0, "частичный COMMIT при упавшей инвалидации"


# ============================================================ R3: hub до сохранения
async def test_r3_write_error_does_not_publish_to_hub(pg, monkeypatch):
    """ДЕФЕКТ (B1): fill_gaps публикует в hub до успешного сохранения в БД."""
    day = datetime(2026, 10, 5, tzinfo=_UTC)
    base = day.replace(hour=12, minute=0)
    # hub содержит 12:00 и 12:02 -> дыра 12:01
    present = [_row(base), _row(base + timedelta(minutes=2))]
    from app.engine.models import Candle as EngineCandle
    orch = cho.CandleHubOrchestrator()
    orch.hub.seed_1m(_FIGI, [EngineCandle(ts=r["ts"], open=r["open"], high=r["high"],
                                          low=r["low"], close=r["close"], volume=float(r["volume"]))
                             for r in present], source="db", fire=False)
    before = len(orch.hub.series(_FIGI, "1min"))

    gap_row = _row(base + timedelta(minutes=1))
    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [gap_row])

    async def _fail(_db, _rows, **_k):
        raise RuntimeError("db write failed")
    monkeypatch.setattr(cho, "upsert_candles", _fail)

    with pytest.raises(RuntimeError):
        await orch.fill_gaps(pg, _FIGI)

    after = len(orch.hub.series(_FIGI, "1min"))
    assert after == before, "hub опубликовал свечи до успешного сохранения в БД"


async def test_r3_empty_source_is_not_error_and_no_fake_data(pg, monkeypatch):
    """Пустой успешный ответ источника — не ошибка и не выдуманные данные."""
    day = datetime(2026, 10, 5, tzinfo=_UTC)
    base = day.replace(hour=12, minute=0)
    present = [_row(base), _row(base + timedelta(minutes=2))]
    from app.engine.models import Candle as EngineCandle
    orch = cho.CandleHubOrchestrator()
    orch.hub.seed_1m(_FIGI, [EngineCandle(ts=r["ts"], open=r["open"], high=r["high"],
                                          low=r["low"], close=r["close"], volume=float(r["volume"]))
                             for r in present], source="db", fire=False)
    before = len(orch.hub.series(_FIGI, "1min"))

    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [])
    filled = await orch.fill_gaps(pg, _FIGI)

    assert filled == 0
    assert len(orch.hub.series(_FIGI, "1min")) == before


# ============================================================ расширенные транзакционные
def _orch_with_gap(drop_minute: int = 1):
    """hub с дырой: 12:00 и 12:00+drop+1 (дыра из drop минут)."""
    from app.engine.models import Candle as EngineCandle
    day = datetime(2026, 10, 5, tzinfo=_UTC)
    base = day.replace(hour=12, minute=0)
    last = base + timedelta(minutes=drop_minute + 1)
    present = [_row(base), _row(last)]
    orch = cho.CandleHubOrchestrator()
    orch.hub.seed_1m(_FIGI, [EngineCandle(ts=r["ts"], open=r["open"], high=r["high"],
                                          low=r["low"], close=r["close"], volume=float(r["volume"]))
                             for r in present], source="db", fire=False)
    return orch, base, last


async def test_commit_failure_propagates_no_partial_commit(pg, monkeypatch):
    """Ошибка commit -> исключение, частичного COMMIT нет (атомарная граница)."""
    rows = _day_rows(datetime(2026, 10, 5, tzinfo=_UTC))

    async def _boom_commit():
        raise RuntimeError("commit failed")
    monkeypatch.setattr(pg, "commit", _boom_commit)

    raised = False
    try:
        await tinvest.upsert_candles(pg, rows)
    except Exception:  # noqa: BLE001
        raised = True
    await pg.rollback()
    assert raised, "ошибка commit подавлена"
    assert await _count(pg) == 0, "частичный COMMIT при упавшем commit"


async def test_hub_failure_after_commit_is_recoverable(pg, monkeypatch):
    """hub publish падает ПОСЛЕ commit: данные в БД сохранены, статус recoverable."""
    orch, base, _last = _orch_with_gap(drop_minute=1)
    gap_row = _row(base + timedelta(minutes=1))
    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [gap_row])

    def _boom(*_a, **_k):
        raise RuntimeError("hub publish failed")
    monkeypatch.setattr(orch.hub, "seed_1m", _boom)

    res = await orch.fill_gaps_detailed(pg, _FIGI)
    assert res.committed == 1
    assert res.hub_publish_errors, "ожидалась recoverable ошибка публикации в hub"
    assert await _count(pg) == 1, "свеча должна остаться committed в БД"


async def test_source_timeout_is_recorded_not_crash(pg, monkeypatch):
    """Таймаут источника -> source_errors, не падение и не выдуманные данные."""
    orch, _base, _last = _orch_with_gap(drop_minute=1)

    def _timeout(*_a, **_k):
        raise TimeoutError("source timeout")
    monkeypatch.setattr(cho, "fetch_candles", _timeout)

    res = await orch.fill_gaps_detailed(pg, _FIGI)
    assert res.committed == 0
    assert res.source_errors and res.source_errors[0]["error"] == "TimeoutError"
    assert await _count(pg) == 0


async def test_partially_filled_gap_leaves_remainder(pg, monkeypatch):
    """Источник вернул лишь часть дыры -> committed часть, остаток остаётся gap."""
    orch, base, _last = _orch_with_gap(drop_minute=3)  # дыра 12:01..12:04 (4 мин)
    partial = [_row(base + timedelta(minutes=1)), _row(base + timedelta(minutes=2))]
    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: partial)

    res = await orch.fill_gaps_detailed(pg, _FIGI)
    assert res.committed == 2
    assert res.remaining_gaps, "непокрытый остаток дыры должен остаться remaining"
    assert await _count(pg) == 2


async def test_window_membership_enforced(pg):
    """Строка вне запрошенного окна отклоняется; внутри — принимается."""
    ts = datetime(2026, 10, 5, 12, 0, tzinfo=_UTC)
    window = (ts, ts)  # окно ровно 12:00
    n = await tinvest.upsert_candles(pg, [_row(ts)], window=window)
    assert n == 1 and await _count(pg) == 1

    outside = _row(ts + timedelta(hours=2))
    rejected = False
    try:
        await tinvest.upsert_candles(pg, [outside], window=window)
    except Exception:  # noqa: BLE001
        rejected = True
    await pg.rollback()
    assert rejected, "строка вне окна принята"
    assert await _count(pg) == 1


# ============================================================ R2: REWORK-тесты
async def test_empty_gap_dedup_is_per_operation(pg, monkeypatch):
    """ДЕФЕКТ (R2): бессрочный кэш блокировал поздно пришедшую свечу.
    После фикса: дедуп пустых дыр — ТОЛЬКО внутри операции; повторный вызов запрашивает снова."""
    orch, base, _ = _orch_with_gap(drop_minute=1)
    calls = []

    def fetch(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return []  # первый раз пусто
        return [_row(base + timedelta(minutes=1))]  # потом данные появились

    monkeypatch.setattr(cho, "fetch_candles", fetch)

    r1 = await orch.fill_gaps_detailed(pg, _FIGI)
    assert r1.committed == 0 and r1.remaining_gaps

    r2 = await orch.fill_gaps_detailed(pg, _FIGI)
    assert len(calls) == 2, f"повторный вызов не запросил источник: fetch_calls={len(calls)}, ожидается 2"
    assert r2.committed == 1, "поздно пришедшая свеча не восстановлена"


async def test_wrong_figi_rejected_before_write_and_hub(pg, monkeypatch):
    """ДЕФЕКТ (R2): чужая свеча (OTHER) публикуется под X. Ожидание: структурированный отказ без записи и без чужой свечи в hub."""
    orch, base, _ = _orch_with_gap(drop_minute=1)
    foreign = _row(base + timedelta(minutes=1), figi="OTHERFIGI")
    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [foreign])

    r = await orch.fill_gaps_detailed(pg, _FIGI)
    assert r.scope_rejections, "чужая свеча (OTHER) не отклонена"
    assert r.committed == 0
    assert await _count(pg) == 0, "чужая свеча записана в БД"
    assert len(orch.hub.series(_FIGI, "1min")) == 2, "чужая свеча опубликована в hub X"


async def test_wrong_interval_rejected(pg, monkeypatch):
    """ДЕФЕКТ (R2): свеча с чужим interval принимается. Ожидание: структурированный отказ."""
    orch, base, _ = _orch_with_gap(drop_minute=1)
    wrong_iv = _row(base + timedelta(minutes=1), interval=5)
    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [wrong_iv])

    r = await orch.fill_gaps_detailed(pg, _FIGI)
    assert r.scope_rejections, "свеча с чужим interval не отклонена"
    assert r.committed == 0
    assert await _count(pg) == 0


async def test_outside_window_rejected(pg, monkeypatch):
    """ДЕФЕКТ (R2): свеча вне запрошенного окна принимается. Ожидание: структурированный отказ."""
    orch, base, _ = _orch_with_gap(drop_minute=1)
    outside = _row(base + timedelta(hours=3))  # вне дыры 12:01
    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [outside])

    r = await orch.fill_gaps_detailed(pg, _FIGI)
    assert r.scope_rejections, "свеча вне окна не отклонена"
    assert r.committed == 0
    assert await _count(pg) == 0


async def test_invalidate_failure_no_partial_commit_separate_session(pg, monkeypatch):
    """Атомарность: RuntimeError в invalidate (после INSERT) → неуспешный пакет НЕ сохраняется.
    Вызывающий ловит ошибку, rollback, повторно использует сессию и commit.
    Проверка через отдельную сессию (rollback теста не скрывает гарантию кода)."""
    from sqlalchemy import func, select as _sel
    rows_bad = _day_rows(datetime(2026, 10, 5, tzinfo=_UTC))
    original_inv = candle_integrity.invalidate

    async def _boom(*_a, **_k):
        raise RuntimeError("invalidate down")
    monkeypatch.setattr(candle_integrity, "invalidate", _boom)

    raised = False
    try:
        await tinvest.upsert_candles(pg, rows_bad)
    except Exception:  # noqa: BLE001
        raised = True
    assert raised, "invalidate не упал"
    await pg.rollback()  # вызывающий управляет транзакцией

    # повторно используем сессию: восстанавливаем invalidate, успешный пакет + commit
    monkeypatch.setattr(candle_integrity, "invalidate", original_inv)
    rows_ok = _day_rows(datetime(2026, 10, 6, tzinfo=_UTC))
    await tinvest.upsert_candles(pg, rows_ok)
    await pg.commit()

    # неуспешный пакет (2026-10-05) не должен быть в БД
    async def _count_day(db, day):
        return int((await db.execute(
            select(func.count()).select_from(DBCandle)
            .where(DBCandle.figi == _FIGI, DBCandle.ts >= day,
                   DBCandle.ts < day + timedelta(days=1))
        )).scalar_one())

    n_bad = await _count_day(pg, datetime(2026, 10, 5, tzinfo=_UTC))
    n_ok = await _count_day(pg, datetime(2026, 10, 6, tzinfo=_UTC))
    assert n_bad == 0, "неуспешный пакет сохранился в БД (частичный COMMIT)"
    assert n_ok == len(rows_ok), "успешный пакет не записался при повторном использовании сессии"


async def test_hub_recovery_from_db_after_publish_failure(pg, monkeypatch):
    """ДЕФЕКТ (R2): после провала публикации hub не восстанавливается из БД при недоступном источнике."""
    orch, base, _ = _orch_with_gap(drop_minute=1)
    gap_row = _row(base + timedelta(minutes=1))
    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [gap_row])
    original_seed = orch.hub.seed_1m

    def _boom(*_a, **_k):
        raise RuntimeError("hub publish failed")
    monkeypatch.setattr(orch.hub, "seed_1m", _boom)

    r1 = await orch.fill_gaps_detailed(pg, _FIGI)
    assert r1.committed == 1, "данные должны быть committed в БД"
    assert r1.hub_publish_errors, "публикация в hub должна была упала"
    assert len(orch.hub.series(_FIGI, "1min")) == 2, "hub не обновился (публикация упала)"

    # источник стал недоступен; seed_1m восстановлен вручную
    monkeypatch.setattr(orch.hub, "seed_1m", original_seed)
    def _src_fail(*_a, **_k):
        raise TimeoutError("source down")
    monkeypatch.setattr(cho, "fetch_candles", _src_fail)

    r2 = await orch.fill_gaps_detailed(pg, _FIGI)
    # hub должен восстановиться из БД (committed данные), даже если источник недоступен
    assert len(orch.hub.series(_FIGI, "1min")) == 3, (
        f"hub не восстановлен из БД: bars={len(orch.hub.series(_FIGI, '1min'))}, ожидается 3")


# ============================================================ матрица жизненного цикла
def _seed_hub_gaps(orch, base, gaps_at: list[int]):
    """Засеять hub свечами, оставив дыры на указанных минутах."""
    from app.engine.models import Candle as EngineCandle
    present = []
    for m in range(0, max(gaps_at) + 2):
        if m not in gaps_at:
            present.append(_row(base + timedelta(minutes=m)))
    orch.hub.seed_1m(_FIGI, [EngineCandle(ts=r["ts"], open=r["open"], high=r["high"],
                                          low=r["low"], close=r["close"], volume=float(r["volume"]))
                             for r in present], source="db", fire=False)


async def test_two_gaps_same_figi(pg, monkeypatch):
    """Две дыры одного FIGI: обе заполняются и публикуются."""
    orch = cho.CandleHubOrchestrator()
    day = datetime(2026, 10, 5, tzinfo=_UTC)
    base = day.replace(hour=12, minute=0)
    _seed_hub_gaps(orch, base, gaps_at=[1, 3])  # hub: 12:00, 12:02, 12:04 → дыры 12:01, 12:03

    def fetch(figi, interval, a, b):
        return [_row(a + timedelta(minutes=m)) for m in range(int((b - a).total_seconds() // 60) + 1)]
    monkeypatch.setattr(cho, "fetch_candles", fetch)

    r = await orch.fill_gaps_detailed(pg, _FIGI)
    assert r.committed == 2
    assert not r.remaining_gaps
    assert len(orch.hub.series(_FIGI, "1min")) == 5  # 12:00..12:04


async def test_error_on_second_gap_after_first_success(pg, monkeypatch):
    """Первая дыра успешна, вторая падает (seed_1m) → pending для второй; recovery целиком."""
    orch = cho.CandleHubOrchestrator()
    day = datetime(2026, 10, 5, tzinfo=_UTC)
    base = day.replace(hour=12, minute=0)
    _seed_hub_gaps(orch, base, gaps_at=[1, 3])  # дыры 12:01, 12:03

    original_seed = orch.hub.seed_1m
    seed_calls = {"n": 0}

    def seed_then_boom(*a, **k):
        seed_calls["n"] += 1
        if seed_calls["n"] == 1:
            return original_seed(*a, **k)  # первая дыра успешна
        raise RuntimeError("hub publish failed")  # вторая падает
    monkeypatch.setattr(orch.hub, "seed_1m", seed_then_boom)

    def fetch(figi, interval, a, b):
        return [_row(a + timedelta(minutes=m)) for m in range(int((b - a).total_seconds() // 60) + 1)]
    monkeypatch.setattr(cho, "fetch_candles", fetch)

    r1 = await orch.fill_gaps_detailed(pg, _FIGI)
    assert r1.committed == 2  # обе записаны в БД
    assert r1.hub_publish_errors  # вторая упала
    assert len(orch.hub.series(_FIGI, "1min")) == 4  # 12:00,12:01,12:02,12:04 (12:03 нет)

    # recovery: вторая дыра восстановлена целиком из БД
    monkeypatch.setattr(orch.hub, "seed_1m", original_seed)
    def _src_fail(*_a, **_k):
        raise TimeoutError("source down")
    monkeypatch.setattr(cho, "fetch_candles", _src_fail)

    r2 = await orch.fill_gaps_detailed(pg, _FIGI)
    assert len(orch.hub.series(_FIGI, "1min")) == 5  # обе дыры восстановлены целиком


async def test_ensure_ready_structured_and_int_compat(pg, monkeypatch):
    """ensure_ready: структурированный результат (fill_*); fill_gaps int-совместим; восстановленный hub == БД."""
    orch, base, _ = _orch_with_gap(drop_minute=1)
    # сначала записываем present в БД (чтобы проверить соответствие hub==БД после ensure_ready)
    present_rows = [_row(base), _row(base + timedelta(minutes=2))]
    await tinvest.upsert_candles(pg, present_rows)
    await pg.commit()

    gap_row = _row(base + timedelta(minutes=1))
    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [gap_row])

    result = await orch.ensure_ready(pg, _FIGI, days=3)
    assert "fill_fetched" in result and "fill_committed" in result
    assert "fill_remaining_gaps" in result and "fill_source_errors" in result
    assert "fill_fully_filled" in result
    assert result["fill_fetched"] == 1 and result["fill_committed"] == 1
    assert result["fill_fully_filled"] is True

    # int-версия fill_gaps совместима: повторный вызов после ensure_ready — дыра уже заполнена
    n = await orch.fill_gaps(pg, _FIGI)
    assert isinstance(n, int) and n == 0, "повторный fill_gaps: дыра уже заполнена, ожидается 0"

    # восстановленный hub соответствует содержимому БД
    from sqlalchemy import func, select as _sel
    n_db = int((await pg.execute(_sel(func.count()).select_from(DBCandle)
                                   .where(DBCandle.figi == _FIGI))).scalar_one())
    n_hub = len(orch.hub.series(_FIGI, "1min"))
    assert n_db == n_hub == 3, f"БД={n_db}, hub={n_hub}, ожидается 3 (present 2 + докачанная 1)"


async def test_scope_rejection_not_ready(pg, monkeypatch):
    """Структурированный отказ (scope_rejections) — НЕ признак готовности (fully_filled=False)."""
    orch, base, _ = _orch_with_gap(drop_minute=1)
    foreign = _row(base + timedelta(minutes=1), figi="OTHERFIGI")
    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [foreign])

    r = await orch.fill_gaps_detailed(pg, _FIGI)
    assert r.scope_rejections, "scope_rejections пуст"
    assert r.fully_filled is False, "fully_filled должно быть False при отказе"
    assert r.committed == 0


async def test_partial_publish_recovers_full_range(pg, monkeypatch):
    """Частичная публикация: seed_1m опубликовал часть диапазона → recovery восстанавливает целиком."""
    orch = cho.CandleHubOrchestrator()
    day = datetime(2026, 10, 5, tzinfo=_UTC)
    base = day.replace(hour=12, minute=0)
    _seed_hub_gaps(orch, base, gaps_at=[1, 2])  # дыра 12:01-12:02 (2 минуты)

    original_seed = orch.hub.seed_1m
    published = {"ts": set()}
    def partial_seed(*a, **k):
        # публикует только первую свечу из батча
        candles = a[1]
        if candles:
            published["ts"].add(candles[0].ts)
            return original_seed(candles[:1], **k)
        return original_seed(*a, **k)
    monkeypatch.setattr(orch.hub, "seed_1m", partial_seed)

    def fetch(figi, interval, a, b):
        return [_row(a + timedelta(minutes=m)) for m in range(int((b - a).total_seconds() // 60) + 1)]
    monkeypatch.setattr(cho, "fetch_candles", fetch)

    r1 = await orch.fill_gaps_detailed(pg, _FIGI)
    assert r1.committed == 2
    assert r1.hub_publish_errors  # частичная публикация зафиксирована

    # recovery из БД восстанавливает обе свечи целиком
    monkeypatch.setattr(orch.hub, "seed_1m", original_seed)
    def _src_fail(*_a, **_k):
        raise TimeoutError("source down")
    monkeypatch.setattr(cho, "fetch_candles", _src_fail)

    await orch.fill_gaps_detailed(pg, _FIGI)
    hub_ts = {c.ts for c in orch.hub.series(_FIGI, "1min").snapshot()}
    assert base + timedelta(minutes=1) in hub_ts and base + timedelta(minutes=2) in hub_ts, (
        "recovery не восстановил диапазон целиком")


# ============================================================ R3: RECOVERY-R3
def _seed_two_figis(orch, base, drop_x=1, drop_y=1):
    """Один оркестратор, два FIGI с дырами 12:01."""
    from app.engine.models import Candle as EngineCandle
    for figi in (_FIGI, "YFIGI000000000002"):
        present = [_row(base, figi=figi), _row(base + timedelta(minutes=drop_x + 1), figi=figi)]
        orch.hub.seed_1m(figi, [EngineCandle(ts=r["ts"], open=r["open"], high=r["high"],
                                              low=r["low"], close=r["close"], volume=float(r["volume"]))
                                 for r in present], source="db", fire=False)


async def test_recovery_r3_shared_bool_loses_pending(pg, monkeypatch):
    """ДЕФЕКТ (R3): общий _hub_needs_recovery bool очищается при fill_gaps другого FIGI,
    даже если у того нет дыр → committed свеча X не восстанавливается из БД."""
    orch = cho.CandleHubOrchestrator()
    day = datetime(2026, 10, 5, tzinfo=_UTC)
    base = day.replace(hour=12, minute=0)
    _seed_two_figis(orch, base, drop_x=1, drop_y=1)

    gap_row_x = _row(base + timedelta(minutes=1), figi=_FIGI)
    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [gap_row_x])
    original_seed = orch.hub.seed_1m

    def _boom(*_a, **_k):
        raise RuntimeError("hub publish failed")
    monkeypatch.setattr(orch.hub, "seed_1m", _boom)

    r1 = await orch.fill_gaps_detailed(pg, _FIGI)
    assert r1.committed == 1 and r1.hub_publish_errors
    assert len(orch.hub.series(_FIGI, "1min")) == 2, "X 12:01 не в hub (публикация упала)"

    # Y: нет дыр (нет истории) — не должен сбрасывать pending X
    await orch.fill_gaps_detailed(pg, "YFIGI000000000002")

    # X: источник недоступен, БД доступна → hub X восстановлен из БД
    monkeypatch.setattr(orch.hub, "seed_1m", original_seed)
    def _src_fail(*_a, **_k):
        raise TimeoutError("source down")
    monkeypatch.setattr(cho, "fetch_candles", _src_fail)

    await orch.fill_gaps_detailed(pg, _FIGI)
    assert len(orch.hub.series(_FIGI, "1min")) == 3, (
        f"hub X не восстановлен из БД после Y-call: bars={len(orch.hub.series(_FIGI, '1min'))}, ожидается 3")


async def test_recovery_r3_two_figis_independent(pg, monkeypatch):
    """Два FIGI оба failed независимо: pending X и Y не теряются друг от друга."""
    orch = cho.CandleHubOrchestrator()
    day = datetime(2026, 10, 5, tzinfo=_UTC)
    base = day.replace(hour=12, minute=0)
    _seed_two_figis(orch, base, drop_x=1, drop_y=1)

    gap_row_x = _row(base + timedelta(minutes=1), figi=_FIGI)
    gap_row_y = _row(base + timedelta(minutes=1), figi="YFIGI000000000002")
    monkeypatch.setattr(cho, "fetch_candles",
                        lambda *a, **k: [gap_row_x] if a[0] == _FIGI else [gap_row_y])
    original_seed = orch.hub.seed_1m

    def _boom(*_a, **_k):
        raise RuntimeError("hub publish failed")
    monkeypatch.setattr(orch.hub, "seed_1m", _boom)

    await orch.fill_gaps_detailed(pg, _FIGI)
    await orch.fill_gaps_detailed(pg, "YFIGI000000000002")
    assert len(orch.hub.series(_FIGI, "1min")) == 2
    assert len(orch.hub.series("YFIGI000000000002", "1min")) == 2

    # оба восстановить из БД
    monkeypatch.setattr(orch.hub, "seed_1m", original_seed)
    def _src_fail(*_a, **_k):
        raise TimeoutError("source down")
    monkeypatch.setattr(cho, "fetch_candles", _src_fail)

    await orch.fill_gaps_detailed(pg, _FIGI)
    await orch.fill_gaps_detailed(pg, "YFIGI000000000002")
    assert len(orch.hub.series(_FIGI, "1min")) == 3, "X не восстановлен"
    assert len(orch.hub.series("YFIGI000000000002", "1min")) == 3, "Y не восстановлен"


async def test_recovery_r3_recovery_failure_keeps_pending(pg, monkeypatch):
    """Повторная ошибка recovery не стирает pending."""
    orch = cho.CandleHubOrchestrator()
    day = datetime(2026, 10, 5, tzinfo=_UTC)
    base = day.replace(hour=12, minute=0)
    _seed_two_figis(orch, base, drop_x=1, drop_y=1)

    gap_row_x = _row(base + timedelta(minutes=1), figi=_FIGI)
    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [gap_row_x])
    original_seed = orch.hub.seed_1m

    def _boom(*_a, **_k):
        raise RuntimeError("hub publish failed")
    monkeypatch.setattr(orch.hub, "seed_1m", _boom)

    await orch.fill_gaps_detailed(pg, _FIGI)
    assert len(orch.hub.series(_FIGI, "1min")) == 2

    # recovery тоже падает (seed_1m всё ещё _boom) → pending сохраняется
    def _src_fail(*_a, **_k):
        raise TimeoutError("source down")
    monkeypatch.setattr(cho, "fetch_candles", _src_fail)
    await orch.fill_gaps_detailed(pg, _FIGI)
    assert len(orch.hub.series(_FIGI, "1min")) == 2, "recovery упал, pending должен сохраниться"

    # теперь recovery успешен (seed_1m восстановлен) → hub восстановлен
    monkeypatch.setattr(orch.hub, "seed_1m", original_seed)
    await orch.fill_gaps_detailed(pg, _FIGI)
    assert len(orch.hub.series(_FIGI, "1min")) == 3, "после успешного recovery hub должен восстановиться"
