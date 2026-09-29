from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.engine.costs import CostModel
from app.engine.exits import ExitPolicy, breakeven_stop, early_abort_exit, intrabar_exit, partial_take_exit
from app.engine.ledger import TradeLedger
from app.engine.models import (
    Candle,
    DecisionAction,
    ExitReason,
    Position,
    PositionState,
    Signal,
    Side,
)
from app.engine.policies import SignalPolicy, SignalPolicyConfig
from app.engine.sessions import SessionPolicy, SessionPolicyConfig
from app.engine.views import CandleWindow


@dataclass
class EngineConfig:
    figi: str = "UNKNOWN"
    qty: int = 1
    mode: str = "both"
    allow_short: bool = True
    cost_model: CostModel = field(default_factory=CostModel)
    signal_policy: SignalPolicyConfig = field(default_factory=SignalPolicyConfig)
    session_policy: SessionPolicyConfig | None = None
    neutral_mode: str | None = None  # "gate" | "semi_flip" | None
    regime_bars: list[dict] | None = None  # regime timeline from RegimeDetector
    # --- План выходов v2 (шаг 3, 2026-09-26): opt-in, по умолчанию выключено ---
    wick_tol: float = 0.0        # прощение хвоста для начального SL (абс. цена; 0 = выкл)
    be_trigger_r: float = 0.0    # безубыток: SL -> вход при прибыли >= trigger_r * risk (0 = выкл)
    be_offset_pct: float = 0.0   # безубыток: сдвиг уровня от входа (доля цены)
    abort_r: float = 0.0         # ранний аборт: провал >= abort_r * risk в первые бары (0 = выкл)
    abort_max_bars: int = 0      # ранний аборт: окно в барах после входа
    partial_r: float = 0.0       # частичный тейк: закрыть долю позиции при прибыли >= partial_r * risk (0 = выкл)
    partial_fraction: float = 0.5  # частичный тейк: доля закрываемой позиции (0..1]
    partial_to_be: bool = True   # частичный тейк: после него стоп переносится на вход (безубыток)

    def validate(self) -> None:
        """Invariant-проверка конфига (ENG-015, audit 2026-09-29).

        Падаем сразу и с понятным текстом вместо AttributeError посреди прогона.
        """
        if int(self.qty) <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        if self.mode not in ("both", "long", "short"):
            raise ValueError(f"mode must be both|long|short, got {self.mode!r}")
        if self.neutral_mode not in (None, "gate", "semi_flip"):
            raise ValueError(
                f"neutral_mode must be None|gate|semi_flip, got {self.neutral_mode!r}"
            )
        if not (0.0 < float(self.partial_fraction) <= 1.0):
            raise ValueError(
                f"partial_fraction must be in (0, 1], got {self.partial_fraction}"
            )
        if int(self.abort_max_bars) < 0:
            raise ValueError(f"abort_max_bars must be >= 0, got {self.abort_max_bars}")
        for _name in ("wick_tol", "be_trigger_r", "be_offset_pct", "abort_r", "partial_r"):
            _v = float(getattr(self, _name) or 0.0)
            if _v < 0:
                raise ValueError(f"{_name} must be >= 0, got {_v}")
        if self.partial_r > 0 and int(self.qty) < 1:
            raise ValueError("partial_r требует qty >= 1")
        _lim = float(getattr(self.signal_policy, "entry_limit_atr", 0.0) or 0.0)
        if _lim > 0:
            # ENG-003: feature не реализована (нет _atr_series/_place_limit и
            # жизненного цикла лимита). Лучше явная ошибка, чем AttributeError.
            raise ValueError(
                "entry_limit_atr не поддерживается: лимитный вход не реализован "
                "(ENG-003, audit 2026-09-29). Используйте market-вход (0)."
            )


@dataclass
class _RunState:
    """Переносимое состояние прогона: бывшие локальные переменные run()."""

    ledger = None
    position = None
    pending = None
    pending_kind = None
    last_exit_side = None
    last_exit_bar = None
    exit_candidate = None
    entry_confirm = None
    current_session_date = None
    warmup = None
    tf_minutes = None
    session_active = None


