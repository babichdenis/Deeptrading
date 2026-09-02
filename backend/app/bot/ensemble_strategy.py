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
    setups: list = field(default_factory=lambda: V2_SETUPS)


class EnsembleV4Strategy:
    """Адаптер Strategy: на закрытии 5м-бара гоняет compute_ensemble
    и возвращает Signal по последнему свежему входу ансамбля."""

    strategy_id = "ensemble_v4"
    version = "1.0.0"

    def __init__(self, params: EnsembleParams):
        self.p = params

    def warmup_bars(self) -> int:
        return 50

    def _is_main_session(self, dt_utc: datetime) -> bool:
        msk = dt_utc.astimezone(MSK)
        if msk.weekday() >= 5:
            return False
        t = msk.hour * 60 + msk.minute
        return 10 * 60 <= t <= 18 * 60 + 45

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if not candles:
            return None
        last = candles[-1]
        if last.ts.minute % 5 != 0:
            return None
        if self.p.session == "main" and not self._is_main_session(last.ts):
            return None
        if len(candles) < 50:
            return None
        try:
            from app.services.ensemble import compute_ensemble

            req = {
                "figi": self.p.figi,
                "bias_mode": "info",
                "bias": {"tf": "hour", "period": 50},
                "entry_tf": "5min",
                "entry": {"tf": "5min", "lookback": 1},
                "entry_session": self.p.session,
                "quorum": self.p.quorum,
                "same_side_reentry_cooldown_bars": 15,
                "carry_overnight": True,
                "opposite_hold": False,
                "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
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
            res = compute_ensemble(list(candles), req)
        except Exception:
            return None
        if "error" in res:
            return None
        entries = res.get("static", {}).get("entries", [])
        if not entries:
            return None
        cutoff = last.ts - timedelta(minutes=FRESH_MIN)
        fresh = [e for e in entries if e.get("ts", "") >= cutoff.isoformat()]
        if not fresh:
            return None
        last_e = fresh[-1]
        side = Side.BUY if last_e.get("side") == "BUY" else Side.SELL
        return Signal(
            strategy_id=self.strategy_id,
            side=side,
            time=last.ts,
            reason=f"ensemble_v4 {last_e.get('side')}",
            kind="entry",
        )
