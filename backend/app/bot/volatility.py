"""volatility.py — прототип динамического universe из MOEX ISS (TQBR quotes).

Одним запросом (0.7с) получаем волатильность (HIGH-LOW)/WAPRICE + оборот
(VALTODAY) всех акций TQBR, ранжируем по волатильности при ликвидности,
обновляем таблицу universe (eligible / eligible2).

Файл-заготовка: ложится в backend/app/bot/volatility.py
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime

from sqlalchemy import text

TQBR_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR"
    "/securities.json?iss.only=marketdata"
)
MIN_TURNOVER_RUB = 100e6  # минимальный оборот дня, ₽
TOP_ELIGIBLE = 30
TOP_ELIGIBLE2 = 60 - TOP_ELIGIBLE


def fetch_tqbr_quotes() -> list[dict]:
    """Снимок всего рынка TQBR: secid, rng_pct, turnover, wap, last, chg."""
    data = json.loads(urllib.request.urlopen(TQBR_URL, timeout=20).read())
    md = data["marketdata"]
    cols = {n: i for i, n in enumerate(md["columns"])}

    def _f(r, k):
        try:
            return float(r[cols[k]])
        except (TypeError, ValueError):
            return None

    out = []
    for r in md["data"]:
        wap = _f(r, "WAPRICE")
        hi = _f(r, "HIGH")
        lo = _f(r, "LOW")
        val = _f(r, "VALTODAY")
        # last = _f(r, "LAST"); chg = _f(r, "LASTCHANGEPRCNT")
        if not wap or wap <= 0 or not val or val < MIN_TURNOVER_RUB:
            continue
        rng = ((hi - lo) / wap * 100) if (hi and lo and hi > 0) else 0.0
        out.append({
            "ticker": r[cols["SECID"]],
            "rng_pct": rng,
            "turnover": val,
            "wap": wap,
        })
    out.sort(key=lambda x: x["rng_pct"], reverse=True)
    return out


def rank_candidates(quotes: list[dict], top_eligible: int = TOP_ELIGIBLE) -> dict:
    """Разбивает кандидатов на eligible (топ-30 по rng%) и eligible2 (следующие 30)."""
    return {
        "eligible": quotes[:top_eligible],
        "eligible2": quotes[top_eligible : top_eligible + TOP_ELIGIBLE2],
    }


def sync_universe(quotes: list[dict], engine) -> dict:
    """Обновляет таблицу universe: upsert tier по рангу, даунгрейд выбывших.

    У каждой акции берём figi/lot/avg_price из instruments (по ticker).
    Выбывшие из топ-60 получают tier=NULL (чтобы бот не торговал).
    """
    ranked = rank_candidates(quotes)
    tickers = [q["ticker"] for q in quotes[:60]]
    if not tickers:
        return {"rows": 0, "errors": "empty quotes"}

    with engine.connect() as db:
        # существующие figi/ticker в universe
        db_rows = db.execute(
            text("SELECT figi, ticker FROM universe")
        ).fetchall()
        cur_figi = {r[0]: r[1] for r in db_rows}
        # маппинг ticker -> instruments (figi, lot, avg_price)
        inst_rows = db.execute(
            text("SELECT figi, ticker, lot FROM instruments WHERE ticker = ANY(:t)")
            .bindparams(t=tickers)
        ).fetchall()
        inst_by_tick = {r[1]: r for r in inst_rows}

    inserts = []
    for q in quotes[:60]:
        ticker = q["ticker"]
        inst = inst_by_tick.get(ticker)
        if not inst:
            continue  # ETF/нет FIGI — пропускаем
        figi, _, lot = inst
        tier = "eligible" if q in ranked["eligible"] else "eligible2"
        inserts.append({
            "figi": figi, "ticker": ticker, "tier": tier,
            "lot": int(lot) if lot else 10,
            "price": q["wap"], "turnover": q["turnover"],
        })

    with engine.begin() as db:
        for d in inserts:
            db.execute(text(
                "INSERT INTO universe (figi, ticker, eligible_tier, lot_size, "
                "avg_price, avg_daily_turnover, sector, updated_at) "
                "VALUES (:figi, :ticker, :tier, :lot, :price, :turnover, NULL, now()) "
                "ON CONFLICT (figi) DO UPDATE SET "
                "ticker=EXCLUDED.ticker, eligible_tier=EXCLUDED.eligible_tier, "
                "lot_size=EXCLUDED.lot_size, avg_price=EXCLUDED.avg_price, "
                "avg_daily_turnover=EXCLUDED.avg_daily_turnover, updated_at=now()"
            ), d)
        # выбывшие из топ-60 -> eligible_tier=NULL
        keep = {d["figi"] for d in inserts}
        old_figs = [f for f in cur_figi if f not in keep and f not in ("BBG0047315D0", "BBG0047315Y7", "BBG00475K2X9", "BBG004S682Z6", "BBG004S68473", "BBG004S68696", "BBG00KDWPPW2", "BBG00QKJSX05")]
        drop = [f for f in old_figs if f not in keep]
        if drop:
            db.execute(
                text("UPDATE universe SET eligible_tier=NULL WHERE figi = ANY(:d)")
                .bindparams(d=drop)
            )
    return {
        "rows": len(inserts),
        "eligible": len([d for d in inserts if d["tier"] == "eligible"]),
        "removed": len(drop),
    }