"""Актуальность производных ТФ: что бот использует и насколько ряд догоняет минуты.

Задача от ревью: держать производные ряды активных ТФ свежими из канонического
минутного ряда, пересчитывать затронутые бакеты инкрементально и **различать**
три разных состояния, которые раньше путались:

- `fresh` — последний закрытый бакет по минутам есть в ряду ТФ;
- `stale` — минуты уже закрыли более поздний бакет, а в ТФ его нет (ряд отстал);
- `no_trades` — в минутах за этот бакет нет ни одной строки (торгов не было,
  отставания нет);
- `missing` — ряда ТФ по бумаге вообще нет.

Список активных ТФ берётся из `configs/tf_active.json`, а не зашит в код.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

ACTIVE_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "tf_active.json"

# Имя интервала → (код в таблице candles, длина бакета в секундах)
TF_SPEC: dict[str, tuple[int, int]] = {
    "1min": (1, 60),
    "5min": (2, 300),
    "10min": (8, 600),
    "15min": (3, 900),
    "hour": (4, 3600),
    "2h": (10, 7200),
    "4h": (11, 14400),
    "day": (24, 86400),
}


@dataclass
class TfState:
    figi: str
    tf: str
    status: str            # fresh | stale | no_trades | missing
    expected_ts: datetime | None
    actual_ts: datetime | None
    behind_bars: int = 0

    def as_dict(self) -> dict:
        return {
            "figi": self.figi, "tf": self.tf, "status": self.status,
            "expected_ts": self.expected_ts.isoformat() if self.expected_ts else None,
            "actual_ts": self.actual_ts.isoformat() if self.actual_ts else None,
            "behind_bars": self.behind_bars,
        }


@lru_cache(maxsize=1)
def active_tfs() -> tuple[str, ...]:
    if not ACTIVE_CONFIG.exists():
        return ("5min", "hour", "day")
    blob = json.loads(ACTIVE_CONFIG.read_text(encoding="utf-8"))
    names = [str(x) for x in blob.get("active", [])]
    return tuple(n for n in names if n in TF_SPEC)


def cache_clear() -> None:
    active_tfs.cache_clear()


def bucket_start(ts: datetime, step_seconds: int) -> datetime:
    u = int(ts.timestamp())
    return datetime.fromtimestamp(u - (u % step_seconds), tz=timezone.utc)


def last_closed_bucket(now: datetime, step_seconds: int) -> datetime:
    """Начало последнего бакета, который уже целиком прошёл по времени."""
    start = bucket_start(now, step_seconds)
    if start + timedelta(seconds=step_seconds) > now:
        start -= timedelta(seconds=step_seconds)
    return start


def classify(
    *,
    figi: str,
    tf: str,
    expected_ts: datetime | None,
    actual_ts: datetime | None,
    minutes_in_bucket: int,
    has_any_tf: bool,
    step_seconds: int,
) -> TfState:
    """Чистое правило состояния: без обращения к БД, чтобы его можно было проверять тестом."""
    if minutes_in_bucket == 0:
        return TfState(figi, tf, "no_trades", expected_ts, actual_ts)
    if not has_any_tf or actual_ts is None:
        return TfState(figi, tf, "missing", expected_ts, actual_ts)
    if expected_ts is None:
        return TfState(figi, tf, "fresh", expected_ts, actual_ts)
    if actual_ts >= expected_ts:
        return TfState(figi, tf, "fresh", expected_ts, actual_ts)
    behind = max(0, int((expected_ts - actual_ts).total_seconds() // step_seconds))
    return TfState(figi, tf, "stale", expected_ts, actual_ts, behind)


async def state_for(db, figi: str, tf: str, *, now: datetime | None = None) -> TfState:
    """Состояние одного ряда ТФ по бумаге."""
    from sqlalchemy import func, select

    from app.models.candle import Candle

    code, step = TF_SPEC[tf]
    now = now or datetime.now(timezone.utc)
    expected = last_closed_bucket(now, step)

    minute_max = (await db.execute(
        select(func.max(Candle.ts)).where(
            Candle.figi == figi, Candle.interval == 1, Candle.ts < expected + timedelta(seconds=step))
    )).scalar()
    minutes_in_bucket = int((await db.execute(
        select(func.count()).select_from(Candle).where(
            Candle.figi == figi, Candle.interval == 1,
            Candle.ts >= expected, Candle.ts < expected + timedelta(seconds=step))
    )).scalar() or 0)

    tf_max = (await db.execute(
        select(func.max(Candle.ts)).where(Candle.figi == figi, Candle.interval == code)
    )).scalar()

    # Если минуты не дошли до ожидаемого бакета, ожидать его в ТФ нечего:
    # сверяемся с последним бакетом, который минуты действительно закрыли.
    exp = expected
    if minute_max is not None and minute_max < expected:
        exp = bucket_start(minute_max, step)
        if exp + timedelta(seconds=step) > now:
            exp -= timedelta(seconds=step)
        minutes_in_bucket = int((await db.execute(
            select(func.count()).select_from(Candle).where(
                Candle.figi == figi, Candle.interval == 1,
                Candle.ts >= exp, Candle.ts < exp + timedelta(seconds=step))
        )).scalar() or 0)

    return classify(figi=figi, tf=tf, expected_ts=exp, actual_ts=tf_max,
                    minutes_in_bucket=minutes_in_bucket,
                    has_any_tf=tf_max is not None, step_seconds=step)


async def states(db, figis: list[str] | None = None) -> list[TfState]:
    """Состояния по всем бумагам и активным ТФ. figis=None — только те, где есть минуты."""
    from sqlalchemy import distinct, select

    from app.models.candle import Candle

    if figis is None:
        figis = [r[0] for r in (await db.execute(
            select(distinct(Candle.figi)).where(Candle.interval == 1))).all()]
    out: list[TfState] = []
    for figi in figis:
        for tf in active_tfs():
            out.append(await state_for(db, figi, tf))
    return out


def summarize(items: list[TfState]) -> dict:
    counts: dict[str, int] = {}
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
    worst = sorted((i for i in items if i.status == "stale"),
                   key=lambda i: -i.behind_bars)[:10]
    return {
        "counts": counts,
        "worst_stale": [w.as_dict() for w in worst],
    }
