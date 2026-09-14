from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Sequence

from zoneinfo import ZoneInfo

from app.engine.models import Candle, Signal, Side

MSK = ZoneInfo("Europe/Moscow")
FRESH_MIN = 30

# V2 params: RSI16/30/80, Boll15/1.0, Pull20/10, Range15/16/40, Donch45
V2_SETUPS = [
    {"strategy_id": "rsi_reversal", "tf": "5min", "params": {"period": 16, "oversold": 30, "overbought": 80}},
    {"strategy_id": "bollinger_reclaim", "tf": "5min", "params": {"period": 15, "k": 1.0}},
    {"strategy_id": "pullback_ema", "tf": "5min", "params": {"trend_ema": 20, "pull_ema": 10}},
    {"strategy_id": "vwap_reclaim", "tf": "5min", "params": {"k": 2.0}},
    {"strategy_id": "range_compression_breakout", "tf": "5min", "params": {"lookback": 15, "atr_period": 16, "pct": 40.0}},
    {"strategy_id": "macd_cross", "tf": "5min", "params": {"fast": 12, "slow": 26, "signal_period": 9}},
    {"strategy_id": "donchian_breakout", "tf": "5min", "params": {"period": 45}},
]


@dataclass
class EnsembleParams:
    figi: str
    lot: int = 10
    capital: float = 2000.0
    quorum: int = 2
    use_all_setups: bool = False
    drop_useless: bool = True
    session: str = "main"
    sessions: list = field(default_factory=lambda: ["day"])
    setups: list = field(default_factory=lambda: V2_SETUPS)
    # --- Таймфреймы ансамбля (как в semi-flip тестах) ---
    bias_tf: str = "hour"        # направление (bias): "5min" | "15min" | "hour"
    bias_period: int = 50        # период EMA для bias
    entry_tf: str = "5min"       # микро-вход: "1min" | "5min"
    entry_lookback: int = 1      # окно микро-брейкаута (бары entry_tf)
    # --- per-ticker optuna-параметры (расширение; дефолты == прежний хардкод) ---
    sl_mult: float = 2.0
    rr: float = 2.0
    vol_thr: float = 0.0
    neutral_mode: str | None = None
    entry_confirm_bars: int = 0  # ждать N подряд подтверждающих свечей в сторону входа; 0 = без подтверждения
    # --- EXP-008: минуточный MACD-фильтр (гистограмма 1m в сторону входа) ---
    entry_macd_1m: bool = False
    entry_from_setups: bool = False  # вход (и сторона) из сигналов стратегий, не micro_breakout
    entry_direction_sid: str = ""    # Путь 2: направление только от этой стратегии
    regime_tf: str = "hour"          # ТФ режим-детектора (H1 по умолчанию)
    entry_macd_fast: int = 12
    entry_macd_slow: int = 26
    entry_macd_signal: int = 9
    # --- Режимные фильтры: {strategy_id: [разрешённые режимы]} (пусто = все режимы) ---
    regime_setups_filter: dict = field(default_factory=dict)
    # --- Быстрый режим-гейт: не гонять тяжёлый ансамбль, если режим не разрешён ---
    trade_regimes: list = field(default_factory=list)
    # --- ML-фильтр (LightGBM gate качества сигналов): {"threshold":0.55} ---
    ml_filter: dict = field(default_factory=dict)


