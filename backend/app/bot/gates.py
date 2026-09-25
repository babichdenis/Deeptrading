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
    GateSpec("AGAINST_BIAS", "Против bias", "signal", "signal", "bias",
             "engine", "Вход против дневного/часового bias ансамбля (ensemble_config.bias)"),
    GateSpec("REGIME_MODE", "Режим сетапов", "signal", "signal", "regime_setups_filter",
             "engine", "Режим рынка запрещает данный сетап (ensemble_config.regime_setups_filter)"),
    GateSpec("IMOEX_VETO", "IMOEX-вето (сигнал)", "signal", "signal", "",
             "engine", "Всплеск индекса против стороны входа на этапе сигнала"),
    GateSpec("STOCH_FILTER", "Стохастик-фильтр", "signal", "signal", "",
             "engine", "Stochastic-фильтр сетапа"),
    GateSpec("RSI_FILTER", "RSI-фильтр", "signal", "signal", "",
             "engine", "RSI-гейт: BUY не входит вне нейтральной зоны (по умолч. RSI 40–60)"),
    GateSpec("VOL_FLOW", "Поток объёма", "signal", "signal", "",
             "engine", "volume flow против входа"),
    GateSpec("SETUP_MISSING", "Сетап не сработал", "signal", "signal", "",
             "engine", "Нет подтверждения конкретного сетапа"),

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
    GateSpec("news_blackout", "Негативная новость", "signal", "signal",
             "entry_news_blackout", "engine",
             "Свежая (≤ entry_news_blackout_min) негативная новость по тикеру "
             "(санкции/дестабилизация/допэмиссия/авария/иск…) — вход блокируется"),
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
    GateSpec("trend_alignment", "Против тренда", "trend", "trend",
             "trend_alignment", "engine", "В TREND_UP только BUY, в TREND_DOWN только SELL"),
    GateSpec("loss_streak_hold", "HOLD после убытков", "risk", "time",
             "loss_streak_hold", "engine", "N убытков подряд → пауза по тикеру/глобально"),
    GateSpec("imoex_guard", "IMOEX guard", "risk", "time",
             "imoex_guard", "engine", "Запрет входов против всплеска индекса MOEX"),

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
    GateSpec("hm_veto", "Veto накопл. движения", "trend", "trend",
             "entry_hm_veto", "engine",
             "Вход против накопленного движения за окно (часовые бары из 1м): "
             "BUY при падении ≤ -thr%, SELL при росте ≥ +thr%"),
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
    GateSpec("beta_filter", "Бета-фильтр", "portfolio", "portfolio",
             "beta_filter_enabled", "engine",
             "Лимит позиций в бета-группе: low≤4 / mid≤3 / high≤2 (beta = корр с IMOEX)"),
    GateSpec("confirmed_cluster", "Подтверждённые кластеры", "portfolio", "portfolio",
             "confirmed_cluster_enabled", "engine",
             "Лимит позиций в подтверждённых кластерах (steel/metals/oil/index/sber)"),
    GateSpec("ls_balance", "Баланс L/S", "portfolio", "portfolio",
             "max_short_share", "engine", "Доля шортов среди позиций ≤ max_short_share"),
    GateSpec("budget", "Сайзинг: бюджет", "sizing", "sizing",
             "pos_pct", "engine", "Цена/лот/бюджет не позволяют взять даже лот"),
    GateSpec("policy_reject", "Политика сигналов", "signal", "signal", "",
             "engine", "SignalPolicy.decide() отклонил сигнал"),

    # ---------- 4. APPROVAL: AI ----------
    GateSpec("ai_reject_cooldown", "Пауза после AI-reject", "ai", "approval",
             "ai_reject_cooldown_min", "engine", "Повторные входы по тикеру после отказа AI"),
    GateSpec("ai_already_pending", "Заявка уже в гейте", "ai", "approval", "",
             "engine", "Не дублируем заявку, пока предыдущая ждёт решения"),
    GateSpec("ai_chase", "AI: чейзинг", "ai", "approval",
             "ai_chase_pct", "ai", "Запрет AI-входа после хода >X% за день без отката"),
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


