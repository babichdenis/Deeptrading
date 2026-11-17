from __future__ import annotations

import asyncio
import contextvars
import json
import os
import time as _time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

from sqlalchemy import select

from app.bot import runtime_diagnostics
from app.bot.runtime_diagnostics import audit_swallow as _audit_swallow
from app.bot.runtime_models import BotConfig, BotOrder
from app.bot.runtime_persistence import RuntimePersistenceMixin
from app.bot.runtime_telemetry import RuntimeTelemetryMixin
from app.bot.events import EventLog
from app.bot.feed import STEP_SEC, CandleFeed
from app.bot.replay_feed import ReplayFeed
from app.bot.paper_broker import PaperBroker
from app.bot.live_broker import EntrySubmissionUnknown, LiveBroker
from app.bot.risk import RiskSnapshot
from app.bot.stream_manager import StreamManager

_AUDIT_SWALLOW_SEEN = runtime_diagnostics.AUDIT_SWALLOW_SEEN


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
from app.engine.exits import (AtrStopPolicy, FixedSlTpPolicy, PLAUSIBLE_LEVEL_SPAN,
                             is_fraction, level_implausible, level_problem)
from app.engine.models import Candle as EngineCandle, ExitPlan, PositionState, Side
from app.engine.policies import SignalPolicy
from app.engine.strategies import build_strategy
from app.models.instrument import Instrument
from app.models.paper import PaperAccount, PaperTrade
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




# Поля BotConfig, которые сохраняются в БД и восстанавливаются при старте бота.
BOT_PERSIST_FIELDS = (
    "initial_cash", "qty_per_trade",
    "sessions", "long_allowed", "short_allowed", "use_margin", "leverage",
    "margin_sessions", "margin_leverage", "margin_sizing",
    "trade_regimes", "trend_alignment",
    "trail_distance_atr", "trail_compress_r", "trail_min_factor", "trail_min_atr", "trail_vol_boost",
    "trail_activation_comm_mult", "trail_info_activation_comm_mult",
    "stop_pct", "target_pct", "sl_mode", "initial_sl_atr", "atr_period", "atr_multiplier",
    "trail_activation_comm_mult", "trail_distance_atr", "trail_compress_r",
    "trail_min_factor", "trail_min_atr", "trail_vol_boost",
    "atr_risk_reward", "top_n", "ensemble_quorum", "commission_rate",
    "overnight", "eod_close_min_before", "weekend_flat", "bias_exit_enabled", "daily_bias", "daily_bias_mode",
    "mtf_align", "mtf_trigger", "ensemble_require_member",
    "entry_h1_align", "entry_tf_conflict", "entry_last_hour_block",
    "entry_hm_veto", "hm_veto_window_h", "hm_veto_thr_pct", "hm_veto_mode",
    "momentum_short", "momentum_n", "momentum_k", "momentum_stop_pct",
    "momentum_entry_time", "momentum_max_lev", "momentum_only", "momentum_side",
    "reentry_cooldown_bars", "confirm_flip", "invert_signals", "flip_to_opposite", "ensemble_entry_tf", "ensemble_entry_from_setups", "ensemble_direction_sid",
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
    # Гейты стакана и L/S-баланса (найдены Signal Trace 01.10: глушили половину
    # входов в тест-прогонах; 0 = гейт выключен — см. gates.gate_orderbook/ls_balance).
    "entry_ob_imbalance_max": 0.0,
    "entry_ob_spread_max": 0.0,
    "max_short_share": 0.0,
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
    ("trail_compress_r", ("exits", "trail_compress_r")),
    ("trail_min_factor", ("exits", "trail_min_factor")),
    ("trail_min_atr", ("exits", "trail_min_atr")),
    ("trail_vol_boost", ("exits", "trail_vol_boost")),
    ("reentry_cooldown_bars", ("entry", "cooldown_bars")),
    ("confirm_flip", ("entry", "confirm_flip")),
    ("entry_last_hour_block", ("entry", "last_hour_block")),
    ("entry_volatility_max_mult", ("entry", "volatility_max_mult")),
    ("signal_trace", ("trace", "enabled")),
    ("trade_regimes", ("regimes",)),
)
_MISS = object()


def _giveback_step(upnl: float, peak: float | None, notional: float,
                   act_pct: float, giv_pct: float) -> tuple[float, bool]:
    """Пик close-based upnl и триггер trail-гивбэка (opt-in).

    Активация: пик ≥ act_pct% от notional. Триггер: текущий upnl ≤ пик − giv_pct% от notional.
    Пик обновляется первым (running max, как в SQL-симуляции), затем проверка триггера.
    """
    new_peak = upnl if peak is None else max(float(peak), upnl)
    if act_pct <= 0.0 or giv_pct <= 0.0 or notional <= 0.0:
        return new_peak, False
    activated = new_peak >= act_pct / 100.0 * notional
    return new_peak, bool(activated and upnl <= new_peak - giv_pct / 100.0 * notional)


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
    for fld, path in _PRESET_FIELD_PATHS:
        cur = rt
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                cur = _MISS
                break
            cur = cur[k]
        if cur is _MISS:
            continue
        if fld == "trade_regimes":
            if isinstance(cur, str) and cur.strip().lower() in ("all", "", "any"):
                continue
            if not isinstance(cur, (list, tuple)):
                continue
        if fld in ("sessions", "margin_sessions"):
            if not isinstance(cur, list) or not cur:
                continue
        out[fld] = cur
    return out


def _universe_v2_mode() -> str:
    """research/test: UNIVERSE_MODE=v2_trend|v2_meanrev (.env → settings).

    Пусто = legacy-отбор (compat). Действует только в тест-контуре
    (см. ветку выбора универса в _startup).
    """
    try:
        from app.config import get_settings as _gs
        return str(getattr(_gs(), "universe_mode", "") or "").strip().lower()
    except Exception:
        return ""


async def _move_select_universe(figi_by_ticker: dict[str, str], cfg, as_of, log) -> list[dict]:
    """Отбор бумаг по силе хода за N дней (move_select).

    Включается только через конфиг (move_select_enabled) и только в реплее.
    Возвращает [] если выключено — тогда работает прежний legacy/v2-отбор.
    """
    try:
        from app.config import get_settings as _gs
        from app.bot.universe.move_select import try_select_move_universe as _try
        from app.bot.universe.move_select import LiquidityEvidenceUnavailable
    except Exception:
        return []
    settings = _gs()
    cfg_path = str(getattr(settings, "move_select_config", "") or "") or None
    preset = None
    try:
        from app.bot.universe.move_select import load_move_select_config as _load
        preset = _load(cfg_path)
    except Exception as exc:
        log(f"move_select: конфиг не читается ({exc})", "warn")
    if not (getattr(settings, "move_select_enabled", False)
            or (preset is not None and preset.enabled)):
        return []
    try:
        async with SessionLocal() as db:
            return await _try(
                db, figi_by_ticker,
                feed=str(getattr(cfg, "feed", "live")),
                as_of=as_of,
                emit=log,
                config_path=cfg_path,
                enabled=True,
            )
    except LiquidityEvidenceUnavailable:
        raise
    except Exception as exc:
        log(f"move_select: сбой ({exc}), берём прежний юниверс", "warn")
        return []


def apply_test_overrides(cfg, locked: set[str] | None = None) -> list[str]:
    """Применить тестовые оверрайды (mode=test). Возвращает список применённых.

    env: TEST_GATES=on|off (гейты как на .2 / выкл), TEST_VARIANT=base|macd1|macd2.

    locked — поля, явно сохранённые из UI (BOT_PERSIST_FIELDS). Для research-прогонов,
    где пресет обязан перебить UI, задать env TEST_PRESET_FORCE=1.

    Пресет всегда применяется последним и без ui-lock (решение владельца):
    пресет — это то, что он хочет тестировать. Расхождения с UI не молчат —
    их отдаёт preset_ui_conflicts(), и UI показывает модалкой после старта.
    """
    import os as _os
    applied: list[str] = []
    _force = str(_os.environ.get("TEST_PRESET_FORCE", "") or "").strip().lower() in ("1", "true", "on", "yes")
    _gates = str(_os.environ.get("TEST_GATES", "on") or "on").lower()
    _variant = str(_os.environ.get("TEST_VARIANT", "base") or "base").lower()
    _src = dict(TEST_MODE_OVERRIDES)
    _src.update(TEST_GATES_ON if _gates != "off" else TEST_GATES_OFF)
    _src.update(TEST_VARIANTS.get(_variant, {}))
    # Движок теста: env TEST_ENGINE=ose_all -> одиночная стратегия из реестра
    # (use_ensemble=False), иначе ensemble_v4 с кворумом из ensemble_config.test.json.
    _engine = str(_os.environ.get("TEST_ENGINE", "") or "").strip().lower()
    if _engine and _engine != "ensemble_v4":
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
    #
    # РЕШЕНИЕ ВЛАДЕЛЬЦА (02.10): тестировать надо то, что в пресете, НО он хочет
    # видеть разницу. Поэтому выбор делается ДО старта в модалке и приходит сюда
    # через TEST_PRESET_MODE (см. run_state.preset_mode):
    #   "preset" — едем по пресету, он перебивает сохранённые ползунки;
    #   "ui"     — едем по ползункам интерфейса, пресет НЕ применяется;
    #   ""/нет   — пресет выбран, но режим не задан (старые вызовы/API) → как "preset".
    # Расхождения в обоих случаях показываем модалкой (preset_ui_conflicts).
    _pmode = str(_os.environ.get("TEST_PRESET_MODE", "") or "").strip().lower()
    _use_ui = _pmode == "ui"
    _preset_src: dict = {}
    _tps = str(_os.environ.get("TEST_PRESET", "") or "").strip()
    if _tps and _use_ui:
        # Выбран режим «по UI»: пресет намеренно не применяем, но сам факт выбора
        # и конфликт остаются в логах, чтобы результат не читался как «пресет не сработал».
        applied.append("preset=SKIPPED (режим: по настройкам UI)")
        try:
            import json as _json2
            _pres = _json2.loads(_tps)
            _rt = _pres.get("runtime") if isinstance(_pres, dict) else None
            if isinstance(_rt, dict):
                _preset_src = _runtime_preset_overrides(_rt)  # только для конфликтов
        except Exception as _tpr_e:
            _audit_swallow('apply_test_overrides@test_preset', _tpr_e)
    elif _tps:
        try:
            import json as _json2
            _pres = _json2.loads(_tps)
            _rt = _pres.get("runtime") if isinstance(_pres, dict) else None
            if isinstance(_rt, dict):
                _preset_src = _runtime_preset_overrides(_rt)
                applied.append("preset=runtime")
        except Exception as _tpr_e:
            _audit_swallow('apply_test_overrides@test_preset', _tpr_e)
    for _k, _v in _src.items():
        if locked and _k in locked and not _force:
            applied.append(f"ui-lock:{_k}")
            continue
        try:
            setattr(cfg, _k, _v)
            applied.append(f"{_k}={_v}")
        except Exception as _sw_e:
            _audit_swallow('apply_test_overrides@L371', _sw_e)  # audit silent-except
            pass
    # Пресет — последним и без ui-lock: в режиме «по пресету» он главный.
    if _preset_src and not _use_ui:
        for _k, _v in _preset_src.items():
            try:
                setattr(cfg, _k, _v)
                applied.append(f"preset:{_k}={_v}")
            except Exception as _sw_e:
                _audit_swallow('apply_test_overrides@preset', _sw_e)  # audit silent-except
    return applied


def preset_ui_conflicts(saved: dict | None = None) -> list[dict]:
    """Расхождения «настройки UI vs пресет» для модалки выбора режима.

    Возвращает [{field, ui, preset}] — только те поля, которые владелец явно
    сохранил из UI (bot_settings) и которые пресет перебивает своим значением.
    Считается независимо от выбранного режима: даже при «еду по UI» полезно
    показать, чем владелец жертвует.
    """
    import os as _os
    _tps = str(_os.environ.get("TEST_PRESET", "") or "").strip()
    if not _tps or not isinstance(saved, dict):
        return []
    try:
        import json as _json3
        _pres = _json3.loads(_tps)
        _rt = _pres.get("runtime") if isinstance(_pres, dict) else None
    except Exception as _pc_e:
        _audit_swallow('preset_ui_conflicts@parse', _pc_e)
        return []
    if not isinstance(_rt, dict):
        return []
    out: list[dict] = []
    for _k, _pv in _runtime_preset_overrides(_rt).items():
        if _k not in saved:
            continue
        _uv = saved.get(_k)
        if _uv == _pv:
            continue
        if isinstance(_uv, (list, tuple)) or isinstance(_pv, (list, tuple)):
            if list(_uv or []) == list(_pv or []):
                continue
        out.append({"field": _k, "ui": _uv, "preset": _pv})
    return out


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




def _new_order_id() -> str:
    return str(uuid.uuid4())


def _as_position_side(value) -> str:
    """Order direction (BUY/SELL) as a position side (LONG/SHORT); positions pass through."""
    return {"BUY": "LONG", "SELL": "SHORT"}.get(value, value)


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


from app.engine.sessions import is_session_active as _sessions_allowed, should_force_close as _should_force_close, weekend_close_due as _weekend_close_due


# --- Ручные переопределения юниверса -------------------------------------
# Раньше путь к файлу был зашит в двух местах, и воспроизводимый прогон на
# замороженном наборе данных молча подхватывал состояние рабочей машины.
# Теперь путь задаётся переменной окружения, по умолчанию — прежний.
# Файл читается на каждом баре, поэтому содержимое кэшируется по (путь, mtime,
# размер): иначе это лишний ввод-вывод в горячем пути расчёта.
_UNIVERSE_OVERRIDES_ENV = "DEEPTRADING_UNIVERSE_OVERRIDES"
_UNIVERSE_OVERRIDES_DEFAULT = Path(r"X:\run\custom_universe.json")
_universe_overrides_cache: dict = {"key": None, "items": None}


def universe_overrides_path() -> Path:
    """Путь к файлу ручных переопределений юниверса (env перекрывает умолчание)."""
    raw = (os.environ.get(_UNIVERSE_OVERRIDES_ENV) or "").strip()
    return Path(raw) if raw else _UNIVERSE_OVERRIDES_DEFAULT


def load_universe_overrides() -> list | None:
    """Переопределения юниверса или None, если файла нет, он пуст или битый.

    Отсутствие файла — штатное состояние, ошибки не пробрасываются.
    """
    if not (os.environ.get(_UNIVERSE_OVERRIDES_ENV) or "").strip():
        _universe_overrides_cache["key"] = None
        _universe_overrides_cache["items"] = None
        return None
    path = universe_overrides_path()
    try:
        st = path.stat()
    except OSError:
        _universe_overrides_cache["key"] = None
        _universe_overrides_cache["items"] = None
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _universe_overrides_cache["key"] == key:
        return _universe_overrides_cache["items"]
    items = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            items = [d for d in data if isinstance(d, dict) and d.get("figi")]
    except (OSError, ValueError):
        items = None
    _universe_overrides_cache["key"] = key
    _universe_overrides_cache["items"] = items
    return items


_MISSING_ACCOUNT = "__missing_account__"


def _account_scope_of(rt) -> str | None:
    """Идентификатор брокерского счёта для sandbox_trades (live/sandbox) — для ЧТЕНИЯ.

    ARCH155: строки live/sandbox должны различаться по счёту, а не только по
    mode. Для paper/replay идентичность задаёт test_name, счёт здесь не нужен
    и остаётся NULL (возвращаем None).

    Если контур live/sandbox, а счёт неизвестен — возвращаем sentinel
    `_MISSING_ACCOUNT`, а не None: равенство с ним не матчит ни реальные счета,
    ни legacy NULL-строки, поэтому выборка возвращает пусто, а не чужую историю.
    """
    mode = getattr(rt, "broker_mode", None)
    if mode not in ("live", "sandbox"):
        return None
    acct = getattr(getattr(rt, "broker", None), "_account", None)
    if acct in (None, ""):
        return _MISSING_ACCOUNT
    return str(acct)


def _account_scope_or_raise(rt) -> str | None:
    """Идентификатор счёта для ЗАПИСИ live/sandbox-сделок.

    Неизвестный счёт у live/sandbox — ошибка конфигурации: писать в NULL (legacy)
    или в чужой счёт нельзя, поэтому поднимаем ValueError; вызывающий метод
    прерывает запись. Для paper/replay возвращает None (там identity = test_name).
    """
    mode = getattr(rt, "broker_mode", None)
    if mode not in ("live", "sandbox"):
        return None
    acct = getattr(getattr(rt, "broker", None), "_account", None)
    if acct in (None, ""):
        raise ValueError("live/sandbox mutation without broker account identity")
    return str(acct)