class EnsembleV4Strategy:
    """Адаптер Strategy: на закрытии 5м-бара гоняет compute_ensemble
    и возвращает Signal по последнему свежему входу ансамбля."""

    strategy_id = "ensemble_v4"
    version = "1.0.0"

    def __init__(self, params: EnsembleParams):
        self.p = params
        self._last_votes = None
        self._last_skip: str | None = None
        self._regime_cache: dict = {"key": None, "state": None}

    def warmup_bars(self) -> int:
        return 50

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        from app.engine.sessions import is_session_active
        if not candles:
            self._last_skip = "no_candles"
            return None
        last = candles[-1]
        # Оценка на каждом баре (1м), а не только на 5м-границе — нужна для 1м-входа.
        # (раньше был гейт last.ts.minute % 5 != 0 → только 5м)
        # Проверяем сессию через sessions список из конфига
        if self.p.sessions:
            if not is_session_active(last.ts, self.p.sessions):
                self._last_skip = f"session_blocked({last.ts.isoformat()})"
                return None
        if len(candles) < 50:
            self._last_skip = f"buffer_small({len(candles)})"
            return None
        # --- Быстрый режим-гейт (H1, кэш на час): если режим запрещён конфигом
        # (напр. торгуем всё, кроме NEUTRAL) — тяжёлый ансамбль НЕ считаем вообще.
        if self.p.trade_regimes:
            try:
                _key = last.ts.replace(minute=0, second=0, microsecond=0)
                if self._regime_cache.get("key") != _key:
                    from app.services.ensemble import resample as _rs
                    from app.services.regime import RegimeDetector as _RD
                    _h1 = _rs(list(candles), 3600)
                    _states = _RD().compute(_h1) if _h1 else []
                    _st = _states[-1]["state"] if _states else None
                    self._regime_cache = {"key": _key, "state": _st}
                _st_now = self._regime_cache.get("state")
                if _st_now:
                    self._last_regime = {"state": _st_now}
                    if _st_now not in self.p.trade_regimes:
                        self._last_skip = f"regime_blocked({_st_now})"
                        return None
            except Exception:
                pass
        try:
            from app.services.ensemble import compute_ensemble

            req = {
                "figi": self.p.figi,
                "bias_mode": "info",
                "bias": {"tf": self.p.bias_tf, "period": self.p.bias_period},
                "entry_tf": self.p.entry_tf,
                "entry": {"tf": self.p.entry_tf, "lookback": self.p.entry_lookback},
                "entry_session": self.p.session,
                "quorum": self.p.quorum,
                "same_side_reentry_cooldown_bars": 15,
                "carry_overnight": True,
                "opposite_hold": False,
                "exit_policy": {"id": "atr_stop",
                                "params": {"period": 14, "multiplier": self.p.sl_mult,
                                           "risk_reward": self.p.rr}},
                "commission_rate": 0.0005,
                "slippage_bps": 2.0,
                "capital": self.p.capital,
                "lot": self.p.lot,
                "use_all_setups": self.p.use_all_setups,
                "drop_useless": self.p.drop_useless,
                "setups": self.p.setups,
                "from_ts": candles[0].ts.isoformat(),
                "to_ts": last.ts.isoformat(),
            }
            if self.p.entry_confirm_bars and self.p.entry_confirm_bars > 0:
                req["entry_confirm_bars"] = self.p.entry_confirm_bars
            if self.p.vol_thr and self.p.vol_thr > 0:
                req["volume_filter_threshold"] = self.p.vol_thr
            if self.p.neutral_mode:
                req["neutral_mode"] = self.p.neutral_mode
            if self.p.regime_setups_filter:
                req["regime_setups_filter"] = self.p.regime_setups_filter
            if self.p.entry_from_setups:
                req["entry_from_setups"] = True
            if self.p.entry_direction_sid:
                req["entry_direction_sid"] = self.p.entry_direction_sid
            req["regime"] = {"tf": getattr(self.p, "regime_tf", "hour") or "hour"}
            if self.p.ml_filter:
                req["ml_filter"] = self.p.ml_filter
            if self.p.entry_macd_1m:
                req["entry_macd_1m"] = True
                req["entry_macd_fast"] = self.p.entry_macd_fast
                req["entry_macd_slow"] = self.p.entry_macd_slow
                req["entry_macd_signal"] = self.p.entry_macd_signal
            res = compute_ensemble(list(candles), req)
        except Exception as e:
            import logging as _lg
            self._last_skip = f"compute_error: {type(e).__name__}: {str(e)[:120]}"
            _lg.getLogger("ensemble_strategy").exception(
                "on_bar compute_ensemble FAILED figi=%s candles=%d: %s", self.p.figi[-6:], len(candles), e)
            return None
        if "error" in res:
            import logging as _lg
            self._last_skip = f"res_error: {res.get('error')} bars={res.get('bars')} buf={len(candles)}"
            _lg.getLogger("ensemble_strategy").warning(
                "on_bar compute_ensemble ERROR figi=%s: %s", self.p.figi[-6:], res.get("error"))
            return None
        try:
            tl = (res.get("regime") or {}).get("timeline") or []
            if tl:
                r_cur = tl[-1]
                feats = r_cur.get("features") or {}
                self._last_regime = {
                    "state": r_cur.get("state"),
                    "reason": r_cur.get("reason"),
                    "features": feats,
                    "from": r_cur.get("from"),
                    "to": r_cur.get("to"),
                }
                self._last_vol = feats.get("volume_ratio")
            else:
                self._last_regime = None
                self._last_vol = None
        except Exception:
            self._last_regime = None
            self._last_vol = None
        entries = res.get("static", {}).get("entries", [])
        if not entries:
            self._last_skip = f"no_entries(funnel_raw={(res.get('static', {}).get('funnel') or {}).get('entries_raw')})"
            if last.ts.minute % 5 == 0:
                import logging as _lg
                _lg.getLogger("ensemble_strategy").debug(
                    "on_bar no-entries figi=%s candles=%d funnel=%s",
                    self.p.figi[-6:], len(candles),
                    (res.get("static", {}).get("funnel") or {}).get("entries_raw"))
            return None
        cutoff = last.ts - timedelta(minutes=FRESH_MIN)
        fresh = [e for e in entries if e.get("ts", "") >= cutoff.isoformat()]
        if not fresh:
            self._last_skip = f"no_fresh(newest={entries[-1].get('ts')} last={last.ts.isoformat()})"
            if last.ts.minute % 5 == 0:
                import logging as _lg
                _lg.getLogger("ensemble_strategy").debug(
                    "on_bar no-fresh-entries figi=%s last=%s newest_entry=%s",
                    self.p.figi[-6:], last.ts.isoformat(), entries[-1].get("ts"))
            return None
        self._last_skip = None
        last_e = fresh[-1]
        side = Side.BUY if last_e.get("side") == "BUY" else Side.SELL
        st = res.get("static", {})
        self._last_votes = st.get("votes_last")
        quorum_list = st.get("quorum_list", [])
        qevent = None
        for q in quorum_list:
            if q.get("event_id") == last_e.get("quorum_event_id"):
                qevent = q
                break
        qe_raw = qevent or {}
        qe_feats = qe_raw if isinstance(qe_raw, dict) else {}
        quorum_event = {
            "ts": qe_raw.get("ts", ""),
            "side": qe_raw.get("side", ""),
            "votes": qe_feats.get("votes", 0),
            "buy_votes": qe_feats.get("buy_votes", 0),
            "sell_votes": qe_feats.get("sell_votes", 0),
            "members_for": qe_feats.get("members_for", []),
            "opposition": qe_feats.get("opposition", []),
            "total_members": qe_feats.get("total_members", 0),
            "quorum_k": qe_feats.get("quorum_k", 0),
            "event_id": qe_raw.get("event_id", ""),
            "reason": qe_raw.get("reason", ""),
        }
        # Объём бара входа (1m): текущий + max/min за последние 20 баров, и режим на входе.
        _vols = [float(c.volume or 0) for c in candles[-20:]]
        _vol_now = float(candles[-1].volume or 0)
        _vol_max = max(_vols) if _vols else _vol_now
        _vol_min = min(_vols) if _vols else _vol_now
        _regime_now = (self._last_regime or {}).get("state") if isinstance(self._last_regime, dict) else None
        meta = {
            "entry": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in last_e.items()},
            "quorum_event": quorum_event,
            "setups": {k: v for k, v in (st.get("setups") or {}).items()},
            "volume": {"v": _vol_now, "max": _vol_max, "min": _vol_min},
            "regime": _regime_now,
        }
        return Signal(
            strategy_id=self.strategy_id,
            side=side,
            time=last.ts,
            reason=f"ensemble_v4 {last_e.get('side')}",
            kind="entry",
            features=meta,
        )
