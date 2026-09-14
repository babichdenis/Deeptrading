import logging
import os
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.runtime import (BotConfig, BOT_PERSIST_FIELDS, load_bot_settings, runtime,
                             save_bot_settings, load_ensemble_config, save_ensemble_config,
                             default_ensemble_config)

logger = logging.getLogger("bot_api")


async def _cfg_from_saved() -> BotConfig:
    """Собрать конфиг из сохранённых настроек (файл/БД) — для работы без запущенного бота."""
    cfg = _build_autostart_cfg("sandbox")
    saved = await load_bot_settings()
    for f in BOT_PERSIST_FIELDS:
        if f in saved:
            try:
                setattr(cfg, f, saved[f])
            except Exception:
                pass
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
        "commission_rate": cfg.commission_rate,
        "overnight": cfg.overnight,
        "reentry_cooldown_bars": cfg.reentry_cooldown_bars,
        "confirm_flip": cfg.confirm_flip,
        "invert_signals": bool(getattr(cfg, "invert_signals", False)),
        "ensemble_entry_tf": getattr(cfg, "ensemble_entry_tf", "5min"),
        "ensemble_entry_from_setups": bool(getattr(cfg, "ensemble_entry_from_setups", True)),
        "ensemble_direction_sid": getattr(cfg, "ensemble_direction_sid", ""),
        "source": "file",
    }

_sandbox_broker = None

def _get_sandbox_broker():
    global _sandbox_broker
    if _sandbox_broker is None:
        from app.bot.live_broker import LiveBroker
        _sandbox_broker = LiveBroker(SessionLocal)
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
    commission_rate: float | None = None
    overnight: bool | None = None
    reentry_cooldown_bars: int | None = None
    confirm_flip: int | None = None
    invert_signals: bool | None = None
    ensemble_entry_tf: str | None = None
    ensemble_entry_from_setups: bool | None = None
    ensemble_direction_sid: str | None = None


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
    if req.ensemble_entry_tf is not None and req.ensemble_entry_tf in ("1min", "5min", "10min", "15min"):
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
    if changes:
        runtime._log("⚙ КОНФИГ: " + " | ".join(changes))
    await save_bot_settings(cfg)
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
    for key in ("quorum", "neutral_mode", "vol_thr", "bias", "entry_tf"):
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


@router.post("/stop")
async def bot_stop() -> dict:
    return await runtime.stop()


class ModeRequest(BaseModel):
    mode: str  # sandbox | live | test
    test_name: str = ""
    replay_start: str = ""  # ISO UTC (обязателен для mode=test)
    replay_end: str = ""
    replay_pace: str = "fast"


def _write_env_mode(mode: str, test_name: str = "", replay_start: str = "", replay_end: str = "") -> None:
    """Обновить BOT_MODE (и параметры теста) в backend/.env — переживают рестарт uvicorn."""
    import os
    from pathlib import Path
    env_path = Path(__file__).resolve().parents[3] / ".env"
    pairs = {"BOT_MODE": mode, "BOT_TEST_NAME": test_name,
             "BOT_TEST_START": replay_start, "BOT_TEST_END": replay_end}
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
    except Exception:
        pass


def _build_autostart_cfg(mode: str, test_name: str = "", replay_start: str = "",
                         replay_end: str = "", replay_pace: str = "fast") -> BotConfig:
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
    _write_env_mode(mode, test_name=name, replay_start=req.replay_start.strip(),
                    replay_end=req.replay_end.strip())
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
        await runtime.start(_build_autostart_cfg(
            mode, test_name=name,
            replay_start=req.replay_start.strip(),
            replay_end=req.replay_end.strip(),
            replay_pace="fast"))
    except Exception as e:
        raise HTTPException(500, f"restart failed: {e}")
    return {"mode": mode, "test_name": name or None, "restarted": was_running}


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


@router.get("/logconfig")
async def bot_logconfig_get() -> dict:
    return {"log_candles": runtime.log_candles, "buffer_max": runtime._live_logs.maxlen}


@router.post("/logconfig")
async def bot_logconfig_set(payload: dict) -> dict:
    if "log_candles" in payload:
        runtime.log_candles = bool(payload["log_candles"])
    return {"log_candles": runtime.log_candles}


@router.post("/logs/clear")
async def bot_logs_clear() -> dict:
    runtime._live_logs.clear()
    return {"cleared": True}


@router.get("/logs")
async def bot_logs(limit: int = 200) -> dict:
    logs = list(runtime._live_logs)[-limit:]
    return {"logs": logs, "count": len(logs)}


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
        from app.api.routes.sandbox import _portfolio_digest
        dig = await _portfolio_digest()
        if dig:
            portfolio = dig
    except Exception:
        portfolio = {}

    return {
        **status,
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


@router.get("/trades")
async def bot_trades(limit: int = 50) -> dict:
    trades = await runtime.broker.trades_history(min(limit, 500))
    return {
        "count": len(trades),
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
        items.append(TestRunInfo(
            name=name,
            replay_start=min(entries).isoformat() if entries else "",
            replay_end=max(entries).isoformat() if entries else "",
            created_at=min(entries).isoformat() if entries else "",
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
    """Удалить прогон теста (все сделки mode='paper' с этим test_name)."""
    from sqlalchemy import delete as _delete
    from app.database import SessionLocal as _DB
    from app.models.sandbox_trade import SandboxTrade
    async with _DB() as db:
        r = await db.execute(
            _delete(SandboxTrade)
            .where(SandboxTrade.mode == "paper", SandboxTrade.test_name == name)
        )
        await db.commit()
        return {"deleted": int(r.rowcount or 0)}


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