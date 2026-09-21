"""Sandbox API — T-Invest sandbox (thread-safe, DB tickers, ATR TP/SL, precise prices)."""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path

from app.config import get_settings

router = APIRouter(prefix="/api/v1/sandbox", tags=["sandbox"])

# Модульный логгер: уровни DEBUG/INFO/WARNING настраиваются стандартно
# (basicConfig или в _configure_logging). Для отладки sandbox/тестов:
#   logging.getLogger("sandbox_routes").setLevel(logging.DEBUG)
_log = logging.getLogger("sandbox_routes")


def _active_creds():
    """Активный контур (sandbox|live) из .env: (token, target, account_id)."""
    s = get_settings()
    mode = s.bot_mode if s.bot_mode in ("sandbox", "live") else "sandbox"
    token = s.get_token(mode)
    target = s.get_target(mode)
    acc = s.get_account(mode) or s.get_account("sandbox")
    return token, target, acc


def _active_mode() -> str:
    s = get_settings()
    return s.bot_mode if s.bot_mode in ("sandbox", "live") else "sandbox"


CASH_FIGI = {"RUB000UTSTOM", "RUB000UT"}

_tcs_to_bbg_cache: dict[str, str] | None = None
_portfolio_cache: tuple[float, str, object] | None = None  # (ts, mode, portfolio)
_portfolio_cache_ttl: float = 8.0
_margin_map: dict[str, float] = {}

def _get_portfolio_cached():
    import time
    global _portfolio_cache
    now = time.monotonic()
    _mode = _active_mode()
    if (_portfolio_cache is not None and _portfolio_cache[1] == _mode
            and now - _portfolio_cache[0] < _portfolio_cache_ttl):
        return _portfolio_cache[2]
    p = _get_portfolio()
    _portfolio_cache = (now, _mode, p)
    return p


async def _resolve_bbg(figi: str) -> str:
    global _tcs_to_bbg_cache
    if _tcs_to_bbg_cache is None:
        try:
            from app.database import SessionLocal
            from app.models.instrument import Instrument
            from sqlalchemy import select, text
            async with SessionLocal() as db:
                rows = await db.execute(select(Instrument.figi, Instrument.ticker))
                bbg_by_ticker = {t: f for f, t in rows.all()}
                ii = await db.execute(text("SELECT figi, ticker FROM instrument_info"))
                _tcs_to_bbg_cache = {}
                for ii_figi, ii_ticker in ii.all():
                    bb = bbg_by_ticker.get(ii_ticker)
                    if bb and ii_figi:
                        _tcs_to_bbg_cache[ii_figi] = bb
        except Exception:
            _tcs_to_bbg_cache = {}
    return _tcs_to_bbg_cache.get(figi, figi)


async def _load_margin_map():
    global _margin_map
    if _margin_map:
        return
    try:
        from app.database import SessionLocal
        from sqlalchemy import text
        async with SessionLocal() as db:
            r = await db.execute(text("SELECT figi, long_lev FROM instruments WHERE long_lev > 0"))
            _margin_map = {figi: lev for figi, lev in r.all()}
    except Exception:
        _margin_map = {}


ATR_PERIOD = 14
ATR_MULT = 2.0
RISK_REWARD = 2.0


def _q(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if hasattr(v, "units") and hasattr(v, "nano"):
        return float(v.units + v.nano / 1e9)
    return float(v)


def _qty(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if hasattr(v, 'units') and hasattr(v, 'nano'):
        return int(v.units + v.nano / 1e9)
    return 0



_client = None
_client_key = None

def _get_client():
    global _client, _client_key
    token, target, _ = _active_creds()
    key = (token, target)
    if _client is None or _client_key != key:
        from t_tech.invest import Client
        _client = Client(token, target=target).__enter__()
        _client_key = key
    return _client

def _get_portfolio():
    c = _get_client()
    return c.operations.get_portfolio(account_id=_active_creds()[2])

_ops_cache: tuple[float, str, object, int] | None = None  # (ts, mode, ops, from_days)

def _get_operations(from_days=3):
    """Операции за N дней (кэш 60с, ключ = контур+окно). Окно маленькое — для entry_time/сделок хватает;
    раньше тянули 60 дней на каждый запрос → таймауты."""
    global _ops_cache
    import time as _time
    now = _time.monotonic()
    _mode = _active_mode()
    if (_ops_cache is not None and _ops_cache[3] == from_days and _ops_cache[1] == _mode
            and now - _ops_cache[0] < 60.0):
        return _ops_cache[2]
    c = _get_client()
    ops = c.operations.get_operations(
        account_id=_active_creds()[2],
        from_=datetime.now(timezone.utc) - timedelta(days=from_days),
        to=datetime.now(timezone.utc),
    )
    _ops_cache = (now, _mode, ops, from_days)
    return ops


def _get_orders():
    c = _get_client()
    return c.orders.get_orders(account_id=_active_creds()[2])


async def _tickers_for(figis):
    figis = list(dict.fromkeys(f for f in figis if f))
    if not figis:
        return {}
    out: dict[str, str] = {}
    try:
        from app.database import SessionLocal
        from app.models.instrument import Instrument
        from sqlalchemy import select, text
        async with SessionLocal() as db:
            rows = await db.execute(select(Instrument.figi, Instrument.ticker).where(Instrument.figi.in_(figis)))
            out.update({f: t for f, t in rows.all()})
            missing = [f for f in figis if f not in out]
            if missing:
                for mf in missing:
                    rows2 = await db.execute(text(
                        'SELECT figi, ticker FROM instrument_info WHERE figi = :f'
                    ), {"f": mf})
                    for ff, tt in rows2.fetchall():
                        out[ff] = tt
                out.update({f: t for f, t in rows2.fetchall()})
    except Exception:
        pass
    return out

async def _atr_data(figi: str):
    figi = await _resolve_bbg(figi)
    """Return (atr, sl, tp, prev_close, last_close) for a LONG position, from 5min candles (fallback 1min)."""
    try:
        from app.database import SessionLocal
        from app.models.candle import Candle
        from sqlalchemy import select
        for interval in (5, 1):
            async with SessionLocal() as db:
                rows = await db.execute(
                    select(Candle.close, Candle.high, Candle.low)
                    .where(Candle.figi == figi, Candle.interval == interval)
                    .order_by(Candle.ts.desc())
                    .limit(ATR_PERIOD + 5)
                )
                candles = list(rows.all())
            if len(candles) >= ATR_PERIOD:
                break
        candles.reverse()
        if len(candles) < 2:
            return None, None, None, None, None
        closes = [float(c[0]) for c in candles]
        atr = None
        if len(candles) >= ATR_PERIOD + 1:
            trs = []
            for i in range(1, len(candles)):
                h = float(candles[i][1]); l = float(candles[i][2]); pc = closes[i - 1]
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr = sum(trs[-ATR_PERIOD:]) / ATR_PERIOD
        prev_close = closes[-2] if len(closes) >= 2 else closes[-1]
        last_close = closes[-1]
        return atr, prev_close, last_close
    except Exception:
        return None, None, None


async def _entry_times(ops):
    """Map figi -> entry time (last BUY per figi)."""
    out = {}
    for op in getattr(ops, "operations", []):
        op_type = str(getattr(op, "type", ""))
        if "Покупка" not in op_type:
            continue
        figi = getattr(op, "figi", "")
        date = getattr(op, "date", None)
        if figi and date:
            out[figi] = str(date)
    return out


_init_cash_cache: float | None = None

def _portfolio_to_dict(p):
    global _init_cash_cache
    cash_val = _q(getattr(p, "total_amount_currencies", None) or 0.0)
    total = _q(getattr(p, "total_amount_portfolio", None) or 0.0)
    long_val = 0.0
    abs_val = 0.0
    for pos in p.positions:
        if pos.figi in CASH_FIGI or pos.instrument_type == "currency":
            continue
        q = _qty(pos.quantity)
        if q == 0:
            continue
        v = _q(pos.current_price) * abs(q)
        abs_val += v
        if q > 0:
            long_val += v
    equity = total if total > 0 else cash_val + abs_val
    free_cash = round(equity - abs_val, 2)
    active = len([pos for pos in p.positions if abs(_qty(pos.quantity)) > 0 and pos.figi not in CASH_FIGI and pos.instrument_type != "currency"])
    if _init_cash_cache is None:
        try:
            from sqlalchemy import create_engine, text
            from app.config import get_settings
            _eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
            with _eng.connect() as _conn:
                row = _conn.execute(text("SELECT initial_cash FROM paper_accounts WHERE name='default'")).fetchone()
                _init_cash_cache = float(row[0]) if row else 10000.0
            _eng.dispose()
        except Exception:
            _init_cash_cache = 10000.0
    return {
        "cash": free_cash,
        "initial_cash": round(_init_cash_cache, 2),
        "equity": round(equity, 2),
        "market_value": round(abs_val, 2),
        "pnl": round(equity - _init_cash_cache, 2),
        "positions_open": active,
    }


_RECONCILE_LOGGER = None


def _reconcile_logger():
    global _RECONCILE_LOGGER
    if _RECONCILE_LOGGER is None:
        import logging
        _RECONCILE_LOGGER = logging.getLogger("portfolio_reconcile")
    return _RECONCILE_LOGGER


_CASH_FLOWS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), "data", "cash_flows.json")
_CASH_FLOWS: dict = {"total": 0.0, "events": []}


