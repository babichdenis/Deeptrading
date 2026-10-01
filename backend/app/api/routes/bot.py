import logging
import os
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.runtime import (BotConfig, BOT_PERSIST_FIELDS, load_bot_settings, runtime,
                             save_bot_settings, load_ensemble_config, save_ensemble_config,
                             default_ensemble_config)

logger = logging.getLogger("bot_api")


async def _cfg_from_saved(mode: str = "sandbox", test_name: str = "", replay_start: str = "",
                          replay_end: str = "", replay_pace: str = "fast",
                          replay_log_persist: bool = False) -> BotConfig:
    """Конфиг = хардкод-база режима + сохранённые настройки пользователя (PATCH/UI).

    Режим/фид/реплей/имя теста берём из аргументов, чтобы сохранёнки их не перетёрли.
    """
    cfg = _build_autostart_cfg(mode, test_name=test_name, replay_start=replay_start,
                               replay_end=replay_end, replay_pace=replay_pace,
                                   replay_log_persist=replay_log_persist)
    saved = await load_bot_settings()
    for f in BOT_PERSIST_FIELDS:
        if f in saved:
            try:
                setattr(cfg, f, saved[f])
            except Exception:
                pass
    cfg.mode = mode
    cfg.feed = "replay" if mode == "test" else "stream"
    if mode == "test":
        cfg.test_name = test_name
        cfg.replay_start = replay_start
        cfg.replay_end = replay_end
        cfg.replay_pace = replay_pace
        cfg.replay_log_persist = replay_log_persist
        # Вариант теста (ensemble_config.test.<variant>.json): сетапы/выходы —
        # из файл-варианта; оверрайды BotConfig (секция "bot") применяет
        # runtime.start() — единственное место, последними по приоритету.
        if not str(getattr(cfg, "test_variant", "") or ""):
            try:
                from app.config import get_settings as _gs
                cfg.test_variant = _gs().bot_test_variant or ""
            except Exception:
                import os as _os
                cfg.test_variant = _os.environ.get("TEST_VARIANT", "") or ""
    return cfg


def _config_payload(cfg: BotConfig) -> dict:
    return {
        "ok": True,
        "sessions": list(cfg.sessions),
        "long_allowed": cfg.long_allowed,
        "short_allowed": cfg.short_allowed,
        "leverage": cfg.leverage,
        "margin_sessions": list(cfg.margin_sessions),
        "margin_leverage": float(cfg.margin_leverage or 0.0),
        "margin_sizing": cfg.margin_sizing,
        "trade_regimes": list(cfg.trade_regimes),
        "trend_alignment": bool(cfg.trend_alignment),
        "stop_pct": cfg.stop_pct,
        "target_pct": cfg.target_pct,
        "sl_mode": cfg.sl_mode,
        "atr_period": cfg.atr_period,
        "atr_multiplier": cfg.atr_multiplier,
        "atr_risk_reward": cfg.atr_risk_reward,
        "top_n": cfg.top_n,
        "ensemble_quorum": cfg.ensemble_quorum,
        "pos_pct": float(getattr(cfg, "pos_pct", 0.4) or 0.4),
        "max_positions": int(getattr(cfg, "max_positions", 0) or 0),
        "reconcile_enabled": bool(getattr(cfg, "reconcile_enabled", True)),
        "max_exposure_pct": float(getattr(cfg, "max_exposure_pct", 1.0) or 0.0),
        "max_short_share": float(getattr(cfg, "max_short_share", 0.7) or 0.0),
        "max_net_exposure_pct": float(getattr(cfg, "max_net_exposure_pct", 0.5) or 0.0),
        "max_sector_pct": float(getattr(cfg, "max_sector_pct", 0.35) or 0.0),
        "max_sector_positions": int(getattr(cfg, "max_sector_positions", 0) or 0),
        "max_margin_use_pct": float(getattr(cfg, "max_margin_use_pct", 0.8) or 0.0),
        "max_stress_loss_pct": float(getattr(cfg, "max_stress_loss_pct", 0.1) or 0.0),
        "queue_enabled": bool(getattr(cfg, "queue_enabled", True)),
        "queue_ttl_min": int(getattr(cfg, "queue_ttl_min", 30) or 30),
        "queue_interval_sec": int(getattr(cfg, "queue_interval_sec", 120) or 120),
        "top_boost": float(getattr(cfg, "top_boost", 2.0) or 2.0),
        "queue_min_turnover": float(getattr(cfg, "queue_min_turnover", 300000) or 0.0),
        "queue_adv_multiple": float(getattr(cfg, "queue_adv_multiple", 200.0) or 0.0),
        "eod_close_min_before": int(getattr(cfg, "eod_close_min_before", 10) or 10),
        "bias_exit_enabled": bool(getattr(cfg, "bias_exit_enabled", False)),
        "ensemble_require_member": str(getattr(cfg, "ensemble_require_member", "") or ""),
        "momentum_short": bool(getattr(cfg, "momentum_short", False)),
        "momentum_n": int(getattr(cfg, "momentum_n", 63) or 63),
        "momentum_k": int(getattr(cfg, "momentum_k", 3) or 3),
        "momentum_stop_pct": float(getattr(cfg, "momentum_stop_pct", 0.03) or 0.03),
        "momentum_entry_time": str(getattr(cfg, "momentum_entry_time", "10:30") or "10:30"),
        "momentum_max_lev": float(getattr(cfg, "momentum_max_lev", 2.0) or 2.0),
        "momentum_only": bool(getattr(cfg, "momentum_only", False)),
        "momentum_side": str(getattr(cfg, "momentum_side", "short") or "short"),
        "mtf_align": bool(getattr(cfg, "mtf_align", False)),
        "mtf_trigger": bool(getattr(cfg, "mtf_trigger", False)),
        "entry_h1_align": bool(getattr(cfg, "entry_h1_align", True)),
        "entry_tf_conflict": bool(getattr(cfg, "entry_tf_conflict", True)),
        "entry_last_hour_block": bool(getattr(cfg, "entry_last_hour_block", True)),
        "entry_ob_imbalance_max": float(getattr(cfg, "entry_ob_imbalance_max", 0.3) or 0.0),
        "entry_ob_spread_max": float(getattr(cfg, "entry_ob_spread_max", 25.0) or 0.0),
        "entry_min_turnover": float(getattr(cfg, "entry_min_turnover", 0.0) or 0.0),
        "entry_volatility_max_mult": float(getattr(cfg, "entry_volatility_max_mult", 3.0) or 0.0),
        "entry_news_blackout": bool(getattr(cfg, "entry_news_blackout", True)),
        "entry_news_blackout_min": int(getattr(cfg, "entry_news_blackout_min", 60) or 0),
        "daily_bias": bool(getattr(cfg, "daily_bias", True)),
        "daily_bias_mode": str(getattr(cfg, "daily_bias_mode", "veto")),
        "top_sizing": str(getattr(cfg, "top_sizing", "multiply")),
        "top_relax_caps": bool(getattr(cfg, "top_relax_caps", True)),
        "rank_enabled": bool(getattr(cfg, "rank_enabled", True)),
        "rank_top_n": int(getattr(cfg, "rank_top_n", 10) or 0),
        "rank_explore": int(getattr(cfg, "rank_explore", 10) or 0),
        "rank_min_hist": int(getattr(cfg, "rank_min_hist", 5) or 0),
        "queue_history_veto": bool(getattr(cfg, "queue_history_veto", True)),
        "dd_reduce1_pct": float(getattr(cfg, "dd_reduce1_pct", 0.05) or 0.05),
        "dd_reduce2_pct": float(getattr(cfg, "dd_reduce2_pct", 0.10) or 0.10),
        "commission_rate": cfg.commission_rate,
        "overnight": cfg.overnight,
        "reentry_cooldown_bars": cfg.reentry_cooldown_bars,
        "confirm_flip": cfg.confirm_flip,
        "invert_signals": bool(getattr(cfg, "invert_signals", False)),
        "ensemble_entry_tf": getattr(cfg, "ensemble_entry_tf", "5min"),
        "ensemble_entry_from_setups": bool(getattr(cfg, "ensemble_entry_from_setups", True)),
        "ensemble_direction_sid": getattr(cfg, "ensemble_direction_sid", ""),
        "entry_confirm_closes": int(getattr(cfg, "entry_confirm_closes", 0) or 0),
        "entry_confirm_closes_sides": list(getattr(cfg, "entry_confirm_closes_sides", []) or []),
        "loss_streak_hold": bool(getattr(cfg, "loss_streak_hold", True)),
        "loss_streak_n": int(getattr(cfg, "loss_streak_n", 2) or 2),
        "loss_streak_hold_min": float(getattr(cfg, "loss_streak_hold_min", 60.0) or 60.0),
        "loss_streak_scope": str(getattr(cfg, "loss_streak_scope", "ticker") or "ticker"),
        "ai_approval": bool(getattr(cfg, "ai_approval", False)),
        "ai_approval_timeout_sec": float(getattr(cfg, "ai_approval_timeout_sec", 45.0) or 45.0),
        "ai_approval_default": str(getattr(cfg, "ai_approval_default", "approve") or "approve"),
        "ai_reject_cooldown_min": float(getattr(cfg, "ai_reject_cooldown_min", 15.0) or 0.0),
        "imoex_guard": bool(getattr(cfg, "imoex_guard", True)),
        "imoex_chase_block_pct": float(getattr(cfg, "imoex_chase_block_pct", 1.5) or 1.5),
        "source": "file",
    }

_sandbox_broker = None
_sandbox_broker_mode = ""

def _get_sandbox_broker():
    """Брокер активного контура (sandbox|live). Пересоздаётся при смене контура,
    иначе после переключения sandbox↔live заявки уходили бы на старый счёт."""
    global _sandbox_broker, _sandbox_broker_mode
    from app.config import get_settings
    _mode = get_settings().bot_mode if get_settings().bot_mode in ("sandbox", "live") else "sandbox"
    if _sandbox_broker is None or _sandbox_broker_mode != _mode:
        from app.bot.live_broker import LiveBroker
        _sandbox_broker = LiveBroker(SessionLocal, mode=_mode)
        _sandbox_broker_mode = _mode
    return _sandbox_broker
from app.database import get_db, SessionLocal
from app.engine.strategies import ParamValidationError, build_strategy
from app.models.paper import PaperAccount, PaperPosition, PaperTrade

router = APIRouter(prefix="/api/v1/bot", tags=["bot"])


class StartRequest(BaseModel):
    strategy_id: str = "rsi_reversal"
    params: dict = Field(default_factory=dict)
    interval_name: str = "5min"
    top_n: int = 6
    qty_per_trade: int = 1
    stop_pct: float = 0.01
    target_pct: float = 0.02
    sl_mode: str = "atr"
    atr_period: int = 14
    atr_multiplier: float = 4.0
    atr_risk_reward: float = 4.0
    allow_short: bool = False
    long_allowed: bool = True
    short_allowed: bool = False
    initial_cash: float = 10_000.0
    daily_loss_limit: float = Field(1000.0, ge=0)
    mode: str = "paper"  # paper | sandbox | live
    use_ensemble: bool = False
    ensemble_capital: float = 2000.0
    ensemble_quorum: int = 2
    ensemble_session: str = "main"
    sessions: list[str] = Field(default_factory=lambda: ["day"])
    leverage: float = 1.0
    commission_rate: float = 0.3  # percent per trade
    slippage_bps: float = 2.0
    confirm_flip: int = 2
    reentry_cooldown_bars: int = 15
    use_margin: bool = True
    margin_sessions: list[str] = Field(default_factory=lambda: ["day"])  # сессии с маржой
    margin_leverage: float = 0.0  # потолок плеча: 0 = Max
    margin_sizing: str = "divide"  # divide | multiply
    trade_regimes: list[str] = Field(default_factory=lambda: ["NEUTRAL", "TREND_UP", "TREND_DOWN", "HIGH_VOLATILITY", "RANGE"])
    trend_alignment: bool = True
    max_margin_pct: float = 80.0
    overnight: bool = False
    feed: str = "live"        # live | replay (источник свечей; реплей = БД)
    replay_start: str = ""    # ISO UTC datetime начала окна реплея
    replay_end: str = ""      # ISO UTC datetime конца окна (пусто = до конца данных)
    replay_pace: str = "fast" # fast | wall
    replay_log_persist: bool = False  # тест: писать логи реплея в bot_logs


@router.post("/start")
async def bot_start(req: StartRequest) -> dict:
    if req.interval_name not in ("1min", "5min", "10min", "15min"):
        raise HTTPException(400, "бот работает на интрадей-таймфреймах 1min/5min/10min/15min")
    if not req.use_ensemble:
        try:
            build_strategy(req.strategy_id, req.params)
        except ParamValidationError as e:
            raise HTTPException(400, str(e))
    cfg = BotConfig(
        strategy_id=req.strategy_id,
        params=req.params,
        interval_name=req.interval_name,
        top_n=max(2, min(req.top_n, 50)),
        qty_per_trade=max(1, req.qty_per_trade),
        stop_pct=req.stop_pct,
        target_pct=req.target_pct,
        sl_mode=req.sl_mode,
        atr_period=req.atr_period,
        atr_multiplier=req.atr_multiplier,
        atr_risk_reward=req.atr_risk_reward,
        allow_short=req.allow_short,
        long_allowed=req.long_allowed,
        short_allowed=req.short_allowed,
        initial_cash=req.initial_cash,
        daily_loss_limit=req.daily_loss_limit,
        mode=req.mode if req.mode in ("paper", "sandbox", "live") else "paper",
        use_ensemble=req.use_ensemble,
        ensemble_capital=req.ensemble_capital,
        ensemble_quorum=max(1, req.ensemble_quorum),
        ensemble_session=req.ensemble_session if req.ensemble_session in ("main", "all") else "main",
        sessions=[s for s in req.sessions if s in ("morning", "day", "evening")] or ["day"],
        leverage=max(1.0, float(req.leverage)),
        commission_rate=req.commission_rate / 100.0,  # convert % to decimal
        slippage_bps=req.slippage_bps,
        confirm_flip=req.confirm_flip,
        reentry_cooldown_bars=req.reentry_cooldown_bars,
        overnight=req.overnight,
        margin_sessions=[s for s in req.margin_sessions if s in ("morning", "day", "evening")],
        margin_leverage=max(0.0, float(req.margin_leverage)),
        margin_sizing=req.margin_sizing if req.margin_sizing in ("divide", "multiply") else "divide",
        trade_regimes=[r for r in req.trade_regimes if r in ("NEUTRAL", "TREND_UP", "TREND_DOWN", "HIGH_VOLATILITY", "RANGE")],
        trend_alignment=bool(req.trend_alignment),
        feed=req.feed if req.feed in ("live", "replay") else "live",
        replay_start=req.replay_start,
        replay_end=req.replay_end,
        replay_pace=req.replay_pace if req.replay_pace in ("fast", "wall") else "fast",
        replay_log_persist=bool(req.replay_log_persist),
    )
    try:
        result = await runtime.start(cfg)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"status": "STARTING", **result}


