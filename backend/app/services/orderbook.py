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
        return bids, asks, _q(ob.last_price), getattr(ob, "order_book_ts", None)

    bids, asks, last, ts = await asyncio.to_thread(_fetch)
    bb = bids[0]["p"] if bids else None
    ba = asks[0]["p"] if asks else None
    mid = ((bb + ba) / 2) if (bb and ba) else (last or 0.0)
    spread_bps = ((ba - bb) / mid * 10000) if (bb and ba and mid > 0) else None
    bq = sum(b["q"] for b in bids)
    aq = sum(a["q"] for a in asks)
    imb = (bq - aq) / (bq + aq) if (bq + aq) > 0 else None
    depth_rub = sum(b["p"] * b["q"] for b in bids) + sum(a["p"] * a["q"] for a in asks)
    out = {
        "figi": figi, "ts": str(ts) if ts else None, "last": last,
        "best_bid": bb, "best_ask": ba,
        "spread_bps": round(spread_bps, 1) if spread_bps is not None else None,
        "bid_qty": bq, "ask_qty": aq,
        "imbalance": round(imb, 3) if imb is not None else None,
        "depth_rub": round(depth_rub, 0),
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