def _load_cash_flows() -> None:
    global _CASH_FLOWS
    try:
        with open(_CASH_FLOWS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict):
                _CASH_FLOWS = {"total": float(d.get("total") or 0.0),
                               "events": list(d.get("events") or [])[-30:]}
    except Exception:
        pass


def _save_cash_flows() -> None:
    try:
        os.makedirs(os.path.dirname(_CASH_FLOWS_FILE), exist_ok=True)
        tmp = _CASH_FLOWS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_CASH_FLOWS, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _CASH_FLOWS_FILE)
    except Exception:
        pass


_load_cash_flows()


# Сверка кэша/позиций (digest) в /status не должна молотить каждые 8с поллинга.
_RECONCILE_MIN_INTERVAL = 120.0  # период полной сверки (сек)
_RECONCILE_LAST_TS: float = 0.0
_RECONCILE_CACHE: dict = {}      # последние цифры сверки для UI между интервалами


def _reconcile_due() -> bool:
    """Полная сверка нужна, только когда включена, бот в live/sandbox, идёт торговое
    время (МСК, будни, сессии конфига) и прошло достаточно времени с предыдущей."""
    global _RECONCILE_LAST_TS
    try:
        from app.bot.runtime import runtime
        if not getattr(runtime, "running", False):
            return False
        cfg = getattr(runtime, "config", None)
        if not getattr(cfg, "reconcile_enabled", True):
            return False
        if getattr(runtime, "broker_mode", "") not in ("sandbox", "live"):
            return False
        sess = list(getattr(cfg, "sessions", []) or ["day"])
        try:
            from app.engine.sessions import is_session_active
            if not is_session_active(datetime.now(timezone.utc), sess):
                return False
        except Exception:
            pass
    except Exception:
        pass
    _now = time.monotonic()
    if _now - _RECONCILE_LAST_TS < _RECONCILE_MIN_INTERVAL:
        return False
    _RECONCILE_LAST_TS = _now
    return True


