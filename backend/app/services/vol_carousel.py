"""vol_carousel.py — волатильная карусель (double-carousel).

Логика: периодически снимаем TQBR quotes из MOEX ISS (тот же источник, что
и правый сайдбар «Рынок TQBR»), ранжируем весь рынок по дневной волатильности
RNG% = (HIGH−LOW)/WAPRICE, и принудительно держим в eligible только top-N самых
волатильных тикеров. Остальные (в т.ч. уже в universe) — снимаем с eligible.

Карусель НЕ трогает runtime напрямую: она меняет только метки eligible_tier в
таблице universe. Дальше всё делает штатный цикл бота:
  - свежая метка + ≥50 свечей 1min → _hot_add_universe подписывает feed и строит стратегию;
  - снятая метка → _hot_add_universe убирает тикер (feed.remove_figis + deactivate_figi).

Режимы:
  - enabled=True  → реально обновляет metky eligible в БД.
  - enabled=False (по умолчанию) → DRY-RUN: только собирает отчёт «что бы сделали»,
    логирует и кладёт в events/report, но БД НЕ трогает.

Запускается отдельной фоновой задачей runtime (см. _vol_carousel_loop).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _blacklist_set(raw: str) -> set[str]:
    """'A B,C' / 'A,B' → {'A','B','C'} (upper)."""
    if not raw:
        return set()
    return {t.strip().upper() for t in raw.replace(",", " ").split() if t.strip()}


def rank_volatile(
    quotes: dict,
    top_n: int = 20,
    min_rng: float = 0.5,
    lot_min: float = 1.0,
    blacklist: str = "",
    current_universe: list = None,
) -> dict:
    """Ранжирует тикеры по RNG% и возвращает желаемый набор.

    quotes: {ticker: {"rng_pct": float, "price": float|None,
                       "turnover": float|None, ...}} — из fetch_tqbr_market().

    Возвращает:
      {
        "now": [ {ticker, rng_pct, price, turnover}, ...],   # желаемые (top-N)
        "add":  [...],  # тикеры, которых надо добавить в eligible
        "keep": [...],  # уже есть в universe, оставить
        "drop": [...],  # тикеры в universe вне top-N — снять с eligible
        "reasons": {...},  # ticker -> причина отсева (для логов)
        "all_sorted": [...],  # полный ранкинг для UI/диагностики
      }
    """
    black = _blacklist_set(blacklist)
    universe: set[str] = {t.upper() for t in (current_universe or [])}

    scored = []
    for tk, q in (quotes or {}).items():
        rng = float(q.get("rng_pct") or 0.0)
        price = float(q.get("price") or 0.0)
        turnover = float(q.get("turnover") or 0.0)
        tk_u = tk.upper()
        scored.append({
            "ticker": tk_u, "rng_pct": rng, "price": price, "turnover": turnover,
        })
    scored.sort(key=lambda x: (x["rng_pct"] or 0.0), reverse=True)

    now = []
    add, keep, drop = [], [], []
    reasons: dict[str, str] = {}
    seat = 0
    for s in scored:
        tk = s["ticker"]
        if tk in black:
            reasons[tk] = "blacklist"
            continue
        # Мусор: дешёвые/мёртвые бумаги без оборота и цены.
        if (s["price"] or 0) <= 0:
            reasons[tk] = "no_price"
            continue
        if s["rng_pct"] <= 0:
            reasons[tk] = "rng_zero"
            continue
        # Место в top-N ещё не занято ИЛИ волатильность выше порога.
        takes_seat = (seat < top_n) and (s["rng_pct"] >= min_rng)
        if takes_seat:
            now.append(s)
            seat += 1
            entry = {"ticker": tk, "rng_pct": round(s["rng_pct"], 3),
                     "price": s["price"], "turnover": round(s["turnover"])}
            if tk in universe:
                keep.append(entry)
            else:
                add.append(entry)
        elif tk in universe:
            # Тикер уже в юниверсе, но волатильность не тянет top-N → снять.
            drop.append({"ticker": tk, "rng_pct": round(s["rng_pct"], 3),
                         "seat": seat + 1})
            reasons[tk] = f"below_top({s['rng_pct']:.2f}%<{'top' + str(top_n)})"

    return {
        "now": now, "add": add, "keep": keep, "drop": drop,
        "reasons": reasons, "all_sorted": scored,
        "meta": {"top_n": top_n, "min_rng": min_rng, "lot_min": lot_min},
    }


def dry_run_report(plan: dict, current_universe: list) -> dict:
    """Человекочитаемый отчёт dry-run: что бы сделали и почему."""
    return {
        "mode": "dry-run",
        "ts": datetime.now(timezone.utc).isoformat(),
        "universe_now": len(current_universe or []),
        "top_n": plan["meta"]["top_n"],
        "target_count": len(plan["now"]),
        "would_add": plan["add"],
        "would_keep": plan["keep"],
        "would_drop": plan["drop"],
        "reasons_dropped": {k: v for k, v in plan["reasons"].items()
                            if k in {d["ticker"] for d in plan["drop"]}},
    }


async def apply_vol_carousel(
    db: "AsyncSession",
    quotes: dict,
    top_n: int,
    min_rng: float,
    lot_min: float,
    blacklist: str,
    current_universe: list,
    enabled: bool,
    emit=None,
) -> dict:
    """Основная точка входа цикла.

    enabled=False → dry-run (отчёт, БД не трогаем).
    enabled=True  → реально UPSERT/DELETE меток eligible_tier в universe.
    emit(msg) — колбэк лога (например runtime._log).
    """
    from sqlalchemy import text

    plan = rank_volatile(quotes, top_n=top_n, min_rng=min_rng,
                         lot_min=lot_min, blacklist=blacklist,
                         current_universe=current_universe)
    report = dry_run_report(plan, current_universe)

    if not enabled:
        # DRY-RUN: не трогаем БД, только лог.
        if emit:
            emit(f"🫧 VOL-CAROUSEL (dry-run): add={len(plan['add'])} "
                 f"keep={len(plan['keep'])} drop={len(plan['drop'])}")
            for e in plan["add"]:
                emit(f"   + add    {e['ticker']:6s} RNG={e['rng_pct']:.2f}%")
            for e in plan["drop"]:
                emit(f"   - drop   {e['ticker']:6s} RNG={e['rng_pct']:.2f}%")
        return report

    # Реальное применение: меняем метки eligible в БД.
    add_done, drop_done = [], []
    for e in plan["add"]:
        tk = e["ticker"]
        row = (await db.execute(
            text("SELECT figi, lot_size FROM universe WHERE ticker = :t"), {"t": tk}
        )).first()
        if row is None:
            if emit:
                emit(f"   ✗ add skipped (нет в universe-таблице): {tk}")
            continue
        figi, lot = row
        await db.execute(text(
            "UPDATE universe SET eligible_tier = 'eligible', updated_at = now() "
            "WHERE figi = :f AND eligible_tier IS NULL"
        ), {"f": figi})
        add_done.append({"ticker": tk, "figi": figi, "lot": lot, "rng_pct": e["rng_pct"]})
    for e in plan["drop"]:
        tk = e["ticker"]
        row = (await db.execute(
            text("SELECT figi FROM universe WHERE ticker = :t"), {"t": tk}
        )).first()
        if row is None:
            continue
        figi = row[0]
        await db.execute(text(
            "UPDATE universe SET eligible_tier = NULL, updated_at = now() "
            "WHERE figi = :f AND eligible_tier IS NOT NULL"
        ), {"f": figi})
        drop_done.append({"ticker": tk, "figi": figi})
    await db.commit()

    if emit:
        emit(f"🫧 VOL-CAROUSEL: добавлено eligible {len(add_done)}, снято {len(drop_done)}")
        for e in add_done:
            emit(f"   + eligible {e['ticker']:6s} ({e['figi'][-6:]}) RNG={e['rng_pct']:.2f}%")
        for e in drop_done:
            emit(f"   - removed  {e['ticker']:6s} ({e['figi'][-6:]})")

    report["mode"] = "enabled"
    report["add_done"] = add_done
    report["drop_done"] = drop_done
    return report


async def run_vol_carousel_once(
    db: "AsyncSession",
    settings,
    runtime,
    emit=None,
) -> dict:
    """Удобная обёртка: берёт quotes (кэшированные TQBR), текущий universe,
    настройки из settings и вызывает apply_vol_carousel."""
    from app.api.routes.screener import fetch_tqbr_market_async

    quotes = await fetch_tqbr_market_async()
    current_universe = [u.get("ticker", "") for u in
                        list(getattr(runtime, "universe", []) or [])]
    return await apply_vol_carousel(
        db=db,
        quotes=quotes,
        top_n=settings.vol_carousel_top_n,
        min_rng=settings.vol_carousel_min_rng,
        lot_min=settings.vol_carousel_lot_min,
        blacklist=settings.vol_carousel_blacklist,
        current_universe=current_universe,
        enabled=settings.vol_carousel_enabled,
        emit=emit,
    )