class BotConfigPatch(BaseModel):
    sessions: list[str] | None = None
    long_allowed: bool | None = None
    short_allowed: bool | None = None
    leverage: float | None = None
    margin_sessions: list[str] | None = None
    margin_leverage: float | None = None
    margin_sizing: str | None = None
    trade_regimes: list[str] | None = None
    trend_alignment: bool | None = None
    stop_pct: float | None = None
    target_pct: float | None = None
    sl_mode: str | None = None
    atr_period: int | None = None
    atr_multiplier: float | None = None
    atr_risk_reward: float | None = None
    top_n: int | None = None
    ensemble_quorum: int | None = None
    pos_pct: float | None = None  # доля equity на позицию (0.4 = 40%)
    max_positions: int | None = None  # максимум одновременных позиций (0 = без лимита)
    reconcile_enabled: bool | None = None  # сверка с брокером (только в торговое время)
    max_exposure_pct: float | None = None  # свои деньги в позициях <= X от equity (1.0 = 100%)
    max_short_share: float | None = None  # макс. доля SHORT среди позиций (0.7 = 70%)
    max_net_exposure_pct: float | None = None  # |net| <= X equity
    max_sector_pct: float | None = None        # сектор <= X equity
    max_sector_positions: int | None = None    # макс. позиций в одном секторе (0=выкл)
    max_margin_use_pct: float | None = None    # starting_margin <= X equity
    max_stress_loss_pct: float | None = None   # убыток при ±5% IMOEX <= X equity
    queue_enabled: bool | None = None          # очередь кандидатов (топ-1 входит с бустом)
    queue_ttl_min: int | None = None           # время жизни кандидата (мин)
    queue_interval_sec: int | None = None      # период проверки очереди (сек)
    top_boost: float | None = None             # множитель слота топ-1 (2.0 = 80% equity)
    queue_min_turnover: float | None = None    # мин. оборот ₽/день (вето illiquid)
    queue_adv_multiple: float | None = None    # слот ≤ 1/N дневного оборота
    rank_enabled: bool | None = None           # ранжирование тикеров (разведка → топ-N)
    eod_close_min_before: int | None = None    # за N минут до конца сессии закрывать (overnight=False)
    bias_exit_enabled: bool | None = None      # закрывать позицию при смене знака bias_eff против позиции
    daily_bias: bool | None = None             # дневной MACD-bias (veto входов против направления)
    mtf_align: bool | None = None              # H1 MACD подтверждает дневной bias
    ensemble_require_member: str | None = None  # обязательный голос кворума (macd_cross)
    momentum_short: bool | None = None          # режим «моментум-шорт»
    momentum_n: int | None = None               # окно моментума (дней)
    momentum_k: int | None = None               # сколько имён шортить
    momentum_stop_pct: float | None = None      # стоп (доля)
    momentum_entry_time: str | None = None      # время входа МСК "10:30"
    momentum_max_lev: float | None = None       # кап плеча
    momentum_only: bool | None = None           # только моментум (сигналы ансамбля игнор.)
    momentum_side: str | None = None            # short | long | both
    mtf_trigger: bool | None = None            # M5 MACD триггер разворота
    entry_h1_align: bool | None = None         # H1 MACD подтверждает сторону входа (правило 7)
    entry_tf_conflict: bool | None = None      # daily bias и H1 не противоречат (правило 6)
    entry_last_hour_block: bool | None = None  # не входить в последний час сессии (правило 16)
    entry_ob_imbalance_max: float | None = None    # стакан: блок против потока (0=выкл)
    entry_ob_spread_max: float | None = None       # стакан: блок при широком спреде (0=выкл)
    entry_min_turnover: float | None = None        # мин. дневной оборот, ₽ (0=выкл)
    entry_volatility_max_mult: float | None = None  # ATR% > X× медианы → блок (0=выкл)
    entry_news_blackout: bool | None = None        # блок по свежей негативной новости
    entry_news_blackout_min: int | None = None     # окно новостей для стоп-блока, мин
    daily_bias_mode: str | None = None         # veto | info
    top_sizing: str | None = None              # режим размера топ-1 (divide|multiply)
    top_relax_caps: bool | None = None         # топ-1: сектор off, net до 100%
    rank_top_n: int | None = None              # сколько тикеров торгуем
    rank_explore: int | None = None            # пробных сделок каждому тикеру
    rank_min_hist: int | None = None           # мин. история для рейтинга
    queue_history_veto: bool | None = None     # вето на токсичную историю
    dd_reduce1_pct: float | None = None        # просадка → закрыть 50%
    dd_reduce2_pct: float | None = None        # просадка → закрыть 80%
    commission_rate: float | None = None
    overnight: bool | None = None
    reentry_cooldown_bars: int | None = None
    confirm_flip: int | None = None
    invert_signals: bool | None = None
    ensemble_entry_tf: str | None = None
    ensemble_entry_from_setups: bool | None = None
    ensemble_direction_sid: str | None = None
    entry_confirm_closes: int | None = None
    entry_confirm_closes_sides: list[str] | None = None
    # HOLD после серии убытков
    loss_streak_hold: bool | None = None
    loss_streak_n: int | None = None
    loss_streak_hold_min: float | None = None
    loss_streak_scope: str | None = None
    # AI-гейт входов
    ai_approval: bool | None = None
    ai_approval_timeout_sec: float | None = None
    ai_approval_default: str | None = None
    ai_reject_cooldown_min: float | None = None
    # IMOEX guard
    imoex_guard: bool | None = None
    imoex_chase_block_pct: float | None = None