async def _portfolio_digest() -> dict:
    """Единый проверенный блок портфеля: источник истины — T-Invest.

    Все цифры считаются из одного живого снимка портфеля T-Invest
    (get_portfolio) + наши записи о плече (sandbox_trades.open). Дополнительно
    тут же выполняется автоматическая СВЕРКА:
      * cash:  total_amount_currencies (тиньков)  vs  equity - market_value
      * pnl:   закрытые net_pnl из нашей таблицы + unrealized (тиньков)  vs  equity - initial
    Расхождения логируются в 'portfolio_reconcile' при каждом опросе.
    """
    lg = _reconcile_logger()
    try:
        p = await asyncio.to_thread(_get_portfolio_cached)
    except Exception as e:
        lg.error("digest portfolio: %s", type(e).__name__)
        return {}
    base = _portfolio_to_dict(p)
    tcur = _q(getattr(p, "total_amount_currencies", None) or 0.0)
    ts_h = getattr(p, "total_amount_shares", None)
    if ts_h is not None:
        tshares = _q(ts_h)
    else:
        tshares = _q(getattr(p, "total_amount_portfolio", None) or 0.0) - tcur
    cash_calc = float(base["cash"])
    delta_cash = round(tcur - cash_calc, 2)

    own = 0.0
    unrealized = 0.0
    lev_map: dict[str, float] = {}
    try:
        from app.database import SessionLocal as _SL
        from app.models.sandbox_trade import SandboxTrade
        from sqlalchemy import select as _sel
        async with _SL() as db:
            r = await db.execute(
                _sel(SandboxTrade.figi, SandboxTrade.leverage)
                .where(SandboxTrade.exit_time.is_(None),
                       SandboxTrade.mode == _active_mode())
            )
            for f, lev in r.all():
                if f not in lev_map:
                    lev_map[f] = float(lev) if lev else 1.0
    except Exception:
        pass

    for pos in getattr(p, "positions", []):
        if pos.figi in CASH_FIGI or pos.instrument_type == "currency":
            continue
        qty = _qty(pos.quantity)
        if abs(qty) < 1:
            continue
        avg = _q(getattr(pos, "average_position_price", None))
        cur = _q(pos.current_price)
        lev = max(1.0, lev_map.get(pos.figi, 1.0))
        own += avg * abs(qty) / lev
        unrealized += (cur - avg) * qty

    total = wins = 0
    closed_net = 0.0
    try:
        from app.database import SessionLocal as _SL2
        from app.models.sandbox_trade import SandboxTrade as _M
        from sqlalchemy import select as _sel2
        async with _SL2() as db2:
            rows = (await db2.execute(
                _sel2(_M.net_pnl).where(_M.exit_time.is_not(None),
                                        _M.mode == _active_mode())
            )).all()
            total = len(rows)
            wins = sum(1 for r_ in rows if (r_[0] or 0) > 0)
            closed_net = sum(float(r_[0] or 0) for r_ in rows)
    except Exception as e:
        lg.warning("digest trades: %s", type(e).__name__)

    accounting_pnl = round(closed_net + unrealized, 2)
    # Для live «начальный депозит» из paper_accounts (10000) не относится к счёту —
    # PnL считаем как результат сделок бота: closed_net + unrealized.
    if _active_mode() == "live":
        base = {**base, "pnl": accounting_pnl,
                "initial_cash": round(float(base["equity"]) - accounting_pnl, 2)}
    # Ввод/вывод средств владельцем (вне сделок бота): крупное необъяснимое расхождение
    # кэша принимаем и запоминаем — иначе каждый опрос пишет MISMATCH.
    due = _reconcile_due()
    if due:
        _flow_thr = max(100.0, 0.05 * abs(float(base.get("equity") or 0.0)))
        _unexplained = delta_cash - float(_CASH_FLOWS.get("total") or 0.0)
        if abs(_unexplained) > _flow_thr:
            _CASH_FLOWS["total"] = round(float(_CASH_FLOWS.get("total") or 0.0) + _unexplained, 2)
            _CASH_FLOWS.setdefault("events", []).append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "delta": round(_unexplained, 2),
                "note": "ввод/вывод средств владельцем (вне сделок бота)",
            })
            _CASH_FLOWS["events"] = _CASH_FLOWS["events"][-30:]
            _save_cash_flows()
            lg.info("RECONCILE: ВВОД/ВЫВОД средств %.2f₽ принят (вне сделок), baseline скорректирован",
                    _unexplained)
    # Вычитаем учтённые потоки ВСЕГДА (не только в момент детекта) — иначе после
    # первого принятия каждый следующий опрос снова показывал бы MISMATCH.
    delta_cash = round(delta_cash - float(_CASH_FLOWS.get("total") or 0.0), 2)

    tinkoff_pnl = float(base["pnl"])
    delta_pnl = round(accounting_pnl - tinkoff_pnl, 2)
    ok = abs(delta_cash) <= 0.5 and abs(delta_pnl) <= 1.0
    if due:
        if ok:
            lg.info("RECONCILE OK delta_cash=%.2f delta_pnl=%.2f equity=%.2f", delta_cash, delta_pnl, float(base["equity"]))
        else:
            lg.warning(
                "RECONCILE MISMATCH delta_cash=%.2f delta_pnl=%.2f | tcur=%.2f cash_calc=%.2f | "
                "closed_net=%.2f unrealized=%.2f vs tinkoff_pnl=%.2f",
                delta_cash, delta_pnl, tcur, cash_calc, closed_net, unrealized, tinkoff_pnl,
            )
    else:
        # _CASH_FLOWS меняется только в период сверки; подсветить факт «не сверялось»
        # нельзя — просто храним «в порядке».
        ok = _RECONCILE_CACHE.get("_ok", ok)

    # Маржинальные показатели (реальные свободные средства = ликвидный портфель − начальная маржа).
    _liquid = float(base["equity"])
    _start_margin = 0.0
    _min_margin = 0.0
    _suff = 0.0
    if due:
        try:
            _ma = _get_client().users.get_margin_attributes(account_id=_active_creds()[2])
            _liquid = _q(_ma.liquid_portfolio)
            _start_margin = _q(_ma.starting_margin)
            _min_margin = _q(_ma.minimal_margin)
            _suff = float(_ma.funds_sufficiency_level.units + _ma.funds_sufficiency_level.nano / 1e9)
        except Exception:
            pass
        _RECONCILE_CACHE.update(_liquid=_liquid, _start_margin=_start_margin,
                                _min_margin=_min_margin, _suff=_suff, _ok=ok)
    else:
        _cc = _RECONCILE_CACHE
        _liquid = _cc.get("_liquid", float(base["equity"]))
        _start_margin = _cc.get("_start_margin", 0.0)
        _min_margin = _cc.get("_min_margin", 0.0)
        _suff = _cc.get("_suff", 0.0)

    return {
        **base,
        "own_in_positions": round(own, 2),
        "positions_value": round(float(base["market_value"]), 2),
        "tinkoff_currencies": round(tcur, 2),
        "tinkoff_shares": round(tshares, 2),
        "free_funds": round(_liquid - _start_margin, 2),
        "starting_margin": round(_start_margin, 2),
        "minimal_margin": round(_min_margin, 2),
        "funds_sufficiency": round(_suff, 2),
        "trades": {
            "total": total,
            "wins": wins,
            "winrate": round(wins / total * 100, 1) if total else 0.0,
        },
        "reconcile": {
            "cash_calc": round(cash_calc, 2),
            "cash_tinkoff": round(tcur, 2),
            "delta_cash": delta_cash,
            "accounting_pnl": accounting_pnl,
            "tinkoff_pnl": tinkoff_pnl,
            "delta_pnl": delta_pnl,
            "closed_net": round(closed_net, 2),
            "unrealized": round(unrealized, 2),
            "ok": bool(ok),
        },
    }


def _bot_running():
    try:
        from app.bot.runtime import runtime
        return runtime.running
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ТЕСТОВЫЙ РЕЖИМ (replay с test_name): sandbox-эндпоинты отдают данные
# активного теста, чтобы фронт показывал их в ОСНОВНЫХ таблицах бота.
# ---------------------------------------------------------------------------

def _active_test_name() -> str | None:
    """Имя активного теста (config.feed == 'replay' + test_name), либо None."""
    try:
        from app.bot.runtime import runtime
        cfg = getattr(runtime, "config", None)
        if cfg is None:
            return None
        feed = getattr(cfg, "feed", None)
        tn = (getattr(cfg, "test_name", "") or "").strip()
        if feed == "replay" and tn:
            _log.debug("active test: %r (feed=%r)", tn, feed)
            return tn
        _log.debug("no active test: feed=%r test_name=%r", feed, tn)
        return None
    except Exception as e:
        _log.warning("active_test_name lookup failed: %s", e)
        return None


async def _test_trades_db(test_name: str):
    """Все сделки теста (mode='paper', test_name=...) — открытые и закрытые."""
    from app.database import SessionLocal as _SL
    from app.models.sandbox_trade import SandboxTrade
    from sqlalchemy import select as _sel
    async with _SL() as db:
        res = await db.execute(
            _sel(SandboxTrade)
            .where(SandboxTrade.mode == "paper", SandboxTrade.test_name == test_name)
            .order_by(SandboxTrade.entry_time.desc())
        )
        rows = list(res.scalars().all())
    _log.debug("test %r: %d stored trades", test_name, len(rows))
    return rows


async def _test_last_close(figi: str) -> float | None:
    """Последняя цена закрытия из БД (5m, fallback 1m), НЕ ПОЗЖЕ виртуального
    времени теста (иначе берётся свежая рыночная свеча → мнимый P&L)."""
    try:
        from app.database import SessionLocal
        from app.models.candle import Candle
        from sqlalchemy import select
        # Виртуальное время replay: ts последней поданной свечи (или начало окна).
        _vt = None
        try:
            from app.bot.runtime import runtime as _rt
            _vt = getattr(_rt, "_replay_cur", None) or getattr(_rt, "_replay_from", None)
        except Exception:
            _vt = None
        for interval in (5, 1):
            async with SessionLocal() as db:
                q = (select(Candle.close)
                     .where(Candle.figi == figi, Candle.interval == interval))
                if _vt is not None:
                    q = q.where(Candle.ts <= _vt)
                row = (await db.execute(q.order_by(Candle.ts.desc()).limit(1))).first()
            if row:
                return float(row[0])
    except Exception as e:
        _log.debug("test last_close %s: %s", figi, e)
    return None


