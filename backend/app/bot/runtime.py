from __future__ import annotations

import asyncio
import json
import time as _time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.bot.events import EventLog
from app.bot.feed import STEP_SEC, CandleFeed
from app.bot.replay_feed import ReplayFeed
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
from app.engine.exits import AtrStopPolicy, FixedSlTpPolicy, intrabar_exit
from app.engine.models import Candle as EngineCandle, PositionState, Side
from app.engine.policies import SignalPolicy
from app.engine.strategies import build_strategy
from app.models.instrument import Instrument
from app.models.paper import PaperTrade
from app.services.signals import _load_candles as _lc
from app.services.tinvest import INTERVAL_NAMES

MAX_BUFFER = 300
ENSEMBLE_BUFFER = 4320  # 3 дня 1m-свечей (~72 часовых бара для bias EMA50) — больше не нужно
DAILY_PNL_TTL = timedelta(seconds=30)
POS_PCT = 0.40  # доля портфеля на одну позицию (модель portfolio_merge)

# Файл, где хранятся настройки бота (единственный источник правды; БД — только резерв).
_BOT_CONFIG_FILE = str(Path(__file__).resolve().parents[2] / "data" / "bot_config.json")


@dataclass
class BotConfig:
    strategy_id: str = "rsi_reversal"
    params: dict = field(default_factory=dict)
    interval_name: str = "5min"
    top_n: int = 40
    qty_per_trade: int = 1
    stop_pct: float = 0.01
    target_pct: float = 0.02
    sl_mode: str = "atr"  # "atr" or "fixed"
    atr_period: int = 14
    atr_multiplier: float = 4.0
    atr_risk_reward: float = 4.0
    # --- Trailing stop ---
    initial_sl_atr: float = 4.0  # стандартный SL = 4.0×ATR (шире, чтобы сигнальные/volume-выходы успевали)
    trail_activation_comm_mult: float | None = None  # None = трейлинг ОТКЛЮЧЁН (только SL/TP+сигналы)
    trail_distance_atr: float = 2.5  # базовая дистанция трейлинга за ценой = 2.5×ATR
    # --- Динамический трейлинг ---
    trail_compress_r: float = 1.0    # сжатие дистанции по прибыли (в R): чем больше плюс, тем теснее
    trail_min_factor: float = 0.6    # минимальный множитель сжатия (2.5 → 1.5×ATR)
    trail_min_atr: float = 1.5       # минимальная дистанция (в ATR)
    trail_vol_boost: float = 0.3     # влияние объёма (высокий → шире, низкий → теснее)
    allow_short: bool = False
    long_allowed: bool = True
    short_allowed: bool = False
    initial_cash: float = 10_000.0
    daily_loss_limit: float = 1000.0
    mode: str = "paper"  # paper | sandbox | live
    use_ensemble: bool = False
    ensemble_capital: float = 2000.0
    ensemble_quorum: int = 2
    ensemble_session: str = "main"
    ensemble_entry_tf: str = "5min"  # ТФ свечей входа (micro_breakout): 1min | 5min | 10min | 15min
    ensemble_entry_from_setups: bool = True  # True: сторона из ансамблей; False: из micro_breakout
    ensemble_direction_sid: str = ""  # Путь 2: направление только от этой стратегии (пусто = общий режим)
    # --- Тройное подтверждение входа на 1м свечах ---
    entry_confirm_closes: int = 3  # N 1м-закрытий строго по направлению (BUY: каждое выше предыдущего)
    entry_confirm_closes_sides: list = field(default_factory=lambda: ["BUY"])  # к каким сторонам применять
    # --- HOLD после серии убытков ---
    loss_streak_hold: bool = True       # пауза входов после серии убытков
    loss_streak_n: int = 2              # сколько убытков подряд
    loss_streak_hold_min: float = 60.0  # длительность паузы, минут
    loss_streak_scope: str = "ticker"   # ticker | global
    # --- Оверрайд SL/TP (0 = брать per-ticker optuna) ---
    ensemble_sl_mult: float = 0.0
    ensemble_rr: float = 0.0
    sessions: list = field(default_factory=lambda: ["day"])
    leverage: float = 1.0
    # --- Commission & slippage (live parity with backtest) ---
    commission_rate: float = 0.0005  # 0.05% per trade (T-Invest, parity с движком)
    slippage_bps: float = 2.0  # 2 bps adverse slippage
    # --- Opposite-hold / confirm_flip ---
    confirm_flip: int = 2  # N встречных сигналов перед закрытием (0=отключено)
    invert_signals: bool = False  # ЭКСПЕРИМЕНТ: инвертировать сторону входа (проверка "обратной" логики)
    # --- Re-entry cooldown ---
    reentry_cooldown_bars: int = 15  # баров между выходом и повторным входом (0=отключено)
    # --- Overnight ---
    overnight: bool = False  # по умолчанию закрывать на конец торгового дня; True = держать через ночь
    # --- Margin ---
    use_margin: bool = True  # использовать маржинальное кредитование
    margin_sessions: list = field(default_factory=lambda: ["day"])  # сессии с маржой: morning/day/evening
    margin_leverage: float = 0.0  # потолок плеча: 0 = Max (как одобрит брокер), иначе 2/3/4/5...
    margin_sizing: str = "divide"  # divide: позиция=бюджет (свои=бюджет/плечо); multiply: позиция=бюджет×плечо
    # --- Торговые режимы (regime gate): в каких режимах рынка разрешены входы ---
    trade_regimes: list = field(default_factory=lambda: ["NEUTRAL", "TREND_UP", "TREND_DOWN", "HIGH_VOLATILITY", "RANGE"])
    trend_alignment: bool = True  # не входить против тренда (TREND_UP→BUY, TREND_DOWN→SELL)
    max_margin_pct: float = 80.0  # макс % от доступного маржинального лимита на сделку
    # --- Replay (V2): тест на исторических данных через те же runtime-пути, что и live ---
    feed: str = "live"        # live | replay (источник свечей; реплей = БД)
    replay_start: str = ""    # ISO UTC datetime начала окна реплея (обязателен при feed=replay)
    replay_end: str = ""      # ISO UTC datetime конца окна (пусто = до конца данных)
    replay_pace: str = "fast" # fast (макс. скорость) | wall (в реальном времени по барам)
    test_name: str = ""       # имя теста (режим test): сделки реплея помечаются им
    intrabar_check_sec: float = 10.0  # период intrabar-проверки SL/TP по последней цене (0=выкл)
    # --- IMOEX guard: запрет новых входов против всплеска индекса MOEX ---
    imoex_guard: bool = True          # вкл/выкл защиту
    imoex_spike_pct: float = 0.8      # порог хода индекса за окно, % (активация)
    imoex_spike_points: float = 20.0  # порог хода индекса за окно, пункты (0=выкл)
    imoex_spike_window_min: int = 20  # окно расчёта хода индекса, минут
    imoex_release_frac: float = 0.5   # релиз, когда ход затух до N от порога
    imoex_min_block_min: float = 5.0  # мин. длительность блока, минут
    imoex_refresh_sec: float = 60.0   # период догрузки 1м свечей IMOEX (live), сек
    imoex_guard_min_beta: float = 0.0  # 0 = блокировать все; >0 = только бумаги с beta >= порога
    imoex_chase_block_pct: float = 1.5  # второй уровень: при |ходе| >= N% блокировать и входы ПО индексу (0=выкл)
    # --- AI-гейт подтверждения входов (approval gate) ---
    ai_approval: bool = False              # True = новые входы ждут подтверждения ИИ/человека
    ai_approval_timeout_sec: float = 45.0  # сколько ждать решение, сек
    ai_approval_default: str = "approve"   # approve | reject — что делать по таймауту
    ai_reject_cooldown_min: float = 15.0   # пауза входов по тикеру после отклонения ИИ, мин (0=выкл)


# Поля BotConfig, которые сохраняются в БД и восстанавливаются при старте бота.
BOT_PERSIST_FIELDS = (
    "sessions", "long_allowed", "short_allowed", "leverage",
    "margin_sessions", "margin_leverage", "margin_sizing",
    "trade_regimes", "trend_alignment",
    "trail_distance_atr", "trail_compress_r", "trail_min_factor", "trail_min_atr", "trail_vol_boost",
    "stop_pct", "target_pct", "sl_mode", "atr_period", "atr_multiplier",
    "atr_risk_reward", "top_n", "ensemble_quorum", "commission_rate",
    "overnight", "reentry_cooldown_bars", "confirm_flip", "invert_signals", "ensemble_entry_tf", "ensemble_entry_from_setups", "ensemble_direction_sid",
    "entry_confirm_closes", "entry_confirm_closes_sides",
    "loss_streak_hold", "loss_streak_n", "loss_streak_hold_min", "loss_streak_scope",
    "imoex_guard", "imoex_spike_pct", "imoex_spike_points", "imoex_spike_window_min",
    "imoex_release_frac", "imoex_min_block_min", "imoex_guard_min_beta", "imoex_chase_block_pct",
    "ai_approval", "ai_approval_timeout_sec", "ai_approval_default",
    "ai_reject_cooldown_min",
)


async def load_bot_settings() -> dict:
    """Прочитать сохранённые настройки бота из файла bot_config.json (и БД как фолбэк)."""
    from app.models.bot_setting import BotSetting
    # Файл — источник правды. БД оставляем как резерв для старых версий/тестов.
    try:
        _p = Path(_BOT_CONFIG_FILE)
        if _p.exists():
            _data = json.loads(_p.read_text(encoding="utf-8"))
            if isinstance(_data, dict) and _data:
                return _data
    except Exception:
        pass
    try:
        async with SessionLocal() as db:
            row = await db.get(BotSetting, "runtime_config")
            return dict(row.value) if (row is not None and row.value) else {}
    except Exception:
        return {}


async def save_bot_settings(cfg) -> None:
    """Сохранить изменяемые настройки BotConfig в файл bot_config.json (+ БД для совместимости)."""
    from app.models.bot_setting import BotSetting
    data = {}
    for f in BOT_PERSIST_FIELDS:
        v = getattr(cfg, f, None)
        if isinstance(v, (list, tuple)):
            v = list(v)
        data[f] = v
    try:
        _p = Path(_BOT_CONFIG_FILE)
        _p.parent.mkdir(parents=True, exist_ok=True)
        _tmp = _p.with_suffix(".json.tmp")
        _tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _tmp.replace(_p)
    except Exception:
        pass
    try:
        async with SessionLocal() as db:
            row = await db.get(BotSetting, "runtime_config")
            if row is None:
                db.add(BotSetting(key="runtime_config", value=data))
            else:
                row.value = data
            await db.commit()
    except Exception:
        pass


# Флаг-состояние runtime, которое должно переживать рестарты бота (ключ='bot_flags').
_BOT_FLAGS_KEY = "bot_flags"

# --- Конфиг состава кворума (UI-управление движком) ---
_ENSEMBLE_CONFIG_FILE = str(Path(__file__).resolve().parents[2] / "data" / "ensemble_config.json")
_ENSEMBLE_ALL_STRATEGIES = [
    "rsi_reversal", "bollinger_reclaim", "vwap_reclaim", "macd_cross", "donchian_breakout",
    "pullback_ema", "range_compression_breakout", "volume_drop", "volume_climax", "stochastic",
]
_ENSEMBLE_DEFAULT_ENABLED = {
    "rsi_reversal", "bollinger_reclaim", "vwap_reclaim", "macd_cross", "donchian_breakout", "volume_drop",
}


def default_ensemble_config() -> dict:
    from app.bot.ensemble_strategy import V2_SETUPS
    V2P = {s["strategy_id"]: dict(s["params"]) for s in V2_SETUPS}
    V2P["volume_drop"] = {"ma_len": 20, "drop_ratio": 1.5}
    V2P["volume_climax"] = {"ma_len": 20, "climax_ratio": 3.0, "wick_frac": 0.5}
    V2P["stochastic"] = {"k_period": 14, "d_period": 3, "oversold": 20, "overbought": 80}
    setups = [{"strategy_id": s, "enabled": s in _ENSEMBLE_DEFAULT_ENABLED,
               "tf": "5min", "params": V2P.get(s, {})} for s in _ENSEMBLE_ALL_STRATEGIES]
    return {"quorum": 2, "neutral_mode": "semi_flip", "setups": setups,
            "regime_setups_filter": {}, "bias": {"tf": "hour", "period": 50},
            "entry_tf": "5min"}


async def load_ensemble_config() -> dict:
    """Состав кворума из data/ensemble_config.json (источник правды), дефолт если нет."""
    try:
        _p = Path(_ENSEMBLE_CONFIG_FILE)
        if _p.exists():
            _d = json.loads(_p.read_text(encoding="utf-8"))
            if isinstance(_d, dict) and _d.get("setups"):
                return _d
    except Exception:
        pass
    return default_ensemble_config()


async def save_ensemble_config(cfg: dict) -> None:
    try:
        _p = Path(_ENSEMBLE_CONFIG_FILE)
        _p.parent.mkdir(parents=True, exist_ok=True)
        _tmp = _p.with_suffix(".json.tmp")
        _tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        _tmp.replace(_p)
    except Exception:
        pass


async def load_bot_flags() -> dict:
    """Прочитать персистентные runtime-флаги (entries_paused и т.п.) из bot_settings."""
    from app.models.bot_setting import BotSetting
    try:
        async with SessionLocal() as db:
            row = await db.get(BotSetting, _BOT_FLAGS_KEY)
            return dict(row.value) if (row is not None and row.value) else {}
    except Exception:
        return {}


async def save_bot_flags(flags: dict) -> None:
    """Сохранить отдельный флаг (merge) в bot_settings."""
    from app.models.bot_setting import BotSetting
    try:
        async with SessionLocal() as db:
            row = await db.get(BotSetting, _BOT_FLAGS_KEY)
            cur = dict(row.value) if (row is not None and row.value) else {}
            cur.update(flags)
            if row is None:
                db.add(BotSetting(key=_BOT_FLAGS_KEY, value=cur))
            else:
                row.value = cur
            await db.commit()
    except Exception:
        pass


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


def _loss_hold_left(now: datetime, count: int, last_ts: datetime | None,
                    n: int, hold_min: float) -> float:
    """Сколько минут ещё действует пауза после серии убытков (0 = нет паузы).

    Чистая функция (для тестов): count >= n убытков подряд, последний — last_ts,
    пауза hold_min минут от последнего убытка.
    """
    if count < n or last_ts is None or hold_min <= 0:
        return 0.0
    left = hold_min - (now - last_ts).total_seconds() / 60.0
    return left if left > 0 else 0.0