@router.patch("/config")
async def bot_config_patch(req: BotConfigPatch) -> dict:
    """Применить настройки. Работает и при остановленном боте — значения пишутся в config-файл."""
    cfg = runtime.config if (runtime.running or runtime.starting) else await _cfg_from_saved()
    changes: list[str] = []
    valid_sessions = {"morning", "day", "evening"}
    sess_names = {"morning": "утро", "day": "день", "evening": "вечер"}
    if req.sessions is not None:
        new_s = [s for s in req.sessions if s in valid_sessions] or ["day"]
        if new_s != cfg.sessions:
            changes.append(f"сессия: {'/'.join(sess_names.get(s, s) for s in cfg.sessions)} → {'/'.join(sess_names.get(s, s) for s in new_s)}")
        cfg.sessions = new_s
    if req.long_allowed is not None and req.long_allowed != cfg.long_allowed:
        changes.append(f"long: {'вкл' if cfg.long_allowed else 'выкл'} → {'вкл' if req.long_allowed else 'выкл'}")
        cfg.long_allowed = req.long_allowed
    if req.short_allowed is not None and req.short_allowed != cfg.short_allowed:
        changes.append(f"short: {'вкл' if cfg.short_allowed else 'выкл'} → {'вкл' if req.short_allowed else 'выкл'}")
        cfg.short_allowed = req.short_allowed
    if req.leverage is not None and req.leverage != cfg.leverage:
        changes.append(f"плечо: ×{cfg.leverage} → ×{req.leverage}")
        cfg.leverage = req.leverage
    if req.margin_sessions is not None:
        new_ms = [s for s in req.margin_sessions if s in valid_sessions]
        if new_ms != list(cfg.margin_sessions):
            _old = '/'.join(sess_names.get(s, s) for s in cfg.margin_sessions) or '—'
            _new = '/'.join(sess_names.get(s, s) for s in new_ms) or '—'
            changes.append(f"маржа: {_old} → {_new}")
        cfg.margin_sessions = new_ms
    if req.margin_leverage is not None:
        new_ml = max(0.0, float(req.margin_leverage))
        if new_ml != float(cfg.margin_leverage or 0.0):
            _old = 'Max' if (cfg.margin_leverage or 0) <= 0 else f"×{cfg.margin_leverage:g}"
            _new = 'Max' if new_ml <= 0 else f"×{new_ml:g}"
            changes.append(f"плечо маржи: {_old} → {_new}")
        cfg.margin_leverage = new_ml
    if req.margin_sizing is not None and req.margin_sizing in ("divide", "multiply"):
        if req.margin_sizing != cfg.margin_sizing:
            changes.append(f"размер позиции: {cfg.margin_sizing} → {req.margin_sizing}")
        cfg.margin_sizing = req.margin_sizing
    if req.trade_regimes is not None:
        _valid_reg = {"NEUTRAL", "TREND_UP", "TREND_DOWN", "HIGH_VOLATILITY", "RANGE"}
        new_tr = [r for r in req.trade_regimes if r in _valid_reg]
        if new_tr != list(cfg.trade_regimes):
            _old = '/'.join(cfg.trade_regimes) or '—'
            _new = '/'.join(new_tr) or '—'
            changes.append(f"режимы: {_old} → {_new}")
        cfg.trade_regimes = new_tr
    if req.trend_alignment is not None and req.trend_alignment != cfg.trend_alignment:
        changes.append(f"trend-alignment: {'вкл' if cfg.trend_alignment else 'выкл'} → {'вкл' if req.trend_alignment else 'выкл'}")
        cfg.trend_alignment = req.trend_alignment
    if req.stop_pct is not None:
        new_sl = max(0.001, req.stop_pct)
        if new_sl != cfg.stop_pct:
            changes.append(f"SL: {cfg.stop_pct*100:.1f}% → {new_sl*100:.1f}%")
        cfg.stop_pct = new_sl
    if req.target_pct is not None:
        new_tp = max(0.001, req.target_pct)
        if new_tp != cfg.target_pct:
            changes.append(f"TP: {cfg.target_pct*100:.1f}% → {new_tp*100:.1f}%")
        cfg.target_pct = new_tp
    if req.sl_mode is not None and req.sl_mode in ("atr", "fixed"):
        if req.sl_mode != cfg.sl_mode:
            changes.append(f"SL mode: {cfg.sl_mode} → {req.sl_mode}")
        cfg.sl_mode = req.sl_mode
    if req.atr_period is not None:
        cfg.atr_period = max(5, req.atr_period)
    if req.atr_multiplier is not None:
        cfg.atr_multiplier = max(0.5, req.atr_multiplier)
    if req.atr_risk_reward is not None:
        cfg.atr_risk_reward = max(0.5, req.atr_risk_reward)
    if req.top_n is not None:
        new_tn = max(1, req.top_n)
        if new_tn != cfg.top_n:
            changes.append(f"Top-N: {cfg.top_n} → {new_tn}")
        cfg.top_n = new_tn
    if req.ensemble_quorum is not None:
        new_q = max(1, req.ensemble_quorum)
        if new_q != cfg.ensemble_quorum:
            changes.append(f"quorum: {cfg.ensemble_quorum} → {new_q}")
        cfg.ensemble_quorum = new_q
    if req.pos_pct is not None:
        _pp = max(0.05, min(1.0, float(req.pos_pct)))
        if abs(_pp - float(getattr(cfg, "pos_pct", 0.4) or 0.4)) > 1e-9:
            changes.append(f"слот: {float(getattr(cfg, 'pos_pct', 0.4) or 0.4)*100:.0f}% → {_pp*100:.0f}% от EQ")
        cfg.pos_pct = _pp
    if req.max_positions is not None:
        _mp = max(0, min(50, int(req.max_positions)))
        if _mp != int(getattr(cfg, "max_positions", 0) or 0):
            changes.append(f"макс. позиций: {getattr(cfg, 'max_positions', 0)} → {_mp}")
        cfg.max_positions = _mp
    if req.reconcile_enabled is not None:
        _rec = bool(req.reconcile_enabled)
        if _rec != bool(getattr(cfg, "reconcile_enabled", True)):
            changes.append(f"сверка с брокером: {'вкл' if getattr(cfg, 'reconcile_enabled', True) else 'выкл'} → {'вкл' if _rec else 'выкл'}")
        cfg.reconcile_enabled = _rec
    if req.max_exposure_pct is not None:
        _me = max(0.0, min(5.0, float(req.max_exposure_pct)))
        if abs(_me - float(getattr(cfg, "max_exposure_pct", 1.0) or 0.0)) > 1e-9:
            changes.append(f"кап экспозиции: {float(getattr(cfg, 'max_exposure_pct', 1.0) or 0.0)*100:.0f}% → {_me*100:.0f}% от equity")
        cfg.max_exposure_pct = _me
    if req.max_net_exposure_pct is not None:
        _mn = max(0.0, min(5.0, float(req.max_net_exposure_pct)))
        if abs(_mn - float(getattr(cfg, "max_net_exposure_pct", 0.5) or 0.0)) > 1e-9:
            changes.append(f"net-экспозиция: {float(getattr(cfg, 'max_net_exposure_pct', 0.5) or 0.0)*100:.0f}% → {_mn*100:.0f}% equity")
        cfg.max_net_exposure_pct = _mn
    if req.max_sector_pct is not None:
        _msc = max(0.0, min(2.0, float(req.max_sector_pct)))
        if abs(_msc - float(getattr(cfg, "max_sector_pct", 0.35) or 0.0)) > 1e-9:
            changes.append(f"сектор: {float(getattr(cfg, 'max_sector_pct', 0.35) or 0.0)*100:.0f}% → {_msc*100:.0f}% equity")
        cfg.max_sector_pct = _msc
    if req.max_sector_positions is not None:
        _msp = max(0, int(req.max_sector_positions))
        if _msp != int(getattr(cfg, "max_sector_positions", 0) or 0):
            changes.append(f"кластер сектора: {int(getattr(cfg, 'max_sector_positions', 0) or 0)} → {_msp} позиций")
        cfg.max_sector_positions = _msp
    if req.max_margin_use_pct is not None:
        _mmu = max(0.0, min(1.0, float(req.max_margin_use_pct)))
        if abs(_mmu - float(getattr(cfg, "max_margin_use_pct", 0.8) or 0.0)) > 1e-9:
            changes.append(f"маржа: {float(getattr(cfg, 'max_margin_use_pct', 0.8) or 0.0)*100:.0f}% → {_mmu*100:.0f}% equity")
        cfg.max_margin_use_pct = _mmu
    if req.queue_enabled is not None:
        cfg.queue_enabled = bool(req.queue_enabled)
        changes.append(f"очередь кандидатов: {'вкл' if cfg.queue_enabled else 'выкл'}")
    if req.queue_ttl_min is not None:
        cfg.queue_ttl_min = max(1, min(240, int(req.queue_ttl_min)))
        changes.append(f"TTL кандидата: {cfg.queue_ttl_min} мин")
    if req.queue_interval_sec is not None:
        cfg.queue_interval_sec = max(30, min(900, int(req.queue_interval_sec)))
        changes.append(f"период очереди: {cfg.queue_interval_sec}с")
    if req.queue_min_turnover is not None:
        cfg.queue_min_turnover = max(0.0, min(100e6, float(req.queue_min_turnover)))
        changes.append(f"мин. оборот: {cfg.queue_min_turnover/1e6:.2f}M ₽/день")
    if req.momentum_side is not None and req.momentum_side in ("short", "long", "both"):
        cfg.momentum_side = req.momentum_side
        changes.append(f"моментум-направление: {cfg.momentum_side}")
    if req.momentum_only is not None:
        cfg.momentum_only = bool(req.momentum_only)
        changes.append(f"только моментум: {'вкл' if cfg.momentum_only else 'выкл'}")
    if req.momentum_short is not None:
        cfg.momentum_short = bool(req.momentum_short)
        changes.append(f"моментум-шорт: {'вкл' if cfg.momentum_short else 'выкл'}")
    if req.momentum_n is not None:
        cfg.momentum_n = max(5, min(250, int(req.momentum_n)))
        changes.append(f"окно моментума: {cfg.momentum_n} дн.")
    if req.momentum_k is not None:
        cfg.momentum_k = max(1, min(10, int(req.momentum_k)))
        changes.append(f"моментум-имён: {cfg.momentum_k}")
    if req.momentum_stop_pct is not None:
        cfg.momentum_stop_pct = max(0.005, min(0.2, float(req.momentum_stop_pct)))
        changes.append(f"моментум-стоп: {cfg.momentum_stop_pct*100:.1f}%")
    if req.momentum_entry_time is not None:
        cfg.momentum_entry_time = str(req.momentum_entry_time or "10:30").strip()
        changes.append(f"время входа: {cfg.momentum_entry_time} МСК")
    if req.momentum_max_lev is not None:
        cfg.momentum_max_lev = max(1.0, min(5.0, float(req.momentum_max_lev)))
        changes.append(f"кап плеча: ×{cfg.momentum_max_lev:g}")
    if req.ensemble_require_member is not None:
        cfg.ensemble_require_member = str(req.ensemble_require_member or "").strip()
        changes.append(f"якорь кворума: {cfg.ensemble_require_member or '—'}")
    if req.mtf_align is not None:
        cfg.mtf_align = bool(req.mtf_align)
        changes.append(f"MTF H1-подтверждение: {'вкл' if cfg.mtf_align else 'выкл'}")
    if req.mtf_trigger is not None:
        cfg.mtf_trigger = bool(req.mtf_trigger)
        changes.append(f"MTF M5-триггер: {'вкл' if cfg.mtf_trigger else 'выкл'}")
    if req.entry_h1_align is not None:
        cfg.entry_h1_align = bool(req.entry_h1_align)
        changes.append(f"H1-подтверждение входа: {'вкл' if cfg.entry_h1_align else 'выкл'}")
    if req.entry_tf_conflict is not None:
        cfg.entry_tf_conflict = bool(req.entry_tf_conflict)
        changes.append(f"TF-конфликт daily/H1: {'вкл' if cfg.entry_tf_conflict else 'выкл'}")
    if req.entry_last_hour_block is not None:
        cfg.entry_last_hour_block = bool(req.entry_last_hour_block)
        changes.append(f"Блок последнего часа: {'вкл' if cfg.entry_last_hour_block else 'выкл'}")
    if req.entry_ob_imbalance_max is not None:
        cfg.entry_ob_imbalance_max = max(0.0, float(req.entry_ob_imbalance_max))
        changes.append(f"Стакан imbalance max: {cfg.entry_ob_imbalance_max:g}")
    if req.entry_ob_spread_max is not None:
        cfg.entry_ob_spread_max = max(0.0, float(req.entry_ob_spread_max))
        changes.append(f"Стакан спред max: {cfg.entry_ob_spread_max:g} б.п.")
    if req.entry_min_turnover is not None:
        cfg.entry_min_turnover = max(0.0, float(req.entry_min_turnover))
        changes.append(f"Мин. оборот: {cfg.entry_min_turnover:,.0f}₽")
    if req.entry_volatility_max_mult is not None:
        cfg.entry_volatility_max_mult = max(0.0, float(req.entry_volatility_max_mult))
        changes.append(f"Волатильность max: {cfg.entry_volatility_max_mult:g}× медианы")
    if req.entry_news_blackout is not None:
        cfg.entry_news_blackout = bool(req.entry_news_blackout)
        changes.append(f"Новостной стоп-блок: {'вкл' if cfg.entry_news_blackout else 'выкл'}")
    if req.entry_news_blackout_min is not None:
        cfg.entry_news_blackout_min = max(0, int(req.entry_news_blackout_min))
        changes.append(f"Окно новостей: {cfg.entry_news_blackout_min} мин")
    if req.daily_bias is not None:
        cfg.daily_bias = bool(req.daily_bias)
        changes.append(f"дневной bias: {'вкл' if cfg.daily_bias else 'выкл'}")
    if req.daily_bias_mode is not None and req.daily_bias_mode in ("veto", "info"):
        cfg.daily_bias_mode = req.daily_bias_mode
        changes.append(f"дневной bias режим: {cfg.daily_bias_mode}")
    if req.eod_close_min_before is not None:
        cfg.eod_close_min_before = max(0, min(60, int(req.eod_close_min_before)))
        changes.append(f"EOD-закрытие за {cfg.eod_close_min_before} мин до конца сессии")
    if req.bias_exit_enabled is not None:
        new_be = bool(req.bias_exit_enabled)
        if new_be != bool(getattr(cfg, "bias_exit_enabled", False)):
            changes.append(f"bias-выход: {'вкл' if new_be else 'выкл'}")
        cfg.bias_exit_enabled = new_be
    if req.top_sizing is not None and req.top_sizing in ("divide", "multiply"):
        if req.top_sizing != cfg.top_sizing:
            changes.append(f"размер топ-1: {cfg.top_sizing} → {req.top_sizing}")
        cfg.top_sizing = req.top_sizing
    if req.top_relax_caps is not None:
        cfg.top_relax_caps = bool(req.top_relax_caps)
        changes.append(f"лимиты топ-1: {'ослаблены' if cfg.top_relax_caps else 'общие'}")
    if req.rank_enabled is not None:
        cfg.rank_enabled = bool(req.rank_enabled)
        changes.append(f"ранжирование: {'вкл' if cfg.rank_enabled else 'выкл'}")
    if req.rank_top_n is not None:
        cfg.rank_top_n = max(0, min(50, int(req.rank_top_n)))
        changes.append(f"торгуем топ-{cfg.rank_top_n} тикеров")
    if req.rank_explore is not None:
        cfg.rank_explore = max(0, min(100, int(req.rank_explore)))
        changes.append(f"разведка: {cfg.rank_explore} сделок/тикер")
    if req.rank_min_hist is not None:
        cfg.rank_min_hist = max(0, min(50, int(req.rank_min_hist)))
        changes.append(f"мин. история для рейтинга: {cfg.rank_min_hist}")
    if req.queue_adv_multiple is not None:
        cfg.queue_adv_multiple = max(0.0, min(10000.0, float(req.queue_adv_multiple)))
        changes.append(f"ликвидность: слот ≤ 1/{cfg.queue_adv_multiple:g} оборота")
    if req.queue_history_veto is not None:
        cfg.queue_history_veto = bool(req.queue_history_veto)
        changes.append(f"вето истории: {'вкл' if cfg.queue_history_veto else 'выкл'}")
    if req.top_boost is not None:
        cfg.top_boost = max(1.0, min(5.0, float(req.top_boost)))
        changes.append(f"буст топ-1: ×{cfg.top_boost:g} слота")
    if req.dd_reduce1_pct is not None:
        cfg.dd_reduce1_pct = max(0.01, min(0.5, float(req.dd_reduce1_pct)))
        changes.append(f"просадка-1: {cfg.dd_reduce1_pct*100:.0f}% → закрыть 50%")
    if req.dd_reduce2_pct is not None:
        cfg.dd_reduce2_pct = max(0.02, min(0.6, float(req.dd_reduce2_pct)))
        changes.append(f"просадка-2: {cfg.dd_reduce2_pct*100:.0f}% → закрыть 80%")
    if req.max_stress_loss_pct is not None:
        _mst = max(0.0, min(1.0, float(req.max_stress_loss_pct)))
        if abs(_mst - float(getattr(cfg, "max_stress_loss_pct", 0.1) or 0.0)) > 1e-9:
            changes.append(f"стресс-лимит: {float(getattr(cfg, 'max_stress_loss_pct', 0.1) or 0.0)*100:.0f}% → {_mst*100:.0f}% equity")
        cfg.max_stress_loss_pct = _mst
    if req.max_short_share is not None:
        _ms = max(0.0, min(1.0, float(req.max_short_share)))
        if abs(_ms - float(getattr(cfg, "max_short_share", 0.7) or 0.0)) > 1e-9:
            changes.append(f"лимит шортов: {float(getattr(cfg, 'max_short_share', 0.7) or 0.0)*100:.0f}% → {_ms*100:.0f}% позиций")
        cfg.max_short_share = _ms
    if req.commission_rate is not None:
        new_cr = max(0.0, req.commission_rate) / 100.0
        if new_cr != cfg.commission_rate:
            changes.append(f"commission: {cfg.commission_rate*100:.2f}% → {req.commission_rate:.2f}%")
        cfg.commission_rate = new_cr
    if req.overnight is not None and req.overnight != cfg.overnight:
        changes.append(f"overnight: {'вкл' if cfg.overnight else 'выкл'} → {'вкл' if req.overnight else 'выкл'}")
        cfg.overnight = req.overnight
    if req.reentry_cooldown_bars is not None:
        new_rc = max(0, req.reentry_cooldown_bars)
        if new_rc != cfg.reentry_cooldown_bars:
            changes.append(f"cooldown: {cfg.reentry_cooldown_bars} → {new_rc}")
        cfg.reentry_cooldown_bars = new_rc
    if req.confirm_flip is not None:
        new_cf = max(0, req.confirm_flip)
        if new_cf != cfg.confirm_flip:
            changes.append(f"confirm_flip: {cfg.confirm_flip} → {new_cf}")
        cfg.confirm_flip = new_cf
    if req.invert_signals is not None and req.invert_signals != cfg.invert_signals:
        changes.append(f"инверсия сигналов: {'вкл' if cfg.invert_signals else 'выкл'} → {'вкл' if req.invert_signals else 'выкл'}")
        cfg.invert_signals = req.invert_signals
    if req.ensemble_entry_tf is not None and req.ensemble_entry_tf in ("1min", "5min", "10min", "15min", "hour"):
        if req.ensemble_entry_tf != getattr(cfg, "ensemble_entry_tf", "5min"):
            changes.append(f"entry_tf: {getattr(cfg, 'ensemble_entry_tf', '5min')} → {req.ensemble_entry_tf}")
        cfg.ensemble_entry_tf = req.ensemble_entry_tf
    if req.ensemble_entry_from_setups is not None and req.ensemble_entry_from_setups != getattr(cfg, "ensemble_entry_from_setups", True):
        changes.append(f"entry_from_setups: {getattr(cfg, 'ensemble_entry_from_setups', True)} → {req.ensemble_entry_from_setups}")
        cfg.ensemble_entry_from_setups = req.ensemble_entry_from_setups
    if req.ensemble_direction_sid is not None:
        if req.ensemble_direction_sid != getattr(cfg, "ensemble_direction_sid", ""):
            changes.append(f"direction_sid: {getattr(cfg, 'ensemble_direction_sid', '') or '—'} → {req.ensemble_direction_sid or '—'}")
        cfg.ensemble_direction_sid = req.ensemble_direction_sid
    if req.entry_confirm_closes is not None:
        _n = max(0, min(5, int(req.entry_confirm_closes)))
        if _n != int(getattr(cfg, "entry_confirm_closes", 0) or 0):
            changes.append(f"подтверждение входа: {getattr(cfg, 'entry_confirm_closes', 0)}×1м → {_n}×1м")
        cfg.entry_confirm_closes = _n
    if req.entry_confirm_closes_sides is not None:
        _sd = [s for s in req.entry_confirm_closes_sides if s in ("BUY", "SELL")]
        if _sd != list(getattr(cfg, "entry_confirm_closes_sides", []) or []):
            changes.append(f"подтверждение сторон: {'/'.join(getattr(cfg, 'entry_confirm_closes_sides', []) or []) or '—'} → {'/'.join(_sd) or '—'}")
        cfg.entry_confirm_closes_sides = _sd
    if req.loss_streak_hold is not None and req.loss_streak_hold != getattr(cfg, "loss_streak_hold", True):
        changes.append(f"HOLD после убытков: {'вкл' if getattr(cfg, 'loss_streak_hold', True) else 'выкл'} → {'вкл' if req.loss_streak_hold else 'выкл'}")
        cfg.loss_streak_hold = req.loss_streak_hold
    if req.loss_streak_n is not None:
        _n = max(2, min(10, int(req.loss_streak_n)))
        if _n != int(getattr(cfg, "loss_streak_n", 2) or 2):
            changes.append(f"HOLD убытков подряд: {getattr(cfg, 'loss_streak_n', 2)} → {_n}")
        cfg.loss_streak_n = _n
    if req.loss_streak_hold_min is not None:
        _m = max(1.0, min(1440.0, float(req.loss_streak_hold_min)))
        if _m != float(getattr(cfg, "loss_streak_hold_min", 60.0) or 60.0):
            changes.append(f"HOLD пауза: {getattr(cfg, 'loss_streak_hold_min', 60.0)} → {_m} мин")
        cfg.loss_streak_hold_min = _m
    if req.loss_streak_scope is not None and req.loss_streak_scope in ("ticker", "global"):
        cfg.loss_streak_scope = req.loss_streak_scope
    if req.ai_approval is not None and req.ai_approval != getattr(cfg, "ai_approval", False):
        changes.append(f"AI-гейт входов: {'вкл' if getattr(cfg, 'ai_approval', False) else 'выкл'} → {'вкл' if req.ai_approval else 'выкл'}")
        cfg.ai_approval = req.ai_approval
    if req.ai_approval_timeout_sec is not None:
        new_to = max(5.0, min(600.0, float(req.ai_approval_timeout_sec)))
        if new_to != float(getattr(cfg, "ai_approval_timeout_sec", 45.0)):
            changes.append(f"AI-таймаут: {getattr(cfg, 'ai_approval_timeout_sec', 45.0)} → {new_to}")
        cfg.ai_approval_timeout_sec = new_to
    if req.ai_approval_default is not None and req.ai_approval_default in ("approve", "reject"):
        if req.ai_approval_default != getattr(cfg, "ai_approval_default", "approve"):
            changes.append(f"AI-таймаут default: {getattr(cfg, 'ai_approval_default', 'approve')} → {req.ai_approval_default}")
        cfg.ai_approval_default = req.ai_approval_default
    if req.ai_reject_cooldown_min is not None:
        _cd = max(0.0, min(240.0, float(req.ai_reject_cooldown_min)))
        if _cd != float(getattr(cfg, "ai_reject_cooldown_min", 15.0) or 15.0):
            changes.append(f"AI-пауза после отказа: {getattr(cfg, 'ai_reject_cooldown_min', 15.0)} → {_cd} мин")
        cfg.ai_reject_cooldown_min = _cd
    if req.imoex_guard is not None and req.imoex_guard != getattr(cfg, "imoex_guard", True):
        changes.append(f"IMOEX guard: {'вкл' if getattr(cfg, 'imoex_guard', True) else 'выкл'} → {'вкл' if req.imoex_guard else 'выкл'}")
        cfg.imoex_guard = req.imoex_guard
    if req.imoex_chase_block_pct is not None:
        new_cp = max(0.0, float(req.imoex_chase_block_pct))
        cfg.imoex_chase_block_pct = new_cp
    if changes:
        runtime._log("⚙ КОНФИГ: " + " | ".join(changes))
    await save_bot_settings(cfg)
    # Пороговые поля гейтов пишем ещё и в data/gates_config.json (единый конфиг гейтов).
    try:
        from app.bot.gates import save_gates_config as _sgc
        _sgc(cfg)
    except Exception:
        pass
    return _config_payload(cfg)