async def _test_portfolio_digest(test_name: str, rows: list) -> dict:
    """Портфель теста только из нашей таблицы: closed net + unrealized открытых.

    initial_cash берём из runtime.config (капитал реплея), fallback 10000.
    equity = initial + closed_net + unreal (mark-to-market по цене реплея).
    Обеспечение (own) = нотионал / плечо; свободные свои = initial + closed_net − own.
    """
    initial = 10000.0
    try:
        from app.bot.runtime import runtime
        init = getattr(getattr(runtime, "config", None), "initial_cash", None)
        if init and init > 0:
            initial = float(init)
    except Exception:
        pass
    closed = [r for r in rows if r.exit_time is not None and r.exit_price is not None]
    open_ = [r for r in rows if r.exit_time is None]
    closed_net = sum(float(r.net_pnl or 0) for r in closed)
    unreal = 0.0
    mv = 0.0          # рыночная стоимость открытых (по текущей цене реплея)
    own = 0.0         # наше обеспечение = нотионал / плечо
    net_shares = 0.0  # знаковая стоимость: LONG +, SHORT −
    for r in open_:
        entry = float(r.entry_price)
        qty = abs(int(r.qty))
        cur = await _test_price(r.figi)
        if cur is None:
            cur = entry
        side = str(r.side or "LONG").upper()
        is_long = side in ("LONG", "BUY")
        unreal += ((cur - entry) if is_long else (entry - cur)) * qty
        notional = cur * qty
        lev = max(1.0, float(r.leverage or 1.0))
        mv += notional
        own += notional / lev
        net_shares += notional if is_long else -notional
    positions_open = len(open_)
    wins = len([r for r in closed if (r.net_pnl or 0) > 0])
    total = len(closed)
    free_own = initial + closed_net - own
    equity = initial + closed_net + unreal
    return {
        "cash": round(free_own, 2),
        "initial_cash": round(initial, 2),
        "equity": round(equity, 2),
        "market_value": round(mv, 2),
        "pnl": round(closed_net + unreal, 2),
        "positions_open": positions_open,
        "own_in_positions": round(own, 2),
        "positions_value": round(mv, 2),
        "tinkoff_currencies": round(free_own, 2),
        "tinkoff_shares": round(net_shares, 2),
        "trades": {
            "total": total,
            "wins": wins,
            "winrate": round(wins / total * 100, 1) if total else 0,
        },
        "reconcile": {"ok": True},
        "free_funds": round(equity - own, 2),
        "starting_margin": round(own, 2),
    }


def _test_trade_row(r) -> dict:
    """Одна сделка теста в формате /sandbox/trades (закрытая или открытая)."""
    is_open = r.exit_time is None
    return {
        "figi": r.figi, "ticker": r.ticker, "side": r.side,
        "qty": int(r.qty),
        "entry_price": round(float(r.entry_price), 6),
        "exit_price": round(float(r.exit_price), 6) if r.exit_price is not None else None,
        "entry_time": str(r.entry_time),
        "ts": str(r.exit_time) if r.exit_time else None,
        "stop_loss": round(float(r.stop_loss), 6) if r.stop_loss is not None else None,
        "take_profit": round(float(r.take_profit), 6) if r.take_profit is not None else None,
        "commission": round(float(r.commission), 2) if r.commission is not None else 0,
        "net_pnl": None if is_open else round(float(r.net_pnl), 2) if r.net_pnl is not None else None,
        "exit_reason": "на торгах" if is_open else (r.exit_reason or ""),
        "entry_reason": r.entry_reason, "meta": r.meta, "exit_meta": r.exit_meta,
        "strategy_id": "v4_enhanced",
    }


def _test_position_row(r, cur: float | None) -> dict:
    """Открытая позиция теста в формате /sandbox/positions."""
    _raw_side = str(r.side or "LONG").upper()
    is_long = _raw_side in ("LONG", "BUY")
    side = "LONG" if is_long else "SHORT"   # нормализация для UI (кнопка Продать/Купить)
    entry = float(r.entry_price)
    qty = int(r.qty)
    if cur is None:
        cur = entry
    pnl = (cur - entry) * qty if is_long else (entry - cur) * qty
    notional = entry * qty
    lev = max(1.0, float(r.leverage or 1.0))
    own = notional / lev          # свои средства (обеспечение)
    borrowed = notional - own     # заёмные (маржа)
    _sl = float(r.stop_loss) if r.stop_loss is not None else None
    _tp = float(r.take_profit) if r.take_profit is not None else None
    _atr = None
    _cr = 0.0005
    try:
        from app.bot.runtime import runtime as _rt
        _atr = _rt.atr_now(r.figi)
        _cr = float(getattr(_rt.config, "commission_rate", 0.0005) or 0.0005)
    except Exception:
        _atr = None
    _net_est = pnl - _cr * (entry + cur) * qty
    return {
        "figi": r.figi, "ticker": r.ticker, "side": side, "qty": qty,
        "entry_price": round(entry, 6),
        "entry_time": str(r.entry_time),
        "stop_loss": round(_sl, 6) if _sl is not None else None,
        "take_profit": round(_tp, 6) if _tp is not None else None,
        "trail_active": bool(r.trailing_active),
        "strategy_id": "v4_enhanced",
        "current_price": round(cur, 6),
        "prev_close": None,
        "unrealized_pnl": round(pnl, 2),
        "net_pnl_est": round(_net_est, 2),
        "roi_pct": round(pnl / own * 100, 2) if own else 0,
        "sell_value": round(own + pnl, 2),
        "leverage": round(lev, 1),
        "notional": round(notional, 2),
        "own_money": round(own, 2),
        "borrowed": round(borrowed, 2),
        "leveraged": round(borrowed, 2),
        "atr": round(_atr, 6) if _atr else None,
        "dist_sl_atr": (round(abs(cur - _sl) / _atr, 2) if (_atr and _sl) else None),
        "dist_tp_atr": (round(abs(_tp - cur) / _atr, 2) if (_atr and _tp) else None),
        "regime": "", "regime_reason": "", "regime_atr_pct": None, "regime_adx": None, "vol": None,
    }


# Кэш последних цен для unrealized тестовых позиций. Ключ — (figi, виртуальное
# время реплея): цена должна обновляться с каждым поданным баром, иначе цифры
# портфеля «замирают» на 5 реальных минут (в реплее это часы торгов).
_test_price_cache: dict[tuple[str, str], tuple[float, float | None]] = {}

async def _test_price(figi: str) -> float | None:
    import time as _t
    now = _t.monotonic()
    vt = None
    try:
        from app.bot.runtime import runtime as _rt
        vt = getattr(_rt, "_replay_cur", None)
    except Exception:
        vt = None
    key = (figi, vt.isoformat() if vt is not None else "")
    hit = _test_price_cache.get(key)
    if hit and now - hit[0] < 300:
        return hit[1]
    p = await _test_last_close(figi)
    if len(_test_price_cache) > 1024:
        _test_price_cache.clear()
    _test_price_cache[key] = (now, p)
    return p


@router.get("/status")
async def sandbox_status():
    tn = _active_test_name()
    if tn:
        try:
            rows = await _test_trades_db(tn)
            dig = await _test_portfolio_digest(tn, rows)
            _log.info("status: TEST mode %r → positions=%d trades=%d pnl=%.2f",
                      tn, dig["positions_open"], dig["trades"]["total"], dig["pnl"])
            return {"running": _bot_running(), "mode": f"TEST:{tn}", "portfolio": dig, "test_name": tn}
        except Exception as e:
            _log.error("status TEST %r failed: %s", tn, e)
            return {"running": _bot_running(), "mode": f"TEST:{tn}", "error": f"{type(e).__name__}: {e}",
                    "portfolio": {"cash": 0, "initial_cash": 10000, "equity": 0, "market_value": 0, "pnl": 0, "positions_open": 0,
                                  "own_in_positions": 0, "positions_value": 0, "tinkoff_currencies": 0, "tinkoff_shares": 0,
                                  "trades": {"total": 0, "wins": 0, "winrate": 0},
                                  "reconcile": {"ok": False}}}
    try:
        dig = await _portfolio_digest()
        if not dig:
            raise RuntimeError("digest empty")
        return {"running": _bot_running(), "mode": _active_mode().upper(), "portfolio": dig}
    except Exception as e:
        _log.warning("status error: %s", e)
        return {"running": _bot_running(), "mode": _active_mode().upper(), "error": f"{type(e).__name__}: {e}",
                "portfolio": {"cash": 0, "initial_cash": 10000, "equity": 0, "market_value": 0, "pnl": 0, "positions_open": 0,
                              "own_in_positions": 0, "positions_value": 0, "tinkoff_currencies": 0, "tinkoff_shares": 0,
                              "trades": {"total": 0, "wins": 0, "winrate": 0},
                              "reconcile": {"ok": False}}}


