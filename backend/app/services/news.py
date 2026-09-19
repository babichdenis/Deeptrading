"""Новости для AI/гейтов: MOEX ISS + RSS деловых изданий.

Пока только сбор/фильтрация (проверка получения). Куда встраивать (гейт/промпт/MCP) —
отдельным шагом. Все источники бесплатные, без ключей.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

_TIMEOUT = 12.0
_UA = "Mozilla/5.0 (Macintosh) deeptrading-news/1.0"

# --- Источники -----------------------------------------------------------------------
RSS_SOURCES: dict[str, str] = {
    "Ведомости": "https://www.vedomosti.ru/rss/news",
    "Коммерсант": "https://www.kommersant.ru/RSS/news.xml",
    "Интерфакс": "https://www.interfax.ru/rss.asp",
    "ТАСС": "https://tass.ru/rss/v2.xml",
}
MOEX_SITENEWS = "https://iss.moex.com/iss/sitenews.json"

# Алиасы компаний → тикер (для заголовков без явного тикера).
TICKER_ALIASES: dict[str, tuple[str, ...]] = {
    "SBER": ("сбербанк", "сбер ", "сбера", "сберу"),
    "GAZP": ("газпром",),
    "LKOH": ("лукойл",),
    "ROSN": ("роснефт",),
    "NVTK": ("новатэк", "новатэка"),
    "TATN": ("татнефт",),
    "SNGSP": ("сургутнефтегаз", "сургут"),
    "MTSS": ("мтс",),
    "VTBR": ("втб",),
    "GMKN": ("норникел", "норильск"),
    "NLMK": ("нлмк",),
    "MAGN": ("ммк", "магнитогорск"),
    "CHMF": ("северстал",),
    "PLZL": ("полюс",),
    "RUAL": ("русал",),
    "ALRS": ("алроса",),
    "AFLT": ("аэрофлот",),
    "AFKS": ("афк", "система"),
    "MOEX": ("московская биржа", "мосбиржа", "moex"),
    "YDEX": ("яндекс",),
    "OZON": ("ozon", "озон"),
    "MVID": ("м.видео", "мвидео"),
    "SIBN": ("газпром нефть",),
    "PHOR": ("фосagro", "фосагро"),
    "LENT": ("лента",),
    "ASTR": ("астра",),
    "T": ("т-технологии", "тинькофф"),
}

_cache: dict = {"ts": 0.0, "items": []}
_CACHE_TTL = 60.0


@dataclass
class NewsItem:
    source: str
    title: str
    url: str = ""
    ts: str = ""          # ISO/строка как отдал источник (МСК у RSS)
    tickers: list[str] = field(default_factory=list)


def _get(url: str, timeout: float = _TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_moex(limit: int) -> list[NewsItem]:
    d = json.loads(_get(MOEX_SITENEWS))
    cols = d["sitenews"]["columns"]
    rows = d["sitenews"]["data"]
    i_id, i_tag, i_t, i_pub = (cols.index("id"), cols.index("tag"),
                               cols.index("title"), cols.index("published_at"))
    out = []
    for r in rows[:limit]:
        out.append(NewsItem(source="MOEX", title=str(r[i_t]),
                            url=f"https://www.moex.com/n{r[i_id]}",
                            ts=str(r[i_pub] or "")))
    return out


def _fetch_rss(name: str, url: str, limit: int) -> list[NewsItem]:
    root = ET.fromstring(_get(url))
    out = []
    for it in root.findall(".//item")[:limit]:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        if title:
            out.append(NewsItem(source=name, title=title, url=link, ts=pub))
    return out


def detect_tickers(text: str, tickers: list[str] | None = None) -> list[str]:
    """Найти тикеры в заголовке: явный тикер словом + алиасы компаний."""
    t = " " + str(text).lower() + " "
    found: list[str] = []
    known = set(x.upper() for x in (tickers or list(TICKER_ALIASES.keys())))
    for tk in known:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(tk.lower())}(?![A-Za-z0-9])", t):
            found.append(tk)
    for tk, aliases in TICKER_ALIASES.items():
        if tk in found:
            continue
        if any(a in t for a in aliases):
            found.append(tk)
    return sorted(set(found))


def fetch_news(limit_per_source: int = 50, use_cache: bool = True) -> list[NewsItem]:
    """Свежие новости из всех источников (кэш 60с). Ошибки источников не роняют сбор."""
    now = time.monotonic()
    if use_cache and _cache["items"] and now - _cache["ts"] < _CACHE_TTL:
        return list(_cache["items"])
    items: list[NewsItem] = []
    try:
        items.extend(_fetch_moex(limit_per_source))
    except Exception:
        pass
    for name, url in RSS_SOURCES.items():
        try:
            items.extend(_fetch_rss(name, url, limit_per_source))
        except Exception:
            continue
    for it in items:
        it.tickers = detect_tickers(it.title)
    _cache["items"] = items
    _cache["ts"] = now
    return list(items)


def filter_by_tickers(items: list[NewsItem], tickers: list[str]) -> list[NewsItem]:
    """Оставить новости, где упомянут хотя бы один из тикеров (или его алиас)."""
    want = {str(t).upper() for t in tickers if t}
    out = []
    for it in items:
        tags = set(it.tickers) | set(detect_tickers(it.title, list(want)))
        if tags & want:
            it.tickers = sorted(tags)
            out.append(it)
    return out


def news_payload(items: list[NewsItem]) -> list[dict]:
    return [{"source": x.source, "title": x.title, "url": x.url,
             "ts": x.ts, "tickers": x.tickers} for x in items]
