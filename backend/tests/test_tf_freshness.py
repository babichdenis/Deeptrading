"""Актуальность производных ТФ: правила ожидаемого бакета и различение состояний.

Ревью требовало именно этого: проверять, что ряд догнал последний **закрытый**
бакет по исходным минутам, а не просто что строки пересекаются; и не путать
отставание с отсутствием торгов.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import tf_freshness as fr


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_active_tfs_from_config():
    tfs = fr.active_tfs()
    assert tfs, "активные ТФ не заданы"
    assert all(t in fr.TF_SPEC for t in tfs), f"в конфиге неизвестный ТФ: {tfs}"
    assert "1min" not in tfs, "минутный ряд — источник, а не производный"
    assert "5min" in tfs


def test_last_closed_bucket_is_previous_bucket():
    step = 300
    # 12:03 — текущий 5-минутный бакет (12:00) ещё не закрыт, закрыт 11:55
    assert fr.last_closed_bucket(_t("2026-11-15T12:03:00"), step) == _t("2026-11-15T11:55:00")
    # ровно граница 12:00 — бакет 12:00 только начался, закрыт 11:55
    assert fr.last_closed_bucket(_t("2026-11-15T12:00:00"), step) == _t("2026-11-15T11:55:00")
    # 12:07 — 12:00 закрылся (12:05), последний закрытый 12:00
    assert fr.last_closed_bucket(_t("2026-11-15T12:07:00"), step) == _t("2026-11-15T12:00:00")


def test_bucket_start_floors():
    assert fr.bucket_start(_t("2026-11-15T12:07:31"), 300) == _t("2026-11-15T12:05:00")
    assert fr.bucket_start(_t("2026-11-15T12:07:31"), 3600) == _t("2026-11-15T12:00:00")
    assert fr.bucket_start(_t("2026-11-15T23:59:00"), 86400) == _t("2026-11-15T00:00:00")


def test_fresh_when_tf_reaches_expected():
    st = fr.classify(figi="X", tf="5min", expected_ts=_t("2026-11-15T12:00:00"),
                     actual_ts=_t("2026-11-15T12:00:00"), minutes_in_bucket=5,
                     has_any_tf=True, step_seconds=300)
    assert st.status == "fresh"


def test_stale_reports_how_far_behind():
    st = fr.classify(figi="X", tf="5min", expected_ts=_t("2026-11-15T12:00:00"),
                     actual_ts=_t("2026-11-15T09:00:00"), minutes_in_bucket=5,
                     has_any_tf=True, step_seconds=300)
    assert st.status == "stale"
    assert st.behind_bars == 36


def test_no_trades_is_not_stale():
    """Пустой бакет по минутам — это отсутствие торгов, а не отставание."""
    st = fr.classify(figi="X", tf="5min", expected_ts=_t("2026-11-15T12:00:00"),
                     actual_ts=_t("2026-11-15T09:00:00"), minutes_in_bucket=0,
                     has_any_tf=True, step_seconds=300)
    assert st.status == "no_trades"


def test_missing_row_series():
    st = fr.classify(figi="X", tf="day", expected_ts=_t("2026-11-14T00:00:00"),
                     actual_ts=None, minutes_in_bucket=10, has_any_tf=False,
                     step_seconds=86400)
    assert st.status == "missing"


def test_summary_counts_and_worst():
    items = [
        fr.TfState("A", "5min", "fresh", None, None),
        fr.TfState("B", "5min", "stale", None, None, behind_bars=3),
        fr.TfState("C", "5min", "stale", None, None, behind_bars=12),
        fr.TfState("D", "5min", "no_trades", None, None),
    ]
    s = fr.summarize(items)
    assert s["counts"]["stale"] == 2
    assert s["worst_stale"][0]["figi"] == "C"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_active_tf_not_stale_on_liquid_name():
    """Живая проверка: по ликвидной бумаге активные ТФ догнаны до минут.

    Допускается `no_trades` (пустой бакет), но не `stale`: если минуты закрыли
    новый бакет, ряд ТФ обязан его содержать.
    """
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.candle import Candle

    async with SessionLocal() as db:
        figi = (await db.execute(
            select(Candle.figi).where(Candle.interval == 1)
            .group_by(Candle.figi).limit(1))).scalar()
        assert figi, "в базе нет минут"
        for tf in fr.active_tfs():
            st = await fr.state_for(db, figi, tf)
            assert st.status in {"fresh", "no_trades", "missing"}, st.as_dict()