@router.get("/positions")
async def sandbox_positions():
    tn = _active_test_name()
    if tn:
        try:
            rows = await _test_trades_db(tn)
            open_rows = [r for r in rows if r.exit_time is None]
            items = []
            for r in open_rows:
                cur = await _test_price(r.figi)
                items.append(_test_position_row(r, cur))
            items.sort(key=lambda t: t["entry_time"], reverse=True)
            _log.info("positions: TEST %r → %d open positions", tn, len(items))
            return {"count": len(items), "positions": items}
        except Exception as e:
            _log.error("positions TEST %r failed: %s", tn, e)
            return {"count": 0, "positions": [], "error": f"{type(e).__name__}: {e}"}
    try:
        await _load_margin_map()
        p, ops = await asyncio.gather(
            asyncio.to_thread(_get_portfolio),
            asyncio.to_thread(_get_operations),
        )
        figis = [pos.figi for pos in p.positions if pos.figi not in CASH_FIGI and pos.instrument_type != "currency"]
        tmap = await _tickers_for(figis)
        etimes = await _entry_times(ops)
        # leverage + entry_time from sandbox_trades
        trade_lev: dict[str, float] = {}
        trade_et: dict[str, str] = {}
        try:
            from app.database import SessionLocal as _SL2
            from app.models.sandbox_trade import SandboxTrade
            from sqlalchemy import select as _sel2
            async with _SL2() as db:
                r = await db.execute(
                    _sel2(SandboxTrade.figi, SandboxTrade.leverage, SandboxTrade.entry_time)
                    .where(SandboxTrade.exit_time.is_(None),
                           SandboxTrade.mode == _active_mode())
                )
                for f, lev, et in r.all():
                    if f not in trade_lev:
                        trade_lev[f] = float(lev) if lev else 1.0
                    if f not in trade_et and et:
                        trade_et[f] = et.isoformat() if hasattr(et, "isoformat") else str(et)
        except Exception:
            pass
        # Риск-ставки брокера по инструментам (обеспечение): dlong/dshort из instruments.
        risk_map: dict[str, tuple[float, float]] = {}
        try:
            from app.database import SessionLocal as _SL3
            from sqlalchemy import text as _text3
            async with _SL3() as db:
                rr = await db.execute(_text3(
                    "SELECT ticker, coalesce(dlong,0), coalesce(dshort,0) FROM instruments "
                    "WHERE dlong IS NOT NULL OR dshort IS NOT NULL"
                ))
                risk_map = {str(t).upper(): (float(dl or 0), float(ds or 0))
                            for t, dl, ds in rr.all()}
        except Exception:
            pass
        # regime + vol из живого детектора (runtime._regimes), маппинг по ticker
        regime_map: dict[str, dict] = {}
        try:
            from app.bot.runtime import runtime as _rt
            _regs = getattr(_rt, "_regimes", None) or {}
            _tickers = getattr(_rt, "tickers", None) or {}
            for _f, _r in _regs.items():
                _t = _tickers.get(_f, "")
                if _t:
                    regime_map[_t] = _r
        except Exception:
            pass
        _comm_rate = 0.0005
        try:
            from app.bot.runtime import runtime as _rtc
            _comm_rate = float(getattr(_rtc.config, "commission_rate", 0.0005) or 0.0005)
        except Exception:
            pass
        items = []
        for pos in p.positions:
            if pos.figi in CASH_FIGI or pos.instrument_type == "currency":
                continue
            qty = _qty(pos.quantity)
            if abs(qty) < 1:
                continue
            avg = _q(getattr(pos, "average_position_price", None))
            cur = _q(pos.current_price)
            ticker = tmap.get(pos.figi, pos.figi[:8])
            atr, prev_close, last_close = await _atr_data(pos.figi)
            side = "LONG" if qty > 0 else "SHORT"
            # Актуальные уровни выхода — из учёта бота (переживает перезагрузку и
            # подтягивается трейлингом). Fallback — локальный расчёт по ATR.
            rt_stop = rt_target = None
            rt_trail = False
            try:
                from app.bot.runtime import runtime as _rt2
                rt_stop = getattr(_rt2, "_trail_stop", {}).get(pos.figi)
                rt_trail = bool(getattr(_rt2, "_trail_active", {}).get(pos.figi))
                rt_target = getattr(_rt2, "_exit_target", {}).get(pos.figi)
                if not rt_trail and pos.figi not in getattr(_rt2, "_exit_plans", {}):
                    rt_target = None
            except Exception:
                pass
            sl = tp = None
            if atr is not None and atr > 0:
                dist = atr * ATR_MULT
                if side == "LONG":
                    sl = round(avg - dist, 6)
                    tp = round(avg + dist * RISK_REWARD, 6)
                else:
                    sl = round(avg + dist, 6)
                    tp = round(avg - dist * RISK_REWARD, 6)
            if rt_stop is not None and rt_stop > 0:
                sl = round(float(rt_stop), 6)  # актуальный стоп (в т.ч. трейлинг)
            if rt_trail:
                tp = None  # TP выключен после активации трейлинга
            elif rt_target is not None:
                tp = round(float(rt_target), 6)
            trail_active = rt_trail
            if prev_close is None:
                prev_close = last_close
            lev_trade = max(1.0, trade_lev.get(pos.figi, 1.0))
            notional = avg * abs(qty)
            # Обеспечение = номинал × риск-ставка брокера (dlong/dshort) — так же, как
            # в кабинете Т-Инвестиций. Fallback — записанное плечо сделки.
            _rr = risk_map.get(str(ticker).upper())
            _risk = (_rr[1] if side == "SHORT" else _rr[0]) if _rr else 0.0
            if 0 < _risk < 1:
                # Брокер считает обеспечение от ТЕКУЩЕЙ стоимости позиции (проверено:
                # 901.3₽ против 901.71₽ у брокера).
                own = cur * abs(qty) * _risk
                lev = 1.0 / _risk
            else:
                lev = lev_trade
                own = notional / lev
            pnl = (cur - avg) * abs(qty) if qty > 0 else (avg - cur) * abs(qty)
            rg = regime_map.get(ticker) or {}
            rg_state = (rg.get("state") or {}) or {}
            rg_vol = rg.get("vol")
            items.append({
                "figi": pos.figi, "ticker": ticker,
                "side": side, "qty": abs(qty),
                "entry_price": round(avg, 6),
                "entry_time": trade_et.get(pos.figi) or etimes.get(pos.figi, ""),
                "stop_loss": sl, "take_profit": tp,
                "trail_active": trail_active,
                "strategy_id": "v4_enhanced",
                "current_price": round(cur, 6),
                "prev_close": round(prev_close, 6) if prev_close is not None else None,
                "unrealized_pnl": round(pnl, 2),
                "net_pnl_est": round(pnl - _comm_rate * (avg + cur) * abs(qty), 2),
                "roi_pct": round(pnl / own * 100, 2) if own > 0 else 0,
                "sell_value": round(own + pnl, 2),
                "leverage": round(lev, 2),
                "trade_leverage": round(lev_trade, 2),
                "risk_rate": round(_risk, 4) if 0 < _risk < 1 else None,
                "notional": round(notional, 2),
                "own_money": round(own, 2),
                "leveraged": round(notional - own, 2),
                "regime": rg_state.get("state") or "",
                "regime_reason": rg_state.get("reason") or "",
                "regime_atr_pct": (rg_state.get("features") or {}).get("atr_pct"),
                "regime_adx": (rg_state.get("features") or {}).get("adx"),
                "vol": rg_vol if rg_vol is not None else None,
                "atr": round(atr, 6) if atr else None,
                "dist_sl_atr": (round(abs(cur - sl) / atr, 2) if (atr and sl) else None),
                "dist_tp_atr": (round(abs(tp - cur) / atr, 2) if (atr and tp) else None),
            })
        return {"count": len(items), "positions": items}
    except Exception as e:
        return {"count": 0, "positions": [], "error": f"{type(e).__name__}: {e}"}


