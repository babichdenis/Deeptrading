from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.bot.events import EventLog
from app.bot.feed import STEP_SEC, CandleFeed
from app.bot.paper_broker import PaperBroker
from app.bot.live_broker import LiveBroker
from app.bot.risk import RiskSnapshot
from app.bot.stream_manager import StreamManager


def price_from_trade(trade) -> float | None:
    """Extract executed price from PaperTrade."""
    if trade is None:
        return None
    try:
        return float(getattr(trade, "exit_price", None) or getattr(trade, "entry_price", None) or 0)
    except Exception:
        return None
from app.bot.session import session_state
from app.config import get_settings
from app.database import SessionLocal
from app.engine.costs import CostModel
from app.engine.exits import FixedSlTpPolicy, intrabar_exit
from app.engine.models import Candle as EngineCandle, PositionState
from app.engine.policies import SignalPolicy
from app.engine.strategies import build_strategy
from app.models.instrument import Instrument
from app.models.paper import PaperTrade
from app.services.signals import _load_candles as _lc
from app.services.tinvest import INTERVAL_NAMES

MAX_BUFFER = 300
ENSEMBLE_BUFFER = 100000
DAILY_PNL_TTL = timedelta(seconds=30)
POS_PCT = 0.20  # доля портфеля на одну позицию (модель portfolio_merge)


@dataclass
class BotConfig:
    strategy_id: str = "rsi_reversal"
    params: dict = field(default_factory=dict)
    interval_name: str = "5min"
    top_n: int = 20
    qty_per_trade: int = 1
    stop_pct: float = 0.01
    target_pct: float = 0.02
    sl_mode: str = "atr"  # "atr" or "fixed"
    atr_period: int = 14
    atr_multiplier: float = 4.0
    atr_risk_reward: float = 4.0
    allow_short: bool = False
    long_allowed: bool = True
    short_allowed: bool = False
    initial_cash: float = 100_000.0
    daily_loss_limit: float = 1000.0
    mode: str = "paper"  # paper | sandbox | live
    use_ensemble: bool = False
    ensemble_capital: float = 2000.0
    ensemble_quorum: int = 2
    ensemble_session: str = "main"
    sessions: list = field(default_factory=lambda: ["day"])
    leverage: float = 1.0
    # --- Commission & slippage (live parity with backtest) ---
    commission_rate: float = 0.003  # 0.3% per trade (T-Investments)
    slippage_bps: float = 2.0  # 2 bps adverse slippage
    # --- Opposite-hold / confirm_flip ---
    confirm_flip: int = 2  # N встречных сигналов перед закрытием (0=отключено)
    # --- Re-entry cooldown ---
    reentry_cooldown_bars: int = 15  # баров между выходом и повторным входом (0=отключено)
    # --- Overnight ---
    overnight: bool = False  # закрывать позиции в конце сессии
    # --- Margin ---
    use_margin: bool = True  # использовать маржинальное кредитование
    max_margin_pct: float = 80.0  # макс % от доступного маржинального лимита на сделку


@dataclass
class BotOrder:
    id: str
    figi: str
    ticker: str
    action: str
    side: str
    qty: int
    meta: dict | None = None
    status: str = "SUBMITTED"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    filled_at: datetime | None = None
    price: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "figi": self.figi,
            "ticker": self.ticker,
            "action": self.action,
            "side": self.side,
            "qty": self.qty,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "price": round(self.price, 6) if self.price is not None else None,
        }


def _new_order_id() -> str:
    return f"paper-{uuid.uuid4().hex[:12]}"


from app.engine.sessions import is_session_active as _sessions_allowed