@router.get("/config")
async def bot_config_get() -> dict:
    """Вернуть текущие настройки бота (из файла даже когда бот не запущен)."""
    if runtime.running or runtime.starting:
        _payload = _config_payload(runtime.config)
    else:
        _payload = _config_payload(await _cfg_from_saved())
    _payload.pop("ok", None)
    return _payload


@router.get("/gates")
async def bot_gates() -> dict:
    """Единый реестр ВСЕХ гейтов входа: слой, флаг, текущее состояние, статистика отказов.

    key = reason-код из логов и skip_counts. Источник правды — app/bot/gates.py.
    """
    from app.bot.gates import gates_report
    try:
        sk = runtime.get_no_trade_stats()
    except Exception:
        sk = {}
    ec = await load_ensemble_config()
    return gates_report(runtime.config, sk, ec)


class GateToggleIn(BaseModel):
    key: str
    on: bool


@router.post("/gates/toggle")
async def bot_gates_toggle(req: GateToggleIn) -> dict:
    """Вкл/выкл гейта через UI: мутирует BotConfig (или ensemble_config) и персистит."""
    from app.bot.gates import toggle_gate, gates_report
    cfg = runtime.config if (runtime.running or runtime.starting) else await _cfg_from_saved()
    ec = await load_ensemble_config()
    res = toggle_gate(cfg, req.key, bool(req.on), ec=ec)
    if not res.get("ok"):
        return res
    if res.get("ensemble"):
        try:
            await save_ensemble_config(ec)
        except Exception:
            pass
        try:
            await runtime.reload_ensemble()
        except Exception:
            pass
    else:
        try:
            await save_bot_settings(cfg)
        except Exception:
            pass
        try:
            from app.bot.gates import save_gates_config as _sgc
            _sgc(cfg)
        except Exception:
            pass
    try:
        sk = runtime.get_no_trade_stats()
    except Exception:
        sk = {}
    return {"ok": True, "toggle": res, "gates": gates_report(cfg, sk, ec)}


@router.get("/funnel")
async def bot_funnel(figi: str = "", ticker: str = "", limit: int = 400) -> dict:
    """Воронка решений: судьба каждого сигнала — «кто что глушит».

    stage: signal → strategy → gate → order → exit (+ queue/ai).
      stats — счётчики по stage:action[:reason];
      ring  — последние записи (повторы схлопнуты в ×N) с ВИРТУАЛЬНЫМ
              временем реплея (не реальным);
      фильтры ?figi= / ?ticker= — вся история по одной бумаге: видно, кто
      и чем глушил сигналы конкретной сделки (strategy → gate → order → exit).
    """
    try:
        return runtime.get_funnel(figi=figi, ticker=ticker, limit=limit)
    except Exception:
        return {"stats": {}, "ring": [], "total": 0, "ring_size": 0}


@router.get("/ensemble")
async def bot_ensemble_get() -> dict:
    """Состав кворума (UI-управляемый конфиг движка)."""
    cfg = await load_ensemble_config()
    cfg["_all_strategies"] = [
        "rsi_reversal", "bollinger_reclaim", "vwap_reclaim", "macd_cross", "donchian_breakout",
        "pullback_ema", "range_compression_breakout", "volume_drop", "volume_climax", "stochastic",
    ]
    return cfg


@router.patch("/ensemble")
async def bot_ensemble_patch(payload: dict) -> dict:
    """Обновить состав кворума/параметры и применить к запущенному боту."""
    cfg = await load_ensemble_config()
    for key in ("quorum", "neutral_mode", "vol_thr", "bias", "entry_tf", "sl_mult", "rr", "sl_source", "sl_override", "rr_override"):
        if key in payload:
            cfg[key] = payload[key]
    if "setups" in payload and isinstance(payload["setups"], list):
        cfg["setups"] = payload["setups"]
    if "regime_setups_filter" in payload and isinstance(payload["regime_setups_filter"], dict):
        cfg["regime_setups_filter"] = payload["regime_setups_filter"]
    await save_ensemble_config(cfg)
    applied = 0
    try:
        applied = await runtime.reload_ensemble()
    except Exception:
        applied = 0
    return {"ok": True, "applied_strategies": applied, "config": cfg}


@router.post("/ensemble/reset")
async def bot_ensemble_reset() -> dict:
    """Сбросить состав кворума к дефолту."""
    cfg = default_ensemble_config()
    await save_ensemble_config(cfg)
    try:
        await runtime.reload_ensemble()
    except Exception:
        pass
    return {"ok": True, "config": cfg}


class SettingsRequest(BaseModel):
    sl_mode: str = ""
    atr_period: int = 0
    atr_mult: float = 0.0
    atr_rr: float = 0.0
    fixed_sl: float = 0.0
    fixed_tp: float = 0.0
    top_n: int = 0
    commission: float = 0.0
    reentry_cooldown: int = 0
    confirm_flip: int = 0
    quorum: int = 0
    sl_source: str = ""
    sl_override: float = 0.0
    rr_override: float = 0.0


@router.post("/settings")
async def bot_settings(body: SettingsRequest) -> dict:
    """Сохранить настройки бота.

    atr_mult/atr_rr/quorum → ensemble_config (применяются сразу к запущенным
    стратегиям через reload); остальное — на лету в runtime.config.
    """
    changed: list[str] = []

    _ec_dirty = False
    ec = await load_ensemble_config()
    if body.atr_mult > 0:
        ec["sl_mult"] = round(body.atr_mult, 2)
        changed.append("sl_mult")
        _ec_dirty = True
    if body.atr_rr > 0:
        ec["rr"] = round(body.atr_rr, 2)
        changed.append("rr")
        _ec_dirty = True
    if body.quorum > 0:
        ec["quorum"] = int(body.quorum)
        changed.append("quorum")
        _ec_dirty = True
    if body.sl_source in ("optuna", "manual"):
        ec["sl_source"] = body.sl_source
        changed.append("sl_source")
        _ec_dirty = True
    if body.sl_override > 0:
        ec["sl_override"] = round(body.sl_override, 2)
        changed.append("sl_override")
        _ec_dirty = True
    if body.rr_override > 0:
        ec["rr_override"] = round(body.rr_override, 2)
        changed.append("rr_override")
        _ec_dirty = True
    if _ec_dirty:
        await save_ensemble_config(ec)

    _map = {
        "sl_mode": body.sl_mode if body.sl_mode in ("atr", "fixed") else "",
        "atr_period": body.atr_period,
        "atr_multiplier": body.atr_mult,
        "atr_risk_reward": body.atr_rr,
        "stop_pct": body.fixed_sl / 100.0 if body.fixed_sl > 0 else 0.0,
        "target_pct": body.fixed_tp / 100.0 if body.fixed_tp > 0 else 0.0,
        "top_n": body.top_n,
        "commission_rate": body.commission / 100.0 if body.commission > 0 else 0.0,
        "reentry_cooldown_bars": body.reentry_cooldown,
        "confirm_flip": body.confirm_flip,
    }
    for _k, _v in _map.items():
        if not _v:
            continue
        try:
            setattr(runtime.config, _k, int(_v) if isinstance(_v, bool) else _v)
            changed.append(_k)
        except Exception:
            pass

    applied = 0
    if _ec_dirty:
        try:
            applied = await runtime.reload_ensemble()
        except Exception:
            applied = 0
    return {"ok": True, "applied_strategies": applied, "changed": changed}


@router.post("/stop")
async def bot_stop() -> dict:
    return await runtime.stop()


class ModeRequest(BaseModel):
    mode: str  # sandbox | live | test
    test_name: str = ""
    replay_start: str = ""  # ISO UTC (обязателен для mode=test)
    replay_end: str = ""
    replay_pace: str = "fast"
    test_engine: str = ""  # одиночный движок теста (mode=test): id из STRATEGY_REGISTRY (напр. ose_bollinger); пусто = ensemble_v4
    test_interval: str = ""  # TF теста (mode=test): 5min|10min|15min; пусто = 1min
    test_params: dict = Field(default_factory=dict)  # параметры движка теста, напр. {"quorum": 2}
    preset: dict = Field(default_factory=dict)  # «ветка»-пресет: runtime-блок применится к cfg (env TEST_PRESET)
    replay_log_persist: bool = False  # писать логи теста в bot_logs


def _write_env_mode(mode: str, test_name: str = "", replay_start: str = "", replay_end: str = "",
                    replay_pace: str = "fast", replay_log_persist: bool = False) -> None:
    """Обновить BOT_MODE (и параметры теста) в backend/.env — переживают рестарт uvicorn."""
    import os
    from pathlib import Path
    env_path = Path(__file__).resolve().parents[3] / ".env"
    pairs = {"BOT_MODE": mode, "BOT_TEST_NAME": test_name,
             "BOT_TEST_START": replay_start, "BOT_TEST_END": replay_end,
             "BOT_TEST_PACE": replay_pace,
                 "BOT_TEST_LOG_PERSIST": "1" if replay_log_persist else "0"}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
        out, have = [], set()
        for ln in lines:
            key = ln.split("=", 1)[0].strip()
            if key in pairs:
                have.add(key)
                out.append(f"{key}={pairs[key]}")
            else:
                out.append(ln)
        for k, v in pairs.items():
            if k not in have:
                out.append(f"{k}={v}")
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.environ["BOT_MODE"] = mode
        os.environ["BOT_TEST_NAME"] = test_name
        os.environ["BOT_TEST_START"] = replay_start
        os.environ["BOT_TEST_END"] = replay_end
        os.environ["BOT_TEST_PACE"] = replay_pace
        os.environ["BOT_TEST_LOG_PERSIST"] = "1" if replay_log_persist else "0"
    except Exception:
        pass


def _build_autostart_cfg(mode: str, test_name: str = "", replay_start: str = "",
                         replay_end: str = "", replay_pace: str = "fast",
                         replay_log_persist: bool = False) -> BotConfig:
    if mode == "test":
        # Тест: тот же движок, но исторические свечи из БД (feed=replay) + paper-выход.
        return BotConfig(
            strategy_id="ensemble_v4",
            interval_name="1min",
            top_n=40,
            use_ensemble=True,
            mode="test",
            feed="replay",
            replay_start=replay_start,
            replay_end=replay_end,
            replay_pace=replay_pace,
            replay_log_persist=replay_log_persist,
            test_name=test_name,
            sessions=["morning", "day", "evening"],
            long_allowed=True,
            short_allowed=True,
            ensemble_session="all",
            atr_period=14,
            atr_multiplier=4.0,
            atr_risk_reward=4.0,
            leverage=1.0,
            commission_rate=0.0005,
            slippage_bps=2.0,
            confirm_flip=2,
            reentry_cooldown_bars=15,
            overnight=True,
        )
    return BotConfig(
        strategy_id="ensemble_v4",
        interval_name="1min",
        top_n=40,
        use_ensemble=True,
        mode=mode,
        sessions=["morning", "day", "evening"],
        long_allowed=True,
        short_allowed=True,
        ensemble_session="all",
        atr_period=14,
        atr_multiplier=4.0,
        atr_risk_reward=4.0,
        leverage=1.0,
        commission_rate=0.0005,
        slippage_bps=2.0,
        confirm_flip=2,
        reentry_cooldown_bars=15,
        overnight=True,
    )


