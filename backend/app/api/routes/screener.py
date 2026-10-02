"""screener.py — правый сайдбар: все акции из БД + цена/оборот/волатильность из TQBR quotes.

Данные MOEX ISS (0.7с на весь рынок) кэшируются на CACHE_TTL секунд.
Волатильность = RNG% = (HIGH−LOW)/WAPRICE за день. Оборот = VALTODAY (₽).
"""
import asyncio
import time
import urllib.request
import json
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal, get_db
from app.models.instrument import Instrument

router = APIRouter(prefix="/api/v1/screener", tags=["screener"])

_MSK = ZoneInfo("Europe/Moscow")

TQBR_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR"
    "/securities.json?iss.only=marketdata"
)
CACHE_TTL = 30  # сек — MOEX ISS обновляет TQBR quotes каждые 2-5с в сессию
STALE_TTL = 600  # сек — старый кэш отдаём как fallback при сбое MOEX
MICRO_CACHE_TTL = 2
_cached: dict = {"ts": 0.0, "quotes": None, "breadth": {}}
_refresh_lock = asyncio.Lock()
_refresh_task: asyncio.Task | None = None

# журнал последних действий с eligible (add/remove) — показывается в сайдбаре
_carousel_log: deque[dict] = deque(maxlen=40)

# сильные ссылки на фоновые докачки свечей: create_task без ссылки может быть
# собран GC до завершения («non-checked-in connection» в логах uvicorn)
_BG_DOWNLOADS: set[asyncio.Task] = set()


def _carousel_log_add(action: str, ticker: str, msg: str) -> None:
    _carousel_log.appendleft({
        "ts": datetime.now(timezone.utc).astimezone(_MSK).strftime("%H:%M:%S"),
        "action": action,
        "ticker": ticker,
        "msg": msg,
    })


def _fetch_quotes_uncached() -> dict:
    data = json.loads(urllib.request.urlopen(TQBR_URL, timeout=20).read())
    md = data["marketdata"]
    cols = {n: i for i, n in enumerate(md["columns"])}

    def _f(r, k):
        try:
            return float(r[cols[k]])
        except (TypeError, ValueError):
            return None

    out = {}
    for r in md["data"]:
        secid = r[cols["SECID"]]
        if not secid:
            continue
        wap = _f(r, "WAPRICE")
        last = _f(r, "LAST")
        hi = _f(r, "HIGH")
        lo = _f(r, "LOW")
        val = _f(r, "VALTODAY")
        chg = _f(r, "LASTTOPREVPRICE")
        price = last if last and last > 0 else wap
        rng = ((hi - lo) / wap * 100) if (wap and wap > 0 and hi and lo and hi > 0) else 0.0
        out[secid] = {
            "ticker": secid,
            "price": price or 0.0,
            "turnover": val or 0.0,
            "rng_pct": rng,
            "chg_pct": chg,
        }
    return out


def _breadth_from_quotes(quotes: dict) -> dict:
    """Breadth по обороту TQBR: доля ₽-оборота растущих vs падающих бумаг."""
    up_rub = down_rub = 0.0
    up_n = down_n = flat_n = 0
    for q in (quotes or {}).values():
        chg = q.get("chg_pct")
        val = float(q.get("turnover") or 0.0)
        if chg is None or val < 1_000_000:
            continue
        if chg > 0.05:
            up_rub += val
            up_n += 1
        elif chg < -0.05:
            down_rub += val
            down_n += 1
        else:
            flat_n += 1
    total = up_rub + down_rub
    return {
        "up_rub": round(up_rub), "down_rub": round(down_rub),
        "up_pct": round(up_rub / total * 100, 1) if total else 50.0,
        "up_n": up_n, "down_n": down_n, "flat_n": flat_n,
    }


def market_breadth() -> dict:
    """Кэшированный breadth (без сетевых вызовов; пусто, если данных нет)."""
    if _cached["quotes"] is None or (time.monotonic() - _cached["ts"]) > STALE_TTL:
        return {}
    return _cached.get("breadth") or {}


