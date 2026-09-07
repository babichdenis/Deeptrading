"""Sandbox API — T-Invest sandbox (thread-safe, DB tickers, ATR TP/SL, precise prices)."""
import asyncio
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter
from app.config import get_settings

router = APIRouter(prefix="/api/v1/sandbox", tags=["sandbox"])

TOKEN = get_settings().sandbox or get_settings().tinkoff_token
SB = "sandbox-invest-public-api.tbank.ru"
ACC = "413306e6-f634-4aef-a553-c84e764b298a"

CASH_FIGI = {"RUB000UTSTOM", "RUB000UT"}

_tcs_to_bbg_cache: dict[str, str] | None = None
_portfolio_cache: tuple[float, object] | None = None
_portfolio_cache_ttl: float = 8.0
_margin_map: dict[str, float] = {}

def _get_portfolio_cached():
    import time
    global _portfolio_cache
    now = time.monotonic()
    if _portfolio_cache is not None and now - _portfolio_cache[0] < _portfolio_cache_ttl:
        return _portfolio_cache[1]
    p = _get_portfolio()
    _portfolio_cache = (now, p)
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

def _get_client():
    global _client
    if _client is None:
        from t_tech.invest import Client
        _client = Client(TOKEN, target=SB).__enter__()
    return _client

def _get_portfolio():
    c = _get_client()
    return c.operations.get_portfolio(account_id=ACC)

def _get_operations(from_days=60):
    c = _get_client()
    return c.operations.get_operations(
        account_id=ACC,
        from_=datetime.now(timezone.utc) - timedelta(days=from_days),
        to=datetime.now(timezone.utc),
    )


def _get_orders():
    c = _get_client()
    return c.orders.get_orders(account_id=ACC)


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
            from app.config import settings
            _eng = create_engine(settings.database_url.replace("+asyncpg", ""), pool_pre_ping=True)
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


import subprocess

def _bot_running():
    try:
        from app.bot.runtime import runtime
        return runtime.running
    except Exception:
        return False


@router.get("/status")
async def sandbox_status():
    try:
        p = await asyncio.to_thread(_get_portfolio_cached)
        return {"running": _bot_running(), "mode": "SANDBOX", "portfolio": _portfolio_to_dict(p)}
    except Exception as e:
        return {"running": _bot_running(), "mode": "SANDBOX", "error": f"{type(e).__name__}: {e}",
                "portfolio": {"cash": 0, "initial_cash": 10000, "equity": 0, "market_value": 0, "pnl": 0, "positions_open": 0}}


@router.get("/positions")
async def sandbox_positions():
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
                    .where(SandboxTrade.exit_time.is_(None))
                )
                for f, lev, et in r.all():
                    if f not in trade_lev:
                        trade_lev[f] = float(lev) if lev else 1.0
                    if f not in trade_et and et:
                        trade_et[f] = et.isoformat() if hasattr(et, "isoformat") else str(et)
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
            sl = tp = None
            if atr is not None and atr > 0:
                dist = atr * ATR_MULT
                sl = round(avg - dist, 6)
                tp = round(avg + dist * RISK_REWARD, 6)
            if prev_close is None:
                prev_close = last_close
            lev = max(1.0, trade_lev.get(pos.figi, 1.0))
            own = avg * abs(qty) / lev
            pnl = (cur - avg) * abs(qty) if qty > 0 else (avg - cur) * abs(qty)
            rg = regime_map.get(ticker) or {}
            rg_state = (rg.get("state") or {}) or {}
            rg_vol = rg.get("vol")
            items.append({
                "figi": pos.figi, "ticker": ticker,
                "side": "LONG" if qty > 0 else "SHORT", "qty": abs(qty),
                "entry_price": round(avg, 6),
                "entry_time": trade_et.get(pos.figi) or etimes.get(pos.figi, ""),
                "stop_loss": sl, "take_profit": tp,
                "strategy_id": "v4_enhanced",
                "current_price": round(cur, 6),
                "prev_close": round(prev_close, 6) if prev_close is not None else None,
                "unrealized_pnl": round(pnl, 2),
                "roi_pct": round(pnl / own * 100, 2) if own > 0 else 0,
                "sell_value": round(own + pnl, 2),
                "leverage": round(lev, 1),
                "own_money": round(own, 2),
                "leveraged": round(avg * abs(qty) - own, 2),
                "regime": rg_state.get("state") or "",
                "regime_reason": rg_state.get("reason") or "",
                "regime_atr_pct": (rg_state.get("features") or {}).get("atr_pct"),
                "regime_adx": (rg_state.get("features") or {}).get("adx"),
                "vol": rg_vol if rg_vol is not None else None,
            })
        return {"count": len(items), "positions": items}
    except Exception as e:
        return {"count": 0, "positions": [], "error": f"{type(e).__name__}: {e}"}


@router.get("/trades")
async def sandbox_trades(limit: int = 50):
    """Полные сделки: вход (покупка) и выход (продажа) — одной строкой (FIFO)."""
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
                        .where(SandboxTrade.exit_time.is_(None))
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
                    .where(SandboxTrade.exit_time.is_not(None))
                    .order_by(SandboxTrade.exit_time.desc())
                    .limit(200)
                )
                rows = res.scalars().all()
                for r in rows:
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
                    })
        except Exception:
            closed_stored = []
        closed_src = closed_stored if closed_stored else done
        merged = open_items + closed_src
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