from app.engine.sessions import is_session_active as _sessions_allowed, should_force_close as _should_force_close, is_clearing_gap as _is_clearing_gap


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
        self._candles_received = 0
        self._last_q_report_ts = 0.0
        self._persist_flushes = 0
        self.entries_paused = False
        self.last_candle_ts: datetime | None = None
        self.data_source = "—"
        self.feed: CandleFeed | None = None
        self._daily_pnl_cache: tuple[datetime, float] | None = None
        # --- Replay: виртуальные часы. При feed=replay _replay_from = начало окна,
        # _replay_cur = ts последней поданной свечи (двигается runtime в _run()).
        # _bot_now() возвращает виртуальное время в реплее и реальное — в live. ---
        self._replay_from: datetime | None = None
        self._replay_cur: datetime | None = None
        self._signal_busy: set[str] = set()
        self._skip_logged: dict[str, object] = {}  # figi -> ts последней залогированной причины "нет входа"
        self._held: set[str] = set()
        self._held_since: dict[str, float] = {}  # figi -> время добавления в _held (для grace синка)
        self._live_logs: deque[str] = deque(maxlen=400)
        self._log_persist_queue: deque[tuple[str, str, str]] = deque(maxlen=2000)  # (level, source, msg) — дренится в bot_logs флашером
        self._log_persist_queue: deque[tuple[str, str, str]] = deque(maxlen=2000)  # (level, source, msg) — дренится в bot_logs флашером
        self.stream_manager: StreamManager | None = None
        self.carousel_diag: dict = {
            "eligible_count": 0,
            "active_count": 0,
            "hot_adds": 0,
            "hot_skips": 0,
            "last_hot_add_ts": None,
            "last_error": None,
            "instruments": [],
        }
        self._persist_queue: deque = deque(maxlen=2000)
        self._persist_queue_5m: deque = deque(maxlen=2000)  # (figi, ts, o, h, l, c, v) для 5m
        # --- Метрики движка (для мониторинга/автореакции в status) ---
        self.metrics: dict = {
            "started_ts": None,
            "bar_ms_total": 0.0, "bar_ms_n": 0, "bar_ms_max": 0.0,
            "ensemble_ms_total": 0.0, "ensemble_ms_n": 0, "ensemble_ms_max": 0.0,
            "persist_ms_total": 0.0, "persist_ms_n": 0, "persist_ms_max": 0.0,
            "last_alert": None,
            "alerts": [],
        }
        # --- Closed-bar gates ---
        self._last_closed_bar: dict[str, datetime] = {}  # figi -> ts последней обработанной ЗАКРЫТОЙ свечи
        self._last_persist_bar: dict[str, datetime] = {}  # figi -> ts последней записанной в БД ЗАКРЫТОЙ свечи
        # --- Opposite-hold tracking ---
        self._opposite_count: dict[str, int] = {}  # figi -> consecutive opposite signals
        self._last_signal_side: dict[str, str] = {}  # figi -> last signal side
        # --- Re-entry cooldown tracking ---
        self._last_exit_bar: dict[str, int] = {}  # figi -> bar number of last exit
        self._bar_counter: int = 0  # global bar counter
        self._just_opened_this_candle: set[str] = set()  # figis opened this candle
        self._entry_bar_index: dict[str, int] = {}  # figi -> bar_index at entry
        self._no_trade_stats: dict[str, int] = {}  # reason -> count (NO_TRADE diagnostics)
        self._regimes: dict[str, dict] = {}  # figi -> {state, vol} последних 5м баров
        self._votes: dict[str, dict] = {}  # figi -> {ts, buy, sell, members} голоса на последнем 5m баре
        self._votes_logged: dict[str, str] = {}  # figi -> ts последнего залогированного набора голосов
        self._persist_task: asyncio.Task | None = None
        self.log_candles = True
        self._last_candle_log_ts: float = 0.0
        # --- 5m resample cache (trailing stop optimization) ---
        self._5m_cache: dict[str, list] = {}  # figi -> [resampled 5m candles]
        self._5m_last_close: dict[str, int] = {}  # figi -> last 5m close minute
        # --- Exit state per figi (единый учёт выхода, зеркалит движок runner) ---
        # Источник истины для SL/TP/trailing — ЭТИ поля, а не объект позиции брокера
        # (в sandbox/live pos.stop_loss/take_profit/entry_price приходят пустыми).
        self._exit_plans: dict[str, object] = {}   # figi -> ExitPolicy (AtrStopPolicy и т.п.)
        self._exit_side: dict[str, str] = {}       # figi -> "LONG" | "SHORT" (сторона нашего входа)
        self._exit_entry_px: dict[str, float] = {}  # figi -> реальная цена входа (штуки по факту)
        self._exit_qty: dict[str, int] = {}         # figi -> qty в штуках (для комиссии/активации)
        self._trail_active: dict[str, bool] = {}    # figi -> трейлинг активирован (pnl>=comm*4)
        self._trail_stop: dict[str, float] = {}     # figi -> актуальный защитный стоп (изначально = SL входа, потом подтягивается)
        self._exit_target: dict[str, float] = {}    # figi -> актуальный TP (None после активации трейлинга)
        # --- Broken candle validation (mirrors _validate_candles in ensemble.py) ---
        self._prev_close: dict[str, float] = {}  # figi -> last VALID close
        self._day_jumps: dict[str, dict[str, int]] = {}  # figi -> {msk_date: jump_count}
        self._bad_day: dict[str, dict[str, bool]] = {}  # figi -> {msk_date: is_bad}
        self._candles_rejected: int = 0  # total rejected broken candles
        # --- IMOEX guard: непрерывный ряд индекса + защита входов от всплесков ---
        self._imoex_buf: list[tuple[datetime, float]] = []  # [(ts, close)] 1м свечи IMOEX
        self._imoex_state = None  # ImoexGuardState (ленивая инициализация)
        self._imoex_last_tick: str = ""  # минута последнего пересчёта (YYYYMMDDHHMM)
        self._imoex_tick_err: str = ""
        self._imoex_beta: dict[str, float] = {}  # figi -> beta к IMOEX (instruments.imoex_beta)
        self._imoex_stale_warn_ts: float = 0.0  # throttle предупреждений об устаревании
        # --- AI-гейт: заявки на подтверждение входа (order_id -> monotonic) ---
        self._approvals_since: dict[str, float] = {}
        # --- HOLD после серии убытков (loss streak) ---
        self._loss_streak: dict[str, int] = {}          # figi -> подряд убытков
        self._last_loss_ts: dict[str, datetime] = {}    # figi -> время последнего убытка
        self._global_loss_streak: int = 0
        self._global_last_loss_ts: datetime | None = None
        # --- AI-гейт: пауза после отклонения (per-ticker) ---
        self._ai_reject_until: dict[str, datetime] = {}
        # --- AI-гейт: последние решения ИИ (shadow/боевые) для UI ---
        self._ai_decisions: deque = deque(maxlen=50)
        self._ai_prompt: dict = {}  # текущий промпт/модель AI-гейта (для UI)

    def _log(self, msg: str, level: str = "info", source: str = "bot") -> None:
        ts = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S")
        self._live_logs.append(f"[{ts}] {msg}")
        # Персистентная копия (level/source) — пишется в bot_logs флашером,
        # переживает рестарт и видна в Live через фильтры UI.
        self._log_persist_queue.append((level, source, f"[{ts}] {msg}"))


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

    def _is_closed(self, c) -> bool:
        """Только ЗАКРЫТАЯ свеча допускается к торговой логике и записи в БД.

        Метка бара ts — время ЗАКРЫТИЯ минутного интервала. Свеча считается
        закрытой, если now(UTC) >= ts (т.е. интервал уже завершился).
        Незакрытые/текущие обновления текущего бара отбрасываются.
        """
        try:
            now = datetime.now(timezone.utc)
            return now >= c.ts
        except Exception:
            return True

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
        """EnsembleParams из data/ensemble_config.json (UI-управляемый состав кворума).

        Состав голосов, quorum, neutral_mode, bias и режимные фильтры берутся из конфига.
        SL/TP (sl_mult/rr) — из optuna по инструменту.
        """
        from sqlalchemy import text as _t
        row = (await db.execute(
            _t("SELECT optuna_params FROM instruments WHERE figi = :f"), {"f": figi}
        )).first()
        opt = (row[0] if row else None) or {}

        from app.bot.ensemble_strategy import EnsembleParams
        ec = await load_ensemble_config()
        _setups = [
            {"strategy_id": s["strategy_id"], "tf": s.get("tf", "5min"), "params": s.get("params", {})}
            for s in ec.get("setups", []) if s.get("enabled")
        ]
        _bias = ec.get("bias") or {}
        return EnsembleParams(
            figi=figi, lot=int(lot) if lot else 10, capital=capital,
            quorum=int(ec.get("quorum", 2)), session="all", sessions=sessions,
            setups=_setups,
            sl_mult=(float(getattr(self.config, "ensemble_sl_mult", 0.0) or 0.0) or float(opt.get("sl_mult", 4.0))),
            rr=(float(getattr(self.config, "ensemble_rr", 0.0) or 0.0) or float(opt.get("rr", 4.0))),
            vol_thr=float(ec.get("vol_thr", 0.0) or 0.0),
            neutral_mode=str(ec.get("neutral_mode", "semi_flip")),
            entry_tf=str(getattr(self.config, "ensemble_entry_tf", "5min") or "5min"),
            entry_from_setups=bool(getattr(self.config, "ensemble_entry_from_setups", True)),
            entry_direction_sid=str(getattr(self.config, "ensemble_direction_sid", "") or ""),
            entry_confirm_closes=int(getattr(self.config, "entry_confirm_closes", 0) or 0),
            entry_confirm_closes_sides=list(getattr(self.config, "entry_confirm_closes_sides", None) or ["BUY"]),
            entry_macd_1m=True,
            bias_tf=str(_bias.get("tf", "hour")),
            bias_period=int(_bias.get("period", 50)),
            regime_setups_filter=ec.get("regime_setups_filter") or {},
            trade_regimes=list(getattr(self.config, "trade_regimes", []) or []),
            ml_filter=ec.get("ml_filter") or {},
        )

    async def reload_ensemble(self) -> int:
        """Пересобрать стратегии под новый конфиг кворума (data/ensemble_config.json)."""
        cfg = self.config
        if not (self.running or self.starting) or not getattr(cfg, "use_ensemble", False):
            return 0
        from app.bot.ensemble_strategy import EnsembleV4Strategy
        n = 0
        try:
            async with SessionLocal() as db:
                for figi, _old in list(self.strategies.items()):
                    try:
                        u = next((x for x in self.universe if x["figi"] == figi), {})
                        params = await self._build_ensemble_params(
                            db, figi, self.tickers.get(figi, u.get("ticker", "")),
                            u.get("lot_size", 10), cfg.ensemble_capital, cfg.sessions)
                        self.strategies[figi] = EnsembleV4Strategy(params)
                        n += 1
                    except Exception:
                        continue
            self._log(f"⚙ КОНФИГ КВОРУМА применён: пересобрано стратегий {n}")
        except Exception as e:
            self._log(f"⚙ КОНФИГ КВОРУМА: ошибка применения: {e}")
        return n

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
        _lot = 1
        try:
            from app.models.instrument import Instrument
            async with SessionLocal() as db2:
                _lot = int((await db2.execute(select(Instrument.lot).where(Instrument.figi == figi))).scalar_one_or_none() or 1)
        except Exception:
            pass
        qty_shares = int(qty) * _lot
        try:
            async with SessionLocal() as db:
                # Идемпотентность: один открытый ряд на figi. Если уже есть
                # открытая строка того же инструмента — сначала архивируем её
                # (защита от двойных записей при переоткрытии фиджи).
                _now = self._bot_now()
                prev = (await db.execute(
                    select(SandboxTrade)
                    .where(SandboxTrade.figi == figi, SandboxTrade.exit_time.is_(None))
                    .order_by(SandboxTrade.entry_time.desc())
                )).scalars().all()
                for _old in prev:
                    _old.exit_time = _now
                    _old.exit_price = float(_old.entry_price or 0.0)
                    _old.exit_reason = "reopened"
                    _old.net_pnl = 0.0
                    await db.flush()
                db.add(SandboxTrade(
                    figi=figi, ticker=ticker, side=side, qty=qty_shares,
                    entry_time=_now,
                    entry_price=float(price),
                    stop_loss=float(sl) if sl else None,
                    take_profit=float(tp) if tp else None,
                    entry_reason=(meta or {}).get("entry", {}).get("reason") if isinstance(meta, dict) else None,
                    meta=_json.dumps({**(meta or {}), "sl_initial": float(sl) if sl else None,
                                      "entry_price0": float(price)}, ensure_ascii=False, default=str),
                    leverage=float(leverage),
                    mode=self.broker_mode,
                    test_name=getattr(self.config, "test_name", "") or None,
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
                    .where(SandboxTrade.figi == figi, SandboxTrade.exit_time.is_(None),
                           SandboxTrade.mode == self.broker_mode)
                    .order_by(SandboxTrade.entry_time.desc())
                    .limit(1)
                )
                row = res.scalar_one_or_none()
                if row is not None:
                    row.exit_time = self._bot_now()
                    row.exit_price = float(exit_price)
                    row.exit_reason = reason or ""
                    # Диагностика стопов: начальный SL из БД (трейлинг его мог подтянуть),
                    # был ли активен трейлинг, цена входа — для проверки side/дистанции.
                    meta = dict(meta or {})
                    meta.setdefault("entry_price", float(row.entry_price))
                    meta.setdefault("sl_db", float(row.stop_loss) if row.stop_loss is not None else None)
                    meta.setdefault("trail_active", bool(self._trail_active.get(figi, False)))
                    row.exit_meta = _json.dumps(meta, ensure_ascii=False, default=str)
                    if net is not None:
                        row.net_pnl = float(net)
                    costs = CostModel(commission_rate=self.config.commission_rate, slippage_bps=self.config.slippage_bps)
                    entry_comm = costs.commission(float(row.entry_price) * int(row.qty))
                    exit_comm = costs.commission(float(exit_price) * int(row.qty))
                    row.commission = round(entry_comm + exit_comm, 4)
                    await db.commit()
                    # --- HOLD после серии убытков: обновляем счётчик ---
                    try:
                        _net = float(net) if net is not None else (
                            float(row.net_pnl) if row.net_pnl is not None else None)
                        self._update_loss_streak(figi, _net)
                    except Exception:
                        pass
        except Exception:
            pass

    async def _st_update_sl(self, figi: str, sl: float, trail_active: bool | None = None) -> None:
        from app.models.sandbox_trade import SandboxTrade
        try:
            async with SessionLocal() as db:
                res = await db.execute(
                    select(SandboxTrade)
                    .where(SandboxTrade.figi == figi, SandboxTrade.exit_time.is_(None),
                           SandboxTrade.mode == self.broker_mode)
                    .order_by(SandboxTrade.entry_time.desc())
                    .limit(1)
                )
                row = res.scalar_one_or_none()
                if row is not None:
                    if sl is not None:
                        row.stop_loss = float(sl)
                    if trail_active is not None:
                        row.trailing_active = bool(trail_active)
                        if trail_active:
                            row.take_profit = None  # TP выключается при активации трейлинга
                    await db.commit()
        except Exception:
            pass

    async def set_position_levels(self, figi: str, sl: float | None = None, tp: float | None = None) -> dict:
        """Ручная правка SL/TP позиции: in-memory учёт выхода + запись в БД.

        Сбрасывает трейлинг (trail_active=False), чтобы действовали ОБА уровня:
        SL защищает прибыль, TP — цель. Уровни переживают рестарт (читаются из БД).
        """
        from app.models.sandbox_trade import SandboxTrade
        from sqlalchemy import select as _sel
        try:
            pos = await self.broker.get_position(figi)
        except Exception:
            pos = None
        if pos is None:
            return {"ok": False, "error": "no_position"}
        if figi not in self._exit_plans:
            _buf = self.buffers.get(figi)
            _c = list(_buf)[-1] if _buf else None
            if _c is not None:
                await self._ensure_exit_state(figi, _c, pos)
        if figi not in self._exit_plans and figi not in self._trail_stop:
            return {"ok": False, "error": "no_exit_state"}
        if sl is not None:
            self._trail_stop[figi] = float(sl)
        if tp is not None:
            self._exit_target[figi] = float(tp)
        self._trail_active[figi] = False
        try:
            async with SessionLocal() as db:
                row = (await db.execute(
                    _sel(SandboxTrade).where(
                        SandboxTrade.figi == figi, SandboxTrade.exit_time.is_(None),
                        SandboxTrade.mode == self.broker_mode)
                    .order_by(SandboxTrade.entry_time.desc()).limit(1)
                )).scalar_one_or_none()
                if row is not None:
                    if sl is not None:
                        row.stop_loss = float(sl)
                    if tp is not None:
                        row.take_profit = float(tp)
                    row.trailing_active = False
                    await db.commit()
        except Exception as _e:
            self._log(f"SL/TP РУЧНО: ошибка записи в БД: {_e}")
        self._log(f"SL/TP РУЧНО {figi[-6:]}: sl={self._trail_stop.get(figi)} tp={self._exit_target.get(figi)}")
        self.events.log("SLTP_MANUAL", figi=figi,
                        sl=self._trail_stop.get(figi), tp=self._exit_target.get(figi))
        return {"ok": True, "figi": figi, "sl": self._trail_stop.get(figi), "tp": self._exit_target.get(figi)}

    async def state_snapshot(self) -> dict:
        """Компактный снапшот состояния для внешнего управления (ИИ/мониторинг).

        Позиции + последняя цена из буфера + SL/TP + P&L + дистанции до уровней
        + режим + алерты — одним вызовом, без походов в БД.
        """
        out: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode, "running": bool(self.running),
            "equity": None, "positions": [], "alerts": [],
            "imoex_guard": self._imoex_guard_snapshot(),
            "pending_approvals": self.list_approvals(),
        }
        try:
            out["equity"] = float(await self.broker.equity())
        except Exception:
            pass
        try:
            pos_list = await self.broker.positions()
        except Exception:
            pos_list = []
        for p in pos_list:
            figi = str(getattr(p, "figi", "") or "")
            bb = self.tcs_to_bbg.get(figi, figi)
            buf = self.buffers.get(bb) or self.buffers.get(figi)
            last = float(buf[-1].close) if buf else None
            entry = self._exit_entry_px.get(bb) or float(getattr(p, "entry_price", 0) or 0)
            side_raw = str(getattr(p, "side", "")).upper()
            long_ = side_raw in ("LONG", "BUY")
            qty = int(self._exit_qty.get(bb) or getattr(p, "qty", 0) or 0)
            sl = self._trail_stop.get(bb)
            tp = self._exit_target.get(bb)
            pnl = (last - entry) * qty * (1 if long_ else -1) if (last and entry) else None
            d_sl = abs(last - sl) / last * 100 if (last and sl) else None
            d_tp = abs(tp - last) / last * 100 if (last and tp) else None
            reg = (self._regimes.get(bb) or {}).get("state")
            reg_name = reg.get("state") if isinstance(reg, dict) else reg
            out["positions"].append({
                "figi": bb, "ticker": self.tickers.get(bb, getattr(p, "ticker", "") or bb[-6:]),
                "side": "LONG" if long_ else "SHORT", "qty": qty,
                "entry": entry, "last": last, "pnl": pnl,
                "sl": sl, "tp": tp,
                "dist_sl_pct": d_sl, "dist_tp_pct": d_tp,
                "regime": reg_name, "trail_active": bool(self._trail_active.get(bb)),
            })
        for pos in out["positions"]:
            for key, lbl in (("dist_sl_pct", "SL"), ("dist_tp_pct", "TP")):
                d = pos.get(key)
                if d is not None and d <= 0.7:
                    out["alerts"].append(f"{pos['ticker']}: {d:.2f}% до {lbl} (P&L {pos['pnl']:+.1f}₽)")
        return out

    async def _intrabar_exit_loop(self) -> None:
        """Быстрая (intrabar) проверка SL/TP между барами по последней цене брокера.

        Детерминированный слой защиты: не ждём закрытия 1м бара. Работает только
        при наличии учёта выхода (_exit_plans) и реальной позиции у брокера.
        """
        await asyncio.sleep(25.0)  # дать прогреться (warmup/восстановление позиций)
        while True:
            try:
                _sec = float(getattr(self.config, "intrabar_check_sec", 10.0) or 0.0)
                if _sec <= 0:
                    await asyncio.sleep(30.0)
                    continue
                await asyncio.sleep(max(2.0, _sec))
                if not self.running or not self._exit_plans:
                    continue
                _lp = getattr(self.broker, "last_prices", None)
                if _lp is None:
                    continue
                prices = await _lp(list(self._exit_plans.keys()))
                if not prices:
                    continue
                for figi, px in list(prices.items()):
                    try:
                        if not px or px <= 0:
                            continue
                        sl = self._trail_stop.get(figi)
                        tp = self._exit_target.get(figi)
                        if sl is None and tp is None:
                            continue
                        side = self._exit_side.get(figi) or "LONG"
                        long_ = str(side).upper() in ("LONG", "BUY")
                        hit_sl = bool(sl) and ((long_ and px <= sl) or (not long_ and px >= sl))
                        hit_tp = bool(tp) and ((long_ and px >= tp) or (not long_ and px <= tp))
                        if not (hit_sl or hit_tp):
                            continue
                        pos = await self.broker.get_position(figi)
                        if pos is None:
                            continue
                        reason = "intrabar_sl" if hit_sl else "intrabar_tp"
                        trade = await self.broker.close_position(figi, float(px), reason)
                        self._held.discard(figi)
                        self._opposite_count.pop(figi, None)
                        self._last_exit_bar[figi] = self._bar_counter
                        self._clear_exit_state(figi)
                        _bh = self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter)
                        if trade is not None:
                            await self._st_close(figi, float(px), reason=reason,
                                                 net=float(trade.net_pnl),
                                                 meta={"bars_held": _bh, "intrabar": True})
                            self._log(f"INTRABAR ВЫХОД {figi[-6:]} {reason} @ {px:.2f} "
                                      f"pnl={float(trade.net_pnl):+.2f}")
                            self.events.log("ORDER_FILLED", figi=figi, reason=reason,
                                            price=float(px), net_pnl=float(trade.net_pnl),
                                            intrabar=True)
                    except Exception as _ie:
                        self._log(f"intrabar {figi[-6:]}: {type(_ie).__name__}: {str(_ie)[:70]}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"intrabar-exit loop: {type(e).__name__}: {str(e)[:80]}")

    # --- IMOEX guard: непрерывный ряд индекса + запрет входов против всплеска ---

    async def _load_imoex_buf(self, lookback_min: int = 360) -> int:
        """Загрузить 1м свечи IMOEX из БД до текущего (виртуального в реплее) времени.

        В реплее грузим всё окно теста сразу (расчёт берёт только бары <= bot_now,
        поэтому будущие бары не видны — look-ahead исключён), иначе ряд не растёт
        по ходу виртуальных часов.
        """
        try:
            from sqlalchemy import text as _text
            from app.bot.moex import IMOEX_FIGI as _IF
            now = self._bot_now()
            _frm = now - timedelta(minutes=int(lookback_min))
            _upto = now
            _replay = getattr(self.config, "feed", "live") == "replay"
            if _replay:
                _re = getattr(self.config, "replay_end", "") or ""
                if _re:
                    try:
                        _upto = datetime.fromisoformat(_re.replace("Z", "+00:00"))
                        if _upto.tzinfo is None:
                            _upto = _upto.replace(tzinfo=timezone.utc)
                    except Exception:
                        _upto = now
                if _upto < now:
                    _upto = now
                _sql = ("SELECT ts, close FROM candles WHERE figi = :f AND interval = 1 "
                        "AND ts <= :upto AND ts >= :frm ORDER BY ts")
                _params = {"f": _IF, "upto": _upto, "frm": _frm}
            else:
                # Live: берём последние N баров ДО now (переживает выходные/праздники,
                # когда за последние часы свечей нет).
                _sql = ("SELECT ts, close FROM ("
                        "SELECT ts, close FROM candles WHERE figi = :f AND interval = 1 "
                        "AND ts <= :upto ORDER BY ts DESC LIMIT :n) t ORDER BY ts")
                _params = {"f": _IF, "upto": _upto, "n": int(lookback_min) + 30}
            async with SessionLocal() as db:
                rows = (await db.execute(_text(_sql), _params)).all()
            self._imoex_buf = [(r[0], float(r[1])) for r in rows]
            return len(self._imoex_buf)
        except Exception as e:
            self._log(f"⚠ IMOEX buf: {type(e).__name__}: {str(e)[:80]}")
            return 0

    def _imoex_move(self):
        """Ход индекса за окно (pts, pct) или None."""
        from app.bot.imoex_guard import move_at as _move_at
        return _move_at(self._imoex_buf, self._bot_now(),
                        int(getattr(self.config, "imoex_spike_window_min", 20) or 20))

    def _imoex_guard_tick(self) -> None:
        """Пересчитать состояние guard'а (вызывается на каждой новой минуте)."""
        cfg = self.config
        if not getattr(cfg, "imoex_guard", True):
            return
        try:
            mv = self._imoex_move()
            if mv is None:
                return
            pts, pct, _, _ = mv
            from app.bot.imoex_guard import ImoexGuardState as _IGS, step as _step
            if self._imoex_state is None:
                self._imoex_state = _IGS()
            on_pct = float(getattr(cfg, "imoex_spike_pct", 0.8) or 0.8)
            on_pts = float(getattr(cfg, "imoex_spike_points", 0.0) or 0.0)
            rel_frac = float(getattr(cfg, "imoex_release_frac", 0.5) or 0.5)
            ev = _step(
                self._imoex_state, pts, pct, self._bot_now(),
                on_pct=on_pct, off_pct=on_pct * rel_frac,
                on_points=on_pts, off_points=(on_pts * rel_frac) if on_pts > 0 else 0.0,
                min_block_min=float(getattr(cfg, "imoex_min_block_min", 5.0) or 0.0),
            )
            _win = int(getattr(cfg, "imoex_spike_window_min", 20) or 20)
            if ev == "activate_up":
                self._log(f"IMOEX GUARD: всплеск ВВЕРХ {pts:+.1f}п ({pct:+.2f}% за {_win}м) — SELL-входы запрещены")
                self.events.log("IMOEX_GUARD", reason="activate_up",
                                move_pts=round(pts, 1), move_pct=round(pct, 2))
            elif ev == "activate_down":
                self._log(f"IMOEX GUARD: всплеск ВНИЗ {pts:+.1f}п ({pct:+.2f}% за {_win}м) — BUY-входы запрещены")
                self.events.log("IMOEX_GUARD", reason="activate_down",
                                move_pts=round(pts, 1), move_pct=round(pct, 2))
            elif ev == "release":
                self._log(f"IMOEX GUARD: стабилизация {pts:+.1f}п ({pct:+.2f}%) — входы разрешены")
                self.events.log("IMOEX_GUARD", reason="release",
                                move_pts=round(pts, 1), move_pct=round(pct, 2))
        except Exception as e:
            _err = f"{type(e).__name__}: {str(e)[:80]}"
            if _err != self._imoex_tick_err:
                self._imoex_tick_err = _err
                self._log(f"⚠ IMOEX guard: {_err}")

    def _imoex_block_reason(self, side: str, figi: str | None = None) -> str | None:
        """Причина блокировки входа против/вослед всплеска индекса, или None."""
        st = self._imoex_state
        if st is None or not getattr(self.config, "imoex_guard", True):
            return None
        from app.bot.imoex_guard import block_for as _block_for
        _beta = self._imoex_beta.get(figi) if figi else None
        return _block_for(
            st, side,
            beta=_beta,
            min_beta=float(getattr(self.config, "imoex_guard_min_beta", 0.0) or 0.0),
            chase_pct=float(getattr(self.config, "imoex_chase_block_pct", 0.0) or 0.0),
        )

    def _imoex_guard_snapshot(self) -> dict:
        st = self._imoex_state
        cfg = self.config
        last_ts = self._imoex_buf[-1][0] if self._imoex_buf else None
        now = self._bot_now()
        try:
            age_sec = (now - last_ts).total_seconds() if last_ts else None
        except Exception:
            age_sec = None
        _trading = False
        try:
            from app.bot.imoex_guard import imoex_session as _isess
            _trading = _isess(now)  # IMOEX живёт только 09:50–19:00 МСК
        except Exception:
            pass
        _stale_sec = float(getattr(cfg, "imoex_stale_sec", 300.0) or 300.0)
        _stale = bool(_trading and (age_sec is None or age_sec > _stale_sec))
        return {
            "enabled": bool(getattr(cfg, "imoex_guard", True)),
            "active": int(st.active) if st else 0,
            "since": st.since.isoformat() if (st is not None and st.since) else None,
            "move": round(st.move, 2) if st else 0.0,
            "pct": round(st.pct, 3) if st else 0.0,
            "blocks": int(st.blocks) if st else 0,
            "activations": int(st.activations) if st else 0,
            "releases": int(st.releases) if st else 0,
            "window_min": int(getattr(cfg, "imoex_spike_window_min", 20) or 20),
            "on_pct": float(getattr(cfg, "imoex_spike_pct", 0.8) or 0.8),
            "on_points": float(getattr(cfg, "imoex_spike_points", 0.0) or 0.0),
            "min_beta": float(getattr(cfg, "imoex_guard_min_beta", 0.0) or 0.0),
            "chase_pct": float(getattr(cfg, "imoex_chase_block_pct", 0.0) or 0.0),
            "last_candle": last_ts.isoformat() if last_ts else None,
            "age_sec": round(age_sec, 1) if age_sec is not None else None,
            "stale": _stale,
            "trading": bool(_trading),
            "index_session": bool(_trading),
        }

    async def _imoex_loop(self) -> None:
        """Live: периодически догружает 1м свечи IMOEX (MOEX ISS) и обновляет ряд guard'а.

        В реплее данные уже в БД, часы виртуальные — догрузка не нужна.
        """
        await asyncio.sleep(5.0)
        while True:
            try:
                _sec = float(getattr(self.config, "imoex_refresh_sec", 60.0) or 60.0)
                _replay = getattr(self.config, "feed", "live") == "replay"
                if getattr(self.config, "imoex_guard", True) and not _replay:
                    try:
                        from app.bot.moex import sync_imoex_recent
                        _n = await asyncio.to_thread(sync_imoex_recent, max(30, int(_sec) + 30))
                        if _n:
                            self.metrics["last_imoex_sync"] = {
                                "rows": int(_n), "ts": datetime.now(timezone.utc).isoformat()}
                    except Exception as e:
                        self._log(f"⚠ IMOEX sync: {type(e).__name__}: {str(e)[:80]}")
                    await self._load_imoex_buf()
                    self._imoex_guard_tick()
                    # Оперативный алерт: свечи IMOEX не обновляются в торговую сессию.
                    _snap = self._imoex_guard_snapshot()
                    if _snap.get("stale"):
                        _noww = _time.monotonic()
                        if _noww - self._imoex_stale_warn_ts > 600:
                            self._imoex_stale_warn_ts = _noww
                            _age_min = int((_snap.get("age_sec") or 0) // 60)
                            self._log(f"⚠ IMOEX: свечи НЕ ОБНОВЛЯЮТСЯ — последняя {_snap.get('last_candle')} "
                                      f"(возраст {_age_min} мин). Входы против направления не защищены!")
                            self.events.log("IMOEX_STALE", last_candle=_snap.get("last_candle"),
                                            age_sec=_snap.get("age_sec"))
                await asyncio.sleep(max(10.0, _sec))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"imoex-loop: {type(e).__name__}: {str(e)[:80]}")
                await asyncio.sleep(30.0)

    @property
    def status(self) -> dict:
        step = STEP_SEC.get(self.config.interval_name, 300)
        if self.last_candle_ts is None:
            health = "NO_DATA"
        elif self._bot_now() - self.last_candle_ts <= timedelta(seconds=3 * step):
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
                "margin_sessions": list(self.config.margin_sessions),
                "margin_leverage": float(self.config.margin_leverage or 0.0),
                "margin_sizing": self.config.margin_sizing,
                "trade_regimes": list(self.config.trade_regimes),
                "trend_alignment": bool(self.config.trend_alignment),
                "trail_distance_atr": self.config.trail_distance_atr,
                "trail_compress_r": self.config.trail_compress_r,
                "trail_min_factor": self.config.trail_min_factor,
                "trail_min_atr": self.config.trail_min_atr,
                "trail_vol_boost": self.config.trail_vol_boost,
                "commission_rate": self.config.commission_rate,
                "slippage_bps": self.config.slippage_bps,
                "confirm_flip": self.config.confirm_flip,
                "reentry_cooldown_bars": self.config.reentry_cooldown_bars,
                "overnight": self.config.overnight,
                "feed": self.config.feed,
                "replay_start": self.config.replay_start,
                "replay_end": self.config.replay_end,
                "replay_pace": self.config.replay_pace,
                "test_name": self.config.test_name,
            },
            "universe": self.universe,
            "votes": [
                {"figi": _f, "ticker": self.tickers.get(_f, _f[:6]),
                 "buy": int(_v.get("buy", 0)), "sell": int(_v.get("sell", 0)),
                 "votes": max(int(_v.get("buy", 0)), int(_v.get("sell", 0))),
                 "side": "BUY" if int(_v.get("buy", 0)) >= int(_v.get("sell", 0)) else "SELL",
                 "ts": _v.get("ts"),
                 "regime": ((self._regimes.get(_f) or {}).get("state") or {}).get("state")
                            if isinstance((self._regimes.get(_f) or {}).get("state"), dict) else None,
                 "vol": (self._regimes.get(_f) or {}).get("vol"),
                 "vol_abs": (float(self.buffers[_f][-1].volume or 0) if self.buffers.get(_f) else None)}
                for _f, _v in self._votes.items()
            ],
            "candles_seen": self.candles_seen,
            "signals_seen": self.signals_seen,
            "pending_orders": len(self.pending_orders),
            "entries_paused": self.entries_paused,
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
            "carousel": self.carousel_diag,
            "imoex_guard": self._imoex_guard_snapshot(),
            "loss_streak": self.loss_streak_snapshot(),
            "ai_approval": {
                "enabled": bool(getattr(self.config, "ai_approval", False)),
                "pending": len(self.list_approvals()),
                "timeout_sec": float(getattr(self.config, "ai_approval_timeout_sec", 45.0) or 45.0),
                "default": str(getattr(self.config, "ai_approval_default", "approve") or "approve"),
            },
            "ai_decisions": self.list_ai_decisions(8),
            "metrics": dict(self.metrics),
        }

    def risk_snapshot(self) -> RiskSnapshot:
        return RiskSnapshot(
            daily_pnl=self.daily_pnl_cached(),
            daily_loss_limit=self.config.daily_loss_limit,
            entries_paused=self.entries_paused,
        )

    def _bot_now(self) -> datetime:
        """Виртуализированные часы бота.

        В live/sandbox/paper возвращает реальное wall-clock время UTC.
        В ReplayFeed — виртуальное время реплея (начало окна до первой свечи,
        затем ts последней поданной свечи). Все решения, зависящие от «текущего
        момента» (сессионный гейт, свежесть бара, дневной PnL), должны ходить
        сюда, чтобы реплей воспроизводил поведение 1-в-1.
        """
        if self._replay_cur is not None:
            return self._replay_cur
        if self._replay_from is not None:
            return self._replay_from
        return datetime.now(timezone.utc)

    def daily_pnl_cached(self) -> float:
        now = self._bot_now()
        if self._daily_pnl_cache and now - self._daily_pnl_cache[0] < DAILY_PNL_TTL:
            return self._daily_pnl_cache[1]
        self._daily_pnl_cache = (now, 0.0)
        return self._daily_pnl_cache[1]

    async def refresh_daily_pnl(self) -> float:
        msk_now = self._bot_now().astimezone(timezone(timedelta(hours=3)))
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
        # Восстанавливаем сохранённые настройки (переживают перезапуск/старт без фронта).
        try:
            _saved = await load_bot_settings()
            if _saved:
                for _f in BOT_PERSIST_FIELDS:
                    if _f in _saved:
                        try:
                            setattr(cfg, _f, _saved[_f])
                        except Exception:
                            pass
        except Exception:
            pass
        # Восстанавливаем персистентные runtime-флаги (entries_paused переживает рестарт).
        try:
            _flags = await load_bot_flags()
            self.entries_paused = bool(_flags.get("entries_paused", False))
            if self.entries_paused:
                self._log("ВОССТАНОВЛЕНО: пауза новых входов (entries_paused=true) сохранена между рестартами")
        except Exception:
            pass
        if cfg.use_ensemble:
            cfg.interval_name = "1min"
        # --- Replay: стартуем виртуальные часы с начала окна (до первой свечи). ---
        self._replay_from = None
        self._replay_cur = None
        if cfg.feed == "replay":
            try:
                self._replay_from = datetime.fromisoformat(
                    cfg.replay_start.replace("Z", "+00:00")
                )
                if self._replay_from.tzinfo is None:
                    self._replay_from = self._replay_from.replace(tzinfo=timezone.utc)
            except Exception:
                self._replay_from = None
                self._log(f"⚠ REPLAY: не удалось распарсить replay_start={cfg.replay_start!r}")
            if self._replay_from is not None:
                self._log(f"REPLAY START: окно с {self._replay_from.isoformat()} pacing={cfg.replay_pace}")
        self._log(
            f"НАСТРОЙКИ: торги={'/'.join(cfg.sessions) or '—'} · "
            f"маржа={'/'.join(cfg.margin_sessions) or '—'} "
            f"(плечо {'Max' if float(cfg.margin_leverage or 0) <= 0 else '×'+format(float(cfg.margin_leverage),'g')}) · "
            f"long={'да' if cfg.long_allowed else 'нет'} short={'да' if cfg.short_allowed else 'нет'}"
        )
        try:
            from sqlalchemy import text as _text
            async with SessionLocal() as _db:
                await _db.execute(_text("ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS entry_reason VARCHAR(128)"))
                await _db.execute(_text("ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS meta TEXT"))
                await _db.execute(_text("ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS exit_meta TEXT"))
                await _db.execute(_text("ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS trailing_active BOOLEAN DEFAULT FALSE"))
                await _db.execute(_text("ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS mode VARCHAR(8) DEFAULT 'sandbox'"))
                await _db.execute(_text("ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS test_name VARCHAR(64)"))
                await _db.execute(_text("CREATE INDEX IF NOT EXISTS ix_sandbox_trades_test_name ON sandbox_trades (test_name)"))
                await _db.execute(_text("CREATE TABLE IF NOT EXISTS bot_logs (id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), level VARCHAR(8) DEFAULT 'info', source VARCHAR(16) DEFAULT 'bot', msg TEXT)"))
                await _db.commit()
            # Восстановить хвост live-логов из bot_logs (переживают рестарт; Live видит
            # постоянные логи через фильтры UI, а не только deque in-memory).
            try:
                _hd = await _db.execute(
                    _text("SELECT to_char(ts AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD HH24:MI:SS') || ' ' || msg "
                          "FROM (SELECT ts, msg FROM bot_logs ORDER BY id DESC LIMIT 400) t ORDER BY id")
                )
                self._live_logs.extend(_hd.scalars().all())
            except Exception:
                pass
        except Exception:
            pass
        self.error = None
        self.candles_seen = 0
        self.signals_seen = 0
        self.metrics = {
            "started_ts": None,
            "bar_ms_total": 0.0, "bar_ms_n": 0, "bar_ms_max": 0.0,
            "ensemble_ms_total": 0.0, "ensemble_ms_n": 0, "ensemble_ms_max": 0.0,
            "persist_ms_total": 0.0, "persist_ms_n": 0, "persist_ms_max": 0.0,
            "last_alert": None,
            "alerts": [],
        }
        self.strategies = {}
        self.buffers = {}
        self.pending_orders = {}
        self.tickers = {}
        self.entries_paused = False
        self._imoex_state = None  # счётчики guard'а — с чистого листа на каждый запуск
        self.carousel_diag = {
            "eligible_count": 0, "active_count": 0, "hot_adds": 0,
            "hot_skips": 0, "last_hot_add_ts": None, "last_error": None, "instruments": [],
        }
        self.last_candle_ts = None
        # Реплей всегда на paper-брокере: исторические бары невозможны ни в sandbox
        # (не принимает заявки), ни в live. Сделки реплея пишутся в sandbox_trades
        # с mode='paper' — не смешиваются с реальными sandbox/live.
        self.broker_mode = cfg.mode
        if cfg.feed == "replay" or (cfg.mode != "sandbox" and cfg.mode != "live"):
            self.broker_mode = "paper"
            self.broker = PaperBroker(SessionLocal)
        else:
            self.broker = LiveBroker(SessionLocal, config=cfg)
            # --- StreamManager: subscribe to server streams ---
            from app.config import get_settings
            _s = get_settings()
            _TOKEN = _s.get_token(cfg.mode)
            _ACC = _s.get_account(cfg.mode)
            _SB = _s.get_target(cfg.mode)
            target = _SB if cfg.mode == "sandbox" else None
            self.stream_manager = StreamManager(token=_TOKEN, account_id=_ACC, target=target)
            try:
                await self.stream_manager.start()
                self._log("STREAM MANAGER запущен (positions + trades + orders)")
            except Exception as e:
                self._log(f"STREAM MANAGER не запущен: {str(e)[:80]}")
                self.stream_manager = None
            try:
                # Капитал = equity (ликвидный портфель), НЕ свободные деньги:
                # деньги на счёте искажены шортами/позициями, equity — реальная стоимость.
                real_cash = await self.broker.equity()
                if real_cash and real_cash > 0:
                    cfg.initial_cash = real_cash
                    cfg.ensemble_capital = real_cash * POS_PCT
                    self._log(f"КАПИТАЛ со счёта (equity): {real_cash:.0f} ₽ · позиция до {cfg.ensemble_capital:.0f} ({POS_PCT*100:.0f}%)")
            except Exception as e:
                self._log(f"КАПИТАЛ не получен: {str(e)[:80]}")
        # Реплей: детерминированный старт — чистая paper-книга и чистые записи
        # предыдущего прогона. Если задан test_name — чистим только его сделки
        # (пересоздаём конкретный тест), иначе — все mode='paper' (лед legacy реплеи).
        if cfg.feed == "replay":
            try:
                await self.broker.reset(cfg.initial_cash)
                from sqlalchemy import delete as _delete
                from app.models.sandbox_trade import SandboxTrade
                async with SessionLocal() as db:
                    _q = _delete(SandboxTrade).where(SandboxTrade.mode == "paper")
                    if cfg.test_name:
                        _q = _q.where(SandboxTrade.test_name == cfg.test_name)
                    await db.execute(_q)
                    await db.commit()
                self._log(f"REPLAY RESET: чистая бумажная книга ({cfg.initial_cash:.0f} ₽)"
                          + (f" — тест {cfg.test_name!r}" if cfg.test_name else ""))
            except Exception as e:
                self._log(f"REPLAY RESET FAIL: {type(e).__name__}: {str(e)[:80]}")
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
                    # Берём ВСЕ eligible-тикеры сразу (без ограничения top_n),
                    # чтобы не было «горячего» добора через HOT-ADD.
                    self.universe = await select_eligible_universe(db, top_n=9999)
                else:
                    self.universe = await select_volatile_universe(
                        db, figi_by_ticker, top_n=cfg.top_n
                    )
            if not self.universe:
                raise RuntimeError("universe is empty")
            # Лог отбора: сколько тикеров и с какой волатильностью (ATR%).
            try:
                _atrs = [u.get("atr_pct", 0) for u in self.universe if u.get("atr_pct")]
                if _atrs:
                    self._log(f"UNIVERSE: {len(self.universe)} тикеров · ATR% {min(_atrs):.3f}–{max(_atrs):.3f} · "
                              f"{', '.join(u.get('ticker', '?') for u in self.universe)}")
            except Exception:
                pass
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
            # Инициализируем диагностику карусели
            self.carousel_diag["eligible_count"] = len(self.stream_universe)
            self.carousel_diag["active_count"] = len(self.universe)

            from app.engine.models import Candle as EC

            async def _load_one(u):
                try:
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
                                                date_from=self._bot_now() - timedelta(days=3),
                                                date_to=self._bot_now())
                            for row in candles:
                                buf.append(EC(ts=row.ts, open=row.open, high=row.high,
                                              low=row.low, close=row.close, volume=row.volume))
                        else:
                            from app.services.candle_cache import ensure_candles
                            await ensure_candles(db, u["figi"], cfg.interval_name, days=7)
                            interval_value = self._interval_value()
                            candles = await _lc(db, u["figi"], interval_value,
                                                date_from=self._bot_now() - timedelta(days=3))
                            for row in candles[-MAX_BUFFER:]:
                                buf.append(EC(ts=row.ts, open=row.open, high=row.high,
                                              low=row.low, close=row.close, volume=row.volume))
                    return u["figi"], proto, buf
                except Exception as e:
                    self._log(f"⚠ LOAD-ERROR {u.get('ticker', u['figi'][-6:])}: {str(e)[:120]}")
                    return None

            sem = asyncio.Semaphore(3)

            async def _load_bounded(u):
                async with sem:
                    return await _load_one(u)

            # IMOEX (индекс): всегда держим свежие 1м свечи в БД (режим/bias/MOEX-контекст)
            try:
                from app.bot.moex import ensure_imoex_candles
                _n_imoex = await ensure_imoex_candles(days=10)
                if _n_imoex:
                    self._log(f"IMOEX: догружено {_n_imoex} 1м свечей")
            except Exception as e:
                self._log(f"⚠ IMOEX load: {str(e)[:100]}")
            # IMOEX guard: загрузить ряд индекса и посчитать состояние всплеска.
            try:
                _n_buf = await self._load_imoex_buf()
                self._imoex_last_tick = ""
                self._imoex_guard_tick()
                if _n_buf:
                    self._log(f"IMOEX GUARD: ряд {_n_buf} свечей загружен ({self._imoex_guard_snapshot()})")
            except Exception as e:
                self._log(f"⚠ IMOEX guard init: {str(e)[:100]}")
            # Beta бумаг к IMOEX (instruments.imoex_beta) — для per-ticker режима guard'а.
            try:
                from sqlalchemy import text as _textb
                async with SessionLocal() as _dbb:
                    _brows = (await _dbb.execute(_textb(
                        "SELECT figi, imoex_beta FROM instruments WHERE imoex_beta IS NOT NULL"
                    ))).all()
                self._imoex_beta = {r[0]: float(r[1]) for r in _brows}
                if self._imoex_beta:
                    self._log(f"IMOEX GUARD: beta загружена для {len(self._imoex_beta)} бумаг")
            except Exception as e:
                self._log(f"⚠ IMOEX beta load: {str(e)[:100]}")

            results = await asyncio.gather(*[_load_bounded(u) for u in self.universe])
            for res in results:
                if res is None:
                    continue
                figi, proto, buf = res
                self.strategies[figi] = proto
                self.buffers[figi] = buf
            if not self.strategies:
                raise RuntimeError("no strategies loaded (all tickers failed)")
            self.universe = [u for u in self.universe if u["figi"] in self.strategies]

            try:
                held_now = await self.broker.positions()
                self._held = {p.figi for p in held_now}
                import time as _t0
                for _hf in self._held:
                    self._held_since[_hf] = _t0.monotonic()
                if self._held:
                    self._log(f"ОТКРЫТО при старте: {len(self._held)} поз.")
                    # Реставрация exit-состояния после перезагрузки из sandbox_trades:
                    # SL/TP/трейлинг/цена входа переживают рестарт (источник — БД, не брокер).
                    from app.models.sandbox_trade import SandboxTrade as _STM
                    _open_rows: dict[str, _STM] = {}
                    try:
                        async with SessionLocal() as _db_r:
                            _rows_r = (await _db_r.execute(
                                select(_STM)
                                .where(_STM.exit_time.is_(None))
                                .order_by(_STM.entry_time.desc())
                            )).scalars().all()
                        for _r in _rows_r:
                            _open_rows.setdefault(_r.figi, _r)
                    except Exception as _re_err:
                        self._log(f"restore rows error: {_re_err}")
                    from app.engine.exits import AtrStopPolicy, FixedSlTpPolicy
                    for _p in held_now:
                        _f = _p.figi
                        try:
                            if _p.side in ("LONG", "BUY"):
                                _st = Side.BUY
                                _side_str = "LONG"
                            elif _p.side in ("SHORT", "SELL"):
                                _st = Side.SELL
                                _side_str = "SHORT"
                            else:
                                continue
                            _row = _open_rows.get(_f)
                            _entry_px = float(_row.entry_price) if (_row is not None and _row.entry_price) else float(_p.entry_price or 0)
                            _strat_r = self.strategies.get(_f)
                            _rr_r = getattr(_strat_r.p, "rr", None) if _strat_r is not None else None
                            if cfg.sl_mode == "fixed":
                                _pol = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
                                _pl = _pol.plan_entry(_st, _entry_px, [])
                            else:
                                _pol = AtrStopPolicy(period=cfg.atr_period,
                                                     multiplier=cfg.initial_sl_atr,
                                                     risk_reward=float(_rr_r) if _rr_r else cfg.atr_risk_reward,
                                                     trail_activation_comm_mult=cfg.trail_activation_comm_mult,
                                                     trail_distance_r=cfg.trail_distance_atr,
                                                     trail_compress_r=cfg.trail_compress_r,
                                                     trail_min_factor=cfg.trail_min_factor,
                                                     trail_min_atr=cfg.trail_min_atr,
                                                     trail_vol_boost=cfg.trail_vol_boost)
                                _buf_raw = list(self.buffers.get(_f, [])) or None
                                _pl = _pol.plan_entry(_st, _entry_px, _buf_raw or [])
                            # Сохранённые уровни важнее пересчитанного плана:
                            # трейлинг мог уже подтянуть стоп / отключить TP.
                            _sl = float(_row.stop_loss) if (_row is not None and _row.stop_loss) else (
                                float(_pl.stop_loss) if _pl.stop_loss is not None else None)
                            _trail_was = bool(_row.trailing_active) if _row is not None else False
                            _tp = (float(_row.take_profit) if (_row is not None and _row.take_profit) else
                                   (float(_pl.take_profit) if _pl.take_profit is not None else None))
                            if _trail_was:
                                _tp = None  # TP выключен после активации трейлинга
                            self._exit_plans[_f] = _pol
                            self._exit_side[_f] = _side_str
                            self._exit_entry_px[_f] = _entry_px
                            self._exit_qty[_f] = int(abs(getattr(_p, "qty", 0) or 0))
                            self._trail_active[_f] = _trail_was
                            self._trail_stop[_f] = float(_sl) if _sl is not None else 0.0
                            if _tp is not None:
                                self._exit_target[_f] = _tp
                            self._entry_bar_index[_f] = 0
                            self._log(f"ВЫХОД ВОССТАНОВЛЕН {_f[-6:]} {_p.side} entry={_entry_px:.2f} sl={self._trail_stop[_f]:.2f} trail={_trail_was}")
                        except Exception as _restore_e:
                            self._log(f"restore trailing {_f[-6:]} error: {_restore_e}")
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
        await save_bot_flags({"entries_paused": paused})
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
            self._approvals_since.pop(order.id, None)
            self.events.log("ORDER_CANCELLED", figi=figi, ticker=order.ticker,
                            reason="manual", order_id=order.id)
        return {"cancelled": len(cancelled), "orders": cancelled}

    # --- AI-гейт: ожидающие входы и решения (approve/reject) ---

    # --- HOLD после серии убытков ---

    def _update_loss_streak(self, figi: str, net: float | None) -> None:
        """Обновить счётчик серии убытков по закрытой сделке (вызывается из _st_close)."""
        if net is None:
            return
        now = self._bot_now()
        if net < 0:
            self._loss_streak[figi] = self._loss_streak.get(figi, 0) + 1
            self._last_loss_ts[figi] = now
            self._global_loss_streak += 1
            self._global_last_loss_ts = now
            _n = max(2, int(getattr(self.config, "loss_streak_n", 2) or 2))
            if self._loss_streak[figi] >= _n and bool(getattr(self.config, "loss_streak_hold", True)):
                self._log(f"HOLD: {self.tickers.get(figi, figi[-6:])} — {self._loss_streak[figi]} убытка подряд, "
                          f"входы на паузе {float(getattr(self.config, 'loss_streak_hold_min', 60.0) or 0):.0f} мин")
                self.events.log("LOSS_STREAK_HOLD", figi=figi, ticker=self.tickers.get(figi, ""),
                                streak=self._loss_streak[figi])
        else:
            self._loss_streak[figi] = 0
            self._global_loss_streak = 0

    def _loss_streak_block(self, figi: str) -> tuple[bool, str]:
        """Активна ли пауза входов после серии убытков (per-ticker или global)."""
        cfg = self.config
        if not getattr(cfg, "loss_streak_hold", False):
            return (False, "")
        n = max(2, int(getattr(cfg, "loss_streak_n", 2) or 2))
        mins = float(getattr(cfg, "loss_streak_hold_min", 60.0) or 0.0)
        scope = str(getattr(cfg, "loss_streak_scope", "ticker") or "ticker")
        if scope == "global":
            cnt, ts = self._global_loss_streak, self._global_last_loss_ts
        else:
            cnt, ts = self._loss_streak.get(figi, 0), self._last_loss_ts.get(figi)
        left = _loss_hold_left(self._bot_now(), cnt, ts, n, mins)
        if left > 0:
            return (True, f"{cnt} убытков подряд, пауза ещё {left:.0f} мин")
        return (False, "")

    def loss_streak_snapshot(self) -> dict:
        """Активные HOLD-паузы (для UI/AI-гейта)."""
        out = []
        for f, cnt in self._loss_streak.items():
            _ok, why = self._loss_streak_block(f)
            if _ok:
                out.append({"figi": f, "ticker": self.tickers.get(f, f[-6:]),
                            "count": cnt, "why": why})
        return {"holds": out, "global_streak": self._global_loss_streak}

    def list_approvals(self) -> list[dict]:
        """Заявки на вход, ожидающие решения (order_id, тикер, сторона, qty, ожидание, meta)."""
        out = []
        for _figi, o in list(self.pending_orders.items()):
            if o.status != "PENDING_APPROVAL":
                continue
            d = o.to_dict()
            d["meta"] = o.meta
            d["waiting_sec"] = round(_time.monotonic() - self._approvals_since.get(o.id, _time.monotonic()), 1)
            out.append(d)
        return out

    def add_ai_decision(self, payload: dict) -> dict:
        """Записать решение AI-гейта (в т.ч. shadow) — для UI и истории."""
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "order_id": str(payload.get("order_id") or ""),
            "ticker": str(payload.get("ticker") or ""),
            "side": str(payload.get("side") or ""),
            "qty": payload.get("qty"),
            "decision": str(payload.get("decision") or ""),
            "reason": str(payload.get("reason") or "")[:300],
            "advice": str(payload.get("advice") or "")[:300],
            "confidence": payload.get("confidence"),
            "model": str(payload.get("model") or ""),
            "latency_ms": payload.get("latency_ms"),
            "shadow": bool(payload.get("shadow", False)),
        }
        self._ai_decisions.append(rec)
        self.events.log("AI_DECISION", figi=payload.get("figi"), ticker=rec["ticker"],
                        decision=rec["decision"], shadow=rec["shadow"], reason=rec["reason"][:120])
        return {"ok": True, "record": rec}

    def list_ai_decisions(self, limit: int = 20) -> list[dict]:
        return list(self._ai_decisions)[-max(1, min(int(limit), 50)):]

    def set_ai_prompt(self, payload: dict) -> dict:
        """Сохранить текущий промпт/конфиг AI-гейта (воркер присылает при старте)."""
        self._ai_prompt = {
            "updated_ts": datetime.now(timezone.utc).isoformat(),
            "provider": str(payload.get("provider") or ""),
            "model": str(payload.get("model") or ""),
            "shadow": bool(payload.get("shadow", False)),
            "system": str(payload.get("system") or ""),
            "context_schema": payload.get("context_schema"),
        }
        return {"ok": True, "updated_ts": self._ai_prompt["updated_ts"]}

    def get_ai_prompt(self) -> dict:
        return self._ai_prompt or {}

    def approve_order(self, order_id: str, reason: str = "") -> dict:
        """Одобрить ожидающий вход (исполнится на следующем баре)."""
        for o in list(self.pending_orders.values()):
            if o.id == order_id and o.status == "PENDING_APPROVAL":
                o.status = "APPROVED"
                o.meta = {**(o.meta or {}), "ai_decision": "approve", "ai_reason": reason}
                self._log(f"AI-ГЕЙТ: вход {o.ticker} {o.side} ОДОБРЕН ({reason or 'без причины'})")
                self.events.log("AI_APPROVAL_APPROVED", figi=o.figi, ticker=o.ticker,
                                order_id=o.id, reason=reason)
                return {"ok": True, "order_id": order_id, "status": "APPROVED"}
        return {"ok": False, "error": "order not found or not pending approval"}

    def reject_order(self, order_id: str, reason: str = "") -> dict:
        """Отклонить ожидающий вход (заявка снимается)."""
        for figi, o in list(self.pending_orders.items()):
            if o.id == order_id and o.status == "PENDING_APPROVAL":
                o.status = "REJECTED"
                o.meta = {**(o.meta or {}), "ai_decision": "reject", "ai_reason": reason}
                self._approvals_since.pop(o.id, None)
                del self.pending_orders[figi]
                _cd = float(getattr(self.config, "ai_reject_cooldown_min", 15.0) or 0.0)
                if _cd > 0:
                    self._ai_reject_until[figi] = self._bot_now() + timedelta(minutes=_cd)
                self._log(f"AI-ГЕЙТ: вход {o.ticker} {o.side} ОТКЛОНЁН — {reason or 'без причины'}"
                          + (f" (пауза входов {_cd:.0f} мин)" if _cd > 0 else ""))
                self.events.log("AI_APPROVAL_REJECTED", figi=figi, ticker=o.ticker,
                                order_id=o.id, reason=reason)
                return {"ok": True, "order_id": order_id, "status": "REJECTED",
                        "cooldown_min": _cd}
        return {"ok": False, "error": "order not found or not pending approval"}

    async def close_all(self) -> dict:
        positions = await self.broker.positions()
        closed = []
        for p in positions:
            buf = self.buffers.get(p.figi)
            price = float(buf[-1].close) if buf else float(p.entry_price)
            trade = await self.broker.close_position(p.figi, price, "kill_switch_close_all")
            self._held.discard(p.figi)
            self._clear_exit_state(p.figi)
            self._entry_bar_index.pop(p.figi, None)
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
                import time as _t
                real = await self.broker.positions()
                real_set = {p.figi for p in real}
                now = _t.monotonic()
                for f in real_set:
                    self._held_since.setdefault(f, now)
                stale = self._held - real_set
                for f in stale:
                    # Grace: не сноси сразу открытую позицию — лаг видимости в positions()
                    # (позиция может «не успеть» появиться до ~40с после входа).
                    opened_at = self._held_since.get(f, 0.0)
                    if now - opened_at < 90.0 and opened_at > 0.0:
                        continue
                    real_f = await self.broker.get_position(f)
                    if real_f is not None:
                        continue  # позиция реально есть, просто не попала в этот снапшот
                    self._held.discard(f)
                    self._held_since.pop(f, None)
                    self._log(f"♻ ОЧИСТКА _held: {f[-6:]} (нет в портфеле)")
            except Exception:
                pass

    async def _hot_add_universe(self) -> None:
        """Фоновая задача: каждые 60с проверяет eligible тикеры с данными, добавляет в universe."""
        from sqlalchemy import text as _text
        from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy
        from app.engine.models import Candle as EC

        while self.running:
            try:
                async with SessionLocal() as db:
                    # Ищем eligible тикеры которые ещё НЕ в universe
                    current_figi = {u["figi"] for u in self.universe}
                    _skip = list(current_figi) if current_figi else ["__none__"]
                    rows = (await db.execute(
                        _text("SELECT figi, ticker, lot_size FROM universe "
                              "WHERE eligible_tier = 'eligible' AND NOT (figi = ANY(:skip))"),
                        {"skip": _skip}
                    )).all()

                    # Диагностика: считаем все eligible + их свечи
                    all_eligible = (await db.execute(
                        _text("SELECT figi, ticker, lot_size FROM universe "
                              "WHERE eligible_tier = 'eligible'")
                    )).all()
                    diag_instruments = []
                    hot_adds_this_cycle = 0
                    hot_skips_this_cycle = 0
                    for afigi, aticker, alot in all_eligible:
                        cnt = (await db.execute(
                            _text("SELECT count(*) FROM candles WHERE figi = :f AND interval = 1"),
                            {"f": afigi}
                        )).scalar()
                        in_active = afigi in current_figi
                        diag_instruments.append({
                            "figi": afigi, "ticker": aticker,
                            "candle_count": cnt or 0, "active": in_active,
                        })

                    for figi, ticker, lot in rows:
                        # Проверяем наличие данных (хотя бы 50 свечей 1min)
                        cnt = (await db.execute(
                            _text("SELECT count(*) FROM candles WHERE figi = :f AND interval = 1"),
                            {"f": figi}
                        )).scalar()
                        if cnt < 50:
                            hot_skips_this_cycle += 1
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
                                            date_from=datetime.now(timezone.utc) - timedelta(days=3))
                        for row in candles:
                            buf.append(EC(ts=row.ts, open=row.open, high=row.high,
                                          low=row.low, close=row.close, volume=row.volume))

                        self.strategies[figi] = proto
                        self.buffers[figi] = buf
                        # Волатильность (ATR%) из буфера — как в select_eligible_universe.
                        _atr_pct = 0.0
                        try:
                            from app.engine.indicators import atr as _atrfn
                            _by5: dict[int, dict] = {}
                            for b in buf:
                                key = int(b.ts.timestamp() // 300)
                                if key not in _by5:
                                    _by5[key] = {"ts": b.ts, "open": float(b.open), "high": float(b.high),
                                                 "low": float(b.low), "close": float(b.close), "volume": float(b.volume or 0)}
                                else:
                                    g = _by5[key]
                                    g["high"] = max(g["high"], float(b.high))
                                    g["low"] = min(g["low"], float(b.low))
                                    g["close"] = float(b.close)
                                    g["volume"] += float(b.volume or 0)
                            _ec5 = [EC(ts=g["ts"], open=g["open"], high=g["high"], low=g["low"],
                                       close=g["close"], volume=g["volume"]) for g in _by5.values()]
                            _vals = _atrfn(_ec5[-44:], 14)
                            _la = next((v for v in reversed(_vals) if v is not None), None)
                            if _la and _ec5 and _ec5[-1].close:
                                _atr_pct = round(_la / _ec5[-1].close * 100, 3)
                        except Exception:
                            pass
                        self.universe.append({
                            "figi": figi, "ticker": ticker,
                            "lot": int(lot) if lot else 10, "name": ticker,
                            "atr_pct": _atr_pct, "avg_price": 0, "avg_turnover": 0, "sector": "",
                        })
                        self.tickers[figi] = ticker
                        hot_adds_this_cycle += 1
                        self._log(f"➕ HOT-ADD: {ticker} ({figi[-6:]}) {cnt} bars")

                    # Обновляем диагностику
                    self.carousel_diag = {
                        "eligible_count": len(all_eligible),
                        "active_count": len(self.universe),
                        "hot_adds": self.carousel_diag.get("hot_adds", 0) + hot_adds_this_cycle,
                        "hot_skips": self.carousel_diag.get("hot_skips", 0) + hot_skips_this_cycle,
                        "last_hot_add_ts": datetime.now(timezone.utc).isoformat(),
                        "last_error": None,
                        "instruments": diag_instruments,
                    }

            except asyncio.CancelledError:
                raise
            except Exception as _e:
                self._log(f"⚠ HOT-ADD: ошибка — {type(_e).__name__}: {_e}")
                self.carousel_diag["last_error"] = str(_e)
            await asyncio.sleep(60.0)

    async def _session_monitor(self) -> None:
        """Фоновая задача: логирует состояние при смене торговой сессии.

        Пишет в лог: текущая фаза, торговая сессия, разрешены ли торги, маржа
        (с плечом) и активные направления.
        """
        from app.bot.session import session_state, trading_session
        _last = None
        while self.running:
            try:
                _ss = session_state()
                _tss = trading_session()
                if _ss != _last:
                    _last = _ss
                    _sess = self.config.sessions or []
                    _msess = self.config.margin_sessions or []
                    trade_ok = bool(_tss) and _tss in _sess
                    margin_ok = bool(_tss) and _tss in _msess
                    _mlv = float(self.config.margin_leverage or 0)
                    _lev = "Max" if _mlv <= 0 else f"×{_mlv:g}"
                    dirs = []
                    if self.config.long_allowed:
                        dirs.append("Long")
                    if self.config.short_allowed:
                        dirs.append("Short")
                    self._log(
                        f"🕒 СЕССИЯ {_ss} | торговая={_tss or '—'} | "
                        f"торги={'ДА' if trade_ok else 'нет'} ({'/'.join(_sess) or '—'}) | "
                        f"маржа={'ДА' if margin_ok else 'нет'} ({'/'.join(_msess) or '—'}, плечо {_lev}) | "
                        f"направления={','.join(dirs) or '—'}"
                    )
                    self.events.log("SESSION_STATE", reason=_ss,
                                    trading=trade_ok, margin=margin_ok)
            except Exception:
                pass
            await asyncio.sleep(20.0)

    async def _metrics_loop(self) -> None:
        """Фоновая задача: каждые 30с собирает метрики движка, считает rate свечей,
        реагирует на аномалии (нет свечей в торговое время, очередь персиста растёт,
        ensemble тормозит) и пишет их в status. Если всё ОК — короткая TECHINFO-строка.

        Ответ «здоровья» хранится в self.metrics и отдаётся в GET /api/v1/bot/status
        (поле metrics), чтобы UI и внешние проверки видели живое состояние без парсинга лога.
        """
        from app.bot.session import session_state, trading_session

        prev_received = self._candles_received
        prev_time = _time.monotonic()
        self.metrics["started_ts"] = datetime.now(timezone.utc).isoformat()
        while self.running:
            await asyncio.sleep(30.0)
            try:
                now = _time.monotonic()
                dt = now - prev_time
                if dt <= 0:
                    dt = 1.0
                recv = self._candles_received - prev_received
                cps = recv / dt
                prev_received = self._candles_received
                prev_time = now

                m = self.metrics
                bar_avg = m["bar_ms_total"] / m["bar_ms_n"] if m["bar_ms_n"] else 0.0
                ens_avg = m["ensemble_ms_total"] / m["ensemble_ms_n"] if m["ensemble_ms_n"] else 0.0
                ps_avg = m["persist_ms_total"] / m["persist_ms_n"] if m["persist_ms_n"] else 0.0

                m.update({
                    "candles_per_sec": round(cps, 3),
                    "received": self._candles_received,
                    "seen": self.candles_seen,
                    "rejected": self._candles_rejected,
                    "signals": self.signals_seen,
                    "bar_ms_avg": round(bar_avg, 1),
                    "ensemble_ms_avg": round(ens_avg, 1),
                    "persist_ms_avg": round(ps_avg, 1),
                    "persist_q": len(self._persist_queue),
                    "persist_q5": len(self._persist_queue_5m),
                    "universe_active": len(self.strategies),
                    "ts": datetime.now(timezone.utc).isoformat(),
                })

                alerts: list[str] = []
                # 1. Свежесть данных (в рабочую сессию свечи ДОЛЖНЫ идти)
                try:
                    _tss = trading_session()
                    _ss = session_state()
                    _trade_ok = _ss in ("trading", "pre_open") and _tss in (self.config.sessions or [])
                except Exception:
                    _trade_ok = True
                if self.last_candle_ts is None:
                    alerts.append("НЕТ свечей с момента старта")
                elif _trade_ok:
                    age = (self._bot_now() - self.last_candle_ts).total_seconds()
                    if age > 90:
                        alerts.append(f"НЕТ свечей {age:.0f}с (сессия активна, cps={cps:.2f})")
                # 2. Очередь персиста растёт
                if len(self._persist_queue) > 1900 or len(self._persist_queue_5m) > 1900:
                    alerts.append(f"очередь персиста почти полна q={len(self._persist_queue)} q5={len(self._persist_queue_5m)}")
                # 3. Ensemble медленный (полный compute_ensemble на 5м-закрытии)
                if m["ensemble_ms_n"] > 0 and ens_avg > 2000:
                    alerts.append(f"ensemble медленный avg={ens_avg:.0f}ms max={m['ensemble_ms_max']:.0f}ms")
                # 4. Обработка бара в целом не должна простаивать event loop
                if m["bar_ms_n"] > 0 and bar_avg > 500:
                    alerts.append(f"бар обрабатывается avg={bar_avg:.0f}ms max={m['bar_ms_max']:.0f}ms")

                m["last_alert"] = None
                m["alerts"] = alerts[-5:]
                if alerts:
                    self._log("⚠ MЕТРИКИ: " + " · ".join(alerts))
                    for a in alerts[-3:]:
                        self.events.log("METRICS_ALERT", reason=a)
                else:
                    self._log(
                        f"TECHINFO метрики cps={cps:.2f} bar={bar_avg:.0f}ms "
                        f"ens={ens_avg:.0f}ms persist={ps_avg:.0f}ms "
                        f"q={len(self._persist_queue)}/{len(self._persist_queue_5m)} "
                        f"сигналов={self.signals_seen}"
                    )
            except Exception as e:
                self._log(f"METRICS_LOOP_ERR {type(e).__name__}: {str(e)[:120]}")

    async def _reconcile_loop(self) -> None:
        """Фоновая задача: каждые 60с.

        Источник истины — фактические позиции брокера (тиньков). Локальный учёт
        (sandbox_trades, exit_time IS NULL) постоянно приводится к факту:
          * позиция брокера без локальной открытой строки      -> строка создаётся
          * локальная открытая строка без позиции брокера      -> закрывается (orphan)
          * несколько локальных строк на один figi              -> закрываются все,
                                                                   кроме лучшей по цене
        Каждая правка логируется (rec_pnlid). Дупликаты/орфаны не удаляются —
        переводятся в историю с причиной.
        """
        while self.running:
            await asyncio.sleep(60.0)
            try:
                if not isinstance(self.broker, LiveBroker):
                    continue
                await self._reconcile_positions(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as _re_err:
                self._log(f"RECONCILE FAIL: {type(_re_err).__name__}: {_re_err}")

    async def _reconcile_positions(self, force: bool = False) -> None:
        """Привести локальный учёт открытых позиций к факту брокера."""
        from app.models.sandbox_trade import SandboxTrade
        from sqlalchemy import select as _sel_r
        # Форс-сброс кэша портфеля, чтобы reconcile не видел устаревший снапшот
        # после только что отправленного ордера (иначе свежая строка съедается как orphan).
        try:
            await self.broker.flush_portfolio()
        except Exception:
            pass
        broker_pos = {p.figi: p for p in await self.broker.positions()}
        async with SessionLocal() as db:
            rows = (await db.execute(_sel_r(SandboxTrade)
                                     .where(SandboxTrade.exit_time.is_(None),
                                            SandboxTrade.mode == self.broker_mode))).scalars().all()
            local_by_figi: dict[str, list] = {}
            for r in rows:
                local_by_figi.setdefault(r.figi, []).append(r)

            created = closed = 0
            now = datetime.now(timezone.utc)
            # Grace-период: строки, открытые/закрытые в последние 2 минуты, НЕ трогаем —
            # брокер (sandbox/биржа) может отражать позицию с лагом после ордера.
            _grace = 120.0
            # 1) Позиции брокера без локальной строки -> создать.
            for figi, bp in broker_pos.items():
                if figi in local_by_figi:
                    continue
                # Grace-период против «двойняшек orphan»: если по figi НЕДАВНО
                # была локально закрыта строка (напр. только что сработал
                # stop_loss, а брокер ещё держит позицию с лагом отражения) —
                # НЕ создаём rebuilt. Иначе: строку создали, брокер отразил
                # закрытие, позиция исчезла, строка осталась orphan =>
                # следующий цикл пометит её orphan_cleanup (+0₽, дубликат).
                # Ждём поэтому grace-секунд: брокер либо уберёт позицию
                # (всё сходится), либо она останется — тогда rebuilt создастся
                # на следующем цикле уже без потери реальной позиции.
                _recently_closed = (await db.execute(
                    _sel_r(SandboxTrade)
                    .where(SandboxTrade.figi == figi,
                           SandboxTrade.mode == self.broker_mode,
                           SandboxTrade.exit_time.is_not(None),
                           SandboxTrade.exit_time >= now - timedelta(seconds=_grace))
                    .order_by(SandboxTrade.exit_time.desc())
                    .limit(1)
                )).scalar_one_or_none()
                if _recently_closed is not None:
                    _tk = self.tickers.get(figi, figi[:8])
                    self._log(f"RECONCILE: position {_tk} ({figi[-6:]}) не пересоздаю — "
                              f"локально закрыта {(_grace - (now - _recently_closed.exit_time).total_seconds()):.0f}с назад, жду отражения брокера")
                    continue
                ticker = self.tickers.get(figi, figi[:8])
                db.add(SandboxTrade(
                    figi=figi, ticker=ticker,
                    side="SELL" if bp.side in ("SHORT", "SELL") else "BUY",
                    qty=int(abs(bp.qty)),
                    entry_time=datetime.now(timezone.utc),
                    entry_price=float(getattr(bp, "entry_price", 0.0) or 0.0),
                    stop_loss=None, take_profit=None,
                    entry_reason="rebuilt_from_tinkoff",
                    leverage=1.0,
                    mode=self.broker_mode,
                    test_name=getattr(self.config, "test_name", "") or None,
                ))
                created += 1
                self._log(f"RECONCILE: создана строка {ticker} ({figi[-6:]}) {bp.side} {bp.qty} @{getattr(bp, 'entry_price', 0.0):.2f}")

            # 2) Локальные строки без позиции брокера -> закрыть (orphan).
            for figi, lst in local_by_figi.items():
                if figi in broker_pos:
                    continue
                pending = [r for r in lst if (now - (r.entry_time or now)).total_seconds() < _grace]
                if pending and len(pending) == len(lst):
                    self._log(f"RECONCILE: свежие строки {figi[-6:]} ({len(lst)}) — жду отражения позиции (grace {_grace:.0f}с)")
                    continue
                closed_here = 0
                for r in lst:
                    if (now - (r.entry_time or now)).total_seconds() < _grace:
                        continue
                    r.exit_time = now
                    r.exit_price = float(r.entry_price or 0.0)
                    r.exit_reason = "orphan_cleanup"
                    r.net_pnl = 0.0
                    closed_here += 1
                closed += closed_here
                if closed_here:
                    self._log(f"RECONCILE: закрыт orphan {figi[-6:]} ({closed_here} строк)")

            # 3) Дубликаты на один figi -> оставить только лучшую по цене.
            for figi, lst in local_by_figi.items():
                if figi not in broker_pos or len(lst) <= 1:
                    continue
                bp = broker_pos[figi]
                ref = float(getattr(bp, "entry_price", 0.0) or 0.0)
                best = min(lst, key=lambda r: abs(float(r.entry_price or 0.0) - ref) if ref else 0.0)
                for r in lst:
                    if r is best:
                        continue
                    r.exit_time = now
                    r.exit_price = float(r.entry_price or 0.0)
                    r.exit_reason = "duplicate_cleanup"
                    r.net_pnl = 0.0
                    closed += 1
                    _tk = self.tickers.get(figi, figi[:8])
                    self._log(f"RECONCILE: закрыт дубликат {_tk} {figi[-6:]} @{r.entry_price:.2f} (best @{ref:.2f})")
            if created or closed:
                await db.commit()
            # Лог состояния сверки КАЖДЫЙ цикл (не только при изменениях).
            if not force:
                if created or closed:
                    self._log(f"RECONCILE CHECK: MISMATCH → +{created} создано, {closed} закрыто | брокер {len(broker_pos)} поз, локально {len(local_by_figi)} строк")
                else:
                    self._log(f"RECONCILE CHECK: OK | брокер {len(broker_pos)} поз, локально {len(local_by_figi)} строк, расхождений нет")

    def _interval_value(self) -> int:
        if self.config.use_ensemble:
            return 1
        interval = INTERVAL_NAMES[self.config.interval_name]
        return int(getattr(interval, "value", interval))

    async def _flush_persist(self) -> None:
        from sqlalchemy import text as _text

        def _drain(q: deque) -> list:
            return [q.popleft() for _ in range(len(q))]

        while self.running:
            await asyncio.sleep(3.0)
            if not self._persist_queue and not self._persist_queue_5m:
                continue
            batch = _drain(self._persist_queue)
            batch5 = _drain(self._persist_queue_5m)
            try:
                _tp0 = _time.perf_counter()
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
                    log_rows = _drain(self._log_persist_queue)
                    if log_rows:
                        _payload = [{"level": l, "source": s, "msg": m} for (l, s, m) in log_rows]
                        await db.execute(
                            _text(
                                "INSERT INTO bot_logs (level, source, msg) "
                                "SELECT * FROM jsonb_to_recordset(:rows) AS t(level text, source text, msg text)"
                            ),
                            {"rows": json.dumps(_payload, ensure_ascii=False)},
                        )
                    await db.commit()
                _pt = (_time.perf_counter() - _tp0) * 1000
                self.metrics["persist_ms_total"] += _pt
                self.metrics["persist_ms_n"] += 1
                if _pt > self.metrics["persist_ms_max"]:
                    self.metrics["persist_ms_max"] = _pt
                self._persist_flushes += 1
                if batch or batch5:
                    _s = (batch[0] if batch else batch5[0])
                    self._log(
                        f"TECHINFO FLUSH_ITEM f={_s[0][-6:]} ts={_s[1]} "
                        f"iv={1 if batch else 5} flushes={self._persist_flushes} "
                        f"q={len(batch)} q5={len(batch5)}"
                    )
                if self._persist_flushes % 10 == 0:
                    self._log(
                        f"TECHINFO persist ok q={len(batch)} q5={len(batch5)} "
                        f"flushes={self._persist_flushes}"
                    )
            except Exception as e:
                self._log(f"PERSIST_ERR {type(e).__name__}: {str(e)[:80]}")
                # Вернуть данные обратно в очередь, чтобы не потерять
                self._persist_queue.extend(batch)
                self._persist_queue_5m.extend(batch5)

    async def _run(self) -> None:
        # Feed = источник СВЕЧЕЙ: всегда боевой токен + основной API, потому что
        # sandbox НЕ отдаёт market-data стрим (CancelledError на подписке) и свежие
        # бары тогда идут только polling'ом.
        # Broker (LiveBroker) при этом остаётся на sandbox — торговля по-прежнему
        # тестовая. Обратное сочетание (sandbox-токен на боевом API) не работает.
        settings = get_settings()
        if self.config.feed == "replay":
            # Реплей: исторические свечи из БД через те же runtime-пути (paper-брокер).
            # Корректный config.replay_start уже распарсен в start() в self._replay_from.
            if self._replay_from is None:
                try:
                    self._replay_from = datetime.fromisoformat(
                        self.config.replay_start.replace("Z", "+00:00")
                    )
                except Exception:
                    self._replay_from = None
                if self._replay_from is not None and self._replay_from.tzinfo is None:
                    self._replay_from = self._replay_from.replace(tzinfo=timezone.utc)
            _r_end: datetime | None = None
            if self.config.replay_end:
                try:
                    _r_end = datetime.fromisoformat(self.config.replay_end.replace("Z", "+00:00"))
                    if _r_end.tzinfo is None:
                        _r_end = _r_end.replace(tzinfo=timezone.utc)
                except Exception:
                    _r_end = None
            feed: CandleFeed = ReplayFeed(
                self.config.interval_name, self.stream_universe,
                self._replay_from or datetime.now(timezone.utc), _r_end,
                pace=self.config.replay_pace,
            )
            self._log("REPLAY FEED: исторические свечи из БД (paper-брокер, без API)")
        else:
            _feed_token = settings.feed_token
            _feed_target = None
            feed = CandleFeed(_feed_token, self.config.interval_name,
                              self.stream_universe, target=_feed_target)
        feed.on_log = lambda msg: self._log("TECHINFO [feed] " + msg)
        self.feed = feed
        exited = "stream_exhausted"
        self._persist_task = asyncio.create_task(self._flush_persist())
        self._held_sync_task = asyncio.create_task(self._sync_held())
        self._hot_add_task = asyncio.create_task(self._hot_add_universe())
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        self._session_task = asyncio.create_task(self._session_monitor())
        self._metrics_task = asyncio.create_task(self._metrics_loop())
        self._intrabar_task = asyncio.create_task(self._intrabar_exit_loop())
        self._imoex_task = asyncio.create_task(self._imoex_loop())
        try:
            async for candle in feed.stream():
                if not self.running:
                    exited = "running_flag_false"
                    break
                self.mode = feed.mode
                # В реплее виртуальные часы двигаются вместе с подаваемой свечой:
                # все решения (сессии, свежесть, дневной PnL) видят «правильное» время.
                if isinstance(feed, ReplayFeed) and candle.ts is not None:
                    self._replay_cur = candle.ts
                    if self.config.test_name:
                        self.mode = f"test:{self.config.test_name}"
                _t1 = _time.perf_counter()
                await self._process_candle(candle)
                _dt = (_time.perf_counter() - _t1) * 1000
                self.metrics["bar_ms_total"] += _dt
                self.metrics["bar_ms_n"] += 1
                if _dt > self.metrics["bar_ms_max"]:
                    self.metrics["bar_ms_max"] = _dt
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
            for t in ('_held_sync_task', '_hot_add_task', '_reconcile_task', '_metrics_task', '_intrabar_task', '_imoex_task'):
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
        _t0 = _time.perf_counter()
        figi = c.figi
        figi = self.tcs_to_bbg.get(c.figi, c.figi)

        # Обновляем last_candle_ts ДО валидации — health-check должен видеть,
        # что данные ПОСТУПАЮТ, даже если свеча битая и отброшена.
        self._candles_received += 1
        self.last_candle_ts = c.ts
        self.data_source = self.mode
        # Виртуальные часы replay: текущая поданная свеча (для тестового UI-цены).
        if getattr(self.config, "feed", "") == "replay":
            self._replay_cur = c.ts

        # IMOEX guard: пересчёт состояния всплеска раз в минуту (по виртуальным часам).
        try:
            _mm = self._bot_now().strftime("%Y%m%d%H%M")
            if _mm != self._imoex_last_tick:
                self._imoex_last_tick = _mm
                self._imoex_guard_tick()
        except Exception:
            pass

        # Битая свеча (прыжок цены / битые OHLC): пропускаем полностью —
        # не персистим и не кормим стратегию (согласуется с backtest _validate_candles)
        if not self._candle_ok(c):
            self._candles_rejected += 1
            self.events.log("DATA_BAD_CANDLE", figi=figi,
                            reason=f"rejected open={c.open} close={c.close}")
            return

        # Только ЗАКРЫТЫЕ свечи пишем в БД и допускаем к торговой логике.
        # Исторический backfill/незакрытые обновления текущего бара отбрасываются.
        if not self._is_closed(c):
            self._candles_rejected += 1
            self.events.log("DATA_UNCLOSED_CANDLE", figi=figi,
                            reason=f"not closed ts={c.ts}")
            return

        # Всегда сохраняем свечу в БД (для всех 20 eligible тикеров)
        self.candles_seen += 1
        self._bar_counter += 1
        try:
            # В replay свечи УЖЕ в БД (оттуда и читаем) — обратная запись избыточна
            # и тормозит тест (persist ~540ms/флаш через сеть). Пишем только в live/sandbox.
            if self.mode != "replay" and not str(self.mode).startswith("test"):
                self._persist_queue.append(
                    (figi, c.ts, float(c.open), float(c.high), float(c.low), float(c.close), int(c.volume or 0))
                )
        except Exception:
            pass

        # TECHINFO: периодический отчёт (раз в 60с) о поступлении/персисте свечей
        try:
            _now3 = datetime.now(timezone.utc).timestamp()
            if _now3 - self._last_q_report_ts >= 60:
                self._last_q_report_ts = _now3
                self._log(
                    f"TECHINFO stat received={self._candles_received} seen={self.candles_seen} "
                    f"rejected={self._candles_rejected} persist_q={len(self._persist_queue)} "
                    f"persist_q5={len(self._persist_queue_5m)} mode={self.mode}"
                )
        except Exception:
            pass

        # ГЕЙТ СВЕЖЕСТИ (wall-clock): в торговлю допускаем ТОЛЬКО последний
        # закрытый бар. Свеча, чей ts сильно позади now (исторический баклог,
        # перемотка после рестарта), НЕ торгуется — иначе решения BUY/SELL
        # флипают каждые секунды на старых данных.
        try:
            _step = STEP_SEC.get(self.config.interval_name, 60)
            _age = (self._bot_now() - c.ts).total_seconds()
            if _age > _step * 2:
                self._candles_rejected += 1
                self.events.log("DATA_STALE_CANDLE", figi=figi,
                                reason=f"stale age={int(_age)}s ts={c.ts}")
                return
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
            if _min % 5 == 4 and len(buffer) >= 5 and self.mode != "replay" and not str(self.mode).startswith("test"):
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
                self._log(f"СВЕЧА {figi[-6:]} ts={c.ts.strftime('%H:%M:%S')} o={c.open:.2f} h={c.high:.2f} l={c.low:.2f} c={c.close:.2f} v={c.volume}")

        _lookup = self.tcs_to_bbg.get(figi, figi)
        # Позиция брокера/стрима нужна только как ФАКТ открытой позиции (side/qty).
        # Стоп/TP/трейлинг живут в собственном учёте (_exit_*) — в sandbox/live
        # pos.stop_loss/take_profit/entry_price приходят пустыми.
        # Источник правды — БРОКЕР (реальные позиции). Стрим — только кэш entry_price,
        # НЕ источник факта позиции (иначе фантом после закрытия оживает трейлинг).
        pos = await self.broker.get_position(_lookup) or await self.broker.get_position(figi)
        if pos is not None and self.stream_manager is not None:
            _srv_pos = self.stream_manager.get_position(_lookup) or self.stream_manager.get_position(figi)
            if _srv_pos is not None and not pos.entry_price:
                from app.bot.live_broker import LivePosition
                pos = LivePosition(
                    figi=pos.figi, ticker=pos.ticker, side=pos.side,
                    qty=pos.qty, entry_price=_srv_pos.entry_price,
                    entry_time=pos.entry_time, stop_loss=None, take_profit=None,
                    strategy_id=pos.strategy_id,
                )

        # --- Единый механизм выхода (зеркалит движок EngineRunner) ---
        # Пока трейлинг не активирован: стандартный SL/TP (защита от разворота).
        # При pnl >= комиссия_входа × 4 трейлинг активируется: TP и сигнальные
        # выходы отключаются, стоп начинает идти за ценой (ratchet).
        _closed = False
        if pos is not None and figi in self._exit_plans:
            _closed = await self._step_exit(figi, c, pos)
        elif pos is not None and figi not in self._exit_plans:
            # Позиция у брокера есть, но учёта выхода нет (рестарт/ручное открытие).
            await self._ensure_exit_state(figi, c, pos)
            if figi in self._exit_plans:
                _closed = await self._step_exit(figi, c, pos)

        # --- Overnight / EOD: force close outside trading sessions ---
        # overnight=True  = держать позиции через ночь (не закрывать)
        # overnight=False = закрывать на конец торгового дня, но клиринг day→evening
        #                   и активные сессии всегда выдерживаются
        if not _closed and _should_force_close(c.ts, self.config.sessions, self.config.overnight):
            trade = await self.broker.close_position(figi, float(c.open), "overnight_force_close")
            self._held.discard(figi)
            self._opposite_count.pop(figi, None)
            self._last_exit_bar[figi] = self._bar_counter
            self._clear_exit_state(figi)
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
            # Held-тикер: не рассматривать входы В СТОРОНУ открытой позиции
            # (экономия CPU/AI-гейта). Противоположные сигналы остаются — они нужны
            # для сигнальных выходов и флипов.
            try:
                _pos_side = ""
                if figi in self._held:
                    _s = str(self._exit_side.get(figi) or "").upper()
                    _pos_side = "BUY" if _s == "LONG" else ("SELL" if _s == "SHORT" else "")
                if hasattr(strategy, "p") and hasattr(strategy.p, "skip_entry_side"):
                    strategy.p.skip_entry_side = _pos_side
            except Exception:
                pass
            _t2 = _time.perf_counter()
            sig = await asyncio.to_thread(strategy.on_bar, list(buffer))
            # Инверсия на уровне СИГНАЛА: тогда вход, встречный сигнал и выходы
            # трактуются согласованно (раньше инвертировался только ордер входа).
            if sig is not None and getattr(self.config, "invert_signals", False):
                from app.engine.models import Signal as _Sig, Side as _Side
                sig = _Sig(strategy_id=sig.strategy_id,
                           side=(_Side.SELL if sig.side == _Side.BUY else _Side.BUY),
                           time=sig.time, reason=f"inv:{sig.reason}",
                           features=sig.features, kind=sig.kind)
            _et = (_time.perf_counter() - _t2) * 1000
            self.metrics["ensemble_ms_total"] += _et
            self.metrics["ensemble_ms_n"] += 1
            if _et > self.metrics["ensemble_ms_max"]:
                self.metrics["ensemble_ms_max"] = _et
        except Exception as e:
            self.events.log("SIGNAL_ERROR", figi=figi, reason=str(e)[:200])
            sig = None
        finally:
            self._signal_busy.discard(figi)
        # Диагностика «почему нет входа»: на 5m-границе логируем причину из стратегии.
        if sig is None and c.ts.minute % 5 == 0:
            _skip = getattr(strategy, "_last_skip", None)
            if _skip and self._skip_logged.get(figi) != c.ts:
                self._skip_logged[figi] = c.ts
                self._log(f"⏭ НЕТ ВХОДА {self.tickers.get(figi, figi[-6:])} "
                          f"ts={c.ts.strftime('%m-%d %H:%M')}: {_skip}")
        _reg_state = getattr(strategy, "_last_regime", None)
        _reg_vol = getattr(strategy, "_last_vol", None)
        # Fallback: если стратегия не вернула regime timeline (новые/hot-add тикеры),
        # считаем режим сами по 5m-барам — чтобы UI всегда показывал режим и Vol.
        # Дорого, поэтому только на 5m-границе и пока нет данных.
        if _reg_state is None and c.ts.minute % 5 == 0:
            try:
                from app.services.regime import RegimeDetector
                from app.services.ensemble import resample as _resample5
                _c5 = _resample5(list(buffer), 3600)  # режим строго на H1
                _tl = RegimeDetector().compute(_c5)
                if _tl:
                    _r = _tl[-1]
                    _reg_state = {"state": _r.get("state"), "reason": _r.get("reason"),
                                  "features": _r.get("features")}
                    _feats = _r.get("features") or {}
                    _reg_vol = _feats.get("volume_ratio")
            except Exception:
                pass
        if _reg_state is not None:
            self._regimes[figi] = {
                "state": _reg_state,
                "vol": _reg_vol,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        # Голоса на последнем 5m баре (для вкладки «Голоса» и лога недобора кворума).
        _v = getattr(strategy, "_last_votes", None)
        if _v and _v.get("ts"):
            self._votes[figi] = _v
            _k = int(getattr(getattr(strategy, "p", None), "quorum", 3) or 3)
            _need = _k - 1  # недобор: например 2 из 3
            _b, _s = int(_v.get("buy", 0)), int(_v.get("sell", 0))
            if _need >= 1 and max(_b, _s) == _need and self._votes_logged.get(figi) != _v["ts"]:
                self._votes_logged[figi] = _v["ts"]
                _side = "BUY" if _b >= _s else "SELL"
                _mem = _v.get("buy_members" if _side == "BUY" else "sell_members", [])
                self._log(f"🟡 КВОРУМ {_need}/{_k} {self.tickers.get(figi, figi[:6])} {_side} "
                          f"цена={c.close:.2f} ({','.join(_mem)}) — ждём {_k}-й голос")
        if sig is None:
            return
        self.signals_seen += 1
        ticker = self.tickers.get(figi, "")
        # Held-тикер: входные сигналы не спамим и не рассматриваем.
        #  - сигнал В СТОРОНУ позиции — молча пропускаем (входов в held не бывает);
        #  - противоположный — это кандидат на выход/флип, логируем как ВЫХОД.
        if figi in self._held:
            _cur = str(self._exit_side.get(figi) or "").upper()
            _pos_buy = _cur in ("LONG", "BUY")
            _sig_buy = sig.side.value == "BUY"
            if _cur and (_sig_buy == _pos_buy):
                self._log_no_trade(figi, "already_held")
                return
            self._log(f"СИГНАЛ-ВЫХОД {ticker} {sig.side.value} (против позиции {_cur or '?'}) "
                      f"sid={getattr(sig,'strategy_id','?')}")
        else:
            self._log(f"СИГНАЛ {ticker} {sig.side.value} ({sig.kind}) sid={getattr(sig,'strategy_id','?')} reason={getattr(sig,'reason','?')}")
        self.events.log("SIGNAL_CREATED", figi=figi, ticker=ticker,
                        side=sig.side.value)

        pos_now = await self.broker.get_position(figi)
        # Стрим НЕ используем как источник позиции — он держит «фантомную» позицию после
        # реального закрытия, из-за чего confirm_flip крутится вхолостую (ОППОЗИТ каждые 30с).
        if pos_now is None and self.stream_manager is not None:
            _srv_now = self.stream_manager.get_position(figi)
            if _srv_now is not None:
                from app.bot.live_broker import LivePosition
                pos_now = LivePosition(
                    figi=_srv_now.figi, ticker=_srv_now.ticker, side=_srv_now.side,
                    qty=_srv_now.qty, entry_price=_srv_now.entry_price,
                    entry_time=datetime.now(timezone.utc), stop_loss=None, take_profit=None,
                )
        state_now = PositionState.LONG if (pos_now and pos_now.side in ("LONG", "BUY")) else (
            PositionState.SHORT if (pos_now and pos_now.side in ("SHORT", "SELL")) else PositionState.FLAT
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
            if not _sessions_allowed(self._bot_now(), self.config.sessions):
                from app.bot.session import trading_session as _ts
                self._log(f"ПРОПУСК ВХОДА {ticker}: вне торговых сессий "
                          f"(сейчас {_ts() or '—'}, разрешены {'/'.join(self.config.sessions) or '—'})")
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker, reason="SESSION")
                self._log_no_trade(figi, "session_filter")
                return
            if self.entries_paused:
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason="ENTRIES_PAUSED")
                self._log_no_trade(figi, "entries_paused")
                return
            if sig.side.value == "BUY" and not self.config.long_allowed:
                self._log(f"ПРОПУСК ВХОДА {ticker}: Long запрещён (Направление)")
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason="LONG_DISABLED")
                self._log_no_trade(figi, "long_disabled")
                return
            if sig.side.value == "SELL" and not self.config.short_allowed:
                self._log(f"ПРОПУСК ВХОДА {ticker}: Short запрещён (Направление)")
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason="SHORT_DISABLED")
                self._log_no_trade(figi, "short_disabled")
                return
            # Regime gate: режим рынка должен быть в разрешённых trade_regimes.
            _allowed_reg = self.config.trade_regimes or []
            _reg = (self._regimes.get(figi) or {}).get("state")
            _reg_name = _reg.get("state") if isinstance(_reg, dict) else None
            if _reg_name and _allowed_reg and _reg_name not in _allowed_reg:
                self._log(f"ПРОПУСК ВХОДА {ticker}: режим {_reg_name} отключён "
                          f"(разрешены {'/'.join(_allowed_reg) or '—'})")
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason=f"REGIME_{_reg_name}")
                self._log_no_trade(figi, f"regime_{_reg_name.lower()}")
                return
            # Trend-alignment: не входим против тренда (TREND_UP → только BUY, TREND_DOWN → только SELL).
            if getattr(self.config, "trend_alignment", True) and _reg_name in ("TREND_UP", "TREND_DOWN"):
                _against = (_reg_name == "TREND_UP" and sig.side.value == "SELL") or \
                           (_reg_name == "TREND_DOWN" and sig.side.value == "BUY")
                if _against:
                    self._log(f"ПРОПУСК ВХОДА {ticker}: {sig.side.value} против тренда {_reg_name}")
                    self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                    reason=f"TREND_ALIGN_{_reg_name}")
                    self._log_no_trade(figi, "trend_alignment")
                    return
            # HOLD после серии убытков: пауза входов (per-ticker или global).
            _hold, _hold_why = self._loss_streak_block(figi)
            if _hold:
                self._log(f"ПРОПУСК ВХОДА {ticker}: HOLD после убытков — {_hold_why}")
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker, reason="LOSS_STREAK_HOLD")
                self._log_no_trade(figi, "loss_streak_hold")
                return
            # IMOEX guard: не входить против всплеска индекса, пока он не стабилизируется.
            _imoex_why = self._imoex_block_reason(sig.side.value, figi)
            if _imoex_why:
                if self._imoex_state is not None:
                    self._imoex_state.blocks += 1
                self._log(f"ПРОПУСК ВХОДА {ticker}: против IMOEX — {_imoex_why}")
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker, reason="IMOEX_GUARD")
                self._log_no_trade(figi, "imoex_guard")
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
            # Закрываем ТОЛЬКО позиции из нашего учёта (_held). pos_now со стрима/брокера
            # отстаёт на десятки секунд после реального закрытия → без этой защиты бот
            # «закрывает» уже закрытую позицию ещё раз (продаёт в пустоту) и создаёт
            # лишний SHORT: так и родился цикл ОППОЗИТ каждые ~30с.
            if figi not in self._held:
                self._opposite_count.pop(figi, None)
                self._log_no_trade(figi, "exit_no_held",
                                   "позиции нет в _held (уже закрыта/лаг источника)")
                return
            # --- Trailing active: сигнальные выходы и flip отключены ---
            if self._trail_active.get(figi, False):
                self.events.log("HOLD_TRAILING", figi=figi, ticker=ticker,
                                reason="signal exit ignored, trailing stop active")
                self._log(f"ИГНОР ВЫХОДА {ticker}: трейлинг активен, ждём стоп")
                self._opposite_count[figi] = 0
                return
            # --- Opposite-hold / confirm_flip ---
            cf = self.config.confirm_flip
            if cf > 0 and pos_now is not None:
                cur_side = "BUY" if pos_now.side in ("LONG", "BUY") else "SELL"
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
        # Инверсия уже применена на уровне сигнала (см. _process_candle) — здесь НЕ дублируем.
        qty = cfg.qty_per_trade
        _used_lev = 1.0
        if action == "open":
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
                    # Бюджет на ОДИН слот = 20% от equity (собственные деньги на позицию).
                    # Плечо маржи доводит размер позиции до максимума, который разрешит
                    # брокер (см. блок MARGIN ниже) — НЕ до максимума портфеля.
                    _eq = await self.broker.equity()
                    _free = await self.broker.free_funds()
                    budget = min(_eq * POS_PCT, _free) if _free > 0 else _eq * POS_PCT
                except Exception:
                    try:
                        live_cash = await self.broker.cash()
                        budget = live_cash * POS_PCT
                    except Exception:
                        pass
            elif isinstance(self.broker, PaperBroker):
                # Тест-режим: эмулируем Live — бюджет = доля от начального капитала теста
                # (PaperBroker не спрашивает equity у брокера). Плечо ниже берётся из БД.
                try:
                    _acc = await self.broker.ensure_account(cfg.initial_cash)
                    _pos_list = await self.broker.positions()
                    _pv = 0.0
                    for _p in _pos_list:
                        _pb = self.buffers.get(getattr(_p, "figi", ""))
                        _ppx = float(_pb[-1].close) if _pb else float(getattr(_p, "entry_price", 0) or 0)
                        _pv += abs(float(getattr(_p, "qty", 0) or 0)) * _ppx
                    _eq = float(_acc.cash or 0.0) + _pv
                    if _eq <= 0:
                        _eq = float(cfg.initial_cash)
                    budget = _eq * POS_PCT
                    self._log(
                        f"TEST BUDGET {ticker}: equity≈{_eq:.0f}₽ → слот {budget:.0f}₽ "
                        f"(POS_PCT {POS_PCT*100:.0f}%) · initial={cfg.initial_cash:.0f}₽"
                    )
                except Exception as e:
                    self._log(f"TEST BUDGET FAIL {ticker}: {type(e).__name__}: {str(e)[:80]} — слот {budget:.0f}₽")
            lev = max(1.0, float(cfg.leverage or 1.0))
            lot_cost = price * lot
            if price <= 0 or lot <= 0 or lot_cost <= 0:
                self._log(f"ПРОПУСК СДЕЛКИ {ticker}: цена={price} лот={lot}")
                return
            own_per_lot = lot_cost  # divide: позиция = бюджет (свои = бюджет / плечо)
            # Ранняя проверка достаточности бюджета. В divide-режиме плечо НЕ
            # уменьшает требуемые свои на 1 лот (own_per_lot == lot_cost),
            # поэтому если денег нет даже на лот — пропускаем СРАЗУ, без
            # запроса max.lots у брокера (Log: «зачем просить маржу,
            # если даже на одну акцию не хватает»). В multiply-режиме плечо
            # может спасти лот (own_per_lot = lot_cost/lev) — идём в блок MARGIN.
            if budget < lot_cost and str(cfg.margin_sizing).lower() != "multiply":
                self._log(f"ПРОПУСК СДЕЛКИ {ticker}: бюджет {budget:.0f} < стоимость лота {lot_cost:.0f} (divide)")
                return
            # --- Маржинальное плечо: запрашиваем у брокера ДО входа, ответ в лог ---
            if isinstance(self.broker, (LiveBroker, PaperBroker)):
                try:
                    ml = await self.broker.get_max_lots(figi)
                    from app.bot.session import session_state, trading_session
                    _ss = session_state(now=self._bot_now())
                    _tss = trading_session(now=self._bot_now())
                    use_margin = bool(cfg.margin_sessions) and _tss in cfg.margin_sessions
                    _cash_lots = ml.buy_cash if side == "BUY" else ml.sell_cash
                    _mrgn_lots = ml.buy_margin if side == "BUY" else ml.sell_margin
                    self._log(
                        f"MARGIN {ticker}: cash_lots={_cash_lots} margin_lots={_mrgn_lots} "
                        f"leverage={ml.leverage:.2f} session={_ss} margin_session={_tss or '—'} "
                        f"use_margin={use_margin} margin_sessions={'/'.join(cfg.margin_sessions) or '—'} "
                        f"budget={budget:.0f} lot_cost={lot_cost:.0f}"
                    )
                    if use_margin and _mrgn_lots > 0:
                        # Плечо: не выше одобренного брокером (ml.leverage) и не выше
                        # выбранного ползунком (margin_leverage; 0 = Max).
                        _max_lev = ml.leverage
                        _want = float(cfg.margin_leverage or 0.0)
                        lev = _max_lev if _want <= 0 else min(_max_lev, _want)
                        if lev < 1.0:
                            lev = 1.0
                        # Режим размера позиции:
                        #   divide   — позиция = бюджет (свои = бюджет / плечо)  [1-й счёт]
                        #   multiply — позиция = бюджет × плечо (свои = бюджет)  [2-й счёт]
                        if str(cfg.margin_sizing).lower() == "multiply":
                            own_per_lot = lot_cost / lev
                            _pos, _own = budget * lev, budget
                        else:
                            own_per_lot = lot_cost
                            _pos, _own = budget, budget / lev
                        self._log(f"MARGIN LEV {ticker}: брокер ×{_max_lev:.2f} · "
                                  f"выбрано {'Max' if _want <= 0 else '×'+format(_want, 'g')} → ×{lev:.2f} · "
                                  f"режим={cfg.margin_sizing} (позиция {_pos:.0f}₽ = свои {_own:.0f}₽ + заём {_pos-_own:.0f}₽)")
                except Exception as e:
                    self._log(f"MARGIN CHECK FAIL {ticker}: {e} — proceed at cfg.leverage={lev:.1f}")
            if budget < own_per_lot:
                self._log(f"ПРОПУСК СДЕЛКИ {ticker}: бюджет {budget:.0f} < стоимость лота {own_per_lot:.0f}")
                return
            qty = max(1, int(budget / own_per_lot))
            _used_lev = lev
        # --- Margin cap: не превышать max lots брокера (ответ уже в логе MARGIN) ---
        # Развилка: если в этой сессии плечо запрещено (вечер/утро), но инструмент
        # в принципе торгуется (есть маржинальный лимит в нужную сторону), НЕ блокируем —
        # qty уже посчитан по собственному бюджету без плеча (own_per_lot при lev=1).
        # Свои деньги = 0 бывает у шортов в sandbox (шорт требует маржи), при этом
        # cash-бюджет на qty есть — значит ордер без плеча допустим.
        if action == "open" and isinstance(self.broker, (LiveBroker, PaperBroker)) and cfg.use_margin:
            try:
                from app.bot.session import trading_session
                _tss = trading_session(now=self._bot_now())
                use_margin = bool(cfg.margin_sessions) and _tss in cfg.margin_sessions
                ml = await self.broker.get_max_lots(figi)
                cash_max = ml.buy_cash if side == "BUY" else ml.sell_cash
                margin_max = ml.buy_margin if side == "BUY" else ml.sell_margin
                if use_margin:
                    max_lots = margin_max
                    _lim_kind = "маржа"
                elif cash_max > 0:
                    max_lots = cash_max
                    _lim_kind = "свои деньги"
                else:
                    # Без плеча, но инструмент торгуется вообще (шорт через марж. лимит):
                    # потолок — маржинальный лимит, реальный размер уже ограничен бюджетом.
                    max_lots = margin_max
                    _lim_kind = "свои деньги (без плеча, потолок марж. лимита)"
                if max_lots <= 0:
                    self._log(f"ПРОПУСК СДЕЛКИ {ticker}: лимит ({_lim_kind}) = 0")
                    return
                if qty > max_lots:
                    self._log(f"QTY CAP {ticker}: {qty} → {max_lots} ({_lim_kind}, {_tss or '—'})")
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
            meta={**dict(meta or {}), "leverage": float(_used_lev)},
        )
        # --- AI-гейт: дедуп и пауза после отклонения ---
        if action == "open" and bool(getattr(cfg, "ai_approval", False)):
            _pend = self.pending_orders.get(figi)
            if _pend is not None and getattr(_pend, "status", "") == "PENDING_APPROVAL":
                self._log(f"AI-ГЕЙТ: {ticker} уже ждёт решения — новую заявку не создаём")
                self._log_no_trade(figi, "ai_already_pending")
                return
            _cd = float(getattr(cfg, "ai_reject_cooldown_min", 15.0) or 0.0)
            _until = self._ai_reject_until.get(figi)
            if _cd > 0 and _until is not None and self._bot_now() < _until:
                _left = (_until - self._bot_now()).total_seconds() / 60.0
                self._log(f"AI-ГЕЙТ: {ticker} недавно отклонён ИИ — входы на паузе ещё {_left:.0f} мин")
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker, reason="AI_REJECT_COOLDOWN")
                self._log_no_trade(figi, "ai_reject_cooldown")
                return
            order.status = "PENDING_APPROVAL"
            self._approvals_since[order.id] = _time.monotonic()
            self._log(f"AI-ГЕЙТ: вход {ticker} {side} qty={qty} ждёт подтверждения "
                      f"(таймаут {float(getattr(cfg, 'ai_approval_timeout_sec', 45.0)):.0f}с, "
                      f"default={getattr(cfg, 'ai_approval_default', 'approve')})")
            self.events.log("AI_APPROVAL_REQUESTED", figi=figi, ticker=ticker,
                            order_id=order.id, side=side, qty=qty)
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
        # Пауза новых входов: НЕ открываем новые позиции из очереди (закрытия/выходы — можно).
        if order.action == "open" and self.entries_paused:
            order.status = "CANCELLED"
            self._log(f"ПАУЗА: отменён вход {order.ticker} ({order.side}) — entries_paused")
            self.events.log("ORDER_CANCELLED", figi=figi, ticker=order.ticker,
                            order_id=order.id, action="open", reason="entries_paused")
            return False
        # --- AI-гейт: ждём решение по входу (approve/reject), иначе таймаут → default ---
        if order.status == "PENDING_APPROVAL":
            _since = self._approvals_since.get(order.id, _time.monotonic())
            _tmo = float(getattr(cfg, "ai_approval_timeout_sec", 45.0) or 45.0)
            if (_time.monotonic() - _since) >= _tmo:
                _dflt = str(getattr(cfg, "ai_approval_default", "approve") or "approve").lower()
                if _dflt == "reject":
                    order.status = "CANCELLED"
                    self._approvals_since.pop(order.id, None)
                    self._log(f"AI-ГЕЙТ: таймаут {_tmo:.0f}с — вход {order.ticker} отклонён (default=reject)")
                    self.events.log("AI_APPROVAL_TIMEOUT", figi=figi, ticker=order.ticker,
                                    order_id=order.id, decision="reject")
                    return False
                order.status = "APPROVED"
                self._log(f"AI-ГЕЙТ: таймаут {_tmo:.0f}с — вход {order.ticker} одобрен (default=approve)")
                self.events.log("AI_APPROVAL_TIMEOUT", figi=figi, ticker=order.ticker,
                                order_id=order.id, decision="approve")
            if order.status == "PENDING_APPROVAL":
                self.pending_orders[figi] = order  # решение ещё не пришло — вернуть в очередь
                return False
            if order.status == "REJECTED":
                self._approvals_since.pop(order.id, None)
                _why = str((order.meta or {}).get("ai_reason") or "")[:120]
                self._log(f"AI-ГЕЙТ: вход {order.ticker} ОТКЛОНЁН — {_why}")
                self.events.log("AI_APPROVAL_REJECTED", figi=figi, ticker=order.ticker,
                                order_id=order.id, reason=(order.meta or {}).get("ai_reason"))
                return False
            self._approvals_since.pop(order.id, None)
        if order.action == "close":
            trade = await self.broker.close_position(figi, c.open, "signal_exit")
            actual_exit = price_from_trade(trade) if trade else c.open
            order.status = "FILLED"
            order.filled_at = datetime.now(timezone.utc)
            order.price = actual_exit
            self._held.discard(figi)
            self._clear_exit_state(figi)
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
            # SL: стандартный = cfg.initial_sl_atr (4×ATR, фикс вместо optuna sl_mult 4-5).
            # TP: из optuna-параметров rr стратегии figi (EnsembleParams).
            strat = self.strategies.get(figi)
            _sl_mult = cfg.initial_sl_atr
            _rr = getattr(strat.p, "rr", None) if strat is not None else None
            if _rr is None:
                _rr = cfg.atr_risk_reward
            # Проскальзывание на входе (adverse) — parity с бэктестом (fill_price).
            _cm = CostModel(commission_rate=cfg.commission_rate, slippage_bps=cfg.slippage_bps)
            _fill = _cm.fill_price(float(c.open), side)
            if cfg.sl_mode == "fixed":
                exit_policy = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
                plan = exit_policy.plan_entry(side, _fill, [])
            else:
                exit_policy = AtrStopPolicy(period=cfg.atr_period, multiplier=_sl_mult,
                                            risk_reward=_rr,
                                            trail_activation_comm_mult=cfg.trail_activation_comm_mult,
                                            trail_distance_r=cfg.trail_distance_atr,
                                            trail_compress_r=cfg.trail_compress_r,
                                            trail_min_factor=cfg.trail_min_factor,
                                            trail_min_atr=cfg.trail_min_atr,
                                            trail_vol_boost=cfg.trail_vol_boost)
                buf_raw = list(self.buffers.get(figi, []))
                plan = exit_policy.plan_entry(side, _fill, buf_raw)
        else:
            exit_policy = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
            _cm = CostModel(commission_rate=cfg.commission_rate, slippage_bps=cfg.slippage_bps)
            _fill = _cm.fill_price(float(c.open), side)
            plan = exit_policy.plan_entry(side, _fill, [])
        actual_entry = await self.broker.open_position(
            figi=figi,
            ticker=order.ticker,
            side=order.side,
            qty=order.qty,
            price=_fill,
            stop_loss=round(plan.stop_loss, 6) if plan.stop_loss is not None else None,
            take_profit=round(plan.take_profit, 6) if plan.take_profit is not None else None,
            strategy_id=cfg.strategy_id,
        )
        entry_px = actual_entry if actual_entry and actual_entry > 0 else c.open
        order.status = "FILLED"
        order.filled_at = datetime.now(timezone.utc)
        order.price = entry_px
        self._held.add(figi)
        self._held_since.setdefault(figi, __import__("time").monotonic())
        await self._st_open(figi, order.ticker, order.side, order.qty, entry_px,
                            plan.stop_loss, plan.take_profit, meta=order.meta,
                            leverage=max(1.0, float((order.meta or {}).get("leverage") or self.config.leverage or 1.0)))
        self._exit_plans[figi] = exit_policy
        self._exit_side[figi] = "LONG" if order.side == "BUY" else "SHORT"
        self._exit_entry_px[figi] = float(entry_px)
        _lot_entry = next((u.get("lot") for u in self.universe if u.get("figi") == figi), 1) or 1
        self._exit_qty[figi] = int(order.qty) * int(_lot_entry)
        self._trail_active[figi] = False
        self._trail_stop[figi] = float(plan.stop_loss) if plan.stop_loss is not None else 0.0
        if plan.take_profit is not None:
            self._exit_target[figi] = float(plan.take_profit)
        self._entry_bar_index[figi] = self._bar_counter
        self._log(f"СДЕЛКА ВХОД {order.ticker} {order.side} qty={order.qty} @ {entry_px:.2f} (candle={c.open:.2f})")
        self.events.log("ORDER_FILLED", figi=figi, ticker=order.ticker,
                        order_id=order.id, price=entry_px, action="open")
        self.events.log("POSITION_OPENED", figi=figi, ticker=order.ticker,
                        side=order.side, qty=order.qty, entry_price=entry_px)
        return True

    def _clear_exit_state(self, figi: str) -> None:
        """Полная очистка локального учёта выхода позиции (все поля)."""
        self._exit_plans.pop(figi, None)
        self._exit_side.pop(figi, None)
        self._exit_entry_px.pop(figi, None)
        self._exit_qty.pop(figi, None)
        self._trail_active.pop(figi, None)
        self._trail_stop.pop(figi, None)
        self._exit_target.pop(figi, None)

    async def _ensure_exit_state(self, figi: str, c, pos) -> None:
        """Ленивая инициализация учёта выхода, если позиция есть у брокера,
        но её нет в _exit_plans (рестарт/ручное открытие/reconcile).

        Уровни считаем от текущего ATR (5m), entry — из фактической позиции.
        """
        if figi in self._exit_plans:
            return
        if pos is None:
            return
        try:
            cfg = self.config
            _side = "LONG" if str(getattr(pos, "side", "LONG")).upper() in ("LONG", "BUY") else "SHORT"
            _side_enum = Side.BUY if _side == "LONG" else Side.SELL
            _entry_px = self._exit_entry_px.get(figi) or float(getattr(pos, "entry_price", 0) or c.open)
            if cfg.sl_mode == "fixed":
                _pol = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
                _pl = _pol.plan_entry(_side_enum, _entry_px, [])
            else:
                _strat = self.strategies.get(figi)
                _rrc = getattr(_strat.p, "rr", None) if _strat is not None else None
                _pol = AtrStopPolicy(period=cfg.atr_period,
                                     multiplier=cfg.initial_sl_atr,
                                     risk_reward=float(_rrc) if _rrc else cfg.atr_risk_reward,
                                     trail_activation_comm_mult=cfg.trail_activation_comm_mult,
                                     trail_distance_r=cfg.trail_distance_atr,
                                     trail_compress_r=cfg.trail_compress_r,
                                     trail_min_factor=cfg.trail_min_factor,
                                     trail_min_atr=cfg.trail_min_atr,
                                     trail_vol_boost=cfg.trail_vol_boost)
                _buf = self.buffers.get(figi)
                _bars1 = list(_buf) if _buf else [c]
                _pl = _pol.plan_entry(_side_enum, _entry_px, _bars1)
            self._exit_plans[figi] = _pol
            self._exit_side[figi] = _side
            self._exit_entry_px[figi] = float(_entry_px)
            self._exit_qty[figi] = int(getattr(pos, "qty", 0) or 0)
            self._trail_active[figi] = False
            # Источник уровней при восстановлении: сначала БД (ручные правки/прошлый
            # прогон переживают рестарт), иначе — расчёт от ATR.
            _db_sl = _db_tp = None
            try:
                from app.models.sandbox_trade import SandboxTrade
                from sqlalchemy import select as _sel
                async with SessionLocal() as _db:
                    _row = (await _db.execute(
                        _sel(SandboxTrade).where(
                            SandboxTrade.figi == figi, SandboxTrade.exit_time.is_(None),
                            SandboxTrade.mode == self.broker_mode)
                        .order_by(SandboxTrade.entry_time.desc()).limit(1)
                    )).scalar_one_or_none()
                    if _row is not None:
                        _db_sl = float(_row.stop_loss) if _row.stop_loss is not None else None
                        _db_tp = float(_row.take_profit) if _row.take_profit is not None else None
            except Exception:
                pass
            self._trail_stop[figi] = _db_sl if _db_sl is not None else (
                float(_pl.stop_loss) if _pl.stop_loss is not None else 0.0)
            if _db_tp is not None:
                self._exit_target[figi] = _db_tp
            elif _pl.take_profit is not None:
                self._exit_target[figi] = float(_pl.take_profit)
            self._entry_bar_index.setdefault(figi, self._bar_counter)
            self._log(f"EXIT-INIT {figi[-6:]} {_side} entry={_entry_px:.2f} "
                      f"sl={self._trail_stop[figi]:.2f} tp={self._exit_target.get(figi)} "
                      f"src={'db' if _db_sl is not None else 'atr'}")
        except Exception as _ei_e:
            self._log(f"exit-init error {figi[-6:]}: {_ei_e}")

    async def _step_exit(self, figi: str, c, pos) -> bool:
        """Единый шаг управления выходом позиции на одном баре.

        Зеркалит движок EngineRunner.run():
          1) пока трейлинг НЕ активирован — действует СТАНДАРТНЫЙ SL/TP
             (защита от разворота; уровни из нашего учёта, не из брокера);
          2) при pnl >= комиссия_входа × 4 трейлинг АКТИВИРУЕТСЯ:
             TP и сигнальные выходы отключаются;
          3) после активации стоп идёт за ценой (update_stop, ratchet),
             выход — только по трейлинговому стопу.
        Возвращает True, если позиция закрыта на этом баре.
        """
        from app.engine.exits import intrabar_exit as _ibe
        policy = self._exit_plans[figi]
        _raw_side = self._exit_side.get(figi) or getattr(pos, "side", "LONG")
        # pos.side у брокера = "BUY"/"SELL"; нормализуем к LONG/SHORT.
        side_str = "LONG" if str(_raw_side).upper() in ("LONG", "BUY") else "SHORT"
        state = PositionState.LONG if side_str == "LONG" else PositionState.SHORT
        trail_side = Side.BUY if state == PositionState.LONG else Side.SELL
        entry_px = self._exit_entry_px.get(figi)
        if not entry_px or entry_px <= 0:
            entry_px = float(getattr(pos, "entry_price", 0) or 0)
        qty_sh = int(self._exit_qty.get(figi) or getattr(pos, "qty", 0) or 0)
        comm = float(entry_px) * qty_sh * self.config.commission_rate
        # ATR/активация/ratchet — на 1m-барах, как в движке EngineRunner (test=bot).
        # Буфер уже содержит текущий бар (добавлен до вызова _step_exit).
        buf = self.buffers.get(figi)
        act_bars = list(buf) if buf else [c]

        upd = getattr(policy, "update_stop", None)
        act = getattr(policy, "trailing_activated", None)
        trail_active = bool(self._trail_active.get(figi, False))

        # 1) Если трейлинг уже активен — подтягиваем стоп за ценой (ratchet).
        new_stop = None
        if trail_active and upd is not None:
            try:
                new_stop = upd(trail_side, float(entry_px), self._trail_stop.get(figi),
                               act_bars, qty=qty_sh, commission=comm)
            except Exception as _te:
                self._log(f"update_stop error {figi[-6:]}: {_te}")
            if new_stop is not None:
                old_stop = self._trail_stop.get(figi)
                if old_stop is None or abs(new_stop - old_stop) > 1e-9:
                    direction = "вверх" if (trail_side == Side.BUY and new_stop > (old_stop or 0)) else ("вниз" if trail_side == Side.SELL and new_stop < (old_stop or float("inf")) else "=")
                    _dist_pct = abs(float(c.close) - new_stop) / float(c.close) * 100 if c.close else 0
                    self._log(f"ТРЕЙЛИНГ {figi[-6:]} стоп {old_stop if old_stop is not None else '-':.2f}→{new_stop:.2f} ({direction}) цена={c.close:.2f} дист={_dist_pct:.2f}%")
                    self.events.log("TRAILING_STOP", figi=figi, ticker=pos.ticker,
                                    stop=new_stop, prev_stop=old_stop)
                    self._trail_stop[figi] = new_stop
                    await self._st_update_sl(figi, new_stop, trail_active=True)

        # 2) Активация трейлинга при pnl >= комиссия_входа × 4.
        if not trail_active and act is not None:
            try:
                if act(trail_side, float(entry_px), qty_sh, comm, act_bars):
                    self._trail_active[figi] = True
                    trail_active = True
                    self._exit_target.pop(figi, None)  # TP выключается
                    self._log(f"ТРЕЙЛИНГ ВКЛ. {figi[-6:]} pnl>=комиссия*4 (0.05%*4=0.2%); сигнальные выходы и TP отключены")
                    self.events.log("TRAILING_ACTIVATED", figi=figi, ticker=pos.ticker,
                                    reason=f"pnl>=comm_x4 qty={qty_sh} comm={comm:.2f}")
                    _cur_sl = self._trail_stop.get(figi)
                    await self._st_update_sl(figi, _cur_sl if _cur_sl is not None else None, trail_active=True)
            except Exception as _trail_e:
                self._log(f"трейлинг-активация oshibka {figi[-6:]}: {_trail_e}")

        # 3) Выход на этом баре. TP активен только до активации трейлинга.
        tp = None if (trail_active or self._trail_active.get(figi, False)) else self._exit_target.get(figi)
        stop = self._trail_stop.get(figi)
        try:
            from app.config import settings as _s
            _dbg = bool(getattr(_s, "log_debug_engine", False))
        except Exception:
            _dbg = False
        if _dbg:
            self._log(f"DBG-EXIT {figi[-6:]} {state.value} entry={entry_px:.2f} qty={qty_sh} "
                      f"bar_ts={c.ts.strftime('%H:%M:%S')} o={c.open:.2f} h={c.high:.2f} l={c.low:.2f} c={c.close:.2f} "
                      f"stop={stop if stop is not None else '-'} tp={tp if tp is not None else '-'} "
                      f"trail={trail_active} pnl_rub={(c.close - entry_px) * qty_sh if state == PositionState.LONG else (entry_px - c.close) * qty_sh:+.2f}")
        price, reason = _ibe(c, state, stop, tp, close_based=bool(trail_active))
        if price is None:
            return False
        # Захватываем флаг трейлинга ДО _clear_exit_state (иначе диагностика врёт).
        _was_trail = bool(trail_active or self._trail_active.get(figi, False))
        # Проскальзывание на выходе (adverse), как в бэктесте (fill_price).
        try:
            _cm = CostModel(commission_rate=self.config.commission_rate,
                            slippage_bps=self.config.slippage_bps)
            _opp = Side.SELL if state == PositionState.LONG else Side.BUY
            price = _cm.fill_price(float(price), _opp)
        except Exception:
            pass

        trade = await self.broker.close_position(figi, price, reason)
        self._held.discard(figi)
        self._clear_exit_state(figi)
        _bh = self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter)
        await self._st_close(figi, price, reason=reason,
                              net=float(trade.net_pnl) if trade else None,
                              meta={"exit_reason": reason, "exit_price": float(price),
                                    "sl": stop, "tp": tp,
                                    "trailing": _was_trail,
                                    "bars_held": _bh})
        pnl = float(trade.net_pnl) if trade else 0
        tag = "ВЫХОД-ТРЕЙЛИНГ" if trail_active else "ВЫХОД"
        self._log(f"{tag} {figi[-6:]} ({reason}) pnl={pnl:+.2f}")
        self._opposite_count.pop(figi, None)
        self._last_exit_bar[figi] = self._bar_counter
        self.events.log("POSITION_CLOSED", figi=figi, ticker=pos.ticker,
                        reason=reason, net_pnl=float(trade.net_pnl) if trade else None,
                        trailing=bool(trail_active))
        await self._check_circuit_breaker()
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
