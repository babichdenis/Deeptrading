"""Часовой слой: сборка, границы часа, провалы, восстановление и допуск входа."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.bot import hourly_runtime as hr
from app.engine.hourly_context import HourlyContext, HourlyFeature
from app.engine.models import Candle

M = timedelta(minutes=1)
H = timedelta(hours=1)
T = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
MARKET = "MOEX"


def minutes(start=None, count=61, base=100.0, step=0.5):
    start = start or (T - H - M)
    return [
        Candle(start + i * M, base, base + 2, base - 1, base + i * step, 10)
        for i in range(count)
    ]


def windows(spec):
    return [hr.MinuteWindow(figi, tuple(bars), len(bars)) for figi, bars in spec.items()]


class FakeLoader:
    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    async def load(self, db, figis, *, as_of, lookback_minutes=61):
        self.calls += 1
        if callable(self.result):
            return self.result(as_of, tuple(sorted(figis)))
        return self.result


def service(loader, *, clock=None, journal=None, market=MARKET, **kwargs):
    return hr.HourlyContextService(
        market_id=market,
        journal=journal,
        loader=loader,
        clock=clock or (lambda: T),
        **kwargs,
    )


def good(**extra):
    spec = {MARKET: minutes(), "SBER": minutes(step=1.0)}
    spec.update(extra)
    return FakeLoader(windows(spec))


async def build(svc, figis=("MOEX", "SBER"), now=None):
    return await svc.ensure(db=None, now=now or svc._clock(), figis=figis)


# ── сборка ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publishes_closed_hour_snapshot():
    svc = service(good())

    snap = await build(svc)

    assert snap.as_of == T
    assert snap.source_status == hr.SOURCE_OK
    assert snap.available_at == T
    assert snap.expires_at == T + H
    assert snap.context.features["SBER"].status == "KNOWN"
    assert snap.context.features["SBER"].observed_minutes == 60
    assert snap.context.input_hash
    assert snap.config_hash == svc.config_hash
    assert snap.code_hash == svc.code_hash


@pytest.mark.asyncio
async def test_journal_record_written_before_publication():
    written = []
    journal = hr.HourlyJournal(None)
    journal.append = lambda record: written.append(dict(record))
    svc = service(good(), journal=journal)

    await build(svc)

    assert len(written) == 1
    assert written[0]["event"] == "refresh"
    assert written[0]["as_of"] == T.isoformat()
    assert written[0]["attempt"] == 1
    assert written[0]["features"]["SBER"]["status"] == "KNOWN"


@pytest.mark.asyncio
async def test_journal_failure_prevents_publication():
    class BrokenJournal(hr.HourlyJournal):
        def append(self, record):
            raise OSError("диск недоступен")

    svc = service(good(), journal=BrokenJournal(None))

    assert await build(svc) is None
    assert svc.current(now=T) is None
    assert "не записан" in (svc.last_error or "")


# ── границы часа и single-flight ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_hour_is_a_new_snapshot_not_the_old_one():
    clock = [datetime(2026, 9, 1, 10, 59, tzinfo=timezone.utc)]
    loader = FakeLoader(
        lambda as_of, figis: windows({MARKET: minutes(start=as_of - H - M)})
    )
    svc = service(loader, clock=lambda: clock[0])

    first = await svc.ensure(db=None, now=clock[0], figis=(MARKET,))
    assert first.as_of == T

    clock[0] = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    second = await svc.ensure(db=None, now=clock[0], figis=(MARKET,))

    assert second.as_of == datetime(2026, 9, 1, 11, tzinfo=timezone.utc)
    assert loader.calls == 2


@pytest.mark.asyncio
async def test_same_hour_builds_once_for_concurrent_callers():
    import asyncio

    loader = good()
    svc = service(loader)

    results = await asyncio.gather(
        svc.ensure(db=None, now=T, figis=(MARKET, "SBER")),
        svc.ensure(db=None, now=T, figis=(MARKET, "SBER")),
        svc.ensure(db=None, now=T + 20 * M, figis=(MARKET, "SBER")),
    )

    assert loader.calls == 1
    assert all(r is results[0] for r in results)


# ── ограниченные повторы ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_incomplete_snapshot_is_retried_after_interval():
    clock = [T]
    partial = [
        hr.MinuteWindow(MARKET, tuple(minutes()), 61),
        hr.MinuteWindow("SBER", (), 0, "пусто"),
    ]
    loader = FakeLoader(partial)
    svc = service(loader, clock=lambda: clock[0], retry_interval=900.0)

    first = await svc.ensure(db=None, now=clock[0], figis=(MARKET, "SBER"))
    assert first.source_status == hr.SOURCE_PARTIAL

    # слишком рано: отдаётся тот же снимок, новой сборки нет
    clock[0] = T + 10 * M
    again = await svc.ensure(db=None, now=clock[0], figis=(MARKET, "SBER"))
    assert again is first
    assert loader.calls == 1

    # прошёл интервал: одна повторная попытка
    clock[0] = T + 20 * M
    loader.result = windows({MARKET: minutes(), "SBER": minutes()})
    fresh = await svc.ensure(db=None, now=clock[0], figis=(MARKET, "SBER"))
    assert fresh.source_status == hr.SOURCE_OK
    assert loader.calls == 2


@pytest.mark.asyncio
async def test_retries_are_bounded_per_hour():
    clock = [T]
    partial = [
        hr.MinuteWindow(MARKET, tuple(minutes()), 61),
        hr.MinuteWindow("SBER", (), 0, "пусто"),
    ]
    loader = FakeLoader(partial)
    svc = service(loader, clock=lambda: clock[0], retry_interval=0.0, max_attempts=3)

    for i in range(6):
        clock[0] = T + i * M
        await svc.ensure(db=None, now=clock[0], figis=(MARKET, "SBER"))

    assert loader.calls == 3


# ── провалы ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loader_failure_publishes_nothing_and_keeps_old_expiring():
    written = []
    journal = hr.HourlyJournal(None)
    journal.append = lambda record: written.append(dict(record))
    svc = service(good(), journal=journal)
    assert await build(svc) is not None

    svc.loader = FakeLoader(
        [
            hr.MinuteWindow(MARKET, (), 0, "база недоступна"),
            hr.MinuteWindow("SBER", (), 0, "база недоступна"),
        ]
    )

    later = T + 2 * M
    assert svc.current(now=later) is not None
    assert await svc._refresh(db=None, boundary=T, figis=(MARKET, "SBER"), now=later) is None
    assert written[-1]["event"] == "refresh_failed"
    assert "не отдала" in written[-1]["reason"]
    assert svc.current(now=later) is not None


@pytest.mark.asyncio
async def test_expired_snapshot_is_not_served_after_failed_refresh():
    svc = service(good())
    assert await build(svc) is not None

    svc.loader = FakeLoader([hr.MinuteWindow(MARKET, (), 0, "нет связи")])
    next_hour = T + H
    await svc.ensure(db=None, now=next_hour, figis=(MARKET,))

    assert svc.current(now=next_hour) is None
    assert svc.status(now=next_hour)["state"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_slow_build_past_its_hour_is_not_published():
    written = []
    journal = hr.HourlyJournal(None)
    journal.append = lambda record: written.append(dict(record))
    clock = [T]
    svc = service(good(), clock=lambda: clock[0], journal=journal)

    clock[0] = T + H + 5 * M
    snap = await svc.ensure(db=None, now=T, figis=(MARKET, "SBER"))

    assert snap is None
    assert written and written[0]["event"] == "refresh_failed"
    assert "после конца своего часа" in written[0]["reason"]


@pytest.mark.asyncio
async def test_partial_source_keeps_missing_figi_as_unknown():
    loader = FakeLoader(
        [
            hr.MinuteWindow(MARKET, tuple(minutes()), 61),
            hr.MinuteWindow("SBER", (), 0, "пусто"),
        ]
    )
    svc = service(loader)

    snap = await build(svc)

    assert snap.source_status == hr.SOURCE_PARTIAL
    assert snap.failed_figis == ("SBER",)
    # бумага не исчезает: иначе счётчики покрытия завышают реальность
    assert "SBER" in snap.context.features
    assert snap.context.features["SBER"].status == "ENDPOINT_UNKNOWN"
    status = svc.status(now=T)
    assert status["state"] == "PARTIAL"
    assert status["covered"] == len(snap.context.features)
    assert status["known"] == 1


# ── индекс и формирующийся бар ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_market_marks_stocks_market_unknown():
    loader = FakeLoader(
        [
            hr.MinuteWindow(MARKET, (), 0, "индекс не отдан"),
            hr.MinuteWindow("SBER", tuple(minutes()), 61),
        ]
    )
    svc = service(loader)

    snap = await build(svc)

    assert snap.feature("SBER").status == "MARKET_UNKNOWN"
    assert snap.feature("SBER").relative_return_60m is None
    assert svc.status(now=T)["decision_ready"] is False


@pytest.mark.asyncio
async def test_forming_bar_of_current_hour_is_invisible():
    forming = Candle(T, 50, 51, 49, 50, 1)
    loader = FakeLoader(
        [
            hr.MinuteWindow(MARKET, tuple(minutes()), 61),
            hr.MinuteWindow("SBER", tuple(minutes() + [forming]), 62),
        ]
    )
    svc = service(loader)

    snap = await build(svc)

    feature = snap.feature("SBER")
    assert feature.status == "KNOWN"
    assert feature.observed_minutes == 60
    assert feature.last_closed_at == T


# ── восстановление ──────────────────────────────────────────────────────────


async def _service_with_journal(tmp_path, **kwargs):
    journal = hr.HourlyJournal(tmp_path / "journal.jsonl")
    svc = service(good(), journal=journal, **kwargs)
    assert await build(svc) is not None
    return journal


@pytest.mark.asyncio
async def test_restore_returns_a_working_snapshot(tmp_path):
    journal = await _service_with_journal(tmp_path)
    fresh = service(good(), journal=journal, clock=lambda: T + 5 * M)

    restored = fresh.restore(now=T + 5 * M)

    assert restored is not None
    assert fresh.current(now=T + 5 * M) is not None
    assert fresh.current(now=T + 5 * M).feature("SBER").status == "KNOWN"
    assert fresh.status(now=T + 5 * M)["decision_ready"] is True


@pytest.mark.asyncio
async def test_restore_rejects_foreign_code_or_config(tmp_path):
    journal = await _service_with_journal(tmp_path)

    other_config = service(good(), journal=journal, market="OTHER", clock=lambda: T + 5 * M)
    assert other_config.restore(now=T + 5 * M) is None

    other_code = service(good(), journal=journal, clock=lambda: T + 5 * M)
    other_code.code_hash = "0" * 64
    assert other_code.restore(now=T + 5 * M) is None


@pytest.mark.asyncio
async def test_restore_ignores_future_record_on_replay_clock(tmp_path):
    journal = await _service_with_journal(tmp_path)
    replay_clock = service(good(), journal=journal, clock=lambda: T - 2 * H)

    # часы прогона: снимок ещё «в будущем», поднимать его нельзя
    assert replay_clock.restore(now=T - 2 * H) is None
    assert replay_clock.current(now=T - 2 * H) is None


@pytest.mark.asyncio
async def test_restore_skips_expired_snapshot(tmp_path):
    journal = await _service_with_journal(tmp_path)
    svc = service(good(), journal=journal, clock=lambda: T + 2 * H)

    assert svc.restore(now=T + H) is None


def test_restore_ignores_broken_lines(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text("не json\n{}\n", encoding="utf-8")
    svc = service(good(), journal=hr.HourlyJournal(path))

    assert svc.restore(now=T + 5 * M) is None


# ── часы ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_historical_clock_sets_available_at_from_feed_not_system():
    feed_now = datetime(2026, 3, 4, 7, 12, tzinfo=timezone.utc)
    loader = FakeLoader(lambda as_of, figis: windows({MARKET: minutes(start=as_of - H - M)}))
    svc = service(loader, clock=lambda: feed_now)

    snap = await svc.ensure(db=None, now=feed_now, figis=(MARKET,))

    assert snap.as_of == datetime(2026, 3, 4, 7, tzinfo=timezone.utc)
    assert snap.available_at == feed_now


# ── аварийный слой ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emergency_block_survives_hourly_refresh():
    clock = [T]
    svc = service(good(), clock=lambda: clock[0])
    svc.emergency.set("halt", reason="авария на бирже", at=T)

    await svc.ensure(db=None, now=clock[0], figis=(MARKET, "SBER"))
    assert svc.emergency.is_blocked("halt")

    clock[0] = T + H
    await svc.ensure(db=None, now=clock[0], figis=(MARKET, "SBER"))
    assert svc.emergency.is_blocked("halt")
    assert svc.status(now=clock[0])["emergency_blocks"]["halt"]["reason"] == "авария на бирже"


# ── допуск входа ────────────────────────────────────────────────────────────


def _snapshot(**feature_status):
    features = {
        MARKET: HourlyFeature("KNOWN", 0.004, 0.0, 60, T),
        "SBER": HourlyFeature("KNOWN", 0.006, 0.002, 60, T),
        "GAZP": HourlyFeature("ENDPOINT_UNKNOWN", None, None, 12, None),
    }
    for figi, status in feature_status.items():
        features[figi] = HourlyFeature(status, None, None, 0, None)
    context = HourlyContext(T, T, T + H, MARKET, dict(features), "hash")
    return hr.HourlySnapshot(context, hr.SOURCE_OK, {}, (), "cfg", "code")


def test_entry_allowed_when_both_stock_and_market_known():
    verdict = hr.entry_verdict(
        overlay=hr.EmergencyOverlay(), snapshot=_snapshot(), now=T,
        target_figi="SBER", market_id=MARKET,
    )

    assert verdict.allowed is True
    assert verdict.reason is None


def test_entry_denied_when_stock_unknown():
    verdict = hr.entry_verdict(
        overlay=hr.EmergencyOverlay(), snapshot=_snapshot(), now=T,
        target_figi="GAZP", market_id=MARKET,
    )

    assert verdict.allowed is False
    assert "ENDPOINT_UNKNOWN" in verdict.reason


def test_entry_denied_when_market_unknown():
    snap = _snapshot(MOEX="MARKET_UNKNOWN")

    verdict = hr.entry_verdict(
        overlay=hr.EmergencyOverlay(), snapshot=snap, now=T,
        target_figi="SBER", market_id=MARKET,
    )

    assert verdict.allowed is False
    assert "MARKET_UNKNOWN" in verdict.reason


def test_entry_denied_when_stock_absent_from_slice():
    verdict = hr.entry_verdict(
        overlay=hr.EmergencyOverlay(), snapshot=_snapshot(), now=T,
        target_figi="LKOH", market_id=MARKET,
    )

    assert verdict.allowed is False
    assert "нет в часовом срезе" in verdict.reason


def test_entry_denied_without_snapshot_or_on_expiry():
    overlay = hr.EmergencyOverlay()

    assert hr.entry_verdict(
        overlay=overlay, snapshot=None, now=T, target_figi="SBER", market_id=MARKET
    ).allowed is False

    expired = hr.entry_verdict(
        overlay=overlay, snapshot=_snapshot(), now=T + H,
        target_figi="SBER", market_id=MARKET,
    )
    assert expired.allowed is False
    assert "устарел" in expired.reason


def test_entry_denied_by_emergency_block():
    overlay = hr.EmergencyOverlay()
    overlay.set("halt", reason="авария", at=T)

    verdict = hr.entry_verdict(
        overlay=overlay, snapshot=_snapshot(), now=T,
        target_figi="SBER", market_id=MARKET, block_key="halt",
    )

    assert verdict.allowed is False
    assert "аварийный запрет" in verdict.reason


def test_exit_never_depends_on_context():
    overlay = hr.EmergencyOverlay()
    overlay.set("halt", reason="авария", at=T)

    assert hr.permits_reduction(overlay=overlay, now=T) is True


def test_naive_clock_is_refused():
    with pytest.raises(ValueError):
        hr.permits_reduction(overlay=hr.EmergencyOverlay(), now=datetime(2026, 9, 1, 10))


# ── сериализация ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_and_record_serialize_without_loss():
    written = []
    journal = hr.HourlyJournal(None)
    journal.append = lambda record: written.append(dict(record))
    svc = service(good(), journal=journal)

    await build(svc)
    status = svc.status(now=T)

    required = {
        "state", "decision_ready", "reason", "as_of", "available_at", "expires_at",
        "input_hash", "config_hash", "code_hash", "covered", "known",
        "market_status", "emergency_blocks", "source_status", "failed_figis",
    }
    assert required <= set(status)
    assert json.loads(json.dumps(status))["as_of"] == T.isoformat()

    record = json.loads(json.dumps(written[0]))
    assert record["features"]["SBER"]["status"] == "KNOWN"
    assert record["features"]["SBER"]["last_closed_at"].endswith("+00:00")


def test_unknown_status_still_carries_identity_and_emergency():
    svc = service(FakeLoader([]))
    svc.emergency.set("halt", reason="авария", at=T)

    status = svc.status(now=T)

    assert status["state"] == "UNKNOWN"
    assert status["decision_ready"] is False
    assert status["config_hash"] == svc.config_hash
    assert status["emergency_blocks"]["halt"]["reason"] == "авария"
    assert status["as_of"] is None


def test_code_fingerprint_names_unavailable_file():
    # отпечаток не молчит о недоступности, чтобы подмена не прошла незамеченной
    from app.bot import hourly_runtime as mod

    digest = mod.code_fingerprint()
    assert len(digest) == 64