@router.post("/mode")
async def bot_set_mode(req: ModeRequest) -> dict:
    """Переключить контур sandbox/live/test: обновить .env, перезапустить бота."""
    import asyncio
    mode = req.mode if req.mode in ("sandbox", "live", "test") else None
    if not mode:
        raise HTTPException(400, "mode must be 'sandbox', 'live' or 'test'")
    if mode == "test":
        name = req.test_name.strip()
        if not name:
            raise HTTPException(400, "test_name обязателен для mode=test")
        if not req.replay_start:
            raise HTTPException(400, "replay_start обязателен для mode=test")
        # Имена тестов — ключ для идентификации прогона в БД.
        from pathlib import Path
        _bad = set("\\/?%*:|\"<>")
        name = "".join(c if c not in _bad else "_" for c in name).strip()[:48]
        if not name:
            raise HTTPException(400, "test_name пуст после нормализации")
    else:
        name = req.test_name
    pace = req.replay_pace if req.replay_pace in ("fast", "wall") else "fast"
    # Движок теста: непустой test_engine -> одиночная стратегия (use_ensemble=False)
    # через env TEST_ENGINE (читает apply_test_overrides); пустой -> ensemble_v4.
    import os as _os
    _eng = req.test_engine.strip()
    if _eng:
        _os.environ["TEST_ENGINE"] = _eng
    else:
        _os.environ.pop("TEST_ENGINE", None)
    # TF и параметры одиночного движка теста (читает apply_test_overrides):
    _tif = req.test_interval.strip().lower()
    if _tif:
        _os.environ["TEST_INTERVAL"] = _tif
    else:
        _os.environ.pop("TEST_INTERVAL", None)
    if req.test_params:
        import json as _pj
        _os.environ["TEST_PARAMS"] = _pj.dumps(req.test_params, ensure_ascii=False)
    else:
        _os.environ.pop("TEST_PARAMS", None)
    if getattr(req, "preset", None):
        import json as _pj2
        _os.environ["TEST_PRESET"] = _pj2.dumps(req.preset, ensure_ascii=False)
    else:
        _os.environ.pop("TEST_PRESET", None)
    if mode == "test" and getattr(req, "preset", None):
        from app.services.preset_tags import save_sidecar
        try:
            save_sidecar(name, req.preset, {
                "mode": mode, "test_name": name,
                "replay_start": req.replay_start.strip(), "replay_end": req.replay_end.strip(),
                "replay_pace": pace, "test_engine": req.test_engine.strip(),
                "test_interval": req.test_interval.strip(), "test_params": req.test_params,
                "replay_log_persist": bool(req.replay_log_persist), "preset": req.preset,
            })
        except OSError:
            logger.warning("sidecar write failed for %s", name)
    _write_env_mode(mode, test_name=name, replay_start=req.replay_start.strip(),
                    replay_end=req.replay_end.strip(), replay_pace=pace,
                    replay_log_persist=bool(req.replay_log_persist))
    try:
        from app.config import get_settings
        get_settings.cache_clear()
    except Exception:
        pass
    was_running = runtime.running or runtime.starting
    if was_running:
        await runtime.stop()
        for _ in range(40):
            if not runtime.running and not runtime.starting:
                break
            await asyncio.sleep(0.5)
    try:
        await runtime.start(await _cfg_from_saved(
            mode, test_name=name,
            replay_start=req.replay_start.strip(),
            replay_end=req.replay_end.strip(),
            replay_pace=pace,
            replay_log_persist=bool(req.replay_log_persist)))
    except Exception as e:
        raise HTTPException(500, f"restart failed: {e}")
    return {"mode": mode, "test_name": name or None, "restarted": was_running, "replay_pace": pace,
            "replay_log_persist": bool(req.replay_log_persist)}


class PauseRequest(BaseModel):
    paused: bool


@router.post("/pause")
async def bot_pause(req: PauseRequest) -> dict:
    return await runtime.set_entries_paused(req.paused)


@router.post("/orders/cancel-pending")
async def bot_cancel_pending() -> dict:
    return await runtime.cancel_pending()


@router.get("/orders")
async def bot_orders(limit: int = 50) -> dict:
    items = list(runtime.orders)[-limit:]
    return {"count": len(items), "orders": [o.to_dict() for o in reversed(items)]}


@router.get("/approvals")
async def bot_approvals() -> dict:
    """Ожидающие подтверждения входы (AI-гейт) + настройки гейта + активный контур."""
    cfg = runtime.config
    return {
        "enabled": bool(getattr(cfg, "ai_approval", False)),
        "timeout_sec": float(getattr(cfg, "ai_approval_timeout_sec", 45.0) or 45.0),
        "default": str(getattr(cfg, "ai_approval_default", "approve") or "approve"),
        "broker_mode": runtime.broker_mode,
        "contour": runtime.active_contour,
        "ai_mode": str(runtime.get_ai_control().get("mode") or ""),
        "pending": runtime.list_approvals(),
    }


@router.post("/ai_decisions")
async def bot_ai_decision(payload: dict) -> dict:
    """Записать решение AI-гейта (в т.ч. shadow) — воркер шлёт сюда свои вердикты для UI."""
    return await runtime.add_ai_decision(payload or {})


@router.get("/ai_decisions")
async def bot_ai_decisions(limit: int = 20) -> dict:
    """Последние решения AI-гейта (для UI/мониторинга)."""
    return {"count": 0, "decisions": runtime.list_ai_decisions(limit)}


@router.post("/ai_prompt")
async def bot_ai_prompt_set(payload: dict) -> dict:
    """Сохранить текущий промпт/конфиг AI-гейта (воркер шлёт при старте)."""
    return runtime.set_ai_prompt(payload or {})


@router.get("/ai_prompt")
async def bot_ai_prompt_get() -> dict:
    """Текущий промпт AI-гейта (system + модель + схема контекста) — для UI."""
    return runtime.get_ai_prompt()


@router.get("/ai_control")
async def bot_ai_control_get() -> dict:
    """Промпты AI (переопределения + дефолты воркеров) + срочное сообщение — для UI."""
    return runtime.get_ai_control()


@router.put("/ai_control")
async def bot_ai_control_put(payload: dict) -> dict:
    """Сохранить промпт(ы)/срочное сообщение из UI; воркеры читают каждый цикл."""
    return runtime.set_ai_control(payload or {})


@router.post("/ai_report")
async def bot_ai_report_set(payload: dict) -> dict:
    """Отчёт AI о рынке + предложения по механизму бота (ai_trader шлёт каждый цикл)."""
    return runtime.set_ai_report(payload or {})


@router.get("/ai_report")
async def bot_ai_report_get() -> dict:
    """Последний отчёт AI о рынке — для вкладки «Анализ»."""
    return runtime.get_ai_report()


@router.post("/ai_notes")
async def bot_ai_note_set(payload: dict) -> dict:
    """Заметка вахтёра позиций (llama/AI): hold|tighten|close|watch — словами, без управления."""
    return runtime.add_ai_note(payload or {})


@router.get("/ai_notes")
async def bot_ai_notes_get(limit: int = 20) -> dict:
    """Последние заметки вахтёра позиций — для UI."""
    return {"count": 0, "notes": runtime.list_ai_notes(limit)}


class AiTradeRequest(BaseModel):
    ticker: str
    side: str = "SELL"                 # BUY | SELL
    action: str = "open"               # open | close
    notional_pct: float | None = None  # доля стандартного слота (1.0 = слот)
    sl_pct: float | None = None        # стоп, доля (0.03 = 3%)
    tp_pct: float | None = None        # тейк (0 = без TP)
    hold: str = "intraday"             # intraday | swing (swing = держать через ночь)
    sl: float | None = None            # для update_sl/update_tp: новая цена стопа
    tp: float | None = None            # для update_sl/update_tp: новая цена тейка
    reason: str = ""


def _day_change_pct(figi: str) -> float | None:
    """Изменение цены с начала дня (МСК) по живому буферу 1м — для гейта чейзинга."""
    try:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        bb = runtime.tcs_to_bbg.get(figi, figi)
        buf = runtime.buffers.get(bb) or runtime.buffers.get(figi)
        if not buf:
            return None
        _msk = _tz(_td(hours=3))
        _today = _dt.now(_msk).date()
        bars = [c for c in buf if c.ts.astimezone(_msk).date() == _today]
        if len(bars) < 2:
            return None
        first = float(bars[0].open or bars[0].close or 0)
        last = float(bars[-1].close or 0)
        if first <= 0 or last <= 0:
            return None
        return round((last / first - 1.0) * 100.0, 2)
    except Exception:
        return None


@router.post("/ai_trade")
async def bot_ai_trade(req: AiTradeRequest) -> dict:
    """Заявка внешнего AI-трейдера: лимиты маржи/стресса действуют, AI-гейт — нет.

    Плюс жёсткие гейты (настраиваются в BotConfig):
      ai_chase_pct — запрет входа после сильного дневного хода без отката;
      ai_sl_max_pct / ai_tp_max_pct — потолки SL/TP.
    Стакан (spread/imbalance) — общий гейт движка (STAGE S) для всех входов:
      entry_ob_imbalance_max / entry_ob_spread_max.
    """
    import asyncio as _aio
    ticker = str(req.ticker or "").strip().upper()
    figi = next((u.get("figi") for u in (runtime.universe or [])
                 if str(u.get("ticker", "")).upper() == ticker), None)
    if not figi:
        try:
            from app.database import SessionLocal as _SL
            from sqlalchemy import text as _text
            async with _SL() as db:
                row = (await db.execute(_text(
                    "SELECT figi FROM instruments WHERE ticker = :t LIMIT 1"),
                    {"t": ticker})).first()
            figi = str(row[0]) if row else None
        except Exception:
            figi = None
    if not figi:
        raise HTTPException(404, f"ticker {ticker} не найден")
    _spec = runtime.ai_mode_spec()
    if _spec and not _spec.get("trader"):
        raise HTTPException(403, "режим AI не разрешает AI-трейдеру торговать "
                                 "(переключи на «Бот+++» или «AI-трейдер»)")
    _act = str(req.action).lower()
    if _act == "close":
        return await bot_close_position(figi)
    if _act in ("update_sl", "update_tp", "update_levels"):
        _lvl = {}
        if req.sl is not None:
            _lvl["sl"] = float(req.sl)
        if req.tp is not None:
            _lvl["tp"] = float(req.tp)
        if not _lvl:
            raise HTTPException(400, "update_sl/update_tp: нужен sl и/или tp")
        res = await runtime.set_position_levels(figi, **_lvl)
        return {"ok": bool(res.get("ok", True)), "ticker": ticker, "action": _act,
                "levels": _lvl, "result": res, "reason": str(req.reason)[:200]}
    _side = "BUY" if str(req.side).upper() in ("BUY", "LONG") else "SELL"
    # --- Потолки SL/TP (дёшево). Чейзинг/стакан проверяет движок (_submit_order,
    # STAGE C2) — ПОСЛЕ time/trend/portfolio-гейтов и ДО запроса маржи.
    _cfg = runtime.config
    _sl, _tp = req.sl_pct, req.tp_pct
    _sl_cap = float(getattr(_cfg, "ai_sl_max_pct", 0.03) or 0.0)
    if _sl and _sl_cap > 0:
        _sl = min(float(_sl), _sl_cap)
    _tp_cap = float(getattr(_cfg, "ai_tp_max_pct", 0.08) or 0.0)
    if _tp and _tp_cap > 0:
        _tp = min(float(_tp), _tp_cap)
    await runtime._submit_order(figi, ticker, "open", _side, meta={
        "ai_trader": True, "priority": True, "ai_reason": str(req.reason)[:200],
        "notional_pct": req.notional_pct, "sl_pct": _sl, "tp_pct": _tp,
        "hold": str(req.hold or "intraday"),
    })
    return {"ok": True, "ticker": ticker, "side": _side, "action": "open",
            "reason": str(req.reason)[:200]}