def _schedule_refresh() -> None:
    """Фоновая перезарядка кэша quotes (не блокирующая in-flight запрос)."""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    # Вызов из потока без event loop (напр. asyncio.to_thread) — планировать некуда.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    async def _worker() -> None:
        async with _refresh_lock:
            try:
                quotes = await asyncio.to_thread(_fetch_quotes_uncached)
                _cached.update(ts=time.monotonic(), quotes=quotes,
                               breadth=_breadth_from_quotes(quotes))
            except Exception:
                pass
    _refresh_task = loop.create_task(_worker())


async def fetch_tqbr_market_async() -> dict:
    """Асинхронный аналог fetch_tqbr_market для вызова из корутин (vol_carousel).

    НЕ планирует фоновую задачу из треда (иначе RuntimeError: no running event
    loop): при просрочке кэша обновляет его здесь же через to_thread.
    """
    now = time.monotonic()
    if _cached["quotes"] is not None and now - _cached["ts"] < CACHE_TTL:
        return _cached["quotes"]
    try:
        quotes = await asyncio.to_thread(_fetch_quotes_uncached)
        _cached.update(ts=time.monotonic(), quotes=quotes,
                       breadth=_breadth_from_quotes(quotes))
        return quotes
    except Exception:
        return _cached["quotes"] or {}


def fetch_tqbr_market() -> dict:
    """Кэшированные TQBR quotes. Не блокирует запрос: при просрочке кэша
    обновление запускается в фоне, наружу отдаётся старое значение
    (stale). При отсутствии кэша вообще — fallback на пустой dict,
    никогда не бросает исключение."""
    now = time.monotonic()
    if _cached["quotes"] is not None and now - _cached["ts"] < CACHE_TTL:
        return _cached["quotes"]
    if _cached["quotes"] is not None and now - _cached["ts"] < STALE_TTL:
        _schedule_refresh()
        return _cached["quotes"]
    try:
        quotes = _fetch_quotes_uncached()
        _cached.update(ts=now, quotes=quotes, breadth=_breadth_from_quotes(quotes))
        return quotes
    except Exception:
        return {}


_COUNTS_TTL = 300  # сек — счётчики нужны только для.pending (скачать/не хватает)
_counts_cache: dict = {"ts": 0.0, "keys": frozenset(), "data": {}}


async def _candle_counts(db: AsyncSession, figis: list[str]) -> dict[str, int]:
    """Сколько 1m-свечей в БД по каждому FIGI, с кэшем на _COUNTS_TTL.

    count(*) по interval=1 — это 15+ млн строк и 9-11 секунд, а poll карусели
    идёт каждые 15 с и без кэша съедал всю БД целиком (в т.ч. при остановленном
    боте). Кэш ключуется набором FIGI: вселенная меняется редко, а при новом
    FIGI считаем заново, иначе в UI «нужно скачать» для новых тикеров.
    При ошибке отдаём прошлое значение (никогда не бросаем наружу)."""
    keys = frozenset(figis)
    now = time.monotonic()
    cached = _counts_cache["data"]
    if keys and keys == _counts_cache["keys"] and now - _counts_cache["ts"] < _COUNTS_TTL:
        return cached
    try:
        rows = (await db.execute(
            text("SELECT figi, count(*) FROM candles "
                 "WHERE figi = ANY(:fs) AND interval = 1 GROUP BY figi"),
            {"fs": list(figis)},
        )).all()
    except Exception:
        return cached
    data = {f: int(c) for f, c in rows}
    _counts_cache.update(ts=now, keys=keys, data=data)
    return data


