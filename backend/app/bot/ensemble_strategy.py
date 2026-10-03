from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    ticker: str = ""
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
    bias_mode: str = "veto"      # "info" | "veto" | "strict_ct" — отбрасывать ли входы ПРОТИВ bias
    bias_by_state: dict = field(default_factory=dict)  # per-regime bias (combo): {state: {"tf","period"}}
    entry_tf: str = "5min"       # микро-вход: "1min" | "5min"
    entry_lookback: int = 1      # окно микро-брейкаута (бары entry_tf)
    # --- per-ticker optuna-параметры (расширение; дефолты == прежний хардкод) ---
    sl_mult: float = 2.0
    rr: float = 2.0
    vol_thr: float = 0.0
    neutral_mode: str | None = None
    entry_confirm_bars: int = 0  # ждать N подряд подтверждающих свечей в сторону входа; 0 = без подтверждения
    entry_confirm_closes: int = 0  # N 1м-закрытий строго по направлению (BUY: каждое выше предыдущего)
    entry_confirm_closes_sides: list = field(default_factory=lambda: ["BUY"])  # к каким сторонам применять
    skip_entry_side: str = ""  # "BUY"|"SELL": не рассматривать входы этой стороны (held-позиция)
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
    # --- RSI-гейт входа: {"period":14, "min":40, "max":60} — BUY только в нейтральной зоне ---
    rsi_filter: dict = field(default_factory=dict)
    # --- Per-regime вход: {"trend": "breakout"} — в тренде ансамбль off, вход по breakout ---
    regime_entry_policy: dict = field(default_factory=dict)


