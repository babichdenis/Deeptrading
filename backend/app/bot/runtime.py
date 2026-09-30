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


def _msk_fmt(dt, fmt: str = "%H:%M:%S") -> str:
    """Время бара/события сразу в МСК — чтобы не думать, где UTC, а где локальное."""
    try:
        return dt.astimezone(ZoneInfo("Europe/Moscow")).strftime(fmt)
    except Exception:
        return str(dt)[:19]


def _skip_human(s: str) -> str:
    """Перевод причины «нет входа» с внутреннего жаргона на человеческий."""
    _s = str(s)
    if "session_blocked" in _s:
        return "торговая сессия сейчас не активна"
    if "no_fresh" in _s:
        return "нет свежего сигнала ансамбля за 30 мин (стрим исправен, просто давно не было входа по тикеру)"
    if "no_entries" in _s:
        # no_entries(cand=N rej=M: COMBO_BIAS|BUY->SELL:TREND_UPx8, REGIME_OFFx3, ...)
        _CODES = {
            "COMBO_BIAS": "сторона не совпала с bias режима",
            "REGIME_OFF": "режим не торгуется (bias_by_state)",
            "AGAINST_BIAS": "сигнал против общего bias",
            "SETUP_MISSING": "нет сигнала setup-стратегии",
            "VOL_FILTER": "объём ниже порога",
            "VOL_FLOW": "объёмный поток не подтвердил",
            "STOCH_FILTER": "сток/перекупленность за пределами",
            "IMOEX_VETO": "IMOEX-вето",
            "REGIME_MODE": "режим разрешает только одну сторону",
            "MACD_1M": "1m MACD против направления",
            "MACD_1M_AGAINST": "1m MACD против направления",
        }
        _SIDE_RU = {"BUY": "лонг", "SELL": "шорт", "запрет": "вход запрещён"}
        try:
            _parts = _s.split("(", 1)[1].rsplit(")", 1)[0]
            if ":" in _parts:
                _reasons = _parts.split(":", 1)[1]
            else:
                _reasons = _parts
            _detailed = []
            _chunks = _reasons.replace(",", " ").split()
            if any(c == "сигналов" for c in _chunks):
                _detailed = ["сигналов нет"]
            else:
                for _chunk in _chunks:
                    _chunk = _chunk.strip()
                    if not _chunk:
                        continue
                    if "x" in _chunk:
                        _code0, _, _n = _chunk.rpartition("x")
                        if _n.isdigit():
                            if "|" in _code0:
                                _meta, _, _pat = _code0.partition("|")  # COMBO_BIAS | BUY->SELL:TREND_UP
                                _src, _, _dst = _pat.partition("->")
                                _mode = _dst.rsplit(":", 1)[1] if ":" in _dst else ""
                                _dst_side = _dst.rsplit(":", 1)[0] if ":" in _dst else _dst
                                _label = (f"ансамбль хотел {_SIDE_RU.get(_src, _src)}, "
                                          f"bias {_mode} требует {_SIDE_RU.get(_dst_side, _dst_side)}")
                            else:
                                _label = _CODES.get(_code0, _code0)
                            _detailed.append(f"{_label} ({_n})")
                            continue
                        _detailed.append(_code0)
                        continue
                    _detailed.append(_chunk)
            if _detailed:
                return "все кандидаты отклонены фильтрами: " + "; ".join(_detailed)
        except Exception:
            pass
        return "все кандидаты отклонены фильтрами"
    if "quorum" in _s.lower():
        return "не набран кворум голосов стратегий"
    if "no_capital" in _s.lower() or "cash" in _s.lower() or "money" in _s.lower():
        return "недостаточно свободных средств"
    return _s[:110]



# --- audit 2026-09-18: тихие except не должны теряться молча ---------------------
_AUDIT_SWALLOW_SEEN = set()


def _audit_swallow(where, exc=None):
    """Логирует проглоченное исключение; 1 раз на место (анти-флуд для бота)."""
    if where in _AUDIT_SWALLOW_SEEN:
        return
    _AUDIT_SWALLOW_SEEN.add(where)
    try:
        import logging
        logging.getLogger(__name__).warning('SILENT-EXCEPT %s: %s: %s',
            where,
            type(exc).__name__ if exc is not None else "-",
            str(exc)[:120] if exc is not None else "")
    except Exception:
        pass


def price_from_trade(trade) -> float | None:
    """Extract executed price from PaperTrade."""
    if trade is None:
        return None
    try:
        return float(getattr(trade, "exit_price", None) or getattr(trade, "entry_price", None) or 0)
    except Exception as _sw_e:
        _audit_swallow('price_from_trade@L30', _sw_e)  # audit silent-except
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
ENSEMBLE_BUFFER = 14400  # 10 дней 1m-свечей (~240 часовых бара) — warmup RegimeDetector(ema_slow=50)+запас
DAILY_PNL_TTL = timedelta(seconds=30)
POS_PCT = 0.40  # доля портфеля на одну позицию (модель portfolio_merge)

# Файл, где хранятся настройки бота (единственный источник правды; БД — только резерв).
_BOT_CONFIG_FILE = str(Path(__file__).resolve().parents[2] / "data" / "bot_config.json")
_AI_CONTROL_FILE = str(Path(__file__).resolve().parents[2] / "data" / "ai_control.json")

# Режимы AI (селектор в шапке UI): какие нейронки активны + торгует ли двигатель.
AI_MODES: dict[str, dict] = {
    "bot":    {"gate": False, "watch": False, "trader": False, "engine": True,  "label": "🧱 Бот (без AI)"},
    "bot+":   {"gate": False, "watch": True,  "trader": False, "engine": True,  "label": "👁 Бот+ (вахтёр)"},
    "bot++":  {"gate": True,  "watch": True,  "trader": False, "engine": True,  "label": "🚦 Бот++ (воркер-гейт)"},
    "bot+++": {"gate": True,  "watch": True,  "trader": True,  "engine": True,  "label": "🤖 Бот+++ (AI-трейдер)"},
    "ai":     {"gate": False, "watch": True,  "trader": True,  "engine": False, "label": "🧠 AI-трейдер (только AI)"},
}


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
    # Инфо-трейлинг: если реальный выключен (трейлинг=None) но multi задан (напр. 4.0),
    # виртуально считаем, где бы сработал трейл, и пишем в карточку сделки (SL/TP не трогаем).
    trail_info_activation_comm_mult: float | None = None
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
    pos_pct: float = 0.40  # доля equity на одну позицию (слот), 0.4 = 40%
    max_positions: int = 5  # максимум одновременных позиций (0 = без лимита)
    reconcile_enabled: bool = True  # сверка позиций/кэша с брокером (только в торговое время)
    max_exposure_pct: float = 1.0  # свои деньги в позициях <= X от equity (1.0 = 100%, 0 = без лимита)
    max_short_share: float = 0.7  # макс. доля SHORT среди позиций (0 = без лимита)
    # --- Портфельные лимиты (в деньгах) ---
    max_net_exposure_pct: float = 0.5   # |net notional| <= X equity (0=выкл)
    max_sector_pct: float = 0.35        # notional сектора <= X equity (0=выкл)
    max_sector_positions: int = 0       # макс. позиций в одном секторе-кластере (0=выкл)
    beta_filter_enabled: bool = False    # лимит позиций по бета-группе (low/mid/high)
    confirmed_cluster_enabled: bool = False  # лимит позиций в подтверждённых кластерах
    max_margin_use_pct: float = 0.8     # starting_margin <= X equity (0=выкл)
    max_stress_loss_pct: float = 0.10   # убыток при ±5% IMOEX <= X equity (0=выкл)
    queue_enabled: bool = True           # очередь кандидатов: топ-1 по силе входит с бустом
    queue_ttl_min: int = 30              # время жизни кандидата в очереди (мин)
    queue_interval_sec: int = 120        # период проверки очереди (сек)
    queue_min_turnover: float = 300_000.0  # мин. дневной оборот тикера (₽) — иначе вето illiquid
    rank_enabled: bool = True            # ранжирование тикеров: разведка → топ-N по прошлому net
    rank_top_n: int = 10                 # сколько тикеров торгуем после разведки
    rank_explore: int = 10               # пробных сделок каждому тикеру
    rank_min_hist: int = 5               # мин. история для попадания в рейтинг
    queue_adv_multiple: float = 200.0    # слот ≤ 1/N дневного оборота (ликвидность под размер)
    top_sizing: str = "multiply"         # режим размера для топ-1 кандидата (divide|multiply)
    top_relax_caps: bool = True          # топ-1: net до 100% и сектор без лимита (стресс/маржа жёсткие)
    queue_history_veto: bool = True      # вето на явно токсичную историю (n≥20, net<−50₽, WR<25%)
    top_boost: float = 2.0               # множитель слота для топ-1 кандидата
    dd_reduce1_pct: float = 0.05         # просадка от пика equity → закрыть 50% позиций
    dd_reduce2_pct: float = 0.10         # просадка от пика equity → закрыть 80% позиций
    balance_min_positions: int = 3  # баланс L/S включается при >= N позиций
    ensemble_quorum: int = 2
    ensemble_session: str = "main"
    ensemble_entry_tf: str = "5min"  # ТФ свечей входа (micro_breakout): 1min | 5min | 10min | 15min
    ensemble_entry_from_setups: bool = True  # True: сторона из ансамблей; False: из micro_breakout
    ensemble_direction_sid: str = ""  # Путь 2: направление только от этой стратегии (пусто = общий режим)
    # --- Тройное подтверждение входа на 1м свечах ---
    entry_confirm_closes: int = 3  # N 1м-закрытий строго по направлению (BUY: каждое выше предыдущего)
    entry_confirm_closes_sides: list = field(default_factory=lambda: ["BUY", "SELL"])  # к каким сторонам
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
    confirm_flip: int = 3  # N встречных сигналов перед закрытием (0=отключено)
    invert_signals: bool = False  # ЭКСПЕРИМЕНТ: инвертировать сторону входа (проверка "обратной" логики)
    # --- Re-entry cooldown ---
    reentry_cooldown_bars: int = 30  # баров между выходом и повторным входом (0=отключено)
    # --- Overnight ---
    overnight: bool = False  # по умолчанию закрывать на конец торгового дня; True = держать через ночь
    eod_close_min_before: int = 10  # закрывать за N минут до конца последней сессии (overnight=False)
    # --- Bias-exit: закрывать позицию, если bias_by_state сменил знак против позиции ---
    # (по умолчанию ВЫКЛ: эталонный бэктест держит до EOD; опция — для экспериментов)
    bias_exit_enabled: bool = False
    daily_bias: bool = True          # дневной MACD-bias: блокировать входы против дневного направления
    daily_bias_mode: str = "veto"    # veto | info
    mtf_align: bool = False          # H1 MACD должен совпадать с дневным bias (подтверждение)
    ensemble_require_member: str = ""  # обязательный голос кворума (напр. "macd_cross")
    momentum_short: bool = False     # режим «моментум-шорт»: утром шорт низ-K по N-дневному падению
    momentum_n: int = 63             # окно моментума (торговых дней)
    momentum_k: int = 3              # сколько имён шортить
    momentum_stop_pct: float = 0.03  # внутридневной стоп (доля от входа)
    momentum_entry_time: str = "10:30"  # время входа (МСК)
    momentum_max_lev: float = 2.0    # кап плеча для моментум-входа
    momentum_only: bool = False      # только моментум: сигналы ансамбля игнорируются
    momentum_side: str = "short"     # направление моментума: short | long | both
    mtf_trigger: bool = False        # M5 MACD гистограмма должна разворачиваться в сторону входа
    # --- Детерминированные TF-гейты входа (правила AI-гейта, зашиты в движок) ---
    entry_h1_align: bool = True         # 7) H1 MACD должен подтверждать сторону входа
    entry_tf_conflict: bool = True      # 6) daily bias и H1 не должны противоречить
    entry_last_hour_block: bool = True  # 16) не входить в последний час сессии
    # --- Veto по накопленному движению (heatmap-часы из 1м) ---
    entry_hm_veto: bool = False         # вход против накопленного движения за окно → reject
    hm_veto_window_h: int = 24          # окно в часах (закрытые часовые бары)
    hm_veto_thr_pct: float = 3.0        # базовый порог |накопленного %|
    hm_veto_mode: str = "veto"          # veto | info
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
    replay_log_persist: bool = False  # тест: писать логи в БД bot_logs (без флага — только ring/UI)
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
    ai_reject_cooldown_min: float = 5.0   # пауза входов по тикеру после отклонения ИИ, мин (0=выкл)
    # --- Стакан/ликвидность: гейт для ВСЕХ входов (движок + AI), первый после ансамбля ---
    entry_ob_imbalance_max: float = 0.3  # блок входа против потока стакана сильнее X (0=выкл)
    entry_ob_spread_max: float = 25.0    # блок входа при спреде > X б.п. (0=выкл)
    entry_min_turnover: float = 0.0      # мин. дневной оборот тикера, ₽ (0=выкл)
    entry_volatility_max_mult: float = 3.0  # блок если ATR% > X × медианы (0=выкл)
    entry_news_blackout: bool = True     # блок входа по свежей негативной новости
    entry_news_blackout_min: int = 60    # окно новостей для стоп-блока, минут
    # --- AI-ордера (AI-трейдер): чейзинг и потолки SL/TP ---
    ai_chase_pct: float = 3.0         # блок входа после хода >X% за день без отката (0=выкл)
    ai_sl_max_pct: float = 0.03       # потолок SL для AI-ордера, доля (0=без потолка)
    ai_tp_max_pct: float = 0.08       # потолок TP для AI-ордера, доля (0=без потолка)


# Поля BotConfig, которые сохраняются в БД и восстанавливаются при старте бота.
BOT_PERSIST_FIELDS = (
    "sessions", "long_allowed", "short_allowed", "leverage",
    "margin_sessions", "margin_leverage", "margin_sizing",
    "trade_regimes", "trend_alignment",
    "trail_distance_atr", "trail_compress_r", "trail_min_factor", "trail_min_atr", "trail_vol_boost",
    "trail_activation_comm_mult", "trail_info_activation_comm_mult",
    "stop_pct", "target_pct", "sl_mode", "initial_sl_atr", "atr_period", "atr_multiplier",
    "atr_risk_reward", "top_n", "ensemble_quorum", "commission_rate",
    "overnight", "eod_close_min_before", "bias_exit_enabled", "daily_bias", "daily_bias_mode",
    "mtf_align", "mtf_trigger", "ensemble_require_member",
    "entry_h1_align", "entry_tf_conflict", "entry_last_hour_block",
    "entry_hm_veto", "hm_veto_window_h", "hm_veto_thr_pct", "hm_veto_mode",
    "momentum_short", "momentum_n", "momentum_k", "momentum_stop_pct",
    "momentum_entry_time", "momentum_max_lev", "momentum_only", "momentum_side",
    "reentry_cooldown_bars", "confirm_flip", "invert_signals", "ensemble_entry_tf", "ensemble_entry_from_setups", "ensemble_direction_sid",
    "pos_pct", "max_positions", "reconcile_enabled", "max_exposure_pct", "max_short_share", "balance_min_positions",
    "max_net_exposure_pct", "max_sector_pct", "max_margin_use_pct", "max_stress_loss_pct",
    "max_sector_positions",
    "beta_filter_enabled", "confirmed_cluster_enabled",
    "queue_enabled", "queue_ttl_min", "queue_interval_sec", "top_boost",
    "queue_min_turnover", "queue_adv_multiple", "queue_history_veto",
    "top_sizing", "top_relax_caps",
    "rank_enabled", "rank_top_n", "rank_explore", "rank_min_hist",
    "dd_reduce1_pct", "dd_reduce2_pct",
    "entry_confirm_closes", "entry_confirm_closes_sides",
    "loss_streak_hold", "loss_streak_n", "loss_streak_hold_min", "loss_streak_scope",
    "imoex_guard", "imoex_spike_pct", "imoex_spike_points", "imoex_spike_window_min",
    "imoex_release_frac", "imoex_min_block_min", "imoex_guard_min_beta", "imoex_chase_block_pct",
    "ai_approval", "ai_approval_timeout_sec", "ai_approval_default",
    "ai_reject_cooldown_min",
    "ai_chase_pct", "ai_sl_max_pct", "ai_tp_max_pct",
    "entry_ob_imbalance_max", "entry_ob_spread_max", "entry_min_turnover",
    "entry_volatility_max_mult", "entry_news_blackout", "entry_news_blackout_min",
    "daily_loss_limit", "max_margin_pct",
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
    except Exception as _sw_e:
        _audit_swallow('load_bot_settings@L243', _sw_e)  # audit silent-except
        pass
    try:
        async with SessionLocal() as db:
            row = await db.get(BotSetting, "runtime_config")
            return dict(row.value) if (row is not None and row.value) else {}
    except Exception as _sw_e:
        _audit_swallow('load_bot_settings@L249', _sw_e)  # audit silent-except
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
    except Exception as _sw_e:
        _audit_swallow('save_bot_settings@L268', _sw_e)  # audit silent-except
        pass
    try:
        async with SessionLocal() as db:
            row = await db.get(BotSetting, "runtime_config")
            if row is None:
                db.add(BotSetting(key="runtime_config", value=data))
            else:
                row.value = data
            await db.commit()
    except Exception as _sw_e:
        _audit_swallow('save_bot_settings@L278', _sw_e)  # audit silent-except
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
            "bias_mode": "info",
            "entry_tf": "5min", "sl_source": "manual", "sl_mult": 2.0, "rr": 4.0,
            "sl_override": 0.0, "rr_override": 0.0}


# Тестовые оверрайды конфига (применяются ТОЛЬКО в mode=test, live не трогают).
# Стратегия теста: H1 bias + сетапы 10м (ensemble_config.test.json) + триггер 5м.
TEST_MODE_OVERRIDES: dict = {
    "entry_from_setups": False,   # триггер входа = микро-брейкаут (entry_tf), кворум фильтрует
    "ensemble_entry_from_setups": False,
    "ensemble_entry_tf": "5min",
    "mtf_align": False,
    "mtf_trigger": False,
    "daily_bias": False,
    "daily_bias_mode": "info",
}
# Гейты: как на .2 (вкл) / полностью выкл. Управление: env TEST_GATES=on|off.
TEST_GATES_ON: dict = {
    "imoex_guard": True,
    "reentry_cooldown_bars": 15,
    "confirm_flip": 2,
    "trend_alignment": False,
    "ai_approval": False,
    "loss_streak_hold": True,
    "entry_confirm_closes": 0,
    "rank_enabled": True,
    "queue_enabled": True,
}
TEST_GATES_OFF: dict = {
    "imoex_guard": False,
    "reentry_cooldown_bars": 0,
    "confirm_flip": 0,
    "trend_alignment": False,
    "ai_approval": False,
    "loss_streak_hold": False,
    "entry_confirm_closes": 0,
    "rank_enabled": False,
    "queue_enabled": False,
}


TEST_VARIANTS: dict = {
    "base": {},
    "macd1": {"ensemble_require_member": "macd_cross", "ensemble_quorum": 2},
    "macd2": {"ensemble_require_member": "macd_cross", "ensemble_quorum": 3},
    "momentum": {"momentum_short": True, "momentum_k": 3, "momentum_n": 63,
                 "momentum_stop_pct": 0.03, "momentum_entry_time": "10:30",
                 "momentum_max_lev": 2.0, "momentum_only": True, "momentum_side": "short"},
}


_PRESET_FIELD_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sessions", ("sessions",)),
    ("margin_sessions", ("margin_sessions",)),
    ("overnight", ("overnight",)),
    ("eod_close_min_before", ("eod_close_min_before",)),
    ("initial_cash", ("money", "initial_cash")),
    ("qty_per_trade", ("money", "qty_per_trade")),
    ("pos_pct", ("money", "pos_pct")),
    ("max_positions", ("money", "max_positions")),
    ("max_exposure_pct", ("money", "max_exposure_pct")),
    ("sl_mode", ("exits", "sl_mode")),
    ("atr_period", ("exits", "atr_period")),
    ("atr_multiplier", ("exits", "atr_multiplier")),
    ("atr_risk_reward", ("exits", "atr_risk_reward")),
    ("initial_sl_atr", ("exits", "initial_sl_atr")),
    ("stop_pct", ("exits", "stop_pct")),
    ("target_pct", ("exits", "target_pct")),
    ("trail_activation_comm_mult", ("exits", "trail_activation_comm_mult")),
    ("trail_distance_atr", ("exits", "trail_distance_atr")),
    ("reentry_cooldown_bars", ("entry", "cooldown_bars")),
    ("confirm_flip", ("entry", "confirm_flip")),
    ("entry_last_hour_block", ("entry", "last_hour_block")),
    ("trade_regimes", ("regimes",)),
)
_MISS = object()


def _runtime_preset_overrides(rt: dict) -> dict:
    """runtime-блок пресета («ветки») -> оверрайды cfg (только whitelist).

    Семантика: гейты задаются СПИСКОМ включённых (имена как в TEST_GATES_ON) —
    всё остальное из TEST_GATES_OFF выключается; затем явные поля (деньги/выходы/
    вход/режимы) перекрывают значения из гейтов. None у exits применяется как
    «выключено» (напр. trail_activation_comm_mult: null). «regimes: all» — не трогаем.
    """
    out: dict = {}
    entry = rt.get("entry")
    if isinstance(entry, dict) and isinstance(entry.get("gates"), list):
        out.update(TEST_GATES_OFF)
        for g in entry["gates"]:
            if g in TEST_GATES_ON:
                out[g] = TEST_GATES_ON[g]
    for field, path in _PRESET_FIELD_PATHS:
        cur = rt
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                cur = _MISS
                break
            cur = cur[k]
        if cur is _MISS:
            continue
        if field == "trade_regimes":
            if isinstance(cur, str) and cur.strip().lower() in ("all", "", "any"):
                continue
            if not isinstance(cur, (list, tuple)):
                continue
        if field in ("sessions", "margin_sessions"):
            if not isinstance(cur, list) or not cur:
                continue
        out[field] = cur
    return out


def apply_test_overrides(cfg) -> list[str]:
    """Применить тестовые оверрайды (mode=test). Возвращает список применённых.

    env: TEST_GATES=on|off (гейты как на .2 / выкл), TEST_VARIANT=base|macd1|macd2.
    """
    import os as _os
    applied: list[str] = []
    _gates = str(_os.environ.get("TEST_GATES", "on") or "on").lower()
    _variant = str(_os.environ.get("TEST_VARIANT", "base") or "base").lower()
    _src = dict(TEST_MODE_OVERRIDES)
    _src.update(TEST_GATES_ON if _gates != "off" else TEST_GATES_OFF)
    _src.update(TEST_VARIANTS.get(_variant, {}))
    # Движок теста: env TEST_ENGINE=ose_all -> одиночная стратегия из реестра
    # (use_ensemble=False), иначе ensemble_v4 с кворумом из ensemble_config.test.json.
    _engine = str(_os.environ.get("TEST_ENGINE", "") or "").strip().lower()
    if _engine:
        _src["strategy_id"] = _engine
        _src["use_ensemble"] = False
    # TF теста: env TEST_INTERVAL=5min -> interval_name (реплей агрегирует 1m через Resampler).
    _tf = str(_os.environ.get("TEST_INTERVAL", "") or "").strip().lower()
    if _tf:
        _src["interval_name"] = _tf
    # Параметры одиночного движка: env TEST_PARAMS='{"quorum":2}' (JSON -> cfg.params).
    _tp = str(_os.environ.get("TEST_PARAMS", "") or "").strip()
    if _tp:
        try:
            import json as _json
            _p = _json.loads(_tp)
            if isinstance(_p, dict):
                _src["params"] = _p
        except Exception as _tp_e:
            _audit_swallow('apply_test_overrides@test_params', _tp_e)
    # Пресет («ветка») конфигурации: env TEST_PRESET='{"runtime": {...}}' — whitelist
    # полей (сессии/overnight/деньги/выходы/вход/гейты/режимы), раньше живших в .env/сейве.
    _tps = str(_os.environ.get("TEST_PRESET", "") or "").strip()
    if _tps:
        try:
            import json as _json2
            _pres = _json2.loads(_tps)
            _rt = _pres.get("runtime") if isinstance(_pres, dict) else None
            if isinstance(_rt, dict):
                for _pk, _pv in _runtime_preset_overrides(_rt).items():
                    _src[_pk] = _pv
                applied.append("preset=runtime")
        except Exception as _tpr_e:
            _audit_swallow('apply_test_overrides@test_preset', _tpr_e)
    for _k, _v in _src.items():
        try:
            setattr(cfg, _k, _v)
            applied.append(f"{_k}={_v}")
        except Exception as _sw_e:
            _audit_swallow('apply_test_overrides@L371', _sw_e)  # audit silent-except
            pass
    return applied


async def load_ensemble_config(mode: str = "") -> dict:
    """Состав кворума из data/ensemble_config.json (источник правды), дефолт если нет.

    ПАРИТЕТ-СВИЧ (2026-09-24): для mode="test" грузится ensemble_config.test.json —
    осознанный тестовый конфиг (10min setups, SL manual 2.0, TREND-only bias).
    Live/sandbox — ensemble_config.json (5min, optuna SL). Файл остаётся
    приоритетным поверх дефолтов.
    """
    _files = []
    if str(mode) == "test":
        _files.append(str(Path(_ENSEMBLE_CONFIG_FILE).with_name("ensemble_config.test.json")))
    _files.append(_ENSEMBLE_CONFIG_FILE)
    for _f in _files:
        try:
            _p = Path(_f)
            if _p.exists():
                _d = json.loads(_p.read_text(encoding="utf-8"))
                if isinstance(_d, dict) and _d.get("setups"):
                    # Дополняем недостающие ключи дефолтами (напр. bias_mode в старых
                    # ensemble_config.json) — файл остаётся приоритетным.
                    _base = default_ensemble_config()
                    _base.update(_d)
                    return _base
        except Exception as _sw_e:
            _audit_swallow('load_ensemble_config@L393', _sw_e)  # audit silent-except
            continue
    return default_ensemble_config()


async def save_ensemble_config(cfg: dict) -> None:
    try:
        _p = Path(_ENSEMBLE_CONFIG_FILE)
        _p.parent.mkdir(parents=True, exist_ok=True)
        _tmp = _p.with_suffix(".json.tmp")
        _tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        _tmp.replace(_p)
    except Exception as _sw_e:
        _audit_swallow('save_ensemble_config@L405', _sw_e)  # audit silent-except
        pass


