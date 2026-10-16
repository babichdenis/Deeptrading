"""Отбор бумаг по силе хода за N дней (move_select).

Назначение: выбрать N падающих или N растущих бумаг по настраиваемому окну
(1 день, 2 дня, ...) и метрике силы (суммарный ход / RSI / диапазон) — с полным
журналом решений, чтобы потом не искать, почему бумага попала или выпала.

Работает ТОЛЬКО в режиме реплея (feed=replay) и выключается через конфиг.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

from sqlalchemy import text

LogFn = Callable[[str, str], None]

DEFAULT_CONFIG_NAME = "universe-move-v1.json"
DEFAULT_LOG_DIR = "reports/universe_move"

SOURCE_INTERVAL = {"day": 24, "hour": 4}
DIRECTIONS = ("fall", "rise", "any")
METRICS = ("cum_ret", "rsi", "range")


@dataclass(frozen=True)
class MoveSelectConfig:
    enabled: bool = False
    replay_only: bool = True
    direction: str = "fall"
    n: int = 3
    lookback_days: int = 1
    metric: str = "cum_ret"
    tf: str = "day"
    rsi_period: int = 14
    shortable_only: bool = True
    min_turnover: float = 0.0
    exclude_tickers: tuple[str, ...] = ()
    log_dir: str = DEFAULT_LOG_DIR
    config_source: str = "defaults"

    def validate(self) -> "MoveSelectConfig":
        if self.direction not in DIRECTIONS:
            raise ValueError(f"move_select.direction: {self.direction!r} не из {DIRECTIONS}")
        if self.metric not in METRICS:
            raise ValueError(f"move_select.metric: {self.metric!r} не из {METRICS}")
        if self.tf not in SOURCE_INTERVAL:
            raise ValueError(f"move_select.tf: {self.tf!r} не из {tuple(SOURCE_INTERVAL)}")
        if self.n < 1:
            raise ValueError(f"move_select.n: {self.n} < 1")
        if self.lookback_days < 1:
            raise ValueError(f"move_select.lookback_days: {self.lookback_days} < 1")
        if self.rsi_period < 2:
            raise ValueError(f"move_select.rsi_period: {self.rsi_period} < 2")
        return self


def _coerce_exclude(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(t.strip().upper() for t in value.split(",") if t.strip())
    return tuple(str(t).strip().upper() for t in value if str(t).strip())


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if value is None:
            continue
        out[key] = value
    return out


def load_move_select_config(path: str | Path | None = None,
                            overrides: dict[str, Any] | None = None) -> MoveSelectConfig:
    base: dict[str, Any] = {}
    source = "defaults"
    cfg_path = Path(path) if path else _default_config_path()
    if cfg_path and cfg_path.exists():
        base = json.loads(cfg_path.read_text(encoding="utf-8"))
        source = str(cfg_path)
    merged = _merge(base, overrides or {})
    if "exclude_tickers" in merged:
        merged["exclude_tickers"] = _coerce_exclude(merged["exclude_tickers"])
    merged["config_source"] = source
    known = {f for f in MoveSelectConfig.__dataclass_fields__}
    return MoveSelectConfig(**{k: v for k, v in merged.items() if k in known}).validate()


def _default_config_path() -> Path | None:
    root = Path(__file__).resolve().parents[3]
    candidate = root / "configs" / "presets" / DEFAULT_CONFIG_NAME
    return candidate if candidate.exists() else None


class MoveSelectLogger:
    """Пишет каждое решение отбора в JSONL и дублирует короткую строку в лог бота."""

    def __init__(self, log_dir: str | Path, run_id: str, emit: LogFn | None = None):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{run_id}.jsonl"
        self.emit = emit
        self._fh = self.path.open("a", encoding="utf-8")

    def record(self, action: str, ticker: str, **fields: Any) -> None:
        row = {"ts": datetime.now(timezone.utc).isoformat(), "action": action,
               "ticker": ticker, **fields}
        self._fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def note(self, msg: str) -> None:
        self._fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                   "action": "note", "msg": msg}, ensure_ascii=False) + "\n")
        self._fh.flush()
        if self.emit:
            self.emit(msg, "info")

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def _rsi_last(rows: Sequence[tuple], period: int) -> float | None:
    if len(rows) < period + 1:
        return None
    from app.engine.indicatorhub import _rsi as hub_rsi
    from app.engine.models import Candle

    candles = [Candle(ts=r[0], open=float(r[1]), high=float(r[2]),
                      low=float(r[3]), close=float(r[4]), volume=0.0) for r in rows]
    values = hub_rsi(candles, period)
    for value in reversed(values):
        if value is not None:
            return float(value)
    return None


def _move_score(rows: Sequence[tuple], cfg: MoveSelectConfig) -> dict[str, float | None]:
    opens = [r[1] for r in rows]
    highs = [r[2] for r in rows]
    lows = [r[3] for r in rows]
    closes = [r[4] for r in rows]
    first_open = opens[0] if opens else None
    last_close = closes[-1] if closes else None
    cum_ret = None
    if first_open and last_close and first_open > 0:
        cum_ret = last_close / first_open - 1.0
    rng = None
    if highs and lows and last_close:
        top = max(highs)
        bottom = min(lows)
        if last_close > 0:
            rng = (top - bottom) / last_close
    return {"cum_ret": cum_ret, "rsi": _rsi_last(rows, cfg.rsi_period), "range": rng,
            "first_open": first_open, "last_close": last_close, "bars": float(len(rows))}


def _passes_sign(cum_ret: float | None, direction: str) -> bool:
    if direction == "any" or cum_ret is None:
        return True
    return cum_ret < 0 if direction == "fall" else cum_ret > 0


def _sort_key(item: dict[str, Any], metric: str, direction: str) -> float:
    value = item["score"]
    if value is None:
        return float("inf") if direction == "fall" else float("-inf")
    return value if direction != "rise" or metric == "range" else -value


async def _load_margin(db) -> dict[str, bool]:
    rows = await db.execute(text(
        "SELECT DISTINCT ON (figi) figi, short_enabled FROM instrument_margin "
        "ORDER BY figi, updated_at DESC"))
    return {r[0]: bool(r[1]) for r in rows.all()}


async def _load_candles(db, figis: Sequence[str], interval: int,
                        t0: datetime, t1: datetime) -> dict[str, list[tuple]]:
    if not figis:
        return {}
    rows = await db.execute(text(
        "SELECT figi, ts, open, high, low, close FROM candles "
        "WHERE interval = :iv AND ts >= :t0 AND ts < :t1 AND figi = ANY(:figis) "
        "ORDER BY figi, ts"), {"iv": interval, "t0": t0, "t1": t1, "figis": list(figis)})
    out: dict[str, list[tuple]] = {}
    for figi, ts, o, h, l, c in rows.all():
        out.setdefault(figi, []).append((ts, float(o), float(h), float(l), float(c)))
    return out


async def try_select_move_universe(db, figi_by_ticker: dict[str, str], *, feed: str,
                                   as_of: datetime, run_id: str | None = None,
                                   emit: LogFn | None = None,
                                   config_path: str | Path | None = None,
                                   enabled: bool | None = None) -> list[dict[str, Any]]:
    """Точка входа для runtime.

    Возвращает корзину из N падающих/растущих бумаг, либо ПУСТОЙ список, если
    режим выключен или это не реплей. Ничего не бросает: любая ошибка отбора
    превращается в лог и в пустой результат, чтобы не ронять старт бота.
    """
    try:
        cfg = load_move_select_config(config_path)
    except Exception as exc:
        if emit:
            emit(f"move_select: конфиг не прочитан ({exc}), отбор пропущен", "warn")
        return []
    if enabled is not None:
        from dataclasses import replace

        cfg = replace(cfg, enabled=bool(enabled))
    if not cfg.enabled:
        return []
    if cfg.replay_only and feed != "replay":
        if emit:
            emit(f"move_select: пропуск — режим {feed!r}, требуется реплей", "info")
        return []
    try:
        return await select_move_universe(db, figi_by_ticker, cfg, as_of, run_id, emit)
    except Exception as exc:
        if emit:
            emit(f"move_select: ошибка отбора ({exc}), берём legacy-юниверс", "warn")
        return []


async def select_move_universe(db, figi_by_ticker: dict[str, str], cfg: MoveSelectConfig,
                               as_of: datetime, run_id: str | None = None,
                               emit: LogFn | None = None) -> list[dict[str, Any]]:
    """Возвращает top-N бумаг по силе хода за окно, с полным журналом решений."""
    cfg.validate()
    if not cfg.enabled:
        return []
    run_id = run_id or as_of.strftime("%Y%m%dT%H%M%SZ")
    log = MoveSelectLogger(cfg.log_dir, run_id, emit)
    try:
        ticker_by_figi = {f: t for t, f in figi_by_ticker.items()}
        margin = await _load_margin(db)
        interval = SOURCE_INTERVAL[cfg.tf]
        t1 = as_of
        t0 = as_of - timedelta(days=cfg.lookback_days + 7)

        candidates: list[str] = []
        rejected: list[tuple[str, str]] = []
        for ticker, figi in sorted(figi_by_ticker.items()):
            up = ticker.strip().upper()
            if up in cfg.exclude_tickers:
                rejected.append((ticker, "excluded"))
                continue
            if cfg.shortable_only and not margin.get(figi, False):
                rejected.append((ticker, "not_shortable"))
                continue
            candidates.append(figi)

        log.note(f"move_select start as_of={as_of.isoformat()} window={cfg.lookback_days}d "
                 f"tf={cfg.tf} metric={cfg.metric} direction={cfg.direction} n={cfg.n} "
                 f"shortable_only={cfg.shortable_only} кандидатов={len(candidates)} "
                 f"отсеяно={len(rejected)}")
        for ticker, reason in rejected:
            log.record("rejected_prefilter", ticker, reason=reason)

        candles = await _load_candles(db, candidates, interval, t0, t1)
        scored: list[dict[str, Any]] = []
        for figi in candidates:
            rows = candles.get(figi, [])
            if not rows:
                log.record("rejected_nodata", ticker_by_figi.get(figi, figi), reason="no_candles")
                continue
            stats = _move_score(rows, cfg)
            cum_ret = stats["cum_ret"]
            ticker = ticker_by_figi.get(figi, figi)
            if not _passes_sign(cum_ret, cfg.direction):
                log.record("rejected_sign", ticker, reason=f"direction={cfg.direction}",
                           cum_ret=cum_ret)
                continue
            score = stats[cfg.metric]
            if score is None:
                log.record("rejected_noscore", ticker, reason=f"metric={cfg.metric} is None")
                continue
            scored.append({"figi": figi, "ticker": ticker, "score": float(score),
                           "cum_ret": cum_ret, "rsi": stats["rsi"], "range": stats["range"],
                           "bars": stats["bars"], "last_close": stats["last_close"]})

        scored.sort(key=lambda it: _sort_key(it, cfg.metric, cfg.direction))
        picked = scored[: cfg.n]
        dropped = scored[cfg.n: cfg.n + 5]

        for rank, item in enumerate(picked, start=1):
            log.record("picked", item["ticker"], rank=rank, score=item["score"],
                       metric=cfg.metric, cum_ret=item["cum_ret"], rsi=item["rsi"],
                       range=item["range"], bars=item["bars"], figi=item["figi"])
        for item in dropped:
            log.record("not_picked", item["ticker"], score=item["score"], cum_ret=item["cum_ret"])

        summary = ", ".join(f"{it['ticker']}={it['score']:.4f}" for it in picked) or "пусто"
        log.note(f"move_select итог: {len(picked)} бумаг · {summary} · "
                 f"журнал {log.path}")
        return [{"figi": it["figi"], "ticker": it["ticker"], "atr_pct": None,
                 "move_score": it["score"], "move_metric": cfg.metric,
                 "move_cum_ret": it["cum_ret"], "move_rsi": it["rsi"],
                 "move_range": it["range"], "move_lookback_days": cfg.lookback_days} for it in picked]
    finally:
        log.close()
