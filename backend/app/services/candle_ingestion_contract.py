"""Контракт записи свечей (B1, CODE-FIRST-R1-20261011): единая проверка и обработка пакета
на границе записи, чтобы ошибка загрузки не маскировалась под корректный набор.

Гарантии:
- Валидация каждой строки ДО записи: конечные/положительные OHLC,
  low <= open/close <= high, конечный неотрицательный volume, instrument/interval,
  UTC-aware ts и (опционально) принадлежность запрошенному окну.
  Нулевой volume и плоская свеча (o==h==l==c) — законны. Цена 1 сама по себе порчу
  не доказывает: это не жёсткий список тикеров/дат, а только структурные проверки.
- Дубли в пакете: одинаковые идемпотентны (дедуп), конфликтующие — явный отказ ДО записи
  (ConflictingDuplicateError), не случайный last-write и не сырая ошибка драйвера.
- Классификация против текущей БД: inserted / unchanged / corrected с проверяемым
  различием содержимого; повтор того же пакета не меняет экономическое содержимое.

Модуль чистый: без сети, без побочных эффектов при импорте; единственный ввод — dict-строки.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

_Q8 = Decimal("0.00000001")

# --- машинные коды причин отказа ---
NAN = "nan"                    # любое из OHLCV не конечно
BAD_PRICE = "bad_price"        # цена <= 0
BAD_VOLUME = "bad_volume"      # volume < 0
BAD_RANGE = "bad_range"        # high<low или нарушено containment low<=open/close<=high
NAIVE_TS = "naive_ts"          # ts без tzinfo
NOT_DATETIME = "not_datetime"  # ts не datetime
MISSING_FIGI = "missing_figi"
BAD_INTERVAL = "bad_interval"
MISSING_FIELD = "missing_field"
OUTSIDE_WINDOW = "outside_window"
WRONG_FIGI = "wrong_figi"          # строка не того инструмента (не принадлежит запросу)
WRONG_INTERVAL = "wrong_interval"  # строка другого интервала (не принадлежит запросу)


class CandleContractError(ValueError):
    """Базовый контрактный отказ записи свечей (до записи, без частичного эффекта)."""


@dataclass(frozen=True)
class RowRejection:
    index: int
    reason: str
    key: tuple | None = None


class CandleValidationError(CandleContractError):
    """В пакете есть невалидные свечи (весь пакет отклонён атомарно)."""

    def __init__(self, rejections: list[RowRejection]):
        self.rejections = list(rejections)
        first = self.rejections[0]
        super().__init__(
            f"candle validation failed: {len(self.rejections)} bad row(s); "
            f"first idx={first.index} reason={first.reason} key={first.key}"
        )


class ConflictingDuplicateError(CandleContractError):
    """Конфликтующие дубли (тот же natural key, разное содержимое) — отказ до записи."""

    def __init__(self, conflicts: list[tuple[tuple, int, int]]):
        self.conflicts = list(conflicts)
        key, i, j = self.conflicts[0]
        super().__init__(
            f"conflicting duplicates for key {key}: rows #{i} and #{j} differ; "
            f"refusing to pick a random last-write"
        )


@dataclass(frozen=True)
class UpsertResult:
    """Различие inserted/unchanged/corrected + сколько принято и реально записано."""

    accepted: int = 0   # уникальные валидные строки (после валидации и дедупа)
    inserted: int = 0
    unchanged: int = 0
    corrected: int = 0
    written: int = 0    # inserted + corrected (реально записано в БД)
    rejected: int = 0   # всегда 0 при успехе (невалид/конфликт -> исключение)


# ------------------------------------------------------------------ нормализация
def _q8(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(_Q8)


def _utc(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc)


def row_key(row: dict) -> tuple:
    """Natural key (figi, interval, ts-UTC)."""
    return (row.get("figi"), int(row.get("interval")), _utc(row["ts"]))


def _content(row: dict) -> tuple:
    """Экономическое содержимое (нормализовано к 8dp + int volume)."""
    return (_q8(row["open"]), _q8(row["high"]), _q8(row["low"]),
            _q8(row["close"]), int(row["volume"]))


def utc_ts(ts: datetime) -> datetime:
    """ts как tz-aware UTC."""
    return _utc(ts)


def same_content(row: dict, db_obj: Any) -> bool:
    """True, если dict-строка и ORM-строка БД экономически идентичны (8dp+volume)."""
    return _content(row) == (_q8(db_obj.open), _q8(db_obj.high), _q8(db_obj.low),
                             _q8(db_obj.close), int(db_obj.volume))


# ------------------------------------------------------------------ валидация
def validate_row(row: dict, *, window: tuple[datetime, datetime] | None = None,
                 index: int = 0, rel_eps: float = 1e-9) -> RowRejection | None:
    """None = валидна, иначе RowRejection с машинным кодом причины."""
    figi = row.get("figi")
    if not figi or not isinstance(figi, str):
        return RowRejection(index, MISSING_FIGI)
    try:
        interval = int(row.get("interval"))
    except (TypeError, ValueError):
        return RowRejection(index, BAD_INTERVAL)
    if interval < 1:
        return RowRejection(index, BAD_INTERVAL)
    ts = row.get("ts")
    if not isinstance(ts, datetime):
        return RowRejection(index, NOT_DATETIME, (figi, interval, None))
    if ts.tzinfo is None:
        return RowRejection(index, NAIVE_TS, (figi, interval, None))
    tsu = _utc(ts)
    key = (figi, interval, tsu)

    vals: dict[str, float] = {}
    for f in ("open", "high", "low", "close"):
        v = row.get(f)
        if v is None:
            return RowRejection(index, MISSING_FIELD, key)
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return RowRejection(index, MISSING_FIELD, key)
        if not math.isfinite(fv):
            return RowRejection(index, NAN, key)
        if fv <= 0:
            return RowRejection(index, BAD_PRICE, key)
        vals[f] = fv

    vol = row.get("volume")
    if vol is None:
        return RowRejection(index, MISSING_FIELD, key)
    try:
        vv = float(vol)
    except (TypeError, ValueError):
        return RowRejection(index, MISSING_FIELD, key)
    if not math.isfinite(vv):
        return RowRejection(index, NAN, key)
    if vv < 0:
        return RowRejection(index, BAD_VOLUME, key)

    o, h, lo, c = vals["open"], vals["high"], vals["low"], vals["close"]
    eps = rel_eps * max(1.0, abs(h), abs(lo))
    if h + eps < lo:  # high < low
        return RowRejection(index, BAD_RANGE, key)
    if h + eps < max(o, c) or lo - eps > min(o, c):  # containment
        return RowRejection(index, BAD_RANGE, key)

    if window is not None:
        start, end = window
        if not (_utc(start) <= tsu <= _utc(end)):
            return RowRejection(index, OUTSIDE_WINDOW, key)
    return None


def validate_rows(rows: Iterable[dict], *, window: tuple[datetime, datetime] | None = None
                  ) -> list[RowRejection]:
    """Все отказы по строкам (пусто = пакет валиден). Не бросает."""
    out: list[RowRejection] = []
    for i, r in enumerate(rows):
        rej = validate_row(r, window=window, index=i)
        if rej is not None:
            out.append(rej)
    return out


# ------------------------------------------------------------------ дубли в пакете
def split_duplicates(rows: list[dict]) -> tuple[list[dict], list[tuple[tuple, int, int]]]:
    """(deduped, conflicts). Одинаковые дубли отбрасываются (идемпотентно),
    конфликтующие (тот же ключ, иное содержимое) собираются как (key, first_i, second_i)."""
    seen: dict[tuple, tuple[int, tuple]] = {}  # key -> (first_index, first_content)
    deduped: list[dict] = []
    conflicts: list[tuple[tuple, int, int]] = []
    for i, r in enumerate(rows):
        k = row_key(r)
        if k not in seen:
            seen[k] = (i, _content(r))
            deduped.append(r)
            continue
        j, first_content = seen[k]
        if first_content == _content(r):
            continue  # идентичный дубль -> пропуск (идемпотентно)
        conflicts.append((k, j, i))
    return deduped, conflicts


# ------------------------------------------------------------------ итог пакета
def assert_valid_batch(rows: list[dict], *, window: tuple[datetime, datetime] | None = None
                       ) -> list[dict]:
    """Полная проверка пакета: валидация всех строк + обработка дублей.

    Возвращает deduped валидные строки. Бросает CandleValidationError /
    ConflictingDuplicateError ДО любой записи (атомарный отказ пакета).
    """
    rejections = validate_rows(rows, window=window)
    if rejections:
        raise CandleValidationError(rejections)
    deduped, conflicts = split_duplicates(list(rows))
    if conflicts:
        raise ConflictingDuplicateError(conflicts)
    return deduped


def assert_request_scope(rows: list[dict], *, figi: str, interval: int,
                        window: tuple[datetime, datetime] | None = None) -> list[dict]:
    """Проверка принадлежности пакета фактическому запросу (до записи/публикации).

    Отклоняет строки чужого инструмента (figi), чужого интервала и — отдельно —
    структурно невалидные/вне окна. Возвращает тот же список (после проверок).
    Это защита от публикации чужой свечи под данным инструментом.
    """
    for i, r in enumerate(rows):
        if r.get("figi") != figi:
            raise CandleValidationError([RowRejection(i, WRONG_FIGI)])
        try:
            iv = int(r.get("interval"))
        except (TypeError, ValueError):
            raise CandleValidationError([RowRejection(i, BAD_INTERVAL)])
        if iv != interval:
            raise CandleValidationError([RowRejection(i, WRONG_INTERVAL)])
        rej = validate_row(r, window=window, index=i)
        if rej is not None:
            raise CandleValidationError([rej])
    return rows