async def _carousel_status(db: AsyncSession) -> dict:
    """Статус карусели бота, считанный из базы + рантайма. Работает всегда."""
    eligible_rows = (await db.execute(
        text("SELECT figi, ticker, lot_size FROM universe WHERE eligible_tier = 'eligible'")
    )).all()

    # активные тикеры в карусели рантайма (когда бот жив)
    active_set: set[str] = set()
    bot_running = False
    try:
        from app.bot.runtime import runtime
        active_set = {u.get("figi") for u in runtime.universe}
        bot_running = runtime.running
    except Exception:
        pass

    # Счётчики свечей нужны ТОЛЬКО для pending (неактивных) тикеров. Когда бот
    # идёт по реплею, активны все — тогда запрос не выполняется вовсе.
    pending_figis = [f for f, _t, _lot in eligible_rows if f not in active_set]
    candle_rows = await _candle_counts(db, pending_figis) if pending_figis else {}

    pending = []
    insufficient = 0
    for f, t, lot in eligible_rows:
        if f in active_set:
            continue
        pending.append({
            "ticker": t,
            "candle_count": candle_rows.get(f, 0),
            "need_download": candle_rows.get(f, 0) < 50,
        })
        if candle_rows.get(f, 0) < 50:
            insufficient += 1

    return {
        "bot_running": bot_running,
        "eligible_count": len(eligible_rows),
        "active_count": len(active_set),
        "pending": pending,
        "insufficient": insufficient,
        "log": list(_carousel_log),
    }


# --- Срезы движений (моментум-витрина): 1 / 5 / 21 / 63 торговых дня ---
_MOVERS_TTL = 180  # сек (живые цены обновляются чаще — 1д должен быть актуальным)
_movers_cache: dict = {"ts": 0.0, "data": None}
_HORIZONS = ((1, "1д"), (5, "1н"), (21, "1м"), (63, "3м"))


async def _compute_movers(top: int = 5) -> dict:
    from zoneinfo import ZoneInfo as _ZI
    from datetime import datetime as _dt, timezone as _tz
    _msk = _ZI("Europe/Moscow")
    today = _dt.now(_tz.utc).astimezone(_msk).date()
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT c.figi, i.ticker, c.ts, c.close FROM candles c "
            "JOIN instruments i ON i.figi = c.figi "
            "WHERE c.interval = 24 AND i.class_code = 'TQBR' ORDER BY c.figi, c.ts"
        ))).all()
    by: dict[str, list] = {}
    names: dict[str, str] = {}
    for f, t, ts, close in rows:
        f = str(f)
        names[f] = str(t)
        by.setdefault(f, []).append((ts, float(close or 0.0)))
    # Живая цена (TQBR quotes, кэш 60с) как «сегодняшнее» закрытие: иначе 1д-движение
    # показывает вчерашний день и расходится с live (замечание AI/UI).
    try:
        _quotes = fetch_tqbr_market()
    except Exception:
        _quotes = {}
    live: dict[str, float] = {}
    for f, tk in names.items():
        try:
            _px = float((_quotes.get(tk) or {}).get("price") or 0.0)
        except Exception:
            _px = 0.0
        if _px > 0:
            live[f] = _px
    out: dict = {}
    for days, label in _HORIZONS:
        # дневные закрытия по всем тикерам (для истории групп/стажа) + live за сегодня
        series: dict[str, list[float]] = {}
        for f, bars in by.items():
            bb = [b for b in bars if b[0].astimezone(_msk).date() != today]
            if len(bb) >= days + 1:
                cl = [x[1] for x in bb]
                if f in live:
                    cl = cl + [live[f]]
                series[names[f]] = cl
        items = []
        for tk, cl in series.items():
            if cl[-days - 1] <= 0:
                continue
            ch = (cl[-1] / cl[-days - 1] - 1) * 100
            items.append({"ticker": tk, "chg": round(ch, 1), "price": round(cl[-1], 2)})
        items.sort(key=lambda x: -x["chg"])
        # стаж: сколько дней подряд тикер в той же группе (топ-N / низ-N).
        # Считаем членство за последние _sw дней (нужна история days+_sw баров).
        streaks: dict[str, int] = {}
        _sw = 30
        try:
            if series and max(len(cl) for cl in series.values()) >= days + _sw + 1:
                for k_off in range(_sw, -1, -1):
                    m: dict[str, float] = {}
                    for tk, cl in series.items():
                        i_now = len(cl) - 1 - k_off
                        i_old = i_now - days
                        if i_now >= 0 and i_old >= 0 and cl[i_old] > 0:
                            m[tk] = cl[i_now] / cl[i_old] - 1.0
                    if len(m) < 2 * top:
                        continue
                    r = sorted(m, key=lambda x: -m[x])
                    up_set, dn_set = set(r[:top]), set(r[-top:])
                    for tk in list(streaks):
                        if tk not in up_set and tk not in dn_set:
                            streaks.pop(tk, None)
                    for tk in up_set | dn_set:
                        streaks[tk] = streaks.get(tk, 0) + 1
        except Exception:
            streaks = {}
        for x in items:
            x["streak"] = streaks.get(x["ticker"], 0)
        out[label] = {
            "n": len(items),
            "up": items[:top],
            "down": items[-top:][::-1],
            # Полный список (все тикеры) — для AI: видеть движение по всему рынку,
            # а не только топ/дно. Компактно: ticker/chg/price.
            "all": [{"ticker": x["ticker"], "chg": x["chg"], "price": x["price"]}
                    for x in items],
            "counts": {str(thr): [sum(1 for x in items if x["chg"] >= thr),
                                  sum(1 for x in items if x["chg"] <= -thr)]
                       for thr in (5, 10, 20, 40)},
        }
    return out


