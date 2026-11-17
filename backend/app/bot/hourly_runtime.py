"""Часовой контекст: загрузка через хаб, одна сборка на час, журнал и восстановление.

Общий расчёт живёт в ``app.engine.hourly_context`` и принадлежит архитектору.
Здесь только проводка в рабочий цикл: откуда берутся минуты, как не собрать дважды,
что писать в журнал и что показывать после перезапуска.

Правила, которые держит этот модуль:

* минуты читаются только через адаптер хаба, прямого SQL по свечам здесь нет;
* на один ``as_of`` — одна сборка; одновременные запросы ждут её, а не начинают свою;
* неполный снимок повторяется ограниченно: число попыток и пауза заданы явно;
* ``available_at`` — фактический момент готовности, снимок не публикуется задним числом;
* результат, пришедший после конца своего часа, не публикуется вовсе;
* журнал пишется до публикации: сбой записи не оставляет годного, но неучтённого снимка;
* восстановление после перезапуска собирает настоящий снимок и отбрасывает несовместимый;
* каждая запрошенная бумага остаётся в признаках, даже если минут по ней нет;
* аварийный слой живёт отдельно и часовым пересчётом не сбрасывается;
* допуск входа требует явного «известно» и по самой бумаге, и по индексу.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Sequence

from app.engine.hourly_context import (
    HOUR,
    HourlyContext,
    HourlyFeature,
    build_hourly_context,
    hour_boundary,
)

log = logging.getLogger(__name__)

MINUTE_TF = "1min"
LOOKBACK_MINUTES = 61
SOURCE_OK = "ok"
SOURCE_PARTIAL = "partial"
SOURCE_FAILED = "failed"

# Неполный снимок повторяется не бесконечно: свежие минуты приходят с задержкой,
# но опоздавшая попытка не должна превращаться в очередь повторов на весь час.
MAX_ATTEMPTS_PER_HOUR = 4
RETRY_INTERVAL_SECONDS = 90.0

VALID_STATUSES = frozenset(
    {"KNOWN", "ENDPOINT_UNKNOWN", "INSUFFICIENT_OBSERVATIONS", "MARKET_UNKNOWN"}
)


class HourlySourceError(RuntimeError):
    """Источник минут не отдал данные; это не повод выдумывать контекст."""


@dataclass(frozen=True)
class HourlySnapshot:
    """Публикуемый снимок часа: расчёт плюс честное описание источника."""

    context: HourlyContext
    source_status: str
    counts: Mapping[str, int]
    failed_figis: tuple[str, ...]
    config_hash: str
    code_hash: str

    @property
    def as_of(self) -> datetime:
        return self.context.as_of

    @property
    def available_at(self) -> datetime:
        return self.context.available_at

    @property
    def expires_at(self) -> datetime:
        return self.context.expires_at

    def usable_at(self, at: datetime) -> bool:
        return self.context.usable_at(at)

    def feature(self, figi: str) -> HourlyFeature | None:
        return self.context.features.get(figi)

    def to_record(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "available_at": self.available_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "market_id": self.context.market_id,
            "input_hash": self.context.input_hash,
            "config_hash": self.config_hash,
            "code_hash": self.code_hash,
            "source_status": self.source_status,
            "counts": dict(self.counts),
            "failed_figis": list(self.failed_figis),
            "features": {
                figi: {
                    "status": f.status,
                    "return_60m": f.return_60m,
                    "relative_return_60m": f.relative_return_60m,
                    "observed_minutes": f.observed_minutes,
                    "last_closed_at": (
                        f.last_closed_at.isoformat() if f.last_closed_at else None
                    ),
                }
                for figi, f in sorted(self.context.features.items())
            },
        }


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        # Пустой хеш скрыл бы подмену: недоступность файла называется прямо.
        log.warning("hourly: не прочитан %s для отпечатка: %s", path.name, exc)
        return f"unavailable:{exc.__class__.__name__}"


def code_fingerprint() -> str:
    """Отпечаток правил расчёта И проводки: смена любого меняет журнал."""
    here = Path(__file__).resolve()
    digest = hashlib.sha256()
    for name in ("hourly_context.py", "hourly_runtime.py"):
        target = (
            here.with_name(name)
            if name == "hourly_runtime.py"
            else here.parent.parent / "engine" / name
        )
        digest.update(_sha256_file(target).encode())
    return digest.hexdigest()


def config_fingerprint(*, market_id: str, minimum_observations: int, lookback: int) -> str:
    payload = {
        "market_id": market_id,
        "minimum_observations": minimum_observations,
        "lookback": lookback,
        "tf": MINUTE_TF,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class MinuteWindow:
    """Закрытые минуты по одной бумаге и счётчики загрузки."""

    figi: str
    bars: tuple
    rows: int
    error: str | None = None


class HubMinuteLoader:
    """Адаптер загрузки минут через хаб.

    ``read_range`` на orchestrator — единственный путь; никакого ``SELECT`` по
    ``candles`` отсюда не идёт. ``as_of`` передаётся границей часа, поэтому
    формирующийся бакет не виден.
    """

    def __init__(self, orchestrator=None):
        self._orchestrator = orchestrator

    def _reader(self):
        if self._orchestrator is not None:
            return self._orchestrator
        from app.services.candle_hub_orchestrator import CandleHubOrchestrator

        self._orchestrator = CandleHubOrchestrator()
        return self._orchestrator

    async def load(
        self,
        db,
        figis: Iterable[str],
        *,
        as_of: datetime,
        lookback_minutes: int = LOOKBACK_MINUTES,
    ) -> list[MinuteWindow]:
        boundary = hour_boundary(as_of)
        start = boundary - timedelta(minutes=lookback_minutes)
        reader = self._reader()
        out: list[MinuteWindow] = []
        for figi in sorted(set(figis)):
            try:
                result = await reader.read_range(
                    db, figi, MINUTE_TF, start, boundary, as_of=boundary
                )
                bars = tuple(getattr(result, "candles", ()) or ())
            except Exception as exc:  # источник сломался именно по этой бумаге
                log.warning("hourly: минуты %s не прочитаны: %s", figi, exc)
                out.append(MinuteWindow(figi, (), 0, str(exc)))
                continue
            out.append(MinuteWindow(figi, bars, len(bars)))
        return out


class HourlyJournal:
    """Журнал снимков: append-only строки JSON, чтение — с проверкой целостности."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None

    def append(self, record: Mapping[str, object]) -> None:
        """Запись обязана удаться: вызывающий не публикует неучтённый снимок."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(record), sort_keys=True, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def last_record(self, *, now: datetime) -> dict | None:
        """Последняя годная запись, чей час уже начался и ещё не истёк."""
        if self.path is None or not self.path.exists():
            return None
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("hourly: журнал не прочитан: %s", exc)
            return None
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                log.warning("hourly: строка журнала повреждена, пропускаю")
                continue
            try:
                as_of = datetime.fromisoformat(record["as_of"])
                expires_at = datetime.fromisoformat(record["expires_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if record.get("source_status") == SOURCE_FAILED:
                continue
            # запись должна принадлежать уже начавшемуся и ещё не истёкшему часу
            if as_of <= now < expires_at:
                return record
        return None


@dataclass
class EmergencyOverlay:
    """Немедленные ограничения: часовой пересчёт их не сбрасывает и не создаёт."""

    blocks: dict = field(default_factory=dict)

    def set(self, key: str, *, reason: str, at: datetime) -> None:
        self.blocks[key] = {"reason": reason, "set_at": at.isoformat()}

    def clear(self, key: str) -> None:
        self.blocks.pop(key, None)

    def is_blocked(self, key: str) -> bool:
        return key in self.blocks

    def snapshot(self) -> Mapping[str, dict]:
        return MappingProxyType(dict(self.blocks))


@dataclass(frozen=True)
class EntryVerdict:
    """Результат допуска: не просто «да/нет», а причина для журнала."""

    allowed: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


class HourlyContextService:
    """Одна сборка на час; публикация целого снимка; прошлое не продлевается само."""

    def __init__(
        self,
        *,
        market_id: str,
        lookback_minutes: int = LOOKBACK_MINUTES,
        minimum_observations: int = 48,
        journal: HourlyJournal | None = None,
        loader: HubMinuteLoader | None = None,
        clock: Callable[[], datetime] | None = None,
        max_attempts: int = MAX_ATTEMPTS_PER_HOUR,
        retry_interval: float = RETRY_INTERVAL_SECONDS,
    ):
        self.market_id = market_id
        self.lookback_minutes = lookback_minutes
        self.minimum_observations = minimum_observations
        self.journal = journal or HourlyJournal(None)
        self.loader = loader or HubMinuteLoader()
        self.emergency = EmergencyOverlay()
        self.max_attempts = max(1, int(max_attempts))
        self.retry_interval = max(0.0, float(retry_interval))
        # Часы цикла, а не системные: исторические решения считаются по времени
        # источника, иначе прогон на истории видит «сейчас» вместо своего момента.
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._snapshot: HourlySnapshot | None = None
        self._lock: asyncio.Lock | None = None
        self._last_error: str | None = None
        self._attempts: dict[str, int] = {}
        self._last_attempt: dict[str, datetime] = {}
        self.config_hash = config_fingerprint(
            market_id=market_id,
            minimum_observations=minimum_observations,
            lookback=lookback_minutes,
        )
        self.code_hash = code_fingerprint()

    # ── состояние ────────────────────────────────────────────────────────────

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def current(self, *, now: datetime | None = None) -> HourlySnapshot | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        if now is not None and not snapshot.usable_at(now):
            return None
        return snapshot

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def status(self, *, now: datetime) -> dict:
        """Статус различает загруженный источник и контекст, годный для решения."""
        snapshot = self.current(now=now)
        base = {
            "config_hash": self.config_hash,
            "code_hash": self.code_hash,
            "market_id": self.market_id,
            "emergency_blocks": dict(self.emergency.snapshot()),
        }
        if snapshot is None:
            return {
                **base,
                "state": "UNKNOWN",
                "decision_ready": False,
                "reason": self._last_error or "часовой контекст ещё не собран",
                "as_of": None,
                "available_at": None,
                "expires_at": None,
                "input_hash": None,
                "covered": 0,
                "known": 0,
                "market_status": None,
            }
        unknown = [
            f for f, feat in snapshot.context.features.items() if feat.status != "KNOWN"
        ]
        market = snapshot.feature(self.market_id)
        market_known = market is not None and market.status == "KNOWN"
        known = len(snapshot.context.features) - len(unknown)
        if snapshot.source_status != SOURCE_OK:
            state = snapshot.source_status.upper()
        elif not market_known or known == 0:
            # источник отдал данные, но контекст для решения негоден: состояние
            # не должно выглядеть как KNOWN только из-за успешной загрузки
            state = "UNKNOWN"
        else:
            state = "KNOWN"
        return {
            **base,
            "state": state,
            "decision_ready": market_known and known > 0,
            "reason": self._last_error,
            "as_of": snapshot.as_of.isoformat(),
            "available_at": snapshot.available_at.isoformat(),
            "expires_at": snapshot.expires_at.isoformat(),
            "input_hash": snapshot.context.input_hash,
            "covered": len(snapshot.context.features),
            "known": known,
            "unknown_figis": unknown,
            "market_status": market.status if market else None,
            "source_status": snapshot.source_status,
            "failed_figis": list(snapshot.failed_figis),
        }

    # ── восстановление ───────────────────────────────────────────────────────

    def restore(self, *, now: datetime) -> HourlySnapshot | None:
        """После перезапуска поднимается только совместимый неистёкший снимок.

        Возвращается готовый снимок, а не запись журнала: иначе снаружи выглядит
        как успех, а рабочий цикл видит пустое состояние.
        """
        record = self.journal.last_record(now=now)
        if record is None:
            return None
        snapshot = self._snapshot_from_record(record, now=now)
        if snapshot is None:
            return None
        self._snapshot = snapshot
        self._last_error = None
        log.info(
            "hourly: поднят снимок as_of=%s из журнала (доступен до %s)",
            snapshot.as_of.isoformat(),
            snapshot.expires_at.isoformat(),
        )
        return snapshot

    def _snapshot_from_record(self, record: Mapping, *, now: datetime) -> HourlySnapshot | None:
        def reject(reason: str) -> None:
            log.warning("hourly: снимок из журнала отброшен: %s", reason)
            self._last_error = f"снимок в журнале несовместим: {reason}"

        try:
            as_of = datetime.fromisoformat(record["as_of"])
            available_at = datetime.fromisoformat(record["available_at"])
            expires_at = datetime.fromisoformat(record["expires_at"])
            input_hash = str(record["input_hash"])
            config_hash = str(record["config_hash"])
            code_hash = str(record["code_hash"])
            market_id = str(record["market_id"])
            source_status = str(record.get("source_status", SOURCE_OK))
            features_raw = record["features"]
            counts = dict(record.get("counts") or {})
            failed = tuple(record.get("failed_figis") or ())
        except (KeyError, TypeError, ValueError) as exc:
            reject(f"неполная запись ({exc.__class__.__name__})")
            return None

        if config_hash != self.config_hash:
            reject("другой отпечаток конфигурации")
            return None
        if code_hash != self.code_hash:
            reject("другой отпечаток кода")
            return None
        if market_id != self.market_id:
            reject("другой индекс рынка")
            return None
        if expires_at != as_of + HOUR:
            reject("истечение не равно границе часа")
            return None
        if not (as_of <= now < expires_at):
            reject("час записи не текущий")
            return None
        if not isinstance(features_raw, Mapping) or not features_raw:
            reject("в записи нет признаков")
            return None

        features: dict[str, HourlyFeature] = {}
        for figi, data in features_raw.items():
            if not isinstance(data, Mapping):
                reject("признак не является записью")
                return None
            status = str(data.get("status"))
            if status not in VALID_STATUSES:
                reject(f"неизвестный статус бумаги «{status}»")
                return None
            last_closed = data.get("last_closed_at")
            try:
                last_closed_at = datetime.fromisoformat(last_closed) if last_closed else None
            except (TypeError, ValueError):
                reject("нечитаемое время закрытия")
                return None
            observed = data.get("observed_minutes")
            if not isinstance(observed, int) or observed < 0:
                reject("нечитаемое число наблюдений")
                return None
            features[str(figi)] = HourlyFeature(
                status=status,
                return_60m=data.get("return_60m"),
                relative_return_60m=data.get("relative_return_60m"),
                observed_minutes=observed,
                last_closed_at=last_closed_at,
            )

        context = HourlyContext(
            as_of=as_of,
            available_at=available_at,
            expires_at=expires_at,
            market_id=market_id,
            features=MappingProxyType(features),
            input_hash=input_hash,
        )
        return HourlySnapshot(
            context=context,
            source_status=source_status,
            counts=MappingProxyType({str(k): int(v) for k, v in counts.items()}),
            failed_figis=failed,
            config_hash=config_hash,
            code_hash=code_hash,
        )

    # ── сборка ───────────────────────────────────────────────────────────────

    async def ensure(
        self,
        *,
        db,
        now: datetime,
        figis: Sequence[str] | None = None,
    ) -> HourlySnapshot | None:
        """Снимок на текущий час: готовый отдаётся сразу, неполный повторяется."""
        boundary = hour_boundary(now)
        ready = self.current(now=now)
        if ready is not None and ready.as_of == boundary and self._is_complete(ready):
            return ready
        if ready is not None and ready.as_of == boundary and not self._retry_allowed(boundary, now):
            return ready
        async with self._get_lock():
            ready = self.current(now=now)
            if ready is not None and ready.as_of == boundary:
                if self._is_complete(ready) or not self._retry_allowed(boundary, now):
                    return ready
            return await self._refresh(db=db, boundary=boundary, figis=figis, now=now)

    @staticmethod
    def _is_complete(snapshot: HourlySnapshot) -> bool:
        return snapshot.source_status == SOURCE_OK and not snapshot.failed_figis

    def _retry_allowed(self, boundary: datetime, now: datetime) -> bool:
        key = boundary.isoformat()
        if self._attempts.get(key, 0) >= self.max_attempts:
            return False
        last = self._last_attempt.get(key)
        if last is not None and (now - last).total_seconds() < self.retry_interval:
            return False
        return True

    async def _refresh(
        self,
        *,
        db,
        boundary: datetime,
        figis: Sequence[str] | None,
        now: datetime,
    ) -> HourlySnapshot | None:
        key = boundary.isoformat()
        self._attempts[key] = self._attempts.get(key, 0) + 1
        self._last_attempt[key] = now

        # старые ключи не нужны: час прошёл — счётчик неактуален
        for stale in [k for k in self._attempts if k != key]:
            self._attempts.pop(stale, None)
            self._last_attempt.pop(stale, None)

        universe = tuple(sorted(set(str(f) for f in (figis or ()))))
        if not universe:
            self._last_error = "не передан список бумаг для часового среза"
            self._append_failure(boundary, {}, (), self._last_error)
            log.warning("hourly: %s", self._last_error)
            return None
        if self.market_id not in universe:
            universe = tuple(sorted(set(universe) | {self.market_id}))

        windows = await self.loader.load(
            db, universe, as_of=boundary, lookback_minutes=self.lookback_minutes
        )
        counts = {w.figi: w.rows for w in windows}
        failed = tuple(w.figi for w in windows if w.error)
        # Каждая запрошенная бумага остаётся во входе, даже пустая: иначе она
        # исчезает из признаков и счётчики покрытия завышают реальность.
        series = {w.figi: list(w.bars) for w in windows}

        if not any(series.values()):
            self._last_error = "ни одна бумага не отдала минуты за последний час"
            self._append_failure(boundary, counts, failed, self._last_error)
            log.warning("hourly: %s", self._last_error)
            return None

        available_at = self._ready_at(boundary, now)
        if available_at is None:
            reason = "сборка завершилась после конца своего часа: снимок не публикуется"
            self._last_error = reason
            self._append_failure(boundary, counts, failed, reason)
            log.warning("hourly: %s", reason)
            return None

        context = build_hourly_context(
            series,
            market_id=self.market_id,
            as_of=boundary,
            available_at=available_at,
            minimum_observations=self.minimum_observations,
        )
        status = (
            SOURCE_FAILED if failed and len(failed) == len(windows)
            else SOURCE_PARTIAL if failed else SOURCE_OK
        )
        snapshot = HourlySnapshot(
            context=context,
            source_status=status,
            counts=MappingProxyType(dict(counts)),
            failed_figis=failed,
            config_hash=self.config_hash,
            code_hash=self.code_hash,
        )
        record = snapshot.to_record()
        record["event"] = "refresh"
        record["attempt"] = self._attempts[key]
        # Журнал раньше публикации: иначе сбой записи оставит пригодный снимок,
        # о котором никто не узнает после перезапуска.
        try:
            self.journal.append(record)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"журнал часового контекста не записан: {exc}"
            log.error("hourly: %s", self._last_error)
            return None
        self._snapshot = snapshot
        self._last_error = None if not failed else f"нет минут у {len(failed)} бумаг"
        log.info(
            "hourly: снимок as_of=%s, бумаг %d, без минут %d, известных %d, попытка %d",
            boundary.isoformat(),
            len(windows),
            len(failed),
            sum(1 for f in context.features.values() if f.status == "KNOWN"),
            self._attempts[key],
        )
        return snapshot

    def _ready_at(self, boundary: datetime, now: datetime) -> datetime | None:
        """Фактический момент готовности; None, если час уже закрылся.

        Готовность — это когда снимок действительно собран, а не когда его
        запросили: если часы цикла успели выйти за границу часа, публиковать
        такой снимок задним числом нельзя.
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Часы цикла должны быть aware")
        clock = self._clock()
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError("Часы цикла должны быть aware")
        ready = max(now, clock)
        if not (boundary <= ready < boundary + HOUR):
            return None
        return ready

    def _append_failure(self, boundary, counts, failed, reason) -> None:
        try:
            self.journal.append(
                {
                    "event": "refresh_failed",
                    "as_of": boundary.isoformat(),
                    "reason": reason,
                    "counts": dict(counts),
                    "failed_figis": list(failed),
                    "attempt": self._attempts.get(boundary.isoformat(), 0),
                    "config_hash": self.config_hash,
                    "code_hash": self.code_hash,
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.error("hourly: не записан сбой пересчёта: %s", exc)


def build_series_from_windows(windows: Iterable[MinuteWindow]) -> dict[str, list]:
    """Вход для расчёта: бумага без минут остаётся с пустым рядом."""
    return {w.figi: list(w.bars) for w in windows}


def entry_verdict(
    *,
    overlay: EmergencyOverlay,
    snapshot: HourlySnapshot | None,
    now: datetime,
    target_figi: str | None = None,
    market_id: str | None = None,
    block_key: str | None = None,
) -> EntryVerdict:
    """Допуск новой позиции: нужны «известно» по самой бумаге и по индексу.

    Пропуск по любой из сторон — отказ, а не нейтральная подстановка: пустая
    доходность не должна выглядеть как «рынок спокоен». Вызов без явной бумаги
    или индекса тоже отказ: иначе проверка «на всякий случай» пропускает вход.
    """
    if block_key is not None and overlay.is_blocked(block_key):
        return EntryVerdict(False, f"аварийный запрет: {block_key}")
    if snapshot is None:
        return EntryVerdict(False, "часовой контекст не собран")
    if not snapshot.usable_at(now):
        return EntryVerdict(False, "часовой контекст устарел")
    if not target_figi:
        return EntryVerdict(False, "не указана целевая бумага")
    if not market_id:
        return EntryVerdict(False, "не указан индекс рынка")
    market = snapshot.feature(market_id)
    if market is None:
        return EntryVerdict(False, "индекс отсутствует в часовом срезе")
    if market.status != "KNOWN":
        return EntryVerdict(False, f"рынок: {market.status}")
    target = snapshot.feature(target_figi)
    if target is None:
        return EntryVerdict(False, "бумаги нет в часовом срезе")
    if target.status != "KNOWN":
        return EntryVerdict(False, f"бумага: {target.status}")
    if target.last_closed_at != snapshot.as_of:
        return EntryVerdict(False, "нет закрытия ровно на границе часа")
    if target.relative_return_60m is None:
        return EntryVerdict(False, "относительная доходность не посчитана")
    return EntryVerdict(True, None)


def permits_new_entry(
    *,
    overlay: EmergencyOverlay,
    snapshot: HourlySnapshot | None,
    now: datetime,
    target_figi: str | None = None,
    market_id: str | None = None,
    block_key: str | None = None,
) -> bool:
    """Короткая форма допуска для вызовов, которым нужен только факт."""
    return entry_verdict(
        overlay=overlay,
        snapshot=snapshot,
        now=now,
        target_figi=target_figi,
        market_id=market_id,
        block_key=block_key,
    ).allowed


def permits_reduction(*, overlay: EmergencyOverlay, now: datetime) -> bool:
    """Сокращение и выход не зависят от часового контекста и аварийных запретов.

    Функция существует как явный контракт: даже при неизвестном контексте,
    провале загрузки или активном запрете выход из позиции остаётся доступен.
    """
    hour_boundary(now)
    return True
