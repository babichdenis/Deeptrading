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
from datetime import datetime, timedelta, timezone

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
    dt: datetime | None = None   # распарсенное время публикации (UTC-aware)

    @property
    def age_min(self) -> int | None:
        if self.dt is None:
            return None
        try:
            return max(0, int((datetime.now(timezone.utc) - self.dt).total_seconds() // 60))
        except Exception:
            return None


def _parse_ts(ts: str) -> datetime | None:
    """Распарсить время публикации: RSS (RFC822, МСК) или MOEX ("YYYY-MM-DD HH:MM:SS", МСК)."""
    t = str(ts or "").strip()
    if not t:
        return None
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(t)
        if d is not None:
            return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(t[:19], fmt).replace(
                tzinfo=timezone(timedelta(hours=3))).astimezone(timezone.utc)
        except Exception:
            continue
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


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
        it.dt = _parse_ts(it.ts)
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
             "ts": x.ts, "age_min": x.age_min, "tickers": x.tickers} for x in items]


# --- News blackout: детерминированный стоп по ключевым словам ------------------------

NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "санкц", "sanctions", "ограничительн", "дестабилизац", "дискретн",
    "приостанов", "торги приостановлены", "допэмисс",
    "дивидендный гэп", "авари", "пожар", "взрыв", "остановк производства",
    "крупный иск", "штраф", "банкротств", "дефолт", "обыск", "изъят",
)

# Составные правила: (тема, стемы события) — «дивиденд» + «отмен/отказ/не выплат…».
NEGATIVE_COMBOS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("дивиденд", ("отмен", "отказ", "не выплат", "перенос", "снижен", "приостанов")),
    ("эмисси", ("доп", "дополнительн")),
)


def blackout_reason(items: list[NewsItem], ticker: str, window_min: int = 60) -> str | None:
    """Причина стоп-блока тикера по свежей негативной новости (или None).

    Смотрит только новости с явным упоминанием тикера и возрастом <= window_min.
    """
    tk = str(ticker or "").upper()
    if not tk:
        return None
    for x in items:
        if tk not in (x.tickers or []):
            continue
        age = x.age_min
        if age is None or age > int(window_min):
            continue
        t = x.title.lower()
        for kw in NEGATIVE_KEYWORDS:
            if kw in t:
                return f"{x.source}: {x.title[:140]} (возраст {age} мин)"
        for topic, stems in NEGATIVE_COMBOS:
            if topic in t and any(w in t for w in stems):
                return f"{x.source}: {x.title[:140]} (возраст {age} мин)"
    return None