@router.get("/trades")
async def sandbox_trades(limit: int = 50):
    """Полные сделки: вход (покупка) и выход (продажа) — одной строкой (FIFO)."""
    tn = _active_test_name()
    if tn:
        try:
            rows = await _test_trades_db(tn)
            merged = [_test_trade_row(r) for r in rows]
            merged.sort(key=lambda t: (t["ts"] or ""), reverse=True)
            _log.info("trades: TEST %r → %d rows (limit %d)", tn, len(merged), limit)
            return {"count": len(merged), "trades": merged[:limit], "test_name": tn}
        except Exception as e:
            _log.error("trades TEST %r failed: %s", tn, e)
            return {"count": 0, "trades": [], "error": f"{type(e).__name__}: {e}"}
    try:
        ops = await asyncio.to_thread(_get_operations)
        events = []
        figis = []
        for op in ops.operations:
            op_type = str(getattr(op, "type", ""))
            if not op_type or "Пополнение" in op_type:
                continue
            figis.append(op.figi)
            ts = op.date if hasattr(op, "date") and op.date else None
            events.append({
                "figi": op.figi, "type": op_type, "ts": ts,
                "price": _q(getattr(op, "price", None)),
                "qty": abs(int(_qty(getattr(op, "quantity", None)))),
                "amount": _q(getattr(op, "payment", None)),
            })
        tmap = await _tickers_for(figis)
        events.sort(key=lambda e: e["ts"] or "")

        lots: dict[str, list] = {}  # figi -> открытые батчи
        done = []
        pending_comm = 0.0
        for ev in events:
            ot = ev["type"]
            if "комисси" in ot.lower():
                pending_comm += abs(ev["amount"])
                continue
            is_buy = "Покупка" in ot
            is_sell = "Продажа" in ot
            if not (is_buy or is_sell):
                continue
            figi = ev["figi"]
            ticker = tmap.get(figi, figi[:8])
            qty = ev["qty"]
            price = ev["price"]
            comm = round(pending_comm, 2)
            pending_comm = 0.0
            if qty <= 0 or price <= 0:
                continue
            bucket = lots.setdefault(figi, [])
            if is_buy:
                bucket.append({"qty": qty, "price": price, "comm": comm, "ts": ev["ts"]})
                continue
            # продажа: закрываем накопленные покупки FIFO
            need = qty
            used = []
            while need > 0 and bucket:
                b = bucket[0]
                take = min(need, b["qty"])
                used.append({"qty": take, "price": b["price"], "comm": b["comm"] * (take / b["qty"]) if b["qty"] else 0, "ts": b["ts"]})
                b["qty"] -= take
                if b["qty"] <= 0:
                    bucket.pop(0)
                need -= take
            if not used:
                continue
            closed_qty = sum(u["qty"] for u in used)
            entry_cost = sum(u["qty"] * u["price"] for u in used)
            entry_comm = sum(u["comm"] for u in used)
            avg_entry = entry_cost / closed_qty if closed_qty else 0
            gross = (price - avg_entry) * closed_qty
            net = round(gross - entry_comm - comm, 2)
            entry_time = used[0]["ts"]
            exit_time = ev["ts"]
            done.append({
                "figi": figi, "ticker": ticker,
                "side": "LONG", "qty": closed_qty,
                "entry_price": round(avg_entry, 6),
                "exit_price": round(price, 6),
                "entry_time": str(entry_time) if entry_time else "",
                "ts": str(exit_time) if exit_time else "",
                "commission": round(entry_comm + comm, 2),
                "net_pnl": net,
                "exit_reason": ot,
                "strategy_id": "v4_enhanced",
            })
        done.sort(key=lambda t: t["ts"], reverse=True)
        # --- Открытые позиции: источник истины = реальный портфель T-Invest ---
        open_items = []
        try:
            _p = await asyncio.to_thread(_get_portfolio)
            _ops = await asyncio.to_thread(_get_operations)
            etimes = await _entry_times(_ops)
            ofigis = [pos.figi for pos in _p.positions
                      if pos.figi not in CASH_FIGI and pos.instrument_type != "currency"]
            omap = await _tickers_for(ofigis)
            enrich: dict[str, dict] = {}
            try:
                from app.database import SessionLocal as _SL
                from app.models.sandbox_trade import SandboxTrade
                from sqlalchemy import select as _sel
                async with _SL() as db:
                    res = await db.execute(
                        _sel(SandboxTrade)
                        .where(SandboxTrade.exit_time.is_(None),
                               SandboxTrade.mode == _active_mode())
                        .order_by(SandboxTrade.entry_time.desc())
                    )
                    for r in res.scalars().all():
                        if r.figi in enrich:
                            continue
                        enrich[r.figi] = {
                            "ticker": r.ticker, "side": r.side, "qty": int(r.qty),
                            "entry_price": float(r.entry_price),
                            "entry_time": str(r.entry_time),
                            "stop_loss": float(r.stop_loss) if r.stop_loss is not None else None,
                            "take_profit": float(r.take_profit) if r.take_profit is not None else None,
                            "leverage": float(r.leverage) if r.leverage else 1.0,
                            "entry_reason": r.entry_reason, "meta": r.meta,
                        }
            except Exception:
                pass
            for pos in _p.positions:
                if pos.figi in CASH_FIGI or pos.instrument_type == "currency":
                    continue
                qty = _qty(pos.quantity)
                if abs(qty) < 1:
                    continue
                avg = _q(getattr(pos, "average_position_price", None))
                if avg <= 0:
                    continue
                side = "LONG" if qty > 0 else "SHORT"
                ticker = omap.get(pos.figi, pos.figi[:8])
                e = enrich.get(pos.figi, {})
                open_items.append({
                    "figi": pos.figi, "ticker": e.get("ticker") or ticker,
                    "side": e.get("side") or side, "qty": int(abs(qty)),
                    "entry_price": e.get("entry_price", round(avg, 6)), "exit_price": None,
                    "entry_time": e.get("entry_time") or str(etimes.get(pos.figi, "")),
                    "ts": None, "commission": 0, "net_pnl": None,
                    "stop_loss": e.get("stop_loss"), "take_profit": e.get("take_profit"),
                    "exit_reason": "на торгах",
                    "entry_reason": e.get("entry_reason"), "meta": e.get("meta"),
                    "exit_meta": None, "strategy_id": "v4_enhanced",
                    "leverage": round(float(e.get("leverage") or 1.0), 1),
                    "notional": round(float(e.get("entry_price") or round(avg, 6)) * int(abs(qty)), 2),
                    "own_money": round(round(float(e.get("entry_price") or round(avg, 6)) * int(abs(qty)), 2)
                                       / (float(e.get("leverage") or 1.0)), 2),
                })
        except Exception:
            pass
        open_items.sort(key=lambda t: t["entry_time"], reverse=True)
        # Закрытые сделки с SL/TP из нашей таблицы (бот записывает при open/close)
        closed_stored: list[dict] = []
        try:
            from app.database import SessionLocal as _SL
            from app.models.sandbox_trade import SandboxTrade
            from sqlalchemy import select as _sel
            async with _SL() as db:
                res = await db.execute(
                    _sel(SandboxTrade)
                    .where(SandboxTrade.exit_time.is_not(None),
                           SandboxTrade.mode == _active_mode())
                    .order_by(SandboxTrade.exit_time.desc())
                    .limit(200)
                )
                rows = res.scalars().all()
                for r in rows:
                    _not = round(float(r.entry_price) * int(r.qty), 2)
                    _lev = float(r.leverage) if r.leverage else 1.0
                    closed_stored.append({
                        "figi": r.figi, "ticker": r.ticker, "side": r.side,
                        "qty": r.qty,
                        "entry_price": round(float(r.entry_price), 6),
                        "exit_price": round(float(r.exit_price), 6) if r.exit_price is not None else None,
                        "entry_time": str(r.entry_time),
                        "ts": str(r.exit_time) if r.exit_time else None,
                        "stop_loss": round(float(r.stop_loss), 6) if r.stop_loss is not None else None,
                        "take_profit": round(float(r.take_profit), 6) if r.take_profit is not None else None,
                        "commission": round(float(r.commission), 2) if r.commission is not None else 0,
                        "net_pnl": round(float(r.net_pnl), 2) if r.net_pnl is not None else None,
                        "exit_reason": r.exit_reason or "",
                        "entry_reason": r.entry_reason,
                        "meta": r.meta,
                        "exit_meta": r.exit_meta,
                        "strategy_id": "v4_enhanced",
                        "leverage": round(_lev, 1),
                        "notional": _not,
                        "own_money": round(_not / _lev, 2),
                    })
        except Exception:
            closed_stored = []
        # Показываем ТОЛЬКО сделки бота из нашей таблицы (mode активного контура).
        # FIFO-реконструкция из операций брокера (done) не используется — иначе в live
        # подмешивается ручная/старая история счёта, не относящаяся к боту.
        merged = open_items + closed_stored
        return {"count": len(merged), "trades": merged[:limit]}
    except Exception as e:
        return {"count": 0, "trades": [], "error": f"{type(e).__name__}: {e}"}