class PaperBotRuntime:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self.startup_task: asyncio.Task | None = None
        self.running = False
        self.starting = False
        self.mode = "—"
        self.started_at: datetime | None = None
        self.error: str | None = None
        self.config = BotConfig()
        self.broker = PaperBroker(SessionLocal)
        self.broker_mode = "paper"
        self.events = EventLog()
        self.strategies: dict[str, object] = {}
        self.buffers: dict[str, deque] = {}
        self.tickers: dict[str, str] = {}
        self.pending_orders: dict[str, BotOrder] = {}
        self.orders: deque[BotOrder] = deque(maxlen=200)
        self.universe: list[dict] = []
        self.stream_universe: list[str] = []
        self.candles_seen = 0
        self.signals_seen = 0
        self.entries_paused = False
        self.last_candle_ts: datetime | None = None
        self.data_source = "—"
        self.feed: CandleFeed | None = None
        self._daily_pnl_cache: tuple[datetime, float] | None = None
        self._signal_busy: set[str] = set()
        self._held: set[str] = set()
        self._live_logs: deque[str] = deque(maxlen=400)
        self.stream_manager: StreamManager | None = None
        self._persist_queue: list = []
        self._persist_last: dict[str, float] = {}
        self._persist_queue_5m: list = []  # (figi, ts, o, h, l, c, v) для 5m
        # --- Opposite-hold tracking ---
        self._opposite_count: dict[str, int] = {}  # figi -> consecutive opposite signals
        self._last_signal_side: dict[str, str] = {}  # figi -> last signal side
        # --- Re-entry cooldown tracking ---
        self._last_exit_bar: dict[str, int] = {}  # figi -> bar number of last exit
        self._bar_counter: int = 0  # global bar counter
        self._exit_plans: dict = {}  # figi -> ExitPolicy (for trailing stop)
        self._just_opened_this_candle: set[str] = set()  # figis opened this candle
        self._entry_bar_index: dict[str, int] = {}  # figi -> bar_index at entry
        self._no_trade_stats: dict[str, int] = {}  # reason -> count (NO_TRADE diagnostics)
        self._persist_task: asyncio.Task | None = None
        self.log_candles = True
        self._last_candle_log_ts: float = 0.0
        # --- 5m resample cache (trailing stop optimization) ---
        self._5m_cache: dict[str, list] = {}  # figi -> [resampled 5m candles]
        self._5m_last_close: dict[str, int] = {}  # figi -> last 5m close minute
        # --- Broken candle validation (mirrors _validate_candles in ensemble.py) ---
        self._prev_close: dict[str, float] = {}  # figi -> last VALID close
        self._day_jumps: dict[str, dict[str, int]] = {}  # figi -> {msk_date: jump_count}
        self._bad_day: dict[str, dict[str, bool]] = {}  # figi -> {msk_date: is_bad}
        self._candles_rejected: int = 0  # total rejected broken candles

    def _log(self, msg: str) -> None:
        ts = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S")
        self._live_logs.append(f"[{ts}] {msg}")


    def _get_5m_bars(self, figi: str, buf_list: list) -> list:
        """Return cached 5m bars, rebuild only when new 5m bar closes."""
        if not buf_list:
            return []
        now_minute = buf_list[-1].ts.minute if hasattr(buf_list[-1], "ts") else 0
        last_close = self._5m_last_close.get(figi, -1)
        current_5m = (now_minute // 5) * 5
        if current_5m != last_close:
            from app.services.ensemble import resample as _resample5u
            self._5m_cache[figi] = _resample5u(buf_list, 300)
            self._5m_last_close[figi] = current_5m
        return self._5m_cache.get(figi, [])

    def _candle_ok(self, c) -> bool:
        """Инкрементальная валидация свечи из стрима (зеркало _validate_candles).

        Отбрасывает БИТЫЕ бары: некорректные OHLC/volume и единичный прыжок
        цены >40% от последнего ВАЛИДНОГО close. prev_close обновляется только
        валидными барами, поэтому при мерцании (32->85->32) битые бары 85
        отбрасываются, а нормальные 32 принимаются — позиция управляется по
        валидным ценам. Счётчик прыжков по дню логируется (для статистики),
        но день целиком НЕ отбрасывается в live (нужно управлять позицией).
        """
        try:
            o, h, l, cl = float(c.open), float(c.high), float(c.low), float(c.close)
        except Exception:
            return False
        cfigi = getattr(c, "figi", "")
        tcs_map = getattr(self, "tcs_to_bbg", {}) or {}
        figi = tcs_map.get(cfigi, cfigi)
        if o <= 0 or cl <= 0 or h <= 0 or l <= 0:
            return False
        if h < l or h < o or h < cl or l > o or l > cl:
            return False
        try:
            if float(c.volume) < 0:
                return False
        except Exception:
            pass
        # MSK date tracking (для статистики)
        try:
            _d = c.ts.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
        except Exception:
            _d = str(getattr(c, "ts", ""))[:10]
        dj = self._day_jumps.setdefault(figi, {})
        pv = self._prev_close.get(figi)
        if pv is not None and pv > 0:
            jump = abs(cl - pv) / pv
            if jump > 0.50:
                dj[_d] = dj.get(_d, 0) + 1
                if dj[_d] in (3, 10, 30):
                    self.events.log("DATA_BAD_DAY", figi=figi,
                                    reason=f"flicker {_d} jumps={dj[_d]} prev={pv} close={cl}")
            if jump > 0.40:
                # единичный прыжок — битый бар, отбрасываем; prev_close НЕ обновляем
                self._candles_rejected += 1
                return False
        self._prev_close[figi] = cl
        return True

    async def _build_ensemble_params(self, db, figi, ticker, lot, capital, sessions):
        """Собрать EnsembleParams с per-ticker optuna-параметрами из instruments.

        Если optuna_params есть: setups из активных стратегий + их параметров,
        quorum/sl_mult/rr/vol_thr из optuna, neutral_mode=semi_flip.
        Если нет — V2 дефолты.
        """
        from sqlalchemy import text as _t
        row = (await db.execute(
            _t("SELECT optuna_params FROM instruments WHERE figi = :f"), {"f": figi}
        )).first()
        opt = (row[0] if row else None) or {}

        from app.bot.ensemble_strategy import EnsembleParams, V2_SETUPS
        ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
                    "range_compression_breakout", "macd_cross", "donchian_breakout"]
        V2P = {s["strategy_id"]: s["params"] for s in V2_SETUPS}

        if not opt.get("active_sids"):
            # V2 дефолт: все 7, SL4 RR4 q2, semi_flip
            return EnsembleParams(
                figi=figi, lot=int(lot) if lot else 10, capital=capital,
                quorum=2, session="all", sessions=sessions,
                setups=V2_SETUPS, sl_mult=4.0, rr=4.0, vol_thr=0.0,
                neutral_mode="semi_flip",
            )

        active = list(opt.get("active_sids", ALL_SIDS))
        sp = {k: dict(v) for k, v in (opt.get("strategy_params") or {}).items()}
        setups = [{"strategy_id": s, "tf": "5min",
                   "params": dict(sp.get(s, V2P.get(s, {})))} for s in active]
        return EnsembleParams(
            figi=figi, lot=int(lot) if lot else 10, capital=capital,
            quorum=int(opt.get("quorum", 2)), session="all", sessions=sessions,
            setups=setups,
            sl_mult=float(opt.get("sl_mult", 4.0)),
            rr=float(opt.get("rr", 4.0)),
            vol_thr=float(opt.get("vol_thr", 0.0) or 0.0),
            neutral_mode="semi_flip",
        )

    def _log_no_trade(self, figi: str, reason: str, detail: str = "") -> None:
        """Log why no trade was made for diagnostics (NO_TRADE analysis)."""
        self._no_trade_stats[reason] = self._no_trade_stats.get(reason, 0) + 1
        ticker = self.tickers.get(figi, figi[-6:])
        self.events.log("NO_TRADE", figi=figi, ticker=ticker, reason=reason, detail=detail)

    def get_no_trade_stats(self) -> dict[str, int]:
        """Return aggregated NO_TRADE reasons for diagnostics."""
        return dict(self._no_trade_stats)

    def log_no_trade_summary(self) -> None:
        """Print NO_TRADE stats summary to logs."""
        if not self._no_trade_stats:
            return
        total = sum(self._no_trade_stats.values())
        self._log(f"NO_TRADE STATS (total={total}): {dict(sorted(self._no_trade_stats.items(), key=lambda x: -x[1]))}")

    async def get_exit_stats(self) -> dict:
        """Aggregate exit_meta from sandbox_trades for diagnostics."""
        from sqlalchemy import text as _text
        import json as _json
        stats = {"total": 0, "by_reason": {}, "avg_bars_held": 0.0, "bars_held_sum": 0}
        try:
            async with SessionLocal() as db:
                rows = (await db.execute(
                    _text("SELECT exit_reason, exit_meta FROM sandbox_trades WHERE exit_meta IS NOT NULL")
                )).fetchall()
                for row in rows:
                    stats["total"] += 1
                    reason = row[0] or "unknown"
                    stats["by_reason"][reason] = stats["by_reason"].get(reason, 0) + 1
                    try:
                        meta = _json.loads(row[1]) if row[1] else {}
                        bh = meta.get("bars_held", 0)
                        stats["bars_held_sum"] += bh
                    except Exception:
                        pass
                if stats["total"] > 0:
                    stats["avg_bars_held"] = round(stats["bars_held_sum"] / stats["total"], 1)
        except Exception:
            pass
        return stats

    async def _st_open(self, figi, ticker, side, qty, price, sl, tp, meta: dict | None = None, leverage: float = 1.0) -> None:
        from app.models.sandbox_trade import SandboxTrade
        import json as _json
        try:
            async with SessionLocal() as db:
                db.add(SandboxTrade(
                    figi=figi, ticker=ticker, side=side, qty=int(qty),
                    entry_time=datetime.now(timezone.utc),
                    entry_price=float(price),
                    stop_loss=float(sl) if sl else None,
                    take_profit=float(tp) if tp else None,
                    entry_reason=(meta or {}).get("entry", {}).get("reason") if isinstance(meta, dict) else None,
                    meta=_json.dumps(meta, ensure_ascii=False, default=str) if meta else None,
                    leverage=float(leverage),
                ))
                await db.commit()
        except Exception:
            pass

    async def _st_close(self, figi, exit_price, reason="", net=None, meta: dict | None = None) -> None:
        from app.models.sandbox_trade import SandboxTrade
        from app.engine.costs import CostModel
        import json as _json
        try:
            async with SessionLocal() as db:
                res = await db.execute(
                    select(SandboxTrade)
                    .where(SandboxTrade.figi == figi, SandboxTrade.exit_time.is_(None))
                    .order_by(SandboxTrade.entry_time.desc())
                    .limit(1)
                )
                row = res.scalar_one_or_none()
                if row is not None:
                    row.exit_time = datetime.now(timezone.utc)
                    row.exit_price = float(exit_price)
                    row.exit_reason = reason or ""
                    row.exit_meta = _json.dumps(meta, ensure_ascii=False, default=str) if meta else row.exit_meta
                    if net is not None:
                        row.net_pnl = float(net)
                    costs = CostModel(commission_rate=self.config.commission_rate, slippage_bps=self.config.slippage_bps)
                    entry_comm = costs.commission(float(row.entry_price) * int(row.qty))
                    exit_comm = costs.commission(float(exit_price) * int(row.qty))
                    row.commission = round(entry_comm + exit_comm, 4)
                    await db.commit()
        except Exception:
            pass

    @property
    def status(self) -> dict:
        step = STEP_SEC.get(self.config.interval_name, 300)
        if self.last_candle_ts is None:
            health = "NO_DATA"
        elif datetime.now(timezone.utc) - self.last_candle_ts <= timedelta(seconds=3 * step):
            health = "HEALTHY"
        else:
            health = "STALE"
        risk = self.risk_snapshot()
        return {
            "running": self.running,
            "starting": self.starting,
            "mode": self.mode,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "error": self.error,
            "config": {
                "strategy_id": self.config.strategy_id,
                "interval_name": self.config.interval_name,
                "top_n": self.config.top_n,
                "qty": self.config.qty_per_trade,
                "stop_pct": self.config.stop_pct,
                "target_pct": self.config.target_pct,
                "sl_mode": self.config.sl_mode,
                "atr_period": self.config.atr_period,
                "atr_multiplier": self.config.atr_multiplier,
                "atr_risk_reward": self.config.atr_risk_reward,
                "mode": self.config.mode,
                "long_allowed": self.config.long_allowed,
                "short_allowed": self.config.short_allowed,
                "use_ensemble": self.config.use_ensemble,
                "ensemble_capital": self.config.ensemble_capital,
                "ensemble_quorum": self.config.ensemble_quorum,
                "ensemble_session": self.config.ensemble_session,
                "sessions": self.config.sessions,
                "leverage": self.config.leverage,
                "commission_rate": self.config.commission_rate,
                "slippage_bps": self.config.slippage_bps,
                "confirm_flip": self.config.confirm_flip,
                "reentry_cooldown_bars": self.config.reentry_cooldown_bars,
                "overnight": self.config.overnight,
            },
            "universe": self.universe,
            "candles_seen": self.candles_seen,
            "signals_seen": self.signals_seen,
            "pending_orders": len(self.pending_orders),
            "session": session_state(),
            "data": {
                "health": health,
                "source": self.data_source,
                "last_candle_ts": self.last_candle_ts.isoformat() if self.last_candle_ts else None,
            },
            "risk": {
                "state": risk.state,
                "daily_pnl": round(risk.daily_pnl, 2),
                "daily_loss_limit": risk.daily_loss_limit,
                "entries_paused": risk.entries_paused,
            },
        }

    def risk_snapshot(self) -> RiskSnapshot:
        return RiskSnapshot(
            daily_pnl=self.daily_pnl_cached(),
            daily_loss_limit=self.config.daily_loss_limit,
            entries_paused=self.entries_paused,
        )

    def daily_pnl_cached(self) -> float:
        now = datetime.now(timezone.utc)
        if self._daily_pnl_cache and now - self._daily_pnl_cache[0] < DAILY_PNL_TTL:
            return self._daily_pnl_cache[1]
        self._daily_pnl_cache = (now, 0.0)
        return self._daily_pnl_cache[1]

    async def refresh_daily_pnl(self) -> float:
        msk_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3)))
        day_start = msk_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        async with SessionLocal() as db:
            res = await db.execute(
                select(PaperTrade.net_pnl).where(PaperTrade.exit_time >= day_start)
            )
            total = sum(float(v) for v in res.scalars())
        self._daily_pnl_cache = (datetime.now(timezone.utc), total)
        return total

    async def start(self, cfg: BotConfig) -> dict:
        if self.running or self.starting:
            raise RuntimeError("bot already running")
        self.config = cfg
        if cfg.use_ensemble:
            cfg.interval_name = "1min"
        try:
            from sqlalchemy import text as _text
            async with SessionLocal() as _db:
                await _db.execute(_text("ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS entry_reason VARCHAR(128)"))
                await _db.execute(_text("ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS meta TEXT"))
                await _db.execute(_text("ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS exit_meta TEXT"))
                await _db.commit()
        except Exception:
            pass
        self.error = None
        self.candles_seen = 0
        self.signals_seen = 0
        self.strategies = {}
        self.buffers = {}
        self.pending_orders = {}
        self.tickers = {}
        self.entries_paused = False
        self.last_candle_ts = None
        self.broker_mode = cfg.mode
        if cfg.mode == "sandbox" or cfg.mode == "live":
            self.broker = LiveBroker(SessionLocal, config=cfg)
            # --- StreamManager: subscribe to server streams ---
            from app.bot.live_broker import TOKEN as _TOKEN, SB as _SB, ACC as _ACC
            target = _SB if cfg.mode == "sandbox" else None
            self.stream_manager = StreamManager(token=_TOKEN, account_id=_ACC, target=target)
            try:
                await self.stream_manager.start()
                self._log("STREAM MANAGER запущен (positions + trades + orders)")
            except Exception as e:
                self._log(f"STREAM MANAGER не запущен: {str(e)[:80]}")
                self.stream_manager = None
            try:
                real_cash = await self.broker.cash()
                if real_cash and real_cash > 0:
                    cfg.initial_cash = real_cash
                    cfg.ensemble_capital = real_cash * POS_PCT
                    self._log(f"КАПИТАЛ со счёта: {real_cash:.0f} ₽ · позиция до {cfg.ensemble_capital:.0f} ({POS_PCT*100:.0f}%)")
            except Exception as e:
                self._log(f"КАПИТАЛ не получен: {str(e)[:80]}")
        else:
            self.broker = PaperBroker(SessionLocal)
        await self.broker.ensure_account(cfg.initial_cash)
        await self.refresh_daily_pnl()
        self.starting = True
        self.startup_task = asyncio.create_task(self._startup(cfg))
        return {"started": True, "async": True}

    async def _startup(self, cfg: BotConfig) -> None:
        try:
            async with SessionLocal() as db:
                rows = await db.execute(select(Instrument.ticker, Instrument.figi))
                figi_by_ticker = dict(rows.all())
                tick_rows = await db.execute(select(Instrument.figi, Instrument.ticker))
                self.tickers = dict(tick_rows.all())

            from sqlalchemy import text
            ii_rows = await db.execute(text(
                "SELECT figi, ticker FROM instrument_info"))
            self.bbg_to_tcs = {}
            self.tcs_to_bbg = {}
            for ii_figi, ii_ticker in ii_rows.all():
                bb = figi_by_ticker.get(ii_ticker)
                if bb and ii_figi:
                    self.bbg_to_tcs[bb] = ii_figi
                    self.tcs_to_bbg[ii_figi] = bb

            from app.bot.universe import select_eligible_universe, select_volatile_universe

            async with SessionLocal() as db:
                if cfg.use_ensemble:
                    self.universe = await select_eligible_universe(db, top_n=cfg.top_n)
                else:
                    self.universe = await select_volatile_universe(
                        db, figi_by_ticker, top_n=cfg.top_n
                    )
            if not self.universe:
                raise RuntimeError("universe is empty")
            # Loaded all eligible for streaming
            async with SessionLocal() as db2:
                all_eligible = (await db2.execute(
                    text("SELECT figi FROM universe WHERE eligible_tier = 'eligible'")
                )).scalars().all()
                self.stream_universe = list(all_eligible) if all_eligible else [u["figi"] for u in self.universe]
            for u in self.universe:
                if u.get("ticker"):
                    self.tickers.setdefault(u["figi"], u["ticker"])
                    self.tickers.setdefault(self.tcs_to_bbg.get(u["figi"], u["figi"]), u["ticker"])

            from app.engine.models import Candle as EC

            async def _load_one(u):
                async with SessionLocal() as db:
                    if cfg.use_ensemble:
                        from app.bot.ensemble_strategy import EnsembleV4Strategy
                        proto = EnsembleV4Strategy(await self._build_ensemble_params(
                            db, u["figi"], u.get("ticker", ""), u.get("lot_size", 10),
                            cfg.ensemble_capital, cfg.sessions))
                    else:
                        proto = build_strategy(cfg.strategy_id, cfg.params)
                buf = deque(maxlen=ENSEMBLE_BUFFER if cfg.use_ensemble else MAX_BUFFER)
                async with SessionLocal() as db:
                    if cfg.use_ensemble:
                        from app.bot.moex import ensure_moex_candles
                        await ensure_moex_candles(u["figi"], u.get("ticker", ""), days=10)
                        candles = await _lc(db, u["figi"], 1,
                                            date_from=datetime.now(timezone.utc) - timedelta(days=60))
                        for row in candles:
                            buf.append(EC(ts=row.ts, open=row.open, high=row.high,
                                          low=row.low, close=row.close, volume=row.volume))
                    else:
                        from app.services.candle_cache import ensure_candles
                        await ensure_candles(db, u["figi"], cfg.interval_name, days=7)
                        interval_value = self._interval_value()
                        candles = await _lc(db, u["figi"], interval_value,
                                            date_from=datetime.now(timezone.utc) - timedelta(days=60))
                        for row in candles[-MAX_BUFFER:]:
                            buf.append(EC(ts=row.ts, open=row.open, high=row.high,
                                          low=row.low, close=row.close, volume=row.volume))
                return u["figi"], proto, buf

            sem = asyncio.Semaphore(3)

            async def _load_bounded(u):
                async with sem:
                    return await _load_one(u)

            results = await asyncio.gather(*[_load_bounded(u) for u in self.universe])
            for figi, proto, buf in results:
                self.strategies[figi] = proto
                self.buffers[figi] = buf

            try:
                held_now = await self.broker.positions()
                self._held = {p.figi for p in held_now}
                if self._held:
                    self._log(f"ОТКРЫТО при старте: {len(self._held)} поз.")
            except Exception:
                self._held = set()
            self.running = True
            self._log("БОТ ЗАПУЩЕН")
            self.started_at = datetime.now(timezone.utc)
            self.task = asyncio.create_task(self._run())
            self.events.log("BOT_STARTED", reason=f"{cfg.strategy_id}@{cfg.interval_name} "
                                                 f"universe={len(self.universe)} limit={cfg.daily_loss_limit}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = str(e)[:300]
            self.events.log("ERROR", reason=self.error)
        finally:
            self.starting = False

    async def stop(self) -> dict:
        was_running = self.running
        self.running = False
        if self.feed is not None:
            self.feed.request_stop()
        # --- Stop StreamManager ---
        if self.stream_manager is not None:
            try:
                await self.stream_manager.stop()
            except Exception:
                pass
            self.stream_manager = None
        for t in (self.startup_task, self.task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self.startup_task = None
        self.task = None
        self.starting = False
        self.mode = "—"
        self.events.log("BOT_STOPPED", reason="manual" if was_running else "was_not_running")
        return {"stopped": True, "was_running": was_running}

    async def set_entries_paused(self, paused: bool) -> dict:
        prev = self.entries_paused
        self.entries_paused = paused
        if paused != prev:
            self.events.log("PAUSE_NEW_ENTRIES" if paused else "RESUME_NEW_ENTRIES",
                            reason="manual")
        return {"entries_paused": self.entries_paused}

    async def cancel_pending(self) -> dict:
        cancelled = []
        for figi, order in list(self.pending_orders.items()):
            order.status = "CANCELLED"
            cancelled.append(order.to_dict())
            del self.pending_orders[figi]
            self.events.log("ORDER_CANCELLED", figi=figi, ticker=order.ticker,
                            reason="manual", order_id=order.id)
        return {"cancelled": len(cancelled), "orders": cancelled}

    async def close_all(self) -> dict:
        positions = await self.broker.positions()
        closed = []
        for p in positions:
            buf = self.buffers.get(p.figi)
            price = float(buf[-1].close) if buf else float(p.entry_price)
            trade = await self.broker.close_position(p.figi, price, "kill_switch_close_all")
            closed.append({"figi": p.figi, "ticker": p.ticker,
                           "price": round(price, 6),
                           "net_pnl": float(trade.net_pnl) if trade else None})
            self.events.log("POSITION_CLOSED", figi=p.figi, ticker=p.ticker,
                            reason="kill_switch_close_all", net_pnl=closed[-1]["net_pnl"])
        self.events.log("CLOSE_ALL", reason=f"closed={len(closed)}")
        return {"closed": len(closed), "positions": closed}

    async def _sync_held(self) -> None:
        while self.running:
            await asyncio.sleep(30.0)
            try:
                real = await self.broker.positions()
                real_set = {p.figi for p in real}
                stale = self._held - real_set
                for f in stale:
                    self._held.discard(f)
                    self._log(f"♻ ОЧИСТКА _held: {f[-6:]} (нет в портфеле)")
            except Exception:
                pass

    async def _hot_add_universe(self) -> None:
        """Фоновая задача: каждые 60с проверяет eligible тикеры с данными, добавляет в universe."""
        from sqlalchemy import text as _text
        from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy
        from app.engine.models import Candle as EC

        while self.running:
            await asyncio.sleep(60.0)
            try:
                async with SessionLocal() as db:
                    # Ищем eligible тикеры которые ещё НЕ в universe
                    current_figi = {u["figi"] for u in self.universe}
                    rows = (await db.execute(
                        _text("SELECT figi, ticker, lot_size FROM universe "
                              "WHERE eligible_tier = 'eligible' AND figi NOT IN :skip"),
                        {"skip": list(current_figi) if current_figi else ["__none__"]}
                    )).all()

                    for figi, ticker, lot in rows:
                        # Проверяем наличие данных (хотя бы 50 свечей 1min)
                        cnt = (await db.execute(
                            _text("SELECT count(*) FROM candles WHERE figi = :f AND interval = 1"),
                            {"f": figi}
                        )).scalar()
                        if cnt < 50:
                            continue

                        # Создаём стратегию и буфер
                        cfg = self.config
                        if cfg.use_ensemble:
                            from app.bot.ensemble_strategy import EnsembleV4Strategy
                            proto = EnsembleV4Strategy(await self._build_ensemble_params(
                                db, figi, ticker, lot, cfg.ensemble_capital, cfg.sessions))
                        else:
                            proto = build_strategy(cfg.strategy_id, cfg.params)

                        buf = deque(maxlen=ENSEMBLE_BUFFER if cfg.use_ensemble else MAX_BUFFER)
                        candles = await _lc(db, figi, 1,
                                            date_from=datetime.now(timezone.utc) - timedelta(days=60))
                        for row in candles:
                            buf.append(EC(ts=row.ts, open=row.open, high=row.high,
                                          low=row.low, close=row.close, volume=row.volume))

                        self.strategies[figi] = proto
                        self.buffers[figi] = buf
                        self.universe.append({
                            "figi": figi, "ticker": ticker,
                            "lot": int(lot) if lot else 10, "name": ticker,
                            "atr_pct": 0, "avg_price": 0, "avg_turnover": 0, "sector": "",
                        })
                        self.tickers[figi] = ticker
                        self._log(f"➕ HOT-ADD: {ticker} ({figi[-6:]}) {cnt} bars")

            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def _reconcile_loop(self) -> None:
        """Фоновая задача: каждые 60с сверяет StreamManager vs broker positions."""
        while self.running:
            await asyncio.sleep(60.0)
            try:
                if self.stream_manager is None or not isinstance(self.broker, LiveBroker):
                    continue
                stream_positions = {p.figi: p for p in self.stream_manager.get_positions()}
                broker_positions = {p.figi: p for p in await self.broker.positions()}
                all_figi = set(stream_positions.keys()) | set(broker_positions.keys())
                for figi in all_figi:
                    sp = stream_positions.get(figi)
                    bp = broker_positions.get(figi)
                    if sp is None and bp is not None:
                        self._log(f"RECONCILE: stream=NONE broker={bp.side} {bp.qty} {figi[-6:]}")
                    elif sp is not None and bp is None:
                        self._log(f"RECONCILE: stream={sp.side} {sp.qty} broker=NONE {figi[-6:]}")
                    elif sp is not None and bp is not None:
                        if sp.qty != bp.qty:
                            self._log(f"RECONCILE: qty mismatch {figi[-6:]} stream={sp.qty} broker={bp.qty}")
                        if sp.side != bp.side:
                            self._log(f"RECONCILE: side mismatch {figi[-6:]} stream={sp.side} broker={bp.side}")
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    def _interval_value(self) -> int:
        if self.config.use_ensemble:
            return 1
        interval = INTERVAL_NAMES[self.config.interval_name]
        return int(getattr(interval, "value", interval))

    async def _flush_persist(self) -> None:
        from sqlalchemy import text as _text
        while self.running:
            await asyncio.sleep(3.0)
            if not self._persist_queue and not self._persist_queue_5m:
                continue
            batch = self._persist_queue
            batch5 = self._persist_queue_5m
            self._persist_queue = []
            self._persist_queue_5m = []
            try:
                sql = _text(
                    "INSERT INTO candles (figi, interval, ts, open, high, low, close, volume) "
                    "VALUES (:f, 1, :ts, :o, :h, :l, :c, :v) "
                    "ON CONFLICT (figi, interval, ts) DO UPDATE SET "
                    "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
                    "close=EXCLUDED.close, volume=EXCLUDED.volume"
                )
                sql5 = _text(
                    "INSERT INTO candles (figi, interval, ts, open, high, low, close, volume) "
                    "VALUES (:f, 5, :ts, :o, :h, :l, :c, :v) "
                    "ON CONFLICT (figi, interval, ts) DO UPDATE SET "
                    "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
                    "close=EXCLUDED.close, volume=EXCLUDED.volume"
                )
                async with SessionLocal() as db:
                    for f, ts, o, h, l, cl, v in batch:
                        await db.execute(sql, {"f": f, "ts": ts, "o": o, "h": h, "l": l, "c": cl, "v": v})
                    for f, ts, o, h, l, cl, v in batch5:
                        await db.execute(sql5, {"f": f, "ts": ts, "o": o, "h": h, "l": l, "c": cl, "v": v})
                    await db.commit()
            except Exception:
                pass

    async def _run(self) -> None:
        # feed-токен и target: для sandbox используем sandbox-токен и sandbox API,
        # для paper/live — боевой. sandbox-токен НЕ работает на боевом API.
        if self.config.mode == "sandbox":
            from app.bot.live_broker import TOKEN as _TOKEN, SB as _SB
            _feed_token = _TOKEN
            _feed_target = _SB
        elif self.config.mode == "live":
            settings = get_settings()
            _feed_token = settings.tinkoff_token
            _feed_target = None
        else:
            settings = get_settings()
            _feed_token = settings.tinkoff_token
            _feed_target = None
        feed = CandleFeed(_feed_token, self.config.interval_name,
                          self.stream_universe, target=_feed_target)
        self.feed = feed
        exited = "stream_exhausted"
        self._persist_task = asyncio.create_task(self._flush_persist())
        self._held_sync_task = asyncio.create_task(self._sync_held())
        self._hot_add_task = asyncio.create_task(self._hot_add_universe())
        self._reconcile_task = asyncio.create_task(self._reconcile_loop()) if self.stream_manager else None
        try:
            async for candle in feed.stream():
                if not self.running:
                    exited = "running_flag_false"
                    break
                self.mode = feed.mode
                await self._process_candle(candle)
        except asyncio.CancelledError:
            exited = "cancelled"
            raise
        except Exception as e:
            self.error = str(e)[:300]
            exited = f"exception: {self.error[:80]}"
            self.events.log("ERROR", reason=self.error)
        else:
            self.mode = feed.mode
        finally:
            if self._persist_task:
                self._persist_task.cancel()
                try:
                    await self._persist_task
                except (asyncio.CancelledError, Exception):
                    pass
            for t in ('_held_sync_task', '_hot_add_task', '_reconcile_task'):
                task = getattr(self, t, None)
                if task:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            self.running = False
            self.events.log("BOT_LOOP_EXITED", reason=exited)

    async def _process_candle(self, c) -> None:
        figi = c.figi
        figi = self.tcs_to_bbg.get(c.figi, c.figi)

        # Битая свеча (прыжок цены / битые OHLC): пропускаем полностью —
        # не персистим и не кормим стратегию (согласуется с backtest _validate_candles)
        if not self._candle_ok(c):
            self.events.log("DATA_BAD_CANDLE", figi=figi,
                            reason=f"rejected open={c.open} close={c.close}")
            return

        # Всегда сохраняем свечу в БД (для всех 20 eligible тикеров)
        self.candles_seen += 1
        self._bar_counter += 1
        self.last_candle_ts = c.ts
        self.data_source = self.mode
        try:
            _now2 = datetime.now(timezone.utc).timestamp()
            if _now2 - self._persist_last.get(figi, 0) >= 5.0:
                self._persist_last[figi] = _now2
                self._persist_queue.append(
                    (figi, c.ts, float(c.open), float(c.high), float(c.low), float(c.close), int(c.volume or 0))
                )
        except Exception:
            pass

        # Стратегию и позиции обрабатываем только для тикеров из universe
        strategy = self.strategies.get(figi)
        buffer = self.buffers.get(figi)
        if strategy is None or buffer is None:
            return

        _did_execute = await self._execute_pending(figi, c)

        buffer.append(EngineCandle(ts=c.ts, open=c.open, high=c.high,
                                   low=c.low, close=c.close, volume=c.volume))
        # --- Ресемпл 1m → 5m: когда закрыт 5m бар (последняя минута интервала),
        # строим 5m из буфера и пишем в очередь (interval=5 в БД).
        try:
            _min = c.ts.minute
            if _min % 5 == 4 and len(buffer) >= 5:
                _tail = list(buffer)[-5:]
                if _tail and _tail[0].ts.minute % 5 == 0:
                    _o = _tail[0].open
                    _h = max(x.high for x in _tail)
                    _l = min(x.low for x in _tail)
                    _c = _tail[-1].close
                    _v = int(sum(x.volume for x in _tail))
                    _ts5 = _tail[0].ts.replace(second=0, microsecond=0)
                    # только если последний 1m бар имеет ts с минутой == %5==4 (не дубль)
                    if not any(abs((x[1] - _ts5).total_seconds()) < 1 and x[0] == figi
                               for x in self._persist_queue_5m):
                        self._persist_queue_5m.append((figi, _ts5, _o, _h, _l, _c, _v))
        except Exception:
            pass
        if self.log_candles:
            _now = datetime.now(timezone.utc).timestamp()
            if _now - self._last_candle_log_ts >= 3.0:
                self._last_candle_log_ts = _now
                self._log(f"СВЕЧА {figi[-6:]} o={c.open:.2f} h={c.high:.2f} l={c.low:.2f} c={c.close:.2f} v={c.volume}")

        _lookup = self.tcs_to_bbg.get(figi, figi)
        # --- Stream position (single source of truth) ---
        _srv_pos = None
        if self.stream_manager is not None:
            _srv_pos = self.stream_manager.get_position(_lookup) or self.stream_manager.get_position(figi)
        # Fallback: broker poll (paper mode or stream unavailable)
        pos = None
        if _srv_pos is not None:
            # Convert ServerPosition → LivePosition for intrabar_exit compatibility
            from app.bot.live_broker import LivePosition
            pos = LivePosition(
                figi=_srv_pos.figi,
                ticker=_srv_pos.ticker,
                side=_srv_pos.side,
                qty=_srv_pos.qty,
                entry_price=_srv_pos.entry_price,
                entry_time=datetime.now(timezone.utc),
                stop_loss=None,
                take_profit=None,
            )
        elif self.stream_manager is None or self.mode != "sandbox":
            pos = await self.broker.get_position(_lookup) or await self.broker.get_position(figi)
        if pos is not None:
            state = PositionState.LONG if pos.side == "LONG" else PositionState.SHORT
            stop = float(pos.stop_loss) if pos.stop_loss is not None else None
            target = float(pos.take_profit) if pos.take_profit is not None else None
            if figi.endswith("N88"):
                self._log(f"DEBUG {figi[-6:]} state={state} sl={pos.stop_loss} tp={pos.take_profit} stop_calc={stop} target_calc={target} bar_o={c.open:.2f} bar_l={c.low:.2f} bar_h={c.high:.2f}")
            price, reason = intrabar_exit(c, state, stop, target)
            if price is not None:
                trade = await self.broker.close_position(figi, price, reason)
                self._held.discard(figi)
                self._exit_plans.pop(figi, None)
                _exit_meta_sl = {
                    "exit_reason": reason,
                    "exit_price": float(price),
                    "sl": stop, "tp": target,
                    "bars_held": self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter),
                }
                await self._st_close(figi, price, reason=reason,
                                      net=float(trade.net_pnl) if trade else None,
                                      meta=_exit_meta_sl)
                pnl = float(trade.net_pnl) if trade else 0
                self._log(f"ВЫХОД {figi[-6:]} ({reason}) pnl={pnl:+.2f}")
                self._opposite_count.pop(figi, None)
                self._last_exit_bar[figi] = self._bar_counter
                self.events.log("POSITION_CLOSED", figi=figi, ticker=pos.ticker,
                                reason=reason, net_pnl=float(trade.net_pnl) if trade else None)
                await self._check_circuit_breaker()

        # --- Trailing stop: DISABLED (ATR SL/TP is static) ---

        # --- Overnight: force close at session end ---
        if self.config.overnight:
            
            if not _sessions_allowed(c.ts, self.config.sessions):
                trade = await self.broker.close_position(figi, float(c.open), "overnight_force_close")
                self._held.discard(figi)
                self._opposite_count.pop(figi, None)
                self._last_exit_bar[figi] = self._bar_counter
                _bh_overnight = self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter)
                if trade:
                    await self._st_close(figi, float(c.open), reason="overnight_force_close",
                                          net=float(trade.net_pnl), meta={"bars_held": _bh_overnight})
                    self._log(f"ВЫХОД {figi[-6:]} (overnight) pnl={float(trade.net_pnl):+.2f}")

        # --- Re-entry cooldown check ---
        cooldown = self.config.reentry_cooldown_bars
        if cooldown > 0 and figi in self._last_exit_bar:
            bars_since = self._bar_counter - self._last_exit_bar[figi]
            if bars_since < cooldown:
                self._log_no_trade(figi, "cooldown", f"bars_since={bars_since} < {cooldown}")
                return

        if figi in self._signal_busy:
            return
        self._signal_busy.add(figi)
        try:
            sig = await asyncio.to_thread(strategy.on_bar, list(buffer))
        except Exception as e:
            self.events.log("SIGNAL_ERROR", figi=figi, reason=str(e)[:200])
            sig = None
        finally:
            self._signal_busy.discard(figi)
        if sig is None:
            return
        self.signals_seen += 1
        ticker = self.tickers.get(figi, "")
        self._log(f"СИГНАЛ {ticker} {sig.side.value} ({sig.kind})")
        self.events.log("SIGNAL_CREATED", figi=figi, ticker=ticker,
                        side=sig.side.value)

        pos_now = None
        if self.stream_manager is not None:
            _srv_now = self.stream_manager.get_position(figi)
            if _srv_now is not None:
                from app.bot.live_broker import LivePosition
                pos_now = LivePosition(
                    figi=_srv_now.figi, ticker=_srv_now.ticker, side=_srv_now.side,
                    qty=_srv_now.qty, entry_price=_srv_now.entry_price,
                    entry_time=datetime.now(timezone.utc), stop_loss=None, take_profit=None,
                )
        if pos_now is None:
            pos_now = await self.broker.get_position(figi)
        state_now = PositionState.LONG if (pos_now and pos_now.side == "LONG") else (
            PositionState.SHORT if (pos_now and pos_now.side == "SHORT") else PositionState.FLAT
        )
        bars_held = 0
        policy = SignalPolicy()
        action, note = policy.decide(sig, state_now, bars_held)

        from app.engine.models import DecisionAction
        if action is DecisionAction.ACCEPT_ENTRY:
            if figi in self._held:
                self._log(f"ПРОПУСК ВХОДА {ticker}: поз. уже открыта")
                self._log_no_trade(figi, "already_held")
                return
            if not _sessions_allowed(datetime.now(timezone.utc), self.config.sessions):
                self._log(f"ПРОПУСК ВХОДА {ticker}: вне торговых сессий")
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker, reason="SESSION")
                self._log_no_trade(figi, "session_filter")
                return
            if self.entries_paused:
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason="ENTRIES_PAUSED")
                self._log_no_trade(figi, "entries_paused")
                return
            if sig.side.value == "BUY" and not self.config.long_allowed:
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason="LONG_DISABLED")
                self._log_no_trade(figi, "long_disabled")
                return
            if sig.side.value == "SELL" and not self.config.short_allowed:
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason="SHORT_DISABLED")
                self._log_no_trade(figi, "short_disabled")
                return
            risk = self.risk_snapshot()
            if not risk.entries_allowed():
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason=f"RISK_{risk.state}",
                                daily_pnl=risk.daily_pnl)
                self._log_no_trade(figi, f"risk_{risk.state.lower()}")
                return
            await self._submit_order(figi, ticker, "open", sig.side.value, meta=dict(sig.features or {}))
        elif action is DecisionAction.ACCEPT_EXIT:
            # --- Opposite-hold / confirm_flip ---
            cf = self.config.confirm_flip
            if cf > 0 and pos_now is not None:
                cur_side = "BUY" if pos_now.side == "LONG" else "SELL"
                if sig.side.value != cur_side:
                    # Противоположный сигнал — считаем
                    self._opposite_count[figi] = self._opposite_count.get(figi, 0) + 1
                    self._log(f"ОППОЗИТ {ticker} {self._opposite_count[figi]}/{cf} ({sig.side.value})")
                    if self._opposite_count[figi] < cf:
                        self.events.log("OPPOSITE_HOLD", figi=figi, ticker=ticker,
                                        count=self._opposite_count[figi], needed=cf)
                        self._log_no_trade(figi, "opposite_hold", f"count={self._opposite_count[figi]}/{cf}")
                        return  # Hold — не закрываем пока не наберём cf
                else:
                    self._opposite_count[figi] = 0  # Тот же сброс счётчика
            self._opposite_count[figi] = 0
            await self._submit_order(figi, ticker, "close", sig.side.value, meta=dict(sig.features or {}))
        else:
            self.events.log("SIGNAL_IGNORED", figi=figi, ticker=ticker,
                            reason=str(note))
            self._log_no_trade(figi, f"policy_reject:{note}")

    async def _submit_order(self, figi: str, ticker: str, action: str, side: str, meta: dict | None = None) -> None:
        cfg = self.config
        qty = cfg.qty_per_trade
        if action == "open" and cfg.use_ensemble:
            buf = self.buffers.get(figi)
            price = float(buf[-1].close) if buf else 0.0
            lot = 10
            for u in self.universe:
                if u.get("figi") == figi and u.get("lot"):
                    lot = int(u["lot"])
                    break
            budget = cfg.ensemble_capital
            if isinstance(self.broker, LiveBroker):
                try:
                    # Модель как в бэктесте: позиция = POS_PCT (20%) от текущего equity
                    _eq = await self.broker.equity()
                    budget = _eq * POS_PCT
                except Exception:
                    try:
                        live_cash = await self.broker.cash()
                        budget = live_cash * POS_PCT
                    except Exception:
                        pass
            lev = max(1.0, float(cfg.leverage or 1.0))
            lot_cost = price * lot
            if price <= 0 or lot <= 0 or lot_cost <= 0:
                self._log(f"ПРОПУСК СДЕЛКИ {ticker}: цена={price} лот={lot}")
                return
            own_per_lot = lot_cost / lev
            if budget < own_per_lot:
                self._log(f"ПРОПУСК СДЕЛКИ {ticker}: бюджет {budget:.0f} < own/лот {own_per_lot:.0f}")
                return
            qty = max(1, int(budget / own_per_lot))
        # --- Margin cap: check max lots from broker ---
        if action == "open" and isinstance(self.broker, LiveBroker) and cfg.use_margin:
            try:
                ml = await self.broker.get_max_lots(figi)
                max_lots = ml.buy_margin if side == "BUY" else ml.sell_margin
                if max_lots <= 0:
                    self._log(f"ПРОПУСК СДЕЛКИ {ticker}: маржинальный лимит = 0")
                    return
                if qty > max_lots:
                    self._log(f"QTY CAP {ticker}: {qty} → {max_lots} (margin limit)")
                    qty = max_lots
            except Exception as e:
                self._log(f"MARGIN CHECK FAIL {ticker}: {e} — proceed without cap")
        order = BotOrder(
            id=_new_order_id(),
            figi=figi,
            ticker=ticker,
            action=action,
            side=side,
            qty=qty,
            meta=dict(meta or {}),
        )
        self.pending_orders[figi] = order
        self.orders.append(order)
        self.events.log("ORDER_SUBMITTED", figi=figi, ticker=ticker,
                        order_id=order.id, action=action, side=side)

    async def _execute_pending(self, figi: str, c) -> bool:
        """Process pending order."""
        order = self.pending_orders.pop(figi, None)
        if order is None or order.status == "CANCELLED":
            return False
        cfg = self.config
        if order.action == "close":
            trade = await self.broker.close_position(figi, c.open, "signal_exit")
            actual_exit = price_from_trade(trade) if trade else c.open
            order.status = "FILLED"
            order.filled_at = datetime.now(timezone.utc)
            order.price = actual_exit
            self._held.discard(figi)
            _bars_held = self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter)
            _exit_meta_sig = {"bars_held": _bars_held, "signal_note": "signal_exit"}
            await self._st_close(figi, actual_exit, reason="signal_exit",
                                  net=float(trade.net_pnl) if trade else None,
                                  meta=_exit_meta_sig)
            pnl = float(trade.net_pnl) if trade else 0
            self._log(f"СДЕЛКА ЗАКРЫТИЕ {order.ticker} @ {actual_exit:.2f} pnl={pnl:+.2f} (candle={c.open:.2f})")
            self.events.log("ORDER_FILLED", figi=figi, ticker=order.ticker,
                            order_id=order.id, price=actual_exit, action="close",
                            net_pnl=float(trade.net_pnl) if trade else None)
            return True
        from app.engine.models import Side
        side = Side(order.side)
        if cfg.use_ensemble:
            from app.engine.exits import AtrStopPolicy, FixedSlTpPolicy
            # SL/TP: берём из optuna-параметров стратегии figi (EnsembleParams),
            # НЕ из cfg.atr_multiplier (иначе UI перезапишет optuna).
            strat = self.strategies.get(figi)
            _sl_mult = getattr(strat.p, "sl_mult", None) if strat is not None else None
            _rr = getattr(strat.p, "rr", None) if strat is not None else None
            if _sl_mult is None:
                _sl_mult = cfg.atr_multiplier
            if _rr is None:
                _rr = cfg.atr_risk_reward
            if cfg.sl_mode == "fixed":
                exit_policy = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
                plan = exit_policy.plan_entry(side, c.open, [])
            else:
                exit_policy = AtrStopPolicy(period=cfg.atr_period, multiplier=_sl_mult, risk_reward=_rr)
                from app.services.ensemble import resample as _resample5
                buf_raw = list(self.buffers.get(figi, []))
                buf_5m = _resample5(buf_raw, 300) if buf_raw else []
                plan = exit_policy.plan_entry(side, c.open, buf_5m)
        else:
            exit_policy = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
            plan = exit_policy.plan_entry(side, c.open, [])
        actual_entry = await self.broker.open_position(
            figi=figi,
            ticker=order.ticker,
            side=order.side,
            qty=order.qty,
            price=c.open,
            stop_loss=round(plan.stop_loss, 6) if plan.stop_loss is not None else None,
            take_profit=round(plan.take_profit, 6) if plan.take_profit is not None else None,
            strategy_id=cfg.strategy_id,
        )
        entry_px = actual_entry if actual_entry and actual_entry > 0 else c.open
        order.status = "FILLED"
        order.filled_at = datetime.now(timezone.utc)
        order.price = entry_px
        self._held.add(figi)
        await self._st_open(figi, order.ticker, order.side, order.qty, entry_px,
                            plan.stop_loss, plan.take_profit, meta=order.meta,
                            leverage=max(1.0, float(self.config.leverage or 1.0)))
        self._exit_plans[figi] = exit_policy
        self._entry_bar_index[figi] = self._bar_counter
        self._log(f"СДЕЛКА ВХОД {order.ticker} {order.side} qty={order.qty} @ {entry_px:.2f} (candle={c.open:.2f})")
        self.events.log("ORDER_FILLED", figi=figi, ticker=order.ticker,
                        order_id=order.id, price=entry_px, action="open")
        self.events.log("POSITION_OPENED", figi=figi, ticker=order.ticker,
                        side=order.side, qty=order.qty, entry_price=entry_px)
        return True

    async def _check_circuit_breaker(self) -> None:
        risk = RiskSnapshot(daily_pnl=await self.refresh_daily_pnl(),
                            daily_loss_limit=self.config.daily_loss_limit,
                            entries_paused=self.entries_paused)
        if risk.state == "LOSS_LIMIT" and not self.entries_paused:
            self.entries_paused = True
            self.events.log("CIRCUIT_BREAKER_TRIGGERED",
                            reason="LOSS_LIMIT", daily_pnl=risk.daily_pnl)


runtime = PaperBotRuntime()