# =====================================================================================
# Переключаемые гейты (UI на «Складе»): key → как менять BotConfig, чтобы гейт
# РЕАЛЬНО вкл/выкл. kind:
#   bool       — флаг вкл/выкл напрямую;
#   inv_bool   — гейт активен при ВЫКЛЮЧЕННОМ флаге (long_allowed=False → «Лонги запрещены»);
#   threshold  — число: 0 = выкл, >0 = вкл (default_on — значение при включении);
#   list       — список: пустой = выкл, default_on = разрешённый набор;
#   enum       — строковое значение: on_value / off_value (напр. bias_mode veto/info);
#   dict       — словарь: {}=выкл, default_on или авто-карта при вкл.
# Поля "ensemble": True — хранятся в data/ensemble_config.json (не в BotConfig).
# Гейтов без записи здесь в UI нет — они структурные (всегда активны).
# =====================================================================================
_ALL_REGIMES = ("NEUTRAL", "TREND_UP", "TREND_DOWN", "HIGH_VOLATILITY", "RANGE")

GATE_TOGGLE: dict[str, dict] = {
    # ---------- signal ----------
    "orderbook":          {"field": "entry_ob_imbalance_max", "kind": "threshold", "default_on": 0.3},
    "liquidity_check":    {"field": "entry_min_turnover", "kind": "threshold", "default_on": 500_000.0},
    "volatility_check":   {"field": "entry_volatility_max_mult", "kind": "threshold", "default_on": 3.0},
    "news_blackout":      {"field": "entry_news_blackout", "kind": "bool"},
    # ---------- pre_order ----------
    "cooldown":           {"field": "reentry_cooldown_bars", "kind": "threshold", "default_on": 30},
    "long_disabled":      {"field": "long_allowed", "kind": "inv_bool"},
    "short_disabled":     {"field": "short_allowed", "kind": "inv_bool"},
    "trend_alignment":    {"field": "trend_alignment", "kind": "bool"},
    "loss_streak_hold":   {"field": "loss_streak_hold", "kind": "bool"},
    "imoex_guard":        {"field": "imoex_guard", "kind": "bool"},
    # ---------- order ----------
    "daily_bias":         {"field": "daily_bias", "kind": "bool"},
    "last_hour":          {"field": "entry_last_hour_block", "kind": "bool"},
    "h1_align":           {"field": "entry_h1_align", "kind": "bool"},
    "tf_conflict":        {"field": "entry_tf_conflict", "kind": "bool"},
    "mtf_h1_align":       {"field": "mtf_align", "kind": "bool"},
    "mtf_m5_trigger":     {"field": "mtf_trigger", "kind": "bool"},
    "hm_veto":            {"field": "entry_hm_veto", "kind": "bool"},
    "rank_filter":        {"field": "rank_enabled", "kind": "bool"},
    "max_exposure":       {"field": "max_exposure_pct", "kind": "threshold", "default_on": 1.0},
    "portfolio_limit":    {"field": "max_net_exposure_pct", "kind": "threshold", "default_on": 0.5},
    "max_positions":      {"field": "max_positions", "kind": "threshold", "default_on": 5},
    "sector_cluster":     {"field": "max_sector_positions", "kind": "threshold", "default_on": 2},
    "beta_filter":        {"field": "beta_filter_enabled", "kind": "bool"},
    "confirmed_cluster":  {"field": "confirmed_cluster_enabled", "kind": "bool"},
    "ls_balance":         {"field": "max_short_share", "kind": "threshold", "default_on": 0.7},
    # ---------- approval / AI ----------
    "ai_reject_cooldown": {"field": "ai_reject_cooldown_min", "kind": "threshold", "default_on": 15.0},
    "ai_chase":           {"field": "ai_chase_pct", "kind": "threshold", "default_on": 3.0},
    # ---------- ensemble (data/ensemble_config.json) ----------
    "AGAINST_BIAS":       {"field": "bias_mode", "kind": "enum", "ensemble": True,
                           "on_value": "veto", "off_value": "info"},
    "REGIME_MODE":        {"field": "regime_setups_filter", "kind": "dict", "ensemble": True},
}