@router.get("/vol-carousel/preview")
async def vol_carousel_preview(db: AsyncSession = Depends(get_db)):
    """Dry-run отчёт волатильной карусели: что бы добавили/сняли при включении.

    Только диагностика — БД НЕ трогает. Управление настройками —
    через VOL_CAROUSEL_* в .env/settings (включение = осознанное решение).
    """
    from app.config import get_settings
    from app.bot.runtime import runtime
    from app.services.vol_carousel import run_vol_carousel_once

    settings = get_settings()
    report = await run_vol_carousel_once(db, settings, runtime,
                                         emit=lambda m: print(m, flush=True))
    return {
        "mode": report.get("mode", "dry-run"),
        "enabled": settings.vol_carousel_enabled,
        "ts": report.get("ts"),
        "universe_now": report.get("universe_now"),
        "target_count": report.get("target_count"),
        "top_n": settings.vol_carousel_top_n,
        "min_rng_pct": settings.vol_carousel_min_rng,
        "would_add": report.get("would_add", []),
        "would_keep": report.get("would_keep", []),
        "would_drop": report.get("would_drop", []),
        "reasons_dropped": report.get("reasons_dropped", {}),
    }


@router.get("/movers")
async def movers(top: int = 5) -> dict:
    """Срезы движений по горизонтам (1д/1н/1м/3м) — витрина для UI и AI."""
    import time as _time
    now = _time.monotonic()
    if _movers_cache["data"] is not None and now - _movers_cache["ts"] < _MOVERS_TTL:
        return {"ok": True, "cached": True, "horizons": _movers_cache["data"]}
    try:
        data = await _compute_movers(top)
        _movers_cache.update(ts=now, data=data)
        return {"ok": True, "cached": False, "horizons": data}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}",
                "horizons": _movers_cache.get("data") or {}}


