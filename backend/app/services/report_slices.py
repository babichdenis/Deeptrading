"""Ядро срезов аналитики прогонов (сессии, режим ADX, ER, час, день недели, тикер).

Единственный источник правды для:
  - `scripts/trades_split.py` (консольная разбивка),
  - `scripts/import_reports.py` (заполнение `report_slices`),
  - API `/api/v1/analysis/reports/{id}/slices`.

Словари — одинаковые везде, чтобы «сделка из отчёта» и «строка среза»
совпадали бит-в-бит.

Сессии заданы в ЧАСАХ UTC и подписаны МСК (сдвиг +3):
  07:00–11:00 UTC = 10:00–14:00 МСК — утро,
  11:00–16:00 UTC = 14:00–19:00 МСК — день,
  16:00–21:00 UTC = 19:00–24:00 МСК — вечер.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

SESSIONS: tuple[tuple[str, int, int], ...] = (
    ("утро 10-14", 7, 11),
    ("день 14-19", 11, 16),
    ("вечер 19-24", 16, 21),
)
OUT_OF_SESSION = "вне сессий"

ADXTrendThreshold = 25.0
ADXRangeThreshold = 20.0

ER_LOW = 0.15
ER_MID = 0.30

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

DIMS: tuple[str, ...] = ("session", "regime_adx", "er", "ticker", "hour", "weekday")
DIM_LABELS: dict[str, str] = {
    "session": "Сессия (МСК)",
    "regime_adx": "Режим ADX (вход)",
    "er": "ER входа",
    "ticker": "Тикер",
    "hour": "Час (МСК)",
    "weekday": "День недели",
}


def _as_dt(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value


def session_bucket(ts: datetime | str | None) -> str:
    """Сессия входа по МСК. Подписи — МСК, границы — UTC."""
    dt = _as_dt(ts)
    if dt is None:
        return OUT_OF_SESSION
    hour = dt.astimezone(MSK).hour
    for name, a, b in _MSK_SESSION_BOUNDS:
        if a <= hour < b:
            return name
    return OUT_OF_SESSION


# Границы уже пересчитаны в МСК (те же интервалы, что SESSIONS в UTC).
_MSK_SESSION_BOUNDS: tuple[tuple[str, int, int], ...] = tuple(
    (name, a + 3, b + 3) for name, a, b in SESSIONS
)


def adx_regime_bucket(
    adx: float | None,
    trend_thr: float = ADXTrendThreshold,
    range_thr: float = ADXRangeThreshold,
) -> str:
    if adx is None:
        return "нет данных"
    if adx >= trend_thr:
        return "тренд"
    if adx >= range_thr:
        return "переход"
    return "диапазон"


def er_bucket(er: float | None) -> str:
    if er is None:
        return "нет данных"
    if er < ER_LOW:
        return f"<{ER_LOW}"
    if er < ER_MID:
        return f"{ER_LOW}-{ER_MID}"
    return f">={ER_MID}"


def hour_bucket(ts: datetime | str | None) -> str:
    dt = _as_dt(ts)
    return f"{dt.astimezone(MSK).hour:02d}" if dt else "?"


def weekday_bucket(ts: datetime | str | None) -> str:
    dt = _as_dt(ts)
    return WEEKDAYS[dt.astimezone(MSK).weekday()] if dt else "?"


def bucket_for(dim: str, trade: dict) -> str | None:
    """Значение бакета одного среза для одной сделки. None — срез неприменим."""
    if dim == "session":
        return session_bucket(trade.get("entry_time"))
    if dim == "regime_adx":
        return trade.get("regime_adx") or "нет данных"
    if dim == "er":
        return er_bucket(trade.get("er_in"))
    if dim == "ticker":
        return str(trade.get("ticker") or "?")
    if dim == "hour":
        return hour_bucket(trade.get("entry_time"))
    if dim == "weekday":
        return weekday_bucket(trade.get("entry_time"))
    return None


def compute_slices(
    trades: Iterable[dict],
    dims: Sequence[str] = DIMS,
    strategy_key: str = "strategy",
) -> list[dict]:
    """Группировка сделок по всем срезам.

    Возвращает строки вида {strategy, dim, bucket, trades, wins, gw, gl, net,
    gross_wins_n, gross_losses_n} — ровно колонки `report_slices`.
    `gw` — сумма положительных net, `gl` — сумма модулей отрицательных.
    """
    acc: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for trade in trades:
        strategy = str(trade.get(strategy_key) or "")
        for dim in dims:
            bucket = bucket_for(dim, trade)
            if bucket is None:
                continue
            cell = acc[(strategy, dim, bucket)]
            net = float(trade.get("net_pnl") or 0.0)
            cell[0] += 1
            cell[1] += 1 if net > 0 else 0
            cell[2] += net if net > 0 else 0.0
            cell[3] += -net if net < 0 else 0.0
    rows: list[dict] = []
    for (strategy, dim, bucket), (n, wins, gw, gl) in sorted(acc.items()):
        rows.append({
            "strategy": strategy,
            "dim": dim,
            "bucket": bucket,
            "trades": n,
            "wins": wins,
            "gw": round(gw, 6),
            "gl": round(gl, 6),
            "net": round(gw - gl, 6),
            "gross_wins_n": wins,
            "gross_losses_n": n - wins,
        })
    return rows


def enrich_session(trade: dict) -> dict:
    """Гарантирует наличие `session` (для сделок без неё — считаем на месте)."""
    if not trade.get("session"):
        trade["session"] = session_bucket(trade.get("entry_time"))
    return trade


def bucket_order(dim: str) -> list[str] | None:
    """Канонический порядок бакетов для выдачи (None — сортировка по имени)."""
    if dim == "session":
        return [s[0] for s in SESSIONS] + [OUT_OF_SESSION]
    if dim == "regime_adx":
        return ["тренд", "переход", "диапазон", "нет данных"]
    if dim == "er":
        return [f"<{ER_LOW}", f"{ER_LOW}-{ER_MID}", f">={ER_MID}", "нет данных"]
    if dim == "hour":
        return [f"{h:02d}" for h in range(24)]
    if dim == "weekday":
        return list(WEEKDAYS)
    return None


def ordered_buckets(dim: str, buckets: Iterable[str]) -> list[str]:
    """Канонический порядок бакетов.

    Для измерений с фиксированным набором (сессия/режим/ER/час/день) возвращается
    ВЕСЬ канонический список, даже если в данных пустые бакеты — иначе у разных
    стратегий колонки разъезжаются и «пусто» не отличить от «нет в выдаче».
    """
    canon = bucket_order(dim)
    if canon is None:
        return sorted(set(buckets))
    present = set(canon) | set(buckets)
    head = [b for b in canon if b in present]
    tail = sorted(present - set(head))
    return head + tail


__all__ = [
    "SESSIONS",
    "OUT_OF_SESSION",
    "DIMS",
    "DIM_LABELS",
    "MSK",
    "session_bucket",
    "adx_regime_bucket",
    "er_bucket",
    "hour_bucket",
    "weekday_bucket",
    "bucket_for",
    "compute_slices",
    "bucket_order",
    "ordered_buckets",
    "enrich_session",
]
