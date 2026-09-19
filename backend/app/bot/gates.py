"""Единый реестр гейтов входа (аудит + структура).

Все проверки на пути заявки раскинуты по трём слоям:
  1) signal   — внутри compute_ensemble()/стратегии (кворум, сетапы, объём, режим);
  2) pre_order— в runtime._process_candle() (сессии, пауза, L/S, тренд, риск дня);
  3) order    — в runtime._submit_order() (TF-гейты, портфель, позиции, сайзинг, AI-гейт);
  4) approval — AI-гейт (модель) и жёсткие гейты AI-ордеров (/ai_trade).

Этот модуль — единственный источник правды по составу гейтов: key = reason-код
из логов/skip_counts. Используется эндпоинтом GET /api/v1/bot/gates.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GateSpec:
    key: str            # reason-код (логи, skip_counts, events)
    title: str          # человеческое имя
    category: str       # signal | session | trend | risk | portfolio | sizing | ai | data
    stage: str          # signal | pre_order | order | approval | sizing
    config: str = ""    # флаг BotConfig ("" = задаётся конфигом кворума/всегда включён)
    scope: str = "engine"   # engine | ai | both
    desc: str = ""


# Порядок в кортеже = порядок прохождения заявки (для читаемости аудита).
GATES: tuple[GateSpec, ...] = (
    # ---------- 1. SIGNAL: внутри compute_ensemble / стратегии ----------
    GateSpec("no_entries", "Кворум не набрал вход", "signal", "signal", "ensemble_quorum",
             "engine", "Сетапы ансамбля не дали вход (funnel_raw); quorum в ensemble_config"),
    GateSpec("no_fresh", "Нет свежего сигнала", "signal", "signal", "",
             "engine", "Входы есть, но старее FRESH_MIN минут — не торгуем"),
    GateSpec("vol_thr", "Объёмный фильтр", "signal", "signal", "vol_thr",
             "engine", "volume/mean50 < порога — вход/выход отклонён (ensemble_config.vol_thr)"),
    GateSpec("AGAINST_BIAS", "Против bias", "signal", "signal", "bias",
             "engine", "Вход против дневного/часового bias ансамбля (ensemble_config.bias)"),
    GateSpec("REGIME_MODE", "Режим сетапов", "signal", "signal", "regime_setups_filter",
             "engine", "Режим рынка запрещает данный сетап (ensemble_config.regime_setups_filter)"),
    GateSpec("IMOEX_VETO", "IMOEX-вето (сигнал)", "signal", "signal", "",
             "engine", "Всплеск индекса против стороны входа на этапе сигнала"),
    GateSpec("STOCH_FILTER", "Стохастик-фильтр", "signal", "signal", "",
             "engine", "Stochastic-фильтр сетапа"),
    GateSpec("VOL_FLOW", "Поток объёма", "signal", "signal", "",
             "engine", "volume flow против входа"),
    GateSpec("SETUP_MISSING", "Сетап не сработал", "signal", "signal", "",
             "engine", "Нет подтверждения конкретного сетапа"),
    GateSpec("entry_confirm", "Подтверждение 1м-закрытиями", "signal", "signal",
             "entry_confirm_closes", "engine", "N 1м-закрытий строго по направлению"),
    GateSpec("entry_macd_1m", "MACD 1м триггер", "signal", "signal", "",
             "engine", "1м MACD должен подтверждать сторону (entry_macd_1m)"),

    GateSpec("orderbook", "Стакан против входа", "signal", "signal",
             "entry_ob_imbalance_max", "engine",
             "Общий гейт (движок+AI): imbalance против стороны / широкий спред "
             "(entry_ob_imbalance_max, entry_ob_spread_max) — первый после ансамбля"),
    GateSpec("orderbook_error", "Стакан недоступен", "data", "signal", "",
             "engine", "Не удалось получить стакан — вход пропущен (не торгуем вслепую)"),
    GateSpec("liquidity_check", "Низкая ликвидность", "signal", "signal",
             "entry_min_turnover", "engine", "Дневной оборот тикера < entry_min_turnover ₽"),
    GateSpec("volatility_check", "Аномальная волатильность", "signal", "signal",
             "entry_volatility_max_mult", "engine",
             "ATR% тикера > X × медианы по универсу (защита от выбросов)"),
    # ---------- 2. PRE_ORDER: _process_candle ----------
    GateSpec("cooldown", "Пауза после выхода", "risk", "time",
             "reentry_cooldown_bars", "engine", "Баров между выходом и повторным входом"),
    GateSpec("already_held", "Позиция уже открыта", "portfolio", "time", "",
             "engine", "По тикеру уже есть позиция в учёте бота"),
    GateSpec("session_filter", "Вне торговых сессий", "session", "time",
             "sessions", "engine", "Вход только в разрешённых сессиях (morning/day/evening)"),
    GateSpec("entries_paused", "Входы на паузе", "risk", "time", "",
             "engine", "Ручная пауза входов (UI: «Пауза»)"),
    GateSpec("long_disabled", "Лонги запрещены", "session", "time",
             "long_allowed", "engine", "Направление long выключено в конфиге"),
    GateSpec("short_disabled", "Шорты запрещены", "session", "time",
             "short_allowed", "engine", "Направление short выключено в конфиге"),
    GateSpec("regime_off", "Режим рынка запрещён", "trend", "trend",
             "trade_regimes", "engine", "Режим (TREND_UP/…/RANGE) не в trade_regimes"),
    GateSpec("trend_alignment", "Против тренда", "trend", "trend",
             "trend_alignment", "engine", "В TREND_UP только BUY, в TREND_DOWN только SELL"),
    GateSpec("loss_streak_hold", "HOLD после убытков", "risk", "time",
             "loss_streak_hold", "engine", "N убытков подряд → пауза по тикеру/глобально"),
    GateSpec("imoex_guard", "IMOEX guard", "risk", "time",
             "imoex_guard", "engine", "Запрет входов против всплеска индекса MOEX"),
    GateSpec("risk_limit", "Лимит дня", "risk", "time",
             "daily_loss_limit", "engine", "risk.state != NORMAL (дневной лимит убытка/пауза)"),

    # ---------- 3. ORDER: _submit_order ----------
    GateSpec("daily_bias", "Дневной MACD-bias", "trend", "trend",
             "daily_bias", "engine", "Вход против дневного bias (режим veto; info = только лог)"),
    GateSpec("last_hour", "Последний час сессии", "session", "time",
             "entry_last_hour_block", "engine", "Последний час ПОСЛЕДНЕЙ сессии бота (вечер→день→утро)"),
    GateSpec("h1_align", "H1 подтверждение", "trend", "trend",
             "entry_h1_align", "engine", "H1 MACD должен совпадать со стороной входа"),
    GateSpec("tf_conflict", "Конфликт daily/H1", "trend", "trend",
             "entry_tf_conflict", "engine", "Дневной bias и H1 противоречат друг другу"),
    GateSpec("mtf_h1_align", "MTF H1 (legacy)", "trend", "trend",
             "mtf_align", "engine", "Старый комбинированный MTF-фильтр (H1 vs bias)"),
    GateSpec("mtf_m5_trigger", "M5 триггер (legacy)", "trend", "trend",
             "mtf_trigger", "engine", "M5 MACD должен разворачиваться в сторону входа"),
    GateSpec("require_member", "Якорь кворума", "signal", "trend",
             "ensemble_require_member", "engine", "Обязательный голос кворума (напр. macd_cross)"),
    GateSpec("rank_filter", "Рейтинг тикеров", "signal", "trend",
             "rank_enabled", "engine", "Тикер не в top-N по истории после разведки"),
    GateSpec("market_reversal", "Разворот рынка", "trend", "portfolio", "",
             "engine", "Режим reversal против книги — не добавляем в убыточную сторону"),
    GateSpec("max_exposure", "Кап экспозиции", "portfolio", "portfolio",
             "max_exposure_pct", "engine", "Свои деньги в позициях ≤ X equity"),
    GateSpec("portfolio_limit", "Портфельные лимиты", "portfolio", "portfolio",
             "max_net_exposure_pct", "engine",
             "net/sector/margin/stress лимиты (max_net_exposure_pct, max_sector_pct, "
             "max_margin_use_pct, max_stress_loss_pct)"),
    GateSpec("max_positions", "Лимит позиций", "portfolio", "portfolio",
             "max_positions", "engine", "Максимум одновременных позиций"),
    GateSpec("sector_cluster", "Кластер сектора", "portfolio", "portfolio",
             "max_sector_positions", "engine", "Максимум позиций в одном секторе"),
    GateSpec("ls_balance", "Баланс L/S", "portfolio", "portfolio",
             "max_short_share", "engine", "Доля шортов среди позиций ≤ max_short_share"),
    GateSpec("budget", "Сайзинг: бюджет", "sizing", "sizing",
             "pos_pct", "engine", "Цена/лот/бюджет не позволяют взять даже лот"),
    GateSpec("margin_limit", "Сайзинг: маржа", "sizing", "sizing",
             "max_margin_pct", "engine", "Лимит брокера/маржи: max lots = 0"),
    GateSpec("queue", "Очередь кандидатов", "signal", "portfolio",
             "queue_enabled", "engine", "Слабый вход отложен в очередь (top-1 входит с бустом)"),
    GateSpec("policy_reject", "Политика сигналов", "signal", "signal", "",
             "engine", "SignalPolicy.decide() отклонил сигнал"),

    # ---------- 4. APPROVAL: AI ----------
    GateSpec("ai_approval", "AI-гейт (approve/reject)", "ai", "approval",
             "ai_approval", "engine", "Заявка ждёт решения модели; таймаут → ai_approval_default"),
    GateSpec("ai_reject_cooldown", "Пауза после AI-reject", "ai", "approval",
             "ai_reject_cooldown_min", "engine", "Повторные входы по тикеру после отказа AI"),
    GateSpec("ai_already_pending", "Заявка уже в гейте", "ai", "approval", "",
             "engine", "Не дублируем заявку, пока предыдущая ждёт решения"),
    GateSpec("ai_chase", "AI: чейзинг", "ai", "approval",
             "ai_chase_pct", "ai", "Запрет AI-входа после хода >X% за день без отката"),
    GateSpec("ai_sl_tp", "AI: потолки SL/TP", "ai", "approval",
             "ai_sl_max_pct", "ai", "SL ≤ ai_sl_max_pct, TP ≤ ai_tp_max_pct для AI-ордеров"),
    GateSpec("ai_watch", "AI-вахтёр позиций", "ai", "approval",
             "ai_approval", "ai", "Вахтёр: close/tighten по позициям (Бот+++)"),

    # ---------- 5. DATA ----------
    GateSpec("bad_candle", "Битая свеча", "data", "signal", "",
             "engine", "OHLC/объём не прошли валидацию — свеча отброшена"),
    GateSpec("stale_candle", "Незакрытая/устаревшая свеча", "data", "signal", "",
             "engine", "Бар не закрыт или вне окна свежести"),
)


def _flag_enabled(cfg, name: str):
    """Состояние флага конфига: bool / число>0 / непустой список. None = нет флага."""
    if not name:
        return True
    v = getattr(cfg, name, None)
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (list, tuple, set)):
        return len(v) > 0
    try:
        return float(v) > 0
    except Exception:
        return bool(v)


def gates_report(cfg, skip_counts: dict | None = None) -> dict:
    """Реестр гейтов + текущее состояние + статистика отказов (skip_counts)."""
    sk = {str(k): int(v or 0) for k, v in (skip_counts or {}).items()}
    items = []
    for g in GATES:
        items.append({
            "key": g.key, "title": g.title, "category": g.category, "stage": g.stage,
            "config": g.config, "scope": g.scope, "desc": g.desc,
            "enabled": _flag_enabled(cfg, g.config),
            "flag_value": (getattr(cfg, g.config, None) if g.config else None),
            "rejects": sk.get(g.key, 0),
        })
    cats: dict[str, list] = {}
    stages: dict[str, dict] = {}
    for x in items:
        cats.setdefault(x["category"], []).append(x)
        st = stages.setdefault(x["stage"], {"gates": 0, "enabled": 0, "rejects": 0})
        st["gates"] += 1
        if x["enabled"]:
            st["enabled"] += 1
        st["rejects"] += x["rejects"]
    _order = ["signal", "time", "trend", "portfolio", "sizing", "approval"]
    return {"count": len(items), "categories": cats, "by_stage": stages,
            "skip_counts": sk, "order": _order}


# =====================================================================================
# Исполняемые гейты (вынесены из runtime; чистые функции — легко тестировать).
# Контексты собирает runtime, логирование и события — тоже runtime (_reject_entry).
# =====================================================================================

@dataclass
class GateResult:
    passed: bool
    key: str = ""
    detail: str = ""


@dataclass
class TimeContext:
    """Контекст слоя time (STAGE A): время/сессии/риск дня — без брокера."""
    cfg: object
    side: str
    entries_paused: bool = False
    sessions_allowed: bool = True
    is_last_hour: bool = False
    risk_allowed: bool = True
    risk_state: str = "NORMAL"
    daily_pnl: float = 0.0
    loss_hold: bool = False
    loss_why: str = ""
    already_held: bool = False


@dataclass
class MarketContext:
    """Контекст слоя signal/STAGE S: стакан/ликвидность/волатильность."""
    cfg: object
    side: str
    turnover: float = 0.0
    atr_pct: float | None = None
    atr_pct_median: float | None = None
    orderbook: dict | None = None
    orderbook_checked: bool = True  # False = стакан не запрашивали (лимиты выключены)


# --- time (STAGE A) ------------------------------------------------------------------

def gate_entries_paused(ctx: TimeContext) -> GateResult:
    if ctx.entries_paused:
        return GateResult(False, "entries_paused", "входы на паузе")
    return GateResult(True)


def gate_session(ctx: TimeContext) -> GateResult:
    if not ctx.sessions_allowed:
        _s = "/".join(getattr(ctx.cfg, "sessions", []) or []) or "—"
        return GateResult(False, "session_filter", f"вне торговых сессий (разрешены {_s})")
    return GateResult(True)


def gate_last_hour(ctx: TimeContext) -> GateResult:
    if bool(getattr(ctx.cfg, "entry_last_hour_block", True)) and ctx.is_last_hour:
        _ss = list(getattr(ctx.cfg, "sessions", []) or [])
        _last = "evening" if "evening" in _ss else "day" if "day" in _ss else "morning"
        return GateResult(False, "last_hour",
                          f"последний час последней сессии ({_last}, вход не успеет выйти)")
    return GateResult(True)


def gate_direction(ctx: TimeContext) -> GateResult:
    if ctx.side == "BUY" and not bool(getattr(ctx.cfg, "long_allowed", True)):
        return GateResult(False, "long_disabled", "Long запрещён (Направление)")
    if ctx.side == "SELL" and not bool(getattr(ctx.cfg, "short_allowed", True)):
        return GateResult(False, "short_disabled", "Short запрещён (Направление)")
    return GateResult(True)


def gate_risk_day(ctx: TimeContext) -> GateResult:
    if not ctx.risk_allowed:
        return GateResult(False, f"risk_{str(ctx.risk_state).lower()}",
                          f"риск дня {ctx.risk_state} (daily_pnl {ctx.daily_pnl:+.0f}₽)")
    return GateResult(True)


def gate_loss_streak(ctx: TimeContext) -> GateResult:
    if ctx.loss_hold:
        return GateResult(False, "loss_streak_hold", f"HOLD после убытков — {ctx.loss_why}")
    return GateResult(True)


def gate_already_held(ctx: TimeContext) -> GateResult:
    if ctx.already_held:
        return GateResult(False, "already_held", "поз. уже открыта")
    return GateResult(True)


# --- signal / STAGE S: стакан, ликвидность, волатильность ----------------------------

def gate_liquidity(ctx: MarketContext) -> GateResult:
    _min_to = float(getattr(ctx.cfg, "entry_min_turnover", 0.0) or 0.0)
    if _min_to > 0 and 0 < ctx.turnover < _min_to:
        return GateResult(False, "liquidity_check",
                          f"оборот {ctx.turnover:,.0f}₽ < {_min_to:,.0f}₽")
    return GateResult(True)


def gate_volatility(ctx: MarketContext) -> GateResult:
    _mult = float(getattr(ctx.cfg, "entry_volatility_max_mult", 3.0) or 0.0)
    if (_mult > 0 and ctx.atr_pct is not None and ctx.atr_pct_median is not None
            and ctx.atr_pct_median > 0 and ctx.atr_pct > ctx.atr_pct_median * _mult):
        return GateResult(False, "volatility_check",
                          f"ATR {ctx.atr_pct:.2f}% > {_mult:g}× медианы "
                          f"{ctx.atr_pct_median:.2f}%")
    return GateResult(True)


def gate_orderbook(ctx: MarketContext) -> GateResult:
    _imb_lim = float(getattr(ctx.cfg, "entry_ob_imbalance_max", 0.3) or 0.0)
    _spr_lim = float(getattr(ctx.cfg, "entry_ob_spread_max", 25.0) or 0.0)
    if _imb_lim <= 0 and _spr_lim <= 0:
        return GateResult(True)
    if ctx.orderbook is None:
        return GateResult(False, "orderbook_error", "стакан недоступен — не торгуем вслепую")
    why: list[str] = []
    _imb = ctx.orderbook.get("imbalance")
    if _imb_lim > 0 and _imb is not None:
        if ctx.side == "BUY" and float(_imb) < -_imb_lim:
            why.append(f"imbalance {_imb} против BUY")
        if ctx.side == "SELL" and float(_imb) > _imb_lim:
            why.append(f"imbalance {_imb} против SELL")
    _spr = ctx.orderbook.get("spread_bps")
    if _spr_lim > 0 and _spr is not None and float(_spr) > _spr_lim:
        why.append(f"спред {_spr} > {_spr_lim:g} б.п.")
    if why:
        return GateResult(False, "orderbook", "; ".join(why))
    return GateResult(True)


TIME_GATES = (gate_entries_paused, gate_session, gate_last_hour, gate_direction,
              gate_risk_day, gate_loss_streak, gate_already_held)
MARKET_GATES = (gate_liquidity, gate_volatility, gate_orderbook)


def run_gate_chain(gates, ctx) -> GateResult:
    """Прогнать цепочку гейтов до первого отказа (short-circuit)."""
    for g in gates:
        r = g(ctx)
        if not r.passed:
            return r
    return GateResult(True)


# =====================================================================================
# Конфиг гейтов (единый файл data/gates_config.json) — пороги не в коде, а в конфиге.
# =====================================================================================

import json as _json
from pathlib import Path as _Path

GATES_CONFIG_FILE = str(_Path(__file__).resolve().parents[2] / "data" / "gates_config.json")

# Поля BotConfig, которыми управляет gates_config.json (порядок = порядок в файле).
GATE_CONFIG_FIELDS: tuple[str, ...] = (
    # time
    "entry_last_hour_block",
    # signal / STAGE S
    "entry_ob_imbalance_max",
    "entry_ob_spread_max",
    "entry_min_turnover",
    "entry_volatility_max_mult",
    # trend
    "entry_h1_align",
    "entry_tf_conflict",
    # portfolio
    "max_sector_positions",
    # approval / AI
    "ai_chase_pct",
    "ai_sl_max_pct",
    "ai_tp_max_pct",
)

# Дефолты файла (используются, если файла нет; значения — как в BotConfig).
GATE_CONFIG_DEFAULTS: dict = {
    "entry_last_hour_block": True,
    "entry_ob_imbalance_max": 0.3,
    "entry_ob_spread_max": 25.0,
    "entry_min_turnover": 0.0,
    "entry_volatility_max_mult": 3.0,
    "entry_h1_align": True,
    "entry_tf_conflict": True,
    "max_sector_positions": 0,
    "ai_chase_pct": 3.0,
    "ai_sl_max_pct": 0.03,
    "ai_tp_max_pct": 0.08,
}


def load_gates_config() -> dict:
    """Прочитать data/gates_config.json (только gate-поля). Нет файла — дефолты."""
    try:
        p = _Path(GATES_CONFIG_FILE)
        if p.exists():
            data = _json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: data[k] for k in GATE_CONFIG_FIELDS if k in data}
    except Exception:
        pass
    return {}


def save_gates_config(cfg) -> dict:
    """Сохранить gate-поля конфига в data/gates_config.json (единственный источник)."""
    data = {}
    for k in GATE_CONFIG_FIELDS:
        v = getattr(cfg, k, None)
        if v is not None:
            data[k] = v
    try:
        p = _Path(GATES_CONFIG_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass
    return data


def apply_gates_config(cfg) -> list[str]:
    """Наложить gates_config.json на BotConfig (файл важнее сохранёнок/дефолтов)."""
    applied = []
    data = load_gates_config()
    for k, v in data.items():
        try:
            setattr(cfg, k, v)
            applied.append(k)
        except Exception:
            pass
    return applied