@router.get("")
async def screener(db: AsyncSession = Depends(get_db)) -> dict:
    try:
        quotes = fetch_tqbr_market()
    except Exception:
        quotes = {}
    result = await db.execute(select(Instrument).where(Instrument.class_code == "TQBR"))
    universe = set(
        row[0] for row in (await db.execute(
            text("SELECT ticker FROM universe WHERE eligible_tier = 'eligible'")
        )).all()
    )
    rows = []
    for inst in result.scalars():
        q = quotes.get(inst.ticker)
        rows.append({
            "figi": inst.figi,
            "ticker": inst.ticker,
            "name": inst.name,
            "lot": inst.lot,
            "price": q["price"] if q else None,
            "turnover": q["turnover"] if q else None,
            "rng_pct": q["rng_pct"] if q else None,
            "in_universe": inst.ticker in universe,
        })
    rows.sort(key=lambda x: (x["turnover"] or 0), reverse=True)
    carousel = await _carousel_status(db)
    return {"count": len(rows), "items": rows, "carousel": carousel}


class _EligibleReq(BaseModel):
    ticker: str


async def _download_candles(figi: str, ticker: str) -> None:
    """Фоновая загрузка 3 дней 1min свечей (fire-and-forget)."""
    from app.bot.moex import ensure_moex_candles
    await asyncio.to_thread(ensure_moex_candles, figi, ticker, 3)
    _carousel_log_add("ready", ticker, "свечи скачаны, ждёт hot-add бота")


@router.post("/eligible")
async def add_eligible(req: _EligibleReq, db: AsyncSession = Depends(get_db)) -> dict:
    """Добавить тикер в eligible (елиту): вставляем в universe + запускаем скачку свечей."""
    row = (await db.execute(
        text("SELECT figi, lot FROM instruments WHERE ticker = :t AND class_code = 'TQBR'"),
        {"t": req.ticker},
    )).first()
    if not row:
        return {"ok": False, "error": "ticker not found in instruments"}
    figi, lot = row
    was_eligible = (await db.execute(
        text("SELECT 1 FROM universe WHERE figi = :f AND eligible_tier = 'eligible'"),
        {"f": figi},
    )).first()
    await db.execute(text(
        "INSERT INTO universe (figi, ticker, eligible_tier, lot_size, updated_at) "
        "VALUES (:figi, :ticker, 'eligible', :lot, now()) "
        "ON CONFLICT (figi) DO UPDATE "
        "SET eligible_tier = 'eligible', ticker = EXCLUDED.ticker, "
        "lot_size = EXCLUDED.lot_size, updated_at = now()"
    ), {"figi": figi, "ticker": req.ticker, "lot": lot})
    await db.commit()
    if was_eligible:
        _carousel_log_add("add", req.ticker, "уже был в eligible")
    else:
        _carousel_log_add("add", req.ticker, "добавлен, скачиваются свечи за 3 дня…")
    _t = asyncio.create_task(_download_candles(figi, req.ticker))
    _BG_DOWNLOADS.add(_t)
    _t.add_done_callback(_BG_DOWNLOADS.discard)
    return {"ok": True, "figi": figi, "ticker": req.ticker}


@router.delete("/eligible/{ticker}")
async def remove_eligible(ticker: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Убрать тикер из eligible: снять метку в БД + немедленно деактивировать в рантайме."""
    row = (await db.execute(
        text("SELECT figi FROM universe WHERE ticker = :t AND eligible_tier = 'eligible'"),
        {"t": ticker},
    )).first()
    await db.execute(text(
        "UPDATE universe SET eligible_tier = NULL WHERE ticker = :t"
    ), {"t": ticker})
    await db.commit()
    _carousel_log_add("remove", ticker, "убран из eligible")
    # Немедленная деактивация в работающем боте (без ожидания цикла 60с
    # в _hot_add_universe). Если бот не запущен — метка снята, при старте
    # тикер просто не попадёт в universe.
    if row and row[0]:
        try:
            from app.bot.runtime import runtime
            _res = await runtime.deactivate_figi(row[0])
            if _res.get("ok"):
                _carousel_log_add("remove", ticker,
                                  f"тикер отключён в боте (позиций закрыто: {_res.get('closed', 0)})")
        except Exception as _e:
            _carousel_log_add("remove", ticker, f"бот не запущен — тикер снят из БД ({type(_e).__name__})")
    return {"ok": True, "ticker": ticker}