#!/usr/bin/env python3
"""Догон производных ТФ из канонического минутного ряда — инкрементально.

Не полная пересборка: для каждой бумаги и активного ТФ берётся последний бакет,
который минуты уже закрыли, и дописываются только недостающие бакеты. Повторный
запуск ничего не меняет (идемпотентно). Незакрытый бакет не трогается.

Состояния различаются: `stale` (ряд отстал) догоняется, `no_trades` (в минутах за
бакет пусто) пропускается, `missing` (ряда нет) — сообщается, а не выдумывается.

Запуск:
  python scripts/refresh_tf_from_1m.py --dry
  python scripts/refresh_tf_from_1m.py
  python scripts/refresh_tf_from_1m.py --figi BBG004730N88 --tf 5min
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import distinct, select, text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.candle import Candle  # noqa: E402
from app.services import tf_freshness as fr  # noqa: E402
from app.services import data_invalidity as di  # noqa: E402

# Рубеж, с которого минутный ряд признан достоверным (шкала совпадает с источником).
# До него история помечена в манифесте и не используется как основа для рядов ТФ.
CLEAN_FROM = datetime.fromisoformat(
    os.environ.get("CANDLE_CLEAN_FROM", "2025-11-24T00:00:00+03:00"))
from app.services.candle_cache import resample_from_1m  # noqa: E402


async def _minutes_aggregate(db, figi: str, tf: str, bucket: datetime) -> dict | None:
    """Агрегат минут за бакет: open/high/low/close. None — минут нет."""
    code, step = fr.TF_SPEC[tf]
    rows = (await db.execute(
        select(Candle.open, Candle.high, Candle.low, Candle.close)
        .where(Candle.figi == figi, Candle.interval == 1,
               Candle.ts >= bucket, Candle.ts < bucket + timedelta(seconds=step))
        .order_by(Candle.ts))).all()
    if not rows:
        return None
    return {"open": rows[0][0], "high": max(r[1] for r in rows),
            "low": min(r[2] for r in rows), "close": rows[-1][3]}


async def _agrees(db, figi: str, tf: str, bucket: datetime) -> bool | None:
    """Совпадает ли бар ТФ за бакет с агрегатом минут (в пределах 50 %)."""
    code, step = fr.TF_SPEC[tf]
    tf_close = (await db.execute(
        select(Candle.close).where(Candle.figi == figi, Candle.interval == code,
                                    Candle.ts == bucket))).scalar()
    if tf_close is None or not float(tf_close):
        return None
    agg = await _minutes_aggregate(db, figi, tf, bucket)
    if agg is None or not float(agg["close"]):
        return None
    ratio = float(agg["close"]) / float(tf_close)
    return 0.66 <= ratio <= 1.5


async def _scale_cut(db, figi: str, tf: str, *, now: datetime) -> datetime | None:
    """Граница, после которой ряд ТФ согласуется с минутами.

    Прежний загрузчик писал историю в другой шкале, поэтому старый хвост ряда нельзя
    использовать как опору для догона: защита от скачков отвергнет весь новый участок.
    Граница ищется делением пополам, а не по одному бару.
    """
    code, step = fr.TF_SPEC[tf]
    first = (await db.execute(
        select(Candle.ts).where(Candle.figi == figi, Candle.interval == code)
        .order_by(Candle.ts).limit(1))).scalar()
    if first is None:
        return None
    last = fr.last_closed_bucket(now, step)
    lo, hi = first, last          # lo — расходится (старая шкала), hi — согласуется?
    if await _agrees(db, figi, tf, hi):
        return None                # весь ряд согласован, резать нечего
    if not await _agrees(db, figi, tf, lo):
        # Первый бар расходится: ищем первую согласованную точку.
        left, right = lo, hi
        while right - left > timedelta(seconds=step):
            mid = left + (right - left) / 2
            mid = fr.bucket_start(mid, step)
            ok = await _agrees(db, figi, tf, mid)
            if ok is None:
                # Пустой бакет — судить нельзя, сдвигаем границу вправо.
                left = mid + timedelta(seconds=step)
                continue
            if ok:
                right = mid
            else:
                left = mid + timedelta(seconds=step)
        return right
    return None


async def _drop_inconsistent_tail(db, figi: str, tf: str, *, max_drop: int = 200) -> int:
    """Убрать хвост ТФ, который не согласуется с минутами (след прежней испорченной шкалы).

    Источник истины — минутный ряд. Если последний бар ТФ расходится с агрегатом минут
    за тот же бакет более чем на 50 %, он не может служить опорой для догона, и защита
    от скачков отвергнет весь новый участок. Такой бар снимается, и проверка повторяется.
    """
    code, step = fr.TF_SPEC[tf]
    dropped = 0
    while dropped < max_drop:
        last = (await db.execute(
            select(Candle.ts, Candle.open, Candle.high, Candle.low, Candle.close)
            .where(Candle.figi == figi, Candle.interval == code)
            .order_by(Candle.ts.desc()).limit(1))).one_or_none()
        if last is None:
            break
        agg = await _minutes_aggregate(db, figi, tf, last[0])
        if agg is None:
            # Минут за бакет нет — судить не о чем, оставляем как есть.
            break
        tf_close = float(last[4])
        ratio = (float(agg["close"]) / tf_close) if tf_close else None
        if ratio is None or 0.66 <= ratio <= 1.5:
            break
        await db.execute(text("DELETE FROM candles WHERE figi=:f AND interval=:i AND ts=:t"),
                         {"f": figi, "i": code, "t": last[0]})
        await db.commit()
        dropped += 1
    return dropped


async def refresh_one(db, figi: str, tf: str, *, dry: bool, now: datetime) -> dict:
    code, step = fr.TF_SPEC[tf]
    st = await fr.state_for(db, figi, tf, now=now)
    if st.status != "stale":
        return {"figi": figi, "tf": tf, "state": st.status, "written": 0}
    if dry:
        return {"figi": figi, "tf": tf, "state": st.status, "written": 0,
                "plan": f"{st.actual_ts:%Y-%m-%d %H:%M}..{st.expected_ts:%Y-%m-%d %H:%M}"}
    dropped = await _drop_inconsistent_tail(db, figi, tf)
    cut = await _scale_cut(db, figi, tf, now=now)
    if dropped:
        st = await fr.state_for(db, figi, tf, now=now)
        if st.status != "stale":
            return {"figi": figi, "tf": tf, "state": st.status, "written": 0, "dropped": dropped}
    # Догоняем от границы согласованной шкалы (или от последнего бакета) до последнего закрытого.
    span_from = st.actual_ts
    if cut is not None and cut > span_from:
        span_from = cut
    span_to = st.expected_ts + timedelta(seconds=step) - timedelta(seconds=1)
    try:
        written = await resample_from_1m(db, figi, tf, span_from, span_to)
        await db.commit()
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        return {"figi": figi, "tf": tf, "state": st.status, "written": 0, "dropped": dropped,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}
    return {"figi": figi, "tf": tf, "state": st.status, "written": written, "dropped": dropped,
            "from": span_from.isoformat()}


async def _verify_matches(db, figi: str, tf: str, *, bucket_count: int = 20) -> int:
    """Сверить последние бакеты ТФ с агрегатом минут. Возвращает число расхождений.

    Это замена защите от скачков на время догона: там опора может стоять в прежней
    шкале, а здесь проверяется сам результат — бар обязан совпадать с минутами.
    """
    code, step = fr.TF_SPEC[tf]
    tail = (await db.execute(
        select(Candle.ts, Candle.close).where(Candle.figi == figi, Candle.interval == code)
        .order_by(Candle.ts.desc()).limit(bucket_count))).all()
    bad = 0
    for ts, close in tail:
        agg = await _minutes_aggregate(db, figi, tf, ts)
        if agg is None or not float(close):
            continue
        ratio = float(agg["close"]) / float(close)
        if not (0.99 <= ratio <= 1.01):
            bad += 1
    return bad


def invalid_start(figi: str) -> datetime | None:
    """Первый момент, с которого минуты достоверны (по манифесту невалидности)."""
    ranges = di.ranges_for(figi, 1)
    if not ranges:
        return None
    last_bad = max(r["ts_to"] for r in ranges)
    if isinstance(last_bad, str):
        last_bad = datetime.fromisoformat(last_bad.replace("Z", "+00:00"))
    if last_bad.tzinfo is None:
        last_bad = last_bad.replace(tzinfo=timezone.utc)
    return last_bad + timedelta(minutes=1)


async def build_missing(db, figi: str, tf: str, *, now: datetime, dry: bool) -> dict:
    """Построить отсутствующий ряд ТФ от границы достоверных минут.

    Ничего не выдумывается: начало берётся из манифеста невалидности (последний
    недостоверный бакет минут + 1 минута), конец — последний закрытый бакет.
    """
    code, step = fr.TF_SPEC[tf]
    cut = invalid_start(figi) or CLEAN_FROM
    if cut < CLEAN_FROM:
        cut = CLEAN_FROM
    first_minute = (await db.execute(
        select(Candle.ts).where(Candle.figi == figi, Candle.interval == 1)
        .order_by(Candle.ts).limit(1))).scalar()
    if first_minute is None:
        return {"figi": figi, "tf": tf, "state": "missing", "written": 0,
                "skipped": "минут нет вовсе"}
    if first_minute > cut:
        cut = first_minute
    span_from = fr.bucket_start(cut, step)
    span_to = fr.last_closed_bucket(now, step) + timedelta(seconds=step) - timedelta(seconds=1)
    if span_from > span_to:
        return {"figi": figi, "tf": tf, "state": "missing", "written": 0, "skipped": "окно пусто"}
    if dry:
        return {"figi": figi, "tf": tf, "state": "missing", "written": 0,
                "plan": f"{span_from:%Y-%m-%d %H:%M}..{span_to:%Y-%m-%d %H:%M}"}
    try:
        written = await resample_from_1m(db, figi, tf, span_from, span_to)
        await db.commit()
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        return {"figi": figi, "tf": tf, "state": "missing", "written": 0,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}
    return {"figi": figi, "tf": tf, "state": "missing", "written": written,
            "from": span_from.isoformat()}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--figi", default="")
    ap.add_argument("--tf", default="")
    ap.add_argument("--log", default="")
    ap.add_argument("--build-missing", action="store_true",
                    help="построить отсутствующие ряды ТФ от границы достоверных минут")
    a = ap.parse_args()

    tfs = [a.tf] if a.tf else list(fr.active_tfs())
    bad = [t for t in tfs if t not in fr.TF_SPEC]
    if bad:
        print(f"неизвестные ТФ: {bad}; известны {sorted(fr.TF_SPEC)}")
        return 2

    now = datetime.now(timezone.utc)
    # Догон строит ТФ строго из минут, без обращения к сети. Прежняя история могла быть
    # записана в другой шкале, и опора тогда заведомо неверна, поэтому защита от скачков
    # на время догона снята, а вместо неё результат сверяется с минутным рядом
    # (_verify_matches): бар обязан совпасть с агрегатом минут с точностью до процента.
    os.environ["CANDLE_JUMP_MAX_PCT"] = "0"
    async with SessionLocal() as db:
        if a.figi:
            figis = [a.figi]
        else:
            figis = [r[0] for r in (await db.execute(
                select(distinct(Candle.figi)).where(Candle.interval == 1))).all()]
        before = fr.summarize(await fr.states(db, figis))
        print(f"до догона: {before['counts']}")
        results = []
        for figi in figis:
            for tf in tfs:
                r = await refresh_one(db, figi, tf, dry=a.dry, now=now)
                if r.get("state") == "missing" and a.build_missing:
                    r = await build_missing(db, figi, tf, now=now, dry=a.dry)
                results.append(r)
        after = fr.summarize(await fr.states(db, figis))
        print(f"после догона: {after['counts']}")
        wrote = sum(r.get("written", 0) for r in results)
        errors = [r for r in results if r.get("error")]
        print(f"дописано баров: {wrote}{' (dry)' if a.dry else ''}, ошибок: {len(errors)}")
        for e in errors[:10]:
            print("  ", e)
        if not a.dry:
            mismatched = []
            for r in results:
                if not r.get("written"):
                    continue
                n = await _verify_matches(db, r["figi"], r["tf"])
                if n:
                    mismatched.append({"figi": r["figi"], "tf": r["tf"], "bad_buckets": n})
            print(f"сверка с минутами: расходится {len(mismatched)} рядов")
            for m in mismatched[:10]:
                print("  ", m)
        else:
            mismatched = []
    if a.log:
        Path(a.log).parent.mkdir(parents=True, exist_ok=True)
        Path(a.log).write_text(json.dumps({"before": before, "after": after,
                                           "results": results, "mismatched": mismatched},
                                          ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"лог: {a.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