async def load_bot_flags() -> dict:
    """Прочитать персистентные runtime-флаги (entries_paused и т.п.) из bot_settings."""
    from app.models.bot_setting import BotSetting
    try:
        async with SessionLocal() as db:
            row = await db.get(BotSetting, _BOT_FLAGS_KEY)
            return dict(row.value) if (row is not None and row.value) else {}
    except Exception as _sw_e:
        _audit_swallow('load_bot_flags@L416', _sw_e)  # audit silent-except
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
    except Exception as _sw_e:
        _audit_swallow('save_bot_flags@L433', _sw_e)  # audit silent-except
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
        self.tcs_to_bbg: dict[str, str] = {}   # figi T-Invest → BBG (заполняется на старте)
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
        self._replay_to: datetime | None = None
        self._last_replay_log: float = -1.0
        self._signal_busy: set[str] = set()
        self._skip_logged: dict[str, object] = {}  # figi -> ts последней залогированной причины "нет входа"
        self._held: set[str] = set()
        self._swing: set[str] = set()  # figis AI-сделок "swing" (не закрывать на EOD/ночь)
        self._peak_pnl: dict[str, dict] = {}  # figi -> {pnl, ts, price, atr_pct} точка макс. прибыли позиции
        # Виртуальный (info) трейлинг — считаем, где бы сработал трейл, НЕ меняя реальный SL/TP.
        # figi -> {active: bool, trail_stop: float, trail_dist_atr: float, dist_pct_now: float,
        #          peak_px: float, peak_pnl: float, peak_atr_pct: float,
        #          hit_ts: str, hit_price: float, hit_reason: str}
        self._trail_info: dict[str, dict] = {}
        self._held_since: dict[str, float] = {}  # figi -> время добавления в _held (для grace синка)
        self._log_persist_queue: deque[tuple[str, str, str, str]] = deque(maxlen=2000)  # (level, source, msg, ts_msk)
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
        self._bias_exit_ts: dict[str, float] = {}  # figi -> last bias-exit init (monotonic), anti-spam
        # --- Re-entry cooldown tracking ---
        self._last_exit_bar: dict[str, int] = {}  # figi -> bar number of last exit
        self._bar_counter: int = 0  # global bar counter
        self._just_opened_this_candle: set[str] = set()  # figis opened this candle
        self._entry_bar_index: dict[str, int] = {}  # figi -> bar_index at entry
        self._no_trade_stats: dict[str, int] = {}  # reason -> count (NO_TRADE diagnostics)
        self._regimes: dict[str, dict] = {}  # figi -> {state, vol} последних 5м баров
        self._entry_regime: dict[str, str | None] = {}  # figi -> regime, зафиксированный НА ВХОДЕ (сразу и навсегда)
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
        # Пик P&L сделки: лучший ход в нашу сторону (макс. цена при лонге /
        # мин. при шорте) + MAE; обновляется в _step_exit на каждом баре,
        # пишется в exit_meta при закрытии (_st_close).
        self._peak_pnl: dict[str, dict] = {}   # figi -> {pnl, ts, price, atr_abs, atr_pct, mae}
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
        self._ai_reject_reason: dict[str, str] = {}     # последняя причина отклонения по тикеру
        self._ai_reject_last_log: dict[str, float] = {}  # monotonic-время последнего лога «на паузе»
        self._ai_reject_cnt: dict[str, int] = {}        # сколько сигналов пропущено в паузе
        # --- Экспозиция: плечо по каждой позиции (для капа «свои ≤ equity») ---
        self._pos_leverage: dict[str, float] = {}
        # --- Портфель: кэш меты (sector/beta) и снапшота ---
        self._sector_meta: dict[str, dict] = {}
        self._sector_meta_ts: float = 0.0
        self._pf_cache: tuple[float, dict] | None = None
        self._regime_cache: tuple[float, dict] | None = None
        self._hist_cache: dict[str, dict] = {}
        self._hist_ts: float = 0.0
        self._turnover_cache: dict[str, float] = {}
        self._turnover_ts: float = 0.0
        self._cand_queue: dict[str, dict] = {}
        self._equity_peak: float = 0.0
        self._mtf_cache: dict[str, dict] = {}
        self._mtf_ts: float = 0.0
        self._hm_cache: dict[int, dict[str, tuple]] = {}
        self._hm_ts: float = 0.0
        self._momentum_done: object = None
        self._momentum_cache: dict[str, float] = {}
        self._momentum_ts: float = 0.0
        self._daily_bias_cache: dict[str, dict] = {}
        self._daily_bias_ts: float = 0.0
        self._overnight_cache: list[str] | None = None
        self._overnight_ts: float = 0.0
        self._dd_level_done: int = 0
        # --- AI-гейт: последние решения ИИ (shadow/боевые) для UI ---
        self._ai_decisions: deque = deque(maxlen=50)
        self._ai_notes: deque = deque(maxlen=50)  # заметки вахтёра позиций (llama)
        self._ai_prompt: dict = {}  # текущий промпт/модель AI-гейта (для UI)
        # Управление AI из UI: переопределения промптов (gate/watch/trader) + срочная заметка.
        self._ai_control: dict = {"prompts": {}, "note": "", "updated_ts": ""}
        self._ai_save_task = None  # ссылка на фоновое сохранение настроек AI (защита от GC)
        self._ai_defaults: dict = {}  # дефолтные промпты воркеров (gate/watch/trader)
        try:
            _acp = Path(_AI_CONTROL_FILE)
            if _acp.exists():
                _acd = json.loads(_acp.read_text(encoding="utf-8"))
                if isinstance(_acd, dict):
                    self._ai_control.update(_acd)
        except Exception as _sw_e:
            _audit_swallow('__init__@L637', _sw_e)  # audit silent-except
            pass
        self._ai_report: dict = {}  # отчёт AI о рынке + предложения по боту (для UI)

    async def sector_meta(self) -> dict:
        """{ticker: {sector, beta}} из instruments (кэш 10 мин)."""
        import time as _t
        if self._sector_meta and (_t.monotonic() - self._sector_meta_ts) < 600:
            return self._sector_meta
        try:
            from sqlalchemy import text as _text
            async with SessionLocal() as db:
                rows = (await db.execute(_text(
                    "SELECT ticker, figi, coalesce(sector,'other'), coalesce(imoex_beta,0), "
                    "coalesce(dlong,0), coalesce(dshort,0) "
                    "FROM instruments WHERE sector IS NOT NULL OR imoex_beta IS NOT NULL"
                ))).all()
            _m: dict[str, dict] = {}
            for r in rows:
                _v = {"sector": str(r[2]), "beta": float(r[3] or 0.0),
                      "dlong": float(r[4] or 0.0), "dshort": float(r[5] or 0.0)}
                _m[str(r[0]).upper()] = _v
                if r[1]:
                    _m[str(r[1])] = _v
            self._sector_meta = _m
            self._sector_meta_ts = _t.monotonic()
        except Exception as _sw_e:
            _audit_swallow('sector_meta@L663', _sw_e)  # audit silent-except
            pass
        return self._sector_meta

    async def portfolio_snapshot(self, ttl: float = 10.0) -> dict:
        """Сводка портфеля: экспозиции/сектора/маржа/стресс (кэш ttl сек)."""
        import time as _t
        from app.bot.portfolio import snapshot as _snap
        if self._pf_cache and (_t.monotonic() - self._pf_cache[0]) < ttl:
            return self._pf_cache[1]
        equity = 0.0
        positions: list[dict] = []
        margin: dict = {}
        try:
            equity = float(await self.broker.equity() or 0.0)
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L678', _sw_e)  # audit silent-except
            pass
        try:
            for p in (await self.broker.positions()):
                bb = self.tcs_to_bbg.get(getattr(p, "figi", ""), getattr(p, "figi", ""))
                buf = self.buffers.get(bb)
                last = float(buf[-1].close) if buf else float(getattr(p, "entry_price", 0) or 0)
                positions.append({
                    "ticker": str(getattr(p, "ticker", "") or self.tickers.get(bb, "")).upper(),
                    "figi": bb,
                    "side": "LONG" if str(getattr(p, "side", "")).upper() in ("LONG", "BUY") else "SHORT",
                    "qty": abs(float(getattr(p, "qty", 0) or 0)),
                    "entry": float(getattr(p, "entry_price", 0) or 0),
                    "last": last,
                })
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L693', _sw_e)  # audit silent-except
            pass
        _ma_fn = getattr(self.broker, "margin_attributes", None)
        if _ma_fn is not None:
            try:
                margin = await _ma_fn() or {}
            except Exception as _sw_e:
                _audit_swallow('portfolio_snapshot@L699', _sw_e)  # audit silent-except
                margin = {}
        meta = await self.sector_meta()
        snap = _snap(equity, positions, meta, margin)
        snap["skip_counts"] = self.get_no_trade_stats()
        try:
            await self.trade_history()
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L706', _sw_e)  # audit silent-except
            pass
        try:
            _mtfm = await self.mtf_macd_map()
            snap["mtf"] = {k: {"h1": (v.get("h1") or {}).get("side"),
                               "m5": (v.get("m5") or {}).get("side"),
                               "m5_trend": (v.get("m5") or {}).get("trend")}
                           for k, v in list(_mtfm.items())[:40]}
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L714', _sw_e)  # audit silent-except
            snap["mtf"] = {}
        try:
            _dbm = await self.daily_bias_map()
            snap["daily_bias"] = {k: v.get("bias") for k, v in list(_dbm.items())[:40]}
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L719', _sw_e)  # audit silent-except
            snap["daily_bias"] = {}
        try:
            snap["regime"] = await self.market_regime()
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L723', _sw_e)  # audit silent-except
            snap["regime"] = {}
        snap["queue"] = sorted(
            [{"ticker": c.get("ticker"), "side": c.get("side"), "score": c.get("score"),
              "factors": c.get("factors"), "veto": c.get("veto"), "hist": c.get("hist"),
              "why": c.get("why"), "ts": c.get("ts")} for c in self._cand_queue.values()],
            key=lambda x: x.get("score") or 0, reverse=True)[:10]
        try:
            _ranked = sorted(((k, float(v.get("net") or 0.0)) for k, v in (self._hist_cache or {}).items()
                              if int(v.get("n") or 0) >= int(getattr(self.config, "rank_min_hist", 5) or 0)),
                             key=lambda kv: -kv[1])
            snap["rank"] = {
                "enabled": bool(getattr(self.config, "rank_enabled", True)),
                "top_n": int(getattr(self.config, "rank_top_n", 10) or 0),
                "explore": int(getattr(self.config, "rank_explore", 10) or 0),
                "top": [k for k, _ in _ranked[:int(getattr(self.config, "rank_top_n", 10) or 0)]],
            }
        except Exception as _sw_e:
            _audit_swallow('portfolio_snapshot@L740', _sw_e)  # audit silent-except
            snap["rank"] = {}
        snap["dd"] = {"peak": round(self._equity_peak, 2),
                      "dd_pct": round(max(0.0, (self._equity_peak - equity) / self._equity_peak), 4)
                      if self._equity_peak > 0 else 0.0,
                      "level_done": self._dd_level_done}
        self._pf_cache = (_t.monotonic(), snap)
        return snap

    async def market_regime(self, ttl: float = 30.0) -> dict:
        """Режим рынка: bear/bull/neutral/reversal по IMOEX (20м/60м/день) + breadth."""
        import time as _t
        if self._regime_cache and (_t.monotonic() - self._regime_cache[0]) < ttl:
            return self._regime_cache[1]
        now = self._bot_now()
        last_v = self._imoex_buf[-1][1] if self._imoex_buf else None

        def _pct(mins: int) -> float | None:
            if last_v is None:
                return None
            target = now - timedelta(minutes=mins)
            ref = next((v for t, v in reversed(self._imoex_buf) if t <= target), None)
            return ((last_v - ref) / ref * 100) if ref else None

        p20, p60 = _pct(20), _pct(60)
        pday = None
        try:
            from zoneinfo import ZoneInfo as _ZI
            _msk = _ZI("Europe/Moscow")
            _day = now.astimezone(_msk).date()
            _dv = [v for t, v in self._imoex_buf if t.astimezone(_msk).date() == _day]
            if _dv and last_v:
                pday = (last_v - _dv[0]) / _dv[0] * 100
        except Exception as _sw_e:
            _audit_swallow('_pct@L773', _sw_e)  # audit silent-except
            pass
        br_up = None
        try:
            from app.api.routes.screener import market_breadth
            br_up = (market_breadth() or {}).get("up_pct")
        except Exception as _sw_e:
            _audit_swallow('_pct@L779', _sw_e)  # audit silent-except
            br_up = None
        st = "neutral"
        if p20 is not None and p60 is not None:
            if p60 <= -0.3 and p20 <= 0.1 and (br_up is None or br_up <= 50):
                st = "bear"
            elif p60 >= 0.3 and p20 >= -0.1 and (br_up is None or br_up >= 50):
                st = "bull"
            if (p60 <= -0.3 and p20 >= 0.25) or (p60 >= 0.3 and p20 <= -0.25):
                st = "reversal"
        out = {"state": st,
               "pct_20m": round(p20, 3) if p20 is not None else None,
               "pct_60m": round(p60, 3) if p60 is not None else None,
               "pct_day": round(pday, 3) if pday is not None else None,
               "breadth_up_pct": br_up,
               "ts": datetime.now(timezone.utc).isoformat()}
        self._regime_cache = (_t.monotonic(), out)
        return out

    async def trade_history(self) -> dict:
        """История сделок по тикерам: {ticker: {n, wr, wr5, net}} + ликвидность (кэш 10 мин)."""
        import time as _t
        if self._hist_cache and (_t.monotonic() - self._hist_ts) < 600:
            return self._hist_cache
        try:
            from sqlalchemy import text as _text
            async with SessionLocal() as db:
                rows = (await db.execute(_text(
                    "SELECT ticker, net_pnl FROM sandbox_trades "
                    "WHERE net_pnl IS NOT NULL AND exit_time IS NOT NULL "
                    "ORDER BY exit_time DESC"
                ))).all()
                uni = (await db.execute(_text(
                    "SELECT ticker, coalesce(avg_daily_turnover, 0) FROM universe"
                ))).all()
            out: dict[str, dict] = {}
            for r in rows:
                tk = str(r[0] or "").upper()
                if not tk:
                    continue
                pnl = float(r[1] or 0.0)
                d = out.setdefault(tk, {"n": 0, "wins": 0, "net": 0.0, "last5": []})
                d["n"] += 1
                d["net"] += pnl
                if pnl > 0:
                    d["wins"] += 1
                if len(d["last5"]) < 5:
                    d["last5"].append(1 if pnl > 0 else 0)
            for d in out.values():
                d["wr"] = d["wins"] / d["n"] if d["n"] else 0.0
                d["wr5"] = (sum(d["last5"]) / len(d["last5"])) if d["last5"] else d["wr"]
                d.pop("last5", None)
            self._hist_cache = out
            self._turnover_cache = {str(r[0] or "").upper(): float(r[1] or 0.0) for r in uni}
            # Реальный дневной оборот — с MOEX ISS (скринер), колонка universe часто устарела.
            try:
                from app.api.routes.screener import fetch_tqbr_market
                q = await asyncio.to_thread(fetch_tqbr_market)
                for tk, row in (q or {}).items():
                    val = float(row.get("turnover") or 0.0)
                    if val > 0:
                        self._turnover_cache[str(tk).upper()] = val
            except Exception as _sw_e:
                _audit_swallow('trade_history@L841', _sw_e)  # audit silent-except
                pass
            self._hist_ts = _t.monotonic()
        except Exception as _sw_e:
            _audit_swallow('trade_history@L844', _sw_e)  # audit silent-except
            pass
        return self._hist_cache

    async def daily_bias_map(self, ttl: float = 900.0) -> dict[str, dict]:
        """Дневной MACD-bias по тикерам (кэш 15 мин): {ticker: {bias, hist, bars}}.

        В реплее кэш отключён и берётся срез на момент реплея (иначе look-ahead).
        """
        import time as _t
        _replay = str(getattr(self.config, "feed", "")) == "replay"
        if (not _replay and self._daily_bias_cache
                and (_t.monotonic() - self._daily_bias_ts) < ttl):
            return self._daily_bias_cache
        _cutoff = self._bot_now()
        from app.bot.daily_bias import bias_from_closes as _bfc
        out: dict[str, dict] = {}
        try:
            from sqlalchemy import text as _text
            async with SessionLocal() as db:
                _figs = [u.get("figi") for u in (self.universe or []) if u.get("figi")]
                if not _figs:
                    return self._daily_bias_cache
                # Дневные бары (interval=24, качаются scripts/download_daily_candles.py)
                rows = (await db.execute(_text(
                    "SELECT figi, close FROM candles WHERE interval = 24 "
                    "AND figi = ANY(:fs) AND ts <= :cut ORDER BY figi, ts"
                ), {"fs": _figs, "cut": _cutoff})).all()
                if not rows:
                    # fallback: собираем дневные закрытия из 5м свечей
                    rows = (await db.execute(_text(
                        "SELECT figi, close FROM ("
                        "  SELECT figi, (ts AT TIME ZONE 'Europe/Moscow')::date AS d, ts, close,"
                        "         row_number() OVER (PARTITION BY figi,"
                        "                            (ts AT TIME ZONE 'Europe/Moscow')::date"
                        "                            ORDER BY ts DESC) AS rn"
                        "  FROM candles WHERE interval = 5 AND figi = ANY(:fs)"
                        "        AND ts <= :cut AND ts > :cut - interval '90 days'"
                        ") t WHERE rn = 1 ORDER BY figi, d"
                    ), {"fs": _figs, "cut": _cutoff})).all()
            by_figi: dict[str, list[float]] = {}
            for figi, close in rows:
                by_figi.setdefault(str(figi), []).append(float(close or 0.0))
            for figi, closes in by_figi.items():
                tk = str(self.tickers.get(figi, "") or "").upper()
                if not tk:
                    continue
                out[tk] = _bfc(closes)
            self._daily_bias_cache = out
            self._daily_bias_ts = _t.monotonic()
        except Exception as _sw_e:
            _audit_swallow('daily_bias_map@L894', _sw_e)  # audit silent-except
            pass
        return self._daily_bias_cache

    async def mtf_macd_map(self, ttl: float = 900.0) -> dict[str, dict]:
        """M5 и H1 MACD по тикерам (кэш 15 мин): {ticker: {m5: {...}, h1: {...}}}."""
        import time as _t
        _replay = str(getattr(self.config, "feed", "")) == "replay"
        if not _replay and self._mtf_cache and (_t.monotonic() - self._mtf_ts) < ttl:
            return self._mtf_cache
        _cutoff = self._bot_now()
        from app.bot.daily_bias import macd_state as _ms
        out: dict[str, dict] = {}
        try:
            from sqlalchemy import text as _text
            _figs = [u.get("figi") for u in (self.universe or []) if u.get("figi")]
            if not _figs:
                return self._mtf_cache
            async with SessionLocal() as db:
                rows = (await db.execute(_text(
                    "SELECT figi, ts, close FROM candles WHERE interval = 5 "
                    "AND figi = ANY(:fs) AND ts <= :cut "
                    "AND ts > :cut - interval '20 days' ORDER BY figi, ts"
                ), {"fs": _figs, "cut": _cutoff})).all()
            by_figi: dict[str, list[tuple]] = {}
            for figi, ts, close in rows:
                by_figi.setdefault(str(figi), []).append((ts, float(close or 0.0)))
            for figi, seq in by_figi.items():
                tk = str(self.tickers.get(figi, "") or "").upper()
                if not tk:
                    continue
                m5 = _ms([c for _ts, c in seq])
                # H1: последнее закрытие каждого часового бакета
                h1: list[float] = []
                bucket = None
                last_close = None
                for ts, c in seq:
                    b = ts.replace(minute=0, second=0, microsecond=0)
                    if bucket is not None and b != bucket and last_close is not None:
                        h1.append(last_close)
                    bucket, last_close = b, c
                if last_close is not None:
                    h1.append(last_close)
                out[tk] = {"m5": m5, "h1": _ms(h1)}
            self._mtf_cache = out
            self._mtf_ts = _t.monotonic()
        except Exception as _sw_e:
            _audit_swallow('mtf_macd_map@L940', _sw_e)  # audit silent-except
            pass
        return self._mtf_cache

    async def hm_map(self, window_h: int = 24, ttl: float = 180.0) -> dict[str, tuple]:
        """Накопленное движение за window_h часов по тикерам (heatmap-карта).

        Возвращает {ticker: (pct, dur)}:
          pct — % изменения цены за окно (часовые бары, закрытия из 1м свечей БД);
          dur — длительность тренда: сколько последних часов подряд цена шла в ту же
                сторону, что суммарное смещение за окна (для прогрессивного veto).
        """
        import time as _t
        _replay = str(getattr(self.config, "feed", "")) == "replay"
        if not _replay and self._hm_cache and (_t.monotonic() - self._hm_ts) < ttl:
            return self._hm_cache.get(window_h) or {}
        _cut = self._bot_now()
        _frm = _cut - timedelta(hours=max(1, int(window_h)))
        out: dict[str, tuple] = {}
        try:
            from sqlalchemy import text as _text
            _figs = [u.get("figi") for u in (self.universe or []) if u.get("figi")]
            if _figs:
                async with SessionLocal() as db:
                    rows = (await db.execute(_text(
                        "SELECT figi, date_trunc('hour', ts) AS h, "
                        "(array_agg(close ORDER BY ts DESC))[1] AS close "
                        "FROM candles WHERE interval = 1 AND figi = ANY(:fs) "
                        "AND ts >= :frm AND ts <= :cut "
                        "GROUP BY figi, h ORDER BY figi, h"
                    ), {"fs": _figs, "frm": _frm, "cut": _cut})).all()
                by: dict[str, list[float]] = {}
                for figi, _h, close in rows:
                    by.setdefault(str(figi), []).append(float(close or 0.0))
                for figi, closes in by.items():
                    tk = str(self.tickers.get(figi, "") or "").upper()
                    if not tk or len(closes) < 2:
                        continue
                    c0, c1 = closes[0], closes[-1]
                    if c0 <= 0:
                        continue
                    pct = round((c1 / c0 - 1.0) * 100.0, 3)
                    dur = 0
                    _sign = 1 if pct >= 0 else -1
                    for i in range(len(closes) - 1, 0, -1):
                        _ch = closes[i] - closes[i - 1]
                        if (_ch >= 0 and _sign >= 0) or (_ch < 0 and _sign < 0):
                            dur += 1
                        else:
                            break
                    out[tk] = (pct, dur)
                self._hm_cache = {}
                self._hm_cache[window_h] = out
                self._hm_ts = _t.monotonic()
        except Exception as _sw_e:
            _audit_swallow('hm_map', _sw_e)  # audit silent-except
            pass
        return out

    async def momentum_map(self, ttl: float = 1800.0) -> dict[str, float]:
        """{ticker: изменение за N дней, %} по дневным барам (кэш 30 мин)."""
        import time as _t
        _replay = str(getattr(self.config, "feed", "")) == "replay"
        if not _replay and self._momentum_cache and (_t.monotonic() - self._momentum_ts) < ttl:
            return self._momentum_cache
        _n = max(2, int(getattr(self.config, "momentum_n", 63) or 63))
        _cutoff = self._bot_now()
        out: dict[str, float] = {}
        try:
            from sqlalchemy import text as _text
            _figs = [u.get("figi") for u in (self.universe or []) if u.get("figi")]
            if not _figs:
                return self._momentum_cache
            async with SessionLocal() as db:
                rows = (await db.execute(_text(
                    "SELECT figi, close FROM candles WHERE interval = 24 "
                    "AND figi = ANY(:fs) AND ts <= :cut ORDER BY figi, ts"
                ), {"fs": _figs, "cut": _cutoff})).all()
            by: dict[str, list[float]] = {}
            for figi, close in rows:
                by.setdefault(str(figi), []).append(float(close or 0.0))
            for figi, closes in by.items():
                tk = str(self.tickers.get(figi, "") or "").upper()
                if not tk or len(closes) < _n + 1:
                    continue
                c0, c1 = closes[-_n - 1], closes[-1]
                if c0 > 0:
                    out[tk] = round((c1 / c0 - 1.0) * 100.0, 1)
            self._momentum_cache = out
            self._momentum_ts = _t.monotonic()
        except Exception as _sw_e:
            _audit_swallow('momentum_map@L975', _sw_e)  # audit silent-except
            pass
        return self._momentum_cache

    async def _momentum_loop(self) -> None:
        """Моментум-шорт: в окне времени входа шортим низ-K по N-дневному падению.

        Выход — вечерний EOD (overnight=false), стоп — momentum_stop_pct (3%).
        """
        from zoneinfo import ZoneInfo as _ZI
        _msk = _ZI("Europe/Moscow")
        while self.running:
            try:
                # В реплее время идёт быстрее wall-clock — спим коротко, иначе окно входа
                # (30 мин) проскакивается между итерациями.
                _replay = str(getattr(self.config, "feed", "")) == "replay"
                await asyncio.sleep(0.5 if _replay else 30.0)
                cfg = self.config
                if not bool(getattr(cfg, "momentum_short", False)):
                    continue
                now = self._bot_now()
                msk = now.astimezone(_msk)
                if msk.weekday() >= 5:
                    continue
                _et = str(getattr(cfg, "momentum_entry_time", "10:30") or "10:30")
                try:
                    _eh, _em = (int(x) for x in _et.split(":"))
                except Exception as _sw_e:
                    _audit_swallow('_momentum_loop@L1002', _sw_e)  # audit silent-except
                    _eh, _em = 10, 30
                _mins = msk.hour * 60 + msk.minute
                _from = _eh * 60 + _em
                if not (_from <= _mins < _from + 30):
                    continue
                if self._momentum_done == msk.date():
                    continue
                self._momentum_done = msk.date()
                await self._momentum_enter()
            except asyncio.CancelledError:
                return
            except Exception as _sw_e:
                _audit_swallow('_momentum_loop@L1014', _sw_e)  # audit silent-except
                pass

    async def _momentum_enter(self) -> None:
        """Вход: низ-K по моментуму (только падающие), через _submit_order(momentum)."""
        cfg = self.config
        mm = await self.momentum_map()
        if not mm:
            self._log("МОМЕНТУМ-ШОРТ: нет данных моментума")
            return
        k = max(1, int(getattr(cfg, "momentum_k", 3) or 3))
        side_mode = str(getattr(cfg, "momentum_side", "short") or "short").lower()
        ranked = sorted(mm.items(), key=lambda kv: kv[1])
        picks: list[tuple[str, float, str]] = []
        if side_mode in ("short", "both"):
            picks += [(t, ch, "SELL") for t, ch in ranked[:k] if ch < 0]
        if side_mode in ("long", "both"):
            picks += [(t, ch, "BUY") for t, ch in ranked[-k:][::-1] if ch > 0]
        if not picks:
            self._log(f"МОМЕНТУМ ({side_mode}): нет кандидатов")
            return
        _max_pos = int(getattr(cfg, "max_positions", 0) or 0)
        if _max_pos > 0:
            _free = max(0, _max_pos - len(self._held))
            picks = picks[:_free]
            if not picks:
                self._log(f"МОМЕНТУМ ({side_mode}): нет свободных слотов "
                          f"({len(self._held)}/{_max_pos})")
                return
        self._log(f"МОМЕНТУМ ({side_mode}): "
                  + ", ".join(f"{sd} {t} {ch:+.1f}%" for t, ch, sd in picks))
        for t, ch, sd in picks:
            _figi = next((u.get("figi") for u in (self.universe or [])
                          if str(u.get("ticker", "")).upper() == t), None)
            if not _figi:
                continue
            await self._submit_order(_figi, t, "open", sd,
                                     meta={"momentum": True, "priority": True, "mom_chg": ch})

    def _rank_ok(self, ticker: str) -> tuple[bool, str]:
        from app.bot.portfolio import rank_ok as _rk
        cfg = self.config
        if not bool(getattr(cfg, "rank_enabled", True)):
            return True, "выкл"
        return _rk(self._hist_cache or {}, ticker,
                   top_n=int(getattr(cfg, "rank_top_n", 10) or 0),
                   explore=int(getattr(cfg, "rank_explore", 10) or 0),
                   min_hist=int(getattr(cfg, "rank_min_hist", 5) or 0))

    def _min_turnover(self, snap: dict | None) -> float:
        """Порог ликвидности: max(floor, слот × multiple) — растёт вместе с капиталом."""
        from app.bot.portfolio import min_turnover_for_slot as _mt
        cfg = self.config
        floor = float(getattr(cfg, "queue_min_turnover", 300_000) or 0.0)
        mult = float(getattr(cfg, "queue_adv_multiple", 200.0) or 0.0)
        try:
            eq = float((snap or {}).get("equity") or 0.0)
            boost = float(getattr(cfg, "top_boost", 2.0) or 2.0)
            slot = eq * self._pos_pct() * boost if eq > 0 else 0.0
        except Exception as _sw_e:
            _audit_swallow('_min_turnover@L1073', _sw_e)  # audit silent-except
            slot = 0.0
        return _mt(slot, mult, floor)

    def _fit_score(self, snap: dict | None, ticker: str) -> float:
        """Насколько кандидат вписывается в лимиты (0..1): мин. запас net/сектор/маржа/стресс."""
        if not snap:
            return 0.5
        try:
            from app.bot.portfolio import meta_for as _mf
            sec = str(_mf(self._sector_meta, ticker).get("sector") or "other")
            cfg = self.config

            def _room(used: float, lim: float) -> float:
                return max(0.0, 1.0 - float(used) / max(0.01, float(lim)))

            net_room = _room(abs(float(snap.get("net_exposure_pct") or 0.0)),
                             float(getattr(cfg, "max_net_exposure_pct", 0.5) or 0.5))
            sec_room = _room(float((snap.get("sector_pct") or {}).get(sec) or 0.0),
                             float(getattr(cfg, "max_sector_pct", 0.35) or 0.35))
            mar_room = _room(float(snap.get("margin_use_pct") or 0.0),
                             float(getattr(cfg, "max_margin_use_pct", 0.8) or 0.8))
            st = snap.get("stress_pct") or {}
            worst = abs(min(0.0, min(st.values()))) if st else 0.0
            st_room = _room(worst, float(getattr(cfg, "max_stress_loss_pct", 0.1) or 0.1))
            return round(min(net_room, sec_room, mar_room, st_room), 3)
        except Exception as _sw_e:
            _audit_swallow('_room@L1099', _sw_e)  # audit silent-except
            return 0.5

    def _strength_for(self, bb: str, ticker: str, side: str, meta: dict | None = None,
                      snap: dict | None = None) -> dict:
        """Сила кандидата: ret20m vs IMOEX (beta-adj) + объём + breadth."""
        from app.bot.portfolio import candidate_score as _cs, meta_for as _mf
        try:
            ret_tk = 0.0
            vol_ratio = 1.0
            buf = self.buffers.get(bb)
            if buf and len(buf) > 21:
                _b = list(buf)  # deque не поддерживает срезы
                c_now = float(_b[-1].close)
                c_20 = float(_b[-21].close)
                ret_tk = (c_now - c_20) / c_20 if c_20 else 0.0
                vols = [float(getattr(c, "volume", 0) or 0) for c in _b[-51:-1]]
                avg = sum(vols) / len(vols) if vols else 0.0
                v_now = float(getattr(_b[-1], "volume", 0) or 0)
                vol_ratio = (v_now / avg) if avg > 0 else 1.0
            beta = float((_mf(self._sector_meta, ticker).get("beta")) or 1.0)
            reg = self._regime_cache[1] if self._regime_cache else {}
            ret_idx = float(reg.get("pct_20m") or 0.0) / 100.0
            tk = str(ticker or "").upper()
            qe = ((meta or {}).get("quorum_event") or {}) if isinstance(meta, dict) else {}
            return _cs(ret_ticker=ret_tk, ret_index=ret_idx, beta=beta, side=side,
                       hist=(self._hist_cache or {}).get(tk) or {},
                       turnover=float((self._turnover_cache or {}).get(tk) or 0.0),
                       votes=int(qe.get("votes") or 0),
                       total_members=int(qe.get("total_members") or 0),
                       vol_ratio=vol_ratio,
                       regime=str(reg.get("state") or "neutral"),
                       fit=self._fit_score(snap, tk),
                       min_turnover=self._min_turnover(snap),
                       history_veto=bool(getattr(self.config, "queue_history_veto", True)))
        except Exception as _sw_e:
            _audit_swallow('_strength_for@L1134', _sw_e)  # audit silent-except
            return {"score": 50.0, "factors": {}, "veto": []}

    async def _enqueue_candidate(self, figi: str, ticker: str, side: str, why: str,
                                 meta: dict | None = None, snap: dict | None = None) -> None:
        """Кандидат, отклонённый лимитом, встаёт в очередь приоритетного входа."""
        import time as _t
        try:
            cfg = self.config
            ttl = float(getattr(cfg, "queue_ttl_min", 30) or 30) * 60
            now = _t.monotonic()
            for f, c in list(self._cand_queue.items()):
                if now - c.get("ts_mono", 0) > ttl:
                    self._cand_queue.pop(f, None)
            s = self._strength_for(self.tcs_to_bbg.get(figi, figi), ticker, side,
                                   meta=meta, snap=snap)
            self._cand_queue[figi] = {
                "ticker": ticker, "side": side, "why": why,
                "score": s.get("score", 50.0), "rs": s.get("rs"),
                "factors": s.get("factors") or {}, "veto": s.get("veto") or [],
                "hist": s.get("hist"),
                "ts_mono": now,
                "ts": datetime.now(timezone(timedelta(hours=3))).strftime("%H:%M:%S"),
            }
            if len(self._cand_queue) > 20:
                _w = min(self._cand_queue.items(), key=lambda kv: kv[1].get("score", 0))
                self._cand_queue.pop(_w[0], None)
            _f = s.get("factors") or {}
            self._log(f"ОЧЕРЕДЬ {ticker} {side}: score {s.get('score')} "
                      f"(hist {_f.get('hist')} rs {_f.get('rs')} conf {_f.get('conf')} "
                      f"liq {_f.get('liq')} fit {_f.get('fit')})"
                      + (f" ⛔{','.join(s.get('veto') or [])}" if s.get("veto") else "")
                      + f" — {why}")
        except Exception as _sw_e:
            _audit_swallow('_enqueue_candidate@L1167', _sw_e)  # audit silent-except
            pass

    async def _priority_entry_loop(self) -> None:
        """Очередь кандидатов: топ-1 по силе входит с бустом слота, когда лимит позволяет."""
        import time as _t
        while self.running:
            try:
                await asyncio.sleep(max(30.0, float(getattr(self.config, "queue_interval_sec", 120) or 120)))
                cfg = self.config
                if not bool(getattr(cfg, "queue_enabled", True)) or not self._cand_queue:
                    continue
                ttl = float(getattr(cfg, "queue_ttl_min", 30) or 30) * 60
                now = _t.monotonic()
                self._cand_queue = {f: c for f, c in self._cand_queue.items()
                                    if now - c.get("ts_mono", 0) <= ttl and f not in self._held}
                if not self._cand_queue:
                    continue
                f, c = max(self._cand_queue.items(), key=lambda kv: kv[1].get("score", 0))
                self._cand_queue.pop(f, None)
                if c.get("veto"):
                    self._log(f"ОЧЕРЕДЬ {c.get('ticker')}: отклонён (вето {','.join(c['veto'])})")
                    continue
                _mx = int(getattr(cfg, "max_positions", 0) or 0)
                if _mx > 0 and len(self._held) >= _mx:
                    continue
                _boost = float(getattr(cfg, "top_boost", 2.0) or 2.0)
                self._log(f"ПРИОРИТЕТ {c.get('ticker')}: сила {c.get('score')} → вход ×{_boost:g} слот")
                await self._submit_order(f, str(c.get("ticker") or ""), "open",
                                         str(c.get("side") or "BUY"),
                                         meta={"priority": True, "queue_score": c.get("score")})
            except asyncio.CancelledError:
                return
            except Exception as _sw_e:
                _audit_swallow('_priority_entry_loop@L1200', _sw_e)  # audit silent-except
                pass

    async def reduce_positions(self, close_pct: float, reason: str, side: str = "",
                               figis: set[str] | None = None) -> dict:
        """Закрыть долю позиций (худшие по P&L) — трейлинг-стоп портфеля/разворот/ночь."""
        if not self._in_trading_session():
            self._session_gate_log("закрытие позиций")
            return {}
        positions = await self.broker.positions()
        items = []
        for p in positions:
            if figis is not None and p.figi not in figis:
                continue
            if side and str(getattr(p, "side", "")).upper() != side.upper():
                continue
            bb = self.tcs_to_bbg.get(p.figi, p.figi)
            buf = self.buffers.get(bb) or self.buffers.get(p.figi)
            last = float(buf[-1].close) if buf else float(getattr(p, "entry_price", 0) or 0)
            entry = float(getattr(p, "entry_price", 0) or 0)
            qty = float(getattr(p, "qty", 0) or 0)
            long_ = str(getattr(p, "side", "")).upper() in ("LONG", "BUY")
            pnl = (last - entry) * qty * (1 if long_ else -1)
            items.append((pnl, p, bb, last))
        items.sort(key=lambda x: x[0])
        n = max(1, int(round(len(items) * float(close_pct)))) if items else 0
        closed = []
        for pnl, p, bb, last in items[:n]:
            try:
                trade = await self.broker.close_position(p.figi, last, reason)
            except Exception as e:
                self._log(f"ОШИБКА ЗАКРЫТИЯ {getattr(p, 'ticker', '')}: {type(e).__name__}")
                continue
            self._held.discard(p.figi)
            self._held.discard(bb)
            self._clear_exit_state(bb)
            _net = float(trade.net_pnl) if trade else None
            _px = float(trade.price) if (trade and getattr(trade, "price", None)) else last
            try:
                await self._st_close(p.figi, _px, reason=reason, net=_net,
                                     meta={"source": "reduce_positions", "close_pct": close_pct})
            except Exception as _e:
                self._log(f"ST_CLOSE FAIL {getattr(p, 'ticker', '')}: {type(_e).__name__}")
            closed.append({"figi": p.figi, "ticker": getattr(p, "ticker", ""),
                           "pnl": round(pnl, 2), "price": round(_px, 6),
                           "net_pnl": _net})
            self.events.log("POSITION_CLOSED", figi=p.figi, ticker=getattr(p, "ticker", ""),
                            reason=reason, net_pnl=_net)
        return {"closed": len(closed), "positions": closed, "reason": reason}

    async def _overnight_positions(self) -> list[str]:
        """figis открытых позиций, вошедших ДО сегодняшнего дня (пережили ночь)."""
        import time as _t
        if self._overnight_cache is not None and (_t.monotonic() - self._overnight_ts) < 120:
            return list(self._overnight_cache)
        _msk = timezone(timedelta(hours=3))
        today = self._bot_now().astimezone(_msk).date()
        out: list[str] = []
        try:
            from sqlalchemy import text as _text
            async with SessionLocal() as db:
                rows = (await db.execute(_text(
                    "SELECT figi, entry_time FROM sandbox_trades WHERE exit_time IS NULL"
                ))).all()
            for f, et in rows:
                try:
                    if et is not None and et.astimezone(_msk).date() < today:
                        out.append(str(f))
                except Exception as _sw_e:
                    _audit_swallow('_overnight_positions@L1265', _sw_e)  # audit silent-except
                    pass
        except Exception as _sw_e:
            _audit_swallow('_overnight_positions@L1267', _sw_e)  # audit silent-except
            pass
        self._overnight_cache = out
        self._overnight_ts = _t.monotonic()
        return out

    async def _portfolio_guard_loop(self) -> None:
        """Трейлинг-стоп портфеля (просадка от пика) + закрытие на ночь по времени."""
        from app.bot.portfolio import drawdown_action as _dd
        while self.running:
            try:
                await asyncio.sleep(60.0)
                cfg = self.config
                # --- EOD: overnight=False → закрываем позиции по ВРЕМЕНИ, не по свече.
                # (поток свечей может оборваться раньше конца сессии — тогда старое
                #  закрытие на свече не срабатывало и позиции уходили через ночь)
                try:
                    if not bool(getattr(cfg, "overnight", False)) and self._held:
                        from app.engine.sessions import eod_close_due as _eod
                        if _eod(self._bot_now(), cfg.sessions,
                                minutes_before=int(getattr(cfg, "eod_close_min_before", 10) or 10),
                                overnight=False):
                            _eod_figs = set(self._held) - self._swing
                            r = await self.reduce_positions(1.0, "eod_overnight",
                                                            figis=_eod_figs) if _eod_figs else {}
                            if r.get("closed"):
                                self._log(f"🌙 EOD: закрыто {r['closed']} поз. "
                                          f"(overnight=False, {self._bot_now().astimezone(timezone(timedelta(hours=3))).strftime('%H:%M')} МСК)")
                                self.events.log("EOD_CLOSE", closed=r["closed"],
                                                reason="overnight_false")
                except Exception as _e_eod:
                    _audit_swallow('_portfolio_guard_loop@L1297', _e_eod)  # audit silent-except
                    pass
                # --- Уборка ночных: overnight=False, а позиция вошла до сегодня → закрыть.
                try:
                    if not bool(getattr(cfg, "overnight", False)) and self._held:
                        _figs = [f for f in await self._overnight_positions()
                                 if f not in self._swing]
                        if _figs:
                            r = await self.reduce_positions(1.0, "overnight_cleanup", figis=set(_figs))
                            if r.get("closed"):
                                self._overnight_cache = None
                                self._log(f"🌙 НОЧНЫЕ ЗАКРЫТЫ: {r['closed']} поз. "
                                          f"(вошли до сегодня, overnight=False)")
                                self.events.log("OVERNIGHT_CLEANUP", closed=r["closed"])
                except Exception as _sw_e:
                    _audit_swallow('_portfolio_guard_loop@L1311', _sw_e)  # audit silent-except
                    pass
                # Equity для DD-защиты: ликвидный портфель брокера (правда), fallback — equity().
                eq = 0.0
                try:
                    _ma = await self.broker.margin_attributes() or {}
                    eq = float(_ma.get("liquid") or 0.0)
                except Exception as _sw_e:
                    _audit_swallow('_portfolio_guard_loop@L1318', _sw_e)  # audit silent-except
                    eq = 0.0
                if eq <= 0:
                    eq = float(await self.broker.equity() or 0.0)
                if eq <= 0:
                    continue
                # Мусорный пик (чужой счёт / неполный портфель в момент старта): если пик
                # в разы больше текущего ликвидного — сбрасываем, иначе DD-защита зря
                # режет позиции «по просадке 40%».
                if self._equity_peak > 0 and self._equity_peak > eq * 2.0:
                    self._log(f"⚠ пик equity сброшен: {self._equity_peak:.0f} → {eq:.0f}₽ (мусорный отсчёт)")
                    self._equity_peak = eq
                    self._dd_level_done = 0
                if eq > self._equity_peak:
                    self._equity_peak = eq
                    self._dd_level_done = 0
                act = _dd(eq, self._equity_peak,
                          reduce1=float(getattr(cfg, "dd_reduce1_pct", 0.05) or 0.05),
                          reduce2=float(getattr(cfg, "dd_reduce2_pct", 0.10) or 0.10))
                if act["level"] > self._dd_level_done:
                    self._dd_level_done = act["level"]
                    r = await self.reduce_positions(act["close_pct"], f"portfolio_dd_L{act['level']}")
                    self._log(f"🛡 ТРЕЙЛИНГ ПОРТФЕЛЯ: просадка {act['dd']*100:.1f}% от пика "
                              f"{self._equity_peak:.0f}₽ → закрыто {r['closed']} поз.")
                    self.events.log("PORTFOLIO_DD", level=act["level"], dd=act["dd"],
                                    closed=r["closed"], peak=round(self._equity_peak, 2))
            except asyncio.CancelledError:
                return
            except Exception as _sw_e:
                _audit_swallow('_portfolio_guard_loop@L1346', _sw_e)  # audit silent-except
                pass

    def _pos_pct(self) -> float:
        """Доля equity на одну позицию (слот). Настраивается (PATCH /config), по умолчанию 40%."""
        try:
            v = float(getattr(self.config, "pos_pct", POS_PCT) or POS_PCT)
            return min(max(v, 0.05), 1.0)
        except Exception as _sw_e:
            _audit_swallow('_pos_pct@L1354', _sw_e)  # audit silent-except
            return POS_PCT

    def _log(self, msg: str, level: str = "info", source: str = "bot") -> None:
        from app.services.loghub import hub, msk_now_str
        ts = msk_now_str()
        # Реплей/тест: время в логе = ВИРТУАЛЬНОЕ время бота (чтобы видеть дату/время свечей).
        try:
            if str(getattr(self.config, "feed", "")) == "replay":
                ts = self._bot_now().astimezone(timezone(timedelta(hours=3))).strftime(
                    "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        hub.push(msg, level=level, source=source, ts=ts)
        # Персистентная копия (level/source/msg/ts) — пишется в bot_logs флашером,
        # переживает рестарт и видна в Live через фильтры UI.
        # НЕ в реплее (умолчание): тест гонит десятки тысяч баров, запись в БД
        # стоит ~235мс/бар, история логов прогона не нужна (в памяти кольцо).
        # replay_log_persist=True — писать логи теста в bot_logs (прогон виден
        # в истории логов UI, переживает рестарт).
        if (str(getattr(self.config, "feed", "")) != "replay"
                or bool(getattr(self.config, "replay_log_persist", False))):
            self._log_persist_queue.append((level, source, msg, ts))


    _last_session_gate_log: float = 0.0

    def _session_gate_log(self, what: str) -> None:
        """INFO-дедуп для операций, отложенных из-за закрытой торговой сессии.

        Раз/минуту (не на каждый цикл intrabar/reduce), чтобы не спамить лог
        ошибками, которые предсказуемы: sandbox/биржа не принимает заявки
        вне сессии (код 30079), и SDK печатает это как ERROR.
        """
        now = _time.monotonic()
        if now - self._last_session_gate_log < 60.0:
            return
        self._last_session_gate_log = now
        self._log(f"ВНЕ ТОРГОВОЙ СЕССИИ: {what} отложен(а) до открытия биржи")

    def _in_trading_session(self) -> bool:
        """True, если сейчас активна хотя бы одна торговая сессия бота."""
        if not self.config.sessions:
            return True
        return _sessions_allowed(self._bot_now(), self.config.sessions)


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
            now = self._bot_now()
            return now >= c.ts
        except Exception as _sw_e:
            _audit_swallow('_is_closed@L1389', _sw_e)  # audit silent-except
            return True

    def _candle_ok(self, c) -> bool:
        """Инкрементальная валидация свечи из стрима (общая логика ballot_guard).

        Отбрасывает БИТЫЕ бары: некорректные OHLC/volume и единичный прыжок
        цены >40% от последнего ВАЛИДНОГО close. prev_close обновляется только
        валидными барами, поэтому при мерцании (32->85->32) битые бары 85
        отбрасываются, а нормальные 32 принимаются — позиция управляется по
        валидным ценам. Счётчик прыжков по дню логируется (для статистики),
        но день целиком НЕ отбрасывается в live (нужно управлять позицией).
        """
        from app.services.candle_guard import bar_ok, jump_ratio
        try:
            o, h, l, cl = float(c.open), float(c.high), float(c.low), float(c.close)
        except Exception as _sw_e:
            _audit_swallow('_candle_ok@L1405', _sw_e)  # audit silent-except
            return False
        cfigi = getattr(c, "figi", "")
        tcs_map = getattr(self, "tcs_to_bbg", {}) or {}
        figi = tcs_map.get(cfigi, cfigi)
        if not bar_ok(o, h, l, cl, volume=getattr(c, "volume", None)):
            return False
        # MSK date tracking (для статистики)
        try:
            _d = c.ts.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
        except Exception as _sw_e:
            _audit_swallow('_candle_ok@L1415', _sw_e)  # audit silent-except
            _d = str(getattr(c, "ts", ""))[:10]
        dj = self._day_jumps.setdefault(figi, {})
        pv = self._prev_close.get(figi)
        if pv is not None and pv > 0:
            jump = jump_ratio(pv, cl)
            if jump is not None and jump > 0.50:
                dj[_d] = dj.get(_d, 0) + 1
                if dj[_d] in (3, 10, 30):
                    self.events.log("DATA_BAD_DAY", figi=figi,
                                    reason=f"flicker {_d} jumps={dj[_d]} prev={pv} close={cl}")
            if jump is not None and jump > 0.40:
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
        ec = await load_ensemble_config(str(getattr(self.config, "mode", "") or ""))
        _setups = [
            {"strategy_id": s["strategy_id"], "tf": s.get("tf", "5min"), "params": s.get("params", {})}
            for s in ec.get("setups", []) if s.get("enabled")
        ]
        _bias = ec.get("bias") or {}
        _src = str(ec.get("sl_source", "manual") or "manual")
        _sl_key = "sl_override" if _src == "optuna" else "sl_mult"
        _rr_key = "rr_override" if _src == "optuna" else "rr"
        return EnsembleParams(
        figi=figi, lot=int(lot) if lot else 10, capital=capital,
        quorum=int(ec.get("quorum", 2)), session="all", sessions=sessions,
        drop_useless=bool(ec.get("drop_useless", False)),
        setups=_setups,
        sl_mult=(float(ec.get(_sl_key) or 0.0) or float(getattr(self.config, "ensemble_sl_mult", 0.0) or 0.0) or float(opt.get("sl_mult", 2.0))),
        rr=(float(ec.get(_rr_key) or 0.0) or float(getattr(self.config, "ensemble_rr", 0.0) or 0.0) or float(opt.get("rr", 4.0))),
            vol_thr=float(ec.get("vol_thr", 0.0) or 0.0),
            neutral_mode=str(ec.get("neutral_mode", "semi_flip")),
            entry_tf=str(getattr(self.config, "ensemble_entry_tf", "5min") or "5min"),
            entry_from_setups=bool(getattr(self.config, "ensemble_entry_from_setups", True)),
            entry_direction_sid=str(getattr(self.config, "ensemble_direction_sid", "") or ""),
            entry_confirm_closes=int(getattr(self.config, "entry_confirm_closes", 0) or 0),
            entry_confirm_closes_sides=list(getattr(self.config, "entry_confirm_closes_sides", None) or ["BUY"]),
            entry_macd_1m=bool(ec.get("entry_macd_1m", False)),
            bias_tf=str(_bias.get("tf", "hour")),
            bias_period=int(_bias.get("period", 50)),
            bias_mode=str(ec.get("bias_mode") or "veto"),
            bias_by_state=dict(ec.get("bias_by_state") or {}),
            ticker=str(ticker or ""),
            regime_setups_filter=ec.get("regime_setups_filter") or {},
            trade_regimes=list(getattr(self.config, "trade_regimes", []) or []),
            ml_filter=ec.get("ml_filter") or {},
            rsi_filter=ec.get("rsi_filter") or {},
            regime_entry_policy=ec.get("regime_entry_policy") or {},
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
                    except Exception as _sw_e:
                        _audit_swallow('reload_ensemble@L1490', _sw_e)  # audit silent-except
                        continue
            self._log(f"⚙ КОНФИГ КВОРУМА применён: пересобрано стратегий {n}")
        except Exception as e:
            self._log(f"⚙ КОНФИГ КВОРУМА: ошибка применения: {e}")
        return n

    async def _daily_turnover(self, ticker: str) -> float:
        """Дневной оборот тикера (VALTODAY из TQBR quotes, кэш 5 мин).

        В universe.avg_daily_turnover лежит не дневной оборот (мелкие значения),
        поэтому ликвидность считаем по живым котировкам биржи.
        """
        import time as _t
        tk = str(ticker or "").upper()
        now = _t.monotonic()
        if self._turnover_cache and self._turnover_ts and (now - self._turnover_ts) < 300.0:
            return float(self._turnover_cache.get(tk) or 0.0)
        # Кэш пуст/устарел — обновляем дневной оборот с MOEX ISS (VALTODAY).
        try:
            from app.api.routes.screener import fetch_tqbr_market
            q = await asyncio.to_thread(fetch_tqbr_market)
            for _tk, row in (q or {}).items():
                val = float((row or {}).get("turnover") or 0.0)
                if val > 0:
                    self._turnover_cache[str(_tk).upper()] = val
            self._turnover_ts = now
        except Exception:
            pass
        return float(self._turnover_cache.get(tk) or 0.0)

    def _log_no_trade(self, figi: str, reason: str, detail: str = "") -> None:
        """Log why no trade was made for diagnostics (NO_TRADE analysis)."""
        self._no_trade_stats[reason] = self._no_trade_stats.get(reason, 0) + 1
        ticker = self.tickers.get(figi, figi[-6:])
        self.events.log("NO_TRADE", figi=figi, ticker=ticker, reason=reason, detail=detail)

    def get_no_trade_stats(self) -> dict[str, int]:
        """Return aggregated NO_TRADE reasons for diagnostics."""
        return dict(self._no_trade_stats)

    def get_funnel(self, figi: str = "", ticker: str = "", limit: int = 400) -> dict:
        """Воронка решений по аккумуляторам стратегий (raw→quorum→gate→entry).

        Счётчики собираются в EnsembleV4Strategy.on_bar на каждом 5м-баре:
          raw_setup_signals  — «сырые» сигналы setup-стратегий (до кворума)
          quorum_candidates  — принятые merge_quorum (BUY/SELL кворум прошедшие)
          gate_pass          — входы, прошедшие ensemble-гейты (accepted_decisions)
          executed_entries   — фактически открытые брокером (заполняется при филах)
        """
        import time as _t
        _out = {
            "raw_setup_signals": 0, "quorum_candidates": 0,
            "gate_pass": 0, "executed_entries": 0,
            "by_reason": {}, "votes": {},
            "period_start": None, "period_end": None,
            "per_ticker": {}, "ring": [], "total": 0, "ring_size": 0,
        }
        try:
            _items = []
            for _f, _st in list((self.strategies or {}).items()):
                try:
                    _snap = _st.funnel_snapshot() if hasattr(_st, "funnel_snapshot") else {}
                except Exception:
                    _snap = {}
                if not _snap:
                    continue
                _tk = str(getattr(_st, "p", object).ticker if getattr(_st, "p", None) else "") or _f[-6:]
                if figi and _f != figi:
                    continue
                _low = str(ticker or "").upper()
                if _low and _tk.upper() != _low:
                    continue
                _out["raw_setup_signals"] += int(_snap.get("raw_setup_signals", 0) or 0)
                _out["quorum_candidates"] += int(_snap.get("quorum_candidates", 0) or 0)
                _out["gate_pass"] += int(_snap.get("gate_pass", 0) or 0)
                _out["executed_entries"] += int(_snap.get("executed_entries", 0) or 0)
                for _r, _n in (_snap.get("by_reason") or {}).items():
                    _out["by_reason"][str(_r)] = _out["by_reason"].get(str(_r), 0) + int(_n)
                for _v, _n in (_snap.get("votes") or {}).items():
                    _out["votes"][str(_v)] = _out["votes"].get(str(_v), 0) + int(_n)
                if not _out["period_start"] or (_snap.get("period_start") and _snap["period_start"] < _out["period_start"]):
                    _out["period_start"] = _snap.get("period_start")
                if not _out["period_end"] or (_snap.get("period_end") and _snap["period_end"] > _out["period_end"]):
                    _out["period_end"] = _snap.get("period_end")
                _out["per_ticker"][_tk] = {
                    "raw": int(_snap.get("raw_setup_signals", 0) or 0),
                    "quorum": int(_snap.get("quorum_candidates", 0) or 0),
                    "gate_pass": int(_snap.get("gate_pass", 0) or 0),
                    "executed": int(_snap.get("executed_entries", 0) or 0),
                }
        except Exception:
            pass
        _out["total"] = _out["raw_setup_signals"]
        return _out

    def _reject_entry(self, stage: str, key: str, detail: str, figi: str, ticker: str) -> None:
        """Единое логирование отказа гейта: лог (ГЕЙТ, красный в UI) + событие + skip_counts."""
        self._log(f"ГЕЙТ {ticker}: [{stage}] {key} — {detail}", level="warn", source="gate")
        self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                        reason=str(key).upper(), detail=str(detail)[:160])
        self._log_no_trade(figi, key)

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
                    except Exception as _sw_e:
                        _audit_swallow('get_exit_stats@L1532', _sw_e)  # audit silent-except
                        pass
                if stats["total"] > 0:
                    stats["avg_bars_held"] = round(stats["bars_held_sum"] / stats["total"], 1)
        except Exception as _sw_e:
            _audit_swallow('get_exit_stats@L1536', _sw_e)  # audit silent-except
            pass
        return stats

    async def _st_open(self, figi, ticker, side, qty, price, sl, tp, meta: dict | None = None, leverage: float = 1.0) -> None:
        try:
            if str((meta or {}).get("hold") or "").lower() == "swing":
                self._swing.add(str(figi))
                self._log(f"SWING {ticker}: долгая сделка — EOD/ночь не закрывает")
        except Exception as _sw_e:
            _audit_swallow('_st_open@L1545', _sw_e)  # audit silent-except
            pass
        from app.models.sandbox_trade import SandboxTrade
        import json as _json
        _lot = 1
        try:
            from app.models.instrument import Instrument
            async with SessionLocal() as db2:
                _lot = int((await db2.execute(select(Instrument.lot).where(Instrument.figi == figi))).scalar_one_or_none() or 1)
        except Exception as _sw_e:
            _audit_swallow('_st_open@L1554', _sw_e)  # audit silent-except
            pass
        qty_shares = int(qty) * _lot
        _er = (self._regimes.get(figi) or {}).get("state")
        # _regimes[figi]["state"] может быть dict (timeline-запись от стратегии):
        # всегда нормализуем до строки — в _entry_regime и в meta сделки, иначе
        # в таблицу позиций уходит сырой dict (режим = "{'state': 'TREND_UP', ...}").
        _er_norm = (_er.get("state") if isinstance(_er, dict) else _er) or "NO_REGIME"
        self._entry_regime[figi] = _er_norm
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
                                      "entry_price0": float(price),
                                      "entry_regime": _er_norm,
                                      "entry_time": _now.isoformat()}, ensure_ascii=False, default=str),
                    leverage=float(leverage),
                    mode=self.broker_mode,
                    test_name=getattr(self.config, "test_name", "") or None,
                ))
                await db.commit()
                # Funnel: фактический вход (executed_entries) — инкремент стратегии figi
                try:
                    _st_f = self.strategies.get(figi)
                    if _st_f is not None and hasattr(_st_f, "_funnel_stats"):
                        _st_f._funnel_stats["executed_entries"] = int(_st_f._funnel_stats.get("executed_entries", 0) or 0) + 1
                except Exception:
                    pass
        except Exception as _sw_e:
            _audit_swallow('_st_open@L1588', _sw_e)  # audit silent-except
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
                    # Инфо-трейлинг: где бы сработал трейл (виртуальный след, SL/TP не трогали).
                    _ti = self._trail_info.pop(figi, None)
                    if _ti:
                        # Пишем ВСЕГДА (30.09): карточка должна отвечать «где трейл»
                        # даже когда он не активировался или активировался, но не сработал.
                        _hit = bool(_ti.get("hit_ts"))
                        _hit_pnl = None
                        try:
                            _hp = float(_ti.get("hit_price") or 0.0)
                            if _hit and _hp > 0 and row.entry_price:
                                _dir = 1.0 if str(row.side).upper() in ("LONG", "BUY") else -1.0
                                _hit_pnl = round((_hp - float(row.entry_price)) * int(row.qty) * _dir, 2)
                        except Exception:
                            _hit_pnl = None
                        meta["trail_info"] = {
                            "activated": bool(_ti.get("active")),
                            "trail_stop": float(_ti.get("act_track") or 0.0),
                            "trail_dist_atr": _ti.get("trail_dist_atr"),
                            "hit_ts": str(_ti.get("hit_ts") or ""),
                            "hit_time": str(_ti.get("hit_ts") or ""),  # имя, которое читает карточка
                            "hit_price": float(_ti.get("hit_price") or 0.0) if _hit else None,
                            "hit_reason": str(_ti.get("hit_reason") or "") if _hit else "",
                            "hit_pnl": _hit_pnl,
                        }
                    # Пик PnL позиции (трекается в _step_exit на каждом баре).
                    # Пик P&L сделки: лучший ход (макс. цена лонг / мин. шорт), ATR, MAE.
                    # .get, НЕ pop: этот же пик читает открытая позиция (_test_position_row).
                    _peak = (meta.pop("peak", None) or self._peak_pnl.get(figi) or {})
                    if _peak:
                        meta["max_pnl"] = float(_peak.get("pnl") or 0.0)
                        meta["max_pnl_time"] = str(_peak.get("ts") or "")
                        meta["max_pnl_price"] = float(_peak.get("price") or 0.0)
                        if _peak.get("atr_abs") is not None:
                            meta["max_pnl_atr"] = float(_peak.get("atr_abs"))
                        if _peak.get("atr_pct") is not None:
                            meta["max_pnl_atr_pct"] = float(_peak.get("atr_pct"))
                        # MAE (fix 29.09): макс. уход ПРОТИВ сделки за всю её жизнь.
                        # Раньше max_pnl_mae_atr считался как ₽-PnL (с qty), делённый
                        # на ATR за штуку — отсюда дикие «99.8 ATR». Теперь:
                        #   mae_atr   — расстояние в ATR (per-share, единицы риска SL)
                        #   mae_pct   — % от цены входа
                        #   mae_price / mae_time — где и когда уходило хуже всего.
                        try:
                            _entry_mae = float(meta.get("entry_price") or row.entry_price or 0.0)
                            # Adverse до самого выхода: уход против, случившийся на
                            # открытии бара выхода, тоже часть MAE (29.09).
                            _exit_fill = float(exit_price or 0.0)
                            _adv_exit = 0.0
                            if _entry_mae > 0 and _exit_fill > 0:
                                _is_long = str(row.side).upper() in ("LONG", "BUY")
                                _adv_exit = (_entry_mae - _exit_fill) if _is_long else (_exit_fill - _entry_mae)
                                _adv_exit = max(0.0, _adv_exit)
                            _peak_dist = float(_peak.get("mae_dist") or 0.0)
                            _mae_dist = max(_peak_dist, _adv_exit)
                            _atr_c = None
                            try:
                                _atr_c = self.atr_now(figi)
                            except Exception:
                                _atr_c = None
                            if _mae_dist > 0 and _atr_c:
                                _mae_atr = _mae_dist / float(_atr_c)
                            else:
                                _mae_atr = float(_peak.get("mae_cur") or 0.0)
                            meta["mae_atr"] = round(_mae_atr, 2)
                            if _mae_dist > 0 and _entry_mae > 0:
                                meta["mae_pct"] = round(_mae_dist / _entry_mae * 100.0, 2)
                            if _adv_exit > _peak_dist:
                                meta["mae_price"] = _exit_fill
                                if row.exit_time is not None:
                                    meta["mae_time"] = str(row.exit_time.isoformat())
                            else:
                                if _peak.get("mae_px") is not None:
                                    meta["mae_price"] = float(_peak.get("mae_px"))
                                if _peak.get("mae_ts"):
                                    meta["mae_time"] = str(_peak.get("mae_ts"))
                            # legacy-ключ карточки — теперь в корректных единицах
                            meta["max_pnl_mae_atr"] = meta["mae_atr"]
                        except Exception as _sw_e:
                            _audit_swallow('_st_close@peak_mae', _sw_e)  # audit silent-except
                            pass
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
                    except Exception as _sw_e:
                        _audit_swallow('_st_close@L1628', _sw_e)  # audit silent-except
                        pass
        except Exception as _sw_e:
            _audit_swallow('_st_close@L1630', _sw_e)  # audit silent-except
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
        except Exception as _sw_e:
            _audit_swallow('_st_update_sl@L1653', _sw_e)  # audit silent-except
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
        except Exception as _sw_e:
            _audit_swallow('set_position_levels@L1666', _sw_e)  # audit silent-except
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
        # --- Защита от «прижимания» уровней (AI любит подтягивать слишком рано) ---
        _px = 0.0
        _atr = 0.0
        try:
            _b = list(self.buffers.get(figi) or [])
            _px = float(_b[-1].close) if _b else 0.0
            _atr = float(self.atr_now(figi) or 0.0)
        except Exception as _sw_e:
            _audit_swallow('set_position_levels@L1684', _sw_e)  # audit silent-except
            pass
        _is_long = str(getattr(pos, "side", "")).upper() in ("BUY", "LONG")
        _entry = float(getattr(pos, "entry_price", 0) or 0)
        if _px > 0 and _atr > 0:
            _min_sl = 1.0 * _atr   # ближе 1 ATR к цене — запрещено (шум выбивает)
            _min_tp = 0.5 * _atr
            _max_step = 3.0 * _atr
            if sl is not None:
                _sl = float(sl)
                if _is_long and _sl >= _px:
                    return {"ok": False, "error": f"SL {_sl:.4g} выше цены {_px:.4g} — мгновенный стоп (лонг)"}
                if (not _is_long) and _sl <= _px:
                    return {"ok": False, "error": f"SL {_sl:.4g} ниже цены {_px:.4g} — мгновенный стоп (шорт)"}
                if abs(_px - _sl) < _min_sl:
                    return {"ok": False, "error": f"SL слишком близко к цене (<1 ATR = {_min_sl:.4g}) — шум выбьет"}
                # SL в зону прибыли — только когда прибыль реально есть (>= 1 ATR)
                if _entry > 0:
                    _in_profit = (_sl > _entry) if _is_long else (_sl < _entry)
                    _best = (_px - _entry) if _is_long else (_entry - _px)
                    if _in_profit and _best < 1.0 * _atr:
                        return {"ok": False,
                                "error": f"SL в плюс только при прибыли ≥1 ATR "
                                         f"(сейчас {_best / _atr:.2f} ATR) — дай позиции дышать"}
                _cur = self._trail_stop.get(figi)
                if _cur and abs(_sl - float(_cur)) > _max_step:
                    return {"ok": False, "error": f"шаг SL {abs(_sl - float(_cur)):.4g} > 3 ATR ({_max_step:.4g})"}
                sl = _sl
            if tp is not None:
                _tp = float(tp)
                if _is_long and _tp <= _px:
                    return {"ok": False, "error": f"TP {_tp:.4g} ниже цены {_px:.4g} (лонг)"}
                if (not _is_long) and _tp >= _px:
                    return {"ok": False, "error": f"TP {_tp:.4g} выше цены {_px:.4g} (шорт)"}
                if abs(_tp - _px) < _min_tp:
                    return {"ok": False, "error": f"TP слишком близко к цене (<0.2 ATR = {_min_tp:.4g})"}
                _curtp = self._exit_target.get(figi)
                if _curtp:
                    if _is_long and _tp > float(_curtp) + 1e-9:
                        return {"ok": False, "error": "TP отодвигается дальше от цены — не разрешено"}
                    if (not _is_long) and _tp < float(_curtp) - 1e-9:
                        return {"ok": False, "error": "TP отодвигается дальше от цены — не разрешено"}
                    if abs(_tp - float(_curtp)) > _max_step:
                        return {"ok": False, "error": f"шаг TP {abs(_tp - float(_curtp)):.4g} > 3 ATR"}
                tp = _tp
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
            "broker_mode": self.broker_mode, "contour": self.active_contour,
            "equity": None, "positions": [], "alerts": [],
            "imoex_guard": self._imoex_guard_snapshot(),
            "pending_approvals": self.list_approvals(),
        }
        try:
            out["equity"] = float(await self.broker.equity())
        except Exception as _sw_e:
            _audit_swallow('state_snapshot@L1772', _sw_e)  # audit silent-except
            pass
        try:
            pos_list = await self.broker.positions()
        except Exception as _sw_e:
            _audit_swallow('state_snapshot@L1776', _sw_e)  # audit silent-except
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
            _atr = self.atr_now(bb)
            _dsa = (abs(last - sl) / _atr) if (last and sl and _atr) else None
            _dta = (abs(tp - last) / _atr) if (last and tp and _atr) else None
            # regime сделки = зафиксированный НА ВХОД (self._entry_regime[bb]),
            # НЕ текущее состояние детектора (иначе "не сразу и не всегда").
            reg_name = self._entry_regime.get(bb) or (self._regimes.get(bb) or {}).get("state")
            out["positions"].append({
                "figi": bb, "ticker": self.tickers.get(bb, getattr(p, "ticker", "") or bb[-6:]),
                "side": "LONG" if long_ else "SHORT", "qty": qty,
                "entry": entry, "last": last, "pnl": pnl,
                "sl": sl, "tp": tp,
                "dist_sl_pct": d_sl, "dist_tp_pct": d_tp,
                "atr": _atr, "dist_sl_atr": _dsa, "dist_tp_atr": _dta,
                "regime": self._entry_regime.get(bb), "trail_active": bool(self._trail_active.get(bb)),
                "hold": "swing" if bb in self._swing else "intraday",
                "notional": (abs(qty) * last) if last else None,
            })
        for pos in out["positions"]:
            for key, lbl in (("dist_sl_pct", "SL"), ("dist_tp_pct", "TP")):
                d = pos.get(key)
                if d is not None and d <= 0.7:
                    out["alerts"].append(f"{pos['ticker']}: {d:.2f}% до {lbl} (P&L {pos['pnl']:+.1f}₽)")
            _dsa = pos.get("dist_sl_atr")
            if _dsa is not None and _dsa <= 1.5:
                out["alerts"].append(f"{pos['ticker']}: {_dsa:.2f} ATR до SL (близко)")
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
                        # Вне сессии биржа/sandbox не принимает заявки (30079) —
                        # не дёргаем брокера, выход дожидается открытия сессии.
                        if not self._in_trading_session():
                            self._session_gate_log("intrabar-выход")
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
                    except Exception as _sw_e:
                        _audit_swallow('_load_imoex_buf@L1903', _sw_e)  # audit silent-except
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
        except Exception as _sw_e:
            _audit_swallow('_imoex_guard_snapshot@L1993', _sw_e)  # audit silent-except
            age_sec = None
        _trading = False
        try:
            from app.bot.imoex_guard import imoex_session as _isess
            _trading = _isess(now)  # IMOEX живёт только 09:50–19:00 МСК
        except Exception as _sw_e:
            _audit_swallow('_imoex_guard_snapshot@L1999', _sw_e)  # audit silent-except
            pass
        _stale_sec = float(getattr(cfg, "imoex_stale_sec", 300.0) or 300.0)
        _stale = bool(_trading and (age_sec is None or age_sec > _stale_sec))
        # Направление индекса: изменение за 5м и 20м (по буферу 1м свечей).
        _p5 = _p20 = None
        if self._imoex_buf:
            _last_v = self._imoex_buf[-1][1]
            for _mins in (5, 20):
                _target = now - timedelta(minutes=_mins)
                _ref = next((v for t, v in reversed(self._imoex_buf) if t <= _target), None)
                if _ref:
                    _v = (_last_v - _ref) / _ref * 100
                    if _mins == 5:
                        _p5 = _v
                    else:
                        _p20 = _v
        _dp = _p20 if _p20 is not None else 0.0
        _dir = "up" if _dp > 0.05 else ("down" if _dp < -0.05 else "flat")
        return {
            "dir": _dir,
            "dir_pct_5m": round(_p5, 3) if _p5 is not None else None,
            "dir_pct_20m": round(_p20, 3) if _p20 is not None else None,
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

    def _replay_progress(self) -> dict | None:
        """Прогресс реплея/теста для прогресс-бара UI (bot-replay-bar).

        Возвращает None вне теста/реплея. wall_start = старт прогона
        (для замера времени теста). Безопасен: любая ошибка -> None,
        статус никогда не ломается.
        """
        try:
            mode = str(self.mode or "")
            is_test = mode == "test" or mode.startswith("test:")
            is_replay = str(getattr(self.config, "feed", "")) == "replay"
            if not (is_test or is_replay) or not self.running:
                return None

            def _parse(v):
                if not v:
                    return None
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    d = _dt.fromisoformat(str(v).replace("Z", "+00:00"))
                    return d if d.tzinfo else d.replace(tzinfo=_tz.utc)
                except Exception:
                    return None

            start = _parse(getattr(self.config, "replay_start", ""))
            end = _parse(getattr(self.config, "replay_end", ""))
            now = self.last_candle_ts
            pct = 0.0
            if start is not None and end is not None and end > start and now is not None:
                total = (end - start).total_seconds()
                if total > 0:
                    pct = max(0.0, min(100.0, (now - start).total_seconds() / total * 100))
            return {
                "active": True,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "now": now.isoformat() if now else None,
                "now_msk": None,
                "pct": round(pct, 1),
                "pace": str(getattr(self.config, "replay_pace", "") or ""),
                "wall_start": self.started_at.isoformat() if self.started_at else None,
            }
        except Exception:
            return None

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
            "broker_mode": self.broker_mode,
            "contour": self.active_contour,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "replay": self._replay_progress(),
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
                "trail_activation_comm_mult": self.config.trail_activation_comm_mult,
                "trail_info_activation_comm_mult": self.config.trail_info_activation_comm_mult,
                "commission_rate": self.config.commission_rate,
                "slippage_bps": self.config.slippage_bps,
                "confirm_flip": self.config.confirm_flip,
                "reentry_cooldown_bars": self.config.reentry_cooldown_bars,
                "overnight": self.config.overnight,
                "feed": self.config.feed,
                "replay_start": self.config.replay_start,
                "replay_end": self.config.replay_end,
                "replay_pace": self.config.replay_pace,
                "replay_log_persist": bool(getattr(self.config, "replay_log_persist", False)),
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
            "replay": ({
                "active": True,
                "start": self._replay_from.isoformat(),
                "end": self._replay_to.isoformat(),
                "now": self._replay_cur.isoformat(),
                "now_msk": self._replay_cur.astimezone(timezone(timedelta(hours=3))).isoformat(),
                "pct": round(self._replay_progress_pct(), 2) if self._replay_progress_pct() is not None else None,
                "pace": str(getattr(self.config, "replay_pace", "") or ""),
            } if (self._replay_from is not None and self._replay_cur is not None)
              else {
                "active": False,
                "start": None, "end": None, "now": None, "now_msk": None,
                "pct": None, "pace": str(getattr(self.config, "replay_pace", "") or ""),
            }),
            "risk": {
                "state": risk.state,
                "daily_pnl": round(risk.daily_pnl, 2),
                "daily_loss_limit": risk.daily_loss_limit,
                "entries_paused": risk.entries_paused,
            },
            "carousel": self.carousel_diag,
            "imoex_guard": self._imoex_guard_snapshot(),
            "loss_streak": self.loss_streak_snapshot(),
            "long_short": self.long_short_snapshot(),
            "ai_approval": {
                "enabled": bool(getattr(self.config, "ai_approval", False)),
                "pending": len(self.list_approvals()),
                "timeout_sec": float(getattr(self.config, "ai_approval_timeout_sec", 45.0) or 45.0),
                "default": str(getattr(self.config, "ai_approval_default", "approve") or "approve"),
            },
            "ai_decisions": self.list_ai_decisions(8),
            "ai_notes": self.list_ai_notes(5),
            "metrics": dict(self.metrics),
        }

    def risk_snapshot(self) -> RiskSnapshot:
        return RiskSnapshot(
            daily_pnl=self.daily_pnl_cached(),
            daily_loss_limit=self.config.daily_loss_limit,
            entries_paused=self.entries_paused,
        )

    def _replay_progress_pct(self) -> float | None:
        """Прогресс реплея в % (0-100). None вне реплея или если нет конца окна."""
        if self._replay_from is None or self._replay_cur is None or self._replay_to is None:
            return None
        _span = (self._replay_to - self._replay_from).total_seconds()
        if _span <= 0:
            return None
        _done = (self._replay_cur - self._replay_from).total_seconds()
        return max(0.0, min(100.0, _done / _span * 100.0))

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
        self._turnover_cache: dict[str, float] = {}
        self._turnover_ts = 0.0
        return self._daily_pnl_cache[1]

    async def refresh_daily_pnl(self) -> float:
        msk_now = self._bot_now().astimezone(timezone(timedelta(hours=3)))
        day_start = msk_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        _mode = str(getattr(self, "broker_mode", "") or "")
        async with SessionLocal() as db:
            if _mode in ("sandbox", "live"):
                # Дневной P&L — только по активному контуру, иначе после переключения
                # sandbox↔live в лимит дня попадали бы сделки чужого счёта.
                from app.models.sandbox_trade import SandboxTrade as _ST
                res = await db.execute(
                    select(_ST.net_pnl).where(_ST.exit_time >= day_start, _ST.mode == _mode)
                )
            else:
                res = await db.execute(
                    select(PaperTrade.net_pnl).where(PaperTrade.exit_time >= day_start)
                )
            total = sum(float(v) for v in res.scalars() if v is not None)
        self._daily_pnl_cache = (datetime.now(timezone.utc), total)
        return total

    async def _restore_swing(self) -> None:
        """Восстановить список swing-позиций из открытых сделок (meta.hold) активного контура."""
        try:
            import json as _json
            _cfg_mode = str(getattr(self.config, "mode", "") or "")
            _mode = _cfg_mode if _cfg_mode in ("sandbox", "live") else "paper"
            from sqlalchemy import text as _text
            async with SessionLocal() as db:
                rows = (await db.execute(_text(
                    "SELECT figi, meta FROM sandbox_trades WHERE exit_time IS NULL AND mode = :m"),
                    {"m": _mode})).all()
            for f, m in rows:
                try:
                    if str((_json.loads(m or "{}") or {}).get("hold") or "").lower() == "swing":
                        self._swing.add(str(f))
                except Exception as _sw_e:
                    _audit_swallow('_restore_swing@L2248', _sw_e)  # audit silent-except
                    pass
        except Exception as _sw_e:
            _audit_swallow('_restore_swing@L2250', _sw_e)  # audit silent-except
            pass

    async def start(self, cfg: BotConfig) -> dict:
        if self.running or self.starting:
            raise RuntimeError("bot already running")
        self.config = cfg
        self._pf_cache = None  # контур мог смениться — не отдаём портфель прошлого счёта
        await self._restore_swing()
        # Восстанавливаем сохранённые настройки (переживают перезапуск/старт без фронта).
        try:
            _saved = await load_bot_settings()
            if _saved:
                for _f in BOT_PERSIST_FIELDS:
                    if _f in _saved:
                        try:
                            setattr(cfg, _f, _saved[_f])
                        except Exception as _sw_e:
                            _audit_swallow('start@L2267', _sw_e)  # audit silent-except
                            pass
        except Exception as _sw_e:
            _audit_swallow('start@L2269', _sw_e)  # audit silent-except
            pass
        # Конфиг гейтов (data/gates_config.json) — источник правды по порогам гейтов
        # (entry_min_turnover, entry_ob_*, entry_volatility_max_mult, ai_* и т.д.).
        try:
            from app.bot.gates import apply_gates_config as _agc
            _gapplied = _agc(cfg)
            if _gapplied:
                self._log(f"ГЕЙТЫ: конфиг применён ({len(_gapplied)} полей из gates_config.json)")
        except Exception:
            pass
        # Режим AI применяем ПОСЛЕ сохранёнок: селектор (Бот+++/AI) — источник правды
        # для ai_approval/momentum_only, иначе сохранённый флаг гейта глушил воркеров
        # после переключения контура (sandbox↔live) и AI «только рассуждал».
        try:
            self.apply_ai_mode()
        except Exception as _sw_e:
            _audit_swallow('start@L2276', _sw_e)  # audit silent-except
            pass
        # Восстанавливаем персистентные runtime-флаги (entries_paused переживает рестарт).
        try:
            _flags = await load_bot_flags()
            self.entries_paused = bool(_flags.get("entries_paused", False))
            if self.entries_paused:
                self._log("ВОССТАНОВЛЕНО: пауза новых входов (entries_paused=true) сохранена между рестартами")
        except Exception as _sw_e:
            _audit_swallow('start@L2284', _sw_e)  # audit silent-except
            pass
        if cfg.use_ensemble:
            cfg.interval_name = "1min"
        if str(cfg.mode) == "test":
            # Тест-оверрайды — ПОСЛЕДНИМИ по приоритету (после сохранёнок и
            # gates_config): TEST_ENGINE/TEST_INTERVAL/TEST_PARAMS/TEST_GATES
            # (см. apply_test_overrides). Вызов был потерян — переключение
            # движка теста (напр. ose_all) молча не работало: env ставился,
            # но никто его не читал.
            try:
                _ov = apply_test_overrides(cfg)
                if _ov:
                    self._log("ТЕСТ-ОВЕРРАЙДЫ: " + ", ".join(_ov[:10]))
            except Exception as _sw_e:
                _audit_swallow('start@test_overrides', _sw_e)
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
                try:
                    self._replay_to = datetime.fromisoformat(
                        cfg.replay_end.replace("Z", "+00:00")
                    )
                except Exception:
                    self._replay_to = None
                if self._replay_to is not None and self._replay_to.tzinfo is None:
                    self._replay_to = self._replay_to.replace(tzinfo=timezone.utc)
                self._log(f"REPLAY START: окно с {self._replay_from.isoformat()} "
                          f"до {self._replay_to.isoformat() if self._replay_to else 'нет-конца'} pacing={cfg.replay_pace}")
        self._log(
            f"Параметры бота: сессии: {'/'.join(cfg.sessions) or '—'} · "
            f"маржа: {'/'.join(cfg.margin_sessions) or '—'} "
            f"(плечо {'Max' if float(cfg.margin_leverage or 0) <= 0 else '×'+format(float(cfg.margin_leverage),'g')}) · "
            f"направления: {'Long, Short' if cfg.long_allowed and cfg.short_allowed else ('Long' if cfg.long_allowed else ('Short' if cfg.short_allowed else '—'))}"
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
                _rows = (await _db.execute(
                    _text(
                        "SELECT id, to_char(ts AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD HH24:MI:SS.MS') AS ts_s, "
                        "COALESCE(level,'info') AS lvl, COALESCE(source,'bot') AS src, msg "
                        "FROM (SELECT id, ts, level, source, msg FROM bot_logs ORDER BY id DESC LIMIT 400) t ORDER BY id"
                    )
                )).all()
                from app.services.loghub import hub, strip_legacy_ts
                for _rid, _ts_s, _lvl, _src, _msg in _rows:
                    hub.push(strip_legacy_ts(_msg or ""), level=_lvl, source=_src, ts=_ts_s)
                if _rows:
                    hub.set_seq(int(_rows[-1][0]))
                self._log(f"ВОССТАНОВЛЕНО {len(_rows)} строк логов из истории")
            except Exception as _sw_e:
                _audit_swallow('start@L2337', _sw_e)  # audit silent-except
                pass
        except Exception as _sw_e:
            _audit_swallow('start@L2339', _sw_e)  # audit silent-except
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
            # CostModel из конфига: комиссия/проскальзывание теста = настройки бота
            from app.engine.costs import CostModel as _CostModel
            self.broker = PaperBroker(SessionLocal, cost_model=_CostModel(
                commission_rate=cfg.commission_rate, slippage_bps=cfg.slippage_bps))
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
                    cfg.ensemble_capital = real_cash * self._pos_pct()
                    self._log(f"КАПИТАЛ со счёта (equity): {real_cash:.0f} ₽ · позиция до {cfg.ensemble_capital:.0f} ({self._pos_pct()*100:.0f}%)")
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
                # Окно прогона в bot_test_runs (upsert): /bot/tests видит тест сразу,
                # даже до первой сделки; replay_start/end — истинные границы окна.
                if cfg.test_name:
                    from sqlalchemy import text as _btr_text
                    _btr_end = None
                    if cfg.replay_end:
                        try:
                            _btr_end = datetime.fromisoformat(cfg.replay_end.replace("Z", "+00:00"))
                            if _btr_end.tzinfo is None:
                                _btr_end = _btr_end.replace(tzinfo=timezone.utc)
                        except Exception as _sw_e:
                            _audit_swallow('start@test_window', _sw_e)
                            _btr_end = None
                    try:
                        async with SessionLocal() as db:
                            await db.execute(_btr_text(
                                "CREATE TABLE IF NOT EXISTS bot_test_runs ("
                                "name VARCHAR(64) PRIMARY KEY, replay_start TIMESTAMPTZ, "
                                "replay_end TIMESTAMPTZ, updated_at TIMESTAMPTZ DEFAULT now())"))
                            await db.execute(_btr_text(
                                "INSERT INTO bot_test_runs (name, replay_start, replay_end, updated_at) "
                                "VALUES (:n, :s, :e, now()) "
                                "ON CONFLICT (name) DO UPDATE SET "
                                "replay_start = EXCLUDED.replay_start, "
                                "replay_end = EXCLUDED.replay_end, updated_at = now()"
                            ), {"n": cfg.test_name, "s": self._replay_from, "e": _btr_end})
                            await db.commit()
                    except Exception as _sw_e:
                        _audit_swallow('start@bot_test_runs', _sw_e)
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
                    self._log(f"Вселенная: {len(self.universe)} бумаг · волатильность ATR% {min(_atrs):.3f}–{max(_atrs):.3f} · "
                              f"{', '.join(u.get('ticker', '?') for u in self.universe)}")
            except Exception as _sw_e:
                _audit_swallow('_startup@L2458', _sw_e)  # audit silent-except
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
                                                date_from=self._bot_now() - timedelta(days=10),
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
                    _gs = self._imoex_guard_snapshot()
                    self._log(f"IMOEX-щит: загружен ряд из {_n_buf} свечей · состояние: {_gs.get('dir', '—')} · "
                              f"активен {_gs.get('active', '—')}, блокировок {_gs.get('blocks', 0)}, "
                              f"срабатываний {_gs.get('activations', 0)}")
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
            except Exception as _sw_e:
                _audit_swallow('_load_bounded@L2633', _sw_e)  # audit silent-except
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
            _audit_swallow('_load_bounded@L2643', e)  # audit silent-except
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
            except Exception as _sw_e:
                _audit_swallow('stop@L2658', _sw_e)  # audit silent-except
                pass
            self.stream_manager = None
        # Закрыть gRPC-канал брокера: non-daemon потоки Client держат процесс
        # после завершения uvicorn (shutdown «висит» до kill -9).
        try:
            _cl = getattr(self.broker, "close", None)
            if callable(_cl):
                _cl()
        except Exception as _sw_e:
            _audit_swallow('stop@L2667', _sw_e)  # audit silent-except
            pass
        for t in (self.startup_task, self.task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        # Хвостовые циклы (persist/imoex/queue/…): если _run уже завершился сам,
        # они остаются висеть и блокируют graceful shutdown uvicorn — гасим явно.
        for _attr in ('_persist_task', '_held_sync_task', '_hot_add_task', '_vol_carousel_task',
                  '_reconcile_task',
                      '_session_task', '_metrics_task', '_intrabar_task', '_imoex_task',
                      '_queue_task', '_momentum_task', '_guard_task'):
            _t = getattr(self, _attr, None)
            if _t and not _t.done():
                _t.cancel()
                try:
                    await _t
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

    def long_short_snapshot(self) -> dict:
        """Соотношение LONG/SHORT по открытым позициям (для UI и AI-контекста)."""
        longs = shorts = 0
        for _f in list(self._held):
            _s = str(self._exit_side.get(_f, "")).upper()
            if _s == "LONG":
                longs += 1
            elif _s == "SHORT":
                shorts += 1
        total = longs + shorts
        return {"longs": longs, "shorts": shorts, "total": total,
                "short_share": (round(shorts / total, 3) if total else 0.0)}

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

    async def add_ai_decision(self, payload: dict) -> dict:
        """Записать решение AI-гейта (в т.ч. shadow) — в память (UI) и БД (статистика)."""
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
            "provider": str(payload.get("provider") or ""),
            "agreement": payload.get("agreement"),
            "applied": bool(payload.get("applied", False)),
            "latency_ms": payload.get("latency_ms"),
            "shadow": bool(payload.get("shadow", False)),
            "usage": payload.get("usage") or {},
        }
        self._ai_decisions.append(rec)
        self.events.log("AI_DECISION", figi=payload.get("figi"), ticker=rec["ticker"],
                        decision=rec["decision"], shadow=rec["shadow"], reason=rec["reason"][:120])
        # Персист в БД — для статистики (трекер исходов достраивает контрфакт).
        try:
            from app.models.ai_decision import AiDecision
            from datetime import datetime as _dt, timezone as _tz
            async with SessionLocal() as db:
                db.add(AiDecision(
                    ts=_dt.now(_tz.utc), order_id=rec["order_id"], figi=str(payload.get("figi") or ""),
                    ticker=rec["ticker"], side=rec["side"], qty=int(rec["qty"] or 0),
                    price=float(payload.get("price") or 0.0), provider=rec["provider"],
                    model=rec["model"], decision=rec["decision"], reason=rec["reason"],
                    advice=rec["advice"], confidence=float(rec["confidence"] or 0.0),
                    latency_ms=int(rec["latency_ms"] or 0), agreement=bool(rec["agreement"]),
                    applied=bool(rec["applied"]), shadow=bool(rec["shadow"]),
                ))
                await db.commit()
        except Exception as _e:
            self._log(f"AI_DECISION persist: {type(_e).__name__}: {str(_e)[:80]}")
        return {"ok": True, "record": rec}

    def list_ai_decisions(self, limit: int = 20) -> list[dict]:
        return list(self._ai_decisions)[-max(1, min(int(limit), 50)):]

    def atr_now(self, figi: str) -> float | None:
        """Текущий ATR на ТФ ВХОДА (5м, период из конфига) — паритет с бэктестом.

        ВАЖНО: раньше считался по 1м → стопы выходили в ~2.2 раза уже бэктеста
        (там ATR на 5м баров) → позиции выбивало шумом и комиссиями.
        """
        try:
            from app.engine.indicators import atr as _atr
            buf = self.buffers.get(figi)
            if not buf:
                return None
            _p = int(getattr(self.config, "atr_period", 14) or 14)
            _bars = self._get_5m_bars(figi, list(buf)) or list(buf)
            vals = _atr(list(_bars), _p)
            v = vals[-1] if vals else None
            return float(v) if v else None
        except Exception as _sw_e:
            _audit_swallow('atr_now@L2851', _sw_e)  # audit silent-except
            return None

    def add_ai_note(self, payload: dict) -> dict:
        """Заметка вахтёра позиций (llama/AI) — словами, без управления."""
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": str(payload.get("ticker") or ""),
            "side": str(payload.get("side") or ""),
            "action": str(payload.get("action") or ""),      # hold | tighten | close | watch
            "note": str(payload.get("note") or payload.get("reason") or "")[:4000],
            "advice": str(payload.get("advice") or "")[:1200],
            "model": str(payload.get("model") or ""),
            "provider": str(payload.get("provider") or ""),
            "latency_ms": payload.get("latency_ms"),
            "dist_sl_atr": payload.get("dist_sl_atr"),
            "dist_tp_atr": payload.get("dist_tp_atr"),
            "pnl": payload.get("pnl"),
        }
        self._ai_notes.append(rec)
        self.events.log("AI_POSITION_NOTE", ticker=rec["ticker"], action=rec["action"],
                        note=rec["note"][:120])
        return {"ok": True, "record": rec}

    def list_ai_notes(self, limit: int = 20) -> list[dict]:
        return list(self._ai_notes)[-max(1, min(int(limit), 50)):]

    def set_ai_prompt(self, payload: dict) -> dict:
        """Сохранить текущий промпт/конфиг AI-гейта (воркер присылает при старте)."""
        _kind = str((payload or {}).get("kind") or "gate")
        try:
            self._ai_defaults[_kind] = {
                "system": str((payload or {}).get("system") or ""),
                "model": str((payload or {}).get("model") or ""),
                "provider": str((payload or {}).get("provider") or ""),
                "updated_ts": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as _sw_e:
            _audit_swallow('set_ai_prompt@L2888', _sw_e)  # audit silent-except
            pass
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

    def get_ai_control(self) -> dict:
        """Текущие переопределения промптов + срочное сообщение + режим (для UI и воркеров)."""
        out = dict(self._ai_control or {})
        out.setdefault("prompts", {})
        out.setdefault("note", "")
        _m = str(self._ai_control.get("mode") or "")
        out["mode"] = _m
        out["spec"] = dict(AI_MODES.get(_m) or {})
        out["modes"] = {k: v.get("label") for k, v in AI_MODES.items()}
        out["defaults"] = self._ai_defaults or {}
        # Активный контур (sandbox|live|paper|test) — воркеры обязаны работать по нему.
        out["contour"] = self.active_contour
        out["broker_mode"] = self.broker_mode
        out["running"] = bool(self.running)
        return out

    @property
    def active_contour(self) -> str:
        """Активный контур бота: sandbox|live|paper|test.

        Воркеры (гейт/вахтёр/трейдер) используют его, чтобы брать данные и
        ставить заявки именно того счёта, который выбран в UI.
        """
        _bm = str(getattr(self, "broker_mode", "") or "")
        if _bm in ("sandbox", "live"):
            return _bm
        _cfg = str(getattr(self.config, "mode", "") or "")
        if _cfg in ("sandbox", "live", "test", "paper"):
            return _cfg
        return _bm or "paper"

    def ai_mode_spec(self) -> dict:
        """Спека текущего режима AI (пусто = режим не задан, работаем по конфигу)."""
        return dict(AI_MODES.get(str(self._ai_control.get("mode") or "")) or {})

    def apply_ai_mode(self) -> None:
        """Применить режим AI к конфигу (гейт = ai_approval; 'ai' = двигатель выкл)."""
        spec = self.ai_mode_spec()
        if not spec:
            return
        try:
            self.config.ai_approval = bool(spec.get("gate"))
            self.config.momentum_only = not bool(spec.get("engine"))
        except Exception as _sw_e:
            _audit_swallow('apply_ai_mode@L2946', _sw_e)  # audit silent-except
            pass
        try:
            import asyncio as _aio
            # Ссылку держим в self: «голая» create_task без ссылки может быть
            # собрана GC до закрытия asyncpg-соединения ->
            # «non-checked-in connection ... will be terminated» в логах.
            _t = _aio.get_running_loop().create_task(save_bot_settings(self.config))
            self._ai_save_task = _t
            _t.add_done_callback(lambda _ft: setattr(self, "_ai_save_task", None))
        except Exception as _sw_e:
            _audit_swallow('apply_ai_mode@L2951', _sw_e)  # audit silent-except
            pass
        try:
            _m = str(self._ai_control.get("mode") or "")
            self._log(f"Режим бота: {_m or '—'} · торговля двигателем={'вкл' if spec.get('engine') else 'выкл'} · "
                      f"гейт={'вкл' if spec.get('gate') else 'выкл'} · вахтёр={'вкл' if spec.get('watch') else 'выкл'} · "
                      f"трейдер={'вкл' if spec.get('trader') else 'выкл'}")
        except Exception as _sw_e:
            _audit_swallow('apply_ai_mode@L2959', _sw_e)  # audit silent-except
            pass

    def set_ai_control(self, payload: dict) -> dict:
        """Сохранить промпт(ы) и/или срочное сообщение из UI (файл data/ai_control.json)."""
        prompts = payload.get("prompts")
        if isinstance(prompts, dict):
            cur = dict(self._ai_control.get("prompts") or {})
            for k in ("gate", "watch", "trader"):
                if k in prompts:
                    cur[k] = str(prompts.get(k) or "")
            self._ai_control["prompts"] = cur
        if "note" in payload:
            self._ai_control["note"] = str(payload.get("note") or "")
        _mode = str(payload.get("mode") or "").lower()
        if _mode in AI_MODES:
            self._ai_control["mode"] = _mode
        elif "mode" in payload:
            # Выбор «— не задан —» (пустая строка) — сброс: убираем override, бот работает по конфигу.
            self._ai_control.pop("mode", None)
        self._ai_control["updated_ts"] = datetime.now(timezone.utc).isoformat()
        try:
            _p = Path(_AI_CONTROL_FILE)
            _p.parent.mkdir(parents=True, exist_ok=True)
            _tmp = _p.with_suffix(".json.tmp")
            _tmp.write_text(json.dumps(self._ai_control, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            _tmp.replace(_p)
        except Exception as _sw_e:
            _audit_swallow('set_ai_control@L2984', _sw_e)  # audit silent-except
            pass
        try:
            self.apply_ai_mode()
        except Exception as _sw_e:
            _audit_swallow('set_ai_control@L2988', _sw_e)  # audit silent-except
            pass
        return {"ok": True, "updated_ts": self._ai_control["updated_ts"]}

    def set_ai_report(self, payload: dict) -> dict:
        """Сохранить отчёт AI о рынке (ai_trader присылает каждый цикл)."""
        self._ai_report = {
            "updated_ts": datetime.now(timezone.utc).isoformat(),
            "model": str(payload.get("model") or ""),
            "analysis": str(payload.get("analysis") or ""),
            "suggestions": payload.get("suggestions") or [],
            "actions": payload.get("actions") or [],
            "now_msk": str(payload.get("now_msk") or ""),
            "usage": payload.get("usage") or {},
        }
        return {"ok": True, "updated_ts": self._ai_report["updated_ts"]}

    def get_ai_report(self) -> dict:
        return self._ai_report or {}

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
                self._ai_reject_reason[figi] = reason or "отклонение ИИ"
                _cd = float(getattr(self.config, "ai_reject_cooldown_min", 15.0) or 0.0)
                if _cd > 0:
                    self._ai_reject_until[figi] = self._bot_now() + timedelta(minutes=_cd)
                    self._ai_reject_last_log.pop(figi, None)
                    self._ai_reject_cnt.pop(figi, None)
                self._log(f"AI-ГЕЙТ: вход {o.ticker} {o.side} ОТКЛОНЁН — {reason or 'без причины'}"
                          + (f" (пауза входов {_cd:.0f} мин)" if _cd > 0 else ""))
                self.events.log("AI_APPROVAL_REJECTED", figi=figi, ticker=o.ticker,
                                order_id=o.id, reason=reason)
                return {"ok": True, "order_id": order_id, "status": "REJECTED",
                        "cooldown_min": _cd}
        return {"ok": False, "error": "order not found or not pending approval"}

    async def close_all(self) -> dict:
        if not self._in_trading_session():
            self._session_gate_log("закрытие всех позиций (kill-switch)")
            return {"closed": 0, "positions": []}
        positions = await self.broker.positions()
        closed = []
        for p in positions:
            buf = self.buffers.get(p.figi)
            price = float(buf[-1].close) if buf else float(p.entry_price)
            trade = await self.broker.close_position(p.figi, price, "kill_switch_close_all")
            self._held.discard(p.figi)
            self._clear_exit_state(p.figi)
            self._entry_bar_index.pop(p.figi, None)
            try:
                await self._st_close(p.figi,
                                     float(trade.price) if (trade and getattr(trade, "price", None)) else price,
                                     reason="kill_switch_close_all",
                                     net=float(trade.net_pnl) if trade else None)
            except Exception as _sw_e:
                _audit_swallow('close_all@L3054', _sw_e)  # audit silent-except
                pass
            closed.append({"figi": p.figi, "ticker": p.ticker,
                           "price": round(price, 6),
                           "net_pnl": float(trade.net_pnl) if trade else None})
            self.events.log("POSITION_CLOSED", figi=p.figi, ticker=p.ticker,
                            reason="kill_switch_close_all", net_pnl=closed[-1]["net_pnl"])
        self.events.log("CLOSE_ALL", reason=f"closed={len(closed)}")
        return {"closed": len(closed), "positions": closed}

    async def deactivate_figi(self, figi: str) -> dict:
        """Убрать тикер из работающей карусели без рестарта (метка в БД уже снята).

        Закрывает открытую позицию, отписывает стрим, вычищает стратегию/буфер
        и все локальные state. Возвращает результат деактивации (для лога UI).
        """
        ticker = self.tickers.get(figi, figi[-6:])
        closed = []
        # 1) Закрываем открытую позицию (если есть) — как close_all, но для одной figi.
        if self.broker is not None:
            try:
                pos = await self.broker.get_position(figi)
                if pos is not None:
                    buf = self.buffers.get(figi)
                    price = float(buf[-1].close) if buf else float(pos.entry_price)
                    try:
                        trade = await self.broker.close_position(figi, price, "carousel_remove")
                    except Exception as _cl_e:
                        # Вне торговой сессии sandbox не принимает заявки (30079) — тикер
                        # со снятой меткой останется в карусели до закрытия позиции, каждый
                        # следующий цикл hot-add повторит попытку.
                        self._log(f"⚠ deactivate {ticker}: позиция не закрылась — "
                                  f"{type(_cl_e).__name__}: {str(_cl_e)[:120]}. Тикер остаётся до закрытия.")
                        return {"ok": False, "ticker": ticker, "figi": figi,
                                "closed": 0, "pending_position": True}
                    self._held.discard(figi)
                    self._clear_exit_state(figi)
                    self._entry_bar_index.pop(figi, None)
                    try:
                        await self._st_close(figi,
                                             float(trade.price) if (trade and getattr(trade, "price", None)) else price,
                                             reason="carousel_remove",
                                             net=float(trade.net_pnl) if trade else None)
                    except Exception as _sw_e:
                        _audit_swallow('deactivate_figi@st_close', _sw_e)
                    closed.append({"figi": figi, "ticker": ticker, "price": round(price, 6),
                                   "net_pnl": float(trade.net_pnl) if trade else None})
                    self.events.log("POSITION_CLOSED", figi=figi, ticker=ticker,
                                    reason="carousel_remove", net_pnl=closed[-1]["net_pnl"])
            except Exception as _pos_e:
                self._log(f"⚠ deactivate {ticker}: позиция — {type(_pos_e).__name__}: {str(_pos_e)[:120]}")

        # 2) Отписываем стрим данных (stream-unsubscribe / polling берёт из self.figis).
        try:
            if self.feed is not None and figi in self.feed.figis:
                self.feed.remove_figis([figi])
                self._log(f"➖ FEED отписка {ticker} ({figi[-6:]})")
        except Exception as _fb_e:
            self._log(f"⚠ deactivate {ticker}: feed — {type(_fb_e).__name__}: {str(_fb_e)[:120]}")
        try:
            if figi in self.stream_universe:
                self.stream_universe.remove(figi)
        except Exception:
            pass

        # 3) Вычищаем все локальные state тикера.
        self.strategies.pop(figi, None)
        self.buffers.pop(figi, None)
        self.tickers.pop(figi, None)
        self.universe = [u for u in self.universe if u.get("figi") != figi]
        self._clear_exit_state(figi)
        self._entry_bar_index.pop(figi, None)
        self._opposite_count.pop(figi, None)
        self.pending_orders.pop(figi, None)
        self._cand_queue.pop(figi, None)
        self.events.log("CAROUSEL_REMOVE", figi=figi, ticker=ticker,
                        reason=f"closed={len(closed)}", universe_now=len(self.universe))
        self._log(f"➖ КАРУСЕЛЬ: {ticker} убран (позиций закрыто: {len(closed)}), "
                  f"universe={len(self.universe)}")
        return {"ok": True, "ticker": ticker, "figi": figi, "closed": len(closed)}

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
            except Exception as _sw_e:
                _audit_swallow('_sync_held@L3087', _sw_e)  # audit silent-except
                pass

    async def _hot_add_universe(self) -> None:
        """Фоновая задача: каждые 60с проверяет eligible тикеры с данными, добавляет в universe."""
        from sqlalchemy import text as _text
        from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy
        from app.engine.models import Candle as EC

        while self.running:
            try:
                # Хоровод универса крутим независимо от торговых сессий:
                # добавление/удаление тикеров должно работать как часы (ночью/в выходные
                # свечи всё равно пишутся в БД потоком, а подписку подтянем к сессии).
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
                                            date_from=datetime.now(timezone.utc) - timedelta(days=10))
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
                        except Exception as _sw_e:
                            _audit_swallow('_hot_add_universe@L3177', _sw_e)  # audit silent-except
                            pass
                        self.universe.append({
                            "figi": figi, "ticker": ticker,
                            "lot": int(lot) if lot else 10, "name": ticker,
                            "atr_pct": _atr_pct, "avg_price": 0, "avg_turnover": 0, "sector": "",
                        })
                        self.tickers[figi] = ticker
                        # Подписываем feed (stream-unsubscribe карусельный): новый тикер
                        # получает живые свечи теми же батовыми механизмами, что и остальные.
                        try:
                            if self.feed is not None and figi not in self.feed.figis:
                                self.feed.add_figis([figi])
                        except Exception as _fa_e:
                            self._log(f"⚠ HOT-ADD feed.subscribe {ticker}: {type(_fa_e).__name__}: {str(_fa_e)[:100]}")
                        if figi not in self.stream_universe:
                            self.stream_universe.append(figi)
                        hot_adds_this_cycle += 1
                        self._log(f"➕ HOT-ADD: {ticker} ({figi[-6:]}) {cnt} bars")

                    # --- Удаление: тикеры в universe, которых больше нет в eligible (метка снята) ---
                    eligible_set = {r[0] for r in all_eligible}
                    for _cur in list(self.universe):
                        _f = _cur.get("figi")
                        if _f in eligible_set:
                            continue
                        try:
                            self._log(f"➖ КАРУСЕЛЬ (цикл): {_cur.get('ticker', _f[-6:])} — метка снята, убираю")
                            await self.deactivate_figi(_f)
                        except Exception as _de_e:
                            self._log(f"⚠ карусель-remove {_f[-6:]}: {type(_de_e).__name__}: {str(_de_e)[:120]}")

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

    async def _vol_carousel_loop(self) -> None:
        """Волатильная карусель: периодически ранжирует TQBR по RNG% и обновляет
        eligible-метки (реализация — app/services/vol_carousel.py).

        Чтобы ничего не сломать «втемную», работает всегда, но:
          - enabled=False (по умолчанию) → DRY-RUN: только диагностика/лог,
            БД не трогает;
          - enabled=True → реально меняет eligible_tier, а дальше обычный
            hot-add/remove цикл подхватывает без рестарта.
        Управление только через settings (VOL_CAROUSEL_ENABLED и т.д.).
        """
        from app.config import get_settings
        from app.services.vol_carousel import run_vol_carousel_once
        from app.database import SessionLocal as _SL

        _cycle = get_settings().vol_carousel_cycle_sec
        while self.running:
            try:
                _settings = get_settings()
                async with _SL() as db:
                    _report = await run_vol_carousel_once(db, _settings, self,
                                                          emit=self._log)
                # Кладём свежий отчёт в carousel_diag для UI/лог-диагностики.
                self.carousel_diag["vol_carousel"] = {
                    "mode": _report.get("mode", "dry-run"),
                    "ts": _report.get("ts"),
                    "universe_now": _report.get("universe_now"),
                    "target": _report.get("target_count"),
                    "would_add": len(_report.get("would_add", [])),
                    "would_drop": len(_report.get("would_drop", [])),
                    "top": [e["ticker"] for e in _report.get("now", [])],
                }
            except asyncio.CancelledError:
                raise
            except Exception as _e:
                self._log(f"⚠ VOL-CAROUSEL: ошибка — {type(_e).__name__}: {_e}")
                try:
                    self.carousel_diag["vol_carousel"] = {"error": str(_e)}
                except Exception:
                    pass
            await asyncio.sleep(_cycle)

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
            except Exception as _sw_e:
                _audit_swallow('_session_monitor@L3239', _sw_e)  # audit silent-except
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
                except Exception as _sw_e:
                    _audit_swallow('_metrics_loop@L3294', _sw_e)  # audit silent-except
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
                    self._log("⚠ Мониторинг: " + " · ".join(alerts))
                    for a in alerts[-3:]:
                        self.events.log("METRICS_ALERT", reason=a)
                else:
                    self._log(
                        f"TECHINFO метрики cps={cps:.2f} bar={bar_avg:.0f}ms "
                        f"ens={ens_avg:.0f}ms persist={ps_avg:.0f}ms "
                        f"q={len(self._persist_queue)}/{len(self._persist_queue_5m)} "
                        f"сигналов={self.signals_seen}",
                        level="debug",
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
                # Сверка только по явному включению и в торговое время (МСК, будни,
                # сессии конфига) — вне сессии позиции и так заморожены, опрос только шумит.
                if not getattr(self.config, "reconcile_enabled", True):
                    continue
                if not _sessions_allowed(self._bot_now(), self.config.sessions):
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
        except Exception as _sw_e:
            _audit_swallow('_reconcile_positions@L3359', _sw_e)  # audit silent-except
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
                    # Двойная проверка: снапшот positions() мог быть неполным (рестарт,
                    # сбой API) — если позиция у брокера есть, НЕ помечаем orphan'ом.
                    try:
                        _real = await self.broker.get_position(figi)
                    except Exception as _sw_e:
                        _audit_swallow('_reconcile_positions@L3434', _sw_e)  # audit silent-except
                        _real = None
                    if _real is not None:
                        self._log(f"RECONCILE: orphan {figi[-6:]} — у брокера позиция есть, не трогаю")
                        continue
                    # Реальная цена/P&L: позиция могла быть закрыта вручную/стопом
                    # у брокера — тогда net=0 недопустим. Берём из операций брокера,
                    # fallback — последняя известная цена.
                    _exit_px = None
                    _net = None
                    _entry_px_broker = None
                    try:
                        import asyncio as _aio
                        from app.api.routes.sandbox import _get_operations
                        _ops = await _aio.to_thread(_get_operations, 3)
                        _lng0 = str(r.side).upper() in ("BUY", "LONG")
                        _close_kind = "Покупка" if not _lng0 else "Продажа"   # закрывающая сторона
                        _open_kind = "Продажа" if not _lng0 else "Покупка"    # сторона входа
                        _et = r.entry_time
                        if _et is not None and getattr(_et, "tzinfo", None) is None:
                            _et = _et.replace(tzinfo=timezone.utc)
                        _best_close = None   # (t, price) — ближайшая закрывающая после входа
                        _best_open = None
                        for _op in (getattr(_ops, "operations", None) or []):
                            try:
                                if str(getattr(_op, "figi", "")) != figi:
                                    continue
                                _t = getattr(_op, "date", None)
                                if _t is not None and getattr(_t, "tzinfo", None) is None:
                                    _t = _t.replace(tzinfo=timezone.utc)
                                if _et is not None and _t is not None and _t < _et - timedelta(minutes=3):
                                    continue
                                _otype = str(getattr(_op, "type", "") or "")
                                if "комисси" in _otype.lower():
                                    continue
                                _pm = getattr(_op, "price", None)
                                _p = 0.0
                                if _pm is not None:
                                    _p = float(getattr(_pm, "units", 0) or 0) + \
                                         float(getattr(_pm, "nano", 0) or 0) / 1e9
                                if _p <= 0:
                                    continue
                                if _close_kind in _otype:
                                    if _best_close is None or (_t is not None and _t < _best_close[0]):
                                        _best_close = (_t or _et or _t, _p)
                                elif _open_kind in _otype:
                                    if _best_open is None or (_t is not None and _t > _best_open[0]):
                                        _best_open = (_t or _et or _t, _p)
                            except Exception as _sw_e:
                                _audit_swallow('_reconcile_positions@L3482', _sw_e)  # audit silent-except
                                continue
                        if _best_close is not None:
                            _exit_px = round(_best_close[1], 6)
                        if _best_open is not None:
                            _entry_px_broker = round(_best_open[1], 6)
                    except Exception as _sw_e:
                        _audit_swallow('_reconcile_positions@L3488', _sw_e)  # audit silent-except
                        pass
                    # P&L считаем по ценам: (выход − вход) × qty × направление − комиссии.
                    _px_out = float(_exit_px or 0.0)
                    if _px_out <= 0:
                        try:
                            _buf = self.buffers.get(figi)
                            _px_out = float(_buf[-1].close) if _buf else 0.0
                        except Exception as _sw_e:
                            _audit_swallow('_reconcile_positions@L3496', _sw_e)  # audit silent-except
                            _px_out = 0.0
                    if _px_out > 0:
                        _q = abs(float(r.qty or 0))
                        _lng = str(r.side).upper() in ("BUY", "LONG")
                        # Цена входа: если брокер дал операцию входа — берём её (точнее стрима)
                        _epx = float(_entry_px_broker or r.entry_price or 0.0)
                        _pnl = (_px_out - _epx) * _q * (1 if _lng else -1)
                        _cr = float(getattr(self.config, "commission_rate", 0.0005) or 0.0005)
                        _net = round(_pnl - _cr * (_epx + _px_out) * _q, 4)
                        _exit_px = _px_out
                    r.exit_time = now
                    r.exit_price = float(_exit_px or r.entry_price or 0.0)
                    r.exit_reason = "closed_at_broker"
                    r.net_pnl = _net
                    if _px_out > 0:
                        r.commission = round(_cr * (_epx + _px_out) * _q, 4)
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
                    self._log(f"Сверка с брокером: были расхождения → создано {created}, закрыто {closed} (брокер: {len(broker_pos)} поз., локально: {len(local_by_figi)})")
                else:
                    self._log(f"Сверка с брокером: ✓ сходится (позиций: брокер {len(broker_pos)}, локально {len(local_by_figi)})")

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
            if (not self._persist_queue and not self._persist_queue_5m
                    and not self._log_persist_queue):
                continue
            batch = _drain(self._persist_queue)
            batch5 = _drain(self._persist_queue_5m)
            # Логи дреним ВСЕГДА (раньше: если свечей нет — continue, и логи не писались;
            # при ошибке вставки свечей — тоже). Теперь пишем отдельной транзакцией.
            log_rows = _drain(self._log_persist_queue)
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
                        f"q={len(batch)} q5={len(batch5)}",
                        level="debug",
                    )
                if self._persist_flushes % 10 == 0:
                    self._log(
                        f"TECHINFO persist ok q={len(batch)} q5={len(batch5)} "
                        f"flushes={self._persist_flushes}",
                        level="debug",
                    )
            except Exception as e:
                self._log(f"PERSIST_ERR {type(e).__name__}: {str(e)[:80]}")
                # Вернуть данные обратно в очередь, чтобы не потерять
                self._persist_queue.extend(batch)
                self._persist_queue_5m.extend(batch5)
            # Логи — своей транзакцией: ошибка свечей их больше не блокирует.
            if log_rows:
                try:
                    async with SessionLocal() as db:
                        _payload = [{"level": l, "source": s, "msg": m, "ts": t + "+03:00"}
                                    for (l, s, m, t) in log_rows]
                        await db.execute(
                            _text(
                                "INSERT INTO bot_logs (level, source, ts, msg) "
                                "SELECT * FROM jsonb_to_recordset(:rows) AS t(level text, source text, ts timestamptz, msg text)"
                            ),
                            {"rows": json.dumps(_payload, ensure_ascii=False)},
                        )
                        await db.commit()
                except Exception as e:
                    self._log(f"PERSIST_LOG_ERR {type(e).__name__}: {str(e)[:80]}")

    async def _finalize_replay(self) -> None:
        """Финал реплея: открытые позиции закрываются по последним ценам свечей
        (replay_end_close), прогон фиксируется в bot_test_runs. Раньше тест
        «зависал» с открытыми сделками и без итогов в /bot/tests."""
        try:
            _pos_list = list(await self.broker.positions())
        except Exception as _sw_e:
            _audit_swallow('_finalize_replay@positions', _sw_e)
            _pos_list = []
        _prices: dict = {}
        try:
            _lp = getattr(self.broker, "last_prices", None)
            if _lp is not None and _pos_list:
                _prices = await _lp([p.figi for p in _pos_list]) or {}
        except Exception as _sw_e:
            _audit_swallow('_finalize_replay@prices', _sw_e)
            _prices = {}
        _closed = 0
        for _p in _pos_list:
            try:
                _px = float(_prices.get(_p.figi) or float(_p.entry_price or 0.0))
                _trade = await self.broker.close_position(_p.figi, _px, "replay_end_close")
                if _trade is None:
                    continue
                _closed += 1
                await self._st_close(_p.figi, float(_trade.exit_price), reason="replay_end_close",
                                     net=float(_trade.net_pnl))
                self._log(f"ВЫХОД {_p.figi[-6:]} (replay_end) pnl={float(_trade.net_pnl):+.2f}")
            except Exception as _sw_e:
                _audit_swallow('_finalize_replay@close', _sw_e)
        self._held.clear()
        self._exit_plans.clear()
        if getattr(self.config, "test_name", ""):
            try:
                from sqlalchemy import text as _btr_text2
                async with SessionLocal() as db:
                    await db.execute(_btr_text2(
                        "UPDATE bot_test_runs SET updated_at = now() WHERE name = :n"
                    ), {"n": self.config.test_name})
                    await db.commit()
            except Exception as _sw_e:
                _audit_swallow('_finalize_replay@test_run', _sw_e)
        self._log(f"REPLAY ЗАВЕРШЁН: закрыто {_closed} поз. по последним ценам (итоги в /bot/tests)")

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
                except Exception as _sw_e:
                    _audit_swallow('_run@L3641', _sw_e)  # audit silent-except
                    self._replay_from = None
                if self._replay_from is not None and self._replay_from.tzinfo is None:
                    self._replay_from = self._replay_from.replace(tzinfo=timezone.utc)
            _r_end: datetime | None = None
            if self.config.replay_end:
                try:
                    _r_end = datetime.fromisoformat(self.config.replay_end.replace("Z", "+00:00"))
                    if _r_end.tzinfo is None:
                        _r_end = _r_end.replace(tzinfo=timezone.utc)
                except Exception as _sw_e:
                    _audit_swallow('_run@L3651', _sw_e)  # audit silent-except
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
        feed.on_log = lambda msg: self._log(msg, level="debug")
        self.feed = feed
        exited = "stream_exhausted"
        self._persist_task = asyncio.create_task(self._flush_persist())
        self._held_sync_task = asyncio.create_task(self._sync_held())
        self._hot_add_task = asyncio.create_task(self._hot_add_universe())
        self._vol_carousel_task = asyncio.create_task(self._vol_carousel_loop())
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        self._session_task = asyncio.create_task(self._session_monitor())
        self._metrics_task = asyncio.create_task(self._metrics_loop())
        self._intrabar_task = asyncio.create_task(self._intrabar_exit_loop())
        self._imoex_task = asyncio.create_task(self._imoex_loop())
        self._queue_task = asyncio.create_task(self._priority_entry_loop())
        self._momentum_task = asyncio.create_task(self._momentum_loop())
        self._guard_task = asyncio.create_task(self._portfolio_guard_loop())
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
                    _pct = self._replay_progress_pct()
                    if _pct is not None and (_pct - self._last_replay_log) >= 5.0:
                        self._last_replay_log = (_pct // 5.0) * 5.0
                        self._log(
                            f"REPLAY PROGRESS: {self._replay_cur.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m %H:%M')} МСК · "
                            f"{_pct:.0f}% ({self._replay_from.strftime('%d.%m %H:%M')} → {self._replay_to.strftime('%d.%m %H:%M')})"
                        )
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
            _audit_swallow('_run@L3700', e)  # audit silent-except
            self.error = str(e)[:300]
            exited = f"exception: {self.error[:80]}"
            self.events.log("ERROR", reason=self.error)
        else:
            self.mode = feed.mode
        finally:
            # Реплей: финализация — закрыть открытые поз., зафиксировать прогон.
            if getattr(self.config, "feed", "") == "replay":
                try:
                    await self._finalize_replay()
                except Exception as _sw_e:
                    _audit_swallow('_run@finalize_replay', _sw_e)
            if self._persist_task:
                self._persist_task.cancel()
                try:
                    await self._persist_task
                except (asyncio.CancelledError, Exception):
                    pass
            for t in ('_held_sync_task', '_hot_add_task', '_vol_carousel_task', '_reconcile_task',
                      '_metrics_task',
                      '_intrabar_task', '_imoex_task', '_session_task', '_queue_task',
                      '_momentum_task', '_guard_task'):
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
        except Exception as _sw_e:
            _audit_swallow('_process_candle@L3746', _sw_e)  # audit silent-except
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
        # PaperBroker: последняя цена = close валидной свечи (equity/маржа/intrabar)
        _upd_px = getattr(self.broker, "update_price", None)
        if _upd_px is not None:
            _upd_px(figi, float(c.close))
        try:
            # В replay свечи УЖЕ в БД (оттуда и читаем) — обратная запись избыточна
            # и тормозит тест (persist ~540ms/флаш через сеть). Пишем только в live/sandbox.
            if self.mode != "replay" and not str(self.mode).startswith("test"):
                self._persist_queue.append(
                    (figi, c.ts, float(c.open), float(c.high), float(c.low), float(c.close), int(c.volume or 0))
                )
        except Exception as _sw_e:
            _audit_swallow('_process_candle@L3775', _sw_e)  # audit silent-except
            pass

        # TECHINFO: периодический отчёт (раз в 60с) о поступлении/персисте свечей
        try:
            _now3 = datetime.now(timezone.utc).timestamp()
            if _now3 - self._last_q_report_ts >= 60:
                self._last_q_report_ts = _now3
                self._log(
                    f"TECHINFO stat received={self._candles_received} seen={self.candles_seen} "
                    f"rejected={self._candles_rejected} persist_q={len(self._persist_queue)} "
                    f"persist_q5={len(self._persist_queue_5m)} mode={self.mode}",
                    level="debug",
                )
        except Exception as _sw_e:
            _audit_swallow('_process_candle@L3788', _sw_e)  # audit silent-except
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
        except Exception as _sw_e:
            _audit_swallow('_process_candle@L3803', _sw_e)  # audit silent-except
            pass

        # Стратегию и позиции обрабатываем только для тикеров из universe
        strategy = self.strategies.get(figi)
        buffer = self.buffers.get(figi)
        if strategy is None or buffer is None:
            return

        _did_execute = await self._execute_pending(figi, c)

        _new_c = EngineCandle(ts=c.ts, open=c.open, high=c.high,
                              low=c.low, close=c.close, volume=c.volume)
        if buffer and buffer[-1].ts == c.ts:
            # ENG-015 (audit 2026-09-29): стык preload (date_to=_bot_now) с
            # первой живой/реплейной свечой даёт тот же ts. По семантике
            # CandleHub это REPLACE, а не дубль — иначе движок отклоняет
            # немонотонный ряд и ансамбль молчит весь день.
            buffer[-1] = _new_c
        else:
            buffer.append(_new_c)
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
        except Exception as _sw_e:
            _audit_swallow('_process_candle@L3833', _sw_e)  # audit silent-except
            pass
        if self.log_candles:
            _tk = self.tickers.get(figi, figi[-6:])
            _ch = ((c.close - c.open) / c.open * 100) if c.open else 0.0
            _dir = "▲" if _ch > 0 else ("▼" if _ch < 0 else "·")
            # Свечи — debug: в UI (info) не спамим; в консоль/БД пишутся для разбора.
            # В реплее/тесте показываем и дату свечи (видно, где идёт прогон).
            _cfmt = "%d.%m %H:%M" if str(getattr(self.config, "feed", "")) == "replay" else "%H:%M"
            self._log(
                f"Свеча {_tk} {_msk_fmt(c.ts, _cfmt)} МСК: {c.open:.2f} → {c.close:.2f} "
                f"({_dir} {_ch:+.2f}%), объём {c.volume:g}",
                level="debug",
            )

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
        # Решаем по КОНЦУ бара (ts + шаг TF): последний вечерний бар (23:00–24:00 МСК)
        # закрывает позиции по цене закрытия часа (~23:50 МСК) — как live закрывает
        # на EOD-окне. По началу бара окно 23:40–23:50 не ловится: часовые бары
        # прыгают 23:00 → 07:00 следующего дня, и позиции уходили через ночь.
        # Рестарт-защита: бэклоговые ночные свечи не должны ложно закрывать —
        # поэтому закрываем, только если начало бара ещё в разрешённой сессии.
        _now_end = c.ts + timedelta(seconds=STEP_SEC.get(self.config.interval_name, 300))
        if (not _closed and figi not in self._swing
                and _should_force_close(_now_end, self.config.sessions, self.config.overnight)):
            if _sessions_allowed(c.ts, self.config.sessions):
                trade = await self.broker.close_position(figi, float(c.close), "overnight_force_close")
                self._held.discard(figi)
                self._opposite_count.pop(figi, None)
                self._last_exit_bar[figi] = self._bar_counter
                self._clear_exit_state(figi)
                _bh_overnight = self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter)
                if trade:
                    await self._st_close(figi, float(c.open), reason="overnight_force_close",
                                          net=float(trade.net_pnl), meta={"bars_held": _bh_overnight})
                    self._log(f"ВЫХОД {figi[-6:]} (overnight) pnl={float(trade.net_pnl):+.2f}")
            else:
                self._session_gate_log("overnight закрытие")

        # --- Re-entry cooldown check ---
        cooldown = self.config.reentry_cooldown_bars
        if cooldown > 0 and figi in self._last_exit_bar:
            bars_since = self._bar_counter - self._last_exit_bar[figi]
            if bars_since < cooldown:
                self._log_no_trade(figi, "cooldown", f"bars_since={bars_since} < {cooldown}")
                return

        # Вне разрешённых сессий (ночь/выходной): свечи уже собраны и записаны,
        # позиции обслужены — ансамбль/режим/сигналы НЕ гоняем (экономия CPU;
        # входов всё равно не будет: их отсекает session_filter).
        if not _sessions_allowed(self._bot_now(), self.config.sessions):
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
            except Exception as _sw_e:
                _audit_swallow('_process_candle@L3913', _sw_e)  # audit silent-except
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
            _audit_swallow('_process_candle@L3930', e)  # audit silent-except
            self.events.log("SIGNAL_ERROR", figi=figi, reason=str(e)[:200])
            sig = None
        finally:
            self._signal_busy.discard(figi)
        # Диагностика «почему нет входа»: на 5m-границе логируем причину из стратегии.
        if sig is None and c.ts.minute % 5 == 0:
            _skip = getattr(strategy, "_last_skip", None)
            if _skip and self._skip_logged.get(figi) != c.ts:
                self._skip_logged[figi] = c.ts
                self._log(f"ГЕЙТ {self.tickers.get(figi, figi[-6:])}: [ensemble] кандидаты отклонены — {_skip_human(_skip)}",
                    level="warn", source="gate")
        _reg_state = getattr(strategy, "_last_regime", None)
        _reg_vol = getattr(strategy, "_last_vol", None)
        # Fallback: если стратегия не вернула regime timeline (новые/hot-add тикеры),
        # считаем режим сами по 5m-барам — чтобы UI всегда показывал режим и Vol.
        # Дорого, поэтому только на 5m-границе и пока нет данных.
        if _reg_state is None and c.ts.minute % 5 == 0:
            try:
                from app.services.regime import compute_regime as _CR5
                _tl, _, _ = _CR5(list(buffer), 3600)  # режим строго на H1
                if _tl:
                    _r = _tl[-1]
                    _reg_state = {"state": _r.get("state"), "reason": _r.get("reason"),
                                  "features": _r.get("features")}
                    _feats = _r.get("features") or {}
                    _reg_vol = _feats.get("volume_ratio")
            except Exception as _sw_e:
                _audit_swallow('_process_candle@L3959', _sw_e)  # audit silent-except
                pass
        if _reg_state is not None:
            # state мог прийти dict-ом (timeline от стратегии): храним только имя.
            _reg_state_name = (_reg_state.get("state") if isinstance(_reg_state, dict) else _reg_state) or "NO_REGIME"
            self._regimes[figi] = {
                "state": _reg_state_name,
                "vol": _reg_vol,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        # Bias-exit (опция): закрыть позицию, если входной bias сменил знак против неё.
        try:
            await self._bias_exit_check(figi, self.tickers.get(figi, ""), c)
        except Exception as _sw_e:
            _audit_swallow('_process_candle@bias_exit', _sw_e)  # audit silent-except
            pass
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
        # ENG-012 (bot-контур, 29.09): exit-intent на ФЛЭТЕ — это НЕ вход.
        # Раньше голос «закрыл шорт» (close_short → BUY, kind="exit") открывал
        # LONG: в тесте 24.09 так родились 18 из 39 сделок (46% фантомов).
        # Симметрично движку EngineRunner (exit_ignored_flat). Когда позиция
        # есть — exit-голос идёт обычным путём (противоположная сторона
        # закрывает, своя — already_held).
        if state_now is PositionState.FLAT and \
                str(getattr(sig, "kind", "entry") or "entry") == "exit":
            self._log_no_trade(figi, "exit_flat", "exit-голос на флэте проигнорирован (не вход)")
            return
        bars_held = 0
        policy = SignalPolicy()
        action, note = policy.decide(sig, state_now, bars_held)

        from app.engine.models import DecisionAction
        if action is DecisionAction.ACCEPT_ENTRY:
            if figi in self._held:
                self._reject_entry("state", "already_held", "позиция уже открыта", figi, ticker)
                return
            if not _sessions_allowed(self._bot_now(), self.config.sessions):
                from app.bot.session import trading_session as _ts
                self._reject_entry("time", "session_blocked",
                                   f"вне торговых сессий (сейчас {_ts() or '—'}, "
                                   f"разрешены {'/'.join(self.config.sessions) or '—'})",
                                   figi, ticker)
                return
            if self.entries_paused:
                self._reject_entry("pause", "entries_paused", "новые входы на паузе", figi, ticker)
                return
            if sig.side.value == "BUY" and not self.config.long_allowed:
                self._reject_entry("time", "long_disabled", "Long запрещён (Направление)", figi, ticker)
                return
            if sig.side.value == "SELL" and not self.config.short_allowed:
                self._reject_entry("time", "short_disabled", "Short запрещён (Направление)", figi, ticker)
                return
            # Regime gate: режим рынка должен быть в разрешённых trade_regimes.
            _allowed_reg = self.config.trade_regimes or []
            _reg = (self._regimes.get(figi) or {}).get("state")
            _reg_name = _reg.get("state") if isinstance(_reg, dict) else _reg
            if _reg_name and _allowed_reg and _reg_name not in _allowed_reg:
                self._reject_entry("regime", f"regime_{_reg_name.lower()}",
                                   f"режим {_reg_name} отключён "
                                   f"(разрешены {'/'.join(_allowed_reg) or '—'})",
                                   figi, ticker)
                return
            # Trend-alignment: не входим против тренда (TREND_UP → только BUY, TREND_DOWN → только SELL).
            if getattr(self.config, "trend_alignment", True) and _reg_name in ("TREND_UP", "TREND_DOWN"):
                _against = (_reg_name == "TREND_UP" and sig.side.value == "SELL") or \
                           (_reg_name == "TREND_DOWN" and sig.side.value == "BUY")
                if _against:
                    self._reject_entry("trend", "trend_alignment",
                                       f"{sig.side.value} против тренда {_reg_name}", figi, ticker)
                    return
            # HOLD после серии убытков: пауза входов (per-ticker или global).
            _hold, _hold_why = self._loss_streak_block(figi)
            if _hold:
                self._reject_entry("time", "loss_streak_hold", f"HOLD после убытков — {_hold_why}", figi, ticker)
                return
            # IMOEX guard: не входить против всплеска индекса, пока он не стабилизируется.
            _imoex_why = self._imoex_block_reason(sig.side.value, figi)
            if _imoex_why:
                if self._imoex_state is not None:
                    self._imoex_state.blocks += 1
                self._reject_entry("time", "imoex_guard", f"против IMOEX — {_imoex_why}", figi, ticker)
                return
            risk = self.risk_snapshot()
            if not risk.entries_allowed():
                self._reject_entry("risk", f"risk_{risk.state.lower()}",
                                   f"стоп-лимит {risk.state} (daily_pnl={risk.daily_pnl:+.2f})",
                                   figi, ticker)
                return
            if bool(getattr(self.config, "momentum_only", False)):
                self._log(f"МОМЕНТУМ-ONLY: сигнал ансамбля {ticker} {sig.side.value} игнорируется")
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

    async def _bias_exit_check(self, figi: str, ticker: str, c) -> bool:
        """EIOT по смене bias_eff против позиции (bias_by_state, как в bt_flip2).

        Проверяет направления входного bias (TREND→30m, HV→10m) на текущем баре;
        если знак сменился против позиции — закрывает (reduce_positions).
        Возвращает True, если инициировано закрытие.
        """
        try:
            if not bool(getattr(self.config, "bias_exit_enabled", False)):
                return False
            if figi not in self._held:
                return False
            if figi in self._swing:
                return False
            # Быстрый гейт: не дублировать в течение 2 минут после инициации закрытия.
            _last = self._bias_exit_ts.get(figi, 0.0)
            from time import monotonic as _mono
            if _mono() - _last < 120.0:
                return False
            strat = self.strategies.get(figi)
            bbs = dict(getattr(getattr(strat, "p", None), "bias_by_state", {}) or {})
            if not bbs:
                return False
            # Текущий режим фиги (H1) + его bias-конфиг (tf/period).
            _reg = (self._regimes.get(figi) or {}).get("state")
            _s = _reg.get("state") if isinstance(_reg, dict) else None
            _bcfg = bbs.get(_s) if _s else None
            if not _bcfg or not isinstance(_bcfg, dict):
                return False  # NEUTRAL/RANGE и режимы без bias_by_state — выхода нет
            _tf_name = str(_bcfg.get("tf") or "30min")
            from app.services.ensemble import TF_SECONDS as _TF
            _tf_sec = _TF.get(_tf_name)
            if not _tf_sec:
                return False
            from app.services.ensemble import resample as _rs, compute_bias as _cb
            buf = self.buffers.get(figi)
            if not buf or len(buf) < 20:
                return False
            _tf_bars = _rs(list(buf), _tf_sec)
            if not _tf_bars:
                return False
            _bias_map = _cb(_tf_bars, int(_bcfg.get("period") or 50), tf_seconds=_tf_sec)
            _bucket = int(c.ts.timestamp()) // _tf_sec
            _bv = _bias_map.get(_bucket, 0)
            # Сторона позиции: BUY требует +1, SELL требует -1.
            _pos_side = str(self._exit_side.get(figi) or "").upper()
            _pos_buy = _pos_side in ("LONG", "BUY")
            _desired = 1 if _pos_buy else -1
            if _bv == 0 or _bv == _desired:
                return False
            self._bias_exit_ts[figi] = _mono()
            self._log(f"🟣 BIAS-ВЫХОД {ticker}: позиция {'BUY' if _pos_buy else 'SELL'} против bias "
                      f"{_bv:+d} ({_tf_name}) в режиме {_s or '?'} @ {_msk_fmt(c.ts, '%H:%M')} МСК — закрываю")
            self.events.log("BIAS_EXIT", figi=figi, ticker=ticker, side="BUY" if _pos_buy else "SELL",
                            bias=_bv, tf=_tf_name, regime=_s or "")
            await self.reduce_positions(1.0, "bias_exit", figis={figi})
            return True
        except Exception as _e:
            _audit_swallow('_bias_exit_check', _e)  # audit silent-except
            return False

    async def _submit_order(self, figi: str, ticker: str, action: str, side: str, meta: dict | None = None) -> None:
        cfg = self.config
        # Инверсия уже применена на уровне сигнала (см. _process_candle) — здесь НЕ дублируем.
        qty = cfg.qty_per_trade
        _used_lev = 1.0
        _is_momentum = bool((meta or {}).get("momentum"))
        _is_ai = bool((meta or {}).get("ai_trader"))
        _is_priority = bool((meta or {}).get("priority"))
        # Путь пройденных гейтов (time/market/trend/portfolio) — локал ЭТОЙ функции.
        # Кладём в order.meta при создании ордера: _execute_pending — другая функция,
        # её локалы здесь недоступны. ФИКС 2026-09-24: раньше _gate_path нигде не
        # определялся → NameError на первом входе, прошедшем time-гейт (SILENT-EXCEPT
        # _run, сигнал гиб ещё до создания ордера).
        _gate_path: list[str] = []
        _boost = (float(getattr(cfg, "top_boost", 2.0) or 2.0) if _is_priority else 1.0)
        if _is_ai:
            _boost = float((meta or {}).get("notional_pct") or 1.0)
        _sizing = str(getattr(cfg, "margin_sizing", "divide") or "divide").lower()
        # Топ-1 очередь: multiply-сайзинг только для сигналов движка. Для AI-трейдера
        # (priority=True, ai_trader=True) — обычный divide: иначе позиция ×плечо
        # (7000×5.95≈42k) и ордер режется портфельным лимитом экспозиции.
        if _is_priority and not _is_ai:
            _sizing = str(getattr(cfg, "top_sizing", "multiply") or "multiply").lower()
        # ================= STAGE A: время/контекст (дёшево, без брокера) =================
        # Цепочка вынесена в app/bot/gates.py (TIME_GATES): пауза → сессия → последний
        # час → направление → риск дня → loss-streak → already-held. Short-circuit:
        # если гейт выше рангом отклонил — дальше не идём (и маржу не спрашиваем).
        if action == "open":
            from app.bot.gates import TimeContext, TIME_GATES, run_gate_chain
            from app.bot.session import session_last_hour as _slh
            _hold_a, _hold_why_a = self._loss_streak_block(figi)
            _risk_a = self.risk_snapshot()
            _tc = TimeContext(
                cfg=cfg, side=side,
                entries_paused=bool(self.entries_paused),
                sessions_allowed=bool(_sessions_allowed(self._bot_now(), cfg.sessions)),
                is_last_hour=bool(_slh(self._bot_now(), list(cfg.sessions or []))),
                risk_allowed=bool(_risk_a.entries_allowed()),
                risk_state=str(_risk_a.state), daily_pnl=float(_risk_a.daily_pnl),
                loss_hold=bool(_hold_a), loss_why=str(_hold_why_a),
                already_held=bool(figi in self._held),
            )
            _res_a = run_gate_chain(TIME_GATES, _tc)
            if not _res_a.passed:
                self._reject_entry("time", _res_a.key, _res_a.detail, figi, ticker)
                return
        if action == "open":
            _gate_path.append("time")
        # ================= STAGE S: стакан/ликвидность (первый после ансамбля) ==========
        # MARKET_GATES из app/bot/gates.py: ликвидность → волатильность → стакан.
        # Ансамбль может кричать SHORT, но если стакан против (перевес бидов) или спред
        # широкий — сейчас не время входить, будет минус. Для ВСЕХ входов (движок + AI),
        # до тренда/портфеля/маржи. Данные — app/services/orderbook.py (кэш 5с).
        if action == "open":
            from app.bot.gates import MarketContext, MARKET_GATES, run_gate_chain
            # В реплее живые котировки не относятся к датам прогона — гейт ликвидности
            # пропускаем (иначе ложные блоки на устаревшем/нулевом обороте).
            _to_s = (0.0 if str(getattr(cfg, "feed", "")) == "replay"
                     else await self._daily_turnover(ticker))
            _atr_s = None
            for _u in self.universe:
                if _u.get("figi") == figi:
                    _ap = _u.get("atr_pct")
                    _atr_s = float(_ap) if _ap else None
                    break
            _atrs = [float(u.get("atr_pct") or 0.0)
                     for u in (self.universe or []) if u.get("atr_pct")]
            _med_s = None
            if _atrs:
                _srt = sorted(_atrs)
                _med_s = _srt[len(_srt) // 2]
            _imb_lim_s = float(getattr(cfg, "entry_ob_imbalance_max", 0.3) or 0.0)
            _spr_lim_s = float(getattr(cfg, "entry_ob_spread_max", 25.0) or 0.0)
            _ob_s = None
            if _imb_lim_s > 0 or _spr_lim_s > 0:
                try:
                    from app.services.orderbook import fetch_orderbook
                    _ob_s = await fetch_orderbook(figi, 10)
                except Exception:
                    _ob_s = None  # gate_orderbook вернёт orderbook_error
            _nb_reason = ""
            if bool(getattr(cfg, "entry_news_blackout", True)):
                try:
                    import asyncio as _aio_n
                    from app.services.news import blackout_reason as _nbr, fetch_news as _nf
                    _news_items = await _aio_n.to_thread(_nf)
                    _nb_reason = _nbr(_news_items, ticker,
                                      int(getattr(cfg, "entry_news_blackout_min", 60) or 60)) or ""
                except Exception:
                    _nb_reason = ""
            _mc = MarketContext(cfg=cfg, side=side, turnover=_to_s, atr_pct=_atr_s,
                                atr_pct_median=_med_s, orderbook=_ob_s,
                                news_blackout_reason=_nb_reason)
            _res_s = run_gate_chain(MARKET_GATES, _mc)
            if not _res_s.passed:
                self._reject_entry("signal", _res_s.key, _res_s.detail, figi, ticker)
                return
            if action == "open":
                _gate_path.append("market")
        # ================= STAGE B: сигнал/тренд (кэш-карты, без брокера) ================
        # TREND_GATES из app/bot/gates.py: daily_bias → h1_align → tf_conflict →
        # legacy MTF → якорь кворума → рейтинг. Short-circuit: первый отказ — стоп.
        if action == "open" and not (_is_momentum or _is_ai):
            from app.bot.gates import TrendContext, TREND_GATES, run_gate_chain
            try:
                _mtf = (await self.mtf_macd_map()).get(str(ticker).upper()) or {}
                _db = (await self.daily_bias_map()).get(str(ticker).upper()) or {}
            except Exception as _ge:
                self._reject_entry("trend", "tf_gate_error",
                                   f"{type(_ge).__name__}: {str(_ge)[:80]}", figi, ticker)
                return
            _h1 = _mtf.get("h1") or {}
            _m5 = _mtf.get("m5") or {}
            _qe = ((meta or {}).get("quorum_event") or {}) if isinstance(meta, dict) else {}
            _bias = str(_db.get("bias") or "")
            # info-режим daily_bias: только лог, без блокировки (veto — через гейт)
            if (bool(getattr(cfg, "daily_bias", False)) and _bias in ("up", "down")
                    and str(getattr(cfg, "daily_bias_mode", "veto")).lower() != "veto"):
                _against = ((_bias == "up" and side == "SELL")
                            or (_bias == "down" and side == "BUY"))
                if _against:
                    self._log(f"ДНЕВНОЙ BIAS {ticker}: {side} против {_bias} "
                              f"(hist {_db.get('hist'):+}, info-режим) — пропускаю дальше")
            _rwhy = ""
            try:
                await self.trade_history()
                _rok, _rwhy = self._rank_ok(ticker)
                _rwhy = "" if _rok else str(_rwhy)
            except Exception:
                _rwhy = ""
            _hmp = None
            _hmd = 0
            try:
                _hmw = max(1, int(getattr(cfg, "hm_veto_window_h", 24) or 24))
                _hm = (await self.hm_map(_hmw)).get(str(ticker).upper())
                if _hm:
                    _hmp, _hmd = float(_hm[0]), int(_hm[1])
            except Exception:
                pass
            _tctx = TrendContext(
                cfg=cfg, side=side,
                daily_bias=_bias, daily_hist=_db.get("hist"),
                h1_ok=bool(_h1.get("ok")), h1_side=str(_h1.get("side") or ""),
                h1_hist=_h1.get("hist"),
                m5_ok=bool(_m5.get("ok")), m5_trend=str(_m5.get("trend") or ""),
                m5_hist=_m5.get("hist"),
                require_member=str(getattr(cfg, "ensemble_require_member", "") or "").strip(),
                members_for=tuple(str(x) for x in (_qe.get("members_for") or [])),
                votes=int(_qe.get("votes") or 0),
                quorum=int(getattr(cfg, "ensemble_quorum", 2) or 2),
                rank_why=_rwhy,
                hm_pct=_hmp, hm_dur=_hmd,
            )
            _res_b = run_gate_chain(TREND_GATES, _tctx)
            if not _res_b.passed:
                self._reject_entry("trend", _res_b.key, _res_b.detail, figi, ticker)
                return
            if action == "open":
                _gate_path.append("trend")
        # ================= STAGE C: портфель (позиции/кластер/L-S, без брокера) ==========
        # PORTFOLIO_GATES из app/bot/gates.py: max_positions → sector_cluster → ls_balance.
        if action == "open":
            from app.bot.gates import PortfolioContext, PORTFOLIO_GATES, run_gate_chain
            _sector = ""
            _sector_count = 0
            try:
                _meta_s = await self.sector_meta()
                _sector = str((_meta_s.get(str(ticker).upper())
                               or _meta_s.get(figi) or {}).get("sector") or "")
                if _sector and _sector != "other":
                    for _f in self._held:
                        _tk = self.tickers.get(_f, "")
                        _s2 = str((_meta_s.get(str(_tk).upper())
                                   or _meta_s.get(_f) or {}).get("sector") or "")
                        if _s2 == _sector:
                            _sector_count += 1
            except Exception:
                pass
            _shorts_c = sum(1 for _f in self._held
                            if str(self._exit_side.get(_f, "")).upper() == "SHORT")
            _pctx = PortfolioContext(cfg=cfg, side=side, held_count=len(self._held),
                                     sector=_sector, sector_count=_sector_count,
                                     short_count=_shorts_c, ticker=ticker,
                                     held_tickers=tuple(self.tickers.get(_f, "").upper()
                                                        for _f in self._held))
            _res_c = run_gate_chain(PORTFOLIO_GATES, _pctx)
            if not _res_c.passed:
                self._reject_entry("portfolio", _res_c.key, _res_c.detail, figi, ticker)
                return
            if action == "open":
                _gate_path.append("portfolio")
        # ================= STAGE C2: AI-чейзинг (стакан — общий, в STAGE S) =============
        # Только для AI-ордеров: после time/trend/portfolio, но ДО запроса маржи.
        if action == "open" and _is_ai:
            _ai_skip: list[str] = []
            _ch = float(getattr(cfg, "ai_chase_pct", 3.0) or 0.0)
            _chg = None
            try:
                _buf_chg = self.buffers.get(figi)
                if _buf_chg:
                    _msk = timezone(timedelta(hours=3))
                    _today = self._bot_now().astimezone(_msk).date()
                    _bars_today = [b for b in _buf_chg
                                   if b.ts.astimezone(_msk).date() == _today]
                    if len(_bars_today) >= 2:
                        _f0 = float(_bars_today[0].open or _bars_today[0].close or 0)
                        _l0 = float(_bars_today[-1].close or 0)
                        if _f0 > 0 and _l0 > 0:
                            _chg = (_l0 / _f0 - 1.0) * 100.0
            except Exception:
                _chg = None
            if _ch and _chg is not None:
                if side == "BUY" and _chg > _ch:
                    _ai_skip.append(f"чейзинг: +{_chg:.1f}% за день без отката")
                if side == "SELL" and _chg < -_ch:
                    _ai_skip.append(f"чейзинг: {_chg:.1f}% за день без отскока")
            if _ai_skip:
                self._log(f"ПРОПУСК ВХОДА {ticker}: [approval] ai_chase — {'; '.join(_ai_skip)}")
                self.events.log("AI_ORDER_SKIPPED", figi=figi, ticker=ticker,
                                reason="; ".join(_ai_skip)[:200])
                self._log_no_trade(figi, "ai_chase")
                return
        # ================= STAGE D: сайзинг и маржа (запросы к брокеру) ==================
        if action == "open":
            buf = self.buffers.get(figi)
            price = float(buf[-1].close) if buf else 0.0
            lot = 10
            for u in self.universe:
                if u.get("figi") == figi and u.get("lot"):
                    lot = int(u["lot"])
                    break
            else:
                # Не в eligible-универсе (AI-трейдер может брать лидеров движения) —
                # лот берём из БД, иначе дефолт 10 даёт ошибку размера в 10 раз.
                try:
                    from app.models.instrument import Instrument as _Inst
                    async with SessionLocal() as _db:
                        _l = (await _db.execute(
                            select(_Inst.lot).where(_Inst.figi == figi)
                        )).scalar_one_or_none()
                    if _l:
                        lot = int(_l)
                except Exception as _sw_e:
                    _audit_swallow('_submit_order@L4173', _sw_e)  # audit silent-except
                    pass
            budget = cfg.ensemble_capital
            if isinstance(self.broker, LiveBroker):
                try:
                    # Бюджет на ОДИН слот = pos_pct от equity. НЕ режем по свободному
                    # кэшу: маржа (риск-ставка брокера) позволяет позиции больше кэша,
                    # а реальный потолок qty ставит блок MARGIN (max_lots брокера).
                    _eq = await self.broker.equity()
                    _free = await self.broker.free_funds()
                    _slot_pct = self._pos_pct() * _boost
                    budget = _eq * _slot_pct
                except Exception as _sw_e:
                    _audit_swallow('_submit_order@L4185', _sw_e)  # audit silent-except
                    try:
                        live_cash = await self.broker.cash()
                        budget = live_cash * self._pos_pct() * _boost
                    except Exception as _sw_e:
                        _audit_swallow('_submit_order@L4189', _sw_e)  # audit silent-except
                        pass
            elif isinstance(self.broker, PaperBroker):
                # Тест-режим: эмулируем Live — бюджет = доля от начального капитала теста
                # (PaperBroker не спрашивает equity у брокера). Плечо ниже берётся из БД.
                try:
                    _acc = await self.broker.ensure_account(cfg.initial_cash)
                    _pos_list = await self.broker.positions()
                    # PaperBroker: cash = свои минус комиссии, номинал не списывается,
                    # поэтому equity = cash + НЕРЕАЛИЗОВАННЫЙ P&L позиций
                    # (прежняя формула cash + Σ|qty×price| удваивала шорты: 10к → 16к).
                    _pnl_open = 0.0
                    for _p in _pos_list:
                        _pb = self.buffers.get(getattr(_p, "figi", ""))
                        _ppx = float(_pb[-1].close) if _pb else float(getattr(_p, "entry_price", 0) or 0)
                        _q = abs(float(getattr(_p, "qty", 0) or 0))
                        _ep = float(getattr(_p, "entry_price", 0) or 0)
                        _short = str(getattr(_p, "side", "")).upper() in ("SELL", "SHORT")
                        _pnl_open += ((_ep - _ppx) if _short else (_ppx - _ep)) * _q
                    _eq = float(_acc.cash or 0.0) + _pnl_open
                    if _eq <= 0:
                        _eq = float(cfg.initial_cash)
                    budget = _eq * self._pos_pct() * _boost
                    self._log(
                        f"TEST BUDGET {ticker}: equity≈{_eq:.0f}₽ → слот {budget:.0f}₽ "
                        f"(слот {self._pos_pct()*_boost*100:.0f}% от EQ) · initial={cfg.initial_cash:.0f}₽"
                    )
                except Exception as e:
                    self._log(f"TEST BUDGET FAIL {ticker}: {type(e).__name__}: {str(e)[:80]} — слот {budget:.0f}₽")
            lev = max(1.0, float(cfg.leverage or 1.0))
            lot_cost = price * lot
            if price <= 0 or lot <= 0 or lot_cost <= 0:
                self._log(f"ПРОПУСК СДЕЛКИ {ticker}: [sizing] price_lot — цена={price} лот={lot}")
                return
            own_per_lot = lot_cost  # divide: позиция = бюджет (свои = бюджет / плечо)
            # Ранняя проверка достаточности бюджета. В divide-режиме плечо НЕ
            # уменьшает требуемые свои на 1 лот (own_per_lot == lot_cost),
            # поэтому если денег нет даже на лот — пропускаем СРАЗУ, без
            # запроса max.lots у брокера (Log: «зачем просить маржу,
            # если даже на одну акцию не хватает»). В multiply-режиме плечо
            # может спасти лот (own_per_lot = lot_cost/lev) — идём в блок MARGIN.
            if budget < lot_cost and str(cfg.margin_sizing).lower() != "multiply":
                self._log(f"ПРОПУСК СДЕЛКИ {ticker}: [sizing] budget — бюджет {budget:.0f} < стоимость лота {lot_cost:.0f} (divide)")
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
                    if _is_momentum:
                        # Максимальное плечо по риск-ставке с капом momentum_max_lev
                        _mstop_lev = float(getattr(cfg, "momentum_max_lev", 2.0) or 2.0)
                    if use_margin and _mrgn_lots > 0:
                        # Плечо: риск-ставка брокера по инструменту (dlong/dshort) — это
                        # реальное обеспечение; ml.leverage из GetMaxLots зависит от
                        # свободных денег счёта и часто врёт (×1.00 при марже).
                        _m_risk = (self._sector_meta or {}).get(str(ticker).upper()) or {}
                        _risk = float(_m_risk.get("dshort" if side == "SELL" else "dlong") or 0.0)
                        _lev_src = "риск-ставка"
                        if 0 < _risk < 1:
                            _max_lev = 1.0 / _risk
                        else:
                            _max_lev = ml.leverage
                            _lev_src = "max_lots"
                        _want = float(cfg.margin_leverage or 0.0)
                        lev = _max_lev if _want <= 0 else min(_max_lev, _want)
                        if _is_momentum:
                            lev = min(lev, float(getattr(cfg, "momentum_max_lev", 2.0) or 2.0))
                        if lev < 1.0:
                            lev = 1.0
                        # Режим размера позиции:
                        #   divide   — позиция = бюджет (свои = бюджет / плечо)  [1-й счёт]
                        #   multiply — позиция = бюджет × плечо (свои = бюджет)  [2-й счёт]
                        if _sizing == "multiply":
                            own_per_lot = lot_cost / lev
                            _pos, _own = budget * lev, budget
                        else:
                            own_per_lot = lot_cost
                            _pos, _own = budget, budget / lev
                        self._log(f"MARGIN LEV {ticker}: брокер ×{_max_lev:.2f} ({_lev_src}"
                                  + (f", риск {_risk:.3f}" if 0 < _risk < 1 else "") + ") · "
                                  f"выбрано {'Max' if _want <= 0 else '×'+format(_want, 'g')} → ×{lev:.2f} · "
                                  f"режим={_sizing}{' [топ-1]' if (_is_priority and not _is_ai) else ''} "
                                  f"(позиция {_pos:.0f}₽ = свои {_own:.0f}₽ + заём {_pos-_own:.0f}₽)")
                except Exception as e:
                    self._log(f"MARGIN CHECK FAIL {ticker}: {e} — proceed at cfg.leverage={lev:.1f}")
            if budget < own_per_lot:
                self._log(f"ПРОПУСК СДЕЛКИ {ticker}: [sizing] budget — бюджет {budget:.0f} < стоимость лота {own_per_lot:.0f}")
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
                    self._log(f"ПРОПУСК СДЕЛКИ {ticker}: [sizing] margin_limit — лимит ({_lim_kind}) = 0")
                    return
                if qty > max_lots:
                    self._log(f"QTY CAP {ticker}: {qty} → {max_lots} ({_lim_kind}, {_tss or '—'})")
                    qty = max_lots
            except Exception as e:
                self._log(f"MARGIN CHECK FAIL {ticker}: {e} — proceed without cap")
        # --- Кап совокупной экспозиции: свои деньги в позициях <= max_exposure_pct от equity ---
        if action == "open":
            try:
                _cap = float(getattr(cfg, "max_exposure_pct", 0.0) or 0.0)
                if _cap > 0:
                    _eq_cap = float(await self.broker.equity() or 0.0)
                    if _eq_cap > 0:
                        # Свои деньги/маржа — «правда» брокера (starting_margin), а не наша
                        # оценка entry×qty/lev (она завышала в разы). Если брокер недоступен —
                        # fallback на оценку.
                        _own_now = None
                        _eff_lev = None
                        _ma_fn = getattr(self.broker, "margin_attributes", None)
                        if _ma_fn is not None:
                            try:
                                _ma = await _ma_fn()
                                if _ma and _ma.get("liquid"):
                                    _own_now = float(_ma.get("starting_margin") or 0.0)
                                    _mv_cap = float(await self.broker.market_value() or 0.0)
                                    _eff_lev = max(1.0, _mv_cap / max(_own_now, 1.0))
                                    _eq_cap = float(_ma.get("liquid") or _eq_cap)
                            except Exception as _sw_e:
                                _audit_swallow('_submit_order@L4345', _sw_e)  # audit silent-except
                                _own_now = None
                        if _own_now is None:
                            _own_now = 0.0
                            for _f in list(self._held):
                                _ep = float(self._exit_entry_px.get(_f) or 0.0)
                                _q = float(self._exit_qty.get(_f) or 0.0)
                                _lev = max(1.0, float(self._pos_leverage.get(_f, 1.0) or 1.0))
                                _own_now += (_ep * _q) / _lev
                            _eff_lev = 1.0
                        _lot_cap = next((u.get("lot") for u in self.universe
                                         if u.get("figi") == figi), 1) or 1
                        # Свои деньги НОВОЙ позиции = номинал / плечо именно этой позиции
                        # (_used_lev). Точно в обоих режимах: divide — own=бюджет/lev,
                        # multiply — own=бюджет=номинал/lev. Портфельный _eff_lev не годится:
                        # при пустом портфеле он =1 и own_new раздувался до полного номинала
                        # (лог: «свои 10000+9892 > equity 10000» при выбранном ×5).
                        _own_new = ((float(price) * int(qty) * int(_lot_cap))
                                    / max(1.0, float(_used_lev or 1.0)))
                        if _own_now + _own_new > _eq_cap * _cap:
                            self._log(f"ПРОПУСК ВХОДА {ticker}: [portfolio] max_exposure — кап экспозиции "
                                      f"{_cap*100:.0f}% (свои {_own_now:.0f}+{_own_new:.0f} > equity {_eq_cap:.0f}₽)")
                            self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                            reason="MAX_EXPOSURE")
                            self._log_no_trade(figi, "max_exposure")
                            return
            except Exception as _sw_e:
                _audit_swallow('_submit_order@L4366', _sw_e)  # audit silent-except
                pass
        order = BotOrder(
            id=_new_order_id(),
            figi=figi,
            ticker=ticker,
            action=action,
            side=side,
            qty=qty,
            meta={**dict(meta or {}), "leverage": float(_used_lev),
                  "gate_path": list(_gate_path)},
        )
        # --- Портфельные лимиты (net exposure / сектор / маржа / стресс) ---
        if action == "open":
            try:
                from app.bot.portfolio import (PortfolioLimits as _PL, check_order as _pcheck,
                                               regime_limits as _rlim)
                _snap = await self.portfolio_snapshot()
                _regime = await self.market_regime()
                _base = _PL(
                    max_net_exposure_pct=float(getattr(cfg, "max_net_exposure_pct", 0.5) or 0.0),
                    max_sector_pct=float(getattr(cfg, "max_sector_pct", 0.35) or 0.0),
                    max_margin_use_pct=float(getattr(cfg, "max_margin_use_pct", 0.8) or 0.0),
                    max_stress_loss_pct=float(getattr(cfg, "max_stress_loss_pct", 0.1) or 0.0),
                )
                _lim = _rlim(_base, _regime)
                if _is_priority and not _is_ai and bool(getattr(cfg, "top_relax_caps", True)):
                    # Сильнейшему — большая сумма: сектор без лимита, net до 100%
                    # (если net-лимит вообще включён). Жёсткими остаются стресс и маржа.
                    if _lim.max_net_exposure_pct > 0:
                        _lim.max_net_exposure_pct = max(_lim.max_net_exposure_pct, 1.0)
                    _lim.max_sector_pct = 0.0
                # лот: из universe
                _lot_pf = next((u.get("lot") for u in self.universe if u.get("figi") == figi), 1) or 1
                _notional = float(price) * int(qty) * int(_lot_pf)
                # Разворот рынка против книги: не добавляем в убыточную сторону.
                _net = float(_snap.get("net_notional") or 0.0)
                if str(_regime.get("state")) == "reversal" and (
                        (_net < 0 and side == "SELL") or (_net > 0 and side == "BUY")):
                    self._log(f"ПРОПУСК ВХОДА {ticker}: [portfolio] market_reversal — разворот рынка против книги "
                              f"(net {_net:+.0f}₽, IMOEX 20м {_regime.get('pct_20m')}%)")
                    self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                    reason="MARKET_REVERSAL")
                    self._log_no_trade(figi, "market_reversal")
                    return
                _ok_pf, _why_pf = _pcheck(_snap, side, _notional, ticker,
                                          await self.sector_meta(), _lim)
                if not _ok_pf:
                    self._log(f"ПРОПУСК ВХОДА {ticker}: [portfolio] portfolio_limit — портфельный лимит — {_why_pf}"
                              + (f" [режим {_regime.get('state')}]" if _regime.get("state") != "neutral" else ""))
                    self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                    reason="PORTFOLIO_LIMIT", detail=_why_pf)
                    self._log_no_trade(figi, "portfolio_limit")
                    if bool(getattr(cfg, "queue_enabled", True)) and not (meta or {}).get("priority"):
                        await self._enqueue_candidate(figi, ticker, side, _why_pf,
                                                      meta=meta, snap=_snap)
                    return
            except Exception as _e:
                self._log(f"ПОРТФЕЛЬ-ЛИМИТ {ticker}: проверка не удалась ({type(_e).__name__}) — пропускаю")
        # --- AI-гейт: дедуп и пауза после отклонения ---
        if action == "open" and not _is_ai and bool(getattr(cfg, "ai_approval", False)):
            _pend = self.pending_orders.get(figi)
            if _pend is not None and getattr(_pend, "status", "") == "PENDING_APPROVAL":
                self._log(f"AI-ГЕЙТ: {ticker} уже ждёт решения — новую заявку не создаём")
                self._log_no_trade(figi, "ai_already_pending")
                return
            _cd = float(getattr(cfg, "ai_reject_cooldown_min", 15.0) or 0.0)
            _until = self._ai_reject_until.get(figi)
            if _cd > 0 and _until is not None and self._bot_now() < _until:
                _left = (_until - self._bot_now()).total_seconds() / 60.0
                # Пауза по тикеру: пишем понятный лог про каждый пропуск (раз в минуту,
                # со счётчиком), чтобы всегда было видно, что именно отклонило вход.
                self._ai_reject_cnt[figi] = self._ai_reject_cnt.get(figi, 0) + 1
                _nn = self._ai_reject_cnt[figi]
                _ll = self._ai_reject_last_log.get(figi, 0.0)
                _now_m = _time.monotonic()
                if _nn == 1 or (_ll and _now_m - _ll >= 60.0):
                    self._ai_reject_last_log[figi] = _now_m
                    _rsn = self._ai_reject_reason.get(figi, "отклонение ИИ")[:90]
                    self._log(f"AI-ГЕЙТ: {ticker} на паузе ещё {_left:.0f} мин — отклонён: {_rsn} "
                              f"(пропущено сигналов: {_nn})")
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker, reason="AI_REJECT_COOLDOWN")
                self._log_no_trade(figi, "ai_reject_cooldown")
                return
            order.status = "PENDING_APPROVAL"
            self._approvals_since[order.id] = _time.monotonic()
            self._ai_reject_cnt.pop(figi, None)
            self._ai_reject_last_log.pop(figi, None)
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
                    # Таймаут-отклонение тоже ставит паузу по тикеру (иначе один и тот же
                    # сигнал переспрашивает AI каждый бар).
                    try:
                        _cdt = float(getattr(cfg, "ai_reject_cooldown_min", 15.0) or 0.0)
                        if _cdt > 0:
                            self._ai_reject_until[figi] = self._bot_now() + timedelta(minutes=_cdt)
                            self._ai_reject_reason[figi] = "таймаут — AI-гейт не ответил (default=reject)"
                    except Exception as _sw_e:
                        _audit_swallow('_execute_pending@L4625', _sw_e)  # audit silent-except
                        pass
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
                if _why:
                    self._ai_reject_reason[figi] = _why
                self._log(f"AI-ГЕЙТ: вход {order.ticker} ОТКЛОНЁН — {_why}")
                self.events.log("AI_APPROVAL_REJECTED", figi=figi, ticker=order.ticker,
                                order_id=order.id, reason=(order.meta or {}).get("ai_reason"))
                return False
            self._approvals_since.pop(order.id, None)
        if order.action == "close":
            try:
                trade = await self.broker.close_position(figi, c.open, "signal_exit")
            except Exception as _e:
                order.status = "REJECTED"
                self._log(f"⛔ ЗАКРЫТИЕ ОТКЛОНЕНО БРОКЕРОМ {order.ticker}: "
                          f"{type(_e).__name__}: {str(_e)[:140]}")
                self.events.log("ORDER_REJECTED", figi=figi, ticker=order.ticker,
                                order_id=order.id, action="close", error=str(_e)[:200])
                return False
            actual_exit = price_from_trade(trade) if trade else c.open
            order.status = "FILLED"
            order.filled_at = datetime.now(timezone.utc)
            order.price = actual_exit
            self._held.discard(figi)
            self._clear_exit_state(figi)
            _bars_held = self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter)
            _exit_meta_sig = {"bars_held": _bars_held, "signal_note": "signal_exit",
                              "gate_path_in": list((order.meta or {}).get("gate_path") or [])}
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
            # локальный импорт НЕ нужен: AtrStopPolicy/FixedSlTpPolicy уже на top-level (L81).
            # Если оставить import здесь — в Python это локальная переменная ВСЕЙ функции,
            # и else-ветка ниже получит UnboundLocalError (баг 2026-09-23: бот падал,
            # запись свечей в БД вставала).
            # SL: стандартный = cfg.initial_sl_atr (4×ATR, фикс вместо optuna sl_mult 4-5).
            # AtrStopPolicy/FixedSlTpPolicy импортированы на уровне модуля (L81); локальный
            # импорт здесь делал имя локальным для всей функции → UnboundLocalError в else.
            # TP: из optuna-параметров rr стратегии figi (EnsembleParams).
            strat = self.strategies.get(figi)
            _sl_mult = cfg.initial_sl_atr
            _rr = getattr(strat.p, "rr", None) if strat is not None else None
            if _rr is None:
                _rr = cfg.atr_risk_reward
            # Проскальзывание на входе (adverse) — parity с бэктестом (fill_price).
            _cm = CostModel(commission_rate=cfg.commission_rate, slippage_bps=cfg.slippage_bps)
            _fill = _cm.fill_price(float(c.open), side)
            if (order.meta or {}).get("ai_trader") and (
                    order.meta.get("sl_pct") or order.meta.get("tp_pct")):
                _slp = float(order.meta.get("sl_pct") or 0.03)
                _tpp = float(order.meta.get("tp_pct") or 0.0)
                exit_policy = FixedSlTpPolicy(stop_pct=_slp,
                                              target_pct=_tpp if _tpp > 0 else 10.0)
                plan = exit_policy.plan_entry(side, _fill, [])
                if _tpp <= 0:
                    # AI не задал TP — не ставим его вовсе (target_pct=10.0 у шорта
                    # давал отрицательную цену TP, напр. -66.6 в UI)
                    plan.take_profit = None
            elif (order.meta or {}).get("momentum"):
                # Моментум-режим: стоп momentum_stop_pct (3%), TP не ставим (выход — EOD)
                exit_policy = FixedSlTpPolicy(
                    stop_pct=float(getattr(cfg, "momentum_stop_pct", 0.03) or 0.03),
                    target_pct=10.0)
                plan = exit_policy.plan_entry(side, _fill, [])
                plan.take_profit = None
            elif cfg.sl_mode == "fixed":
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
                # ATR для SL/TP — на ТФ входа (5м), как в бэктесте (паритет).
                # Раньше брали 1м буфер → SL/TP были ~2.2× уже и выбивались шумом.
                _b1 = list(self.buffers.get(figi, []))
                buf_raw = self._get_5m_bars(figi, _b1) or _b1
                plan = exit_policy.plan_entry(side, _fill, buf_raw)
        else:
            exit_policy = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
            _cm = CostModel(commission_rate=cfg.commission_rate, slippage_bps=cfg.slippage_bps)
            _fill = _cm.fill_price(float(c.open), side)
            plan = exit_policy.plan_entry(side, _fill, [])
        try:
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
        except Exception as _e:
            # Ошибка брокера (нехватка средств, отказ биржи) НЕ должна ронять рантайм:
            # помечаем ордер REJECTED, логируем — бот продолжает работать.
            order.status = "REJECTED"
            self._log(f"⛔ ОРДЕР ОТКЛОНЁН БРОКЕРОМ {order.ticker} {order.side} "
                      f"qty={order.qty}: {type(_e).__name__}: {str(_e)[:140]}")
            self.events.log("ORDER_REJECTED", figi=figi, ticker=order.ticker,
                            order_id=order.id, action="open", error=str(_e)[:200])
            return False
        entry_px = actual_entry if actual_entry and actual_entry > 0 else c.open
        order.status = "FILLED"
        order.filled_at = datetime.now(timezone.utc)
        order.price = entry_px
        try:
            _om = dict(order.meta or {})
            # gate_path берём из meta ордера (заполнен в _submit_order при создании):
            # локалов _submit_order в этой функции нет — раньше NameError, который
            # глушился этим же except и gate_path у сделок терялся.
            _g = list(_om.get("gate_path") or [])
            if _om.get("ai_trader"):
                _g.append("ai")
            if _om.get("momentum"):
                _g.append("momentum")
            _om["gate_path"] = _g
            # ATR на входе — для UI «SL/TP был начальный в ATR/R/ROI» (meta сделки).
            _ate = None
            try:
                if isinstance(exit_policy, AtrStopPolicy) and plan is not None \
                        and plan.stop_loss is not None and plan.stop_loss != _fill \
                        and exit_policy.multiplier > 0:
                    _ate = abs(_fill - float(plan.stop_loss)) / exit_policy.multiplier
            except Exception:
                _ate = None
            if _ate is not None and _ate > 0:
                _om["atr_entry"] = round(float(_ate), 6)
            order.meta = _om
        except Exception:
            pass
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
        self._pos_leverage[figi] = max(1.0, float((order.meta or {}).get("leverage") or 1.0))
        self._trail_active[figi] = False
        self._trail_stop[figi] = float(plan.stop_loss) if plan.stop_loss is not None else 0.0
        # Старт пика P&L: от входа, лучший ход = 0.
        self._peak_pnl[figi] = {"pnl": 0.0, "ts": c.ts, "price": float(entry_px),
                                "atr_abs": None, "atr_pct": None, "mae": 0.0}
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
        self._swing.discard(figi)
        self._exit_plans.pop(figi, None)
        self._exit_side.pop(figi, None)
        self._exit_entry_px.pop(figi, None)
        self._exit_qty.pop(figi, None)
        self._trail_active.pop(figi, None)
        self._trail_stop.pop(figi, None)
        self._exit_target.pop(figi, None)
        self._pos_leverage.pop(figi, None)

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
            # Пик P&L после рестарта считаем заново (прошлые бары недоступны).
            self._peak_pnl[figi] = {"pnl": 0.0, "ts": c.ts, "price": float(_entry_px),
                                    "atr_abs": None, "atr_pct": None, "mae": 0.0}
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
            except Exception as _sw_e:
                _audit_swallow('_ensure_exit_state@L4840', _sw_e)  # audit silent-except
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
        # Вне сессии позицию не закрываем: sandbox/биржа не принимает заявки (30079),
        # а бэклог ночных свечей не должен генерировать исполнения.
        if not self._in_trading_session():
            self._session_gate_log("сигнальный выход")
            return False
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

        # --- Пик P&L сделки: лучший ход в нашу сторону (макс. цена при лонге /
        # мин. при шорте) и макс. просадка против нас (MAE) — на каждом баре.
        try:
            _pk = self._peak_pnl.setdefault(figi, {
                "pnl": 0.0, "ts": c.ts, "price": float(entry_px),
                "atr_abs": None, "atr_pct": None, "mae": 0.0})
            _sgn = 1.0 if state == PositionState.LONG else -1.0
            # Экстремумы бара с точки зрения позиции: лучший ход — макс. цена
            # при лонге / мин. при шорте; худший — наоборот (иначе у шорта
            # «пик» считался бы от high, т.е. от хода ПРОТИВ нас).
            _best_px = float(c.high) if state == PositionState.LONG else float(c.low)
            _worst_px = float(c.low) if state == PositionState.LONG else float(c.high)
            _pnl_best = (_best_px - float(entry_px)) * qty_sh * _sgn    # лучший ход внутри бара
            _pnl_worst = (_worst_px - float(entry_px)) * qty_sh * _sgn  # худший ход внутри бара
            if _pnl_best > float(_pk.get("pnl") or 0.0):
                _atr = self.atr_now(figi)
                _pk.update(pnl=_pnl_best, ts=c.ts, price=_best_px,
                           atr_abs=(float(_atr) if _atr else None),
                           atr_pct=((float(_atr) / float(c.close) * 100.0) if (_atr and c.close) else None))
            _mae_bar = max(0.0, -_pnl_worst)
            if _mae_bar > float(_pk.get("mae") or 0.0):
                _pk["mae"] = _mae_bar
        except Exception as _pk_e:
            _audit_swallow('_step_exit@peak_pnl', _pk_e)  # audit silent-except
            pass

        upd = getattr(policy, "update_stop", None)
        act = getattr(policy, "trailing_activated", None)
        trail_active = bool(self._trail_active.get(figi, False))

        # 1) Если трейлинг уже активен — подтягиваем стоп за ценой (ratchet).
        new_stop = None
        if trail_active and upd is not None:
            try:
                _pk_px = None
                try:
                    _pk_px = float((self._peak_pnl.get(figi) or {}).get("price")) or None
                except Exception:
                    _pk_px = None
                new_stop = upd(trail_side, float(entry_px), self._trail_stop.get(figi),
                               act_bars, qty=qty_sh, commission=comm, peak_price=_pk_px)
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
            from app.config import get_settings as _get_settings
            _s = _get_settings()
            _dbg = bool(getattr(_s, "log_debug_engine", False))
        except Exception as _sw_e:
            _audit_swallow('_step_exit@L4928', _sw_e)  # audit silent-except
            _dbg = False
        if _dbg:
            self._log(f"DBG-EXIT {figi[-6:]} {state.value} entry={entry_px:.2f} qty={qty_sh} "
                      f"bar_ts={c.ts.strftime('%H:%M:%S')} o={c.open:.2f} h={c.high:.2f} l={c.low:.2f} c={c.close:.2f} "
                      f"stop={stop if stop is not None else '-'} tp={tp if tp is not None else '-'} "
                      f"trail={trail_active} pnl_rub={(c.close - entry_px) * qty_sh if state == PositionState.LONG else (entry_px - c.close) * qty_sh:+.2f}")
        # Пик PnL позиции: точка макс. прибыли на каждом баре (по экстремуму
        # intrabar: high для LONG / low для SHORT, без комиссий) — для отчёта
        # max PnL + время + ATR в карточке сделки.
        try:
            _peak_px = c.high if state == PositionState.LONG else c.low
            _cur_pnl = (_peak_px - entry_px) * qty_sh if state == PositionState.LONG else (entry_px - _peak_px) * qty_sh
            # MAE до пика: макс. просадка ниже входа (в ATR) на пути к пику прибыли.
            # LONG — низ бара ниже входа, SHORT — верх бара выше входа.
            # Копим с входа до момента пика; провалы ПОСЛЕ пика не учитываем.
            _atr_v = None
            try:
                _atr_v = self.atr_now(figi)
            except Exception:
                _atr_v = None
            _mae_cur = 0.0
            _adv_px = 0.0
            try:
                if entry_px:
                    # Adverse = ход ПРОТИВ позиции: LONG — ниже входа, SHORT — выше.
                    # (29.09: для LONG знак был перевёрнут — low-entry>0 — MAE молчал.)
                    _adv_px = (float(entry_px) - float(c.low)) if state == PositionState.LONG else (float(c.high) - float(entry_px))
                    if _adv_px > 0 and _atr_v:
                        _mae_cur = _adv_px / float(_atr_v)
            except Exception:
                _mae_cur = 0.0
                _adv_px = 0.0
            _peak = self._peak_pnl.get(figi)
            if _peak is None:
                _peak = {"pnl": float("-inf"), "mae_cur": 0.0, "mae_atr": 0.0}
            _peak["mae_cur"] = max(float(_peak.get("mae_cur", 0.0)), _mae_cur)
            # Макс. просадка в ЦЕНЕ за всю сделку (для калибровки SL): расстояние,
            # цена и время самого глубокого ухода против позиции (29.09).
            if _adv_px > float(_peak.get("mae_dist", 0.0)):
                _peak["mae_dist"] = float(_adv_px)
                _peak["mae_px"] = float(c.low) if state == PositionState.LONG else float(c.high)
                _peak["mae_ts"] = c.ts.isoformat()
            if _cur_pnl > float(_peak.get("pnl", -1e18)):
                _atr_p = 0.0
                try:
                    _atr_p = float(_atr_v / entry_px * 100) if (_atr_v and entry_px) else 0.0
                except Exception:
                    pass
                # Новая вершина: фиксируем накопленную на этот момент просадку.
                _peak.update({"pnl": round(_cur_pnl, 2), "ts": c.ts.isoformat(),
                              "price": float(_peak_px), "atr_pct": round(_atr_p, 2),
                              "atr_abs": (round(float(_atr_v), 4) if _atr_v else None),
                              "mae_atr": round(float(_peak.get("mae_cur", 0.0)), 2)})
            self._peak_pnl[figi] = _peak
        except Exception as _sw_e:
            _audit_swallow('_step_exit@peak', _sw_e)  # audit silent-except
            pass
        # --- Виртуальный (info) трейлинг: считаем, где бы сработал трейл, НЕ трогая
        # реальный SL/TP (реальный трейлинг может быть выключен — trail_activation_comm_mult=None).
        # Логика зеркалит реальную: активация при pnl >= комиссия_входа×4, стоп идёт за ценой
        # (update_stop, ratchet), срабатывание — только по закрытию бара за уровнем (close_based).
        try:
            if not self._trail_active.get(figi, False):
                _ti = self._trail_info.get(figi)
                if _ti is None:
                    # Ленивая инициализация: виртуальный трейл-стоп стартует от реального SL.
                    _ti = {"active": False, "act_track": self._trail_stop.get(figi)}
                    self._trail_info[figi] = _ti
                _ti_cfg = (getattr(self.config, "trail_info_activation_comm_mult", None)
                            or getattr(self.config, "trail_activation_comm_mult", None))
                if _ti_cfg is not None and _ti is not None:
                    _act_bars = list(buf) if buf else [c]
                    from dataclasses import replace as _dc_replace
                    if getattr(policy, "trailing_activated", None) is None:
                        # Реальная политика без трейлинга (fixed_sl_tp — «копия
                        # OsEngine») — виртуальный трейл строим от ATR-конфига
                        # НЕЗАВИСИМО. Раньше блок молчал: _dc_replace не добавляет
                        # методы, и карточки не получали «трейл бы сработал» (29.09).
                        from app.engine.exits import AtrStopPolicy as _AtrInfoPolicy
                        _tpol = _AtrInfoPolicy(
                            period=int(getattr(self.config, "atr_period", 14) or 14),
                            multiplier=float(getattr(self.config, "initial_sl_atr", 4.0) or 4.0),
                            trail_activation_comm_mult=_ti_cfg,
                            trail_distance_r=float(getattr(self.config, "trail_distance_atr", 2.5) or 2.5),
                            trail_compress_r=float(getattr(self.config, "trail_compress_r", 0.0) or 0.0),
                            trail_min_factor=float(getattr(self.config, "trail_min_factor", 0.3) or 0.3),
                            trail_min_atr=float(getattr(self.config, "trail_min_atr", 0.0) or 0.0),
                            trail_vol_boost=float(getattr(self.config, "trail_vol_boost", 0.0) or 0.0),
                        )
                    else:
                        _tpol = _dc_replace(policy, trail_activation_comm_mult=_ti_cfg)
                    _tact = getattr(_tpol, "trailing_activated", None)
                    _tupd = getattr(_tpol, "update_stop", None)
                    _born_this_bar = False
                    if not _ti.get("active"):
                        # Этап активации: как бы включился трейлинг (info).
                        try:
                            _activated = False
                            if _tact is not None:
                                _activated = _tact(trail_side, float(entry_px), qty_sh, comm, _act_bars)
                            if _activated:
                                _ti["active"] = True
                                _ti["act_track"] = self._trail_stop.get(figi)
                                _born_this_bar = True
                                self._log(f"ИНФО-ТРЕЙЛ ВКЛ. {figi[-6:]} pnl>=комиссия×{_ti_cfg}")
                        except Exception as _tie:
                            self._log(f"инфо-трейл activate error {figi[-6:]}: {_tie}")
                    # Этап движения стопа (что было бы) + проверка срабатывания.
                    if _ti.get("active") and _tupd is not None:
                        try:
                            _prev = _ti.get("act_track")
                            # Срабатывание — по стопу, действовавшему НА НАЧАЛО бара
                            # (и не проверяем на баре рождения стопа): стоп, созданный
                            # в этом баре, не мог быть «пробит гэпом» его же открытия
                            # (баг YDEX 11.09.2026: open 3806 ≥ стоп 3782.97 → −0₽).
                            if (_prev is not None and not _born_this_bar
                                    and not _ti.get("hit_ts")):
                                _ti_price, _ti_reason = _ibe(c, state, float(_prev), None, close_based=True)
                                if _ti_price is not None:
                                    _ti["hit_ts"] = c.ts.isoformat()
                                    _ti["hit_price"] = float(_ti_price)
                                    _ti["hit_reason"] = _ti_reason or "TRAIL"
                                    self._log(f"ИНФО-ТРЕЙЛ СРАБОТАЛ БЫ {figi[-6:]} цена={_ti_price:.2f} "
                                              f"({_ti_reason}) стоп={_prev:.2f} текущий_стоп={stop if stop is not None else '-'}")
                            _pk_i = None
                            try:
                                _pk_i = float((self._peak_pnl.get(figi) or {}).get("price")) or None
                            except Exception:
                                _pk_i = None
                            _new_stop = _tupd(trail_side, float(entry_px), _prev, _act_bars,
                                              qty=qty_sh, commission=comm, peak_price=_pk_i)
                            if _new_stop is not None:
                                _ti["act_track"] = float(_new_stop)
                                # Дистанция трейл-стопа в ATR и % для отчёта.
                                _tpol2_dist = None
                                try:
                                    _risk_now = float(_tpol._risk(float(entry_px), _act_bars))
                                    _atr_now_val = (_risk_now / _tpol.multiplier) if _tpol.multiplier else None
                                    if _atr_now_val:
                                        _ti["trail_dist_atr"] = round(
                                            (abs(float(c.close) - float(_new_stop)) / float(_atr_now_val)), 2)
                                except Exception:
                                    pass
                                # Сработал бы трейл? Только закрытие за стопом (close_based).
                                _ti_price, _ti_reason = _ibe(c, state, float(_new_stop), None, close_based=True)
                                if _ti_price is not None and not _ti.get("hit_ts"):
                                    _ti["hit_ts"] = c.ts.isoformat()
                                    _ti["hit_price"] = float(_ti_price)
                                    _ti["hit_reason"] = _ti_reason or "TRAIL"
                                    self._log(f"ИНФО-ТРЕЙЛ СРАБОТАЛ БЫ {figi[-6:]} цена={_ti_price:.2f} "
                                              f"({_ti_reason}) стоп={_new_stop:.2f} текущий_стоп={stop if stop is not None else '-'}")
                        except Exception as _twe:
                            self._log(f"инфо-трейл update error {figi[-6:]}: {_twe}")
        except Exception as _tge:
            self._log(f"инфо-трейл общий error {figi[-6:]}: {_tge}")

        price, reason = _ibe(c, state, stop, tp, close_based=bool(trail_active))
        if price is None:
            return False
        # Выход — строго по цене intrabar_exit (стоп/гэп/close/target), БЕЗ подмены
        # на «лучшую» цену бара. Прежнее правило («выход по лучшей») превращало
        # реальные стоп-убытки в прибыли, если бар задевал и хороший экстремум:
        # YDEX 11.09.2026 — SHORT-стоп 3843.29 дал бы −74.6₽, а был записан выход
        # по low бара 3758.75 → +86.20₽ (владелец заметил «SL с плюсом»).
        _exit_best = False
        # Захватываем флаг трейлинга ДО _clear_exit_state (иначе диагностика врёт).
        _was_trail = bool(trail_active or self._trail_active.get(figi, False))
        # Проскальзывание на выходе (adverse), как в бэктесте (fill_price).
        try:
            _cm = CostModel(commission_rate=self.config.commission_rate,
                            slippage_bps=self.config.slippage_bps)
            _opp = Side.SELL if state == PositionState.LONG else Side.BUY
            price = _cm.fill_price(float(price), _opp)
        except Exception as _sw_e:
            _audit_swallow('_step_exit@L4946', _sw_e)  # audit silent-except
            pass

        trade = await self.broker.close_position(figi, price, reason)
        self._held.discard(figi)
        # Пик P&L снимаем ДО очистки состояния (иначе потеряем экстремумы сделки).
        _peak_snap = dict(self._peak_pnl.pop(figi, None) or {})
        self._clear_exit_state(figi)
        _bh = self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter)
        await self._st_close(figi, price, reason=reason,
                              net=float(trade.net_pnl) if trade else None,
                              meta={"exit_reason": reason, "exit_price": float(price),
                                    "exit_best": _exit_best,
                                    "sl": stop, "tp": tp,
                                    "trailing": _was_trail,
                                    "bars_held": _bh,
                                    "peak": _peak_snap})
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
