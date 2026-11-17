"""Живой стакан T-Invest: единая точка для API и движка (AI-гейты).

Держит ОДИН переиспользуемый Client (создание нового ~14с!) и короткий кэш,
иначе гейт и трейдер дёргают стакан десятками запросов за цикл.
"""
from __future__ import annotations

import asyncio
import threading
import time

_client = None
_client_key = None
_lock = threading.Lock()
_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_TTL = 5.0

# Имя метки времени стакана зависит от слоя SDK: в сыром ответе gRPC это
# orderbook_ts, в удобной модели — time. Читаем оба, чтобы метка не терялась
# при обновлении пакета.
_TS_FIELDS = ("orderbook_ts", "order_book_ts", "time")

SHARE_BOARDS = frozenset({"TQBR", "TQTF", "TQTY", "MTQR", "SPBXM", "SPBEQRU", "SPBRU"})
_META_TTL = 600.0
_meta_cache: dict[str, tuple[float, dict | None]] = {}


def _book_ts(ob) -> str | None:
    """Метка времени стакана в ISO-виде или None, если источник её не дал.

    Нулевая метка (эпоха) означает «источник времени не заполнен», а не 1970 год:
    закрытый рынок отдаёт именно такую.
    """
    for name in _TS_FIELDS:
        value = getattr(ob, name, None)
        if value is None:
            continue
        if hasattr(value, "seconds"):  # google.protobuf.Timestamp
            if not value.seconds:
                continue
            from datetime import datetime, timezone

            return datetime.fromtimestamp(value.seconds, tz=timezone.utc).isoformat()
        if hasattr(value, "isoformat"):
            if getattr(value, "year", 2000) <= 1971:
                continue
            return value.isoformat()
        return str(value)
    return None


async def instrument_meta(figi: str) -> dict | None:
    """Лот и площадка инструмента: без них рублёвая глубина стакана неизвестна."""
    hit = _meta_cache.get(figi)
    now = time.monotonic()
    if hit is not None and (now - hit[0]) < _META_TTL:
        return hit[1]
    meta = None
    try:
        from sqlalchemy import text as _sql

        from app.database import SessionLocal

        async with SessionLocal() as db:
            row = (
                await db.execute(
                    _sql("SELECT lot, class_code, currency FROM instruments WHERE figi = :f"),
                    {"f": figi},
                )
            ).first()
        if row is not None:
            meta = {
                "lot": int(row[0] or 0),
                "class_code": str(row[1] or ""),
                "currency": str(row[2] or ""),
            }
    except Exception:
        meta = None
    _meta_cache[figi] = (now, meta)
    return meta


def book_depth_rub(bids: list[dict], asks: list[dict], meta: dict | None) -> tuple[float | None, str]:
    """Денежная глубина стакана: quantity в лотах, поэтому цена × лот × количество."""
    if meta is None:
        return None, "instrument_unknown"
    if meta.get("class_code") not in SHARE_BOARDS or meta.get("lot", 0) <= 0:
        return None, "not_share"
    currency = str(meta.get("currency") or "").strip().lower()
    if not currency:
        return None, "currency_unknown"
    if currency != "rub":
        return None, "not_rub"
    total = sum(b["p"] * b["q"] for b in bids) + sum(a["p"] * a["q"] for a in asks)
    return total * meta["lot"], "shares"


async def fetch_orderbook(figi: str, depth: int = 10) -> dict:
    """Стакан: last/best_bid/best_ask/spread_bps/imbalance/depth_rub + топ-5 уровней.

    Бросает исключение, если стакан недоступен (вызывающий сам решает, что делать).
    """
    from app.config import get_settings
    from t_tech.invest import Client

    _depth = max(1, min(int(depth), 20))
    _key = (figi, _depth)
    _hit = _cache.get(_key)
    if _hit is not None and (time.monotonic() - _hit[0]) < _TTL:
        return _hit[1]
    _token = get_settings().feed_token

    def _fetch():
        global _client, _client_key
        with _lock:
            if _client is None or _client_key != _token:
                _client = Client(_token).__enter__()
                _client_key = _token
        ob = _client.market_data.get_order_book(figi=figi, depth=_depth)

        def _q(v):
            return float(v.units) + float(v.nano) / 1e9 if v is not None else 0.0

        bids = [{"p": _q(b.price), "q": int(b.quantity)} for b in (ob.bids or [])]
        asks = [{"p": _q(a.price), "q": int(a.quantity)} for a in (ob.asks or [])]
        return bids, asks, _q(ob.last_price), _book_ts(ob), bool(getattr(ob, "is_consistent", True))

    bids, asks, last, ts, consistent = await asyncio.to_thread(_fetch)
    meta = await instrument_meta(figi)
    depth_rub, depth_state = book_depth_rub(bids, asks, meta)
    bb = bids[0]["p"] if bids else None
    ba = asks[0]["p"] if asks else None
    mid = ((bb + ba) / 2) if (bb and ba) else (last or 0.0)
    spread_bps = ((ba - bb) / mid * 10000) if (bb and ba and mid > 0) else None
    bq = sum(b["q"] for b in bids)
    aq = sum(a["q"] for a in asks)
    imb = (bq - aq) / (bq + aq) if (bq + aq) > 0 else None
    # Пустой, перекошенный или несогласованный стакан — не «нормальная ликвидность»:
    # состояние называется прямо, чтобы гейт не принял его за живой рынок.
    if not bids and not asks:
        state = "empty"
    elif not bids or not asks:
        state = "one_sided"
    elif ba < bb:
        state = "crossed"
    elif not consistent:
        state = "inconsistent"
    else:
        state = "ok"
    out = {
        "figi": figi, "ts": ts, "state": state, "is_consistent": consistent, "last": last,
        "best_bid": bb, "best_ask": ba,
        "spread_bps": round(spread_bps, 1) if spread_bps is not None else None,
        "bid_qty": bq, "ask_qty": aq,
        "imbalance": round(imb, 3) if imb is not None else None,
        "depth_rub": round(depth_rub, 0) if depth_rub is not None else None,
        "depth_rub_state": depth_state,
        "lot": (meta or {}).get("lot") if depth_state == "shares" else None,
        "top_bids": bids[:5], "top_asks": asks[:5],
    }
    try:
        _cache[_key] = (time.monotonic(), out)
        if len(_cache) > 64:
            _oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(_oldest, None)
    except Exception:
        pass
    return out