class _No5mMarker(Exception):
    """Внутренний маркер «не 5m-бар» для funnel-агрегации."""


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
        # Funnel-аккумулятор на 5m-баре (счётчики raw/quorum/gate/entry для UI «Анализ»).
        self._NO_5M = type("_No5m", (Exception,), {})
        self._funnel_stats: dict = {
            "period_start": None, "period_end": None,
            "raw_setup_signals": 0, "quorum_candidates": 0,
            "gate_pass": 0, "executed_entries": 0,
            "by_reason": {}, "votes": {},
        }
        self._funnel_last: dict = {}
        self._last_5m_key: str | None = None

    @property
    def _tag(self) -> str:
        """Метка инструмента для логов: тикер, иначе последние 6 символов FIGI."""
        return (getattr(self.p, "ticker", "") or (self.p.figi[-6:] if self.p.figi else "?"))

    def warmup_bars(self) -> int:
        return 50

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        from app.engine.sessions import is_session_active
        if not candles:
            self._last_skip = "no_candles"
            return None
        last = candles[-1]
        # 5m-граница бара: на ней логируем funnel и ENSEMBLE ENTRY.
        # ФИКС: _is_5m_bar раньше нигде не присваивался → NameError ниже по on_bar
        # (строка "if _is_5m_bar:") на КАЖДОМ входе, прошедшем все гейты —
        # сигнал гиб до возврата из on_bar, runtime ловил SIGNAL_ERROR (events),
        # поэтому signals_seen=0 и 0 сделок при полном funnel.
        _is_5m_bar = (last.ts.minute % 5 == 0)
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
                    from app.services.regime import compute_regime as _CR
                    _states, _, _ = _CR(list(candles), 3600)
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
                "ticker": self.p.ticker,
                "bias_mode": self.p.bias_mode,
                "bias": {"tf": self.p.bias_tf, "period": self.p.bias_period},
                "bias_by_state": self.p.bias_by_state or None,
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
            if self.p.entry_confirm_closes and self.p.entry_confirm_closes > 0:
                req["entry_confirm_closes"] = self.p.entry_confirm_closes
                req["entry_confirm_closes_sides"] = list(self.p.entry_confirm_closes_sides or ["BUY"])
            if self.p.skip_entry_side:
                req["skip_entry_side"] = self.p.skip_entry_side
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
            if self.p.rsi_filter:
                req["rsi_filter"] = self.p.rsi_filter
            if self.p.regime_entry_policy:
                req["regime_entry_policy"] = self.p.regime_entry_policy
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
                "on_bar compute_ensemble FAILED inst=%s candles=%d: %s", self._tag, len(candles), e)
            return None
        if "error" in res:
            import logging as _lg
            self._last_skip = f"res_error: {res.get('error')} bars={res.get('bars')} buf={len(candles)}"
            _lg.getLogger("ensemble_strategy").warning(
                "on_bar compute_ensemble ERROR inst=%s: %s", self._tag, res.get("error"))
            return None

        # --- Funnel-аккумуляция и лог ENSEMBLE (только на 5m-баре) ---
        try:
            if last.ts.minute % 5 != 0:
                raise _No5mMarker()
            _st5 = res.get("static", {}) or {}
            _fun = _st5.get("funnel", {}) or {}
            _fun_tf = res.get("static", {}).get("funnel_tf", {}) or {}
            _raw = int(_fun.get("raw_signals", 0))
            _qc = int(_fun.get("quorum_unique", 0))
            _gp = int(_fun.get("accepted_decisions", 0))
            _votes_last = _st5.get("votes_last", {}) or {}
            _em = getattr(self, "_trace_emitter", None)
            if _em is not None and (_raw or _votes_last):
                try:
                    from app.engine.trace import Stage, Status, TraceEvent
                    _em.emit(TraceEvent(
                        stage=Stage.FILTER, status=Status.CREATED, ts_bar=last.ts,
                        ticker=str(getattr(self, "_tag", "") or ""), reason_code="funnel",
                        context={"raw": _raw, "quorum": _qc, "gate": _gp,
                                 "votes_last": (_votes_last if isinstance(_votes_last, dict) else {})},
                    ))
                except Exception:
                    pass
            _bkey = last.ts.strftime("%Y-%m-%d %H:%M") if hasattr(last.ts, "strftime") else str(last.ts)
            if self._last_5m_key != _bkey:
                self._last_5m_key = _bkey
            fs = self._funnel_stats
            if fs.get("period_start") is None:
                fs["period_start"] = _bkey
            fs["period_end"] = _bkey
            fs["raw_setup_signals"] = fs.get("raw_setup_signals", 0) + _raw
            fs["quorum_candidates"] = fs.get("quorum_candidates", 0) + _qc
            fs["gate_pass"] = fs.get("gate_pass", 0) + _gp
            fs["executed_entries"] = fs.get("executed_entries", 0)
            _rej = _fun_tf.get("rejected_by_reason", {}) or {}
            for _code, _n in _rej.items():
                fs["by_reason"][str(_code)] = fs["by_reason"].get(str(_code), 0) + int(_n)
            for _k, _v in (_votes_last or {}).items():
                if isinstance(_v, (int, float)):
                    fs["votes"][str(_k)] = fs["votes"].get(str(_k), 0) + int(_v)
            self._funnel_last = {
                "ts": _bkey,
                "raw": _raw, "quorum": _qc, "gate_pass": _gp,
                "votes": dict(_votes_last or {}),
                "funnel": dict(_fun),
            }
        except Exception:
            pass

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
            _st = res.get("static", {}) or {}
            _funnel = _st.get("funnel") or {}
            _rej = _st.get("rejected") or []
            _by_reason: dict = {}
            for _r in _rej:
                _code = str(_r.get("reason") or "?").split(":")[0]
                # COMBO_BIAS несёт в reason ещё направление сторона->требуемое:
                # COMBO_BIAS:BUY->SELL:TREND_UP:bias=SELL(-1) — группируем по полному
                # шаблону COMBO_BIAS:A->B:режим, чтобы в логе было видно само расхождение.
                if _code == "COMBO_BIAS":
                    _sub = _code.split(":")[0]
                    _rest = str(_r.get("reason")).split(":", 1)[1] if ":" in str(_r.get("reason")) else "?"
                    _pat = _rest.rsplit(":", 1)[0]
                    _code = f"{_sub}|{_pat}"
                _by_reason[_code] = _by_reason.get(_code, 0) + 1
            _why = ", ".join(f"{k}x{v}" for k, v in sorted(_by_reason.items(), key=lambda x: -x[1]))
            self._last_skip = (f"no_entries(cand={_funnel.get('entries_raw')} "
                               f"rej={len(_rej)}: {_why or 'сигналов нет'})")
            if last.ts.minute % 5 == 0:
                import logging as _lg
                _lg.getLogger("ensemble_strategy").debug(
                    "on_bar no-entries inst=%s candles=%d funnel=%s",
                    self._tag, len(candles),
                    (res.get("static", {}).get("funnel") or {}).get("entries_raw"))
            return None
        cutoff = last.ts - timedelta(minutes=FRESH_MIN)
        fresh = [e for e in entries if e.get("ts", "") >= cutoff.isoformat()]
        if not fresh:
            self._last_skip = f"no_fresh(newest={entries[-1].get('ts')} last={last.ts.isoformat()})"
            if last.ts.minute % 5 == 0:
                import logging as _lg
                _lg.getLogger("ensemble_strategy").debug(
                    "on_bar no-fresh-entries inst=%s last=%s newest_entry=%s",
                    self._tag, last.ts.isoformat(), entries[-1].get("ts"))
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
        # Bias на момент входа: базовый (bias_tf) и per-regime (если на входе режим).
        try:
            _bias_value = self._bias_at(candles, last_e.get("ts"))
        except Exception:
            _bias_value = None
        meta = {
            "entry": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in last_e.items()},
            "quorum_event": quorum_event,
            "setups": {k: v for k, v in (st.get("setups") or {}).items()},
            "volume": {"v": _vol_now, "max": _vol_max, "min": _vol_min},
            "regime": _regime_now,
            "bias": _bias_value,
            "gate_path": [],
        }
        if _is_5m_bar:
            import logging as _lg
            _mems = ",".join(str(x) for x in (quorum_event.get("members_for") or [])) or "-"
            _opp = ",".join(str(x) for x in (quorum_event.get("opposition") or [])) or "-"
            _votes_txt = " ".join(
                f"{k}~{v.get('signals')}" for k, v in (st.get("setups") or {}).items()
            ) or "-"
            _lg.getLogger("ensemble_strategy").warning(
                "ENSEMBLE %s ENTRY %s q=%s votes=[%s] in=[%s] opp=[%s] bias=%s reg=%s",
                self._tag, side, quorum_event.get("quorum_k"),
                _votes_txt, _mems, _opp,
                (meta.get("bias") or {}).get("label") if isinstance(meta.get("bias"), dict) else "?",
                _regime_now)
        return Signal(
            strategy_id=self.strategy_id,
            side=side,
            time=last.ts,
            reason=f"ensemble_v4 {last_e.get('side')}",
            kind="entry",
            features=meta,
        )

    def _bias_at(self, candles, ts_src) -> dict:
        """Значение bias (bias_tf) на момент входа + per-regime bias, если есть."""
        from app.services.ensemble import cached_resample, compute_bias, TF_SECONDS
        out: dict = {}
        try:
            _tf = TF_SECONDS.get(self.p.bias_tf or "hour", 3600)
            _bars = cached_resample(list(candles), _tf)
            _map = compute_bias(_bars, int(self.p.bias_period or 50), tf_seconds=_tf)
            if ts_src is not None:
                _b = ts_src if isinstance(ts_src, datetime) else datetime.fromisoformat(str(ts_src))
                _bb = int(_b.timestamp()) // _tf
                _v = _map.get(_bb, 0)
                out["tf"] = self.p.bias_tf or "hour"
                out["value"] = _v
                out["label"] = ("UP" if _v > 0 else "DOWN" if _v < 0 else "NEUTRAL")
        except Exception:
            pass
        _reg = (self._last_regime or {}).get("state") if isinstance(self._last_regime, dict) else None
        if _reg and self.p.bias_by_state:
            _bbs = self.p.bias_by_state.get(_reg) or {}
            if _bbs:
                try:
                    _rtf = TF_SECONDS.get(str(_bbs.get("tf", "30min")), 1800)
                    _rb = cached_resample(list(candles), _rtf)
                    _rm = compute_bias(_rb, int(_bbs.get("period", 50)), tf_seconds=_rtf)
                    if ts_src is not None:
                        _b = ts_src if isinstance(ts_src, datetime) else datetime.fromisoformat(str(ts_src))
                        _rv = _rm.get(int(_b.timestamp()) // _rtf, 0)
                        out["regime_state"] = _reg
                        out["regime_tf"] = str(_bbs.get("tf", "30min"))
                        out["regime_bias"] = _rv
                except Exception:
                    pass
        return out

    def funnel_snapshot(self) -> dict:
        """Снимок аккумулятора funnel для UI «Анализ» (не мутирует)."""
        return dict(self._funnel_stats)