class EngineRunner:
    def __init__(
        self,
        strategy,
        exit_policy: ExitPolicy,
        config: EngineConfig | None = None,
    ):
        self.strategy = strategy
        self.exit_policy = exit_policy
        self.cfg = config or EngineConfig()
        self.cfg.validate()
        self.policy = SignalPolicy(self.cfg.signal_policy)
        self.session = (
            SessionPolicy(self.cfg.session_policy) if self.cfg.session_policy else None
        )
        self._regime_bars = self.cfg.regime_bars or []
        self._regime_ts_cache = None
        self._regime_idx_cache = None
        self._regime_len_cache = None  # L2.6: длина regime_bars на момент кэша
        self._trailing_active = False
        self._pos_key = None      # (entry_index, entry_time) позиции с закэшированным risk
        self._entry_risk = None   # |entry - initial_stop| на момент входа (для BE/abort)
        self._partial_done = None  # pos_key позиции, у которой частичный тейк уже исполнен
        self.exit_coverage: dict = {
            "opposite_received": 0,
            "exit_ignored_flat": 0,
            "held": 0,
            "candidate": 0,
            "confirmed": 0,
            "accepted": 0,
        }

    def _regime_at(self, ts) -> str | None:
        """Look up regime state for timestamp ts from regime_bars (bisect, O(log n))."""
        if not self._regime_bars:
            return None
        import bisect as _bisect
        # L2.6: regime_bars может расти между extend() — кэш валиден только
        # пока те же длина и последний элемент (append-only); иначе перестройка
        # тем же кодом (для статического списка поведение бит-в-бит).
        _stamp = (len(self._regime_bars),
                  id(self._regime_bars[-1]) if self._regime_bars else None,
                  self._regime_bars[-1].get("ts") if self._regime_bars else None)
        if self._regime_ts_cache is None or self._regime_len_cache != _stamp:
            self._regime_len_cache = _stamp
            ts_list = [r.get("ts") for r in self._regime_bars]
            # Отбрасываем None-таймстампы (сохраняем соответствие индексов).
            pairs = [(t, i) for i, t in enumerate(ts_list) if t is not None]
            pairs.sort(key=lambda x: x[0])
            self._regime_ts_cache = [p[0] for p in pairs]
            self._regime_idx_cache = [p[1] for p in pairs]
        ts_list = self._regime_ts_cache
        i = _bisect.bisect_right(ts_list, ts) - 1
        if i < 0:
            return None
        return self._regime_bars[self._regime_idx_cache[i]].get("state")

    def _new_state(self, candles) -> "_RunState":
        st = _RunState()
        total = len(candles)
        st.ledger = TradeLedger()
        # ENG-007: состояние прогона живёт на состоянии _RunState/runner и
        # сбрасывается здесь — повторный run() тем же runner детерминирован.
        self._pos_key = None
        self._entry_risk = None
        self._partial_done = None
        self._trailing_active = False
        self._chain_seq = 0
        self.exit_coverage = {
            "opposite_received": 0,
            "exit_ignored_flat": 0,
            "held": 0,
            "candidate": 0,
            "confirmed": 0,
            "accepted": 0,
        }
        st.position: Position | None = None
        st.pending: Signal | None = None
        st.pending_kind: str | None = None
        st.last_exit_side: Side | None = None
        st.last_exit_bar = -10**9
        st.exit_candidate: dict | None = None
        st.entry_confirm: dict | None = None  # {signal, side, confirm_needed} — ожидание N подряд подтверждающих свечей
        st.warmup = self.strategy.warmup_bars()

        st.tf_minutes = 1440
        if total > 1:
            delta = (candles[1].ts - candles[0].ts).total_seconds() / 60
            st.tf_minutes = max(1, round(delta))
        st.session_active = self.session is not None and st.tf_minutes < 1440

        st.current_session_date = None

        return st

    def _body(self, i, bar, candles, st) -> None:
        """Один бар конвейера (бывшее тело цикла run); return = бывший continue."""
        bar = candles[i]

        if st.session_active and not self.cfg.session_policy.overnight:
            sd = self.session.local(bar.ts).date()
            if st.current_session_date is not None and sd != st.current_session_date:
                if st.position is not None:
                    st.last_exit_side = Side.BUY if st.position.state is PositionState.LONG else Side.SELL
                    st.last_exit_bar = i
                    st.exit_candidate = None
                    st.position = self._close(
                        i,
                        bar.ts,
                        st.position,
                        bar.open,
                        ExitReason.SESSION_CLOSE.value,
                        st.ledger,
                    )
            st.current_session_date = sd
            # принудительное закрытие в конце основной сессии (после close_time)
            if st.position is not None and self.cfg.session_policy.force_flat_at_session_end:
                lt = self.session.local(bar.ts)
                ch, cm = (int(x) for x in self.session.config.close_time.split(":"))
                bar_min = lt.hour * 60 + lt.minute
                close_m = ch * 60 + cm
                if bar_min > close_m:
                    st.last_exit_side = Side.BUY if st.position.state is PositionState.LONG else Side.SELL
                    st.last_exit_bar = i
                    st.exit_candidate = None
                    st.position = self._close(
                        i, bar.ts, st.position, bar.open, ExitReason.SESSION_CLOSE.value, st.ledger,
                    )

        if st.pending is not None:
            if st.pending_kind == "entry":
                st.position = self._open(i, bar, candles, st.pending, st.position, st.ledger)
            elif st.pending_kind == "flip":
                # --- NEUTRAL gate / semi-flip ---
                neutral_mode = self.cfg.neutral_mode
                regime = self._regime_at(bar.ts) if neutral_mode else None
                if neutral_mode and regime == "NEUTRAL":
                    if neutral_mode == "gate":
                        # Полный запрет flip в NEUTRAL: не закрываем, не входим
                        st.pending = None
                        st.pending_kind = None
                        st.ledger.log(i, bar.ts, "DECISION", "NEUTRAL_GATE flip blocked")
                        return
                    elif neutral_mode == "semi_flip":
                        # Semi-flip: закрываем позицию, НЕ входим в новую
                        if st.position is not None:
                            st.last_exit_side = Side.BUY if st.position.state is PositionState.LONG else Side.SELL
                            st.last_exit_bar = i
                            st.exit_candidate = None
                            st.position = self._close(
                                i, bar.ts, st.position, bar.open, ExitReason.SIGNAL_EXIT.value, st.ledger,
                            )
                        st.pending = None
                        st.pending_kind = None
                        st.ledger.log(i, bar.ts, "DECISION", "NEUTRAL_SEMI_FLIP closed, staying flat")
                        return
                # Обычный flip (вне NEUTRAL или neutral_mode=None)
                if st.position is not None:
                    st.last_exit_side = Side.BUY if st.position.state is PositionState.LONG else Side.SELL
                    st.last_exit_bar = i
                    st.exit_candidate = None
                    st.entry_confirm = None
                    self._close(
                        i, bar.ts, st.position, bar.open, ExitReason.SIGNAL_EXIT.value, st.ledger,
                    )
                st.position = self._open(i, bar, candles, st.pending, st.position, st.ledger)
            else:
                if st.position is not None:
                    st.last_exit_side = Side.BUY if st.position.state is PositionState.LONG else Side.SELL
                    st.last_exit_bar = i
                    st.exit_candidate = None
                st.position = self._close(
                    i,
                    bar.ts,
                    st.position,
                    bar.open,
                    ExitReason.SIGNAL_EXIT.value,
                    st.ledger,
                )
            st.pending = None
            st.pending_kind = None

        if st.position is not None:
            _side_for_pol = Side.BUY if st.position.state is PositionState.LONG else Side.SELL
            _pos_key = (st.position.entry_index, st.position.entry_time)
            if self._pos_key != _pos_key:
                self._pos_key = _pos_key
                self._entry_risk = (
                    abs(st.position.entry_price - st.position.initial_stop)
                    if st.position.initial_stop is not None else None
                )
            # --- Фаза 1 (ENG-002, audit 2026-09-29): выходы только по уровням,
            # известным ДО этого бара. BE/trailing пересчитываются в конце бара
            # (фаза 2) по его close и вступают в силу со СЛЕДУЮЩЕГО бара —
            # иначе close текущего бара ретроактивно управлял бы его low/high.
            tp = None if self._trailing_active else st.position.target
            price, reason = intrabar_exit(
                bar, st.position.state, st.position.initial_stop, tp,
                wick_tol=self.cfg.wick_tol,
            )
            if price is None and self.cfg.abort_r > 0 and self._entry_risk is not None:
                _ap, _ar = early_abort_exit(
                    bar,
                    _side_for_pol,
                    st.position.entry_price,
                    risk=self._entry_risk,
                    bars_held=st.position.bars_held,
                    max_bars=self.cfg.abort_max_bars,
                    abort_r=self.cfg.abort_r,
                )
                if _ap is not None:
                    price, reason = _ap, _ar
            if (
                price is None
                and self.cfg.partial_r > 0
                and self._entry_risk is not None
                and st.position.qty > 0
                and self._partial_done != _pos_key
            ):
                _pp = partial_take_exit(
                    bar, _side_for_pol, st.position.entry_price,
                    risk=self._entry_risk, partial_r=self.cfg.partial_r,
                )
                if _pp is not None:
                    _closed = self._close_partial(
                        i, bar.ts, st.position, _pp, self.cfg.partial_fraction, st.ledger,
                    )
                    self._partial_done = _pos_key
                    st.ledger.log(i, bar.ts, "DECISION",
                               f"PARTIAL_TAKE r={self.cfg.partial_r} closed={_closed} left={st.position.qty}")
                    if st.position.qty <= 0:
                        st.last_exit_side = Side.BUY if st.position.state is PositionState.LONG else Side.SELL
                        st.last_exit_bar = i
                        st.exit_candidate = None
                        st.position = None
                    elif self.cfg.partial_to_be:
                        _moved = False
                        if st.position.state is PositionState.LONG and (
                            st.position.initial_stop is None
                            or st.position.initial_stop < st.position.entry_price
                        ):
                            st.position.initial_stop = st.position.entry_price
                            _moved = True
                        elif st.position.state is PositionState.SHORT and (
                            st.position.initial_stop is None
                            or st.position.initial_stop > st.position.entry_price
                        ):
                            st.position.initial_stop = st.position.entry_price
                            _moved = True
                        if _moved:
                            st.ledger.log(i, bar.ts, "DECISION",
                                       f"PARTIAL_STOP_TO_BE -> {st.position.entry_price:.6f}")
            if price is not None:
                st.last_exit_side = Side.BUY if st.position.state is PositionState.LONG else Side.SELL
                st.last_exit_bar = i
                st.exit_candidate = None
                st.position = self._close(i, bar.ts, st.position, price, reason, st.ledger)
            elif st.position is not None:
                # --- Фаза 2 (ENG-002): пересчёт защитных уровней по ЗАКРЫТОМУ
                # бару; действуют со следующего бара.
                if self.cfg.be_trigger_r > 0 and self._entry_risk is not None:
                    _be = breakeven_stop(
                        _side_for_pol,
                        st.position.entry_price,
                        st.position.initial_stop,
                        CandleWindow(candles, 0, i + 1),
                        risk=self._entry_risk,
                        trigger_r=self.cfg.be_trigger_r,
                        offset_pct=self.cfg.be_offset_pct,
                    )
                    if _be != st.position.initial_stop:
                        st.position.initial_stop = _be
                        st.ledger.log(i, bar.ts, "DECISION",
                                   f"BREAKEVEN_STOP -> {_be:.6f}")
                update_stop = getattr(self.exit_policy, "update_stop", None)
                if update_stop is not None:
                    st.position.initial_stop = update_stop(
                        _side_for_pol,
                        st.position.entry_price,
                        st.position.initial_stop,
                        CandleWindow(candles, 0, i + 1),
                        qty=st.position.qty,
                        commission=st.position.entry_commission,
                    )
                if not self._trailing_active:
                    activate = getattr(self.exit_policy, "trailing_activated", None)
                    _act_res = activate(
                        _side_for_pol,
                        st.position.entry_price,
                        st.position.qty,
                        st.position.entry_commission,
                        CandleWindow(candles, 0, i + 1),
                    ) if activate is not None else False
                    if _act_res:
                        self._trailing_active = True
                        st.position.target = None
                        st.ledger.log(i, bar.ts, "DECISION",
                                   "TRAILING_ACTIVATED pnl>=comm*4, signal/tp выходят отключены")

        if st.position is not None:
            st.position.bars_held += 1

        if st.entry_confirm is not None and st.position is None:
            n_conf = max(0, int(self.cfg.signal_policy.entry_confirm_bars))
            if n_conf > 0:
                bullish = (st.entry_confirm["side"] is Side.BUY and bar.close > bar.open)
                bearish = (st.entry_confirm["side"] is Side.SELL and bar.close < bar.open)
                if bullish or bearish:
                    st.entry_confirm["confirm_needed"] -= 1
                    st.ledger.log(
                        i, bar.ts, "DECISION",
                        f"ENTRY_CONFIRM {bar.close:.4f} {st.entry_confirm['side'].value} "
                        f"{st.entry_confirm['confirm_needed']} left",
                    )
                    if st.entry_confirm["confirm_needed"] <= 0:
                        st.pending, st.pending_kind = st.entry_confirm["signal"], "entry"
                        st.entry_confirm = None
                else:
                    st.entry_confirm["confirm_needed"] = n_conf
                    st.ledger.log(
                        i, bar.ts, "DECISION",
                        f"ENTRY_CONFIRM_RESET {bar.close:.4f} не в сторону {st.entry_confirm['side'].value}",
                    )


    def _poll(self, i, candles, st) -> None:
        """Опрос стратегии за бар i (бывший if-блок); return = бывший continue."""
        bar = candles[i]
        signal = self.strategy.on_bar(CandleWindow(candles, 0, i + 1))
        if signal is not None and st.position is not None and self._trailing_active:
            # Трейлинг активен: любые противоположные сигналы (и entry-флипы,
            # и явные exit) игнорируются — позиция живёт до подтянутого стопа.
            opp = (st.position.state is PositionState.LONG and signal.side is Side.SELL) or (
                st.position.state is PositionState.SHORT and signal.side is Side.BUY
            )
            if opp:
                self.exit_coverage["held"] += 1
                st.ledger.log(i, bar.ts, "DECISION", "HOLD_TRAILING opposite signal ignored")
                return
        if signal is not None and signal.kind == "exit":
            # === поток выхода: противоположный сигнал, отдельно от входа ===
            if st.position is None:
                self.exit_coverage["exit_ignored_flat"] += 1
                return
            same_side = (st.position.state is PositionState.LONG and signal.side is Side.BUY) or (
                st.position.state is PositionState.SHORT and signal.side is Side.SELL
            )
            if same_side:
                return
            self.exit_coverage["opposite_received"] += 1
            policy = self.cfg.signal_policy
            if policy.opposite_hold:
                self.exit_coverage["held"] += 1
                st.exit_candidate = None
                st.ledger.log(i, bar.ts, "DECISION", f"HOLD_NO_EXIT {signal.side.value} held")
                return
            confirm = policy.exit_confirm_window_bars
            if policy.confirm_flip and confirm <= 0:
                self.exit_coverage["accepted"] += 1
                st.pending, st.pending_kind = signal, "flip"
                return
            if confirm > 0:
                if st.exit_candidate is None:
                    st.exit_candidate = {"bar": i, "side": signal.side}
                    self.exit_coverage["candidate"] += 1
                    st.ledger.log(
                        i, bar.ts, "DECISION",
                        f"EXIT_CANDIDATE {signal.side.value} waiting confirm within {confirm}b",
                    )
                elif policy.confirm_flip:
                    st.exit_candidate = None
                    self.exit_coverage["accepted"] += 1
                    st.ledger.log(i, bar.ts, "DECISION", f"FLIP_CONFIRMED {signal.side.value}")
                    st.pending, st.pending_kind = signal, "flip"
                else:
                    st.exit_candidate = None
                    self.exit_coverage["accepted"] += 1
                    st.ledger.log(i, bar.ts, "DECISION", f"EXIT_CONFIRMED {signal.side.value}")
                    st.pending, st.pending_kind = signal, "exit"
                return
            self.exit_coverage["accepted"] += 1
            st.pending, st.pending_kind = signal, "exit"
            return

        state = st.position.state if st.position else PositionState.FLAT
        bars_held = st.position.bars_held if st.position else 0
        action, note = self.policy.decide(signal, state, bars_held)
        st.ledger.log(i, bar.ts, "DECISION", f"{action.value} {note}".strip())
        if action is DecisionAction.ACCEPT_ENTRY:
            entry_allowed = True
            cooldown = self.cfg.signal_policy.same_side_reentry_cooldown_bars
            if (
                cooldown > 0
                and st.last_exit_side is not None
                and st.last_exit_side is signal.side
                and i - st.last_exit_bar <= cooldown
            ):
                entry_allowed = False
                st.ledger.log(
                    i, bar.ts, "DECISION",
                    f"REJECT_REENTRY same-side {signal.side.value} "
                    f"{(i - st.last_exit_bar)}b < cooldown {cooldown}b",
                )
            if st.session_active:
                ok_session, cutoff_note = self.session.can_enter(bar.ts, st.tf_minutes)
                if not ok_session:
                    entry_allowed = False
                    st.ledger.log(i, bar.ts, "DECISION",
                               f"REJECT_SESSION_CUTOFF {cutoff_note}")
            # ENG-004 (audit 2026-09-29): ограничение направления действует
            # независимо от session state — раньше mode=short пропускал LONG
            # на intraday-барах.
            if self.cfg.mode == "long" and signal.side is Side.SELL:
                entry_allowed = False
                st.ledger.log(i, bar.ts, "DECISION", "SKIP_ENTRY mode=long")
            elif self.cfg.mode == "short" and signal.side is Side.BUY:
                entry_allowed = False
                st.ledger.log(i, bar.ts, "DECISION", "SKIP_ENTRY mode=short")
            if signal.side is Side.SELL and not self.cfg.allow_short:
                entry_allowed = False
                st.ledger.log(i, bar.ts, "DECISION", "REJECT_SHORT short not allowed")
            if entry_allowed:
                n_conf = max(0, int(self.cfg.signal_policy.entry_confirm_bars))
                if n_conf > 0:
                    if st.entry_confirm is None or st.entry_confirm["side"] != signal.side:
                        st.entry_confirm = {"signal": signal, "side": signal.side,
                                         "confirm_needed": n_conf}
                        st.ledger.log(
                            i, bar.ts, "DECISION",
                            f"ENTRY_WAIT_CONFIRM {n_conf} следующих свечей в сторону {signal.side.value}",
                        )
                else:
                    st.pending, st.pending_kind = signal, "entry"
        elif action is DecisionAction.ACCEPT_EXIT:
            policy = self.cfg.signal_policy
            if policy.opposite_hold:
                # держим позицию: слабый противоположный сигнал не закрывает
                st.exit_candidate = None
                st.ledger.log(i, bar.ts, "DECISION", f"HOLD_NO_EXIT {signal.side.value} held")
                return
            confirm = policy.exit_confirm_window_bars
            if policy.confirm_flip and confirm <= 0:
                # мгновенный переворот по противоположному сигналу
                st.pending, st.pending_kind = signal, "flip"
            elif confirm > 0:
                if st.exit_candidate is None:
                    st.exit_candidate = {"bar": i, "side": signal.side}
                    st.ledger.log(
                        i, bar.ts, "DECISION",
                        f"EXIT_CANDIDATE {signal.side.value} waiting confirm within {confirm}b",
                    )
                elif policy.confirm_flip:
                    st.exit_candidate = None
                    st.ledger.log(i, bar.ts, "DECISION", f"FLIP_CONFIRMED {signal.side.value}")
                    st.pending, st.pending_kind = signal, "flip"
                else:
                    st.exit_candidate = None
                    st.ledger.log(i, bar.ts, "DECISION", f"EXIT_CONFIRMED {signal.side.value}")
                    st.pending, st.pending_kind = signal, "exit"
            else:
                st.pending, st.pending_kind = signal, "exit"
        if (
            st.exit_candidate is not None
            and i - st.exit_candidate["bar"] >= self.cfg.signal_policy.exit_confirm_window_bars
        ):
            st.exit_candidate = None

    @staticmethod
    def _validate_candles(candles: Sequence[Candle]) -> None:
        """Инварианты входной серии (ENG-015, audit 2026-09-29)."""
        import math
        prev_ts = None
        for idx, c in enumerate(candles):
            for name, value in (
                ("open", c.open), ("high", c.high), ("low", c.low), ("close", c.close),
            ):
                try:
                    fv = float(value)
                except (TypeError, ValueError):
                    raise ValueError(f"candle[{idx}].{name} is not a number: {value!r}") from None
                if not math.isfinite(fv):
                    raise ValueError(f"candle[{idx}].{name} is not finite: {value!r}")
            if float(c.high) < float(c.low):
                raise ValueError(f"candle[{idx}]: high({c.high}) < low({c.low})")
            if float(c.high) < max(float(c.open), float(c.close)) - 1e-12:
                raise ValueError(f"candle[{idx}]: high({c.high}) < max(open, close)")
            if float(c.low) > min(float(c.open), float(c.close)) + 1e-12:
                raise ValueError(f"candle[{idx}]: low({c.low}) > min(open, close)")
            if prev_ts is not None and c.ts <= prev_ts:
                raise ValueError(
                    f"candle[{idx}]: ts not strictly increasing ({c.ts} <= {prev_ts})"
                )
            prev_ts = c.ts

    def run(self, candles: Sequence[Candle], progress_cb=None) -> TradeLedger:
        reset = getattr(self.strategy, "reset", None)
        if callable(reset):
            # ENG-007: повторный run() тем же runner детерминирован — стратегия
            # возвращается в исходное состояние перед прогоном.
            reset()
        total = len(candles)
        if total == 0:
            # ENG-006: пустой вход — пустой журнал (симметрия с finalize()).
            return TradeLedger()
        self._validate_candles(candles)
        st = self._new_state(candles)
        for i in range(total):
            bar = candles[i]
            if progress_cb is not None and i > 0 and i % 500 == 0:
                progress_cb(i, total, bar.ts)
            self._body(i, bar, candles, st)
            if i + 1 < total:
                if i >= st.warmup - 1:
                    self._poll(i, candles, st)
                else:
                    # ENG-008: stateful-стратегии получают закрытые бары с самого
                    # начала; сигналы до формального warmup подавляются.
                    self.strategy.on_bar(CandleWindow(candles, 0, i + 1))
        last = candles[-1]
        if st.position is not None:
            self._close(total - 1, last.ts, st.position, last.close, ExitReason.END_OF_DATA.value, st.ledger)

        return st.ledger

    def _open(        self,
        index: int,
        bar: Candle,
        candles: Sequence[Candle],
        signal: Signal,
        position: Position | None,
        ledger: TradeLedger,
    ) -> Position:
        self._trailing_active = False
        self._partial_done = None  # новая позиция — частичный тейк снова доступен
        side = signal.side
        base = bar.open
        fill = self.cfg.cost_model.fill_price(base, side)
        notional = fill * self.cfg.qty
        commission = self.cfg.cost_model.commission(notional)
        slippage = abs(fill - base) * self.cfg.qty
        # ENG-001 (audit 2026-09-29): план выходов строится по ЗАКРЫТОЙ истории
        # ДО бара исполнения. Вход по open этого бара, его high/low/close ещё
        # неизвестны — передавать их политике нельзя (look-ahead).
        plan = self.exit_policy.plan_entry(side, fill, CandleWindow(candles, 0, index))
        state = PositionState.LONG if side is Side.BUY else PositionState.SHORT
        self._chain_seq = getattr(self, "_chain_seq", 0) + 1
        chain_id = f"{self.cfg.figi}-P{self._chain_seq:04d}"
        ledger.log(index, bar.ts, "FILL_ENTRY",
                   f"{side.value} qty={self.cfg.qty} price={fill} chain={chain_id}")
        return Position(
            figi=self.cfg.figi,
            state=state,
            qty=self.cfg.qty,
            entry_time=bar.ts,
            entry_index=index,
            entry_price=fill,
            initial_stop=plan.stop_loss,
            target=plan.take_profit,
            bars_held=0,
            entry_commission=commission,
            entry_slippage=slippage,
            chain_id=chain_id,
        )

    def _close(
        self,
        index: int,
        ts,
        position: Position | None,
        base_price: float,
        reason: str,
        ledger: TradeLedger,
    ) -> None:
        if position is None:
            return None
        action_side = Side.SELL if position.state is PositionState.LONG else Side.BUY
        fill = self.cfg.cost_model.fill_price(base_price, action_side)
        exit_commission = self.cfg.cost_model.commission(fill * position.qty)
        exit_slippage = abs(fill - base_price) * position.qty

        if position.state is PositionState.LONG:
            gross = (fill - position.entry_price) * position.qty
        else:
            gross = (position.entry_price - fill) * position.qty

        commission = position.entry_commission + exit_commission
        slippage = position.entry_slippage + exit_slippage
        net = gross - commission

        trade = ledger.add_trade(
            figi=position.figi,
            side=position.state.value,
            qty=position.qty,
            entry_index=position.entry_index,
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            exit_index=index,
            exit_time=ts,
            exit_price=fill,
            bars_held=position.bars_held,
            gross_pnl=gross,
            commission=commission,
            slippage=slippage,
            net_pnl=net,
            exit_reason=reason,
            initial_stop=position.initial_stop,
            take_profit=position.target,
            chain_id=position.chain_id,
        )
        ledger.log(index, ts, "FILL_EXIT", f"trade={trade.trade_id} reason={reason} net={net:.4f}")
        return None

    def _close_partial(
        self,
        index: int,
        ts,
        position: Position,
        base_price: float,
        fraction: float,
        ledger: TradeLedger,
    ) -> int:
        """Частичное закрытие позиции (шаг 5 плана выходов, 2026-09-26).

        Закрывает max(1, round(qty * fraction)) единиц по цене base_price и
        записывает Trade с причиной partial_take. Входная комиссия и слиппедж
        распределяются пропорционально между закрытой и оставшейся частью,
        чтобы сумма по сделкам совпала с полным закрытием. Если закрывается
        вся позиция — вызывающий код трактует это как полный выход.
        """
        frac = min(max(float(fraction), 0.0), 1.0)
        qty_to_close = max(1, round(position.qty * frac))
        if qty_to_close >= position.qty:
            qty_to_close = position.qty
        action_side = Side.SELL if position.state is PositionState.LONG else Side.BUY
        fill = self.cfg.cost_model.fill_price(base_price, action_side)
        exit_commission = self.cfg.cost_model.commission(fill * qty_to_close)
        exit_slippage = abs(fill - base_price) * qty_to_close

        share = qty_to_close / position.qty
        entry_commission_share = position.entry_commission * share
        entry_slippage_share = position.entry_slippage * share
        position.entry_commission -= entry_commission_share
        position.entry_slippage -= entry_slippage_share

        if position.state is PositionState.LONG:
            gross = (fill - position.entry_price) * qty_to_close
        else:
            gross = (position.entry_price - fill) * qty_to_close

        commission = entry_commission_share + exit_commission
        slippage = entry_slippage_share + exit_slippage
        net = gross - commission

        ledger.add_trade(
            figi=position.figi,
            side=position.state.value,
            qty=qty_to_close,
            entry_index=position.entry_index,
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            exit_index=index,
            exit_time=ts,
            exit_price=fill,
            bars_held=position.bars_held,
            gross_pnl=gross,
            commission=commission,
            slippage=slippage,
            net_pnl=net,
            exit_reason=ExitReason.PARTIAL.value,
            initial_stop=position.initial_stop,
            take_profit=position.target,
            chain_id=position.chain_id,
        )
        ledger.log(index, ts, "FILL_EXIT",
                   f"partial qty={qty_to_close} price={fill} net={net:.4f} left={position.qty - qty_to_close}")
        position.qty -= qty_to_close
        return qty_to_close