def _all_regimes_map(ec: dict) -> dict:
    """regime_setups_filter включён БЕЗ ограничений: каждому включённому сетапу — все режимы."""
    regs = list(_ALL_REGIMES)
    return {s["strategy_id"]: regs
            for s in (ec.get("setups") or []) if s.get("enabled", True)}


def _field_val(owner, ec: dict | None, name: str):
    """Значение поля: из ensemble_config.json (приоритет) или BotConfig."""
    if name and ec is not None and name in ec:
        return ec[name]
    if name is None:
        return None
    try:
        return getattr(owner, name, None)
    except Exception:
        return None


def _val_enabled(v):
    """enabled-семантика значения: bool / dict / список / число>0."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, dict):
        return len(v) > 0
    if isinstance(v, (list, tuple, set)):
        return len(v) > 0
    try:
        return float(v) > 0
    except Exception:
        return bool(v)


def _gate_logical_enabled(spec: dict | None, owner, ec: dict | None):
    """РЕАЛЬНОЕ состояние гейта для UI (инверсия, enum, threshold, list, dict)."""
    if not spec:
        return True  # структурный — всегда на пути заявки
    if "fields" in spec:
        targets = spec["fields"]
    else:
        targets = [(spec["field"], None)]
    for fname, _ in targets:
        v = _field_val(owner, ec, fname)
        kind = spec["kind"]
        if kind == "inv_bool":
            return not bool(v)
        if kind == "enum":
            return v == spec.get("on_value")
        if isinstance(v, bool):
            return v
        if isinstance(v, dict):
            return len(v) > 0
        if isinstance(v, (list, tuple, set)):
            return len(v) > 0
        try:
            return float(v) > 0
        except Exception:
            return bool(v)
    return True


def toggle_gate(cfg, key: str, enabled: bool, ec: dict | None = None) -> dict:
    """Применить вкл/выкл гейта. cfg = BotConfig; ec = dict ensemble_config (ensemble-гейты)."""
    g = next((x for x in GATES if x.key == key), None)
    if g is None:
        return {"ok": False, "error": f"нет гейта с ключом {key!r}"}
    spec = GATE_TOGGLE.get(key)
    if not spec:
        return {"ok": False, "error": f"гейт {key!r} структурный — не переключается"}
    is_ens = bool(spec.get("ensemble"))
    if is_ens and ec is None:
        return {"ok": False, "error": f"гейт {key!r} — ensemble-гейт: нужен ensemble_config"}
    if "fields" in spec:
        targets: list[tuple] = list(spec["fields"])
    else:
        targets = [(spec["field"], spec.get("default_on"))]
    kind = spec["kind"]
    owner = ec if is_ens else cfg
    changed: list[dict] = []
    for fname, on_val in targets:
        old = owner.get(fname) if is_ens else getattr(cfg, fname, None)
        if kind == "inv_bool":
            v = not bool(enabled)
        elif kind == "bool":
            v = bool(enabled)
        elif kind == "enum":
            v = spec.get("on_value" if enabled else "off_value")
        elif kind == "threshold":
            v = on_val if enabled else 0
        elif kind == "list":
            v = list(on_val) if enabled else []
        elif kind == "dict":
            v = dict(on_val) if (enabled and on_val) else {}
            if not v and enabled:
                v = _all_regimes_map(ec)
        else:
            v = bool(enabled)
        if is_ens:
            owner[fname] = v
        else:
            setattr(cfg, fname, v)
        changed.append({"name": fname, "old": old, "new": v})
    return {"ok": True, "key": key, "title": g.title, "on": bool(enabled),
            "ensemble": is_ens, "fields": changed}


def gates_report(cfg, skip_counts: dict | None = None, ec: dict | None = None) -> dict:
    """Реестр гейтов + текущее состояние + статистика отказов (skip_counts)."""
    sk = {str(k): int(v or 0) for k, v in (skip_counts or {}).items()}
    items = []
    for g in GATES:
        spec = GATE_TOGGLE.get(g.key)
        # Для переключаемых гейтов показываем значение ИХ поля (spec), а не config спеки
        # (напр. AGAINST_BIAS: config="bias", а реальный рычаг — bias_mode).
        _cfg_name = (spec["field"] if (spec and "field" in spec) else g.config)
        fv = _field_val(cfg, ec, _cfg_name) if _cfg_name else None
        if not _cfg_name:
            _enabled = True
        elif fv is None:
            _enabled = None
        else:
            _enabled = _val_enabled(fv)
        items.append({
            "key": g.key, "title": g.title, "category": g.category, "stage": g.stage,
            "config": g.config, "scope": g.scope, "desc": g.desc,
            "enabled": _enabled,
            "logical_enabled": _gate_logical_enabled(spec, cfg, ec),
            "toggleable": bool(spec),
            "flag_value": fv,
            "rejects": sk.get(g.key, 0),
        })
    cats: dict[str, list] = {}
    stages: dict[str, dict] = {}
    for x in items:
        cats.setdefault(x["category"], []).append(x)
        st = stages.setdefault(x["stage"], {"gates": 0, "enabled": 0, "rejects": 0})
        st["gates"] += 1
        if x["logical_enabled"]:
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
    news_blackout_reason: str = ""  # непусто = свежая негативная новость по тикеру


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


def gate_news_blackout(ctx: MarketContext) -> GateResult:
    """Стоп по свежей негативной новости (санкции/дестабилизация/допэмиссия/…)."""
    if not bool(getattr(ctx.cfg, "entry_news_blackout", True)):
        return GateResult(True)
    if ctx.news_blackout_reason:
        return GateResult(False, "news_blackout", ctx.news_blackout_reason)
    return GateResult(True)


TIME_GATES = (gate_entries_paused, gate_session, gate_last_hour, gate_direction,
              gate_risk_day, gate_loss_streak, gate_already_held)
MARKET_GATES = (gate_liquidity, gate_volatility, gate_orderbook, gate_news_blackout)


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
    "entry_news_blackout",
    "entry_news_blackout_min",
    # trend
    "entry_h1_align",
    "entry_tf_conflict",
    "entry_hm_veto",
    "hm_veto_window_h",
    "hm_veto_thr_pct",
    "hm_veto_mode",
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
    "entry_news_blackout": True,
    "entry_news_blackout_min": 60,
    "entry_h1_align": True,
    "entry_tf_conflict": True,
    "entry_hm_veto": False,
    "hm_veto_window_h": 24,
    "hm_veto_thr_pct": 3.0,
    "hm_veto_mode": "veto",
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


@dataclass
class TrendContext:
    """Контекст слоя trend (STAGE B): daily/H1/M5 + якорь кворума + рейтинг."""
    cfg: object
    side: str
    daily_bias: str = ""
    daily_hist: float | None = None
    h1_ok: bool = False
    h1_side: str = ""
    h1_hist: float | None = None
    m5_ok: bool = False
    m5_trend: str = ""
    m5_hist: float | None = None
    require_member: str = ""
    members_for: tuple = ()
    votes: int = 0
    quorum: int = 2
    rank_why: str = ""
    hm_pct: float | None = None
    hm_dur: int = 0


def gate_daily_bias(ctx: TrendContext) -> GateResult:
    if not bool(getattr(ctx.cfg, "daily_bias", False)):
        return GateResult(True)
    _against = ((ctx.daily_bias == "up" and ctx.side == "SELL")
                or (ctx.daily_bias == "down" and ctx.side == "BUY"))
    if _against and str(getattr(ctx.cfg, "daily_bias_mode", "veto")).lower() == "veto":
        return GateResult(False, "daily_bias",
                          f"дневной MACD-bias {ctx.daily_bias} против {ctx.side} "
                          f"(hist {ctx.daily_hist:+})" if ctx.daily_hist is not None
                          else f"дневной MACD-bias {ctx.daily_bias} против {ctx.side}")
    return GateResult(True)


def gate_h1_align(ctx: TrendContext) -> GateResult:
    if (bool(getattr(ctx.cfg, "entry_h1_align", True)) and ctx.h1_ok
            and ctx.h1_side and ctx.h1_side != ctx.side):
        _h = f"{ctx.h1_hist:+.3f}" if ctx.h1_hist is not None else "—"
        return GateResult(False, "h1_align",
                          f"H1 MACD {ctx.h1_side} против {ctx.side} (hist {_h})")
    return GateResult(True)


def gate_tf_conflict(ctx: TrendContext) -> GateResult:
    if not bool(getattr(ctx.cfg, "entry_tf_conflict", True)):
        return GateResult(True)
    if ctx.h1_ok and ctx.h1_side and ctx.daily_bias in ("up", "down"):
        _bias_side = "BUY" if ctx.daily_bias == "up" else "SELL"
        if ctx.h1_side != _bias_side:
            return GateResult(False, "tf_conflict",
                              f"daily bias {ctx.daily_bias} против H1 {ctx.h1_side} "
                              f"(противоречие ТФ)")
    return GateResult(True)


def gate_hm_veto(ctx: TrendContext) -> GateResult:
    if not bool(getattr(ctx.cfg, "entry_hm_veto", False)):
        return GateResult(True)
    thr = float(getattr(ctx.cfg, "hm_veto_thr_pct", 3.0) or 3.0)
    p = ctx.hm_pct
    d = max(0, int(ctx.hm_dur or 0))
    if p is None:
        return GateResult(True)
    # Чем дольше тренд (часы подряд в одну сторону), тем ниже эффективный порог:
    # длинный тренд считается более «убедительным» — вход против него жёстче режем.
    _factor = 1.0 + min(d, 48) / 24.0
    thr_eff = thr / _factor
    _against = (ctx.side == "BUY" and p <= -thr_eff) or (ctx.side == "SELL" and p >= thr_eff)
    if not _against:
        return GateResult(True)
    mode = str(getattr(ctx.cfg, "hm_veto_mode", "veto")).lower()
    detail = (f"накоплен {p:+.2f}% за окно, тренд {d}ч против {ctx.side} "
              f"(порог ±{thr_eff:.2f}% на базе {thr}%)")
    if mode == "veto":
        return GateResult(False, "hm_veto", detail)
    return GateResult(True)


def gate_mtf_h1(ctx: TrendContext) -> GateResult:
    if (not bool(getattr(ctx.cfg, "mtf_align", False)) or not ctx.h1_ok
            or not ctx.h1_side):
        return GateResult(True)
    if ctx.h1_side != ctx.side:
        _h = f"{ctx.h1_hist:+.3f}" if ctx.h1_hist is not None else "—"
        return GateResult(False, "mtf_h1_align",
                          f"H1 MACD {ctx.h1_side} против {ctx.side} (hist {_h})")
    if ctx.daily_bias in ("up", "down"):
        _bias_side = "BUY" if ctx.daily_bias == "up" else "SELL"
        if ctx.h1_side != _bias_side:
            return GateResult(False, "mtf_h1_align",
                              f"H1 MACD {ctx.h1_side} против дневного bias {ctx.daily_bias}")
    return GateResult(True)


def gate_mtf_m5(ctx: TrendContext) -> GateResult:
    if not bool(getattr(ctx.cfg, "mtf_trigger", False)) or not ctx.m5_ok:
        return GateResult(True)
    _ok = ((ctx.side == "BUY" and ctx.m5_trend == "rising")
           or (ctx.side == "SELL" and ctx.m5_trend == "falling"))
    if not _ok:
        _h = f"{ctx.m5_hist:+.3f}" if ctx.m5_hist is not None else "—"
        return GateResult(False, "mtf_m5_trigger",
                          f"M5 MACD триггер не в сторону {ctx.side} "
                          f"(trend {ctx.m5_trend}, hist {_h})")
    return GateResult(True)


def gate_require_member(ctx: TrendContext) -> GateResult:
    if not ctx.require_member:
        return GateResult(True)
    _mem = [str(x) for x in (ctx.members_for or ())]
    if ctx.require_member not in _mem or int(ctx.votes) < int(ctx.quorum):
        return GateResult(False, "require_member",
                          f"якорь {ctx.require_member} не в кворуме "
                          f"(голоса {ctx.votes}/{ctx.quorum}, members: {','.join(_mem) or '—'})")
    return GateResult(True)


def gate_rank(ctx: TrendContext) -> GateResult:
    if ctx.rank_why:
        return GateResult(False, "rank_filter", f"рейтинг — {ctx.rank_why}")
    return GateResult(True)


TREND_GATES = (gate_daily_bias, gate_h1_align, gate_tf_conflict, gate_mtf_h1, gate_mtf_m5,
               gate_require_member, gate_rank, gate_hm_veto)


@dataclass
class PortfolioContext:
    """Контекст слоя portfolio (STAGE C): позиции/сектор/баланс L-S.

    held_tickers — тикеры уже открытых позиций (для beta/кластерных гейтов).
    """
    cfg: object
    side: str
    held_count: int = 0
    sector: str = ""
    sector_count: int = 0
    short_count: int = 0
    ticker: str = ""                 # текущий кандидат на вход
    held_tickers: tuple[str, ...] = ()  # тикеры открытых позиций


# --- beta-фильтр и подтверждённые кластеры ---------------------------------
# Beta = корреляция дневных доходностей с IMOEX (замер macro_corr, 2026-05..09).
# Пороги: low <0.55 (диверсификаторы), mid 0.55–0.75, high >0.75 (привязаны к рынку).
TICKER_BETA: dict[str, float] = {
    "ASTR": 0.434, "PLZL": 0.461, "SNGSP": 0.494, "LENT": 0.496, "SFIN": 0.514,
    "TRNFP": 0.539, "MVID": 0.542,
    "SMLT": 0.550, "OZON": 0.569, "MTSS": 0.574, "GMKN": 0.612, "VTBR": 0.628,
    "RNFT": 0.630, "ALRS": 0.647, "AFKS": 0.651, "NLMK": 0.668, "MAGN": 0.674,
    "PHOR": 0.679, "AFLT": 0.679, "SIBN": 0.690, "VKCO": 0.694, "RUAL": 0.697,
    "ROSN": 0.728, "CHMF": 0.731,
    "TATN": 0.784, "SBER": 0.791, "LKOH": 0.800, "YDEX": 0.817, "T": 0.829,
    "NVTK": 0.839, "GAZP": 0.850,
}
DEFAULT_BETA = 0.70  # для тикеров вне словаря — считаем средней (mid)

K_BETA_LOW = 0.55   # beta < порога → low-beta
K_BETA_HIGH = 0.75  # beta > порога → high-beta

# Ограничения на число позиций в группе (по beta кандидата)
BETA_LIMITS = {
    "low": 4,   # диверсификаторы можно держать до 4
    "mid": 3,
    "high": 2,  # высокобета — максимум 2
}


def _beta_group(beta: float) -> str:
    if beta < K_BETA_LOW:
        return "low"
    if beta > K_BETA_HIGH:
        return "high"
    return "mid"


# Подтверждённые кластеры: только пары с реально высокой корреляцией (не сектора).
# Эмпирика macro_corr (2026-05..09).
CONFIRMED_CLUSTERS: dict[str, dict] = {
    "steel":  {"tickers": ("CHMF", "MAGN", "NLMK"), "avg_r": 0.90, "max_positions": 1},
    "metals": {"tickers": ("GMKN", "RUAL"),         "avg_r": 0.81, "max_positions": 1},
    "oil":    {"tickers": ("LKOH", "ROSN", "TATN", "SIBN"),
               "avg_r": 0.72, "max_positions": 2},
    "index":  {"tickers": ("GAZP", "NVTK", "T", "AFKS"),
               "avg_r": 0.73, "max_positions": 2},
    "sber":   {"tickers": ("SBER", "TRNFP", "VTBR"), "avg_r": 0.82, "max_positions": 2},
}


def gate_beta_filter(ctx: PortfolioContext) -> GateResult:
    """Лимит числа позиций по бета-группе (для текущего тикера)."""
    if not getattr(ctx.cfg, "beta_filter_enabled", False):
        return GateResult(True)
    beta = TICKER_BETA.get(ctx.ticker.upper(), DEFAULT_BETA)
    group = _beta_group(beta)
    limit = BETA_LIMITS[group]
    cnt = sum(1 for t in ctx.held_tickers if _beta_group(
        TICKER_BETA.get(t.upper(), DEFAULT_BETA)) == group)
    if cnt >= limit:
        return GateResult(False, "beta_filter",
                          f"бета-группа '{group}' уже {cnt}/{limit} (beta {beta:.2f} для {ctx.ticker})")
    return GateResult(True)


def gate_confirmed_cluster(ctx: PortfolioContext) -> GateResult:
    """Лимит числа позиций внутри подтверждённого кластера (steel/metals/oil/index/sber)."""
    if not getattr(ctx.cfg, "confirmed_cluster_enabled", False):
        return GateResult(True)
    t_up = ctx.ticker.upper()
    cluster = next((name for name, cfg in CONFIRMED_CLUSTERS.items()
                    if t_up in cfg["tickers"]), None)
    if not cluster:
        return GateResult(True)
    cfg = CONFIRMED_CLUSTERS[cluster]
    cnt = sum(1 for t in ctx.held_tickers if t.upper() in cfg["tickers"])
    if cnt >= cfg["max_positions"]:
        return GateResult(False, "confirmed_cluster",
                          f"кластер '{cluster}' уже {cnt}/{cfg['max_positions']} "
                          f"(avg_r={cfg['avg_r']:.2f})")
    return GateResult(True)


def gate_max_positions(ctx: PortfolioContext) -> GateResult:
    _mp = int(getattr(ctx.cfg, "max_positions", 0) or 0)
    if _mp > 0 and ctx.held_count >= _mp:
        return GateResult(False, "max_positions",
                          f"лимит позиций {_mp} (сейчас {ctx.held_count})")
    return GateResult(True)


def gate_sector_cluster(ctx: PortfolioContext) -> GateResult:
    _ms = int(getattr(ctx.cfg, "max_sector_positions", 0) or 0)
    if _ms > 0 and ctx.sector and ctx.sector != "other" and ctx.sector_count >= _ms:
        return GateResult(False, "sector_cluster",
                          f"кластер «{ctx.sector}» — уже {ctx.sector_count} позиций (лимит {_ms})")
    return GateResult(True)


def gate_ls_balance(ctx: PortfolioContext) -> GateResult:
    _share = float(getattr(ctx.cfg, "max_short_share", 0.0) or 0.0)
    _min_total = int(getattr(ctx.cfg, "balance_min_positions", 3) or 3)
    if ctx.side != "SELL" or _share <= 0 or ctx.held_count < _min_total:
        return GateResult(True)
    if (ctx.short_count + 1) / (ctx.held_count + 1) > _share:
        return GateResult(False, "ls_balance",
                          f"дисбаланс L/S — шортов {ctx.short_count} из {ctx.held_count} "
                          f"(лимит {_share*100:.0f}%)")
    return GateResult(True)


PORTFOLIO_GATES = (gate_max_positions, gate_sector_cluster, gate_ls_balance,
                   gate_beta_filter, gate_confirmed_cluster)