class PaperBotRuntime(RuntimeTelemetryMixin, RuntimePersistenceMixin):
    def _entry_pause(self):
        from app.bot.entry_pause import EntryPause
        state = getattr(self, '_entry_pause_control', None)
        if state is None:
            state = EntryPause()
            self._entry_pause_control = state
        return state

    @property
    def entries_paused(self):
        return self._entry_pause().paused

    @entries_paused.setter
    def entries_paused(self, value):
        self._entry_pause().manual = bool(value)

    def _pause_close(self, scope, request_id, active=True):
        self._entry_pause().set_blocker('close_reconciliation', active, *scope, request_id)

    def _database_session(self):
        return SessionLocal()

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
        self._trace = None  # SignalTraceEmitter (P0), включается флагом SIGNAL_TRACE/signal_trace
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
        # Итог завершённого реплея: виртуальные часы снимаются в _finalize_replay,
        # иначе статус вечно показывал active=true на застывшем проценте.
        self._replay_finished: bool = False
        self._replay_status: str | None = None  # P1.2: терминальный статус финализации
        self._replay_summary: dict | None = None
        # Расхождения «настройки UI vs пресет» последнего старта (mode=test):
        # пресет главный, но список показывается в UI модалкой сразу после старта.
        self._preset_conflicts: list[dict] = []
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
        self._log_persist_queue: deque[tuple[str, str, str, str, str]] = deque(maxlen=2000)  # (level, source, msg, ts_msk, request_id)
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
        self._task_errors: dict[str, dict] = {}
        # --- Метрики движка (для мониторинга/автореакции в status) ---
        self.metrics: dict = {
            "started_ts": None,
            "bar_ms_total": 0.0, "bar_ms_n": 0, "bar_ms_max": 0.0,
            "ensemble_ms_total": 0.0, "ensemble_ms_n": 0, "ensemble_ms_max": 0.0,
            "persist_ms_total": 0.0, "persist_ms_n": 0, "persist_ms_max": 0.0,
            "persist_failures": 0, "persist_dropped": 0, "persist_queue_depth": 0,
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
        self._task_registry: dict[str, asyncio.Task] = {}
        self.log_candles = True
        self._last_candle_log_ts: float = 0.0
        # --- 5m resample cache (trailing stop optimization) ---
        self._5m_cache: dict[str, list] = {}  # figi -> [resampled 5m candles]
        self._5m_last_close: dict[str, int] = {}
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
        self._gb_peak: dict[str, float] = {}   # figi -> пик close-based upnl (₽) для trail-гивбэка
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
                       turnover=(None if str(getattr(self.config, "feed", "")) == "replay"
                                 else (self._turnover_cache or {}).get(tk)),
                       votes=int(qe.get("votes") or 0),
                       total_members=int(qe.get("total_members") or 0),
                       vol_ratio=vol_ratio,
                       regime=str(reg.get("state") or "neutral"),
                       fit=self._fit_score(snap, tk),
                       min_turnover=self._min_turnover(snap),
                       history_veto=bool(getattr(self.config, "queue_history_veto", True)))
        except Exception as _sw_e:
            _audit_swallow('_strength_for@L1134', _sw_e)
            return {"score": 0.0, "factors": {}, "veto": ["candidate_inputs_unknown"]}

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
                               figis: set[str] | None = None,
                               force: bool = False) -> dict:
        """Закрыть долю позиций (худшие по P&L) — трейлинг-стоп портфеля/разворот/ночь.

        force=True — закрыть мимо сессионного гейта: выходные не отменяют
        требование «не пережить выходные» (там гейт иначе просто не пустит).
        """
        if not force and not self._in_trading_session():
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
        unresolved = []
        for pnl, p, bb, last in items[:n]:
            try:
                outcome = await self._submit_close_reconciled(
                    p.figi, getattr(p, "ticker", ""), p.qty, last, reason, paper_filled=False)
                if outcome["status"] == "reconciled_closed":
                    self._held.discard(bb)
                    if bb != p.figi:
                        self._clear_exit_state(bb)
                    closed.append({"figi": p.figi, "ticker": getattr(p, "ticker", ""),
                                   "price": None, "net_pnl": None, "order_id": outcome["order"].id})
                    continue
                if outcome["status"] == "pending_partial":
                    self._exit_qty[bb] = outcome["residual"]
                if outcome["status"] != "closed":
                    unresolved.append({"figi": p.figi, "status": outcome["status"]})
                    continue
                trade = outcome["trade"]
            except Exception as e:
                self._log(f"ОШИБКА ЗАКРЫТИЯ {getattr(p, 'ticker', '')}: {type(e).__name__}")
                unresolved.append({"figi": p.figi, "status": "unresolved_error"})
                continue
            self._held.discard(p.figi)
            self._held.discard(bb)
            self._clear_exit_state(bb)
            _net = float(trade.net_pnl) if trade else None
            _px = float(getattr(trade, "exit_price", None) or getattr(trade, "price", None)) if (trade and (getattr(trade, "exit_price", None) or getattr(trade, "price", None))) else last
            try:
                await self._st_close(p.figi, _px, execution=trade, reason=reason, net=_net,
                                     meta={"source": "reduce_positions", "close_pct": close_pct})
            except Exception as _e:
                self._log(f"ST_CLOSE FAIL {getattr(p, 'ticker', '')}: {type(_e).__name__}")
            closed.append({"figi": p.figi, "ticker": getattr(p, "ticker", ""),
                           "pnl": round(pnl, 2), "price": round(_px, 6),
                           "net_pnl": _net})
            self.events.log("POSITION_CLOSED", figi=p.figi, ticker=getattr(p, "ticker", ""),
                            reason=reason, net_pnl=_net)
        return {"closed": len(closed), "positions": closed, "reason": reason,
                "unresolved": unresolved}

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
                # --- Weekend flat: перед выходными закрыть всё (overnight не спасает).
                try:
                    if bool(getattr(cfg, "weekend_flat", True)) and self._held:
                        from app.engine.sessions import weekend_close_due as _wk_due_fn
                        if _wk_due_fn(self._bot_now(), cfg.sessions):
                            _wk_figs = set(self._held) - self._swing
                            r = (await self.reduce_positions(1.0, "weekend_force_close",
                                                             figis=_wk_figs, force=True)
                                 if _wk_figs else {})
                            if r.get("closed"):
                                _now_msk = self._bot_now().astimezone(timezone(timedelta(hours=3)))
                                self._log(f"📅 WEEKEND: закрыто {r['closed']} поз. перед выходными "
                                          f"({_now_msk.strftime('%a %d.%m %H:%M')} МСК)")
                                self.events.log("WEEKEND_CLOSE", closed=r["closed"])
                except Exception as _e_wk:
                    _audit_swallow('_portfolio_guard_loop@weekend', _e_wk)  # audit silent-except
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
        from app.services.loghub import hub, msk_now_str, _redact
        ts = msk_now_str()
        # Реплей/тест: время в логе = ВИРТУАЛЬНОЕ время бота (чтобы видеть дату/время свечей).
        try:
            if str(getattr(self.config, "feed", "")) == "replay":
                ts = self._bot_now().astimezone(timezone(timedelta(hours=3))).strftime(
                    "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        msg = _redact(msg)
        rid = _request_id_ctx.get()
        if rid:
            msg = f"[rid={rid}] {msg}"
        hub.push(msg, level=level, source=source, ts=ts)
        # Персистентная копия (level/source/msg/ts) — пишется в bot_logs флашером,
        # переживает рестарт и видна в Live через фильтры UI.
        # НЕ в реплее (умолчание): тест гонит десятки тысяч баров, запись в БД
        # стоит ~235мс/бар, история логов прогона не нужна (в памяти кольцо).
        # replay_log_persist=True — писать логи теста в bot_logs (прогон виден
        # в истории логов UI, переживает рестарт).
        if (str(getattr(self.config, "feed", "")) != "replay"
                or bool(getattr(self.config, "replay_log_persist", False))):
            # RECHECK P1: RID фиксируем на enqueue (producer boundary) — ContextVar
            # фоновой persist-task'а не соответствует задаче, создавшей запись.
            self._log_persist_queue.append((level, source, msg, ts, rid))


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

    def _last_session_close(self, figi: str, fallback: float) -> float:
        """Цена последней СЕССИОННОЙ свечи — цена выхода перед выходными.

        В уикенд T-Invest отдаёт бары (тонкие/пустые), цена там не рыночная,
        поэтому выходные приравниваем к последней пятничной цене.
        """
        try:
            bb = self.tcs_to_bbg.get(figi, figi)
            buf = self.buffers.get(bb) or self.buffers.get(figi) or []
            for _b in reversed(list(buf)[-300:]):
                _ts = getattr(_b, "ts", None)
                if _ts is not None and _sessions_allowed(_ts, self.config.sessions):
                    return float(_b.close)
        except Exception as _sw_e:
            _audit_swallow('_last_session_close', _sw_e)  # audit silent-except
        return float(fallback)


    def _get_5m_bars(self, figi: str, buf_list: list) -> list:
        """Return cached 5m bars, rebuild only when new 5m bar closes."""
        if not buf_list:
            return []
        last_close = self._5m_last_close.get(figi, -1)
        current_5m = int(buf_list[-1].ts.timestamp()) // 300 if hasattr(buf_list[-1], "ts") else 0
        if current_5m != last_close:
            from app.services.ensemble import resample as _resample5u
            self._5m_cache[figi] = _resample5u(buf_list, 300)
            self._5m_last_close[figi] = current_5m
        return self._5m_cache.get(figi, [])

    def _is_closed(self, c) -> bool:
        """Страж на «бар дошёл до потребителя», а не на полноту интервала.

        Канон проекта: метка бара = НАЧАЛО бакета (см. ADR 0002), т.е. бар с
        меткой ts закрывается в ts + ТФ. Формально «уже закрыт» означало бы
        now >= c.ts + ТФ, но здесь сравнение now >= c.ts — и это НЕ описка,
        а другая (рантаймная) конвенция: «бар с меткой ts отдан в работу в
        момент ts». Обе конвенции несовместимы, и сменить гейт на ts + ТФ
        нельзя: в реплее _bot_now() возвращает метку последней поданной
        свечи, поэтому stricter-вариант отсёк бы ВСЕ бары и бот не торговал бы
        (parity 0). Гарантию закрытия здесь даёт не сравнение, а источник:
        feed отдаёт только закрытые бары (waiting_close), а незакрытые
        отсекает гейт свежести по wall-clock ниже по потоку.

        Не менять это сравнение молча: правка = отдельное решение владельца
        вместе с переводом реплей-часов на ts + ТФ.
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
        _skip_side = ""
        try:
            for _c in load_universe_overrides() or []:
                if _c.get("figi") == figi:
                    if _c.get("side") == "short":
                        _skip_side = "BUY"
                    elif _c.get("side") == "long":
                        _skip_side = "SELL"
                    break
        except Exception:
            _skip_side = ""
        return EnsembleParams(
        figi=figi, lot=int(lot) if lot else 10, capital=capital,
        quorum=int(ec.get("quorum", 2)), session="all", sessions=sessions,
        drop_useless=bool(ec.get("drop_useless", False)),
        setups=_setups,
        sl_mult=(float(ec.get(_sl_key) or 0.0) or float(getattr(self.config, "ensemble_sl_mult", 0.0) or 0.0) or float(opt.get("sl_mult", 2.0))),
        rr=(float(ec.get(_rr_key) or 0.0) or float(getattr(self.config, "ensemble_rr", 0.0) or 0.0) or float(opt.get("rr", 4.0))),
        trail_activation_comm_mult=(lambda _v: (0.0 if _v >= 1e6 else _v) or None)(
            float(ec.get("trail_activation_comm_mult") or 0.0)
            or float(getattr(self.config, "trail_activation_comm_mult", 0.0) or 0.0)),
        trail_distance_r=float(getattr(self.config, "trail_distance_atr", 0.0) or 0.0) or 2.5,
        trail_compress_r=float(getattr(self.config, "trail_compress_r", 0.0) or 0.0) or 1.0,
        trail_min_factor=float(getattr(self.config, "trail_min_factor", 0.0) or 0.0) or 0.6,
        trail_min_atr=float(getattr(self.config, "trail_min_atr", 0.0) or 0.0) or 1.5,
        trail_vol_boost=float(getattr(self.config, "trail_vol_boost", 0.0) or 0.0) or 0.3,
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
            skip_entry_side=_skip_side,
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

    async def _daily_turnover(self, ticker: str) -> float | None:
        """Дневной оборот тикера (VALTODAY из TQBR quotes, кэш 5 мин).

        В universe.avg_daily_turnover лежит не дневной оборот (мелкие значения),
        поэтому ликвидность считаем по живым котировкам биржи.
        """
        if str(getattr(self.config, "feed", "")) == "replay":
            return None
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

    def _entry_card(self, figi: str, meta: dict | None, regime: str | None) -> dict:
        """Каноническая карточка входа для любого движка: причина, голоса, режим.

        Ансамбль (bot/ensemble_strategy.py) и OSE-стратегии (engine/ose/strategy.py)
        карточку уже приносят в features — тогда только добираем режим. Одиночные
        движки (rsi_trade_hub, моментум, AI, очередь) приносят голые фичи
        ({"rsi": 37.09}), из-за чего в таблице «Последние сделки» вход был пустой:
        не видно ни кто голосовал, ни в каком режиме входили. Здесь пишем голос
        движка как есть — один движок = один голос (требование владельца: голос
        хоть одного робота должен попадать в карточку).

        Ключи meta повторяют формат ансамбля: entry.reason/entry.features + плоские
        rsi*/голоса, quorum_event, regime — их читает фронт (renderTrades,
        tradeDetailsHtml) и bot.py.
        """
        out = dict(meta or {})
        sig = out.pop("_sig", None)
        sig = sig if isinstance(sig, dict) else {}
        strat = None
        try:
            strat = self.strategies.get(figi)
        except Exception:
            strat = None
        sid = str(sig.get("strategy_id") or getattr(strat, "strategy_id", "")
                  or getattr(self.config, "strategy_id", "") or "")
        robot = str(getattr(strat, "robot_name", "") or sid or "?")
        is_buy = str(sig.get("side") or "").upper() in ("BUY", "LONG")
        reg = regime or out.get("entry_regime") or out.get("regime")
        if isinstance(out.get("entry"), dict) and out.get("entry"):
            # Движок карточку уже принёс (ансамбль/OSE) — не перетираем.
            if reg and not out.get("regime"):
                out["regime"] = reg
            if sid and not out.get("strategy_id"):
                out["strategy_id"] = sid
            return out
        flat = {k: v for k, v in out.items()
                if k not in ("leverage", "gate_path", "atr_entry", "entry_regime",
                             "sl_initial", "entry_price0", "entry_time", "regime",
                             "quorum_event", "buy_votes", "sell_votes", "votes",
                             "vote_detail", "entry")}
        reason = str(sig.get("reason") or "").strip() or (f"{sid}:signal" if sid else "signal")
        entry = {
            "reason": reason[:128],          # колонка entry_reason VARCHAR(128)
            "side": sig.get("side"),
            "ts": sig.get("ts"),
            "strategy_id": sid,
            "robot": robot,
            "kind": sig.get("kind"),
            "votes": 1,
            "buy_votes": 1 if is_buy else 0,
            "sell_votes": 0 if is_buy else 1,
            "vote_detail": f"{robot}:{reason}"[:120],
            "features": flat,
        }
        entry.update(flat)                   # rsi/rsi_5m/rsi_sig — как у ансамбля
        out["entry"] = entry
        out["quorum_event"] = {
            "ts": sig.get("ts"),
            "side": sig.get("side"),
            "votes": 1,
            "buy_votes": entry["buy_votes"],
            "sell_votes": entry["sell_votes"],
            "members_for": [robot] if is_buy else [],
            "opposition": [] if is_buy else [robot],
            "total_members": 1,
            "quorum_k": 1,
            "reason": reason[:120],
        }
        out["votes"] = 1
        out["buy_votes"] = entry["buy_votes"]
        out["sell_votes"] = entry["sell_votes"]
        out["vote_detail"] = entry["vote_detail"]
        out["engine"] = "single"             # режим входа: одиночный движок, не ансамбль
        out["strategy_id"] = sid
        if reg:
            out["regime"] = reg
        return out

    async def _entry_lot_size(self, figi, meta):
        lot = (meta or {}).get("lot_size")
        if type(lot) is int and lot > 0:
            return lot
        if isinstance(self.broker, LiveBroker):
            raise ValueError("confirmed_entry_lot_missing")
        from app.models.instrument import Instrument
        async with SessionLocal() as db:
            lot = (await db.execute(
                select(Instrument.lot).where(Instrument.figi == figi)
            )).scalar_one_or_none()
        if type(lot) is not int or lot <= 0:
            raise ValueError("entry_lot_unknown")
        return lot

    async def _st_open(self, figi, ticker, side, qty, price, sl, tp, meta: dict | None = None, leverage: float = 1.0, entry_evidence=None) -> None:
        ledger_mode = self.broker_mode
        ledger_account = _account_scope_or_raise(self)
        try:
            if str((meta or {}).get("hold") or "").lower() == "swing":
                self._swing.add(str(figi))
                self._log(f"SWING {ticker}: долгая сделка — EOD/ночь не закрывает")
        except Exception as _sw_e:
            _audit_swallow('_st_open@L1545', _sw_e)  # audit silent-except
            pass
        from app.models.sandbox_trade import SandboxTrade
        import json as _json
        from decimal import Decimal
        _lot = await self._entry_lot_size(figi, meta)
        qty_shares = int(qty) * _lot
        _er = (self._regimes.get(figi) or {}).get("state")
        # _regimes[figi]["state"] может быть dict (timeline-запись от стратегии):
        # всегда нормализуем до строки — в _entry_regime и в meta сделки, иначе
        # в таблицу позиций уходит сырой dict (режим = "{'state': 'TREND_UP', ...}").
        _er_norm = (_er.get("state") if isinstance(_er, dict) else _er) or "NO_REGIME"
        self._entry_regime[figi] = _er_norm
        # Карточка входа (reason/голоса/режим) — для ЛЮБОГО движка, см. _entry_card.
        _meta_row = self._entry_card(figi, meta, _er_norm)
        client_order_id = str((meta or {}).get("client_order_id") or "")
        if client_order_id:
            _meta_row["client_order_id"] = client_order_id
            _meta_row["lot_size"] = _lot
        entry_time = None
        if ledger_mode in ('live', 'sandbox'):
            from app.services.broker_fill_evidence import entry_evidence_metadata
            evidence_meta = entry_evidence_metadata(entry_evidence, mode=ledger_mode,
                broker_account_id=ledger_account, client_order_id=client_order_id, figi=figi,
                side=side, units=qty_shares, lot_size=_lot, price=price)
            _meta_row.update(evidence_meta)
            if evidence_meta['entry_time_confirmed']:
                entry_time = datetime.fromisoformat(evidence_meta['entry_time_confirmed'])
        import logging as _lg
        _lg.getLogger("app.bot.runtime").info(
            "ST_OPEN enter figi=%s mode=%s test=%s", figi, ledger_mode,
            getattr(self.config, "test_name", "") or "")
        try:
            async with SessionLocal() as db:
                # Идемпотентность: один открытый ряд на figi. Если уже есть
                # открытая строка того же инструмента — сначала архивируем её
                # (защита от двойных записей при переоткрытии фиджи).
                _now = self._bot_now()
                prev = (await db.execute(
                    select(SandboxTrade)
                    .where(SandboxTrade.figi == figi, SandboxTrade.exit_time.is_(None),
                           SandboxTrade.mode == ledger_mode,
                           SandboxTrade.test_name == (getattr(self.config, "test_name", "") or None),
                           SandboxTrade.broker_account_id == ledger_account)
                    .order_by(SandboxTrade.entry_time.desc())
                )).scalars().all()
                if client_order_id:
                    history = (await db.execute(select(SandboxTrade).where(
                        SandboxTrade.figi == figi,
                        SandboxTrade.mode == ledger_mode,
                        SandboxTrade.broker_account_id == ledger_account,
                    ))).scalars().all()
                    matching = []
                    for row in history:
                        row_meta = _json.loads(row.meta or "{}")
                        if row_meta.get("client_order_id") == client_order_id:
                            matching.append((row, row_meta))
                    if len(matching) > 1:
                        raise ValueError("duplicate_entry_identity")
                    if matching:
                        row, row_meta = matching[0]
                        if (row.exit_time is not None or row.side != side
                                or row_meta.get("lot_size") != _lot
                                or qty_shares < row.qty):
                            raise ValueError("entry_execution_conflict")
                        same_execution = (row.qty == qty_shares
                            and Decimal(str(row.entry_price)).quantize(Decimal('0.000001'))
                            == Decimal(str(price)).quantize(Decimal('0.000001')))
                        row.qty = qty_shares
                        row.entry_price = float(price)
                        row_meta["entry_price0"] = float(price)
                        if ledger_mode in ('live', 'sandbox'):
                            if (entry_evidence is not None or not same_execution
                                    or row_meta.get('entry_time_source') != 'broker_history'):
                                row_meta.update(evidence_meta)
                            row_meta['entry_observed_at'] = _now.isoformat()
                            if entry_time is not None:
                                row.entry_time = entry_time
                                row_meta['entry_time'] = entry_time.isoformat()
                        row.meta = _json.dumps(row_meta, ensure_ascii=False, default=str)
                        await db.commit()
                        return
                    if prev:
                        raise ValueError("another_open_entry_exists")
                for _old in prev:
                    _old.exit_time = _now
                    _old.exit_price = None
                    _old.exit_reason = "reopened"
                    _old.net_pnl = None
                    await db.flush()
                db.add(SandboxTrade(
                    figi=figi, ticker=ticker, side=side, qty=qty_shares,
                    entry_time=entry_time or _now,
                    entry_price=float(price),
                    stop_loss=float(sl) if sl else None,
                    take_profit=float(tp) if tp else None,
                    entry_reason=(str(_meta_row.get("entry", {}).get("reason") or "")[:128] or None),
                    meta=_json.dumps({**_meta_row, "sl_initial": float(sl) if sl else None,
                                      "entry_price0": float(price),
                                      "entry_regime": _er_norm,
                                      "entry_time": (entry_time or _now).isoformat(),
                                      "entry_observed_at": _now.isoformat()}, ensure_ascii=False, default=str),
                    leverage=float(leverage),
                    mode=ledger_mode,
                    test_name=getattr(self.config, "test_name", "") or None,
                    broker_account_id=ledger_account,
                ))
                await db.commit()
                _lg.getLogger("app.bot.runtime").info(
                    "ST_OPEN persist ok figi=%s mode=%s test=%s", figi,
                    ledger_mode, getattr(self.config, "test_name", "") or "")
                # Funnel: фактический вход (executed_entries) — инкремент стратегии figi
                try:
                    _st_f = self.strategies.get(figi)
                    if _st_f is not None and hasattr(_st_f, "_funnel_stats"):
                        _st_f._funnel_stats["executed_entries"] = int(_st_f._funnel_stats.get("executed_entries", 0) or 0) + 1
                except Exception:
                    pass
        except Exception as _sw_e:
            _lg.getLogger("app.bot.runtime").error(
                "ST_OPEN persist FAILED figi=%s mode=%s test=%s: %r",
                figi, ledger_mode,
                getattr(self.config, "test_name", "") or "", _sw_e, exc_info=True)
            raise

    async def _st_close(self, figi, exit_price, reason="", net=None, meta: dict | None = None, execution=None) -> None:
        from app.models.sandbox_trade import SandboxTrade
        from app.engine.costs import CostModel
        import json as _json
        try:
            async with SessionLocal() as db:
                res = await db.execute(
                    select(SandboxTrade)
                    .where(SandboxTrade.figi == figi, SandboxTrade.exit_time.is_(None),
                           SandboxTrade.mode == self.broker_mode,
                           SandboxTrade.test_name == (getattr(self.config, "test_name", "") or None),
                           SandboxTrade.broker_account_id == _account_scope_of(self))
                    .order_by(SandboxTrade.entry_time.desc())
                    .limit(1)
                )
                row = res.scalar_one_or_none()
                import logging as _lgk
                if row is None:
                    _lgk.getLogger("app.bot.runtime").warning(
                        "ST_CLOSE: нет открытой строки figi=%s mode=%s test=%s (открытие не записалось?)",
                        figi, self.broker_mode, getattr(self.config, "test_name", "") or "")
                if row is not None:
                    paper_settlement = None
                    if isinstance(self.broker, PaperBroker):
                        from app.bot.paper_cashflow import settle_paper_exit
                        net = None
                        if execution is not None and int(execution.qty) == int(row.qty):
                            exit_price = float(execution.exit_price)
                            paper_settlement = settle_paper_exit(
                                float(execution.gross_pnl), float(execution.commission),
                                float(execution.entry_commission) if execution.entry_commission is not None else None,
                            )
                            net = paper_settlement.total_net
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
                            # MAE ДО ПИКА в % от входа (не путать с mae_pct выше —
                            # это максимум за всю сделку, включая провалы после пика).
                            try:
                                meta["max_pnl_mae_pct"] = round(float(_peak.get("mae_pct") or 0.0), 2)
                            except Exception:
                                meta["max_pnl_mae_pct"] = 0.0
                        except Exception as _sw_e:
                            _audit_swallow('_st_close@peak_mae', _sw_e)  # audit silent-except
                            pass
                    row.exit_meta = _json.dumps(meta, ensure_ascii=False, default=str)
                    if net is not None or isinstance(self.broker, PaperBroker):
                        row.net_pnl = float(net) if net is not None else None
                    costs = CostModel(commission_rate=self.config.commission_rate, slippage_bps=self.config.slippage_bps)
                    entry_comm = costs.commission(float(row.entry_price) * int(row.qty))
                    exit_comm = costs.commission(float(exit_price) * int(row.qty))
                    row.commission = round(entry_comm + exit_comm, 4)
                    if isinstance(self.broker, PaperBroker):
                        row.commission = paper_settlement.total_commission if paper_settlement else None
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
            import logging as _lgc
            _lgc.getLogger("app.bot.runtime").error(
                "ST_CLOSE persist FAILED figi=%s mode=%s test=%s: %r",
                figi, self.broker_mode,
                getattr(self.config, "test_name", "") or "", _sw_e, exc_info=True)
            _audit_swallow('_st_close@L1630', _sw_e)  # audit silent-except
            pass

    async def _st_update_sl(self, figi: str, sl: float, trail_active: bool | None = None) -> None:
        from decimal import Decimal
        from app.models.sandbox_trade import SandboxTrade

        broker = self.broker
        mode = self.broker_mode
        account = _account_scope_of(self)
        test_name = getattr(self.config, "test_name", "") or None
        sessions = broker.sessions if isinstance(broker, LiveBroker) else SessionLocal
        def same_scope():
            return (self.broker is broker and self.broker_mode == mode and _account_scope_of(self) == account
                    and (broker.mode == mode if isinstance(broker, LiveBroker)
                         else (getattr(self.config, "test_name", "") or None) == test_name))
        try:
            if not same_scope():
                raise ValueError('Protection update scope mismatch')
            stop = Decimal(str(sl)) if sl is not None and not isinstance(sl, bool) else None
            if (sl is not None and (stop is None or not stop.is_finite() or stop <= 0)
                    or trail_active is not None and type(trail_active) is not bool):
                raise ValueError('Invalid protection update')
            async with sessions() as db:
                async with db.begin():
                    rows = (await db.scalars(select(SandboxTrade).where(
                        SandboxTrade.figi == figi, SandboxTrade.exit_time.is_(None),
                        SandboxTrade.mode == mode, SandboxTrade.broker_account_id == account,
                        *([] if isinstance(broker, LiveBroker) else [SandboxTrade.test_name == test_name])
                    ).with_for_update())).all()
                    if not same_scope():
                        raise ValueError('Protection update scope changed')
                    if len(rows) != 1:
                        raise ValueError('Unique open position required for protection update')
                    row = rows[0]
                    if stop is not None:
                        row.stop_loss = stop
                    if trail_active is not None:
                        row.trailing_active = trail_active
                        if trail_active:
                            row.take_profit = None
            if not same_scope():
                raise ValueError('Protection scope changed after commit')
            self._entry_pause().set_blocker('protection_persistence_failed', False, mode, account, figi)
        except Exception as error:
            self._entry_pause().set_blocker('protection_persistence_failed', True, mode, account, figi)
            self.events.log('PROTECTION_PERSISTENCE_FAILED', figi=figi, error_type=type(error).__name__)

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
                        SandboxTrade.mode == self.broker_mode,
                        SandboxTrade.broker_account_id == _account_scope_of(self))
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
            _init_sl = getattr(p, "initial_sl", None)
            _plan_px = getattr(p, "plan_entry_price", None)
            _atr_entry = getattr(p, "atr_at_entry", None)
            _mult = getattr(p, "atr_multiplier", None)
            _esl_atr = None
            if _init_sl is not None and _atr_entry:
                _base = float(_plan_px) if _plan_px is not None else float(entry or 0)
                _esl_atr = abs(_base - float(_init_sl)) / float(_atr_entry) if _base else None
            out["positions"].append({
                "figi": bb, "ticker": self.tickers.get(bb, getattr(p, "ticker", "") or bb[-6:]),
                "protection_reference": (getattr(self, '_intrabar_reference_status', {}).get(bb)
                    if getattr(self, '_intrabar_reference_scope', None) ==
                    (getattr(self.broker, 'mode', None), getattr(self.broker, '_account', None)) else None),
                "side": "LONG" if long_ else "SHORT", "qty": qty,
                "entry": entry, "last": last, "pnl": pnl,
                "sl": sl, "tp": tp,
                "dist_sl_pct": d_sl, "dist_tp_pct": d_tp,
                "atr": _atr, "dist_sl_atr": _dsa, "dist_tp_atr": _dta,
                "initial_sl": float(_init_sl) if _init_sl is not None else None,
                "plan_entry_price": float(_plan_px) if _plan_px is not None else None,
                "atr_at_entry": float(_atr_entry) if _atr_entry is not None else None,
                "entry_sl_atr": _esl_atr,
                "atr_tf": getattr(p, "atr_tf", None),
                "atr_period": getattr(p, "atr_period", None),
                "atr_as_of": str(getattr(p, "atr_as_of", None) or "") or None,
                "atr_multiplier": float(_mult) if _mult is not None else None,
                "policy_source": getattr(p, "policy_source", None),
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

    async def _intrabar_handle(self, figi: str, px: float) -> str:
        """One intrabar protection check for a single FIGI.

        Returns the classified outcome. Position, protection and entry bookkeeping are
        cleared only on a confirmed full close, so an unacknowledged submission can never
        look like a completed exit.
        """
        sl = self._trail_stop.get(figi)
        tp = self._exit_target.get(figi)
        if sl is None and tp is None:
            return "no_plan"
        side = self._exit_side.get(figi) or "LONG"
        long_ = str(side).upper() in ("LONG", "BUY")
        hit_sl = bool(sl) and ((long_ and px <= sl) or (not long_ and px >= sl))
        hit_tp = bool(tp) and ((long_ and px >= tp) or (not long_ and px <= tp))
        if not (hit_sl or hit_tp):
            return "no_hit"
        pos = await self.broker.get_position(figi)
        if pos is None:
            return "no_position"
        # Вне сессии биржа/sandbox не принимает заявки (30079) —
        # не дёргаем брокера, выход дожидается открытия сессии.
        if not self._in_trading_session():
            self._session_gate_log("intrabar-выход")
            return "session_closed"
        reason = "intrabar_sl" if hit_sl else "intrabar_tp"
        qty = int(self._exit_qty.get(figi) or getattr(pos, "qty", 0) or 0)
        ticker = getattr(pos, "ticker", "") or self.tickers.get(figi, "")
        outcome = await self._submit_close_reconciled(figi, ticker, qty, float(px), reason)
        status = outcome["status"]
        if status == "reconciled_closed":
            return "closed"
        if status == "pending_partial":
            self._exit_qty[figi] = int(outcome["residual"])
        if status != "closed":
            self._log(f"intrabar {figi[-6:]} {status} — позиция и защита сохранены",
                      level="warn")
            return status
        trade = outcome["trade"]
        exit_px = float(getattr(trade, "exit_price", None) or px)
        self._held.discard(figi)
        self._opposite_count.pop(figi, None)
        self._last_exit_bar[figi] = self._bar_counter
        self._peak_pnl.pop(figi, None)
        self._clear_exit_state(figi)
        bars_held = self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter)
        await self._st_close(figi, exit_px, execution=trade, reason=reason,
                             net=float(trade.net_pnl),
                             meta={"bars_held": bars_held, "intrabar": True})
        self._log(f"INTRABAR ВЫХОД {figi[-6:]} {reason} @ {exit_px:.2f} "
                  f"pnl={float(trade.net_pnl):+.2f}")
        self.events.log("ORDER_FILLED", figi=figi, reason=reason,
                        price=exit_px, net_pnl=float(trade.net_pnl), intrabar=True)
        return "closed"

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
                if isinstance(self.broker, LiveBroker):
                    from app.bot.intrabar_reference import check_intrabar_references
                    await check_intrabar_references(self)
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
                        await self._intrabar_handle(figi, float(px))
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
        (для замера времени теста). После финализации отдаёт замороженный итог
        (active=false, finished=true), а не None — иначе UI не отличит «завершён»
        от «не запускался». Безопасен: любая ошибка -> None,
        статус никогда не ломается.
        """
        try:
            mode = str(self.mode or "")
            is_test = mode == "test" or mode.startswith("test:")
            is_replay = str(getattr(self.config, "feed", "")) == "replay"
            if not (is_test or is_replay):
                return None
            _d = self._replay_status_dict()
            if not _d.get("active") and not _d.get("finished"):
                return None
            _d["wall_start"] = self.started_at.isoformat() if self.started_at else None
            return _d
        except Exception:
            return None

    def _replay_status_dict(self) -> dict:
        """Блок replay для статуса: живой прогресс ИЛИ замороженный итог прогона.

        После `_finalize_replay` виртуальные часы снимаются (`_replay_from/_cur`):
        иначе статус вечно показывал `active=true` на застывшем проценте — прогон
        завершался, а UI и монитор думали, что он ещё идёт. Итог живёт в
        `_replay_summary` (окно, процент, сколько позиций закрыто финалом).
        """
        _pace = str(getattr(self.config, "replay_pace", "") or "")
        if self._replay_from is not None and self._replay_cur is not None:
            _pct = self._replay_progress_pct()
            return {
                "active": True, "finished": False, "status": None,
                "start": self._replay_from.isoformat(),
                "end": self._replay_to.isoformat() if self._replay_to else None,
                "now": self._replay_cur.isoformat(),
                "now_msk": self._replay_cur.astimezone(timezone(timedelta(hours=3))).isoformat(),
                "pct": round(_pct, 2) if _pct is not None else None,
                "pace": _pace, "closed": None,
            }
        _sum = getattr(self, "_replay_summary", None) or {}
        return {
            "active": False, "finished": bool(getattr(self, "_replay_finished", False)),
            "status": getattr(self, "_replay_status", None) or (getattr(self, "_replay_summary", None) or {}).get("status"),
            "start": _sum.get("start"), "end": _sum.get("end"), "now": _sum.get("last_ts"),
            "now_msk": _sum.get("last_msk"), "pct": _sum.get("pct"),
            "pace": _pace, "closed": _sum.get("closed"),
        }

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
            "entry_pause_reasons": self._entry_pause().reasons,
            "session": session_state(),
            "data": {
                "health": health,
                "source": self.data_source,
                "last_candle_ts": self.last_candle_ts.isoformat() if self.last_candle_ts else None,
            },
            "replay": self._replay_status_dict(),
            # Пресет главный; здесь — что он перебил в настройках UI (модалка в UI).
            "preset_conflicts": list(getattr(self, "_preset_conflicts", []) or []),
            "risk": {
                "state": risk.state,
                "daily_pnl": round(risk.daily_pnl, 2) if risk.daily_pnl is not None else None,
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

    def _daily_pnl_scope(self) -> tuple | None:
        mode = getattr(self, 'broker_mode', None)
        broker = getattr(self, 'broker', None)
        if mode in ('live', 'sandbox'):
            account = getattr(broker, '_account', None)
            if getattr(broker, 'mode', None) == mode and isinstance(account, str) and account.strip():
                return mode, id(broker), account
        elif mode == 'paper' and broker is not None:
            return mode, id(broker), getattr(self.config, 'test_name', None)
        return None

    def daily_pnl_cached(self) -> float | None:
        from math import isfinite
        cache = getattr(self, '_daily_pnl_cache', None)
        scope = self._daily_pnl_scope()
        if not cache or scope is None or scope != getattr(self, '_daily_pnl_cache_scope', None):
            return None
        now = self._bot_now()
        saved, value = cache
        msk = timezone(timedelta(hours=3))
        if now.utcoffset() is None or saved.utcoffset() is None:
            return None
        if not timedelta(0) <= now - saved < DAILY_PNL_TTL or now.astimezone(msk).date() != saved.astimezone(msk).date():
            return None
        return float(value) if value is not None and isfinite(float(value)) else None

    async def _ensure_risk_fresh(self) -> bool:
        if not hasattr(self, '_risk_refresh_lock'):
            self._risk_refresh_lock = asyncio.Lock()
        async with self._risk_refresh_lock:
            if self.daily_pnl_cached() is not None and 'risk_evidence_unavailable' not in self._entry_pause().reasons:
                return True
            try:
                await self._check_circuit_breaker()
            except Exception:
                return False
            return self.daily_pnl_cached() is not None

    async def refresh_daily_pnl(self) -> float:
        from math import isfinite
        from sqlalchemy import or_
        as_of = self._bot_now()
        msk_now = as_of.astimezone(timezone(timedelta(hours=3)))
        day_start = msk_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        mode = str(getattr(self, 'broker_mode', '') or '')
        broker = self.broker
        scope = self._daily_pnl_scope()
        if scope is None:
            raise ValueError("daily_pnl_scope_unavailable")
        account = _account_scope_or_raise(self) if mode in ('sandbox', 'live') else None
        sessions = broker.sessions
        async with sessions() as db:
            if mode in ('sandbox', 'live'):
                from app.models.sandbox_trade import SandboxTrade
                query = select(SandboxTrade.net_pnl).where(
                    SandboxTrade.exit_time >= day_start, SandboxTrade.exit_time <= as_of,
                    SandboxTrade.mode == mode, SandboxTrade.broker_account_id == account,
                    or_(SandboxTrade.exit_reason.is_(None), SandboxTrade.exit_reason != 'duplicate_cleanup'))
            else:
                from app.bot.paper_broker import DEFAULT_ACCOUNT
                account_id = await db.scalar(select(PaperAccount.id).where(PaperAccount.name == DEFAULT_ACCOUNT))
                if account_id is None:
                    raise ValueError('paper_risk_account_missing')
                query = select(PaperTrade.net_pnl).where(PaperTrade.account_id == account_id,
                    PaperTrade.exit_time >= day_start, PaperTrade.exit_time <= as_of)
            values = (await db.execute(query)).scalars().all()
        if self._daily_pnl_scope() != scope:
            raise ValueError('broker_scope_changed_during_daily_pnl')
        if any(value is None or not isfinite(float(value)) for value in values):
            raise ValueError('daily_pnl_contains_unknown_outcomes')
        total = sum(float(value) for value in values)
        self._daily_pnl_cache = (as_of, total)
        self._daily_pnl_cache_scope = scope
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

    def _reset_run_state(self) -> None:
        self.universe = []
        self.stream_universe = []
        self.orders = deque(maxlen=200)
        self.candles_seen = 0
        self.signals_seen = 0
        self._candles_received = 0
        self._last_q_report_ts = 0.0
        self._persist_flushes = 0
        self.last_candle_ts = None
        self.data_source = "—"
        self.feed = None
        self._daily_pnl_cache = None
        self._replay_from = None
        self._replay_cur = None
        self._replay_to = None
        self._last_replay_log = -1.0
        self._replay_finished = False
        self._replay_status = None
        self._replay_summary = None
        self._signal_busy = set()
        self._skip_logged = {}
        self._entry_runtime_requests = {}
        self._held = set()
        self._swing = set()
        self._peak_pnl = {}
        self._gb_peak = {}
        self._trail_info = {}
        self._held_since = {}
        self._last_closed_bar = {}
        self._last_persist_bar = {}
        self._opposite_count = {}
        self._last_signal_side = {}
        self._bias_exit_ts = {}
        self._last_exit_bar = {}
        self._bar_counter = 0
        self._just_opened_this_candle = set()
        self._entry_bar_index = {}
        self._no_trade_stats = {}
        self._regimes = {}
        self._entry_regime = {}
        self._votes = {}
        self._votes_logged = {}
        self._exit_plans = {}
        self._exit_side = {}
        self._exit_entry_px = {}
        self._exit_qty = {}
        self._trail_active = {}
        self._trail_stop = {}
        self._exit_target = {}
        self._prev_close = {}
        self._day_jumps = {}
        self._bad_day = {}
        self._candles_rejected = 0
        self._imoex_buf = []
        self._imoex_state = None
        self._imoex_last_tick = ""
        self._imoex_tick_err = ""
        self._imoex_beta = {}
        self._imoex_stale_warn_ts = 0.0
        self._approvals_since = {}
        self._loss_streak = {}
        self._last_loss_ts = {}
        self._global_loss_streak = 0
        self._global_last_loss_ts = None
        self._ai_reject_until = {}
        self._ai_reject_reason = {}
        self._ai_reject_last_log = {}
        self._ai_reject_cnt = {}
        self._pos_leverage = {}
        self._sector_meta = {}
        self._sector_meta_ts = 0.0
        self._pf_cache = None
        self._regime_cache = None
        self._hist_cache = {}
        self._hist_ts = 0.0
        self._turnover_cache = {}
        self._turnover_ts = 0.0
        self._cand_queue = {}
        self._equity_peak = 0.0
        self._mtf_cache = {}
        self._mtf_ts = 0.0
        self._hm_cache = {}
        self._hm_ts = 0.0
        self._momentum_done = None
        self._momentum_cache = {}
        self._momentum_ts = 0.0
        self._daily_bias_cache = {}
        self._daily_bias_ts = 0.0
        self._overnight_cache = None
        self._overnight_ts = 0.0
        self._dd_level_done = 0
        self._ai_decisions = deque(maxlen=50)
        self._ai_notes = deque(maxlen=50)
        self._ai_prompt = {}
        self._ai_report = {}
        self._move_selected = None
        self._move_leaving = set()
        self._5m_cache = {}
        self._5m_last_close = {}
        self._last_candle_log_ts = 0.0
        self._persist_queue.clear()
        self._persist_queue_5m.clear()
        self._log_persist_queue.clear()

    async def start(self, cfg: BotConfig) -> dict:
        # ensemble_v4 — не одиночная стратегия: всегда ensemble-путь в тесте.
        if str(getattr(cfg, 'mode', '')) == 'test' and str(getattr(cfg, 'strategy_id', '')) == 'ensemble_v4':
            cfg.use_ensemble = True
        if self.running or self.starting:
            raise RuntimeError("bot already running")
        self.config = cfg
        self._reset_run_state()
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
                # Настройки UI (сохранёнка) ВЫШЕ пресета: поля, сохранённые из UI,
                # пресет не трогает (решение владельца, 02.10). TEST_PRESET_FORCE=1 — наоборот.
                _locked: set[str] = set()
                _saved_ui: dict = {}
                try:
                    _saved = await load_bot_settings()
                    _saved_ui = dict(_saved or {})
                    _locked = {k for k in _saved_ui.keys() if k in BOT_PERSIST_FIELDS}
                except Exception:
                    _locked = set()
                _ov = apply_test_overrides(cfg, locked=_locked)
                if _ov:
                    self._log("ТЕСТ-ОВЕРРАЙДЫ: " + ", ".join(_ov[:10]))
                # Расхождения «UI vs пресет» — пресет главный, но владелец должен
                # УВИДЕТЬ, что его ползунки перебиты (модалка сразу после старта).
                self._preset_conflicts = preset_ui_conflicts(_saved_ui)
                if self._preset_conflicts:
                    self._log(
                        "ПРЕСЕТ ПЕРЕБИЛ UI: " + ", ".join(
                            f"{c['field']}(ui={c['ui']}→{c['preset']})"
                            for c in self._preset_conflicts[:8]))
            except Exception as _sw_e:
                _audit_swallow('start@test_overrides', _sw_e)
        # --- Replay: стартуем виртуальные часы с начала окна (до первой свечи). ---
        self._replay_from = None
        self._replay_cur = None
        self._replay_finished = False
        self._replay_summary = None
        self._replay_status = None
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
                await _db.execute(_text("CREATE TABLE IF NOT EXISTS bot_logs (id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), level VARCHAR(8) DEFAULT 'info', source VARCHAR(64) DEFAULT 'bot', msg TEXT, request_id VARCHAR(32))"))
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
            "persist_failures": 0, "persist_dropped": 0, "persist_queue_depth": 0,
            "last_alert": None,
            "alerts": [],
        }
        self.strategies = {}
        self.buffers = {}
        self.pending_orders = {}
        self.tickers = {}
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
                commission_rate=cfg.commission_rate, slippage_bps=cfg.slippage_bps), clock=self._bot_now)
        else:
            self.broker = LiveBroker(SessionLocal, config=cfg)
            await self._restore_close_intents()
            await self._restore_open_intents()
            await self._restore_close_after_open_requests()
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
                    _del_res = await db.execute(_q)
                    await db.commit()
                    _deleted = int(getattr(_del_res, "rowcount", 0) or 0)
                self._log(f"REPLAY RESET: чистая бумажная книга ({cfg.initial_cash:.0f} ₽)"
                          + f" · удалено сделок: {_deleted}"
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
                            # Отметки завершения прогона (появились позже самой таблицы).
                            await db.execute(_btr_text(
                                "ALTER TABLE bot_test_runs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ"))
                            await db.execute(_btr_text(
                                "ALTER TABLE bot_test_runs ADD COLUMN IF NOT EXISTS closed_replay INT DEFAULT 0"))
                            await db.execute(_btr_text(
                                "INSERT INTO bot_test_runs (name, replay_start, replay_end, updated_at) "
                                "VALUES (:n, :s, :e, now()) "
                                "ON CONFLICT (name) DO UPDATE SET "
                                "replay_start = EXCLUDED.replay_start, "
                                "replay_end = EXCLUDED.replay_end, updated_at = now(), "
                                "finished_at = NULL, closed_replay = 0"
                            ), {"n": cfg.test_name, "s": self._replay_from, "e": _btr_end})
                            await db.commit()
                    except Exception as _sw_e:
                        _audit_swallow('start@bot_test_runs', _sw_e)
            except Exception as e:
                self._log(f"REPLAY RESET FAIL: {type(e).__name__}: {str(e)[:80]}")
        await self.broker.ensure_account(cfg.initial_cash)
        await self._check_circuit_breaker()
        self.starting = True
        self.startup_task = asyncio.create_task(self._startup(cfg))
        return {"started": True, "async": True}

    async def _init_signal_trace(self, cfg) -> None:
        """Signal Trace: эмиттер включается cfg.signal_trace (preset trace.enabled) или env SIGNAL_TRACE;
        DB-sink (фаза 1) — env SIGNAL_TRACE_DB=1."""
        self._trace = None
        self._trace_writer = None
        try:
            from app.config import get_settings as _gs
            _s = _gs()
            if not (bool(getattr(cfg, "signal_trace", False)) or bool(getattr(_s, "signal_trace", False))):
                return
            import os as _os
            from uuid import uuid4
            from app.engine.trace import RunInfo, Stage, Status, TraceEvent
            from app.services.signal_trace import SignalTraceEmitter, short_hash
            _preset_raw = _os.environ.get("TEST_PRESET", "") or ""
            _cfg_keys = ("strategy_id", "interval_name", "sessions", "overnight", "pos_pct",
                         "max_positions", "sl_mode", "stop_pct", "target_pct", "atr_period",
                         "atr_multiplier", "atr_risk_reward", "confirm_flip",
                         "reentry_cooldown_bars", "trade_regimes")
            run = RunInfo(
                run_id=uuid4().hex,
                run_key=str(getattr(cfg, "test_name", "") or f"live:{datetime.now(timezone.utc).isoformat()}"),
                contour="runtime",
                mode=str(getattr(cfg, "mode", "")),
                feed=str(getattr(cfg, "feed", "")),
                test_name=str(getattr(cfg, "test_name", "")),
                strategy_id=str(getattr(cfg, "strategy_id", "")),
                interval=str(getattr(cfg, "interval_name", "")),
                replay_from=str(getattr(cfg, "replay_start", "")),
                replay_to=str(getattr(cfg, "replay_end", "")),
                preset_hash=short_hash(_preset_raw) if _preset_raw else "",
                config_hash=short_hash({k: getattr(cfg, k, None) for k in _cfg_keys}),
            )
            _writer = None
            if bool(getattr(_s, "signal_trace_db", False)):
                from app.services.signal_trace import SqlTraceWriter
                _writer = SqlTraceWriter(run)
                await _writer.start()
            self._trace_writer = _writer
            self._trace = SignalTraceEmitter(
                run,
                directory=(str(getattr(_s, "signal_trace_dir", "") or "") or None),
                db_writer=(_writer.write if _writer is not None else None),
            )
            self._trace.start()
            self._trace.emit(TraceEvent(stage=Stage.RUN_OPEN, status=Status.CREATED))
            self._log(f"SIGNAL-TRACE: пишу {self._trace.path}")
        except Exception as _sw_e:
            _audit_swallow("_init_signal_trace", _sw_e)
            self._trace = None

    async def _close_trace(self, reason: str = "") -> None:
        """Финальный флаш Signal Trace (идемпотентно)."""
        _tr = getattr(self, "_trace", None)
        if _tr is None:
            return
        try:
            from app.engine.trace import Stage, Status, TraceEvent
            _tr.emit(TraceEvent(stage=Stage.RUN_CLOSE, status=Status.EXECUTED, reason=reason))
            _summ = await _tr.aclose()
            _wr = getattr(self, "_trace_writer", None)
            if _wr is not None:
                try:
                    await _wr.close(_summ)
                except Exception:
                    pass
            self._log(f"SIGNAL-TRACE: {_summ.get('events')} событий, dropped={_summ.get('dropped')}, "
                      f"файл {_summ.get('path')}")
        except Exception as _sw_e:
            _audit_swallow("_close_trace", _sw_e)
        finally:
            self._trace = None
            self._trace_writer = None

    def _trace_order_reject(self, figi: str, ticker: str, action: str, side: str,
                            reason_code: str, note: str = "", error: dict | None = None,
                            ts_bar=None) -> None:
        """Signal Trace: ордер принят, но не исполнен (FILL/REJECTED + причина)."""
        if self._trace is None:
            return
        try:
            from app.engine.trace import Stage, Status, TraceEvent
            self._trace.emit(TraceEvent(stage=Stage.FILL, status=Status.REJECTED, ts_bar=ts_bar,
                                        figi=figi, ticker=ticker, action=action, side=side,
                                        reason=note or None, reason_code=reason_code, error=error or {}))
        except Exception:
            pass

    async def _candle_preflight(self) -> None:
        """Watchdog 1m-данных окна теста: check (+докачка T-Invest).

        env CANDLE_PREFLIGHT=off|warn|fill|strict; default — warn для mode=test/feed=replay
        (реплей читает БД как есть; докачка при необходимости включается явным env).
        """
        import os as _os
        mode = str(_os.environ.get("CANDLE_PREFLIGHT", "") or "").strip().lower()
        if not mode:
            mode = "warn" if (str(getattr(self.config, "feed", "")) == "replay"
                              and str(getattr(self.config, "mode", "")) == "test") else "off"
        if mode == "off":
            return
        try:
            from app.services.candle_integrity import preflight_universe
            figis = [u.get("figi") for u in (self.universe or []) if u.get("figi")]
            if not figis:
                return
            warm = 10 if bool(getattr(self.config, "use_ensemble", False)) else 3
            start = self._replay_from or self._bot_now()
            end = self._replay_to or start
            rep = await preflight_universe(figis, start - timedelta(days=warm), end + timedelta(days=1),
                                           fix=(mode in ("fill", "strict")))
            bad = [r for r in rep if r["status"] in ("DEFICIT_DAY", "MISMATCH")]
            self._log(f"CANDLE-PREFLIGHT({mode}): фиг {len(figis)}, проблем {len(bad)}"
                      + (" (докачка при необходимости)" if mode in ("fill", "strict") else ""))
            for r in bad[:10]:
                self._log(f"  {r['status']} {r['figi'][-6:]} {r['day']} "
                          f"rows={r['rows']} missing={r['missing_active']}")
            if bad and mode == "strict":
                raise RuntimeError(f"CANDLE-PREFLIGHT strict: {len(bad)} проблемных (figi,day)")
        except RuntimeError:
            raise
        except Exception as e:
            self._log(f"⚠ CANDLE-PREFLIGHT: {type(e).__name__}: {str(e)[:120]}")

    async def _refresh_broker_candidates(self):
        from app.services.tinvest import fetch_share_catalog
        from app.bot.universe.runtime_select import select_broker_candidates, constrain_manual_candidates

        manual = load_universe_overrides()
        mode = str(self.config.mode)
        now = datetime.now(timezone.utc)
        observed = getattr(self, "_candidate_observed_at", None)
        cached_mode = getattr(self, "_candidate_mode", None)
        if observed is None or cached_mode != mode or not timedelta(0) <= now - observed < timedelta(hours=1):
            catalog, observed = await asyncio.to_thread(fetch_share_catalog, mode)
        else:
            catalog = self._candidate_catalog
        async with SessionLocal() as db:
            candidates, decisions = await select_broker_candidates(
                db, catalog=catalog, observed_at=observed,
                as_of=datetime.now(timezone.utc),
                top_n=9999 if manual or self.config.use_ensemble else self.config.top_n,
                ensemble=bool(self.config.use_ensemble))
        if manual:
            candidates, decisions = constrain_manual_candidates(candidates, decisions, manual)
        self._candidate_catalog = catalog
        self._candidate_observed_at = observed
        self._candidate_mode = mode
        self._candidate_figis = {row["figi"] for row in candidates}
        self._candidate_decisions = decisions
        return candidates

    async def _startup(self, cfg: BotConfig) -> None:
        await self._init_signal_trace(cfg)
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
            from app.bot.universe.discovery import exact_instrument_maps
            self.bbg_to_tcs, self.tcs_to_bbg = exact_instrument_maps(
                figi_by_ticker, ii_rows.all())

            from app.bot.universe import select_eligible_universe, select_volatile_universe

            self._figi_by_ticker = dict(figi_by_ticker)
            _umode = _universe_v2_mode()
            _move_universe = await _move_select_universe(
                figi_by_ticker, cfg, self._replay_from or self._bot_now(),
                lambda msg, level="info": self._log(msg, level=level, source="universe"),
            )
            _custom_uni = load_universe_overrides()
            if _custom_uni:
                if str(cfg.mode) in ("live", "sandbox") and str(cfg.feed) != "replay":
                    self.universe = await self._refresh_broker_candidates()
                else:
                    self.universe = [dict(_c) for _c in _custom_uni]
                self._custom_locks = {
                    str(_c["figi"]): ("BUY" if _c.get("side") == "short" else "SELL")
                    for _c in self.universe if _c.get("side") in ("short", "long")}
                self._log(
                    "UNIVERSE-CUSTOM: %d бумаг · %s" % (
                        len(self.universe),
                        ", ".join("%s(%s)" % (str(_c.get("ticker", "?")), _c.get("side", ""))
                                 for _c in self.universe)),
                    level="info", source="universe")
            elif str(cfg.mode) in ("live", "sandbox") and str(cfg.feed) != "replay":
                self.universe = await self._refresh_broker_candidates()
            elif _move_universe:
                self.universe = _move_universe
                self._move_selected = {str(u["figi"]) for u in self.universe}
                self._move_leaving = set()
                _umode = "v2_move"
                self._log(
                    f"UNIVERSE-MOVE: {len(self.universe)} бумаг · "
                    + ", ".join(str(u.get("ticker", "?")) for u in self.universe),
                    level="info", source="universe")
            elif _umode.startswith("v2_") and str(cfg.mode) == "test":
                from app.bot.universe.runtime_select import select_screened_universe

                async with SessionLocal() as db:
                    self.universe = await select_screened_universe(
                        db, figi_by_ticker, mode=_umode, top_n=cfg.top_n,
                        as_of=self._replay_from or self._bot_now(),
                    )
                self._log(
                    f"UNIVERSE-V2 ({_umode}): {len(self.universe)} бумаг — "
                    f"{', '.join(u.get('ticker', '?') for u in self.universe) or 'пусто'}",
                    level="warn" if not self.universe else "info",
                )
            else:
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
            if _umode == "v2_move" or (_umode.startswith("v2_") and str(cfg.mode) == "test"):
                # v2-режим теста: подписка/стрим только на отобранный скринером набор
                self.stream_universe = [u["figi"] for u in self.universe]
            elif str(cfg.mode) in ("live", "sandbox") and str(cfg.feed) != "replay":
                self.stream_universe = [u["figi"] for u in self.universe]
            else:
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

            # --- Watchdog целостности свечей: preflight окна теста (CANDLE_PREFLIGHT) ---
            await self._candle_preflight()

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
                    try:
                        proto._trace_emitter = getattr(self, "_trace", None)
                    except Exception:
                        pass
                    buf = deque(maxlen=ENSEMBLE_BUFFER if cfg.use_ensemble else MAX_BUFFER)
                    async with SessionLocal() as db:
                        # В реплее свечи берём ТОЛЬКО из БД: окно историческое, API-догрузка
                        # не нужна и надолго стопорит старт (ensure_moex_candles days=10 × 30 тикеров).
                        _is_replay = str(getattr(cfg, "feed", "")) == "replay"
                        if cfg.use_ensemble:
                            if _is_replay:
                                await _replay_candles_coverage(db, u["figi"],
                                    getattr(cfg, "replay_start", ""),
                                    getattr(cfg, "replay_end", ""), self._log)
                            else:
                                from app.bot.moex import ensure_moex_candles
                                await ensure_moex_candles(u["figi"], u.get("ticker", ""), days=10)
                            candles = await _lc(db, u["figi"], 1,
                                                date_from=self._bot_now() - timedelta(days=10),
                                                date_to=self._bot_now())
                            for row in candles:
                                buf.append(EC(ts=row.ts, open=row.open, high=row.high,
                                              low=row.low, close=row.close, volume=row.volume))
                        else:
                            if _is_replay:
                                await _replay_candles_coverage(db, u["figi"],
                                    getattr(cfg, "replay_start", ""),
                                    getattr(cfg, "replay_end", ""), self._log)
                            else:
                                from app.services.candle_cache import ensure_candles
                                await ensure_candles(db, u["figi"], cfg.interval_name, days=7)
                            interval_value = self._interval_value()
                            candles = await _lc(db, u["figi"], interval_value,
                                                date_from=self._bot_now() - timedelta(days=3),
                                                date_to=self._bot_now())
                            # date_to ОБЯЗАТЕЛЕН: без него выборка тянет бары ПОСЛЕ окна
                            # реплея, [-MAX_BUFFER:] берёт их, ряд становится немонотонным
                            # и RSI-стратегия теряет канонические кроссы (найдено 01.10).
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

            # IMOEX (индекс): свежие 1м свечи в БД (режим/bias/MOEX-контекст); реплей — только БД
            try:
                _n_imoex = 0
                if str(getattr(self.config, "feed", "")) != "replay":
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

            if isinstance(self.broker, LiveBroker):
                await self._restore_live_position_exits()
            else:
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
        await self._close_trace("stop")
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
        for _attr in ('_dispatch_task', '_persist_task', '_held_sync_task', '_hot_add_task', '_vol_carousel_task',
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
        if isinstance(self.feed, CandleFeed) and getattr(self.config, "feed", "") != "replay":
            try:
                await asyncio.wait_for(self.feed._flush_persist(), timeout=5.0)
            except TimeoutError as exc:
                _audit_swallow("stop.feed_persist_timeout", exc)
        try:
            await asyncio.wait_for(self._flush_persist_once(), timeout=5.0)
        except Exception:
            pass
        self.startup_task = None
        self.task = None
        self.starting = False
        self.mode = "—"
        self.events.log("BOT_STOPPED", reason="manual" if was_running else "was_not_running")
        return {"stopped": True, "was_running": was_running}

    async def set_entries_paused(self, paused: bool) -> dict:
        previous_effective = self.entries_paused
        self.entries_paused = paused
        if not paused:
            try:
                await self._check_circuit_breaker()
            except Exception:
                self.events.log('ENTRY_RESUME_BLOCKED', reason='risk_evidence_unavailable')
        await save_bot_flags({"entries_paused": paused})
        event = ('PAUSE_NEW_ENTRIES' if self.entries_paused else 'RESUME_NEW_ENTRIES')
        if self.entries_paused == previous_effective:
            event = 'ENTRY_PAUSE_UPDATED'
        self.events.log(event, reason='manual', entry_pause_reasons=self._entry_pause().reasons)
        return {"entries_paused": self.entries_paused, "entry_pause_reasons": self._entry_pause().reasons}

    async def cancel_pending(self) -> dict:
        cancelled = []
        unresolved = []
        for figi, order in list(self.pending_orders.items()):
            if order.status in ("PENDING_RECONCILIATION", "PARTIAL_FILLED"):
                unresolved.append(order.to_dict())
                self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                ticker=order.ticker, order_id=order.id,
                                reason="local_cancel_cannot_confirm_broker_cancellation")
                continue
            order.status = "CANCELLED"
            cancelled.append(order.to_dict())
            del self.pending_orders[figi]
            self._approvals_since.pop(order.id, None)
            self.events.log("ORDER_CANCELLED", figi=figi, ticker=order.ticker,
                            reason="manual", order_id=order.id)
        return {"cancelled": len(cancelled), "orders": cancelled,
                "pending_reconciliation": len(unresolved), "unresolved_orders": unresolved}

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

    async def close_all(self, only_figi=None, reason="kill_switch_close_all") -> dict:
        if not self._in_trading_session():
            self._session_gate_log("закрытие всех позиций (kill-switch)")
            return {"closed": 0, "positions": []}
        broker = self.broker
        scope = (getattr(broker, 'mode', None), getattr(broker, '_account', None))
        positions = await broker.positions()
        if (self.broker is not broker
                or (getattr(broker, 'mode', None), getattr(broker, '_account', None)) != scope):
            return {"closed": 0, "positions": [], "pending": [{"figi": only_figi, "status": "broker_scope_changed"}]}
        if only_figi is not None:
            positions = [position for position in positions if position.figi == only_figi]
        closed = []
        pending = []
        for p in positions:
            if (self.broker is not broker
                    or (getattr(broker, 'mode', None), getattr(broker, '_account', None)) != scope):
                pending.append({"figi": p.figi, "status": "broker_scope_changed"})
                continue
            buf = self.buffers.get(p.figi)
            price = float(buf[-1].close) if buf else float(p.entry_price)
            qty = int(self._exit_qty.get(p.figi) or getattr(p, "qty", 0) or 0)
            outcome = await self._submit_close_reconciled(
                p.figi, p.ticker, qty, price, reason)
            status = outcome["status"]
            if status == "reconciled_closed":
                closed.append({"figi": p.figi, "ticker": p.ticker, "price": None,
                               "net_pnl": None, "order_id": outcome["order"].id})
                continue
            if status == "pending_partial":
                self._exit_qty[p.figi] = int(outcome["residual"])
            if status != "closed":
                pending.append({"figi": p.figi, "ticker": p.ticker, "status": status,
                                "residual": int(outcome.get("residual") or qty)})
                self._log(f"KILL-SWITCH: {p.ticker or p.figi[-6:]} не закрыт ({status}), "
                          "позиция и защита сохранены", level="warn")
                continue
            trade = outcome["trade"]
            _exit_px = float(getattr(trade, "exit_price", None)
                             or getattr(trade, "price", None) or price)
            self._held.discard(p.figi)
            self._peak_pnl.pop(p.figi, None)
            self._clear_exit_state(p.figi)
            self._entry_bar_index.pop(p.figi, None)
            try:
                await self._st_close(p.figi, _exit_px, execution=trade,
                                     reason=reason,
                                     net=float(trade.net_pnl) if trade else None)
            except Exception as _sw_e:
                _audit_swallow('close_all@L3054', _sw_e)  # audit silent-except
                pass
            closed.append({"figi": p.figi, "ticker": p.ticker,
                           "price": round(_exit_px, 6),
                           "net_pnl": float(trade.net_pnl) if trade else None})
            self.events.log("POSITION_CLOSED", figi=p.figi, ticker=p.ticker,
                            reason=reason, net_pnl=closed[-1]["net_pnl"])
        self.events.log("CLOSE_ALL", reason=f"closed={len(closed)} pending={len(pending)}")
        return {"closed": len(closed), "positions": closed, "pending": pending}

    async def _move_reselect_day(self, as_of) -> None:
        """Ежедневная пересборка корзины move_select на границе дня (реплей).

        as_of — начало нового дня: свечи дня ещё не видны, брать решения только
        по закрытым дневным барам прошлых дней. Вылетевшие из корзины фиги
        помечаются в _move_leaving и закрываются на первом баре своего дня
        (по candle.open) через deactivate_figi.
        """
        figi_by_ticker = getattr(self, "_figi_by_ticker", None)
        if not figi_by_ticker:
            return
        try:
            picked = await _move_select_universe(
                figi_by_ticker, self.config, as_of,
                lambda msg, level="info": self._log(msg, level=level, source="universe"),
            )
        except Exception as _e:
            self._log(f"⚠ move_reselect {type(_e).__name__}: {str(_e)[:120]}",
                      level="warn", source="universe")
            return
        if not picked:
            return
        new_sel = {str(u["figi"]) for u in picked}
        prev = set(getattr(self, "_move_selected", None) or set())
        self._move_selected = new_sel
        for u in picked:
            self.tickers.setdefault(str(u["figi"]), str(u.get("ticker", "")))
        leaving = prev - new_sel
        if leaving:
            self._move_leaving = set(getattr(self, "_move_leaving", None) or set()) | leaving
        entering = new_sel - set(self.stream_universe or [])
        if entering:
            try:
                from collections import deque as _dq
                from app.engine.strategies import build_strategy as _bs
                _cap = ENSEMBLE_BUFFER if getattr(self.config, "use_ensemble", False) else MAX_BUFFER
                for _f in entering:
                    if _f not in self.strategies:
                        self.strategies[_f] = _bs(self.config.strategy_id, self.config.params)
                    if _f not in self.buffers:
                        self.buffers[_f] = _dq(maxlen=_cap)
                if self.feed is not None:
                    self.feed.add_figis(list(entering))
                self.stream_universe = list(dict.fromkeys(list(self.stream_universe or []) + list(entering)))
                self._log("➕ UNIVERSE-MOVE add: "
                          + ", ".join(sorted(self.tickers.get(f, f) for f in entering)),
                          source="universe")
            except Exception as _e:
                self._log(f"⚠ move_reselect add {type(_e).__name__}: {str(_e)[:120]}",
                          level="warn", source="universe")
        if leaving:
            self._log("➖ UNIVERSE-MOVE out: "
                      + ", ".join(sorted(self.tickers.get(f, f) for f in leaving)),
                      source="universe")
        self.universe = [u for u in picked]
        self._log("UNIVERSE-MOVE day: "
                  + ", ".join(f"{u.get('ticker')}={u.get('move_cum_ret')}" for u in picked),
                  source="universe")

    async def deactivate_figi(self, figi: str, price: float | None = None) -> dict:
        """Убрать тикер из работающей карусели без рестарта (метка в БД уже снята).

        Закрывает открытую позицию, отписывает стрим, вычищает стратегию/буфер
        и все локальные state. Возвращает результат деактивации (для лога UI).
        """
        ticker = self.tickers.get(figi, figi[-6:])
        closed = []
        if self._close_pending_block(figi) is not None or figi in self._close_inflight():
            return {"ok": False, "ticker": ticker, "figi": figi,
                    "closed": 0, "pending_position": True}
        # 1) Закрываем открытую позицию (если есть) — как close_all, но для одной figi.
        if self.broker is not None:
            try:
                pos = await self.broker.get_position(figi)
                if pos is not None:
                    buf = self.buffers.get(figi)
                    if price is None:
                        price = float(buf[-1].close) if buf else float(pos.entry_price)
                    else:
                        price = float(price)
                    try:
                        outcome = await self._submit_close_reconciled(
                            figi, ticker, pos.qty, price, "carousel_remove", paper_filled=False)
                        if outcome["status"] == "pending_partial":
                            self._exit_qty[figi] = outcome["residual"]
                        if outcome["status"] not in ("closed", "reconciled_closed"):
                            return {"ok": False, "ticker": ticker, "figi": figi,
                                    "closed": 0, "pending_position": True}
                        trade = outcome["trade"]
                    except Exception as _cl_e:
                        # Вне торговой сессии sandbox не принимает заявки (30079) — тикер
                        # со снятой меткой останется в карусели до закрытия позиции, каждый
                        # следующий цикл hot-add повторит попытку.
                        self._log(f"⚠ deactivate {ticker}: позиция не закрылась — "
                                  f"{type(_cl_e).__name__}: {str(_cl_e)[:120]}. Тикер остаётся до закрытия.")
                        return {"ok": False, "ticker": ticker, "figi": figi,
                                "closed": 0, "pending_position": True}
                    if outcome["status"] == "reconciled_closed":
                        closed.append({"figi": figi, "ticker": ticker, "price": None,
                                       "net_pnl": None, "order_id": outcome["order"].id})
                    else:
                        self._held.discard(figi)
                        self._clear_exit_state(figi)
                        self._entry_bar_index.pop(figi, None)
                        try:
                            await self._st_close(figi,
                                                 float(getattr(trade, "exit_price", None) or getattr(trade, "price", None)) if (trade and (getattr(trade, "exit_price", None) or getattr(trade, "price", None))) else price,
                                                 execution=trade, reason="carousel_remove",
                                                 net=float(trade.net_pnl) if trade else None)
                        except Exception as _sw_e:
                            _audit_swallow('deactivate_figi@st_close', _sw_e)
                        closed.append({"figi": figi, "ticker": ticker, "price": round(price, 6),
                                       "net_pnl": float(trade.net_pnl) if trade else None})
                        self.events.log("POSITION_CLOSED", figi=figi, ticker=ticker,
                                        reason="carousel_remove", net_pnl=closed[-1]["net_pnl"])
            except Exception as _pos_e:
                self._log(f"⚠ deactivate {ticker}: позиция — {type(_pos_e).__name__}: {str(_pos_e)[:120]}")
                return {"ok": False, "ticker": ticker, "figi": figi,
                        "closed": 0, "pending_position": True}

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
        from app.bot.ensemble_strategy import EnsembleV4Strategy
        from app.engine.models import Candle as EC

        while self.running:
            try:
                # replay/test: универс фиксируется на старте окна. Hot-add по «сейчас»
                # подмешивал бы бары ПОСЛЕ окна реплея (и 1m в TF-буфер) — ломал прогретые
                # ряды и терял сигналы (найдено 01.10 на прогонах v2trendALL/dbg0901).
                if str(getattr(self.config, "feed", "")) == "replay":
                    await asyncio.sleep(5)
                    continue
                # Хоровод универса крутим независимо от торговых сессий:
                # добавление/удаление тикеров должно работать как часы (ночью/в выходные
                # свечи всё равно пишутся в БД потоком, а подписку подтянем к сессии).
                async with SessionLocal() as db:
                    current_figi = {u["figi"] for u in self.universe}
                    broker_selection = str(self.config.mode) in ("live", "sandbox")
                    if broker_selection:
                        selected = await self._refresh_broker_candidates()
                        all_eligible = [(row["figi"], row["ticker"], row["lot"]) for row in selected]
                        rows = [row for row in all_eligible if row[0] not in current_figi]
                    else:
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
                        if broker_selection:
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
                    "broker_degraded": dict(getattr(self.broker, "degraded", None) or {}),
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

    async def _restore_live_position_exits(self):
        from app.models.sandbox_trade import SandboxTrade
        from app.services.position_exit_recovery import recover_position_exit

        broker = self.broker
        scope = (getattr(broker, 'mode', None), getattr(broker, '_account', None))

        def same_scope():
            return self.broker is broker and (broker.mode, broker._account) == scope and self.broker_mode == scope[0]

        try:
            if scope[0] not in ('live', 'sandbox') or not isinstance(scope[1], str) or not scope[1].strip():
                raise ValueError('Explicit position recovery scope required')
            inventory = await broker.positions()
            if not same_scope():
                raise ValueError('Position recovery scope changed')
            async with broker.sessions() as db:
                rows = (await db.scalars(select(SandboxTrade).where(
                    SandboxTrade.mode == scope[0], SandboxTrade.broker_account_id == scope[1],
                    SandboxTrade.exit_time.is_(None)))).all()
            if not same_scope():
                raise ValueError('Position recovery scope changed')
            grouped = {}
            for row in rows:
                grouped.setdefault(row.figi, []).append(row)
            held = {position.figi for position in inventory}
            if len(held) != len(inventory):
                raise ValueError('Duplicate broker inventory')
            self._held = held
            failed = []
            restored = []
            for position in inventory:
                figi = position.figi
                pending = self.pending_orders.get(figi)
                if pending is not None:
                    if tuple((pending.meta or {}).get('broker_scope', ())) != scope:
                        raise ValueError('Pending position belongs to another scope')
                    continue
                try:
                    candidates = grouped.get(figi, [])
                    if len(candidates) != 1:
                        raise ValueError('Unique scoped local position required')
                    recovered = recover_position_exit(candidates[0], position, mode=scope[0], account=scope[1])
                    self._exit_plans[figi] = recovered.policy
                    self._exit_side[figi] = recovered.side
                    self._exit_qty[figi] = recovered.units
                    self._exit_entry_px[figi] = recovered.entry_price
                    self._trail_active[figi] = recovered.trailing_active
                    self._trail_stop[figi] = recovered.stop_loss if recovered.stop_loss is not None else 0.0
                    if recovered.take_profit is None:
                        self._exit_target.pop(figi, None)
                    else:
                        self._exit_target[figi] = recovered.take_profit
                    self._pos_leverage[figi] = recovered.leverage
                    self._entry_bar_index[figi] = self._bar_counter
                    self._held_since.setdefault(figi, __import__('time').monotonic())
                    self._entry_pause().set_blocker('position_recovery_failed', False, *scope, figi)
                    restored.append(figi)
                except Exception as error:
                    self._entry_pause().set_blocker('position_recovery_failed', True, *scope, figi)
                    self.events.log('POSITION_RECOVERY_PENDING', figi=figi, reason=type(error).__name__)
                    failed.append(figi)
            for figi in grouped.keys() - held:
                if figi not in self.pending_orders:
                    self._entry_pause().set_blocker('position_recovery_failed', True, *scope, figi)
                    failed.append(figi)
            for blocker in tuple(self._entry_pause().blockers):
                if (len(blocker) == 4 and blocker[:3] == ('position_recovery_failed', *scope)
                        and blocker[3] not in held and blocker[3] not in grouped
                        and blocker[3] not in self.pending_orders):
                    self._entry_pause().set_blocker('position_recovery_failed', False, *scope, blocker[3])
            self._entry_pause().set_blocker('position_recovery_failed', False, *scope)
            return dict(restored=restored, pending=failed)
        except Exception as error:
            self._entry_pause().set_blocker('position_recovery_failed', True, *scope)
            self.events.log('POSITION_RECOVERY_PENDING', reason=type(error).__name__)
            return dict(restored=[], pending=None)

    async def _finish_open_runtime(self, broker, scope, order, intent):
        from app.services.broker_order_journal import BrokerOrderJournal
        from app.services.open_fill_accounting import acknowledge_open
        from app.services.open_order_intent import OpenIntent
        from app.services.close_position_projection import price_equal
        from app.models.sandbox_trade import SandboxTrade

        figi = intent.figi
        self._entry_pause().set_blocker('entry_reconciliation', True, *scope, order.id)

        def same_scope():
            return (self.broker is broker and (broker.mode, broker._account) == scope
                    and self.broker_mode == scope[0] and self.pending_orders.get(figi) is order)

        try:
            row = await BrokerOrderJournal(broker.sessions).load(intent)
            if (row is None or row.accounted_units <= 0 or row.accounted_units != row.observed_units
                    or not row.entry_accounting or row.runtime_accounted_units is None):
                raise ValueError('opening_runtime_checkpoint_missing')
            saved = OpenIntent.from_record(row)
            if saved.exit_policy is None:
                raise ValueError('original_exit_policy_unknown')
            policy = saved.exit_policy.restore()
            local_id = row.entry_accounting['local_position_id']
            async with broker.sessions() as db:
                local = await db.get(SandboxTrade, local_id)
            if (local is None or local.exit_time is not None or local.qty != row.accounted_units
                    or (local.mode, local.broker_account_id, local.figi, local.side)
                    != (scope[0], scope[1], figi, saved.side)):
                raise ValueError('opening_local_position_mismatch')
            if not same_scope():
                return False
            await broker.flush_portfolio()
            if not same_scope():
                return False
            inventory = await broker.get_position(figi)
            if not same_scope():
                return False
            side = 'LONG' if saved.side == 'BUY' else 'SHORT'
            if (inventory is None or inventory.figi != figi or inventory.side != side
                    or type(inventory.qty) is not int or inventory.qty != row.accounted_units
                    or not price_equal(inventory.entry_price, local.entry_price)):
                raise ValueError('opening_broker_inventory_mismatch')
            installed = getattr(self, '_entry_runtime_requests', {}).get(figi) == (id(broker), scope, order.id)
            if not installed:
                self._exit_plans[figi] = policy
                self._trail_active[figi] = bool(local.trailing_active)
                self._trail_stop[figi] = float(local.stop_loss) if local.stop_loss is not None else 0.0
                if local.take_profit is not None and not local.trailing_active:
                    self._exit_target[figi] = float(local.take_profit)
                else:
                    self._exit_target.pop(figi, None)
                self._entry_bar_index[figi] = self._bar_counter
                self._pos_leverage[figi] = float(local.leverage)
                self._peak_pnl[figi] = dict(pnl=0.0, ts=self._bot_now(), price=float(local.entry_price),
                                           atr_abs=None, atr_pct=None, mae=0.0)
                self._gb_peak[figi] = 0.0
                self._trail_info.pop(figi, None)
            self._exit_side[figi] = side
            self._exit_qty[figi] = row.accounted_units
            self._exit_entry_px[figi] = float(local.entry_price)
            self._held.add(figi)
            self._held_since.setdefault(figi, __import__('time').monotonic())
            if not hasattr(self, '_entry_runtime_requests'):
                self._entry_runtime_requests = {}
            self._entry_runtime_requests[figi] = (id(broker), scope, order.id)
            order.meta = {**(order.meta or {}), 'protection_installed': True}
            await self._check_circuit_breaker()
            if not same_scope():
                return False
            result = await acknowledge_open(broker.sessions, saved, units=row.accounted_units, local_position_id=local_id)
            if not same_scope():
                return False
            order.meta = {**order.meta, 'runtime_accounted_units': result.accounted_units,
                          'runtime_accounting_pending': False}
            order.qty = saved.requested_units // saved.lot_size
            order.meta['remaining_lots'] = (saved.requested_units - result.accounted_units) // saved.lot_size
            order.price = float(local.entry_price)
            order.status = ('FILLED' if result.accounted_units == saved.requested_units else 'CANCELLED') if result.released else 'PARTIAL_FILLED'
            if result.released:
                self.pending_orders.pop(figi, None)
                self._entry_pause().set_blocker('entry_reconciliation', False, *scope, order.id)
            self.events.log('ORDER_RECONCILED_ACCOUNTED', figi=figi, order_id=order.id,
                            executed_units=result.accounted_units, terminal=result.released)
            return True
        except Exception as error:
            self.events.log('ORDER_RECONCILIATION_REQUIRED', figi=figi, order_id=order.id,
                            reason='opening_runtime_completion_pending', error_type=type(error).__name__)
            return False

    async def _resume_close_after_open_requests(self):
        from decimal import Decimal
        from app.services.open_close_request import prepare_close_after_open

        broker = self.broker
        if not isinstance(broker, LiveBroker):
            return
        scope = (broker.mode, broker._account)
        requests = await self._restore_close_after_open_requests()
        for pending in requests:
            intent = pending.intent
            if intent.figi in self._close_inflight():
                continue
            existing = self.pending_orders.get(intent.figi)
            if existing is not None:
                if existing.action == 'open' and existing.id == intent.client_order_id:
                    await self._submit_close_reconciled(intent.figi, intent.ticker,
                        int((existing.meta or {}).get('durable_accounted_units', 0)), 0.0, pending.request.reason)
                continue
            try:
                proof = await prepare_close_after_open(broker.sessions, pending)
                if proof is None:
                    continue
                await broker.flush_portfolio()
                position = await broker.get_position(intent.figi)
                if (self.broker is not broker or (broker.mode, broker._account) != scope
                        or self.broker_mode != scope[0]):
                    raise ValueError('close_after_open_handoff_scope_changed')
                if (position is None or position.figi != intent.figi or position.qty != proof.units
                        or position.side != ('LONG' if intent.side == 'BUY' else 'SHORT')
                        or Decimal(str(position.entry_price)).quantize(Decimal('0.000001')) != proof.entry_price):
                    raise ValueError('close_after_open_handoff_inventory_changed')
                order = BotOrder(id=proof.close_client_order_id, figi=intent.figi, ticker=intent.ticker,
                    action='close', side='SELL' if intent.side == 'BUY' else 'BUY', qty=proof.units,
                    created_at=self._bot_now(), status='PENDING_RECONCILIATION', meta={})
                await self._submit_close_reconciled(intent.figi, intent.ticker, proof.units,
                    float(proof.entry_price), pending.request.reason, close_order=order, entry_proof=proof)
            except Exception as error:
                self.events.log('ORDER_RECONCILIATION_REQUIRED', figi=intent.figi, action='close',
                                reason='close_after_open_handoff_pending', error_type=type(error).__name__)
        if self.broker is broker and (broker.mode, broker._account) == scope and self.broker_mode == scope[0]:
            await self._restore_close_after_open_requests()

    async def _restore_close_after_open_requests(self):
        from app.services.open_close_request import load_close_after_open

        broker = self.broker
        if not isinstance(broker, LiveBroker):
            return []
        scope = (broker.mode, broker._account)
        try:
            if self.broker_mode != scope[0]:
                raise ValueError('close_after_open_recovery_scope_mismatch')
            requests = await load_close_after_open(broker.sessions, mode=scope[0], broker_account_id=scope[1])
            if (self.broker is not broker or (broker.mode, broker._account) != scope
                    or self.broker_mode != scope[0]):
                raise ValueError('close_after_open_recovery_scope_changed')
            active_requests = {item.intent.client_order_id for item in requests}
            pause = self._entry_pause()
            for key in tuple(pause.blockers):
                if (len(key) == 4 and key[:3] == ('close_after_open', *scope)
                        and key[3] not in active_requests):
                    pause.set_blocker('close_after_open', False, *scope, key[3])
            for item in requests:
                pause.set_blocker('close_after_open', True, *scope, item.intent.client_order_id)
            self._entry_pause().set_blocker('close_after_open_recovery_failed', False, *scope)
            return requests
        except Exception as error:
            self._entry_pause().set_blocker('close_after_open_recovery_failed', True, *scope)
            raise RuntimeError('close_after_open_recovery_failed') from error

    async def _restore_open_intents(self):
        from app.services.broker_order_journal import BrokerOrderJournal
        from app.services.open_order_intent import OpenIntent

        broker = self.broker
        if not isinstance(broker, LiveBroker):
            return 0
        scope = (getattr(broker, 'mode', None), getattr(broker, '_account', None))
        try:
            if scope[0] != self.broker_mode:
                raise ValueError('opening_recovery_scope_mismatch')
            rows = await BrokerOrderJournal(broker.sessions).unresolved(
                mode=scope[0], broker_account_id=scope[1], action='open')
            restored = {}
            for row in rows:
                intent = OpenIntent.from_record(row)
                if ((intent.mode, intent.broker_account_id) != scope or row.unresolved_figi != intent.figi
                        or intent.figi in restored or type(row.accounted_units) is not int
                        or type(row.observed_units) is not int
                        or not 0 <= row.accounted_units <= row.observed_units <= intent.requested_units
                        or row.accounted_units % intent.lot_size or row.observed_units % intent.lot_size):
                    raise ValueError('opening_recovery_checkpoint_invalid')
                restored[intent.figi] = BotOrder(
                    id=intent.client_order_id, figi=intent.figi, ticker=intent.ticker,
                    action='open', side=intent.side, qty=intent.requested_units // intent.lot_size,
                    status='PENDING_RECONCILIATION', created_at=intent.created_at,
                    meta=dict(broker_scope=scope, quantity_unit='lots', durable_open=True,
                              requested_lots=intent.requested_units // intent.lot_size,
                              requested_units=intent.requested_units, lot_size=intent.lot_size,
                              confirmed_executed_lots=row.accounted_units // intent.lot_size,
                              broker_observed_executed_lots=row.observed_units // intent.lot_size,
                              entry_plan=intent.entry_payload(), durable_recovery=True))
            if (self.broker is not broker or (broker.mode, broker._account) != scope
                    or self.broker_mode != scope[0]):
                raise ValueError('opening_recovery_scope_changed')
            for figi, order in restored.items():
                previous = self.pending_orders.get(figi)
                if previous is not None and (previous.id != order.id or previous.action != 'open'
                        or tuple((previous.meta or {}).get('broker_scope', ())) != scope):
                    raise ValueError('opening_recovery_pending_conflict')
            for figi, order in restored.items():
                self.pending_orders.setdefault(figi, order)
                self._entry_pause().set_blocker('entry_reconciliation', True, *scope, order.id)
            self._entry_pause().set_blocker('entry_recovery_failed', False, *scope)
            return len(restored)
        except Exception as error:
            self._entry_pause().set_blocker('entry_recovery_failed', True, *scope)
            raise RuntimeError('opening_journal_recovery_failed') from error

    async def _reconcile_durable_open(self, broker, scope, order, state):
        from app.services.broker_order_journal import BrokerOrderJournal
        from app.services.open_order_intent import OpenIntent
        from app.services.broker_fill_evidence import parse_order_state_fills
        from app.services.open_fill_accounting import settle_open

        self._entry_pause().set_blocker('entry_reconciliation', True, *scope, order.id)

        def same_scope():
            return (self.broker is broker and (broker.mode, broker._account) == scope
                    and self.broker_mode == scope[0] and self.pending_orders.get(order.figi) is order)

        try:
            journal = BrokerOrderJournal(broker.sessions)
            async with broker.sessions() as db:
                from app.models.broker_order_intent import BrokerOrderIntent
                row = await db.get(BrokerOrderIntent, (*scope, order.id))
            if row is None:
                raise ValueError('opening_reservation_missing')
            intent = OpenIntent.from_record(row)
            if (intent.figi != order.figi or intent.side != order.side
                    or (intent.mode, intent.broker_account_id) != scope or not same_scope()):
                raise ValueError('opening_runtime_identity_changed')
            units = state.lots_executed * intent.lot_size
            status = broker._close_evidence_status(state, units, intent.requested_units)
            if not await journal.observe(intent, broker_order_id=state.order_id,
                                         broker_status=status, executed_units=units):
                recorded = await journal.load(intent)
                if (recorded is not None and recorded.status == 'SETTLED'
                        and recorded.accounted_units == recorded.runtime_accounted_units == units and units > 0):
                    return await self._finish_open_runtime(broker, scope, order, intent)
                raise ValueError('opening_observation_not_accepted')
            if not same_scope():
                return False
            if units == 0:
                if status in ('CANCELLED', 'REJECTED'):
                    order.status = status
                    self.pending_orders.pop(order.figi)
                    self._entry_pause().set_blocker('entry_reconciliation', False, *scope, order.id)
                    return True
                return False
            evidence = parse_order_state_fills(state, mode=scope[0], broker_account_id=scope[1],
                client_order_id=intent.client_order_id, figi=intent.figi, side=intent.side,
                requested_lots=intent.requested_units // intent.lot_size, lot_size=intent.lot_size,
                as_of=self._bot_now())
            result = await settle_open(broker.sessions, intent, evidence, as_of=self._bot_now())
            if not same_scope():
                return False
            order.meta = {**(order.meta or {}), 'confirmed_executed_lots': result.accounted_units // intent.lot_size,
                          'durable_accounted_units': result.accounted_units,
                          'local_position_id': result.local_position_id, 'runtime_accounting_pending': True}
            return await self._finish_open_runtime(broker, scope, order, intent)
        except Exception as error:
            self.events.log('ORDER_RECONCILIATION_REQUIRED', figi=order.figi, order_id=order.id,
                            reason='opening_stage_accounting_pending', error_type=type(error).__name__)
            return False

    async def _restore_close_intents(self):
        from app.services.broker_order_journal import BrokerOrderJournal, CloseIntent

        broker = self.broker
        if not isinstance(broker, LiveBroker):
            return 0
        scope = (getattr(broker, 'mode', None), getattr(broker, '_account', None))
        try:
            if (scope[0] not in ('live', 'sandbox') or not isinstance(scope[1], str)
                    or not scope[1].strip() or self.broker_mode != scope[0]):
                raise ValueError('explicit_close_recovery_scope_required')
            rows = await BrokerOrderJournal(broker.sessions).unresolved(mode=scope[0], broker_account_id=scope[1])
            restored = {}
            for row in rows:
                intent = CloseIntent.from_record(row)
                if (intent.mode, intent.broker_account_id) != scope or row.unresolved_figi != intent.figi:
                    raise ValueError('stored_close_recovery_scope_mismatch')
                if (type(row.accounted_units) is not int or type(row.observed_units) is not int
                        or not 0 <= row.accounted_units <= row.observed_units <= intent.requested_units
                        or row.accounted_units % intent.lot_size or row.observed_units % intent.lot_size
                        or row.runtime_accounted_units is not None and (
                            type(row.runtime_accounted_units) is not int
                            or not 0 <= row.runtime_accounted_units <= row.accounted_units
                            or row.runtime_accounted_units % intent.lot_size)
                        or intent.figi in restored):
                    raise ValueError('stored_close_recovery_quantity_conflict')
                snapshot = intent.position_snapshot
                restored[intent.figi] = BotOrder(
                    id=intent.client_order_id, figi=intent.figi,
                    ticker=snapshot.ticker if snapshot else getattr(self, 'tickers', {}).get(intent.figi, intent.figi),
                    action='close', side=intent.side,
                    qty=intent.requested_units - row.accounted_units,
                    status='PARTIAL_FILLED' if row.accounted_units else 'PENDING_RECONCILIATION',
                    created_at=intent.created_at,
                    meta=dict(broker_scope=scope, quantity_unit='shares',
                              requested_units=intent.requested_units, lot_size=intent.lot_size,
                              requested_lots=intent.requested_units // intent.lot_size,
                              executed_units=row.accounted_units, observed_executed_units=row.observed_units,
                              durable_accounted_units=row.accounted_units,
                              runtime_accounted_units=row.runtime_accounted_units,
                              runtime_accounting_pending=row.runtime_accounted_units is not None
                                  and row.runtime_accounted_units < row.accounted_units,
                              broker_order_id=row.broker_order_id, broker_report_status_name=row.broker_status,
                              exit_reason=snapshot.exit_reason if snapshot else 'UNKNOWN',
                              durable_recovery=True, recovery_snapshot_available=snapshot is not None),
                )
            if (self.broker is not broker or (broker.mode, broker._account) != scope
                    or self.broker_mode != scope[0]):
                raise ValueError('broker_scope_changed_during_close_recovery')
            pending = getattr(self, 'pending_orders', {}) or {}
            for figi, order in restored.items():
                existing = pending.get(figi)
                if existing is not None and (existing.id != order.id or existing.action != 'close'
                        or tuple((existing.meta or {}).get('broker_scope', ())) != scope):
                    raise ValueError('existing_intent_conflicts_with_close_recovery')
            for figi, order in restored.items():
                pending.setdefault(figi, order)
            self.pending_orders = pending
            for order in restored.values():
                self._pause_close(scope, order.id)
            self._entry_pause().set_blocker('close_recovery_failed', False, *scope)
            return len(restored)
        except Exception as error:
            self._entry_pause().set_blocker('close_recovery_failed', True, *scope)
            raise RuntimeError('close_journal_recovery_failed') from error

    async def _finish_close_runtime(self, broker, scope, order, intent):
        from app.services.broker_order_journal import BrokerOrderJournal, CloseIntent
        from app.services.close_position_projection import project_close_fills, price_equal

        figi = intent.figi
        self._pause_close(scope, order.id)

        def same_scope():
            return (self.broker is broker and (broker.mode, broker._account) == scope
                    and self.broker_mode == scope[0] and self.pending_orders.get(figi) is order)

        try:
            journal = BrokerOrderJournal(broker.sessions)
            row = await journal.load(intent)
            if (row is None or row.runtime_accounted_units is None or row.accounted_units <= 0
                    or row.observed_units != row.accounted_units):
                raise ValueError('close_runtime_checkpoint_not_ready')
            saved = CloseIntent.from_record(row)
            remaining = saved.requested_units - row.accounted_units
            if not same_scope():
                return False
            await broker.flush_portfolio()
            if not same_scope():
                return False
            position = await broker.get_position(figi)
            if not same_scope():
                return False
            if remaining == 0:
                if position is not None:
                    raise ValueError('broker_position_still_present_after_full_close')
            elif (position is None or getattr(position, 'figi', None) != figi
                    or type(getattr(position, 'qty', None)) is not int or position.qty != remaining
                    or saved.position_snapshot is None
                    or getattr(position, 'side', None) != saved.position_snapshot.position_side
                    or not price_equal(getattr(position, 'entry_price', None), saved.position_snapshot.entry_price)):
                raise ValueError('broker_residual_differs_from_close_accounting')
            applied = await journal.apply_runtime(saved, cumulative_units=row.accounted_units,
                                                   apply=project_close_fills)
            if not same_scope():
                return False
            await self._check_circuit_breaker()
            if not same_scope():
                return False
            meta = dict(order.meta or {})
            meta.update(runtime_accounted_units=applied.accounted_units,
                        runtime_accounting_pending=False, executed_units=applied.accounted_units,
                        remaining_units=remaining)
            meta.pop('durable_observation_pending', None)
            order.meta = meta
            order.qty = remaining
            if remaining:
                self._exit_qty[figi] = remaining
                self._held.add(figi)
                order.status = 'CANCELLED' if applied.released else 'PARTIAL_FILLED'
            else:
                self._clear_exit_state(figi)
                self._held.discard(figi)
                self._opposite_count.pop(figi, None)
                self._entry_bar_index.pop(figi, None)
                self._last_exit_bar[figi] = self._bar_counter
                order.status = 'FILLED'
            if applied.released:
                self.pending_orders.pop(figi, None)
                self._pause_close(scope, order.id, active=False)
            self.events.log('ORDER_RECONCILED_ACCOUNTED', figi=figi, order_id=order.id,
                            executed_units=applied.accounted_units, remaining_units=remaining,
                            terminal=applied.released)
            return True
        except Exception as error:
            self.events.log('ORDER_RECONCILIATION_REQUIRED', figi=figi, order_id=order.id,
                            reason='close_local_projection_pending', error_type=type(error).__name__)
            return False

    async def _reconcile_pending_orders(self, only_figi=None):
        from t_tech.invest.schemas import OrderExecutionReportStatus

        broker = self.broker
        if not isinstance(broker, LiveBroker):
            return
        scope = (broker.mode, broker._account)
        terminal_empty = {
            OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_CANCELLED: "CANCELLED",
            OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_REJECTED: "REJECTED",
        }
        for figi, order in list(self.pending_orders.items()):
            if only_figi is not None and figi != only_figi:
                continue
            if order.status not in ("PENDING_RECONCILIATION", "PARTIAL_FILLED"):
                continue
            meta = dict(order.meta or {})
            if order.action not in ("open", "close"):
                continue
            if ("broker_scope" in meta and tuple(meta["broker_scope"]) != scope):
                self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                order_id=order.id, reason="broker_scope_changed")
                continue
            requested = meta.get("requested_lots")
            if type(requested) is not int or requested <= 0:
                self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                order_id=order.id, reason="original_requested_lots_missing")
                continue
            if order.action == "close":
                lot_size = meta.get("lot_size")
                requested_units = meta.get("requested_units")
                if (type(lot_size) is not int or lot_size <= 0
                        or type(requested_units) is not int
                        or requested_units != requested * lot_size):
                    self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                    order_id=order.id, reason="original_close_quantity_inconsistent")
                    continue
            try:
                state = await broker.order_state(order.id, figi, order.side, requested)
            except Exception as error:
                self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                order_id=order.id, reason="broker_state_unavailable",
                                error_type=type(error).__name__)
                continue
            if (self.broker is not broker or (broker.mode, broker._account) != scope
                    or self.pending_orders.get(figi) is not order):
                continue
            if order.action == 'open' and meta.get('durable_open'):
                await self._reconcile_durable_open(broker, scope, order, state)
                continue
            report_status = state.execution_report_status
            observed_executed = state.lots_executed
            previous_executed = max(
                int(meta.get("broker_observed_executed_lots") or 0),
                int(meta.get("confirmed_executed_lots") or 0),
            )
            if order.action == "close":
                prior_units = max(int(meta.get("executed_units") or 0),
                                  int(meta.get("observed_executed_units") or 0))
                if observed_executed * lot_size < prior_units:
                    self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                    order_id=order.id, reason="broker_execution_regressed")
                    continue
            if observed_executed < previous_executed or (
                    order.status == "PARTIAL_FILLED" and observed_executed == 0):
                self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                order_id=order.id, reason="broker_execution_regressed")
                continue
            if order.action == 'close':
                from app.services.broker_order_journal import BrokerOrderJournal, CloseIntent
                meta['observed_executed_units'] = observed_executed * lot_size
                order.meta = meta
                try:
                    intent = CloseIntent(scope[0], scope[1], order.id, figi, order.side,
                                         requested_units, lot_size, order.created_at)
                    durable_status = broker._close_evidence_status(state, observed_executed * lot_size, requested_units)
                    stored = await BrokerOrderJournal(broker.sessions).observe(intent,
                        broker_order_id=state.order_id, broker_status=durable_status,
                        executed_units=observed_executed * lot_size)
                    if not stored:
                        recorded = await BrokerOrderJournal(broker.sessions).load(intent)
                        if (recorded is not None and recorded.status == 'SETTLED'
                                and recorded.runtime_accounted_units == recorded.accounted_units
                                and recorded.accounted_units == recorded.observed_units
                                and recorded.accounted_units > 0):
                            await self._finish_close_runtime(broker, scope, order, intent)
                            continue
                        raise ValueError('close_observation_not_accepted_by_journal')
                except Exception as error:
                    meta['durable_observation_pending'] = True
                    self._pause_close(scope, order.id)
                    self.events.log('ORDER_RECONCILIATION_REQUIRED', figi=figi, order_id=order.id,
                                    reason='close_observation_persistence_failed', error_type=type(error).__name__)
                    continue
                if (self.broker is not broker or (broker.mode, broker._account) != scope
                        or self.pending_orders.get(figi) is not order):
                    continue
                meta.pop('durable_observation_pending', None)
            meta.update(broker_order_id=state.order_id,
                        broker_observed_executed_lots=observed_executed,
                        broker_report_status=int(report_status),
                        broker_state_observed_at=datetime.now(timezone.utc).isoformat())
            order.meta = meta
            if order.action == 'close' and observed_executed > 0:
                from app.services.broker_fill_evidence import parse_order_state_fills
                accounting_stage = 'parse_evidence'
                try:
                    evidence = parse_order_state_fills(state, mode=scope[0], broker_account_id=scope[1],
                        client_order_id=order.id, figi=figi, side=order.side,
                        requested_lots=requested, lot_size=lot_size, as_of=datetime.now(timezone.utc))
                    accounting_stage = 'settle_execution'
                    settlement = await BrokerOrderJournal(broker.sessions).settle(intent,
                        broker_order_id=state.order_id, cumulative_units=observed_executed * lot_size,
                        evidence=evidence, defer_runtime=True)
                    if (self.broker is not broker or (broker.mode, broker._account) != scope
                            or self.pending_orders.get(figi) is not order):
                        continue
                    meta.update(durable_accounted_units=settlement.accounted_units,
                                runtime_accounting_pending=True)
                    order.meta = meta
                    self._pause_close(scope, order.id)
                    accounting_stage = 'finish_close_runtime'
                    await self._finish_close_runtime(broker, scope, order, intent)
                    continue
                except Exception as error:
                    from traceback import extract_tb
                    origin = extract_tb(error.__traceback__)[-1]
                    self._pause_close(scope, order.id)
                    self.events.log('ORDER_RECONCILIATION_REQUIRED', figi=figi, order_id=order.id,
                                    reason='close_stage_accounting_pending', error_type=type(error).__name__,
                                    accounting_stage=accounting_stage, error_function=origin.name,
                                    error_line=origin.lineno)
                    continue
            if report_status in terminal_empty and observed_executed == 0:
                order.status = terminal_empty[report_status]
                self.pending_orders.pop(figi)
                self._pause_close(scope, order.id, active=False)
                self.events.log("ORDER_RECONCILED_EMPTY", figi=figi,
                                order_id=order.id, status=order.status)
            else:
                self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                order_id=order.id, reason="execution_accounting_pending",
                                executed_lots=observed_executed)

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
                await self._reconcile_pending_orders()
                await self._resume_close_after_open_requests()
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

        def pending_execution_figis():
            orders = getattr(self, 'pending_orders', {}) or {}
            return set(getattr(self, '_closing_inflight', set()) or ()) | {
                figi for figi, order in orders.items()
                if getattr(order, 'action', None) in ('open', 'close')
                and getattr(order, 'status', None) not in ('CANCELLED', 'REJECTED', 'FILLED')
            }
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
                                            SandboxTrade.mode == self.broker_mode,
                                            SandboxTrade.broker_account_id == _account_scope_of(self)))).scalars().all()
            local_by_figi: dict[str, list] = {}
            for r in rows:
                local_by_figi.setdefault(r.figi, []).append(r)

            blocked_figis = pending_execution_figis()
            created = closed = 0
            unresolved = len(blocked_figis & (set(broker_pos) | set(local_by_figi)))
            changed_figis = set()
            now = datetime.now(timezone.utc)
            # Grace-период: строки, открытые/закрытые в последние 2 минуты, НЕ трогаем —
            # брокер (sandbox/биржа) может отражать позицию с лагом после ордера.
            _grace = 120.0
            # 1) Позиции брокера без локальной строки -> создать.
            for figi, bp in broker_pos.items():
                if figi in blocked_figis or figi in local_by_figi:
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
                           SandboxTrade.broker_account_id == _account_scope_of(self),
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
                if figi in pending_execution_figis():
                    unresolved += 1
                    continue
                ticker = self.tickers.get(figi, figi[:8])
                changed_figis.add(figi)
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
                    broker_account_id=_account_scope_or_raise(self),
                ))
                created += 1
                self._log(f"RECONCILE: создана строка {ticker} ({figi[-6:]}) {bp.side} {bp.qty} @{getattr(bp, 'entry_price', 0.0):.2f}")

            # 2) Локальные строки без позиции брокера -> закрыть (orphan).
            for figi, lst in local_by_figi.items():
                if figi in blocked_figis or figi in broker_pos:
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
                        unresolved += 1
                        self._log(f"RECONCILE UNKNOWN {figi[-6:]}: {type(_sw_e).__name__}; local position retained")
                        continue
                    if _real is not None:
                        self._log(f"RECONCILE: orphan {figi[-6:]} — у брокера позиция есть, не трогаю")
                        continue
                    # Реальная цена/P&L: позиция могла быть закрыта вручную/стопом
                    # у брокера — тогда net=0 недопустим. Берём из операций брокера,
                    # fallback — последняя известная цена.
                    _exit_px = None
                    try:
                        _ops = await self.broker.operations(now - timedelta(days=3), now)
                        _lng0 = str(r.side).upper() in ("BUY", "LONG")
                        _close_kind = "Покупка" if not _lng0 else "Продажа"   # закрывающая сторона
                        _et = r.entry_time
                        if _et is not None and getattr(_et, "tzinfo", None) is None:
                            _et = _et.replace(tzinfo=timezone.utc)
                        _best_close = None   # (t, price) — ближайшая закрывающая после входа
                        _close_matches = 0
                        for _op in (getattr(_ops, "operations", None) or []):
                            try:
                                if str(getattr(_op, "figi", "")) != figi:
                                    continue
                                _t = getattr(_op, "date", None)
                                if _t is not None and getattr(_t, "tzinfo", None) is None:
                                    _t = _t.replace(tzinfo=timezone.utc)
                                if _et is None or _t is None or not (_et <= _t <= now):
                                    continue
                                from t_tech.invest import OperationState
                                if getattr(_op, "state", None) != OperationState.OPERATION_STATE_EXECUTED:
                                    continue
                                _operation_qty = getattr(_op, "quantity", None)
                                _operation_remaining = getattr(_op, "quantity_rest", None)
                                if (type(_operation_qty) is not int or _operation_qty != r.qty
                                        or type(_operation_remaining) is not int or _operation_remaining != 0):
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
                                    _close_matches += 1
                                    if _best_close is None or (_t is not None and _t < _best_close[0]):
                                        _best_close = (_t or _et or _t, _p)
                            except Exception as _sw_e:
                                _audit_swallow('_reconcile_positions@L3482', _sw_e)  # audit silent-except
                                continue
                        if _best_close is not None and _close_matches == 1:
                            _exit_px = round(_best_close[1], 6)
                    except Exception as _sw_e:
                        _audit_swallow('_reconcile_positions@L3488', _sw_e)  # audit silent-except
                        pass
                    # ARCH193: закрываем orphan ТОЛЬКО по реальной цене закрытия из
                    # операций брокера. Без неё исход неизвестен — не подставляем свечу
                    # и не фабрикуем сделку: оставляем строку, помечаем unresolved.
                    _px_out = float(_exit_px or 0.0)
                    if _px_out <= 0:
                        unresolved += 1
                        self._log(f"RECONCILE UNKNOWN {figi[-6:]}: нет операции закрытия у брокера; строка оставлена (без фабрикации)")
                        continue
                    if figi in pending_execution_figis():
                        unresolved += 1
                        continue
                    changed_figis.add(figi)
                    r.exit_time = _best_close[0]
                    r.exit_price = _px_out
                    r.exit_reason = "closed_at_broker"
                    r.net_pnl = None
                    r.commission = None
                    closed_here += 1
                closed += closed_here
                if closed_here:
                    self._log(f"RECONCILE: закрыт orphan {figi[-6:]} ({closed_here} строк)")

            # 3) Дубликаты на один figi -> оставить только лучшую по цене.
            for figi, lst in local_by_figi.items():
                if figi in pending_execution_figis() or figi not in broker_pos or len(lst) <= 1:
                    continue
                bp = broker_pos[figi]
                ref = float(getattr(bp, "entry_price", 0.0) or 0.0)
                best = min(lst, key=lambda r: abs(float(r.entry_price or 0.0) - ref) if ref else 0.0)
                for r in lst:
                    if r is best:
                        continue
                    changed_figis.add(figi)
                    r.exit_time = now
                    r.exit_price = float(r.entry_price or 0.0)
                    r.exit_reason = "duplicate_cleanup"
                    r.net_pnl = None  # не экономическая сделка: не 0₽, а «нет P&L»
                    closed += 1
                    _tk = self.tickers.get(figi, figi[:8])
                    self._log(f"RECONCILE: закрыт дубликат {_tk} {figi[-6:]} @{r.entry_price:.2f} (best @{ref:.2f})")
            if created or closed:
                if changed_figis & pending_execution_figis():
                    await db.rollback()
                    self._log('RECONCILE UNKNOWN: execution started during position repair; changes rolled back')
                    return
                await db.commit()
            # Лог состояния сверки КАЖДЫЙ цикл (не только при изменениях).
            if not force:
                if unresolved:
                    self._log(f"Сверка с брокером: не завершена, неизвестных результатов {unresolved}")
                elif created or closed:
                    self._log(f"Сверка с брокером: были расхождения → создано {created}, закрыто {closed} (брокер: {len(broker_pos)} поз., локально: {len(local_by_figi)})")
                else:
                    self._log(f"Сверка с брокером: ✓ сходится (позиций: брокер {len(broker_pos)}, локально {len(local_by_figi)})")

    def _interval_value(self) -> int:
        if self.config.use_ensemble:
            return 1
        interval = INTERVAL_NAMES[self.config.interval_name]
        return int(getattr(interval, "value", interval))

    def _spawn_task(self, name: str, coro, critical: bool = True) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._task_registry[name] = task

        def _on_done(t: asyncio.Task) -> None:
            self._task_registry.pop(name, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self._task_errors[name] = {
                    "error": str(exc)[:200],
                    "traceback": "".join(__import__("traceback").format_exception(type(exc), exc, exc.__traceback__))[-2000:],
                    "count": self._task_errors.get(name, {}).get("count", 0) + 1,
                    "last_seen": _time.time(),
                }
                self._log(f"TASK_DIED {name}: {type(exc).__name__}: {str(exc)[:120]}", level="error")
                self.events.log("TASK_DIED", task=name, error=str(exc)[:200], critical=critical)
                if critical:
                    self._log(f"TASK_DIED {name}: critical task, требуется ручное вмешательство", level="error")

        task.add_done_callback(_on_done)
        return task




    async def _finalize_replay(self, exited: str = "stream_exhausted") -> None:
        """Финал реплея: открытые позиции закрываются по последним ценам свечей
        (replay_end_close), прогон фиксируется в bot_test_runs с терминальным
        статусом. Раньше тест «зависал» с открытыми сделками и без итогов в
        /bot/tests, а аварийный/прерванный прогон выглядел как успешный (P1.2).

        Статусы: COMPLETED — нормальное исчерпание стрима и успешная финализация;
        CANCELLED — ручная остановка/отмена; FAILED — исключение processing loop;
        PARTIAL_CLOSE — стрим дошёл до конца, но часть позиций закрыть не удалось.
        finished/finished_at — только для COMPLETED; незакрытые позиции не теряют
        held-state (повтор/реверс возможны); повторный вызов — no-op.
        """
        if getattr(self, "_replay_status", None):
            self._log(f"REPLAY FINALIZE: повторный вызов пропущен (статус {self._replay_status})")
            return
        if exited.startswith("exception:"):
            _status = "FAILED"
        elif exited in ("cancelled", "running_flag_false"):
            _status = "CANCELLED"
        else:
            _status = "COMPLETED"
        _positions_ok = True
        try:
            _pos_list = list(await self.broker.positions())
        except Exception as _sw_e:
            _audit_swallow('_finalize_replay@positions', _sw_e)
            _pos_list = []
            _positions_ok = False
        _prices: dict = {}
        try:
            _lp = getattr(self.broker, "last_prices", None)
            if _lp is not None and _pos_list:
                _prices = await _lp([p.figi for p in _pos_list]) or {}
        except Exception as _sw_e:
            _audit_swallow('_finalize_replay@prices', _sw_e)
            _prices = {}
        _closed = 0
        _close_errors: list[str] = []
        for _p in _pos_list:
            try:
                from math import isfinite
                _observed = _prices.get(_p.figi)
                if _observed is None or isinstance(_observed, bool):
                    _close_errors.append(f"{_p.figi}: final_price_unknown")
                    continue
                _px = float(_observed)
                if not isfinite(_px) or _px <= 0:
                    _close_errors.append(f"{_p.figi}: final_price_invalid")
                    continue
                _outcome = await self._submit_close_reconciled(
                    _p.figi, _p.ticker, _p.qty, _px, "replay_end_close", paper_filled=False)
                if _outcome["status"] == "reconciled_closed":
                    _closed += 1
                    continue
                if _outcome["status"] == "pending_partial":
                    self._exit_qty[_p.figi] = _outcome["residual"]
                if _outcome["status"] != "closed":
                    _close_errors.append(f"{_p.figi}: {_outcome['status']}")
                    continue
                _trade = _outcome["trade"]
                # Успешно закрытое уходит из held-state; незакрытое остаётся (P1.2).
                self._held.discard(_p.figi)
                self._exit_plans.pop(_p.figi, None)
                _closed += 1
                await self._st_close(_p.figi, float(_trade.exit_price), execution=_trade, reason="replay_end_close",
                                     net=float(_trade.net_pnl))
                self._log(f"ВЫХОД {_p.figi[-6:]} (replay_end) pnl={float(_trade.net_pnl):+.2f}")
                if self._trace is not None:
                    try:
                        from app.engine.trace import Stage, Status, TraceEvent
                        self._trace.emit(TraceEvent(stage=Stage.EXIT, status=Status.EXECUTED,
                                                    figi=_p.figi, ticker=str(getattr(_p, "ticker", "") or ""),
                                                    action="close", price=float(_trade.exit_price),
                                                    qty=getattr(_p, "qty", None),
                                                    reason="replay_end_close", reason_code="replay_end_close"))
                    except Exception:
                        pass
            except Exception as _sw_e:
                _audit_swallow('_finalize_replay@close', _sw_e)
                _close_errors.append(f"{_p.figi}: {type(_sw_e).__name__}: {str(_sw_e)[:80]}")
        if not _positions_ok:
            _close_errors.append("positions(): портфель недоступен")
        # Незакрытое = всё, что осталось в held-state (успешные закрытия удалены выше).
        _remaining = sorted(self._held)
        if _status == "COMPLETED" and (_close_errors or _remaining):
            _status = "PARTIAL_CLOSE"
        # Итог прогона — до снятия виртуальных часов (процент считается по ним),
        # иначе статус остаётся active=true на застывшем проценте навсегда.
        _last_msk = None
        try:
            if self._replay_cur is not None:
                _last_msk = self._replay_cur.astimezone(timezone(timedelta(hours=3))).isoformat()
        except Exception as _sw_e:
            _audit_swallow('_finalize_replay@msk', _sw_e)  # audit silent-except
        self._replay_summary = {
            "closed": _closed,
            "status": _status,
            "remaining_positions": _remaining,
            "close_errors": _close_errors[:10],
            "start": self._replay_from.isoformat() if self._replay_from else None,
            "end": self._replay_to.isoformat() if self._replay_to else None,
            "last_ts": self._replay_cur.isoformat() if self._replay_cur else None,
            "last_msk": _last_msk,
            "pct": self._replay_progress_pct(),
            "test_name": str(getattr(self.config, "test_name", "") or ""),
        }
        self._replay_status = _status
        # finished=true — только для нормального исчерпания и успешной финализации.
        self._replay_finished = _status == "COMPLETED"
        self._replay_from = None
        self._replay_cur = None
        if getattr(self.config, "test_name", ""):
            try:
                from sqlalchemy import text as _btr_text2
                async with SessionLocal() as db:
                    # finished_at/closed_replay могли быть добавлены позже таблицы.
                    await db.execute(_btr_text2(
                        "ALTER TABLE bot_test_runs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ"))
                    await db.execute(_btr_text2(
                        "ALTER TABLE bot_test_runs ADD COLUMN IF NOT EXISTS closed_replay INT DEFAULT 0"))
                    await db.execute(_btr_text2(
                        "ALTER TABLE bot_test_runs ADD COLUMN IF NOT EXISTS status VARCHAR(16)"))
                    # finished_at — только для COMPLETED: аварийный прогон не должен
                    # выглядеть завершённым.
                    _fin_sql = "now()" if _status == "COMPLETED" else "NULL"
                    await db.execute(_btr_text2(
                        "UPDATE bot_test_runs SET updated_at = now(), finished_at = " + _fin_sql +
                        ", closed_replay = :c, status = :st WHERE name = :n"
                    ), {"n": self.config.test_name, "c": int(_closed), "st": _status})
                    await db.commit()
            except Exception as _sw_e:
                _audit_swallow('_finalize_replay@test_run', _sw_e)
        _tail = ""
        if _remaining:
            _shown = ",".join(f[-6:] for f in _remaining[:5])
            _tail = f", НЕ закрыто {len(_remaining)}: {_shown}"
        self._log(f"REPLAY {_status}: закрыто {_closed} поз. по последним ценам"
                  f"{_tail} (итоги в /bot/tests)")
        await self._close_trace(f"replay_{_status.lower()}")

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
        _skip_wall_clock_loops = bool(
            isinstance(feed, ReplayFeed)
            and str(getattr(self.config, "replay_pace", "") or "") != "wall"
        )
        if _skip_wall_clock_loops:
            self._log("ДЕТЕРМИНИРОВАННЫЙ РЕПЛЕЙ: wall-clock циклы выключены "
                      "(held_sync, vol_carousel, intrabar, queue, momentum, guard) — "
                      "итог зависит только от свечей БД")
        self._persist_task = self._spawn_task("persist", self._flush_persist())
        self._hot_add_task = self._spawn_task("hot_add", self._hot_add_universe())
        self._reconcile_task = self._spawn_task("reconcile", self._reconcile_loop())
        self._dispatch_task = (self._spawn_task("dispatch", self._dispatch_loop())
                               if isinstance(self.broker, LiveBroker) else None)
        self._session_task = self._spawn_task("session", self._session_monitor())
        self._metrics_task = self._spawn_task("metrics", self._metrics_loop())
        self._imoex_task = self._spawn_task("imoex", self._imoex_loop())
        self._held_sync_task = None if _skip_wall_clock_loops else self._spawn_task("held_sync", self._sync_held())
        self._vol_carousel_task = None if _skip_wall_clock_loops else self._spawn_task("vol_carousel", self._vol_carousel_loop())
        self._intrabar_task = None if _skip_wall_clock_loops else self._spawn_task("intrabar", self._intrabar_exit_loop())
        self._queue_task = None if _skip_wall_clock_loops else self._spawn_task("queue", self._priority_entry_loop())
        self._momentum_task = None if _skip_wall_clock_loops else self._spawn_task("momentum", self._momentum_loop())
        self._guard_task = None if _skip_wall_clock_loops else self._spawn_task("guard", self._portfolio_guard_loop())
        if getattr(self, "_move_selected", None) is not None and isinstance(feed, ReplayFeed):
            feed.on_day_boundary = self._move_reselect_day
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
                if getattr(self, "_move_selected", None) is not None:
                    _leaving = getattr(self, "_move_leaving", None) or set()
                    if candle.figi in _leaving:
                        await self.deactivate_figi(candle.figi, price=float(candle.open))
                        self._move_leaving.discard(candle.figi)
                        continue
                _t1 = _time.perf_counter()
                from app.bot.pending_dispatch import execution_lock
                async with execution_lock(self):
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
                    await self._finalize_replay(exited)
                except Exception as _sw_e:
                    _audit_swallow('_run@finalize_replay', _sw_e)
            if self._persist_task:
                self._persist_task.cancel()
                try:
                    await self._persist_task
                except (asyncio.CancelledError, Exception):
                    pass
            for t in ('_dispatch_task', '_held_sync_task', '_hot_add_task', '_vol_carousel_task', '_reconcile_task',
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

    async def _dispatch_loop(self):
        from app.bot.pending_dispatch import dispatch_pending

        while self.running:
            try:
                await dispatch_pending(self)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.events.log("DISPATCH_ERROR", reason=type(error).__name__)
            await asyncio.sleep(1.0)

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
            if self.config.interval_name == "1min" and self.mode != "replay" and not str(self.mode).startswith("test"):
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

        _did_execute = False if isinstance(self.broker, LiveBroker) else await self._execute_pending(figi, c)

        _new_c = EngineCandle(ts=c.ts, open=c.open, high=c.high,
                              low=c.low, close=c.close, volume=c.volume)
        from app.bot.strategy_history import update_strategy_history
        update_strategy_history(buffer, _new_c)
        # --- Ресемпл 1m → 5m: когда закрыт 5m бар (последняя минута интервала),
        # строим 5m из буфера и пишем в очередь (interval=5 в БД).
        try:
            _min = c.ts.minute
            if self.config.interval_name == "1min" and _min % 5 == 4 and len(buffer) >= 5 and self.mode != "replay" and not str(self.mode).startswith("test"):
                _tail = list(buffer)[-5:]
                if all(bar.ts == c.ts - timedelta(minutes=4 - offset) for offset, bar in enumerate(_tail)):
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
        _in_ses = _sessions_allowed(c.ts, self.config.sessions)
        # Выходные (решение владельца 02.10): закрыться ДО ухода в выходные —
        # независимо от overnight, иначе позиция живёт до понедельника.
        _wk_due = bool(getattr(self.config, "weekend_flat", True)) and _weekend_close_due(_now_end, self.config.sessions)
        if (not _closed and figi not in self._swing
                and (_wk_due or _should_force_close(_now_end, self.config.sessions, self.config.overnight))):
            if _in_ses or _wk_due:
                # В уикенд-барах (T-Invest их отдаёт) цена не рыночная — выходим
                # по последней СЕССИОННОЙ свече (пятница).
                _px = float(c.close) if _in_ses else self._last_session_close(figi, float(c.close))
                _reason = "weekend_force_close" if _wk_due else "overnight_force_close"
                _closed = await self._close_session_position(figi, _px, _reason)
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
        _trace_signal_id = None
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
                    _lock_skip = ""
                    try:
                        _lock_skip = (getattr(self, "_custom_locks", None) or {}).get(figi, "")
                    except Exception:
                        _lock_skip = ""
                    if figi in self._held:
                        strategy.p.skip_entry_side = _pos_side or _lock_skip
                    else:
                        strategy.p.skip_entry_side = _lock_skip
            except Exception as _sw_e:
                _audit_swallow('_process_candle@L3913', _sw_e)  # audit silent-except
                pass
            if self._trace is not None:
                try:
                    from app.engine.trace import Stage, Status, TraceEvent
                    _st = getattr(strategy, "_hub_rsi_state", None)
                    _ctx = {
                        "called": True,
                        "buffer": {
                            "n": len(buffer),
                            "maxlen": getattr(buffer, "maxlen", None),
                            "first_ts": (buffer[0].ts.isoformat() if buffer else None),
                            "last_ts": (buffer[-1].ts.isoformat() if buffer else None),
                            "last_close": (float(buffer[-1].close) if buffer else None),
                        },
                        "rsi_state": ({
                            "n": _st.get("n"),
                            "len": _st.get("len"),
                            "last": (round(float(_st["series"][-1]), 4) if _st.get("series") else None),
                            "prev": (round(float(_st["series"][-2]), 4)
                                     if _st.get("series") and len(_st["series"]) > 1 else None),
                        } if _st else None),
                        "held": figi in self._held,
                        "pos_side": str(self._exit_side.get(figi) or ""),
                    }
                    self._trace.emit(TraceEvent(stage=Stage.EVAL, status=Status.CREATED, ts_bar=c.ts,
                                                figi=figi, ticker=self.tickers.get(figi, ""), eval_ctx=_ctx))
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
            if sig is not None and getattr(self, "_move_selected", None) is not None:
                if figi not in self._move_selected:
                    sig = None
            if self._trace is not None and sig is not None:
                try:
                    from app.engine.trace import Stage, Status, TraceEvent, make_signal_id
                    _trace_signal_id = make_signal_id(
                        run_id=self._trace.run.run_id, contour="runtime", figi=figi,
                        strategy_id=str(getattr(sig, "strategy_id", "")),
                        strategy_version=str(getattr(strategy, "version", "")),
                        interval=str(getattr(self.config, "interval_name", "")),
                        bar_ts=sig.time, side=sig.side.value, kind=str(getattr(sig, "kind", "entry")),
                    )
                    self._trace.emit(TraceEvent(
                        stage=Stage.RAW, status=Status.CREATED, ts_bar=sig.time,
                        figi=figi, ticker=self.tickers.get(figi, ""), signal_id=_trace_signal_id,
                        side=sig.side.value, kind=str(getattr(sig, "kind", "entry")),
                        reason=str(getattr(sig, "reason", "") or ""),
                        features=dict(sig.features or {}),
                        context={"regime": (self._regimes.get(figi) or None),
                                 "hour_utc": sig.time.hour},
                    ))
                except Exception:
                    pass
            _et = (_time.perf_counter() - _t2) * 1000
            self.metrics["ensemble_ms_total"] += _et
            self.metrics["ensemble_ms_n"] += 1
            if _et > self.metrics["ensemble_ms_max"]:
                self.metrics["ensemble_ms_max"] = _et
        except Exception as e:
            _audit_swallow('_process_candle@L3930', e)  # audit silent-except
            self.events.log("SIGNAL_ERROR", figi=figi, reason=str(e)[:200])
            if self._trace is not None:
                try:
                    from app.engine.trace import Stage, Status, TraceEvent
                    self._trace.emit(TraceEvent(stage=Stage.ERROR, status=Status.ERROR, ts_bar=c.ts,
                                                figi=figi, ticker=self.tickers.get(figi, ""),
                                                error={"type": type(e).__name__, "message": str(e)[:200]}))
                except Exception:
                    pass
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
                from app.services.regime_v2.active import compute_regime_active as _CR5
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
                if self._trace is not None:
                    try:
                        from app.engine.trace import Stage, Status, TraceEvent
                        self._trace.emit(TraceEvent(stage=Stage.DECISION, status=Status.SKIPPED,
                                                    ts_bar=sig.time, figi=figi, ticker=self.tickers.get(figi, ""),
                                                    signal_id=_trace_signal_id, side=sig.side.value,
                                                    action="already_held", reason_code="already_held"))
                    except Exception:
                        pass
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
        # ENG-013 (bot-контур, 15.10): позиция уже в _held, а брокер/стрим ещё отстаёт
        # и отдаёт FLAT. Тогда политика решает «вход», и проверка _held режет его как
        # already_held, а exit-голос проглатывается как exit_flat — переворот глохнет,
        # позиция стоит днями (симптом: 3 дня шорт, потом 3 дня лонг).
        # Сторону берём из самого сигнала: при удержанной позиции и флэте источника
        # сигнал всегда встречный к ней — это ровно ветка закрытия/переворота.
        if state_now is PositionState.FLAT and figi in self._held:
            state_now = PositionState.SHORT if sig.side.value == "BUY" else PositionState.LONG
        # ENG-012 (bot-контур, 29.09): exit-intent на ФЛЭТЕ — это НЕ вход.
        # Раньше голос «закрыл шорт» (close_short → BUY, kind="exit") открывал
        # LONG: в тесте 24.09 так родились 18 из 39 сделок (46% фантомов).
        # Симметрично движку EngineRunner (exit_ignored_flat). Когда позиция
        # есть — exit-голос идёт обычным путём (противоположная сторона
        # закрывает, своя — already_held).
        if state_now is PositionState.FLAT and \
                str(getattr(sig, "kind", "entry") or "entry") == "exit":
            self._log_no_trade(figi, "exit_flat", "exit-голос на флэте проигнорирован (не вход)")
            if self._trace is not None:
                try:
                    from app.engine.trace import Stage, Status, TraceEvent
                    self._trace.emit(TraceEvent(stage=Stage.DECISION, status=Status.SKIPPED,
                                                ts_bar=sig.time, figi=figi, ticker=self.tickers.get(figi, ""),
                                                signal_id=_trace_signal_id, side=sig.side.value,
                                                action="exit_flat", reason_code="exit_flat"))
                except Exception:
                    pass
            return
        bars_held = 0
        policy = SignalPolicy()
        action, note = policy.decide(sig, state_now, bars_held)
        if self._trace is not None:
            try:
                from app.engine.trace import Stage, Status, TraceEvent
                self._trace.emit(TraceEvent(stage=Stage.DECISION, status=Status.CREATED,
                                            ts_bar=sig.time, figi=figi, ticker=ticker,
                                            signal_id=_trace_signal_id, side=sig.side.value,
                                            action=str(getattr(action, "value", action)),
                                            reason=str(note), reason_code="policy"))
            except Exception:
                pass

        from app.engine.models import DecisionAction
        if action is DecisionAction.ACCEPT_ENTRY:
            await self._ensure_risk_fresh()
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
                                   f"стоп-лимит {risk.state} (daily_pnl={risk.daily_pnl})",
                                   figi, ticker)
                return
            if bool(getattr(self.config, "momentum_only", False)):
                self._log(f"МОМЕНТУМ-ONLY: сигнал ансамбля {ticker} {sig.side.value} игнорируется")
                return
            # _sig — подсказка для _entry_card: кто именно проголосовал и почему
            # (причина сигнала). Без неё карточка входа у одиночных движков пустая.
            await self._submit_order(figi, ticker, "open", sig.side.value,
                                     meta={**dict(sig.features or {}),
                                           "_sig": {"strategy_id": str(getattr(sig, "strategy_id", "") or ""),
                                                    "side": str(getattr(getattr(sig, "side", None), "value", "") or ""),
                                                    "reason": str(getattr(sig, "reason", "") or ""),
                                                    "kind": str(getattr(sig, "kind", "entry") or "entry"),
                                                    "ts": (sig.time.isoformat()
                                                           if getattr(sig, "time", None) is not None else None)}})
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
            if bool(getattr(self.config, "flip_to_opposite", False)):
                await self._submit_order(figi, ticker, "open", sig.side.value, meta=dict(sig.features or {}))
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

    def _occupied_entry_slots(self) -> set[str]:
        occupied = set(self._held)
        for figi, order in self.pending_orders.items():
            if (getattr(order, "action", None) == "open"
                    and getattr(order, "status", "") not in ("CANCELLED", "REJECTED")):
                occupied.add(figi)
        return occupied

    async def _submit_order(self, figi: str, ticker: str, action: str, side: str, meta: dict | None = None) -> None:
        if self._trace is not None:
            try:
                from app.engine.trace import Stage, Status, TraceEvent
                self._trace.emit(TraceEvent(stage=Stage.ORDER, status=Status.CREATED,
                                            figi=figi, ticker=ticker, action=action,
                                            side=side, reason="submit"))
            except Exception:
                pass
        # P0 (RECHECK 2026-10-04): пока unresolved-ордер ждёт сверки, ЛЮБОЙ новый
        # open/close по инструменту запрещён — иначе он вытеснит запись из
        # pending_orders[figi], guard в _execute_pending перестанет видеть
        # PENDING_RECONCILIATION и возможна дубль-экспозиция.
        _cur = self.pending_orders.get(figi)
        if _cur is not None and getattr(_cur, "status", "") in ("PENDING_RECONCILIATION", "PARTIAL_FILLED"):
            self._log(f"ЗАЩИТА СВЕРКИ: {ticker} unresolved-ордер не разрешён — {action} {side} отклонён")
            self.events.log("ORDER_BLOCKED_RECONCILIATION", figi=figi, ticker=ticker,
                            action=action, side=side, pending_id=str(getattr(_cur, "id", "")))
            return
        cfg = self.config
        confirmed_broker_lot = None
        if action == "open" and str(getattr(cfg, "mode", "")) in ("live", "sandbox") and str(getattr(cfg, "feed", "")) != "replay":
            confirmed_broker_lot, reason = self._broker_candidate_admission(figi, ticker, side)
            if reason:
                self._trace_order_reject(figi, ticker, action, side, "universe_admission", reason)
                return
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
            await self._ensure_risk_fresh()
            _risk_a = self.risk_snapshot()
            _tc = TimeContext(
                cfg=cfg, side=side,
                entries_paused=bool(self.entries_paused),
                sessions_allowed=bool(_sessions_allowed(self._bot_now(), cfg.sessions)),
                is_last_hour=bool(_slh(self._bot_now(), list(cfg.sessions or []))),
                risk_allowed=bool(_risk_a.entries_allowed()),
                risk_state=str(_risk_a.state), daily_pnl=_risk_a.daily_pnl,
                loss_hold=bool(_hold_a), loss_why=str(_hold_why_a),
                already_held=bool(figi in self._held),
            )
            _res_a = run_gate_chain(TIME_GATES, _tc)
            if not _res_a.passed:
                self._reject_entry("time", _res_a.key, _res_a.detail, figi, ticker)
                self._trace_order_reject(figi, ticker, action, side, f"gate_time:{_res_a.key}", _res_a.detail)
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
            _to_s = (None if str(getattr(cfg, "feed", "")) == "replay"
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
            # В реплее/тесте стакан из fetch_orderbook — ЖИВОЙ (сейчас), а не на дату
            # прогона: проверять по нему исторические входы нельзя, гейт пропускаем
            # (иначе он глушит входы и тест-таблицы пустые; см. Signal Trace 01.10).
            if str(getattr(cfg, "feed", "")) == "replay":
                _imb_lim_s = 0.0
                _spr_lim_s = 0.0
            _ob_s = None
            if _imb_lim_s > 0 or _spr_lim_s > 0:
                try:
                    from app.services.orderbook import fetch_orderbook
                    _ob_s = await fetch_orderbook(figi, 10)
                except Exception:
                    _ob_s = None  # gate_orderbook вернёт orderbook_error
            _nb_reason = ""
            if str(getattr(cfg, "feed", "")) != "replay" and bool(getattr(cfg, "entry_news_blackout", True)):
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
            # gate_orderbook читает лимиты из cfg, а не из локалов выше, поэтому
            # в реплее исключаем его из цепочки целиком (живой стакан ≠ дата прогона).
            _gates_s = MARKET_GATES
            if str(getattr(cfg, "feed", "")) == "replay":
                _gates_s = tuple(g for g in MARKET_GATES
                                 if getattr(g, "__name__", "") != "gate_orderbook")
            _res_s = run_gate_chain(_gates_s, _mc)
            if not _res_s.passed:
                self._reject_entry("signal", _res_s.key, _res_s.detail, figi, ticker)
                self._trace_order_reject(figi, ticker, action, side, f"gate_signal:{_res_s.key}", _res_s.detail)
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
                self._trace_order_reject(figi, ticker, action, side, "gate_tf_error",
                                         f"{type(_ge).__name__}: {str(_ge)[:80]}")
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
            if bool(getattr(cfg, "rank_enabled", True)):
                try:
                    await self.trade_history()
                    _rok, _rwhy = self._rank_ok(ticker)
                    _rwhy = "" if _rok else str(_rwhy)
                except Exception as error:
                    self._reject_entry("trend", "rank_data_error", type(error).__name__, figi, ticker)
                    self._trace_order_reject(figi, ticker, action, side,
                                             "rank_data_error", type(error).__name__)
                    return
            _hmp = None
            _hmd = 0
            if bool(getattr(cfg, "entry_hm_veto", False)):
                try:
                    _hmw = max(1, int(getattr(cfg, "hm_veto_window_h", 24) or 24))
                    _hm = (await self.hm_map(_hmw)).get(str(ticker).upper())
                    if _hm:
                        _hmp, _hmd = float(_hm[0]), int(_hm[1])
                except Exception as error:
                    if str(getattr(cfg, "hm_veto_mode", "veto")).lower() == "veto":
                        self._reject_entry("trend", "hm_data_error", type(error).__name__, figi, ticker)
                        self._trace_order_reject(figi, ticker, action, side,
                                                 "hm_data_error", type(error).__name__)
                        return
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
                self._trace_order_reject(figi, ticker, action, side, f"gate_trend:{_res_b.key}", _res_b.detail)
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
            except Exception as error:
                if int(getattr(cfg, "max_sector_positions", 0) or 0) > 0:
                    self._reject_entry("portfolio", "sector_data_error", type(error).__name__, figi, ticker)
                    self._trace_order_reject(figi, ticker, action, side,
                                             "sector_data_error", type(error).__name__)
                    return
            _shorts_c = sum(1 for _f in self._held
                            if str(self._exit_side.get(_f, "")).upper() == "SHORT")
            _pctx = PortfolioContext(cfg=cfg, side=side, held_count=len(self._occupied_entry_slots()),
                                     sector=_sector, sector_count=_sector_count,
                                     short_count=_shorts_c, ticker=ticker,
                                     held_tickers=tuple(self.tickers.get(_f, "").upper()
                                                        for _f in self._held))
            _res_c = run_gate_chain(PORTFOLIO_GATES, _pctx)
            if not _res_c.passed:
                self._reject_entry("portfolio", _res_c.key, _res_c.detail, figi, ticker)
                self._trace_order_reject(figi, ticker, action, side, f"gate_portfolio:{_res_c.key}", _res_c.detail)
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
                self._trace_order_reject(figi, ticker, action, side, "ai_chase", "; ".join(_ai_skip)[:120])
                return
        # ================= STAGE D: сайзинг и маржа (запросы к брокеру) ==================
        if action == "open":
            buf = self.buffers.get(figi)
            price = float(buf[-1].close) if buf else 0.0
            if confirmed_broker_lot is not None:
                lot = confirmed_broker_lot
            else:
                lot = 0
                for instrument in self.universe:
                    if instrument.get("figi") == figi:
                        raw_lot = instrument.get("lot_size") or instrument.get("lot")
                        try:
                            lot = int(raw_lot)
                        except (TypeError, ValueError, OverflowError):
                            lot = 0
                        break
                if lot <= 0:
                    try:
                        from app.models.instrument import Instrument as _Inst
                        async with SessionLocal() as _db:
                            raw_lot = (await _db.execute(
                                select(_Inst.lot).where(_Inst.figi == figi)
                            )).scalar_one_or_none()
                        lot = int(raw_lot) if raw_lot is not None else 0
                    except Exception as error:
                        self._log(f"LOT UNKNOWN {ticker}: {type(error).__name__}")
                        lot = 0
            import math
            if not math.isfinite(price) or price <= 0 or lot <= 0:
                self._trace_order_reject(figi, ticker, action, side,
                                         "sizing_price_lot", "unknown or invalid price/lot")
                return
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
                except Exception as error:
                    self._trace_order_reject(figi, ticker, action, side,
                                             "sizing_budget_unknown", type(error).__name__)
                    return
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
                    budget = _eq * self._pos_pct() * _boost
                    self._log(
                        f"TEST BUDGET {ticker}: equity≈{_eq:.0f}₽ → слот {budget:.0f}₽ "
                        f"(слот {self._pos_pct()*_boost*100:.0f}% от EQ) · initial={cfg.initial_cash:.0f}₽"
                    )
                except Exception as e:
                    self._trace_order_reject(figi, ticker, action, side,
                                             "sizing_budget_unknown", type(e).__name__)
                    return
            if not math.isfinite(float(budget)) or budget <= 0:
                self._trace_order_reject(figi, ticker, action, side,
                                         "sizing_budget_invalid", "budget must be finite and positive")
                return
            lev = 1.0
            lot_cost = price * lot
            if price <= 0 or lot <= 0 or lot_cost <= 0:
                self._log(f"ПРОПУСК СДЕЛКИ {ticker}: [sizing] price_lot — цена={price} лот={lot}")
                self._trace_order_reject(figi, ticker, action, side, "sizing_price_lot", "цена/лот <= 0")
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
                self._trace_order_reject(figi, ticker, action, side, "sizing_budget_divide", "бюджет < лота")
                return
            # --- Маржинальное плечо: запрашиваем у брокера ДО входа, ответ в лог ---
            if isinstance(self.broker, (LiveBroker, PaperBroker)):
                try:
                    ml = await self.broker.get_max_lots(figi)
                    entry_limits = ml
                    from app.bot.session import session_state, trading_session
                    _ss = session_state(now=self._bot_now())
                    _tss = trading_session(now=self._bot_now())
                    # Маржа допустима только при явно включённом use_margin И сессии
                    # из margin_sessions. Раньше проверялся только список сессий,
                    # поэтому use_margin=false всё равно давало плечо.
                    use_margin = (bool(cfg.use_margin) and bool(cfg.margin_sessions)
                                  and _tss in cfg.margin_sessions)
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
                    self._trace_order_reject(figi, ticker, action, side,
                                             "sizing_margin_unknown", type(e).__name__)
                    return
            if not bool(cfg.use_margin):
                # Явный отказ от маржи: размер считается только по своим деньгам.
                # Ни cfg.leverage, ни брокерский максимум не увеличивают позицию,
                # поэтому плечо принудительно 1.0 даже после fallback блока MARGIN.
                lev = 1.0
                own_per_lot = lot_cost
            if budget < own_per_lot:
                self._log(f"ПРОПУСК СДЕЛКИ {ticker}: [sizing] budget — бюджет {budget:.0f} < стоимость лота {own_per_lot:.0f}")
                self._trace_order_reject(figi, ticker, action, side, "sizing_budget", "бюджет < своих на лот")
                return
            qty = max(1, int(budget / own_per_lot))
            _used_lev = lev
        if action == "open" and isinstance(self.broker, (LiveBroker, PaperBroker)):
            try:
                from app.bot.session import trading_session
                _tss = trading_session(now=self._bot_now())
                use_margin = (bool(cfg.use_margin) and bool(cfg.margin_sessions)
                              and _tss in cfg.margin_sessions)
                ml = entry_limits
                cash_max = ml.buy_cash if side == "BUY" else ml.sell_cash
                margin_max = ml.buy_margin if side == "BUY" else ml.sell_margin
                if use_margin:
                    max_lots = margin_max
                    _lim_kind = "маржа"
                else:
                    max_lots = cash_max
                    _lim_kind = "свои деньги"
                if max_lots <= 0:
                    self._log(f"ПРОПУСК СДЕЛКИ {ticker}: [sizing] margin_limit — лимит ({_lim_kind}) = 0")
                    self._trace_order_reject(figi, ticker, action, side, "sizing_margin_limit",
                                             f"лимит ({_lim_kind}) = 0")
                    return
                if qty > max_lots:
                    self._log(f"QTY CAP {ticker}: {qty} → {max_lots} ({_lim_kind}, {_tss or '—'})")
                    qty = max_lots
            except Exception as e:
                self._trace_order_reject(figi, ticker, action, side,
                                         "sizing_margin_unknown", type(e).__name__)
                return
        # --- Кап совокупной экспозиции: свои деньги в позициях <= max_exposure_pct от equity ---
        if action == "open":
            try:
                _cap = float(getattr(cfg, "max_exposure_pct", 0.0) or 0.0)
                if not math.isfinite(_cap) or _cap < 0:
                    raise ValueError("invalid exposure limit")
                if _cap > 0:
                    _eq_cap = float(await self.broker.equity() or 0.0)
                    if not math.isfinite(_eq_cap) or _eq_cap <= 0:
                        raise ValueError("unknown exposure equity")
                    if _eq_cap > 0:
                        _own_now = None
                        _ma_fn = getattr(self.broker, "margin_attributes", None)
                        if _ma_fn is not None:
                            try:
                                _ma = await _ma_fn()
                                if (_ma and _ma.get("liquid") is not None
                                        and _ma.get("starting_margin") is not None):
                                    _own_now = float(_ma["starting_margin"])
                                    _eq_cap = float(_ma["liquid"])
                            except Exception:
                                if isinstance(self.broker, LiveBroker):
                                    raise
                                _own_now = None
                        if _own_now is None and isinstance(self.broker, LiveBroker):
                            raise ValueError("broker account exposure unavailable")
                        if _own_now is None:
                            _own_now = 0.0
                            for _f in list(self._held):
                                _ep = float(self._exit_entry_px.get(_f) or 0.0)
                                _q = float(self._exit_qty.get(_f) or 0.0)
                                _lev = max(1.0, float(self._pos_leverage.get(_f, 1.0) or 1.0))
                                if (not all(math.isfinite(v) for v in (_ep, _q, _lev))
                                        or _ep <= 0 or _q <= 0):
                                    raise ValueError("unknown held-position exposure")
                                _own_now += (_ep * _q) / _lev
                        # Свои деньги НОВОЙ позиции = номинал / плечо именно этой позиции
                        # (_used_lev). Точно в обоих режимах: divide — own=бюджет/lev,
                        # multiply — own=бюджет=номинал/lev. Портфельный _eff_lev не годится:
                        # при пустом портфеле он =1 и own_new раздувался до полного номинала
                        # (лог: «свои 10000+9892 > equity 10000» при выбранном ×5).
                        _own_new = ((float(price) * int(qty) * lot)
                                    / max(1.0, float(_used_lev or 1.0)))
                        if (not all(math.isfinite(v) for v in (_own_now, _own_new, _eq_cap))
                                or _own_now < 0 or _own_new <= 0 or _eq_cap <= 0):
                            raise ValueError("invalid exposure inputs")
                        from app.engine.margin_budget import check_margin_budget
                        margin_decision = check_margin_budget(
                            liquid_value=_eq_cap,
                            committed_margin=_own_now,
                            additional_notional=float(price) * int(qty) * lot,
                            initial_risk_rate=1.0 / max(1.0, float(_used_lev or 1.0)),
                            max_use_fraction=_cap,
                        )
                        if margin_decision.reason in ("MARGIN_INPUT_UNKNOWN", "MARGIN_INPUT_INVALID"):
                            raise ValueError(margin_decision.reason)
                        if not margin_decision.allowed:
                            self._log(f"ПРОПУСК ВХОДА {ticker}: [portfolio] max_exposure — кап экспозиции "
                                      f"{_cap*100:.0f}% (свои {_own_now:.0f}+{_own_new:.0f} > equity {_eq_cap:.0f}₽)")
                            self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                            reason="MAX_EXPOSURE")
                            self._log_no_trade(figi, "max_exposure")
                            self._trace_order_reject(figi, ticker, action, side, "max_exposure")
                            return
            except Exception as _sw_e:
                self._trace_order_reject(figi, ticker, action, side,
                                         "exposure_unknown", type(_sw_e).__name__)
                return
        order = BotOrder(
            created_at=self._bot_now(),
            id=_new_order_id(),
            figi=figi,
            ticker=ticker,
            action=action,
            side=side,
            qty=qty,
            meta={**dict(meta or {}), "leverage": float(_used_lev),
                  "gate_path": list(_gate_path)},
        )
        if action == "open":
            order.meta.update(lot_size=int(lot), requested_lots=int(qty))
        # --- Портфельные лимиты (net exposure / сектор / маржа / стресс) ---
        if action == "open":
            try:
                from app.bot.portfolio import (PortfolioLimits as _PL, check_order as _pcheck,
                                               regime_limits as _rlim)
                _snap = await self.portfolio_snapshot(ttl=0.0)
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
                _notional = float(price) * int(qty) * lot
                # Разворот рынка против книги: не добавляем в убыточную сторону.
                _net = float(_snap.get("net_notional") or 0.0)
                if str(_regime.get("state")) == "reversal" and (
                        (_net < 0 and side == "SELL") or (_net > 0 and side == "BUY")):
                    self._log(f"ПРОПУСК ВХОДА {ticker}: [portfolio] market_reversal — разворот рынка против книги "
                              f"(net {_net:+.0f}₽, IMOEX 20м {_regime.get('pct_20m')}%)")
                    self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                    reason="MARKET_REVERSAL")
                    self._log_no_trade(figi, "market_reversal")
                    self._trace_order_reject(figi, ticker, action, side, "market_reversal")
                    return
                _ok_pf, _why_pf = _pcheck(_snap, side, _notional, ticker,
                                          await self.sector_meta(), _lim)
                if not _ok_pf:
                    self._log(f"ПРОПУСК ВХОДА {ticker}: [portfolio] portfolio_limit — портфельный лимит — {_why_pf}"
                              + (f" [режим {_regime.get('state')}]" if _regime.get("state") != "neutral" else ""))
                    self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                    reason="PORTFOLIO_LIMIT", detail=_why_pf)
                    self._log_no_trade(figi, "portfolio_limit")
                    self._trace_order_reject(figi, ticker, action, side, "portfolio_limit", str(_why_pf)[:140])
                    if bool(getattr(cfg, "queue_enabled", True)) and not (meta or {}).get("priority"):
                        await self._enqueue_candidate(figi, ticker, side, _why_pf,
                                                      meta=meta, snap=_snap)
                    return
            except Exception as _e:
                self._log(f"ПОРТФЕЛЬ-ЛИМИТ {ticker}: проверка не удалась ({type(_e).__name__}) — вход отклонён")
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason="PORTFOLIO_CHECK_ERROR", detail=type(_e).__name__)
                self._log_no_trade(figi, "portfolio_check_error")
                self._trace_order_reject(figi, ticker, action, side,
                                         "portfolio_check_error", type(_e).__name__)
                return
        # --- AI-гейт: дедуп и пауза после отклонения ---
        if action == "open" and not _is_ai and bool(getattr(cfg, "ai_approval", False)):
            _pend = self.pending_orders.get(figi)
            if _pend is not None and getattr(_pend, "status", "") == "PENDING_APPROVAL":
                self._log(f"AI-ГЕЙТ: {ticker} уже ждёт решения — новую заявку не создаём")
                self._log_no_trade(figi, "ai_already_pending")
                self._trace_order_reject(figi, ticker, action, side, "ai_already_pending")
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
                self._trace_order_reject(figi, ticker, action, side, "ai_reject_cooldown")
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

    def _broker_candidate_admission(self, figi, ticker, side):
        from app.bot.universe.domain import InstrumentRef
        from app.bot.universe.eligibility import broker_eligibility

        if getattr(self, "_candidate_mode", None) != str(getattr(self.config, "mode", "")):
            return None, "broker_evidence_mode_mismatch"
        evidence = getattr(self, "_candidate_catalog", {}).get(figi)
        decision = broker_eligibility(
            InstrumentRef(ticker=ticker, figi=figi), evidence,
            side="long" if side == "BUY" else "short",
            observed_at=getattr(self, "_candidate_observed_at", None),
            as_of=datetime.now(timezone.utc),
        )
        if not decision.eligible or figi not in getattr(self, "_candidate_figis", set()):
            return None, "; ".join(decision.reasons) or "outside_selected_universe"
        return evidence["lot"], None

    async def _persist_entry_fill(self, figi, order, executed_lots, price, plan):
        order.meta = dict(order.meta or {})
        order.meta["client_order_id"] = order.id
        broker = self.broker
        scope = (getattr(broker, 'mode', None), getattr(broker, '_account', None))
        evidence = None
        try:
            if isinstance(broker, LiveBroker):
                from app.services.broker_fill_evidence import read_order_fill_evidence, entry_evidence_metadata
                try:
                    evidence = await read_order_fill_evidence(broker, mode=scope[0], broker_account_id=scope[1],
                        client_order_id=order.id, figi=figi, side=order.side,
                        requested_lots=order.meta.get('requested_lots'), lot_size=order.meta.get('lot_size'),
                        as_of=self._bot_now())
                    entry_evidence_metadata(evidence, mode=scope[0], broker_account_id=scope[1],
                        client_order_id=order.id, figi=figi, side=order.side,
                        units=executed_lots * order.meta['lot_size'], lot_size=order.meta['lot_size'], price=price)
                    order.meta.pop('entry_evidence_error', None)
                except Exception as error:
                    evidence = None
                    order.meta['entry_evidence_error'] = type(error).__name__
                if (self.broker is not broker or (broker.mode, broker._account) != scope
                        or getattr(self, 'broker_mode', scope[0]) != scope[0]):
                    raise ValueError('broker_scope_changed_during_entry_evidence')
            await self._st_open(
                figi, order.ticker, order.side, executed_lots, price,
                plan.stop_loss, plan.take_profit, meta=order.meta, entry_evidence=evidence,
                leverage=max(1.0, float((order.meta or {}).get("leverage")
                                       or self.config.leverage or 1.0)),
            )
            if isinstance(broker, LiveBroker) and (self.broker is not broker
                    or (broker.mode, broker._account) != scope
                    or getattr(self, 'broker_mode', scope[0]) != scope[0]):
                raise ValueError('broker_scope_changed_during_entry_persistence')
            return True
        except Exception:
            self._entry_pause().set_blocker('entry_reconciliation', True, *scope, order.id)
            order.status = "PENDING_RECONCILIATION"
            order.meta = dict(order.meta or {})
            order.meta.update(ledger_write_failed=True,
                              confirmed_executed_lots=executed_lots,
                              confirmed_fill_price=price)
            self.pending_orders[figi] = order
            self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                            ticker=order.ticker, order_id=order.id,
                            action="open", reason="entry_ledger_write_failed")
            return False

    async def _execute_pending(self, figi: str, c) -> bool:
        """Process pending order."""
        from app.bot.execution_reference import ExecutionReference
        is_reference = isinstance(c, ExecutionReference)
        order = self.pending_orders.pop(figi, None)
        if order is None or order.status == "CANCELLED":
            return False
        if order.status in ("PENDING_RECONCILIATION", "PARTIAL_FILLED"):
            self._log(f"RECONCILIATION: {order.ticker} ждёт сверки, повторная отправка заблокирована")
            self.pending_orders[figi] = order
            return False
        execution_price = c.price if is_reference else c.open
        execution_at = c.market_at if is_reference else c.ts
        if order.action == "open" and isinstance(self.broker, LiveBroker):
            try:
                await self._entry_lot_size(figi, order.meta)
            except ValueError:
                order.status = "REJECTED"
                self.events.log("ORDER_REJECTED", figi=figi, ticker=order.ticker,
                                order_id=order.id, reason="confirmed_entry_lot_missing")
                return False
        cfg = self.config
        # --- Подтверждение входа (крос-ТФ фильтр, research-bundle): перед открытием
        # проверяем, был ли сигнал ENTRY_CONFIRM_ENGINE@TF в окне ±N минут.
        # Env: ENTRY_CONFIRM_ENGINE / ENTRY_CONFIRM_TF / ENTRY_CONFIRM_WINDOW_MIN.
        # Fail-open при сбое проверки (лог), чтобы не блокировать торги из-за багa.
        if order.action == "open":
            import os as _os
            _conf = (_os.environ.get("ENTRY_CONFIRM_ENGINE") or "").strip()
            if _conf:
                try:
                    _w = int(_os.environ.get("ENTRY_CONFIRM_WINDOW_MIN", "5") or 5)
                    _tf = (_os.environ.get("ENTRY_CONFIRM_TF") or "10min").strip()
                    _tfsec = {"1min": 60, "5min": 300, "10min": 600, "15min": 900}.get(_tf, 600)
                    _buf = list(self.buffers.get(figi) or [])
                    if _tfsec > 60:
                        from app.services.ensemble import resample as _rs
                        _bars = _rs(_buf, _tfsec)
                    else:
                        _bars = _buf
                    from app.engine.strategies import build_strategy as _bs
                    _sigs = _bs(_conf, {}).on_bar(_bars) or []
                    _now = execution_at
                    _ok = any(abs((getattr(_s, "ts", _now) - _now).total_seconds()) <= _w * 60
                              for _s in _sigs)
                    if not _ok:
                        order.status = "CANCELLED"
                        self._log(f"КОНФИРМ: вход {order.ticker} отменён — нет "
                                  f"{_conf}@{_tf} в ±{_w} мин")
                        self.events.log("ENTRY_CONFIRM_REJECT", figi=figi,
                                        ticker=order.ticker, reason=f"no {_conf}@{_tf} +-{_w}m")
                        return False
                except Exception as _ce:
                    self._log(f"КОНФИРМ: ошибка проверки {type(_ce).__name__}: {str(_ce)[:90]}")
        # --- Фильтр BUY по дневке (research): перепроданность дневки, волатильность дня,
        # положительный импульс H1. Env: ENTRY_FILTER_D1 (+ _ATR_MIN / _RSI_MAX). Fail-open.
        if (order.action == "open"
                and str(order.side).upper() in ("BUY", "LONG", "OPEN_LONG")):
            import os as _fos
            _d1_on = (_fos.environ.get("ENTRY_FILTER_D1") or "").strip().lower() in (
                "1", "true", "on", "yes")
            if _d1_on:
                try:
                    from app.bot.entry_filter_d1 import (BUY_ATR_MIN, D1_INTERVAL, H1_SECONDS,
                                                         buy_verdict, h1_ret3, previous_day_features)
                    from app.services.ensemble import resample as _frs
                    _fstep = STEP_SEC.get(getattr(cfg, "interval_name", ""), 300)
                    _fref = execution_at if getattr(execution_at, "tzinfo", None) else execution_at.replace(tzinfo=timezone.utc)
                    _fref = _fref if is_reference else _fref + timedelta(seconds=_fstep)
                    _fatr = float(_fos.environ.get("ENTRY_FILTER_D1_ATR_MIN") or BUY_ATR_MIN)
                    _frsi = float(_fos.environ.get("ENTRY_FILTER_D1_RSI_MAX") or 45.0)
                    async with SessionLocal() as _fdb:
                        _fdaily = await _lc(_fdb, figi, D1_INTERVAL,
                                            date_from=_fref - timedelta(days=120))
                    _fdr, _fda = previous_day_features(_fdaily, _fref)
                    _fbuf = list(self.buffers.get(figi) or [])
                    _fh1 = _frs(_fbuf, H1_SECONDS) if _fbuf else []
                    _fhr = h1_ret3(_fh1, _fref)
                    _fv = buy_verdict(_fdr, _fda, _fhr, atr_min=_fatr, rsi_max=_frsi)
                    self.events.log("ENTRY_FILTER_D1", figi=figi, ticker=order.ticker,
                                    decision="allow" if _fv.allowed else "reject",
                                    reason=_fv.reason, d_rsi=_fv.d_rsi, d_atr=_fv.d_atr,
                                    h_ret3=_fv.h_ret3)
                    if not _fv.allowed:
                        order.status = "CANCELLED"
                        self._log(f"ФИЛЬТР D1: вход {order.ticker} отменён — {_fv.reason}")
                        self._trace_order_reject(figi, order.ticker, order.action, str(order.side),
                                                 "entry_filter_d1_reject", _fv.reason,
                                                 ts_bar=getattr(c, "ts", None))
                        return False
                except Exception as _fe:
                    self._log(f"ФИЛЬТР D1: ошибка проверки {type(_fe).__name__}: "
                              f"{str(_fe)[:90]} (fail-open)")
        if (order.action == "open"
                and str(order.side).upper() in ("SELL", "SHORT", "OPEN_SHORT")):
            import os as _fos2
            _d1s_on = (_fos2.environ.get("ENTRY_FILTER_D1_SELL") or "").strip().lower() in (
                "1", "true", "on", "yes")
            if _d1s_on:
                try:
                    from app.bot.entry_filter_d1 import (H1_SECONDS, M5_SECONDS, h1_ret3,
                                                         m5_ret12, sell_verdict)
                    from app.services.ensemble import resample as _frs2
                    _fstep2 = STEP_SEC.get(getattr(cfg, "interval_name", ""), 300)
                    _fref2 = execution_at if getattr(execution_at, "tzinfo", None) else execution_at.replace(tzinfo=timezone.utc)
                    _fref2 = _fref2 if is_reference else _fref2 + timedelta(seconds=_fstep2)
                    _fh3 = float(_fos2.environ.get("ENTRY_FILTER_D1_SELL_H3") or -0.001)
                    _fm5 = float(_fos2.environ.get("ENTRY_FILTER_D1_SELL_M5") or -0.0006)
                    _sbuf = list(self.buffers.get(figi) or [])
                    _sh1 = _frs2(_sbuf, H1_SECONDS) if _sbuf else []
                    _sm5b = _frs2(_sbuf, M5_SECONDS) if _sbuf else []
                    _shr = h1_ret3(_sh1, _fref2)
                    _smr = m5_ret12(_sm5b, _fref2)
                    _sv = sell_verdict(_shr, _smr, h3_max=_fh3, m5_max=_fm5)
                    self.events.log("ENTRY_FILTER_D1_SELL", figi=figi, ticker=order.ticker,
                                    decision="allow" if _sv.allowed else "reject",
                                    reason=_sv.reason, h_ret3=_sv.h_ret3, m5_ret12=_sv.m5_ret12)
                    if not _sv.allowed:
                        order.status = "CANCELLED"
                        self._log(f"ФИЛЬТР D1 SELL: вход {order.ticker} отменён — {_sv.reason}")
                        self._trace_order_reject(figi, order.ticker, order.action, str(order.side),
                                                 "entry_filter_d1_sell_reject", _sv.reason,
                                                 ts_bar=getattr(c, "ts", None))
                        return False
                except Exception as _fe2:
                    self._log(f"ФИЛЬТР D1 SELL: ошибка проверки {type(_fe2).__name__}: "
                              f"{str(_fe2)[:90]} (fail-open)")
        # Пауза новых входов: НЕ открываем новые позиции из очереди (закрытия/выходы — можно).
        if order.action == "open":
            refreshed = await self._ensure_risk_fresh()
            if not refreshed or not self.risk_snapshot().entries_allowed():
                order.status = "CANCELLED"
                self.events.log("ORDER_CANCELLED", figi=figi, ticker=order.ticker,
                                order_id=order.id, action="open", reason="risk_not_allowed")
                return False
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
                self._trace_order_reject(figi, order.ticker, order.action, str(order.side),
                                         "ai_approval_rejected", _why,
                                         ts_bar=getattr(c, "ts", None))
                return False
            self._approvals_since.pop(order.id, None)
        if order.action == "close":
            try:
                position = await self.broker.get_position(figi)
            except Exception as error:
                self.pending_orders.setdefault(figi, order)
                self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                order_id=order.id, reason="signal_close_position_unavailable",
                                error_type=type(error).__name__)
                return False
            if position is None:
                self.pending_orders.setdefault(figi, order)
                return False
            order.qty = position.qty
            outcome = await self._submit_close_reconciled(
                figi, order.ticker, order.qty, execution_price, "signal_exit",
                paper_filled=False, close_order=order)
            if outcome["status"] == "reconciled_closed":
                return True
            if outcome["status"] == "pending_partial":
                self._exit_qty[figi] = outcome["residual"]
            if outcome["status"] != "closed":
                if outcome["order"] is None and figi not in self.pending_orders:
                    self.pending_orders[figi] = order
                return False
            trade = outcome["trade"]
            actual_exit = price_from_trade(trade) if trade else execution_price
            order.status = "FILLED"
            order.filled_at = datetime.now(timezone.utc)
            order.price = actual_exit
            self._held.discard(figi)
            self._clear_exit_state(figi)
            _bars_held = self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter)
            _exit_meta_sig = {"bars_held": _bars_held, "signal_note": "signal_exit",
                              "gate_path_in": list((order.meta or {}).get("gate_path") or [])}
            await self._st_close(figi, actual_exit, execution=trade, reason="signal_exit",
                                  net=float(trade.net_pnl) if trade else None,
                                  meta=_exit_meta_sig)
            pnl = float(trade.net_pnl) if trade else 0
            self._log(f"СДЕЛКА ЗАКРЫТИЕ {order.ticker} @ {actual_exit:.2f} pnl={pnl:+.2f} (candle={execution_price:.2f})")
            self.events.log("ORDER_FILLED", figi=figi, ticker=order.ticker,
                            order_id=order.id, price=actual_exit, action="close",
                            net_pnl=float(trade.net_pnl) if trade else None)
            if self._trace is not None:
                try:
                    from app.engine.trace import Stage, Status, TraceEvent
                    self._trace.emit(TraceEvent(stage=Stage.FILL, status=Status.EXECUTED, ts_bar=execution_at,
                                                figi=figi, ticker=order.ticker, order_id=order.id,
                                                action="close", side=str(order.side),
                                                price=float(actual_exit), qty=getattr(order, "qty", None),
                                                reason="signal_exit"))
                except Exception:
                    pass
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
            _rr = getattr(getattr(strat, "p", None), "rr", None)
            if _rr is None:
                _rr = cfg.atr_risk_reward
            # Проскальзывание на входе (adverse) — parity с бэктестом (fill_price).
            _cm = CostModel(commission_rate=cfg.commission_rate, slippage_bps=cfg.slippage_bps)
            _fill = _cm.fill_price(float(execution_price), side)
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
                from app.bot.exit_trail_r import r_trail_kwargs as _rtr
                _rtr_kw = _rtr()
                if _rtr_kw:
                    exit_policy = AtrStopPolicy(period=cfg.atr_period, multiplier=_sl_mult,
                                                risk_reward=_rr, **_rtr_kw)
                    _b1 = list(self.buffers.get(figi, []))
                    buf_raw = self._get_5m_bars(figi, _b1) or _b1
                    plan = exit_policy.plan_entry(side, _fill, buf_raw)
                else:
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
            import os as _os
            _sl1r = (_os.environ.get("SL1R") or "").strip().lower()
            _cm = CostModel(commission_rate=cfg.commission_rate, slippage_bps=cfg.slippage_bps)
            _fill = _cm.fill_price(float(execution_price), side)
            if _sl1r and _sl1r not in ("0", "false", "off", "no"):
                exit_policy = FixedSlTpPolicy(stop_pct=0.022, target_pct=50.0)
                plan = exit_policy.plan_entry(side, _fill, [])
            else:
                from app.bot.exit_trail_r import r_trail_kwargs as _rtr3
                _rtr3_kw = _rtr3()
                if _rtr3_kw:
                    _s3 = self.strategies.get(figi)
                    _rr3 = getattr(getattr(_s3, "p", None), "rr", None)
                    exit_policy = AtrStopPolicy(period=cfg.atr_period,
                                                 multiplier=cfg.initial_sl_atr,
                                                 risk_reward=float(_rr3) if _rr3 else cfg.atr_risk_reward,
                                                 **_rtr3_kw)
                    _b3 = list(self.buffers.get(figi, []))
                    buf_raw3 = self._get_5m_bars(figi, _b3) or _b3
                    plan = exit_policy.plan_entry(side, _fill, buf_raw3)
                elif cfg.sl_mode == "atr":
                    exit_policy = AtrStopPolicy(
                        period=cfg.atr_period,
                        multiplier=cfg.initial_sl_atr,
                        risk_reward=cfg.atr_risk_reward,
                        trail_activation_comm_mult=cfg.trail_activation_comm_mult,
                        trail_distance_r=cfg.trail_distance_atr,
                        trail_compress_r=cfg.trail_compress_r,
                        trail_min_factor=cfg.trail_min_factor,
                        trail_min_atr=cfg.trail_min_atr,
                        trail_vol_boost=cfg.trail_vol_boost)
                    entry_bars = list(self.buffers.get(figi, []))
                    plan = exit_policy.plan_entry(side, _fill, entry_bars)
                else:
                    exit_policy = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
                    plan = exit_policy.plan_entry(side, _fill, [])
        # Уровень, не прошедший проверку, означает ошибку единиц или стратегии.
        # Писать его нельзя, но и открывать позицию без стопа тоже: 15.11.2026
        # в paper-контуре ASTR при 213.17 получила sl=10873.71, SBER при 276.64
        # — sl=-13552.42. Дистанция ровно 50x цены, причём сторона формально
        # правильная, поэтому проверяется ещё и масштаб. Такой вход отклоняем
        # целиком и громко сообщаем причину: молчаливая подмена уровня скрывала
        # бы дефект, а позиция без защиты недопустима.
        _bad_levels: list[str] = []
        for _lv, _kind, _name in ((plan.stop_loss, "stop", "SL"),
                                  (plan.take_profit, "target", "TP")):
            if _lv is None:
                continue
            _why = level_problem(_lv, _kind, order.side, _fill)
            if _why is None and level_implausible(_lv, _fill):
                _why = f"дальше {int(PLAUSIBLE_LEVEL_SPAN * 100)}% от входа — ошибка единиц"
            if _why is not None:
                _bad_levels.append(f"{_name} {float(_lv):.2f} ({_why})")
        if _bad_levels:
            order.status = "REJECTED"
            self._log(f"⛔ ВХОД ОТКЛОНЁН {order.ticker} @ {float(_fill):.2f} "
                      f"{order.side}: {', '.join(_bad_levels)} — проверь stop_pct/"
                      f"target_pct (доля, не проценты) и sl_mode")
            self.events.log("SLTP_INVALID_LEVEL", figi=figi, ticker=order.ticker,
                            side=str(getattr(order.side, "value", order.side)),
                            entry=float(_fill), dropped=", ".join(_bad_levels))
            self._trace_order_reject(figi, order.ticker, "open", str(order.side),
                                     "invalid_level", ", ".join(_bad_levels),
                                     ts_bar=getattr(c, "ts", None))
            return False
        entry_scope = None
        try:
            broker_qty = int(order.qty)
            if isinstance(self.broker, PaperBroker):
                from app.models.instrument import Instrument
                async with SessionLocal() as quantity_db:
                    instrument_lot = (await quantity_db.execute(
                        select(Instrument.lot).where(Instrument.figi == figi)
                    )).scalar_one_or_none()
                if instrument_lot is None or int(instrument_lot) <= 0:
                    raise ValueError("Paper execution requires a known positive instrument lot")
                broker_qty *= int(instrument_lot)
            # ARCH160: client_order_id — только живому брокеру (PaperBroker его не принимает).
            _open_kwargs = {}
            if not isinstance(self.broker, PaperBroker):
                _open_kwargs["client_order_id"] = str(getattr(order, "id", "") or "") or None
            if isinstance(self.broker, LiveBroker):
                current_lot, admission_error = self._broker_candidate_admission(
                    figi, order.ticker, order.side)
                if not admission_error and current_lot != (order.meta or {}).get("lot_size"):
                    admission_error = "broker_lot_changed_since_sizing"
                if admission_error:
                    order.status = "REJECTED"
                    self.events.log("ORDER_REJECTED", figi=figi, ticker=order.ticker,
                                    order_id=order.id, reason=admission_error)
                    return False
            from app.services.exit_policy_snapshot import ExitPolicySnapshot
            order.meta = {**(order.meta or {}), 'entry_exit_policy': ExitPolicySnapshot.capture(exit_policy).to_payload()}
            entry_broker = self.broker
            entry_scope = None
            if isinstance(entry_broker, LiveBroker):
                entry_scope = (entry_broker.mode, entry_broker._account)
                order.meta = {**(order.meta or {}), "broker_scope": entry_scope,
                              "requested_lots": broker_qty, "durable_open": True}
                order.status = 'PENDING_RECONCILIATION'
                self.pending_orders[figi] = order
                self._entry_pause().set_blocker('entry_reconciliation', True, *entry_scope, order.id)
            actual_entry = await entry_broker.open_position(
                figi=figi,
                ticker=order.ticker,
                side=order.side,
                qty=broker_qty,
                price=_fill,
                stop_loss=round(plan.stop_loss, 6) if plan.stop_loss is not None else None,
                take_profit=round(plan.take_profit, 6) if plan.take_profit is not None else None,
                strategy_id=cfg.strategy_id,
                price_is_filled=True,
                **_open_kwargs,
                plan_meta={
                    "exit_policy": order.meta["entry_exit_policy"],
                    "lot_size": (order.meta or {}).get("lot_size"),
                    "leverage": (order.meta or {}).get("leverage") or getattr(cfg, "leverage", 1) or 1,
                    "atr": plan.atr,
                    "atr_period": plan.atr_period,
                    "atr_as_of": plan.atr_as_of,
                    "atr_multiplier": plan.atr_multiplier,
                    "policy_id": plan.policy_id,
                    "atr_tf": str(getattr(cfg, "interval_name", "") or "") or None,
                    "plan_entry_price": float(_fill),
                },
            )
            if entry_scope is not None and (self.broker is not entry_broker
                    or (entry_broker.mode, entry_broker._account) != entry_scope
                    or getattr(self, 'broker_mode', entry_scope[0]) != entry_scope[0]):
                raise EntrySubmissionUnknown('entry_runtime_scope_changed_after_submission')
        except (TimeoutError, EntrySubmissionUnknown) as _te:
            if isinstance(_te, EntrySubmissionUnknown) and entry_scope is not None:
                order.meta = {**(order.meta or {}), 'durable_open': True}
                self._entry_pause().set_blocker('entry_reconciliation', True, *entry_scope, order.id)
            # ARCH160: таймаут = исход НЕИЗВЕСТЕН (ордер мог исполниться после разрыва),
            # а не отказ. Нельзя помечать REJECTED и нельзя пере-отправлять новый ордер:
            # удерживаем pending до сверки; повторная отправка блокируется статусом.
            order.status = "PENDING_RECONCILIATION"
            self.pending_orders[figi] = order
            self._log(f"⏳ ИСХОД НЕИЗВЕСТЕН {order.ticker} {order.side} qty={order.qty}: "
                      f"ответ не подтверждён — ждём сверки, повторная отправка заблокирована")
            self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi, ticker=order.ticker,
                            order_id=order.id, action="open", reason="submission_unknown_outcome")
            return False
        except Exception as _e:
            if entry_scope is not None:
                self._entry_pause().set_blocker("entry_reconciliation", False, *entry_scope, order.id)
            # Ошибка брокера (нехватка средств, отказ биржи) НЕ должна ронять рантайм:
            # помечаем ордер REJECTED, логируем — бот продолжает работать.
            order.status = "REJECTED"
            self._log(f"⛔ ОРДЕР ОТКЛОНЁН БРОКЕРОМ {order.ticker} {order.side} "
                      f"qty={order.qty}: {type(_e).__name__}: {str(_e)[:140]}")
            self.events.log("ORDER_REJECTED", figi=figi, ticker=order.ticker,
                            order_id=order.id, action="open", error=str(_e)[:200])
            self._trace_order_reject(figi, order.ticker, "open", str(order.side),
                                     "broker_reject", f"{type(_e).__name__}: {str(_e)[:140]}",
                                     error={"type": type(_e).__name__, "message": str(_e)[:200]},
                                     ts_bar=getattr(c, "ts", None))
            return False
        if isinstance(entry_broker, LiveBroker):
            from app.bot.open_submission import OpenSubmission
            order.status = 'PENDING_RECONCILIATION'
            self.pending_orders[figi] = order
            order.meta = {**(order.meta or {}), 'durable_open': True}
            self._entry_pause().set_blocker('entry_reconciliation', True, *entry_scope, order.id)
            if (not isinstance(actual_entry, OpenSubmission)
                    or (actual_entry.mode, actual_entry.broker_account_id) != entry_scope
                    or actual_entry.client_order_id != order.id or actual_entry.figi != figi
                    or actual_entry.side != order.side or actual_entry.lot_size != order.meta.get('lot_size')
                    or actual_entry.requested_units != broker_qty * actual_entry.lot_size):
                self.events.log('ORDER_RECONCILIATION_REQUIRED', figi=figi, order_id=order.id,
                                reason='opening_submission_receipt_mismatch')
                return False
            await self._reconcile_pending_orders(only_figi=figi)
            return order.status == 'FILLED' and figi not in self.pending_orders
        if actual_entry is None:
            order.status = "PENDING_RECONCILIATION"
            self.pending_orders[figi] = order
            self._log(f"⛔ ВХОД НЕ ПОДТВЕРЖДЁН {order.ticker} {order.side}: ждём reconciliation")
            self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi, ticker=order.ticker,
                            order_id=order.id, action="open", reason="unknown_broker_result")
            return False
        _exec = getattr(self.broker, "last_execution", None)
        _ex_lots = int(getattr(_exec, "executed_lots", 0) or 0)
        _req_lots = int(getattr(_exec, "requested_lots", 0) or 0)
        if _exec is not None and _ex_lots > 0 and _ex_lots < _req_lots:
            # ARCH160: частичное исполнение входа. Фиксируем только исполненную часть,
            # остаток НЕ считаем исполненным и не отправляем повторно без сверки.
            entry_px = actual_entry if actual_entry and actual_entry > 0 else execution_price
            order.status = "PARTIAL_FILLED"
            self.pending_orders[figi] = order
            order.filled_at = datetime.now(timezone.utc)
            order.price = entry_px
            order.qty = _req_lots - _ex_lots
            self._held.add(figi)
            self._held_since.setdefault(figi, __import__("time").monotonic())
            await self._persist_entry_fill(figi, order, _ex_lots, entry_px, plan)
            _lot_partial = await self._entry_lot_size(figi, order.meta)
            self._exit_qty[figi] = int(_ex_lots) * int(_lot_partial)
            self._exit_plans[figi] = exit_policy
            self._exit_side[figi] = "LONG" if order.side == "BUY" else "SHORT"
            self._exit_entry_px[figi] = float(entry_px)
            self._pos_leverage[figi] = max(1.0, float((order.meta or {}).get("leverage") or 1.0))
            self._trail_active[figi] = False
            self._trail_stop[figi] = float(plan.stop_loss) if plan.stop_loss is not None else 0.0
            self._peak_pnl[figi] = {"pnl": 0.0, "ts": execution_at, "price": float(entry_px),
                                    "atr_abs": None, "atr_pct": None, "mae": 0.0}
            self._gb_peak[figi] = 0.0
            self._trail_info.pop(figi, None)
            if plan.take_profit is not None:
                self._exit_target[figi] = float(plan.take_profit)
            self._entry_bar_index[figi] = self._bar_counter
            self._log(f"ЧАСТИЧНЫЙ ВХОД {order.ticker} {order.side}: исполнено={_ex_lots} лотов, остаток={order.qty} лотов — ждём сверки")
            self.events.log("ORDER_PARTIAL_FILLED", figi=figi, ticker=order.ticker,
                            order_id=order.id, price=entry_px, action="open",
                            executed_lots=_ex_lots, remaining_lots=order.qty)
            return False
        entry_px = actual_entry if actual_entry and actual_entry > 0 else execution_price
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
        ledger_saved = await self._persist_entry_fill(figi, order, order.qty, entry_px, plan)
        self._exit_plans[figi] = exit_policy
        self._exit_side[figi] = "LONG" if order.side == "BUY" else "SHORT"
        self._exit_entry_px[figi] = float(entry_px)
        _lot_entry = await self._entry_lot_size(figi, order.meta)
        self._exit_qty[figi] = int(order.qty) * int(_lot_entry)
        self._pos_leverage[figi] = max(1.0, float((order.meta or {}).get("leverage") or 1.0))
        self._trail_active[figi] = False
        self._trail_stop[figi] = float(plan.stop_loss) if plan.stop_loss is not None else 0.0
        # Старт пика P&L: от входа, лучший ход = 0.
        self._peak_pnl[figi] = {"pnl": 0.0, "ts": execution_at, "price": float(entry_px),
                                "atr_abs": None, "atr_pct": None, "mae": 0.0}
        self._gb_peak[figi] = 0.0
        # Виртуальный трейл — строго с нуля на каждой позиции. Раньше _trail_info
        # жил до _st_close, а часть путей закрытия (_st_close bypass: closed_at_broker,
        # duplicate_cleanup) его не вычищала → новая позиция по тому же figi
        # наследовала activated=True и hit_ts ЧУЖОЙ сделки (PLZL 04.09.2026).
        self._trail_info.pop(figi, None)
        if plan.take_profit is not None:
            self._exit_target[figi] = float(plan.take_profit)
        self._entry_bar_index[figi] = self._bar_counter
        if not ledger_saved:
            return False
        self._log(f"СДЕЛКА ВХОД {order.ticker} {order.side} qty={order.qty} @ {entry_px:.2f} (candle={execution_price:.2f})")
        if self._trace is not None:
            try:
                from app.engine.trace import Stage, Status, TraceEvent
                self._trace.emit(TraceEvent(stage=Stage.FILL, status=Status.EXECUTED, ts_bar=execution_at,
                                            figi=figi, ticker=order.ticker, order_id=order.id,
                                            action="open", side=str(order.side),
                                            price=float(entry_px), qty=getattr(order, "qty", None)))
            except Exception:
                pass
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
        self._gb_peak.pop(figi, None)

    async def _ensure_exit_state(self, figi: str, c, pos) -> None:
        """Ленивая инициализация учёта выхода, если позиция есть у брокера,
        но её нет в _exit_plans (рестарт/ручное открытие/reconcile).

        Уровни считаем от текущего ATR (5m), entry — из фактической позиции.
        """
        if isinstance(self.broker, LiveBroker):
            scope = (self.broker.mode, self.broker._account)
            blockers = self._entry_pause().blockers
            if (figi in self.pending_orders
                    or ('position_recovery_failed', *scope) in blockers
                    or ('position_recovery_failed', *scope, figi) in blockers):
                return
        if figi in self._exit_plans:
            return
        if pos is None:
            return
        try:
            cfg = self.config
            _side = "LONG" if str(getattr(pos, "side", "LONG")).upper() in ("LONG", "BUY") else "SHORT"
            _side_enum = Side.BUY if _side == "LONG" else Side.SELL
            _entry_px = self._exit_entry_px.get(figi) or float(getattr(pos, "entry_price", 0) or c.open)
            from app.bot.exit_trail_r import r_trail_kwargs as _rtr2
            _rtr2_kw = _rtr2()
            if _rtr2_kw:
                _strat = self.strategies.get(figi)
                _rrc = getattr(getattr(_strat, "p", None), "rr", None)
                _pol = AtrStopPolicy(period=cfg.atr_period,
                                     multiplier=cfg.initial_sl_atr,
                                     risk_reward=float(_rrc) if _rrc else cfg.atr_risk_reward,
                                     **_rtr2_kw)
                _buf = self.buffers.get(figi)
                _bars1 = list(_buf) if _buf else [c]
                _pl = _pol.plan_entry(_side_enum, _entry_px, _bars1)
            elif cfg.sl_mode == "fixed":
                _pol = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
                _pl = _pol.plan_entry(_side_enum, _entry_px, [])
            else:
                _strat = self.strategies.get(figi)
                _rrc = getattr(getattr(_strat, "p", None), "rr", None)
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
            # Виртуальный трейл после рестарта — с нуля (см. _open_position).
            self._trail_info.pop(figi, None)
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
                            SandboxTrade.mode == self.broker_mode,
                            SandboxTrade.broker_account_id == _account_scope_of(self))
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

    def _giveback_exit(self, figi: str, c, state, entry_px: float, qty_sh: int
                       ) -> tuple[float | None, str]:
        """Выход по откату от пика (trail_activation_pct/trail_giveback_pct, по close закрытого бара)."""
        p = getattr(self.config, "params", None) or {}
        try:
            act = float(p.get("trail_activation_pct") or 0.0)
            giv = float(p.get("trail_giveback_pct") or 0.0)
        except (TypeError, ValueError):
            return None, ""
        if act <= 0.0 or giv <= 0.0 or entry_px <= 0 or qty_sh <= 0:
            return None, ""
        sgn = 1.0 if state == PositionState.LONG else -1.0
        upnl = (float(c.close) - float(entry_px)) * qty_sh * sgn
        notional = float(entry_px) * qty_sh
        peak, hit = _giveback_step(upnl, self._gb_peak.get(figi), notional, act, giv)
        self._gb_peak[figi] = peak
        if hit:
            return float(c.close), "trail_exit"
        return None, ""

    async def _close_session_position(self, figi: str, price: float, reason: str) -> bool:
        try:
            position = await self.broker.get_position(figi)
        except Exception as error:
            self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                            reason="session_close_position_unavailable", error_type=type(error).__name__)
            return False
        if position is None:
            return False
        outcome = await self._submit_close_reconciled(
            figi, position.ticker, position.qty, price, reason, paper_filled=False)
        if outcome["status"] == "reconciled_closed":
            return True
        if outcome["status"] == "pending_partial":
            self._exit_qty[figi] = outcome["residual"]
        if outcome["status"] != "closed":
            return False
        trade = outcome["trade"]
        self._held.discard(figi)
        self._opposite_count.pop(figi, None)
        self._last_exit_bar[figi] = self._bar_counter
        self._clear_exit_state(figi)
        bars_held = self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter)
        await self._st_close(figi, float(trade.exit_price), execution=trade, reason=reason,
                             net=float(trade.net_pnl), meta={"bars_held": bars_held})
        self.events.log("POSITION_CLOSED", figi=figi, reason=reason,
                        price=float(trade.exit_price), net_pnl=float(trade.net_pnl))
        return True

    def _close_inflight(self) -> set:
        inflight = getattr(self, "_closing_inflight", None)
        if inflight is None:
            inflight = set()
            self._closing_inflight = inflight
        return inflight

    def _close_pending_block(self, figi: str):
        pending = (getattr(self, "pending_orders", None) or {}).get(figi)
        if pending is not None and getattr(pending, "status", "") not in ("FILLED", "CANCELLED", "REJECTED"):
            return pending
        return None

    async def _entry_provenance(self, figi: str, scope, *, side, units, entry_price, lot) -> tuple:
        """Confirmed entry time of this position cycle, read from the durable entry record.

        The broker position itself only carries snapshot time, so a close may use this time
        only when the entry side persisted an explicit provenance for it AND the record proves
        it belongs to this cycle (side, full quantity, entry price, lot, request, stages).
        """
        mode, account = scope if isinstance(scope, (tuple, list)) and len(scope) == 2 else (None, None)
        if mode not in ("live", "sandbox") or not account:
            return None, "unknown", None, None
        from app.models.sandbox_trade import SandboxTrade
        test_name = (getattr(self.config, "test_name", "") or None)
        try:
            async with SessionLocal() as db:
                rows = (await db.execute(
                    select(SandboxTrade)
                    .where(SandboxTrade.figi == figi, SandboxTrade.exit_time.is_(None),
                           SandboxTrade.mode == mode,
                           SandboxTrade.broker_account_id == account,
                           SandboxTrade.test_name == test_name)
                    .order_by(SandboxTrade.entry_time.desc())
                )).scalars().all()
        except Exception as error:
            self._log(f"ENTRY_PROVENANCE_ERR {figi[-6:]}: {type(error).__name__}: {str(error)[:120]}")
            return None, "unknown", None, None
        for row in rows:
            try:
                meta = json.loads(row.meta or "{}")
            except Exception:
                continue
            proven = self._entry_record_matches(row, meta, figi=figi, side=side, units=units,
                                                entry_price=entry_price, lot=lot)
            if proven is None:
                continue
            first_execution, source, request_id, original_units = proven
            if units == original_units:
                return first_execution, source, row.id, request_id
            # A residual after a partial close is provable only from durable linked allocations
            # of this exact entry record; otherwise the remaining quantity is not this cycle.
            covered = await self._entry_closed_units(
                row_id=row.id, request_id=request_id, original_units=original_units,
                mode=mode, account=account, figi=figi)
            if covered is not None and covered == original_units - units:
                return first_execution, source, row.id, request_id
            return None, "unknown", None, None
        return None, "unknown", None, None

    def _entry_record_matches(self, row, meta, *, figi, side, units, entry_price, lot):
        """Accept an entry record only when its whole identity matches this position cycle.

        A recent scoped row with a time source is not identity evidence: it can belong to an
        earlier cycle of the same instrument. Side, full original quantity, entry price, lot,
        the original entry request and the stage payload must all agree.
        """
        from decimal import Decimal
        if type(lot) is not int or lot <= 0 or type(units) is not int or units <= 0:
            return None
        original_units = row.qty
        if type(original_units) is not int or original_units <= 0:
            return None
        if units > original_units or original_units % lot or units % lot:
            return None
        if side not in ("LONG", "SHORT") or _as_position_side(row.side) != side:
            return None
        try:
            if Decimal(str(row.entry_price)).quantize(Decimal("0.000001")) != \
                    Decimal(str(entry_price)).quantize(Decimal("0.000001")):
                return None
        except Exception:
            return None
        if meta.get("lot_size") != lot:
            return None
        if meta.get("entry_time_source") == "sandbox_order_timestamp":
            from app.services.sandbox_order_evidence import accounting_totals
            evidence = meta.get("entry_fill_evidence") or {}
            request_id = meta.get("client_order_id")
            if (row.mode != "sandbox" or evidence.get("source") != "sandbox_order_aggregate"
                    or not isinstance(request_id, str) or not request_id.strip()
                    or evidence.get("client_order_id") != request_id or evidence.get("figi") != figi
                    or _as_position_side(evidence.get("side")) != side or evidence.get("lot_size") != lot
                    or type(row.id) is not int or row.id <= 0):
                return None
            try:
                recorded_units, recorded_price, order_at = accounting_totals(evidence)
                if (recorded_units != original_units or order_at != row.entry_time
                        or order_at.utcoffset() is None
                        or recorded_price.quantize(Decimal("0.000001")) != Decimal(str(row.entry_price)).quantize(Decimal("0.000001"))):
                    return None
            except Exception:
                return None
            return order_at, "sandbox_order_timestamp", request_id, original_units
        source = meta.get("entry_time_source")
        confirmed = meta.get("entry_time_confirmed")
        if source not in ("broker_history", "execution_ledger") or not confirmed:
            return None
        request_id = meta.get("client_order_id")
        evidence = meta.get("entry_fill_evidence")
        if not isinstance(request_id, str) or not request_id.strip():
            return None
        if not isinstance(evidence, dict) or evidence.get("version") != 1:
            return None
        if (evidence.get("client_order_id") != request_id or evidence.get("figi") != figi
                or _as_position_side(evidence.get("side")) != side
                or evidence.get("lot_size") != lot):
            return None
        executed_lots = evidence.get("executed_lots")
        requested_lots = evidence.get("requested_lots")
        if type(executed_lots) is not int or type(requested_lots) is not int or executed_lots <= 0:
            return None
        if executed_lots * lot != original_units or requested_lots < executed_lots:
            return None
        fills = evidence.get("fills")
        if not isinstance(fills, list) or not fills:
            return None
        traded_units = 0
        times = []
        seen_ids = set()
        for fill in fills:
            if not isinstance(fill, dict):
                return None
            trade_id = fill.get("trade_id")
            fill_units = fill.get("units")
            if not isinstance(trade_id, str) or not trade_id.strip() or trade_id in seen_ids:
                return None
            seen_ids.add(trade_id)
            if type(fill_units) is not int or fill_units <= 0 or fill_units % lot:
                return None
            try:
                if Decimal(str(fill.get("price"))).quantize(Decimal("0.000001")) <= 0:
                    return None
                executed_at = datetime.fromisoformat(fill.get("executed_at"))
            except Exception:
                return None
            if executed_at.utcoffset() is None:
                return None
            traded_units += fill_units
            times.append(executed_at)
        if traded_units != original_units:
            return None
        try:
            first_execution = min(times)
            if datetime.fromisoformat(confirmed) != first_execution:
                return None
        except Exception:
            return None
        if type(row.id) is not int or row.id <= 0:
            return None
        return first_execution, source, request_id.strip(), original_units

    async def _entry_closed_units(self, *, row_id, request_id, original_units, mode, account, figi):
        """Already closed units of one entry cycle, proven from durable linked close allocations.

        Returns the covered quantity, or None when the residual cannot be proven: a close of
        this instrument that is unlinked, linked to another entry, not fully accounted, or
        counted twice makes the remainder unattributable.
        """
        from app.models.broker_order_intent import BrokerOrderIntent
        try:
            async with SessionLocal() as db:
                rows = (await db.execute(
                    select(BrokerOrderIntent).where(
                        BrokerOrderIntent.mode == mode,
                        BrokerOrderIntent.broker_account_id == account,
                        BrokerOrderIntent.figi == figi)
                )).scalars().all()
        except Exception as error:
            self._log(f"ENTRY_ALLOCATION_ERR {figi[-6:]}: {type(error).__name__}: {str(error)[:120]}")
            return None
        covered = 0
        seen = set()
        for intent in rows:
            observed = intent.observed_units or 0
            if observed <= 0:
                continue
            if type(observed) is not int or observed <= 0:
                return None
            if intent.accounted_units != observed:
                return None
            snapshot = intent.position_snapshot or {}
            if not isinstance(snapshot, dict):
                return None
            if snapshot.get("local_position_id") != row_id:
                return None
            if snapshot.get("entry_client_order_id") != request_id:
                return None
            if intent.client_order_id in seen:
                return None
            seen.add(intent.client_order_id)
            covered += observed
            if covered > original_units:
                return None
        return covered

    async def _submit_close_reconciled(self, figi: str, ticker: str, qty: int, price: float,
                                       reason: str, *, paper_filled: bool = True, close_order=None, entry_proof=None) -> dict:
        """Submit one close and classify the result by broker evidence.

        Only a confirmed full fill reports ``closed``. An unknown or timed-out result, an
        identity or quantity mismatch and a partial fill keep the position, its protection
        and a pending order, so an unresolved submission is never retried blindly.
        """
        inflight = self._close_inflight()
        if figi in inflight:
            return {"status": "blocked_inflight", "trade": None, "order": None, "residual": int(qty)}
        existing = self._close_pending_block(figi)
        if existing is not None:
            if isinstance(self.broker, LiveBroker) and existing.action == 'open' and (existing.meta or {}).get('durable_open'):
                from app.services.open_close_request import CloseAfterOpen, request_close_after_open
                from app.services.open_order_intent import OpenIntent
                from app.services.broker_order_journal import BrokerOrderJournal

                broker = self.broker
                scope = (broker.mode, broker._account)
                try:
                    if tuple(existing.meta.get('broker_scope', ())) != scope or self.broker_mode != scope[0]:
                        raise ValueError('close_after_open_scope_mismatch')
                    rows = await BrokerOrderJournal(broker.sessions).unresolved(
                        mode=scope[0], broker_account_id=scope[1], action='open')
                    row = next((row for row in rows if row.client_order_id == existing.id and row.figi == figi), None)
                    if row is None:
                        raise ValueError('close_after_open_reservation_missing')
                    saved = await request_close_after_open(broker.sessions, OpenIntent.from_record(row),
                        CloseAfterOpen(reason, self._bot_now()))
                    if (self.broker is not broker or (broker.mode, broker._account) != scope
                            or self.broker_mode != scope[0]):
                        raise ValueError('close_after_open_scope_changed')
                    existing.meta['close_after_open'] = saved.to_payload()
                    self._entry_pause().set_blocker('close_after_open', True, *scope, existing.id)
                    if figi not in inflight:
                        inflight.add(figi)
                        try:
                            try:
                                await broker.request_cancel_order(existing.id, expected_scope=scope)
                            except Exception as error:
                                self.events.log('ORDER_RECONCILIATION_REQUIRED', figi=figi, action='close',
                                                reason='opening_cancel_outcome_unknown', error_type=type(error).__name__)
                            if (self.broker is not broker or (broker.mode, broker._account) != scope
                                    or self.broker_mode != scope[0]):
                                raise ValueError('close_after_open_scope_changed')
                            await self._reconcile_pending_orders(only_figi=figi)
                        finally:
                            inflight.discard(figi)
                except Exception as error:
                    self.events.log('ORDER_RECONCILIATION_REQUIRED', figi=figi, action='close',
                                    reason='close_after_open_not_confirmed', error_type=type(error).__name__)
            return {"status": "blocked_existing", "trade": None, "order": existing, "residual": int(qty)}
        inflight.add(figi)
        try:
            broker = self.broker
            broker_scope = (getattr(broker, "mode", None), getattr(broker, "_account", None))
            close_side = "SELL"
            quantity_meta = {"quantity_unit": "shares", "requested_units": int(qty)}
            if isinstance(broker, LiveBroker):
                try:
                    position = await broker.get_position(figi)
                    lot = await broker._get_lot(figi)
                except Exception as error:
                    self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                    action="close", reason="close_preflight_unavailable",
                                    error_type=type(error).__name__)
                    return {"status": "blocked_preflight", "trade": None, "order": None, "residual": int(qty)}
                if (self.broker is not broker
                        or (getattr(broker, "mode", None), getattr(broker, "_account", None)) != broker_scope
                        or type(qty) is not int or qty <= 0
                        or position is None or getattr(position, "qty", None) != qty
                        or getattr(position, "side", None) not in ("LONG", "SHORT")
                        or type(lot) is not int or lot <= 0 or qty % lot):
                    self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                    action="close", reason="close_preflight_inconsistent")
                    return {"status": "blocked_preflight", "trade": None, "order": None, "residual": int(qty)}
                close_side = "SELL" if position.side == "LONG" else "BUY"
                quantity_meta.update(lot_size=lot, requested_lots=qty // lot)
            order = close_order or BotOrder(
                created_at=self._bot_now(),
                id=_new_order_id(), figi=figi, ticker=ticker, action="close",
                side=close_side, qty=int(qty), status="PENDING_RECONCILIATION",
                meta={**quantity_meta, "exit_reason": reason, "broker_scope": broker_scope},
            )
            order.side = close_side
            order.status = "PENDING_RECONCILIATION"
            order.meta = {**(order.meta or {}), **quantity_meta,
                          "exit_reason": reason, "broker_scope": broker_scope}
            if not hasattr(self, "pending_orders") or self.pending_orders is None:
                self.pending_orders = {}
            self.pending_orders[figi] = order
            if isinstance(self.broker, LiveBroker):
                kwargs = {"client_order_id": order.id, "expected_units": int(qty)}
                # ARCH374/375: only proven entry provenance may accompany a close; the broker
                # position itself carries snapshot time, so anything unproven stays unknown.
                if entry_proof is not None:
                    from app.services.open_close_request import CloseAfterOpenHandoff
                    if (not isinstance(entry_proof, CloseAfterOpenHandoff)
                            or (entry_proof.pending.intent.mode, entry_proof.pending.intent.broker_account_id) != broker_scope
                            or entry_proof.pending.intent.figi != figi or entry_proof.units != qty
                            or entry_proof.close_client_order_id != order.id
                            or position.side != ('LONG' if entry_proof.pending.intent.side == 'BUY' else 'SHORT')
                            or abs(float(position.entry_price) - float(entry_proof.entry_price)) > 0.000001):
                        raise ValueError('close_after_open_submission_proof_mismatch')
                    kwargs.update(entry_time=entry_proof.entry_time, entry_time_source=entry_proof.entry_time_source,
                                  local_position_id=entry_proof.local_position_id,
                                  entry_client_order_id=entry_proof.pending.intent.client_order_id)
                _entry_src = getattr(position, "entry_time_source", "unknown")
                _entry_ts = getattr(position, "entry_time_confirmed", None)
                _entry_row_id = None
                _entry_request = None
                if _entry_ts is None and entry_proof is None:
                    (_entry_ts, _entry_src,
                     _entry_row_id, _entry_request) = await self._entry_provenance(
                        figi, broker_scope, side=position.side, units=int(qty),
                        entry_price=position.entry_price, lot=lot)
                    # Re-check scope after the storage await before trusting what was read.
                    if (getattr(broker, "mode", None), getattr(broker, "_account", None)) != broker_scope:
                        _entry_ts, _entry_src = None, "unknown"
                        _entry_row_id, _entry_request = None, None
                if entry_proof is None and _entry_ts is not None and _entry_src in ("broker_history", "execution_ledger", "sandbox_order_timestamp"):
                    kwargs["entry_time"] = _entry_ts
                    kwargs["entry_time_source"] = _entry_src
                    if _entry_row_id is not None and _entry_request:
                        kwargs["local_position_id"] = _entry_row_id
                        kwargs["entry_client_order_id"] = _entry_request
            elif paper_filled and isinstance(self.broker, PaperBroker):
                kwargs = {"price_is_filled": True}
            else:
                kwargs = {}
            try:
                trade = await broker.close_position(figi, price, reason, **kwargs)
            except Exception as error:
                order.meta["error_type"] = type(error).__name__
                self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi, order_id=order.id,
                                action="close", reason="unknown_broker_result")
                return {"status": "pending_unknown", "trade": None, "order": order, "residual": int(qty)}
            if (self.broker is not broker
                    or (getattr(broker, "mode", None), getattr(broker, "_account", None)) != broker_scope
                    or self.pending_orders.get(figi) is not order):
                order.meta["error_type"] = "broker_scope_or_pending_changed"
                self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi,
                                order_id=order.id, action="close", reason="broker_scope_or_pending_changed")
                return {"status": "pending_unknown", "trade": None, "order": order, "residual": int(qty)}
            from app.bot.close_submission import CloseSubmission
            if isinstance(trade, CloseSubmission):
                if ((trade.mode, trade.broker_account_id) != broker_scope
                        or trade.client_order_id != order.id or trade.figi != figi
                        or trade.side != close_side or trade.requested_units != qty
                        or trade.lot_size != quantity_meta.get('lot_size')):
                    self._pause_close(broker_scope, order.id)
                    return {"status": "pending_unknown", "trade": None, "order": order, "residual": int(qty)}
                order.meta.update(broker_order_id=trade.broker_order_id,
                                  observed_executed_units=trade.observed_units)
                await self._reconcile_pending_orders(only_figi=figi)
                if (self.broker is not broker or (broker.mode, broker._account) != broker_scope):
                    return {"status": "pending_unknown", "trade": None, "order": order, "residual": int(qty)}
                if order.status == 'FILLED' and figi not in self.pending_orders:
                    return {"status": "reconciled_closed", "trade": None, "order": order, "residual": 0}
                if order.status in ('PARTIAL_FILLED', 'CANCELLED') and 0 < order.qty < qty:
                    return {"status": "pending_partial", "trade": None, "order": order, "residual": order.qty}
                return {"status": "pending_unknown", "trade": None, "order": order, "residual": int(qty)}
            from app.bot.close_confirmation import classify_close_confirmation
            confirmation = classify_close_confirmation(
                trade, requested_units=int(qty),
                client_order_id=order.id if isinstance(self.broker, LiveBroker) else None,
            )
            if confirmation.status == "PENDING_RECONCILIATION":
                order.meta["error_type"] = confirmation.reason
                if confirmation.executed_units is not None:
                    order.meta.update(observed_executed_units=confirmation.executed_units,
                                      observed_remaining_units=confirmation.remaining_units)
                self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi, order_id=order.id,
                                action="close", reason=confirmation.reason)
                return {"status": "pending_unknown", "trade": None, "order": order, "residual": int(qty)}
            order.meta["broker_order_id"] = getattr(trade, "broker_order_id", None)
            executed = confirmation.executed_units
            order.meta.update(executed_units=executed, remaining_units=int(qty) - executed,
                              trade_id=getattr(trade, "id", None))
            order.price = confirmation.exit_price
            if executed < int(qty):
                order.status = "PARTIAL_FILLED"
                order.qty = int(qty) - executed
                self.events.log("ORDER_RECONCILIATION_REQUIRED", figi=figi, order_id=order.id,
                                action="close", reason="partial_execution")
                return {"status": "pending_partial", "trade": trade, "order": order,
                        "residual": int(qty) - executed}
            order.status = "FILLED"
            if (getattr(self, "pending_orders", None) or {}).get(figi) is order:
                self.pending_orders.pop(figi, None)
            return {"status": "closed", "trade": trade, "order": order, "residual": 0}
        finally:
            inflight.discard(figi)

    async def _step_exit(self, figi: str, c, pos) -> bool:
        """Evaluate prior protection, then advance shared levels for the next bar."""
        pending = (getattr(self, "pending_orders", None) or {}).get(figi)
        if pending is not None and getattr(pending, "status", "") in ("PENDING_RECONCILIATION", "PARTIAL_FILLED"):
            self._log(f"RECONCILIATION: protective exit {figi[-6:]} waits for existing order")
            return False
        if not self._in_trading_session():
            self._session_gate_log('сигнальный выход')
            return False
        from app.engine.exits import intrabar_exit as _ibe
        from app.engine.protection import ProtectionState, evaluate_protection, advance_protection
        policy = self._exit_plans[figi]
        _raw_side = self._exit_side.get(figi) or getattr(pos, 'side', 'LONG')
        side_str = 'LONG' if str(_raw_side).upper() in ('LONG', 'BUY') else 'SHORT'
        state = PositionState.LONG if side_str == 'LONG' else PositionState.SHORT
        trail_side = Side.BUY if state == PositionState.LONG else Side.SELL
        entry_px = self._exit_entry_px.get(figi)
        if not entry_px or entry_px <= 0:
            entry_px = float(getattr(pos, 'entry_price', 0) or 0)
        qty_sh = int(self._exit_qty.get(figi) or getattr(pos, 'qty', 0) or 0)
        comm = float(entry_px) * qty_sh * self.config.commission_rate
        buf = self.buffers.get(figi)
        act_bars = list(buf) if buf else [c]
        try:
            _pk = self._peak_pnl.setdefault(figi, {'pnl': 0.0, 'ts': c.ts, 'price': float(entry_px), 'atr_abs': None, 'atr_pct': None, 'mae': 0.0})
            _sgn = 1.0 if state == PositionState.LONG else -1.0
            _best_px = float(c.high) if state == PositionState.LONG else float(c.low)
            _worst_px = float(c.low) if state == PositionState.LONG else float(c.high)
            _pnl_best = (_best_px - float(entry_px)) * qty_sh * _sgn
            _pnl_worst = (_worst_px - float(entry_px)) * qty_sh * _sgn
            if _pnl_best > float(_pk.get('pnl') or 0.0):
                _atr = self.atr_now(figi)
                _pk.update(pnl=_pnl_best, ts=c.ts, price=_best_px, atr_abs=float(_atr) if _atr else None, atr_pct=float(_atr) / float(c.close) * 100.0 if _atr and c.close else None)
            _mae_bar = max(0.0, -_pnl_worst)
            if _mae_bar > float(_pk.get('mae') or 0.0):
                _pk['mae'] = _mae_bar
        except Exception as _pk_e:
            _audit_swallow('_step_exit@peak_pnl', _pk_e)
            pass
        trail_active = bool(self._trail_active.get(figi, False))
        tp = None if trail_active or self._trail_active.get(figi, False) else self._exit_target.get(figi)
        stop = self._trail_stop.get(figi)
        try:
            from app.config import get_settings as _get_settings
            _s = _get_settings()
            _dbg = bool(getattr(_s, 'log_debug_engine', False))
        except Exception as _sw_e:
            _audit_swallow('_step_exit@L4928', _sw_e)
            _dbg = False
        if _dbg:
            self._log(f"DBG-EXIT {figi[-6:]} {state.value} entry={entry_px:.2f} qty={qty_sh} bar_ts={c.ts.strftime('%H:%M:%S')} o={c.open:.2f} h={c.high:.2f} l={c.low:.2f} c={c.close:.2f} stop={(stop if stop is not None else '-')} tp={(tp if tp is not None else '-')} trail={trail_active} pnl_rub={((c.close - entry_px) * qty_sh if state == PositionState.LONG else (entry_px - c.close) * qty_sh):+.2f}")
        try:
            _peak_px = c.high if state == PositionState.LONG else c.low
            _cur_pnl = (_peak_px - entry_px) * qty_sh if state == PositionState.LONG else (entry_px - _peak_px) * qty_sh
            _atr_v = None
            try:
                _atr_v = self.atr_now(figi)
            except Exception:
                _atr_v = None
            _mae_cur = 0.0
            _adv_px = 0.0
            try:
                if entry_px:
                    _adv_px = float(entry_px) - float(c.low) if state == PositionState.LONG else float(c.high) - float(entry_px)
                    if _adv_px > 0 and _atr_v:
                        _mae_cur = _adv_px / float(_atr_v)
            except Exception:
                _mae_cur = 0.0
                _adv_px = 0.0
            _peak = self._peak_pnl.get(figi)
            if _peak is None:
                _peak = {'pnl': float('-inf'), 'mae_cur': 0.0, 'mae_atr': 0.0}
            _peak['mae_cur'] = max(float(_peak.get('mae_cur', 0.0)), _mae_cur)
            try:
                _mae_pct_cur = float(_adv_px) / float(entry_px) * 100.0 if entry_px and _adv_px > 0 else 0.0
            except Exception:
                _mae_pct_cur = 0.0
            _peak['mae_cur_pct'] = max(float(_peak.get('mae_cur_pct', 0.0)), _mae_pct_cur)
            if _adv_px > float(_peak.get('mae_dist', 0.0)):
                _peak['mae_dist'] = float(_adv_px)
                _peak['mae_px'] = float(c.low) if state == PositionState.LONG else float(c.high)
                _peak['mae_ts'] = c.ts.isoformat()
            if _cur_pnl > float(_peak.get('pnl', -1e+18)):
                _atr_p = 0.0
                try:
                    _atr_p = float(_atr_v / entry_px * 100) if _atr_v and entry_px else 0.0
                except Exception:
                    pass
                _peak.update({'pnl': round(_cur_pnl, 2), 'ts': c.ts.isoformat(), 'price': float(_peak_px), 'atr_pct': round(_atr_p, 2), 'atr_abs': round(float(_atr_v), 4) if _atr_v else None, 'mae_atr': round(float(_peak.get('mae_cur', 0.0)), 2), 'mae_pct': round(float(_peak.get('mae_cur_pct', 0.0)), 2)})
            self._peak_pnl[figi] = _peak
        except Exception as _sw_e:
            _audit_swallow('_step_exit@peak', _sw_e)
            pass
        try:
            if not self._trail_active.get(figi, False):
                _ti = self._trail_info.get(figi)
                if _ti is None:
                    _ti = {'active': False, 'act_track': self._trail_stop.get(figi)}
                    self._trail_info[figi] = _ti
                _ti_cfg = getattr(self.config, 'trail_info_activation_comm_mult', None) or getattr(self.config, 'trail_activation_comm_mult', None)
                if _ti_cfg is not None and _ti is not None:
                    _act_bars = list(buf) if buf else [c]
                    from dataclasses import replace as _dc_replace
                    if getattr(policy, 'trailing_activated', None) is None:
                        from app.engine.exits import AtrStopPolicy as _AtrInfoPolicy
                        _tpol = _AtrInfoPolicy(period=int(getattr(self.config, 'atr_period', 14) or 14), multiplier=float(getattr(self.config, 'initial_sl_atr', 4.0) or 4.0), trail_activation_comm_mult=_ti_cfg, trail_distance_r=float(getattr(self.config, 'trail_distance_atr', 2.5) or 2.5), trail_compress_r=float(getattr(self.config, 'trail_compress_r', 0.0) or 0.0), trail_min_factor=float(getattr(self.config, 'trail_min_factor', 0.3) or 0.3), trail_min_atr=float(getattr(self.config, 'trail_min_atr', 0.0) or 0.0), trail_vol_boost=float(getattr(self.config, 'trail_vol_boost', 0.0) or 0.0))
                    else:
                        _tpol = _dc_replace(policy, trail_activation_comm_mult=_ti_cfg)
                    _tact = getattr(_tpol, 'trailing_activated', None)
                    _tupd = getattr(_tpol, 'update_stop', None)
                    _born_this_bar = False
                    if not _ti.get('active'):
                        try:
                            _activated = False
                            if _tact is not None:
                                _activated = _tact(trail_side, float(entry_px), qty_sh, comm, _act_bars)
                            if _activated:
                                _ti['active'] = True
                                _ti['act_track'] = self._trail_stop.get(figi)
                                _born_this_bar = True
                                self._log(f'ИНФО-ТРЕЙЛ ВКЛ. {figi[-6:]} pnl>=комиссия×{_ti_cfg}')
                        except Exception as _tie:
                            self._log(f'инфо-трейл activate error {figi[-6:]}: {_tie}')
                    if _ti.get('active') and _tupd is not None:
                        try:
                            _prev = _ti.get('act_track')
                            if _prev is not None and (not _born_this_bar) and (not _ti.get('hit_ts')):
                                _ti_price, _ti_reason = _ibe(c, state, float(_prev), None, close_based=True)
                                if _ti_price is not None:
                                    _ti['hit_ts'] = c.ts.isoformat()
                                    _ti['hit_price'] = float(_ti_price)
                                    _ti['hit_reason'] = _ti_reason or 'TRAIL'
                                    self._log(f"ИНФО-ТРЕЙЛ СРАБОТАЛ БЫ {figi[-6:]} цена={_ti_price:.2f} ({_ti_reason}) стоп={_prev:.2f} текущий_стоп={(stop if stop is not None else '-')}")
                            _pk_i = None
                            try:
                                _pk_i = float((self._peak_pnl.get(figi) or {}).get('price')) or None
                            except Exception:
                                _pk_i = None
                            _new_stop = _tupd(trail_side, float(entry_px), _prev, _act_bars, qty=qty_sh, commission=comm, peak_price=_pk_i)
                            if _new_stop is not None:
                                _ti['act_track'] = float(_new_stop)
                                _tpol2_dist = None
                                try:
                                    _risk_now = float(_tpol._risk(float(entry_px), _act_bars))
                                    _atr_now_val = _risk_now / _tpol.multiplier if _tpol.multiplier else None
                                    if _atr_now_val:
                                        _ti['trail_dist_atr'] = round(abs(float(c.close) - float(_new_stop)) / float(_atr_now_val), 2)
                                except Exception:
                                    pass
                                if not _born_this_bar:
                                    _ti_price, _ti_reason = _ibe(c, state, float(_new_stop), None, close_based=True)
                                    if _ti_price is not None and (not _ti.get('hit_ts')):
                                        _ti['hit_ts'] = c.ts.isoformat()
                                        _ti['hit_price'] = float(_ti_price)
                                        _ti['hit_reason'] = _ti_reason or 'TRAIL'
                                        self._log(f"ИНФО-ТРЕЙЛ СРАБОТАЛ БЫ {figi[-6:]} цена={_ti_price:.2f} ({_ti_reason}) стоп={_new_stop:.2f} текущий_стоп={(stop if stop is not None else '-')}")
                        except Exception as _twe:
                            self._log(f'инфо-трейл update error {figi[-6:]}: {_twe}')
        except Exception as _tge:
            self._log(f'инфо-трейл общий error {figi[-6:]}: {_tge}')
        params = getattr(self.config, 'params', None) or {}
        close_based = params.get('trailing_close_based', False)
        if type(close_based) is not bool:
            raise ValueError('trailing_close_based must be a boolean')
        prior = ProtectionState(stop, tp, trail_active)
        decision = evaluate_protection(c, prior, side=trail_side, entry_price=float(entry_px), entry_risk=None, bars_held=0, trailing_close_based=close_based)
        price, reason = (decision.exit_price, decision.exit_reason)
        if price is None:
            price, reason = self._giveback_exit(figi, c, state, entry_px, qty_sh)
        if price is None:
            next_protection = advance_protection(policy, prior, side=trail_side, entry_price=float(entry_px), qty=qty_sh, entry_commission=comm, closed_bars=act_bars, peak_price=(self._peak_pnl.get(figi) or {}).get('price'))
            next_state = next_protection.state
            if next_state.stop != stop or next_state.trailing_active != trail_active:
                await self._st_update_sl(figi, next_state.stop, trail_active=next_state.trailing_active)
            self._trail_stop[figi] = next_state.stop
            self._trail_active[figi] = next_state.trailing_active
            if next_state.target is None:
                self._exit_target.pop(figi, None)
            else:
                self._exit_target[figi] = next_state.target
            if next_state.stop != stop:
                self.events.log('TRAILING_STOP', figi=figi, ticker=pos.ticker, stop=next_state.stop, prev_stop=stop)
            if next_protection.trailing_activated:
                self.events.log('TRAILING_ACTIVATED', figi=figi, ticker=pos.ticker, reason='shared protection policy')
            return False
        _exit_best = False
        _was_trail = bool(trail_active or self._trail_active.get(figi, False))
        if isinstance(self.broker, PaperBroker):
            try:
                _cm = CostModel(commission_rate=self.config.commission_rate, slippage_bps=self.config.slippage_bps)
                _opp = Side.SELL if state == PositionState.LONG else Side.BUY
                price = _cm.fill_price(float(price), _opp)
            except Exception as _sw_e:
                _audit_swallow('_step_exit@L4946', _sw_e)
                pass
        outcome = await self._submit_close_reconciled(figi, pos.ticker, qty_sh, float(price), reason)
        if outcome["status"] == "reconciled_closed":
            await self._check_circuit_breaker()
            return True
        if outcome["status"] != "closed":
            if outcome["status"] == "pending_partial":
                self._exit_qty[figi] = int(outcome["residual"])
                self._log(f"ЧАСТИЧНОЕ ЗАКРЫТИЕ {figi[-6:]}: остаток {outcome['residual']}, "
                          "защита сохранена", level="warn")
            elif outcome["status"] in ("blocked_existing", "blocked_inflight"):
                self._log(f"RECONCILIATION: protective exit {figi[-6:]} waits for existing order")
            return False
        trade = outcome["trade"]
        price = float(trade.exit_price)
        self._held.discard(figi)
        _peak_snap = dict(self._peak_pnl.pop(figi, None) or {})
        self._clear_exit_state(figi)
        _bh = self._bar_counter - self._entry_bar_index.pop(figi, self._bar_counter)
        await self._st_close(figi, price, execution=trade, reason=reason, net=float(trade.net_pnl) if trade else None, meta={'exit_reason': reason, 'exit_price': float(price), 'exit_best': _exit_best, 'sl': stop, 'tp': tp, 'trailing': _was_trail, 'bars_held': _bh, 'peak': _peak_snap})
        pnl = float(trade.net_pnl) if trade else 0
        tag = 'ВЫХОД-ТРЕЙЛИНГ' if trail_active else 'ВЫХОД'
        self._log(f'{tag} {figi[-6:]} ({reason}) pnl={pnl:+.2f}')
        self._opposite_count.pop(figi, None)
        self._last_exit_bar[figi] = self._bar_counter
        self.events.log('POSITION_CLOSED', figi=figi, ticker=pos.ticker, reason=reason, net_pnl=float(trade.net_pnl) if trade else None, trailing=bool(trail_active))
        await self._check_circuit_breaker()
        return True

    async def _check_circuit_breaker(self) -> None:
        from math import isfinite
        try:
            limit = self.config.daily_loss_limit
            if isinstance(limit, bool) or not isfinite(float(limit)) or float(limit) < 0:
                raise ValueError('invalid_daily_loss_limit')
            risk = RiskSnapshot(daily_pnl=await self.refresh_daily_pnl(),
                                daily_loss_limit=float(limit), entries_paused=self.entries_paused)
        except Exception:
            self._entry_pause().set_blocker('risk_evidence_unavailable', True)
            raise
        self._entry_pause().set_blocker('risk_evidence_unavailable', False)
        breached = risk.state == 'LOSS_LIMIT'
        previous = 'daily_loss_limit' in self._entry_pause().reasons
        self._entry_pause().set_blocker('daily_loss_limit', breached)
        if breached and not previous:
            self.events.log('CIRCUIT_BREAKER_TRIGGERED', reason='LOSS_LIMIT', daily_pnl=risk.daily_pnl)


runtime = PaperBotRuntime()


async def _replay_candles_coverage(db, figi: str, ts_from: str, ts_to: str, log) -> None:
    """Целостность свечей по БД на окно реплея (БЕЗ API): пусто/обрыв границ -> warn."""
    if not ts_from or not ts_to:
        return
    from sqlalchemy import text as _t
    try:
        row = (await db.execute(_t(
            "SELECT count(*), min(ts), max(ts) FROM candles "
            "WHERE figi = :f AND \"interval\" = 1 AND ts BETWEEN :a AND :b"),
            {"f": figi, "a": ts_from, "b": ts_to})).one()
    except Exception:
        return
    n = int(row[0] or 0)
    if n == 0:
        try:
            log(f"ИНТЕГРИТИ: в БД нет 1m-свечей за окно теста ({figi} {ts_from}..{ts_to})")
        except Exception:
            pass