@router.get("/orders")
async def sandbox_orders(limit: int = 30):
    try:
        orders = await asyncio.to_thread(_get_orders)
        figis = [o.figi for o in orders.orders]
        tmap = await _tickers_for(figis)
        items = []
        for o in orders.orders:
            ticker = tmap.get(o.figi, o.figi[:8])
            items.append({
                "id": str(o.order_id),
                "figi": o.figi, "ticker": ticker,
                "action": "BUY" if "BUY" in str(o.direction) else "SELL",
                "side": "BUY" if "BUY" in str(o.direction) else "SELL",
                "qty": int(o.lots_requested),
                "status": str(o.execution_status).split(".")[-1],
                "created_at": str(o.created_at),
                "filled_at": str(o.executed_at) if o.executed_at else None,
                "price": _q(o.initial_order_price) / max(1, int(o.lots_requested)) if o.initial_order_price else None,
            })
        return {"count": len(items), "orders": items[-limit:]}
    except Exception as e:
        return {"count": 0, "orders": [], "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Полный сброс sandbox одной командой: закрыть счёт, завести новый, пополнить,
# стереть сделки/историю этой сессии, перезапустить бота на новом счёте.
# ---------------------------------------------------------------------------

class SandboxResetRequest(BaseModel):
    cash: float = 10_000.0
    name: str = "V4_Bot_10k"
    keep_history: bool = False


_RESET_TABLES = ("sandbox_trades", "paper_trades", "paper_positions",
                 "paper_accounts", "ai_decisions")
_RESET_LOG_FILES = ("reports/ai_approval_log.jsonl", "ai_trader_cycles.jsonl")


async def _wipe_reset_data() -> dict:
    """Удалить сделки/позиции/историю текущей сессии (не трогает настройки/свечи)."""
    from app.database import SessionLocal as _SL
    from sqlalchemy import text as _text
    out: dict = {}
    async with _SL() as db:
        for _t in _RESET_TABLES:
            try:
                r = await db.execute(_text(f"DELETE FROM {_t}"))
                out[_t] = int(r.rowcount or 0)
            except Exception as e:
                out[_t] = f"skip: {type(e).__name__}"
        try:
            await db.execute(_text("DELETE FROM bot_logs"))
            out["bot_logs"] = "cleared"
        except Exception as e:
            out["bot_logs"] = f"skip: {type(e).__name__}"
        await db.commit()
    return out


def _archive_reset_logs(backend_dir: Path) -> None:
    for rel in _RESET_LOG_FILES:
        p = backend_dir / rel
        try:
            if p.exists() and p.stat().st_size > 0:
                dst = p.with_suffix(p.suffix + ".old")
                if dst.exists():
                    dst.unlink()
                p.replace(dst)
        except Exception as _sw_e:
            _log.warning("SB-RESET: журнал %s: %s", rel, type(_sw_e).__name__)


def _close_named_accounts(token: str, name: str, keep_id: str | None = None) -> list[str]:
    """Закрыть ВСЕ sandbox-счёта с именем name, кроме keep_id. Чистит «сирот» от
    оборванных сбросов и старые сессии с тем же именем (сделки удаляются со счётом)."""
    from t_tech.invest import Client
    closed: list[str] = []
    try:
        with Client(token, target="sandbox-invest-public-api.tbank.ru") as _svc:
            accs = _svc.sandbox.get_sandbox_accounts().accounts
            for a in accs:
                if str(a.name) != name:
                    continue
                if keep_id and str(a.id) == keep_id:
                    continue
                try:
                    _svc.sandbox.close_sandbox_account(account_id=a.id)
                    closed.append(str(a.id))
                except Exception as _e2:
                    _log.warning("SB-RESET: close dup %s fail: %s", a.id, _e2)
    except Exception as e:
        _log.warning("SB-RESET: list sandbox accounts fail (пропускаем чистку дублей): %s", e)
    return closed


def _fund_account(token: str, acc: str, target_cash: float, attempts: int = 4,
                  settle_wait: float = 5.0) -> float:
    """Пополнить счёт до target_cash, сверяясь с ФАКТИЧЕСКИМ балансом.

    Платим ТОЛЬКО когда баланс реально читается: если чтение падает (upstream
    в аварии), НЕ платим вслепую и ждём следующий тик. Если платёж «упал с
    таймаутом», но реально дошёл — следующий тик это увидит и доплатим только
    разницу, поэтому никогда не переплатим (лишних 4×10k не будет)."""
    from t_tech.invest import Client
    from t_tech.invest.schemas import MoneyValue

    def _cash():
        try:
            with Client(token, target="sandbox-invest-public-api.tbank.ru") as _svc:
                pf = _svc.sandbox.get_sandbox_portfolio(account_id=acc)
                return _q(getattr(pf, "total_amount_currencies", None) or 0.0)
        except Exception as _e3:
            _log.warning("SB-RESET: fund check fail: %s", _e3)
            return None

    have = _cash()
    for attempt in range(attempts):
        if have is None:
            time.sleep(3.0)
            have = _cash()
            continue
        if have >= target_cash - 0.01:
            return have
        if have < 0:
            time.sleep(2.0)
            have = _cash()
            continue
        try:
            with Client(token, target="sandbox-invest-public-api.tbank.ru") as _svc2:
                _svc2.sandbox.sandbox_pay_in(
                    account_id=acc,
                    amount=MoneyValue(currency="rub",
                                      units=int(target_cash - have), nano=0))
        except Exception as _e4:
            _log.warning("SB-RESET: pay_in attempt %d fail: %s (проверяем баланс)",
                         attempt + 1, type(_e4).__name__)
        time.sleep(settle_wait)
        have = _cash()
    return have if have is not None else 0.0


@router.post("/reset")
async def sandbox_reset(req: SandboxResetRequest) -> dict:
    """Полный сброс sandbox: закрываются ВСЕ счёта с именем --name (включая старые
    сессии и «сирот» от оборванных сбросов), заводится новый счёт, пополняется на
    --cash (с ретраями и сверкой по фактическому балансу), сделки и история текущей
    сессии стираются из БД, бот перезапускается на новом счёте."""
    import re as _re
    from app.config import get_settings as _get_settings
    from app.bot.runtime import runtime as _rt

    _log.warning("SB-RESET: start cash=%.0f name=%s keep_history=%s",
                 req.cash, req.name, req.keep_history)
    s = _get_settings()
    old = s.get_account("sandbox") or ""
    token = s.get_token("sandbox")
    if not token:
        raise HTTPException(400, "нет sandbox-токена в .env")

    was_running = bool(getattr(_rt, "running", False))
    if was_running:
        try:
            await _rt.stop()
            _log.warning("SB-RESET: бот остановлен")
        except Exception as e:
            _log.warning("SB-RESET: stop bot fail: %s", e)

    # 1) закрыть всё, что носит наше имя (дубли/сироты/прошлые сессии).
    closed_all = await asyncio.to_thread(_close_named_accounts, token, req.name)
    if closed_all:
        _log.warning("SB-RESET: закрыты дубли/сироты с именем %s: %s", req.name, closed_all)

    closed_old = None
    if old:
        try:
            def _close_old():
                from t_tech.invest import Client
                with Client(token, target="sandbox-invest-public-api.tbank.ru") as _svc:
                    _svc.sandbox.close_sandbox_account(account_id=old)
            await asyncio.to_thread(_close_old)
            closed_old = old
            _log.warning("SB-RESET: старый счёт закрыт %s", old)
        except Exception as e:
            if "NOT_FOUND" in str(e) or "not found" in str(e).lower():
                _log.warning("SB-RESET: старый счёт %s уже отсутствует — это ок", old)
            else:
                _log.warning("SB-RESET: close old %s fail: %s", old, e)

    # 2) открыть новый счёт и 3) пополнить его ДО фактического баланса.
    try:
        def _open_fund():
            from t_tech.invest import Client
            with Client(token, target="sandbox-invest-public-api.tbank.ru") as _svc3:
                r = _svc3.sandbox.open_sandbox_account(name=req.name)
                return str(r.account_id)
        new_acc = await asyncio.to_thread(_open_fund)
    except Exception as e:
        raise HTTPException(500, f"не удалось завести новый счёт: {e}")
    funded = await asyncio.to_thread(_fund_account, token, new_acc, req.cash)
    if funded < req.cash - 0.01:
        try:
            def _rollback():
                from t_tech.invest import Client
                with Client(token, target="sandbox-invest-public-api.tbank.ru") as _svc:
                    _svc.sandbox.close_sandbox_account(account_id=new_acc)
            await asyncio.to_thread(_rollback)
            _log.warning("SB-RESET: откат — недополненный счёт %s закрыт", new_acc)
        except Exception as _e5:
            _log.warning("SB-RESET: rollback close %s fail: %s", new_acc, _e5)
        raise HTTPException(502, f"счёт {new_acc} создан, но не удалось пополнить "
                                 f"(upstream T-Invest недоступен): cash={funded:.2f}")
    _log.warning("SB-RESET: новый счёт %s (+%.0f₽)", new_acc, funded)

    backend_dir = Path(__file__).resolve().parents[3]
    env_path = backend_dir / ".env"
    try:
        txt = env_path.read_text(encoding="utf-8")
        if _re.search(r"(?m)^SANDBOX_ACCOUNT=.*$", txt):
            txt2 = _re.sub(r"(?m)^SANDBOX_ACCOUNT=.*$",
                           f"SANDBOX_ACCOUNT={new_acc}", txt, count=1)
        else:
            txt2 = txt.rstrip() + f"\nSANDBOX_ACCOUNT={new_acc}\n"
        env_path.write_text(txt2, encoding="utf-8")
        _log.warning("SB-RESET: SANDBOX_ACCOUNT=%s записан в backend/.env", new_acc)
    except Exception as e:
        _log.warning("SB-RESET: .env write fail: %s", e)

    # Сбросить кэш настроек — следующий start поднимет брокера с новым аккаунтом.
    _get_settings.cache_clear()

    wiped: dict = {}
    if not req.keep_history:
        wiped = await _wipe_reset_data()
        _archive_reset_logs(backend_dir)
        _log.warning("SB-RESET: история очищена: %s",
                     {k: v for k, v in wiped.items() if v})
    else:
        wiped = {"skipped": "keep_history"}

    # Кэши портфеля/операций/маржа — чтобы UI сразу показал новый счёт.
    global _portfolio_cache, _ops_cache, _init_cash_cache, _tcs_to_bbg_cache, _margin_map
    _portfolio_cache = None
    _ops_cache = None
    _init_cash_cache = None
    _tcs_to_bbg_cache = None
    _margin_map = {}
    from app.services.loghub import hub
    hub.clear()

    restarted = False
    restart_error: str | None = None
    if not (_rt.running or _rt.starting):
        try:
            from app.api.routes.bot import _cfg_from_saved
            cfg = getattr(_rt, "config", None)
            if cfg is None:
                cfg = await _cfg_from_saved("sandbox")
            if cfg is not None:
                await _rt.start(cfg)
                restarted = True
        except Exception as e:
            restarted = False
            restart_error = f"{type(e).__name__}: {e}"
            _log.error("SB-RESET: start bot fail: %s", e)

    # Синхронизировать наш учёт капитала с фактическим пополнением счёта.
    try:
        from app.database import SessionLocal as _SL
        from sqlalchemy import text as _text
        async with _SL() as db:
            await db.execute(_text(
                "UPDATE paper_accounts SET initial_cash = :c, cash = :c WHERE name = 'default'"),
                {"c": req.cash})
            await db.commit()
        _init_cash_cache = req.cash
    except Exception as _sw_e:
        _log.warning("SB-RESET: paper_accounts sync: %s", type(_sw_e).__name__)

    _log.warning("SB-RESET: готово, новый счёт %s", new_acc)
    return {
        "ok": True,
        "account_id": new_acc,
        "account_name": req.name,
        "cash": req.cash,
        "funded": round(funded, 2),
        "closed_old": closed_old,
        "closed_dups": closed_all,
        "restarted": restarted,
        "restart_error": restart_error,
        "wiped": wiped,
    }
