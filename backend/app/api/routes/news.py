"""Новости (MOEX ISS + RSS) — для AI/MCP и проверки получения.

GET /api/v1/news?tickers=SBER,GAZP&limit=30 — свежие новости, опционально по тикерам.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/news", tags=["news"])


@router.get("")
async def get_news(tickers: str = "", limit: int = 30, source: str = "") -> dict:
    """Свежие новости. tickers=SPER,GAZP — фильтр по упоминаниям; source=MOEX|Ведомости…"""
    from app.services.news import fetch_news, filter_by_tickers, news_payload
    items = fetch_news()
    if source:
        _s = source.strip().lower()
        items = [x for x in items if x.source.lower() == _s]
    _tk = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if _tk:
        items = filter_by_tickers(items, _tk)
    _lim = max(1, min(int(limit), 200))
    return {"count": len(items), "tickers": _tk, "news": news_payload(items[:_lim])}