@router.get("/bars/{figi}")
async def bot_bars(figi: str, tf: str = "5min", limit: int = 20) -> dict:
    """Свечи из ЖИВОГО буфера рантайма (1м из стрима) с ресемплом в tf.

    tf: 1min | 5min | 10min | 15min | hour. limit — число баров после ресемпла.
    """
    from app.services.ensemble import TF_SECONDS, cached_resample
    bb = runtime.tcs_to_bbg.get(figi, figi)
    buf = runtime.buffers.get(bb) or runtime.buffers.get(figi) or []
    if not buf:
        raise HTTPException(404, f"нет буфера для {figi}")
    tf_sec = TF_SECONDS.get(tf, 300)
    bars = list(buf)[-int(limit) * max(1, tf_sec // 60):]
    out_bars = bars if tf_sec == 60 else cached_resample(bars, tf_sec)
    out = [{"ts": c.ts.isoformat(), "o": float(c.open), "h": float(c.high),
            "l": float(c.low), "c": float(c.close), "v": float(getattr(c, "volume", 0) or 0)}
           for c in out_bars[-int(limit):]]
    return {"figi": bb, "ticker": runtime.tickers.get(bb, ""), "tf": tf,
            "count": len(out), "bars": out}


_HM_CACHE: dict = {}
_MSC = ZoneInfo("Europe/Moscow")




@router.get("/portfolio_summary")
async def bot_portfolio_summary() -> dict:
    """Сводка портфеля: экспозиции (long/short/net), сектора, маржа, стресс ±5/±10% IMOEX."""
    return await runtime.portfolio_snapshot(ttl=3.0)


@router.get("/ai_stats")
async def bot_ai_stats(days: int = 7) -> dict:
    """Статистика AI-гейта: решения, согласие, «сэкономлено/упущено» (контрфакт), точность.

    saved_rub/missed_rub считает трекер (scripts/ai_gate_tracker.py) по цене +30 мин:
    для отклонённых — что было бы; для одобренных — фактический net_pnl (actual_pnl).
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from sqlalchemy import text as _t
    from app.database import SessionLocal as _DB
    _frm = _dt.now(_tz.utc) - _td(days=max(1, min(int(days), 90)))
    async with _DB() as db:
        rows = (await db.execute(_t(
            """SELECT provider, decision, agreement, count(*) n,
                      coalesce(sum(saved_rub) FILTER (WHERE decision = 'reject'),0) saved,
                      coalesce(sum(missed_rub) FILTER (WHERE decision = 'reject'),0) missed,
                      coalesce(sum(actual_pnl) FILTER (WHERE decision <> 'reject'),0) actual,
                      count(outcome_ts) with_outcome,
                      coalesce(avg(latency_ms),0) lat
               FROM ai_decisions WHERE ts >= :frm
               GROUP BY 1,2,3"""
        ), {"frm": _frm})).all()
        top_missed = (await db.execute(_t(
            """SELECT ticker, coalesce(sum(missed_rub),0) v, count(*) n
               FROM ai_decisions WHERE ts >= :frm AND missed_rub > 0 AND decision = 'reject' 
               GROUP BY 1 ORDER BY v DESC LIMIT 5"""
        ), {"frm": _frm})).all()
        top_saved = (await db.execute(_t(
            """SELECT ticker, coalesce(sum(saved_rub),0) v, count(*) n
               FROM ai_decisions WHERE ts >= :frm AND saved_rub > 0 AND decision = 'reject' 
               GROUP BY 1 ORDER BY v DESC LIMIT 5"""
        ), {"frm": _frm})).all()
    tot = {"n": 0, "approve": 0, "reject": 0, "skip": 0, "saved": 0.0, "missed": 0.0,
           "actual": 0.0, "with_outcome": 0, "agree": 0, "disagree": 0}
    by_prov: dict = {}
    for r in rows:
        _p = r.provider or "—"
        _d = str(r.decision or "")
        pv = by_prov.setdefault(_p, {"n": 0, "approve": 0, "reject": 0, "skip": 0,
                                     "saved": 0.0, "missed": 0.0, "actual": 0.0,
                                     "with_outcome": 0, "lat": 0.0})
        for tgt in (tot, pv):
            tgt["n"] += int(r.n)
            if _d in ("approve", "reject", "skip"):
                tgt[_d] += int(r.n)
            tgt["saved"] += float(r.saved or 0)
            tgt["missed"] += float(r.missed or 0)
            tgt["actual"] += float(r.actual or 0)
            tgt["with_outcome"] += int(r.with_outcome or 0)
        if r.agreement is True:
            tot["agree"] += int(r.n)
        elif r.agreement is False:
            tot["disagree"] += int(r.n)
        pv["lat"] = float(r.lat or 0)
    for v in by_prov.values():
        v["saved"] = round(v["saved"], 2)
        v["missed"] = round(v["missed"], 2)
        v["actual"] = round(v["actual"], 2)
        v["lat"] = round(v["lat"], 0)
    tot["saved"] = round(tot["saved"], 2)
    tot["missed"] = round(tot["missed"], 2)
    tot["actual"] = round(tot["actual"], 2)
    tot["impact"] = round(tot["saved"] - tot["missed"], 2)
    return {"days": days, "totals": tot, "by_provider": by_prov,
            "top_missed": [{"ticker": r.ticker, "missed": round(float(r.v), 2), "n": int(r.n)}
                           for r in top_missed],
            "top_saved": [{"ticker": r.ticker, "saved": round(float(r.v), 2), "n": int(r.n)}
                          for r in top_saved]}


@router.post("/approvals/{order_id}/approve")
async def bot_approval_approve(order_id: str, payload: dict | None = None) -> dict:
    """Одобрить вход, ожидающий AI-подтверждения (исполнится на следующем баре)."""
    res = runtime.approve_order(order_id, str((payload or {}).get("reason") or ""))
    if not res.get("ok"):
        raise HTTPException(404, res.get("error", "error"))
    return res


@router.post("/approvals/{order_id}/reject")
async def bot_approval_reject(order_id: str, payload: dict | None = None) -> dict:
    """Отклонить вход, ожидающий AI-подтверждения (заявка снимается)."""
    res = runtime.reject_order(order_id, str((payload or {}).get("reason") or ""))
    if not res.get("ok"):
        raise HTTPException(404, res.get("error", "error"))
    return res


@router.get("/events")
async def bot_events(limit: int = 100) -> dict:
    return {"count": min(limit, len(runtime.events)), "events": runtime.events.latest(limit)}


@router.post("/positions/close-all")
async def bot_close_all() -> dict:
    return await runtime.close_all()


@router.post("/positions/close")
async def bot_close_position(figi: str) -> dict:
    import asyncio as _aio
    positions = await runtime.broker.positions()
    pos = next((p for p in positions if p.figi == figi), None)
    if not pos:
        from app.api.routes.sandbox import _get_portfolio, _q
        p = await _aio.to_thread(_get_portfolio)
        bbg = figi
        from app.api.routes.sandbox import _resolve_bbg
        sip = next((x for x in p.positions if x.figi == figi), None)
        if sip is None:
            tcs = runtime.tcs_to_bbg.get(figi)
            if tcs:
                sip = next((x for x in p.positions if x.figi == tcs), None)
        if sip is None:
            for x in p.positions:
                resolved = await _resolve_bbg(x.figi)
                if resolved == figi:
                    sip = x
                    break
        if sip is None:
            raise HTTPException(404, f"позиция {figi} не найдена")
        cur = _q(sip.current_price)
        lb = _get_sandbox_broker()
        try:
            trade = await lb.close_position(figi, cur, "manual_close")
        except Exception as e:
            raise HTTPException(502, f"ошибка закрытия: {e}")
        runtime._held.discard(figi)
        runtime._held.discard(runtime.tcs_to_bbg.get(figi, figi))
        return {"closed": True, "figi": figi, "ticker": figi[:12], "price": float(cur)}
    bbg = runtime.tcs_to_bbg.get(figi, figi)
    buf = runtime.buffers.get(bbg) or runtime.buffers.get(figi)
    price = float(buf[-1].close) if buf else float(pos.entry_price)
    try:
        trade = await runtime.broker.close_position(figi, price, "manual_close")
    except Exception as e:
        raise HTTPException(502, f"ошибка закрытия: {e}")
    runtime._held.discard(figi)
    runtime._held.discard(bbg)
    return {"closed": True, "figi": figi, "ticker": pos.ticker, "price": price}


@router.post("/positions/levels")
async def bot_position_levels(payload: dict) -> dict:
    """Ручная правка SL/TP позиции (защита прибыли / сдвиг цели)."""
    figi = str(payload.get("figi") or "").strip()
    ticker = str(payload.get("ticker") or "").strip().upper()
    if not figi and ticker:
        _u = next((u for u in (runtime.universe or [])
                   if str(u.get("ticker", "")).upper() == ticker), None)
        if _u:
            figi = _u["figi"]
    if not figi:
        raise HTTPException(400, "figi или ticker обязателен")
    sl = payload.get("sl")
    tp = payload.get("tp")
    res = await runtime.set_position_levels(
        figi,
        sl=float(sl) if sl is not None else None,
        tp=float(tp) if tp is not None else None,
    )
    if not res.get("ok"):
        raise HTTPException(404, res.get("error", "error"))
    return res


@router.get("/state")
async def bot_state() -> dict:
    """Единый снапшот состояния (позиции+цены+SL/TP+P&L+режим+алерты) для ИИ/мониторинга."""
    return await runtime.state_snapshot()


@router.get("/logconfig")
async def bot_logconfig_get() -> dict:
    from app.services.loghub import hub
    return {"log_candles": runtime.log_candles, "buffer_max": hub.maxlen}


@router.post("/logconfig")
async def bot_logconfig_set(payload: dict) -> dict:
    if "log_candles" in payload:
        runtime.log_candles = bool(payload["log_candles"])
    return {"log_candles": runtime.log_candles}


@router.post("/logs/clear")
async def bot_logs_clear() -> dict:
    from app.services.loghub import hub
    hub.clear()
    return {"cleared": True}


def _log_match(r, levels, src_low, q_low, ticker_re, date) -> bool:
    if levels and r.level not in levels:
        return False
    if src_low and src_low not in r.source:
        return False
    if q_low and q_low not in r.msg.lower():
        return False
    if ticker_re and not ticker_re.search(r.msg):
        return False
    if date and not r.ts.startswith(date):
        return False
    return True


@router.get("/logs")
async def bot_logs(
    limit: int = 400,
    after_id: int = 0,
    level: str = "",
    source: str = "",
    q: str = "",
    ticker: str = "",
    date: str = "",
    plain: int = 0,
) -> dict:
    """Структурированные логи бота (in-memory кольцо).

    after_id=0  → последние `limit` записей (tail),
    after_id>0  → только записи с id > after_id (инкрементальный поллинг).
    Фильтры: level (в т.ч. список через запятую), source, q (подстрока msg),
    ticker (граница слова), date (МСК 'YYYY-MM-DD'). plain=1 → старый формат.
    """
    import re
    from app.services.loghub import hub

    levels = {x.strip() for x in level.split(",") if x.strip()} or None
    q_low = q.lower() if q else None
    src_low = source.lower() if source else None
    ticker_re = None
    if ticker and ticker.strip():
        ticker_re = re.compile(
            r"\b" + re.escape(ticker.strip()) + r"\b", re.IGNORECASE
        )

    items: list = []
    src_records = hub.after(after_id, limit=2000) if after_id else hub.tail(limit=2000)
    if after_id:
        for r in src_records:
            if not _log_match(r, levels, src_low, q_low, ticker_re, date):
                continue
            items.append(r)
            if len(items) >= limit:
                break
    else:
        matched: list = []
        for r in reversed(src_records):
            if not _log_match(r, levels, src_low, q_low, ticker_re, date):
                continue
            matched.append(r)
            if len(matched) >= limit:
                break
        items = list(reversed(matched))
    if plain:
        return {"logs": [r.line for r in items], "count": len(items)}
    return {"items": [r.to_dict() for r in items], "count": len(items), "total": hub.len()}


@router.get("/logs/history")
async def bot_logs_history(
    before: str = "",
    limit: int = 500,
    level: str = "",
    source: str = "",
    q: str = "",
    ticker: str = "",
    date: str = "",
) -> dict:
    """История логов из PostgreSQL (за границей in-memory кольца).

    before — МСК timestamp 'YYYY-MM-DD HH:MM:SS[.mmm]'; возвращаются записи
    СТАРШЕ этого момента, по возрастанию id (для подгрузки «вверх»).
    """
    import re
    from datetime import datetime as _dt, timedelta, timezone
    from sqlalchemy import text as _text

    from app.database import SessionLocal
    from app.services.loghub import strip_legacy_ts

    before_utc = None
    if before.strip():
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                _d = _dt.strptime(before.strip(), fmt)
                before_utc = _d.replace(tzinfo=timezone(timedelta(hours=3)))
                break
            except ValueError:
                continue

    where: list[str] = []
    params: dict = {"limit": min(limit, 2000)}
    if before_utc:
        where.append("ts < :before_utc")
        params["before_utc"] = before_utc
    lvls = [x.strip() for x in level.split(",") if x.strip()]
    if lvls:
        where.append("level = ANY(:levels)")
        params["levels"] = lvls
    if source:
        where.append("source ILIKE :source")
        params["source"] = f"%{source}%"
    if q:
        where.append("msg ILIKE :q")
        params["q"] = f"%{q}%"
    if ticker.strip():
        where.append("msg ~* :ticker")
        params["ticker"] = r"\m" + re.escape(ticker.strip()) + r"\M"
    if date.strip():
        where.append("(ts AT TIME ZONE 'Europe/Moscow')::date = :date")
        params["date"] = date.strip()
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    sql = _text(
        "SELECT id, "
        "to_char(ts AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD HH24:MI:SS.MS') AS ts_s, "
        "COALESCE(level,'info') AS lvl, COALESCE(source,'bot') AS src, msg "
        f"FROM bot_logs{where_sql} ORDER BY id DESC LIMIT :limit"
    )
    try:
        async with SessionLocal() as db:
            rows = (await db.execute(sql, params)).all()
    except Exception as e:
        return {"items": [], "error": str(e)[:200], "has_more": False}

    items = [
        {"id": int(r.id), "ts": r.ts_s, "level": (r.lvl or "info")[:16],
         "source": (r.src or "bot")[:16], "msg": strip_legacy_ts(r.msg or "")}
        for r in rows
    ]
    items.reverse()  # старые → новые, для вставки в начало списка
    return {"items": items, "has_more": len(rows) >= limit}


@router.get("/status")
async def bot_status() -> dict:
    status = runtime.status

    # При остановленном боте runtime.config может хранить устаревшие значения
    # (например, autostart/последний запуск), тогда как PATCH /config пишет в файл.
    # Отдаём сохранённый конфиг, чтобы фронт не перетирал настройки слайдера/пилюль.
    if not (runtime.running or runtime.starting) and status.get("config"):
        try:
            status["config"].update(_config_payload(await _cfg_from_saved()))
        except Exception:
            logger.exception("status: не смогли подмешать сохранённый конфиг")

    if runtime.running:
        try:
            risk = runtime.risk_snapshot()
            status["risk"] = {
                "state": risk.state,
                "daily_pnl": round(risk.daily_pnl, 2),
                "daily_loss_limit": risk.daily_loss_limit,
                "entries_paused": risk.entries_paused,
            }
        except Exception:
            pass

    portfolio = {}
    try:
        from app.api.routes.sandbox import (
            _active_test_name, _test_trades_db, _test_portfolio_digest,
        )
        _tn = _active_test_name()
        if _tn:
            _rows = await _test_trades_db(_tn)
            portfolio = await _test_portfolio_digest(_tn, _rows)
    except Exception:
        portfolio = {}
    if not portfolio:
        try:
            from app.api.routes.sandbox import _portfolio_digest
            dig = await _portfolio_digest()
            if dig:
                portfolio = dig
        except Exception:
            portfolio = {}

    _breadth = {}
    try:
        from app.api.routes.screener import market_breadth
        _breadth = market_breadth()
    except Exception:
        _breadth = {}
    return {
        **status,
        "ai_mode": str(runtime.get_ai_control().get("mode") or ""),
        "breadth": _breadth,
        "portfolio": {
            "cash": portfolio.get("cash", 0),
            "initial_cash": portfolio.get("initial_cash", 10000),
            "market_value": portfolio.get("market_value", 0),
            "equity": portfolio.get("equity", 0),
            "pnl": portfolio.get("pnl", 0),
            "positions_open": portfolio.get("positions_open", len(runtime.buffers)),
            "own_in_positions": portfolio.get("own_in_positions", 0),
            "positions_value": portfolio.get("positions_value", 0),
            "tinkoff_currencies": portfolio.get("tinkoff_currencies", 0),
            "tinkoff_shares": portfolio.get("tinkoff_shares", 0),
            "trades": portfolio.get("trades", {"total": 0, "wins": 0, "winrate": 0}),
            "reconcile": portfolio.get("reconcile", {"ok": False}),
        },
    }


@router.get("/positions")
async def bot_positions(db: AsyncSession = Depends(get_db)) -> dict:
    res = await db.execute(select(PaperPosition))
    items = [
        {
            "figi": p.figi,
            "ticker": p.ticker,
            "side": p.side,
            "qty": p.qty,
            "entry_price": float(p.entry_price),
            "entry_time": p.entry_time.isoformat(),
            "stop_loss": float(p.stop_loss) if p.stop_loss else None,
            "take_profit": float(p.take_profit) if p.take_profit else None,
            "strategy_id": p.strategy_id,
        }
        for p in res.scalars()
    ]
    return {"count": len(items), "positions": items}


def _trade_regime(t) -> str:
    """regime закрытой сделки = entry_regime из meta (движок фиксирует на входе)."""
    import json as _j
    try:
        m = getattr(t, "meta", None)
        return ( _j.loads(m) if isinstance(m, str) else (m or {}) ).get("entry_regime") or "NO_REGIME"
    except Exception:
        return "NO_REGIME"


@router.get("/trades")
async def bot_trades(limit: int = 50) -> dict:
    """История закрытых сделок АКТИВНОГО контура (sandbox/live).

    Важно: раньше отдавали общую таблицу PaperTrade без фильтра контура — AI-воркеры
    видели live-сделки, когда бот уже переключён на sandbox (и наоборот). Теперь
    выборка идёт из sandbox_trades по mode активного счёта.
    """
    _lim = min(limit, 500)
    _mode = str(getattr(runtime, "broker_mode", "") or "")
    if _mode in ("sandbox", "live"):
        from sqlalchemy import func as _fn
        from sqlalchemy import select as _sel
        from app.database import SessionLocal as _DB
        from app.models.sandbox_trade import SandboxTrade
        async with _DB() as db:
            rows = (await db.execute(
                _sel(SandboxTrade)
                .where(SandboxTrade.mode == _mode, SandboxTrade.exit_time.is_not(None),
                       _fn.coalesce(SandboxTrade.exit_reason, "") != "reopened")
                .order_by(SandboxTrade.exit_time.desc())
                .limit(_lim)
            )).scalars().all()
        return {
            "count": len(rows),
            "broker_mode": _mode,
            "trades": [
                {
                    "figi": t.figi,
                    "ticker": t.ticker,
                    "side": t.side,
                    "qty": int(t.qty or 0),
                    "entry_time": t.entry_time.isoformat() if t.entry_time else "",
                    "entry_price": float(t.entry_price or 0),
                    "exit_time": t.exit_time.isoformat() if t.exit_time else "",
                    "exit_price": float(t.exit_price or 0),
                    "net_pnl": float(t.net_pnl or 0),
                    "commission": float(t.commission or 0),
                    "exit_reason": t.exit_reason or "",
                    "strategy_id": "v4_enhanced",
                    "regime": _trade_regime(t),
                }
                for t in rows
            ],
        }
    trades = await runtime.broker.trades_history(_lim)
    return {
        "count": len(trades),
        "broker_mode": _mode or "paper",
        "trades": [
            {
                "figi": t.figi,
                "ticker": t.ticker,
                "side": t.side,
                "qty": t.qty,
                "entry_time": t.entry_time.isoformat(),
                "entry_price": float(t.entry_price),
                "exit_time": t.exit_time.isoformat(),
                "exit_price": float(t.exit_price),
                "net_pnl": float(t.net_pnl),
                "commission": float(t.commission),
                "exit_reason": t.exit_reason,
                "strategy_id": t.strategy_id,
                "regime": _trade_regime(t),
            }
            for t in trades
        ],
    }


@router.post("/reset")
async def bot_reset(initial_cash: float = 10_000.0) -> dict:
    if runtime.running or runtime.starting:
        raise HTTPException(409, "остановите бота перед сбросом")
    await runtime.broker.reset(initial_cash)
    return {"reset": True}


@router.get("/orderbook/{figi}")
async def bot_orderbook(figi: str, depth: int = 10) -> dict:
    """Стакан (order book) на момент запроса: топ-N уровней + метрики для AI-гейта.

    spread_bps — ширина спреда (б.п.); imbalance — перевес бидов (-1..+1);
    depth_rub — ликвидность в топе (₽). Реализация — app/services/orderbook.py
    (переиспользуемый клиент + кэш 5с: гейт и трейдер дёргают стакан десятками за цикл).
    """
    from app.services.orderbook import fetch_orderbook
    try:
        return await fetch_orderbook(figi, depth)
    except Exception as e:
        raise HTTPException(502, f"orderbook: {type(e).__name__}: {str(e)[:120]}")


@router.get("/trading_status")
async def bot_trading_status() -> dict:
    """Возвращает реальный торговый статус MOEX через market_data.get_trading_status."""
    from t_tech.invest import Client, SecurityTradingStatus
    from app.config import get_settings
    _s = get_settings()
    TOKEN = _s.get_token(_s.bot_mode)
    SB = _s.get_target(_s.bot_mode)
    
    from t_tech.invest import SecurityTradingStatus as STS
    STATUS_MAP = {
        STS.SECURITY_TRADING_STATUS_UNSPECIFIED: "UNSPECIFIED",
        STS.SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING: "NOT_AVAILABLE",
        STS.SECURITY_TRADING_STATUS_OPENING_PERIOD: "OPENING",
        STS.SECURITY_TRADING_STATUS_CLOSING_PERIOD: "CLOSING",
        STS.SECURITY_TRADING_STATUS_BREAK_IN_TRADING: "BREAK",
        STS.SECURITY_TRADING_STATUS_NORMAL_TRADING: "TRADING",
        STS.SECURITY_TRADING_STATUS_CLOSING_AUCTION: "CLOSING_AUCTION",
        STS.SECURITY_TRADING_STATUS_DARK_POOL_AUCTION: "DARK_POOL",
        STS.SECURITY_TRADING_STATUS_DISCRETE_AUCTION: "DISCRETE",
        STS.SECURITY_TRADING_STATUS_OPENING_AUCTION_PERIOD: "OPENING_AUCTION",
        STS.SECURITY_TRADING_STATUS_TRADING_AT_CLOSING_AUCTION_PRICE: "CLOSING_AUCTION_PRICE",
        STS.SECURITY_TRADING_STATUS_SESSION_ASSIGNED: "SESSION_ASSIGNED",
        STS.SECURITY_TRADING_STATUS_SESSION_CLOSE: "SESSION_CLOSE",
        STS.SECURITY_TRADING_STATUS_SESSION_OPEN: "SESSION_OPEN",
        STS.SECURITY_TRADING_STATUS_DEALER_NORMAL_TRADING: "DEALER_TRADING",
        STS.SECURITY_TRADING_STATUS_DEALER_BREAK_IN_TRADING: "DEALER_BREAK",
        STS.SECURITY_TRADING_STATUS_DEALER_NOT_AVAILABLE_FOR_TRADING: "DEALER_NOT_AVAILABLE",
    }
    
    figis = [
        ("BBG004730N88", "SBER"), ("BBG004730RP0", "GAZP"), ("BBG004731032", "LKOH"),
        ("BBG00475KKY8", "NVTK"), ("BBG00F6NKQX3", "SMLT"),
    ]
    
    results = {}
    try:
        with Client(TOKEN, target=SB) as client:
            for figi, ticker in figis:
                try:
                    resp = client.market_data.get_trading_status(figi=figi)
                    code = resp.trading_status
                    results[ticker] = {
                        "figi": figi,
                        "status_code": code,
                        "status": STATUS_MAP.get(code, f"UNKNOWN_{code}"),
                    }
                except Exception as e:
                    results[ticker] = {"figi": figi, "status": "ERROR", "error": str(e)}
    except Exception as e:
        return {"error": str(e), "statuses": {}}
    
    sber = results.get("SBER", {})
    sber_status = sber.get("status", "UNKNOWN")
    
    session_map = {
        "TRADING": "trading", "OPENING": "pre_market", "OPENING_AUCTION": "pre_market",
        "CLOSING": "clearing", "CLOSING_AUCTION": "clearing",
        "CLOSING_AUCTION_PRICE": "clearing",
        "DEALER_TRADING": "weekend", "BREAK": "break",
        "NOT_AVAILABLE": "closed", "SESSION_CLOSE": "closed",
        "SESSION_OPEN": "trading",
    }
    
    return {
        "session": session_map.get(sber_status, "unknown"),
        "status": sber_status,
        "statuses": results,
    }


class TestRunInfo(BaseModel):
    name: str
    replay_start: str = ""
    replay_end: str = ""
    created_at: str = ""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    net: float = 0.0
    pf: float = 0.0
    winrate: float = 0.0
    positions_open: int = 0


@router.get("/tests")
async def bot_tests() -> dict:
    """Список всех прогонов тестов (mode='paper' + test_name) со статистикой."""
    from app.database import SessionLocal as _DB
    from app.models.sandbox_trade import SandboxTrade
    from sqlalchemy import select as _sel
    from sqlalchemy import func as _fn
    async with _DB() as db:
        r = await db.execute(
            _sel(SandboxTrade.test_name)
            .where(SandboxTrade.mode == "paper", SandboxTrade.test_name.is_not(None))
            .distinct()
        )
        names = [x[0] for x in r.all() if x[0]]
    # Истинные окна прогонов (пишет runtime при старте реплея). Fallback — по сделкам.
    windows: dict[str, tuple] = {}
    try:
        from sqlalchemy import text as _t
        async with _DB() as db:
            wr = await db.execute(_t(
                "SELECT name, replay_start, replay_end, updated_at FROM bot_test_runs"))
            for _n, _s, _e, _u in wr.all():
                windows[str(_n)] = (_s, _e, _u)
    except Exception:
        windows = {}
    # Идущий прогон появляется в списке сразу, даже до первой сделки.
    for _wn in windows:
        if _wn not in names:
            names.append(_wn)
    items = []
    for name in names:
        async with _DB() as db:
            rows = (await db.execute(
                _sel(SandboxTrade)
                .where(SandboxTrade.mode == "paper", SandboxTrade.test_name == name)
            )).scalars().all()
        closed = [t for t in rows if t.exit_time is not None and t.net_pnl is not None]
        open_rows = [t for t in rows if t.exit_time is None]
        wins = [t for t in closed if t.net_pnl >= 0]
        losses = [t for t in closed if t.net_pnl < 0]
        gw = sum(float(t.net_pnl) for t in wins)
        gl = abs(sum(float(t.net_pnl) for t in losses))
        net = sum(float(t.net_pnl) for t in closed)
        pf = (gw / gl) if gl > 0 else (gw if gw else 0.0)
        entries = [t.entry_time for t in rows if t.entry_time]
        exits = [t.exit_time for t in rows if t.exit_time]
        _w = windows.get(name)
        items.append(TestRunInfo(
            name=name,
            replay_start=(_w[0].isoformat() if _w and _w[0] else
                          (min(entries).isoformat() if entries else "")),
            replay_end=(_w[1].isoformat() if _w and _w[1] else
                        (max(entries).isoformat() if entries else "")),
            created_at=(_w[2].isoformat() if _w and _w[2] else
                        (min(entries).isoformat() if entries else "")),
            trades=len(closed),
            wins=len(wins), losses=len(losses),
            gross_win=round(gw, 2), gross_loss=round(gl, 2),
            net=round(net, 2),
            pf=round(pf, 3) if pf else 0.0,
            winrate=round(100.0 * len(wins) / len(closed), 1) if closed else 0.0,
            positions_open=len(open_rows),
        ).model_dump())
    return {"tests": sorted(items, key=lambda x: x["created_at"], reverse=True)}


@router.get("/tests/{name}")
async def bot_test_trades(name: str) -> dict:
    """Сделки конкретного теста."""
    from app.database import SessionLocal as _DB
    from app.models.sandbox_trade import SandboxTrade
    from sqlalchemy import select as _sel
    async with _DB() as db:
        rows = (await db.execute(
            _sel(SandboxTrade)
            .where(SandboxTrade.mode == "paper", SandboxTrade.test_name == name)
            .order_by(SandboxTrade.entry_time.desc())
        )).scalars().all()
    trades = []
    for t in rows:
        trades.append({
            "figi": t.figi, "ticker": t.ticker, "side": t.side,
            "qty": int(t.qty),
            "entry_price": round(float(t.entry_price), 6),
            "exit_price": round(float(t.exit_price), 6) if t.exit_price else None,
            "entry_time": str(t.entry_time),
            "ts": str(t.exit_time) if t.exit_time else None,
            "stop_loss": float(t.stop_loss) if t.stop_loss is not None else None,
            "take_profit": float(t.take_profit) if t.take_profit is not None else None,
            "commission": round(float(t.commission), 2) if t.commission is not None else 0,
            "net_pnl": round(float(t.net_pnl), 2) if t.net_pnl is not None else None,
            "exit_reason": t.exit_reason or "",
            "entry_reason": t.entry_reason or "",
            "test_name": t.test_name,
        })
    return {"test_name": name, "trades": trades}


@router.delete("/tests/{name}")
async def bot_test_delete(name: str) -> dict:
    """Удалить прогон теста: сделки mode='paper', окно bot_test_runs, сайдкар пресета."""
    return await _delete_test_runs([name])


class TestsDeleteRequest(BaseModel):
    names: list[str] = Field(min_length=1, max_length=500)


@router.post("/tests/delete")
async def bot_tests_delete(req: TestsDeleteRequest) -> dict:
    """Массовое удаление прогонов тестов."""
    details = await _delete_test_runs(req.names)
    return {"deleted": sum(d["trades"] for d in details), "tests": len(details), "details": details}


async def _delete_test_runs(names: list[str]) -> list[dict]:
    from sqlalchemy import delete as _delete, text as _text
    from app.database import SessionLocal as _DB
    from app.models.sandbox_trade import SandboxTrade
    from app.services.preset_tags import remove_sidecar
    details = []
    async with _DB() as db:
        for name in names:
            r = await db.execute(
                _delete(SandboxTrade)
                .where(SandboxTrade.mode == "paper", SandboxTrade.test_name == name)
            )
            runs = 0
            try:
                rr = await db.execute(_text("DELETE FROM bot_test_runs WHERE name = :n"), {"n": name})
                runs = int(rr.rowcount or 0)
            except Exception:
                pass
            sc = remove_sidecar(name)
            details.append({"name": name, "trades": int(r.rowcount or 0), "windows": runs,
                            "sidecar": sc})
        await db.commit()
    return details


class TestRestartResponse(BaseModel):
    old_name: str
    test_name: str
    restarted: bool = False


@router.post("/tests/{name}/restart")
async def bot_test_restart(name: str) -> TestRestartResponse:
    """Перезапустить прогон под НОВЫМ именем (<base> <YYYYMMDD-HHMM>).

    Старый тест остаётся нетронутым (история сохраняется). Конфиг берётся из
    сайдкара пресета, а при его отсутствии — текущий окно из bot_test_runs.
    """
    import re
    from datetime import datetime, timezone
    from app.database import SessionLocal as _DB
    from app.services.preset_tags import load_sidecar
    from sqlalchemy import text as _text

    sidecar = load_sidecar(name)
    payload = (sidecar or {}).get("payload") or {}
    window: dict = {}
    if not payload.get("replay_start"):
        try:
            async with _DB() as db:
                row = (await db.execute(
                    _text("SELECT replay_start, replay_end FROM bot_test_runs WHERE name = :n"),
                    {"n": name})).first()
            if row:
                window = {"replay_start": _iso_dt(row[0]), "replay_end": _iso_dt(row[1])}
        except Exception:
            window = {}
    if not payload.get("replay_start") and not window.get("replay_start"):
        raise HTTPException(400, f"у теста {name!r} нет ни сайдкара, ни окна в bot_test_runs")

    base = re.sub(r"\s+\d{8}-\d{4}(-\d+)?$", "", name).strip() or name
    existing = set()
    try:
        async with _DB() as db:
            rows = (await db.execute(_text("SELECT name FROM bot_test_runs"))).all()
            existing = {str(r[0]) for r in rows}
    except Exception:
        existing = set()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    suffix = 1
    while True:
        cand = (f"{base} {ts}" if suffix == 1 else f"{base} {ts}-{suffix}")[:48]
        if cand != name and cand not in existing:
            break
        suffix += 1
        if suffix > 30:
            raise HTTPException(409, "не удалось подобрать свободное имя теста")

    req = ModeRequest(
        mode="test",
        test_name=cand,
        replay_start=str(payload.get("replay_start") or window.get("replay_start") or ""),
        replay_end=str(payload.get("replay_end") or window.get("replay_end") or ""),
        replay_pace=str(payload.get("replay_pace") or "fast"),
        test_engine=str(payload.get("test_engine") or ""),
        test_interval=str(payload.get("test_interval") or ""),
        test_params=payload.get("test_params") or {},
        preset=payload.get("preset") or {},
        replay_log_persist=bool(payload.get("replay_log_persist", False)),
    )
    out = await bot_set_mode(req)
    return TestRestartResponse(old_name=name, test_name=str(out.get("test_name") or cand),
                                restarted=bool(out.get("restarted")))


def _iso_dt(value) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _agg(trades: list) -> dict:
    """Агрегат по списку сделок (net>0 = win)."""
    n = len(trades)
    gw = sum(float(t.net_pnl) for t in trades if t.net_pnl is not None and float(t.net_pnl) > 0)
    gl = sum(float(t.net_pnl) for t in trades if t.net_pnl is not None and float(t.net_pnl) <= 0)
    net = gw + gl
    wins = sum(1 for t in trades if t.net_pnl is not None and float(t.net_pnl) > 0)
    return {
        "trades": n, "wins": wins, "losses": n - wins,
        "gross_win": round(gw, 2), "gross_loss": round(gl, 2),
        "net": round(net, 2),
        "pf": round(gw / abs(gl), 2) if gl else None,
        "wr": round(wins / n * 100, 1) if n else 0,
        "avg_win": round(gw / wins, 2) if wins else 0,
        "avg_loss": round(gl / (n - wins), 2) if n - wins else 0,
    }


@router.get("/test_stats")
async def bot_test_stats(test_name: str = "", date_from: str = "", date_to: str = "",
                         mode: str = "") -> dict:
    """Статистика прогона теста (или live за период) по срезам.

    date_from/date_to — ISO-дата или дата-время (UTC); фильтр по entry_time.
    mode — явный режим для live-выборки (sandbox/live); иначе берётся из runtime.
    Срезы: overall, side, regime, ticker, entry_reason, exit_reason, quorum, session.
    """
    import json as _j
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from collections import defaultdict
    from sqlalchemy import select as _sel
    from app.database import SessionLocal as _DB
    from app.models.sandbox_trade import SandboxTrade

    def _parse(v: str, end: bool = False):
        if not v:
            return None
        try:
            if len(v) == 10:
                d = _dt.fromisoformat(v).replace(tzinfo=_tz.utc)
                return d + _td(days=1) - _td(microseconds=1) if end else d
            d = _dt.fromisoformat(v.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=_tz.utc)
            return d
        except Exception:
            return None

    name = test_name.strip()
    mode = (mode or "").strip()
    if not name:
        try:
            from app.bot.runtime import runtime as _rt
            if not mode:
                name = (getattr(_rt.config, "test_name", "") or "").strip()
                if not name:
                    mode = _rt.broker_mode if getattr(_rt, "broker_mode", None) else "sandbox"
        except Exception:
            pass
    if not mode:
        mode = "paper" if name else "sandbox"
    async with _DB() as db:
        q = _sel(SandboxTrade)
        if name:
            q = q.where(SandboxTrade.test_name == name)
        else:
            q = q.where(SandboxTrade.mode == mode)
        _df = _parse(date_from)
        _dtto = _parse(date_to, end=True)
        if _df is not None:
            q = q.where(SandboxTrade.entry_time >= _df)
        if _dtto is not None:
            q = q.where(SandboxTrade.entry_time <= _dtto)
        rows = (await db.execute(q)).scalars().all()

    closed = [t for t in rows if t.exit_time is not None]
    opened = [t for t in rows if t.exit_time is None]

    def _meta(t):
        try:
            return _j.loads(t.meta) if t.meta else {}
        except Exception:
            return {}

    by_side = defaultdict(list)
    by_regime = defaultdict(list)
    by_ticker = defaultdict(list)
    by_entry = defaultdict(list)
    by_exit = defaultdict(list)
    by_quorum = defaultdict(list)
    by_session = defaultdict(list)
    by_strategy = defaultdict(list)
    by_bias = defaultdict(list)
    for t in closed:
        m = _meta(t)
        by_side[t.side or "?"].append(t)
        reg = m.get("regime") or "—"
        by_regime[reg].append(t)
        by_ticker[t.ticker or "?"].append(t)
        by_entry[t.entry_reason or "?"].append(t)
        by_exit[t.exit_reason or "?"].append(t)
        ent = m.get("entry") or {}
        qe = m.get("quorum_event") or {}
        q = qe.get("votes") if qe else (ent.get("quorum") or ent.get("votes"))
        by_quorum[str(q) if q is not None else "—"].append(t)
        _members = qe.get("members_for") or ent.get("quorum_members") or ent.get("members_for") or []
        for _sid in (_members or ["—"]):
            by_strategy[str(_sid)].append(t)
        _ab = ent.get("against_bias")
        by_bias["против bias" if _ab is True else ("по bias" if _ab is False else "—")].append(t)
        try:
            h = t.entry_time.astimezone(__import__("datetime").timezone(
                __import__("datetime").timedelta(hours=3))).hour
            by_session["утро" if h < 10 else ("день" if h < 19 else "вечер")].append(t)
        except Exception:
            pass

    def _map(d):
        return [{"key": k, **_agg(v)} for k, v in sorted(d.items(), key=lambda x: -abs(_agg(x[1])["net"]))]

    return {
        "test_name": name or None,
        "mode": mode,
        "date_from": date_from or None,
        "date_to": date_to or None,
        "overall": _agg(closed),
        "open_positions": len(opened),
        "by_side": _map(by_side),
        "by_regime": _map(by_regime),
        "by_ticker": _map(by_ticker),
        "by_entry_reason": _map(by_entry),
        "by_exit_reason": _map(by_exit),
        "by_quorum": _map(by_quorum),
        "by_session": _map(by_session),
        "by_strategy": _map(by_strategy),
        "by_bias": _map(by_bias),
    }


@router.get("/tests_compare")
async def bot_tests_compare(names: str = "") -> dict:
    """Сравнение нескольких прогонов тестов по срезам (overall/regime/strategy/side/exit)."""
    import json as _j
    from collections import defaultdict
    from sqlalchemy import select as _sel
    from app.database import SessionLocal as _DB
    from app.models.sandbox_trade import SandboxTrade

    def _meta(t):
        try:
            return _j.loads(t.meta) if t.meta else {}
        except Exception:
            return {}

    def _map(d):
        return [{"key": k, **_agg(v)} for k, v in sorted(d.items(), key=lambda x: -abs(_agg(x[1])["net"]))]

    wanted = [n.strip() for n in names.split(",") if n.strip()]
    out: dict = {}
    async with _DB() as db:
        for name in wanted:
            rows = (await db.execute(
                _sel(SandboxTrade).where(SandboxTrade.mode == "paper",
                                         SandboxTrade.test_name == name)
            )).scalars().all()
            closed = [t for t in rows if t.exit_time is not None]
            by_side = defaultdict(list)
            by_regime = defaultdict(list)
            by_strategy = defaultdict(list)
            by_exit = defaultdict(list)
            for t in closed:
                m = _meta(t)
                by_side[t.side or "?"].append(t)
                by_regime[m.get("regime") or "—"].append(t)
                by_exit[t.exit_reason or "?"].append(t)
                ent = m.get("entry") or {}
                qe = m.get("quorum_event") or {}
                members = qe.get("members_for") or ent.get("quorum_members") or ent.get("members_for") or []
                for sid in (members or ["—"]):
                    by_strategy[str(sid)].append(t)
            out[name] = {
                "overall": _agg(closed),
                "by_side": _map(by_side),
                "by_regime": _map(by_regime),
                "by_strategy": _map(by_strategy),
                "by_exit_reason": _map(by_exit),
            }
    return {"tests": out}


def _tz_utc():
    from datetime import timezone
    return timezone.utc


def _dt_now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _bias_from_daily(closes_by_date: dict[str, float]) -> dict[str, int]:
    """Направление по EMA(50) дневных закрытий (ключ — МСК-дата), как compute_bias."""
    items = sorted(closes_by_date.items())
    dates = [d for d, _ in items]
    closes = [c for _, c in items]
    if len(closes) < 3:
        return {}
    _alpha = 2 / (50 + 1)
    emas = [closes[0]]
    for v in closes[1:]:
        emas.append(_alpha * v + (1 - _alpha) * emas[-1])
    out: dict[str, int] = {}
    for i in range(1, len(closes)):
        out[dates[i]] = 1 if closes[i - 1] >= emas[i - 1] else -1
    return out


async def _hm_compute_meta(db, bb: str, frm) -> (dict[str, dict], float, int):
    """bias (daily по МСК-датам) + regime (H1) для тикера.

    Берём 1м за ~32 кал. дня — для прогрева EMA(50) bias и warmup (64+) H1 regime.
    Возвращает (by_hour, last_close, days_ok): by_hour — {iso час: {b, r}}.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from sqlalchemy import text as _t
    from app.engine.models import Candle as _EC
    from app.services.regime import compute_regime as _CR
    _cut = _dt.now(_tz.utc) - _td(minutes=1)
    _frm36 = _cut - _td(days=32)
    _rows1 = (await db.execute(_t(
        "SELECT ts, open, high, low, close, volume FROM candles "
        "WHERE figi = :f AND interval = '1' AND ts >= :frm ORDER BY ts"
    ), {"f": bb, "frm": _frm36})).all()
    if len(_rows1) < 400:
        return {}, 0.0, 0
    _c1m = [_EC(ts=r[0], open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5] or 0.0)) for r in _rows1]
    _st, _tl, _h30 = _CR(_c1m, 1800)  # режим 30m (правило владельца; единый источник с движком)
    _tl = _tl or []
    from app.services.ensemble import compute_bias as _cb
    _bh = _cb(_h30, 100, 1800)  # bias часового горизонта для ячейки (по 30m барам)
    _rmap: dict[int, str] = {}
    for _rr in (_st or []):
        _rrts = _rr["ts"]
        if getattr(_rrts, "tzinfo", None) is None:
            _rrts = _rrts.replace(tzinfo=_tz_utc())
        _rmap[int(_rrts.timestamp()) // 1800] = _rr["state"]
    # дневные закрытия из 1м по МСК-датам (маппинг без гэпов смещения дня)
    _daily: dict[str, float] = {}
    _last_close = 0.0
    for _c in _c1m:
        _ct = _c.ts if getattr(_c.ts, "tzinfo", None) else _c.ts.replace(tzinfo=_tz_utc())
        _d = _ct.astimezone(_MSC).date().isoformat()
        _daily[_d] = float(_c.close)
    _biasd = _bias_from_daily(_daily)
    out: dict[str, dict] = {}
    _days_ok = 0
    for _hb in _h30:
        _hbt = _hb.ts
        if getattr(_hbt, "tzinfo", None) is None:
            _hbt = _hbt.replace(tzinfo=_tz_utc())
        _d = _hbt.astimezone(_MSC).date().isoformat()
        _hour_ts = _hbt.replace(minute=0, second=0, microsecond=0)
        _hour_key = _hour_ts.isoformat()
        _slot = 0 if _hbt.minute < 30 else 1
        _r = _rmap.get(int(_hbt.timestamp()) // 1800, "NEUTRAL")
        _cell = out.get(_hour_key)
        if _cell is None:
            _cell = {"b": _biasd.get(_d, 0),
                     "bh": _bh.get(int(_hour_ts.timestamp()) // 1800, 0),
                     "r": _r if _slot == 0 else "NEUTRAL",
                     "r30": [None, None]}
            out[_hour_key] = _cell
        _cell["r30"][_slot] = _r
        if _slot == 0:
            _cell["r"] = _r
        if _hbt >= frm:
            _days_ok += 1
        _last_close = float(_hb.close)
    for _cell in out.values():
        if _cell.get("r") in (None, "NEUTRAL") and _cell["r30"][1] is not None:
            _cell["r"] = _cell["r30"][1]
    return out, _last_close, _days_ok


@router.get("/heatmap")
async def bot_heatmap(days: int = 5, meta: int = 0, figi: str | None = None) -> dict:
    """Часовые бары ВСЕХ акций universe за N дней — для heatmap на вкладке «Анализ».

    Берём 1м-свечи из БД (candles, interval='1') и ресемплим в часы (date_trunc).
    Возвращаем по каждому тикеру только закрытые часовые бары (ts, close).
    При meta=1 в каждый бар добавляем b (bias дневного ТФ: +1/-1/0) и r (режим H1).
    figi=X — ограничить одним тикером (принимает и tcs-figi, и bbg-код) — для карточки сделки.
    """
    days = max(1, min(int(days), 10))
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from sqlalchemy import text as _t
    from app.database import SessionLocal as _DB
    univ = list(runtime.universe or [])
    _frm = _dt.now(_tz.utc) - _td(days=days)
    _map = runtime.tcs_to_bbg or {}
    if figi:
        _want_bb = _map.get(figi, figi)
        univ = [u for u in univ if str(u.get("figi") or "") in (figi, _want_bb)]
    _want_meta = bool(meta)
    _replay = str(getattr(runtime.config, "feed", "")) == "replay"

    _now = _dt.now(_tz.utc)
    _ck = ("hm", days, _want_meta, figi or "")
    _cached = _HM_CACHE.get(_ck)
    if not _replay and _cached and (_now - _cached[0]).total_seconds() < 60:
        return _cached[1]

    out = []
    async with _DB() as db:
        for u in univ:
            f0 = str(u.get("figi") or "")
            if not f0:
                continue
            bb = _map.get(f0, f0)
            rows = (await db.execute(_t(
                """SELECT date_trunc('hour', ts) AS h,
                          (array_agg(open ORDER BY ts ASC))[1] AS o,
                          (array_agg(close ORDER BY ts DESC))[1] AS close
                   FROM candles WHERE figi = :f AND interval = '1' AND ts >= :frm
                   GROUP BY 1 ORDER BY 1"""
            ), {"f": bb, "frm": _frm})).all()
            bars = [{"h": r.h.isoformat(), "o": float(r.o), "c": float(r.close)}
                    for r in rows if r.h is not None]
            if _want_meta and bars:
                _meta, _lc, _dok = await _hm_compute_meta(db, bb, _frm)
                for b in bars:
                    _m = _meta.get(b["h"])
                    if _m:
                        b["b"], b["bh"], b["r"] = _m["b"], _m["bh"], _m["r"]
            if bars:
                out.append({"figi": bb,
                            "ticker": str(u.get("ticker") or "").upper(),
                            "lot": int(u.get("lot") or 1),
                            "bars": bars})
    _res = {"ok": True, "days": days, "count": len(out), "all": len(univ),
            "tickers": out}
    if not _replay:
        _HM_CACHE[_ck] = (_now, _res)
    return _res
