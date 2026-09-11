from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.engine.costs import CostModel
from app.engine.exits import ExitPolicy, intrabar_exit
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


class CandlePrefix(Sequence):
    """Лёгкое представление префикса candles[:n] без копирования списка.

    Используется в hot-path runner вместо срезов (срез 28k баров × 84k раз = O(n²))."""

    __slots__ = ("_src", "_n")

    def __init__(self, src: Sequence, n: int):
        self._src = src
        self._n = n

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, i):
        n = self._n
        if isinstance(i, slice):
            start, stop, step = i.indices(n)
            return [self._src[j] for j in range(start, stop, step)]
        if i < 0:
            i += n
        if i < 0 or i >= n:
            raise IndexError(i)
        return self._src[i]


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
        self.policy = SignalPolicy(self.cfg.signal_policy)
        self.session = (
            SessionPolicy(self.cfg.session_policy) if self.cfg.session_policy else None
        )
        self._regime_bars = self.cfg.regime_bars or []
        self._regime_ts_cache = None
        self._regime_idx_cache = None
        self._trailing_active = False
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
        if self._regime_ts_cache is None:
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

    def run(self, candles: Sequence[Candle], progress_cb=None) -> TradeLedger:
        ledger = TradeLedger()
        position: Position | None = None
        pending: Signal | None = None
        pending_kind: str | None = None
        last_exit_side: Side | None = None
        last_exit_bar = -10**9
        exit_candidate: dict | None = None
        entry_confirm: dict | None = None  # {signal, side, confirm_needed} — ожидание N подряд подтверждающих свечей
        warmup = self.strategy.warmup_bars()
        total = len(candles)

        tf_minutes = 1440
        if total > 1:
            delta = (candles[1].ts - candles[0].ts).total_seconds() / 60
            tf_minutes = max(1, round(delta))
        session_active = self.session is not None and tf_minutes < 1440

        current_session_date = None

        for i in range(total):
            bar = candles[i]

            if progress_cb is not None and i > 0 and i % 500 == 0:
                progress_cb(i, total, bar.ts)

            if session_active and not self.cfg.session_policy.overnight:
                sd = self.session.local(bar.ts).date()
                if current_session_date is not None and sd != current_session_date:
                    if position is not None:
                        last_exit_side = Side.BUY if position.state is PositionState.LONG else Side.SELL
                        last_exit_bar = i
                        exit_candidate = None
                        position = self._close(
                            i,
                            bar.ts,
                            position,
                            bar.open,
                            ExitReason.SESSION_CLOSE.value,
                            ledger,
                        )
                current_session_date = sd
                # принудительное закрытие в конце основной сессии (после close_time)
                if position is not None and self.cfg.session_policy.force_flat_at_session_end:
                    lt = self.session.local(bar.ts)
                    ch, cm = (int(x) for x in self.session.config.close_time.split(":"))
                    bar_min = lt.hour * 60 + lt.minute
                    close_m = ch * 60 + cm
                    if bar_min > close_m:
                        last_exit_side = Side.BUY if position.state is PositionState.LONG else Side.SELL
                        last_exit_bar = i
                        exit_candidate = None
                        position = self._close(
                            i, bar.ts, position, bar.open, ExitReason.SESSION_CLOSE.value, ledger,
                        )

            if pending is not None:
                if pending_kind == "entry":
                    position = self._open(i, bar, candles, pending, position, ledger)
                elif pending_kind == "flip":
                    # --- NEUTRAL gate / semi-flip ---
                    neutral_mode = self.cfg.neutral_mode
                    regime = self._regime_at(bar.ts) if neutral_mode else None
                    if neutral_mode and regime == "NEUTRAL":
                        if neutral_mode == "gate":
                            # Полный запрет flip в NEUTRAL: не закрываем, не входим
                            pending = None
                            pending_kind = None
                            ledger.log(i, bar.ts, "DECISION", "NEUTRAL_GATE flip blocked")
                            continue
                        elif neutral_mode == "semi_flip":
                            # Semi-flip: закрываем позицию, НЕ входим в новую
                            if position is not None:
                                last_exit_side = Side.BUY if position.state is PositionState.LONG else Side.SELL
                                last_exit_bar = i
                                exit_candidate = None
                                self._close(
                                    i, bar.ts, position, bar.open, ExitReason.SIGNAL_EXIT.value, ledger,
                                )
                            pending = None
                            pending_kind = None
                            ledger.log(i, bar.ts, "DECISION", "NEUTRAL_SEMI_FLIP closed, staying flat")
                            continue
                    # Обычный flip (вне NEUTRAL или neutral_mode=None)
                    if position is not None:
                        last_exit_side = Side.BUY if position.state is PositionState.LONG else Side.SELL
                        last_exit_bar = i
                        exit_candidate = None
                        entry_confirm = None
                        self._close(
                            i, bar.ts, position, bar.open, ExitReason.SIGNAL_EXIT.value, ledger,
                        )
                    position = self._open(i, bar, candles, pending, position, ledger)
                else:
                    if position is not None:
                        last_exit_side = Side.BUY if position.state is PositionState.LONG else Side.SELL
                        last_exit_bar = i
                        exit_candidate = None
                    position = self._close(
                        i,
                        bar.ts,
                        position,
                        bar.open,
                        ExitReason.SIGNAL_EXIT.value,
                        ledger,
                    )
                pending = None
                pending_kind = None

            if position is not None:
                update_stop = getattr(self.exit_policy, "update_stop", None)
                _side_for_pol = Side.BUY if position.state is PositionState.LONG else Side.SELL
                if update_stop is not None:
                    position.initial_stop = update_stop(
                        _side_for_pol,
                        position.entry_price,
                        position.initial_stop,
                        CandlePrefix(candles, i + 1),
                        qty=position.qty,
                        commission=position.entry_commission,
                    )
                if not self._trailing_active:
                    activate = getattr(self.exit_policy, "trailing_activated", None)
                    _act_res = activate(
                        _side_for_pol,
                        position.entry_price,
                        position.qty,
                        position.entry_commission,
                        CandlePrefix(candles, i + 1),
                    ) if activate is not None else False
                    if _act_res:
                        self._trailing_active = True
                        position.target = None
                        ledger.log(i, bar.ts, "DECISION",
                                   "TRAILING_ACTIVATED pnl>=comm*4, signal/tp выходят отключены")
                tp = None if self._trailing_active else position.target
                price, reason = intrabar_exit(
                    bar, position.state, position.initial_stop, tp
                )
                if price is not None:
                    last_exit_side = Side.BUY if position.state is PositionState.LONG else Side.SELL
                    last_exit_bar = i
                    exit_candidate = None
                    position = self._close(i, bar.ts, position, price, reason, ledger)

            if position is not None:
                position.bars_held += 1

            if entry_confirm is not None and position is None:
                n_conf = max(0, int(self.cfg.signal_policy.entry_confirm_bars))
                if n_conf > 0:
                    bullish = (entry_confirm["side"] is Side.BUY and bar.close > bar.open)
                    bearish = (entry_confirm["side"] is Side.SELL and bar.close < bar.open)
                    if bullish or bearish:
                        entry_confirm["confirm_needed"] -= 1
                        ledger.log(
                            i, bar.ts, "DECISION",
                            f"ENTRY_CONFIRM {bar.close:.4f} {entry_confirm['side'].value} "
                            f"{entry_confirm['confirm_needed']} left",
                        )
                        if entry_confirm["confirm_needed"] <= 0:
                            pending, pending_kind = entry_confirm["signal"], "entry"
                            entry_confirm = None
                    else:
                        entry_confirm["confirm_needed"] = n_conf
                        ledger.log(
                            i, bar.ts, "DECISION",
                            f"ENTRY_CONFIRM_RESET {bar.close:.4f} не в сторону {entry_confirm['side'].value}",
                        )

            if i + 1 < total and i >= warmup - 1:
                signal = self.strategy.on_bar(CandlePrefix(candles, i + 1))
                if signal is not None and position is not None and self._trailing_active:
                    # Трейлинг активен: любые противоположные сигналы (и entry-флипы,
                    # и явные exit) игнорируются — позиция живёт до подтянутого стопа.
                    opp = (position.state is PositionState.LONG and signal.side is Side.SELL) or (
                        position.state is PositionState.SHORT and signal.side is Side.BUY
                    )
                    if opp:
                        self.exit_coverage["held"] += 1
                        ledger.log(i, bar.ts, "DECISION", "HOLD_TRAILING opposite signal ignored")
                        continue
                if signal is not None and signal.kind == "exit":
                    # === поток выхода: противоположный сигнал, отдельно от входа ===
                    if position is None:
                        self.exit_coverage["exit_ignored_flat"] += 1
                        continue
                    same_side = (position.state is PositionState.LONG and signal.side is Side.BUY) or (
                        position.state is PositionState.SHORT and signal.side is Side.SELL
                    )
                    if same_side:
                        continue
                    self.exit_coverage["opposite_received"] += 1
                    policy = self.cfg.signal_policy
                    if policy.opposite_hold:
                        self.exit_coverage["held"] += 1
                        exit_candidate = None
                        ledger.log(i, bar.ts, "DECISION", f"HOLD_NO_EXIT {signal.side.value} held")
                        continue
                    confirm = policy.exit_confirm_window_bars
                    if policy.confirm_flip and confirm <= 0:
                        self.exit_coverage["accepted"] += 1
                        pending, pending_kind = signal, "flip"
                        continue
                    if confirm > 0:
                        if exit_candidate is None:
                            exit_candidate = {"bar": i, "side": signal.side}
                            self.exit_coverage["candidate"] += 1
                            ledger.log(
                                i, bar.ts, "DECISION",
                                f"EXIT_CANDIDATE {signal.side.value} waiting confirm within {confirm}b",
                            )
                        elif policy.confirm_flip:
                            exit_candidate = None
                            self.exit_coverage["accepted"] += 1
                            ledger.log(i, bar.ts, "DECISION", f"FLIP_CONFIRMED {signal.side.value}")
                            pending, pending_kind = signal, "flip"
                        else:
                            exit_candidate = None
                            self.exit_coverage["accepted"] += 1
                            ledger.log(i, bar.ts, "DECISION", f"EXIT_CONFIRMED {signal.side.value}")
                            pending, pending_kind = signal, "exit"
                        continue
                    self.exit_coverage["accepted"] += 1
                    pending, pending_kind = signal, "exit"
                    continue

                state = position.state if position else PositionState.FLAT
                bars_held = position.bars_held if position else 0
                action, note = self.policy.decide(signal, state, bars_held)
                ledger.log(i, bar.ts, "DECISION", f"{action.value} {note}".strip())
                if action is DecisionAction.ACCEPT_ENTRY:
                    entry_allowed = True
                    cooldown = self.cfg.signal_policy.same_side_reentry_cooldown_bars
                    if (
                        cooldown > 0
                        and last_exit_side is not None
                        and last_exit_side is signal.side
                        and i - last_exit_bar <= cooldown
                    ):
                        entry_allowed = False
                        ledger.log(
                            i, bar.ts, "DECISION",
                            f"REJECT_REENTRY same-side {signal.side.value} "
                            f"{(i - last_exit_bar)}b < cooldown {cooldown}b",
                        )
                    if session_active:
                        ok_session, cutoff_note = self.session.can_enter(bar.ts, tf_minutes)
                        if not ok_session:
                            entry_allowed = False
                            ledger.log(i, bar.ts, "DECISION",
                                       f"REJECT_SESSION_CUTOFF {cutoff_note}")
                        if self.cfg.mode == "long" and signal.side is Side.SELL:
                            entry_allowed = False
                            ledger.log(i, bar.ts, "DECISION", "SKIP_ENTRY mode=long")
                    elif self.cfg.mode == "short" and signal.side is Side.BUY:
                        entry_allowed = False
                        ledger.log(i, bar.ts, "DECISION", "SKIP_ENTRY mode=short")
                    if signal.side is Side.SELL and not self.cfg.allow_short:
                        entry_allowed = False
                        ledger.log(i, bar.ts, "DECISION", "REJECT_SHORT short not allowed")
                    if entry_allowed:
                        n_conf = max(0, int(self.cfg.signal_policy.entry_confirm_bars))
                        if n_conf > 0:
                            if entry_confirm is None or entry_confirm["side"] != signal.side:
                                entry_confirm = {"signal": signal, "side": signal.side,
                                                 "confirm_needed": n_conf}
                                ledger.log(
                                    i, bar.ts, "DECISION",
                                    f"ENTRY_WAIT_CONFIRM {n_conf} следующих свечей в сторону {signal.side.value}",
                                )
                        else:
                            pending, pending_kind = signal, "entry"
                elif action is DecisionAction.ACCEPT_EXIT:
                    policy = self.cfg.signal_policy
                    if policy.opposite_hold:
                        # держим позицию: слабый противоположный сигнал не закрывает
                        exit_candidate = None
                        ledger.log(i, bar.ts, "DECISION", f"HOLD_NO_EXIT {signal.side.value} held")
                        continue
                    confirm = policy.exit_confirm_window_bars
                    if policy.confirm_flip and confirm <= 0:
                        # мгновенный переворот по противоположному сигналу
                        pending, pending_kind = signal, "flip"
                    elif confirm > 0:
                        if exit_candidate is None:
                            exit_candidate = {"bar": i, "side": signal.side}
                            ledger.log(
                                i, bar.ts, "DECISION",
                                f"EXIT_CANDIDATE {signal.side.value} waiting confirm within {confirm}b",
                            )
                        elif policy.confirm_flip:
                            exit_candidate = None
                            ledger.log(i, bar.ts, "DECISION", f"FLIP_CONFIRMED {signal.side.value}")
                            pending, pending_kind = signal, "flip"
                        else:
                            exit_candidate = None
                            ledger.log(i, bar.ts, "DECISION", f"EXIT_CONFIRMED {signal.side.value}")
                            pending, pending_kind = signal, "exit"
                    else:
                        pending, pending_kind = signal, "exit"
                if (
                    exit_candidate is not None
                    and i - exit_candidate["bar"] >= self.cfg.signal_policy.exit_confirm_window_bars
                ):
                    exit_candidate = None

        last = candles[-1]
        if position is not None:
            self._close(total - 1, last.ts, position, last.close, ExitReason.END_OF_DATA.value, ledger)

        return ledger

    def _open(        self,
        index: int,
        bar: Candle,
        candles: Sequence[Candle],
        signal: Signal,
        position: Position | None,
        ledger: TradeLedger,
    ) -> Position:
        self._trailing_active = False
        side = signal.side
        base = bar.open
        fill = self.cfg.cost_model.fill_price(base, side)
        notional = fill * self.cfg.qty
        commission = self.cfg.cost_model.commission(notional)
        slippage = abs(fill - base) * self.cfg.qty
        plan = self.exit_policy.plan_entry(side, fill, CandlePrefix(candles, index + 1))
        state = PositionState.LONG if side is Side.BUY else PositionState.SHORT
        ledger.log(index, bar.ts, "FILL_ENTRY", f"{side.value} qty={self.cfg.qty} price={fill}")
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
        )
        ledger.log(index, ts, "FILL_EXIT", f"trade={trade.trade_id} reason={reason} net={net:.4f}")
        return None
