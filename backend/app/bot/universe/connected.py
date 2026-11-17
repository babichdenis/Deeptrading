"""C1/C2 connected week runner: hourly Universe, per-bucket ensemble votes, shared long-only portfolio."""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.bot.paper_cashflow import charge_paper_entry_commission, settle_paper_exit
from app.bot.universe.domain import InstrumentRef
from app.bot.universe.features import compute_feature_set
from app.bot.universe.trend import compute_trend_features
from app.engine.costs import CostModel
from app.engine.models import Side
from app.services.signals import generate_signals

MIN5 = timedelta(minutes=5)
MIN15 = timedelta(minutes=15)
MIN60 = timedelta(minutes=60)
TF_BY_MIN = {5: MIN5, 15: MIN15, 60: MIN60}
WINDOW_BARS = 20
ATR_PERIOD = 14
MIN_BARS = {5: 120, 15: 20, 60: 20}
DATA_REASON_PREFIX = ("5m:", "15m:", "60m:")


def data_fault(cand) -> list:
    """Причины, означающие дефект входных данных бумаги (а не отбор по тренду)."""
    return [
        r for r in cand.reasons
        if r.startswith(DATA_REASON_PREFIX) or r == "invalid_features"
    ]


class Series:
    """Барный ряд figi с каузальным доступом «только закрытые бары»."""

    __slots__ = ("bars", "ts")

    def __init__(self, bars):
        self.bars = list(bars)
        self.ts = [b.ts for b in self.bars]

    def count_upto(self, limit: datetime) -> int:
        return bisect.bisect_right(self.ts, limit)

    def last(self, limit: datetime):
        n = self.count_upto(limit)
        return self.bars[n - 1] if n else None

    def tail(self, limit: datetime, n: int):
        end = self.count_upto(limit)
        return self.bars[max(0, end - n):end]


def flop(dt: datetime, minutes: int) -> datetime:
    secs = int(dt.timestamp())
    step = minutes * 60
    return datetime.fromtimestamp(secs - secs % step, tz=timezone.utc)


@dataclass
class Candidate:
    ticker: str
    figi: str
    lot: int
    reasons: list
    atr_pct: float | None = None
    atr: float | None = None
    atr_ts: datetime | None = None
    trend: dict = field(default_factory=dict)
    score: float | None = None

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "figi": self.figi,
            "lot": self.lot,
            "reasons": list(self.reasons),
            "atr_pct": self.atr_pct,
            "atr": self.atr,
            "atr_ts": self.atr_ts.isoformat() if self.atr_ts else None,
            "trend": self.trend,
            "score": self.score,
        }


def snapshots(pool, series, as_of: datetime, *, top_n: int = 8, side: Side = Side.BUY) -> dict:
    """Один каузальный снимок: признаки, допуск, ранг, top_n. Ничего не решает про сделки."""
    side = Side(side)
    direction = 1 if side is Side.BUY else -1
    rows = []
    for item in pool:
        figi = item["figi"]
        ref = InstrumentRef(ticker=item["ticker"], figi=figi)
        reasons = []
        counts = {}
        last_ts = {}
        for minutes in (5, 15, 60):
            tf = TF_BY_MIN[minutes]
            ser = series[minutes][figi]
            counts[str(minutes)] = ser.count_upto(as_of - tf)
            bar = ser.last(as_of - tf)
            last_ts[str(minutes)] = bar.ts.isoformat() if bar else None
            if counts[str(minutes)] < MIN_BARS[minutes]:
                reasons.append(f"{minutes}m:insufficient_history")
            elif bar is None or as_of - (bar.ts + tf) > tf:
                reasons.append(f"{minutes}m:stale")
        cand = Candidate(ticker=ref.ticker, figi=figi, lot=int(item.get("lot") or 1), reasons=reasons)
        cand.bar_counts = counts
        cand.last_closed = last_ts
        if not reasons:
            b5 = series[5][figi].tail(as_of - MIN5, WINDOW_BARS + 2)
            b15 = series[15][figi].tail(as_of - MIN15, WINDOW_BARS + 2)
            b60 = series[60][figi].tail(as_of - MIN60, WINDOW_BARS + 2)
            vol = compute_feature_set(ref, b5, as_of=as_of, window=WINDOW_BARS, period=ATR_PERIOD)
            t15 = compute_trend_features(
                ref, b15, as_of=as_of, window=WINDOW_BARS, atr_period=ATR_PERIOD, interval=MIN15
            )
            t60 = compute_trend_features(
                ref, b60, as_of=as_of, window=WINDOW_BARS, atr_period=ATR_PERIOD, interval=MIN60
            )
            cand.atr_pct = vol.atr_pct
            cand.atr = vol.atr
            cand.atr_ts = b5[-1].ts if b5 else None
            cand.trend = {
                "15": {"valid": t15.valid, "normalized_slope": t15.normalized_slope, "strength": t15.strength},
                "60": {"valid": t60.valid, "normalized_slope": t60.normalized_slope, "strength": t60.strength},
            }
            if not vol.valid or not t15.valid or not t60.valid:
                cand.reasons.append("invalid_features")
            elif direction * t15.normalized_slope <= 0 or direction * t60.normalized_slope <= 0:
                cand.reasons.append("trend_not_up_on_both_frames" if side is Side.BUY
                                    else "trend_not_down_on_both_frames")
            else:
                cand.score = vol.atr_pct * min(t15.strength, t60.strength)
        rows.append(cand)
    admitted = [c for c in rows if not c.reasons]
    admitted.sort(key=lambda c: (-c.score, c.figi))
    return {
        "as_of": as_of.isoformat(),
        "all": rows,
        "admitted": admitted,
        "selected": admitted[:top_n],
        "counts": len(rows),
        "admitted_count": len(admitted),
        "excluded": [c.as_dict() for c in rows if c.reasons],
    }


def signal_maps(series5_for_signals: dict, setups: list) -> dict:
    """vector-путь: однократная генерация сигналов сетапов по полному 5m-ряду."""
    maps = {}
    for figi, bars in series5_for_signals.items():
        per = {}
        for setup in setups:
            raw = generate_signals(setup["strategy_id"], setup["params"], bars, window=400)
            grid = {}
            for sig in raw:
                grid.setdefault(sig["ts"], set()).add(sig["side"])
            per[setup["strategy_id"]] = grid
        maps[figi] = per
    return maps


def votes_at(maps_figi: dict, ts: datetime, setups: list) -> tuple[int, int, list]:
    buy = sell = 0
    detail = []
    for setup in setups:
        sides = maps_figi.get(setup["strategy_id"], {}).get(ts) or set()
        if len(sides) > 1:
            raise ValueError(f"conflicting votes at {ts.isoformat()}: {sorted(sides)}")
        side = next(iter(sides), "NONE")
        if side == "BUY":
            buy += 1
        elif side == "SELL":
            sell += 1
        detail.append({"setup": setup["strategy_id"], "side": side})
    return buy, sell, detail


def increment_trend(series, figi, tf_minutes: int, boundary: datetime, ref) -> dict:
    tf = TF_BY_MIN[tf_minutes]
    bars = series[figi].tail(boundary - tf, WINDOW_BARS + 2)
    t = compute_trend_features(
        ref, bars, as_of=boundary, window=WINDOW_BARS, atr_period=ATR_PERIOD, interval=tf
    )
    return {"valid": t.valid, "normalized_slope": t.normalized_slope, "strength": t.strength}


@dataclass
class Position:
    figi: str
    ticker: str
    lot: int
    qty: int
    entry_ts: datetime
    entry_signal_ts: datetime
    entry_raw: float
    entry_fill: float
    entry_commission: float
    atr: float
    stop: float
    target: float
    rank: int
    path_uncertainty: bool = False


@dataclass
class Portfolio:
    arm_id: str
    cash: float
    cost: CostModel
    max_positions: int = 4
    entry_budget: float = 4000.0
    positions: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    events: list = field(default_factory=list)
    equity: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    invested: float = 0.0
    paper_rounding: bool = False
    stop_risk_budget: float | None = None

    def _entry_fee(self, notional: float) -> float:
        fee = notional * self.cost.commission_rate
        return charge_paper_entry_commission(fee) if self.paper_rounding else fee

    def record(self, ts: datetime, kind: str, **kw):
        row = {"ts": ts.isoformat(), "kind": kind}
        row.update(kw)
        self.events.append(row)

    def has(self, figi: str) -> bool:
        return figi in self.positions

    def open_position(self, cand, open_raw, ts, signal_ts, atr, rank):
        if cand.figi in self.positions:
            self.rejected.append({
                "ts": ts.isoformat(), "figi": cand.figi, "ticker": cand.ticker,
                "reason": "already_held",
            })
            return None
        lot = getattr(cand, "lot", None)
        if not isinstance(lot, int) or isinstance(lot, bool) or lot <= 0:
            self.rejected.append({
                "ts": ts.isoformat(), "figi": cand.figi, "ticker": cand.ticker,
                "reason": "invalid_lot", "lot": repr(lot),
            })
            return None
        fill = self.cost.fill_price(open_raw, Side.BUY)
        budget = min(self.entry_budget, self.cash)
        lots = int(budget / (fill * lot * (1.0 + self.cost.commission_rate)))
        if lots > 0 and self.stop_risk_budget is not None:
            stop = open_raw - 4.0 * atr
            if not math.isfinite(stop) or not 0 < stop < fill:
                raise ValueError("Risk sizing requires a valid protective stop")
            stop_fill = self.cost.fill_price(stop, Side.SELL)
            risk_per_lot = lot * (fill - stop_fill)
            risk_per_lot += self.cost.commission(lot * fill) + self.cost.commission(lot * stop_fill)
            lots = min(lots, math.floor(self.stop_risk_budget / risk_per_lot))
            if lots == 0:
                self.rejected.append({"ts": ts.isoformat(), "figi": cand.figi,
                                      "ticker": cand.ticker, "reason": "stop_risk_no_lot"})
                return None
        if lots <= 0:
            self.rejected.append({
                "ts": ts.isoformat(), "figi": cand.figi, "ticker": cand.ticker,
                "reason": "zero_lots", "budget": budget, "fill": fill, "lot": lot,
            })
            return None
        qty = lots * lot
        fill6 = round(fill, 6) if self.paper_rounding else fill
        notional = qty * fill6
        commission = self._entry_fee(notional)
        if notional + commission > budget + 1e-9:
            self.rejected.append({
                "ts": ts.isoformat(), "figi": cand.figi, "ticker": cand.ticker,
                "reason": "entry_budget_exceeded", "need": notional + commission, "budget": budget,
            })
            return None
        if notional + commission > self.cash + 1e-9:
            self.rejected.append({
                "ts": ts.isoformat(), "figi": cand.figi, "ticker": cand.ticker,
                "reason": "insufficient_cash", "need": notional + commission, "cash": self.cash,
            })
            return None
        if len(self.positions) >= self.max_positions:
            self.rejected.append({
                "ts": ts.isoformat(), "figi": cand.figi, "ticker": cand.ticker,
                "reason": "max_positions", "open": len(self.positions),
            })
            return None
        self.cash -= notional + commission
        self.invested += notional + commission
        self.positions[cand.figi] = Position(
            figi=cand.figi, ticker=cand.ticker, lot=lot, qty=qty,
            entry_ts=ts, entry_signal_ts=signal_ts, entry_raw=open_raw, entry_fill=fill6,
            entry_commission=commission, atr=atr,
            stop=open_raw - 4.0 * atr, target=open_raw + 8.0 * atr, rank=rank,
        )
        self.record(ts, "ENTRY", figi=cand.figi, ticker=cand.ticker, qty=qty,
                    raw=open_raw, fill=fill6, intended_fill=fill, commission=commission,
                    stop=self.positions[cand.figi].stop,
                    target=self.positions[cand.figi].target, atr=atr, rank=rank,
                    signal_ts=signal_ts.isoformat(), cash=self.cash)
        return self.positions[cand.figi]

    def replay_entry(self, figi: str, ticker: str, qty: int, fill: float, ts: datetime):
        if figi in self.positions:
            return None
        fill6 = round(fill, 6) if self.paper_rounding else fill
        notional = qty * fill6
        commission = self._entry_fee(notional)
        self.cash -= notional + commission
        self.invested += notional + commission
        self.positions[figi] = Position(
            figi=figi, ticker=ticker, lot=qty, qty=qty, entry_ts=ts, entry_signal_ts=ts,
            entry_raw=fill, entry_fill=fill6, entry_commission=commission,
            atr=0.0, stop=0.0, target=0.0, rank=0,
        )
        return self.positions[figi]

    def replay_exit(self, figi: str, fill: float, ts: datetime, reason: str = "REPLAY"):
        return self.close_position(self.positions[figi], fill, ts, reason, filled=fill)

    def close_position(self, pos: Position, raw, ts, reason: str, filled=None):
        fill = float(filled) if filled is not None else self.cost.fill_price(raw, Side.SELL)
        notional = pos.qty * fill
        exit_commission = notional * self.cost.commission_rate
        raw_gross = (raw - pos.entry_raw) * pos.qty
        fill_gross = (fill - pos.entry_fill) * pos.qty
        slippage = raw_gross - fill_gross
        fees = pos.entry_commission + exit_commission
        exact_net = fill_gross - fees
        identity_gap = (raw_gross - slippage - fees) - exact_net
        cashflow = settle_paper_exit(fill_gross, exit_commission, pos.entry_commission)
        credit = cashflow.cash_credit
        settled_net = cashflow.total_net
        total_commission = cashflow.total_commission
        if self.paper_rounding:
            self.cash += pos.entry_fill * pos.qty + credit
        else:
            self.cash += notional - exit_commission
        self.invested -= pos.entry_fill * pos.qty + pos.entry_commission
        del self.positions[pos.figi]
        trade = {
            "figi": pos.figi, "ticker": pos.ticker, "qty": pos.qty,
            "entry_ts": pos.entry_ts.isoformat(), "exit_ts": ts.isoformat(),
            "entry_signal_ts": pos.entry_signal_ts.isoformat(),
            "entry_raw": pos.entry_raw, "entry_fill": pos.entry_fill,
            "exit_raw": raw, "exit_fill": fill,
            "raw_gross": raw_gross, "fill_gross": fill_gross, "slippage": slippage,
            "entry_commission": pos.entry_commission, "exit_commission": exit_commission,
            "fees": fees, "net": exact_net, "net_settled": settled_net,
            "cash_credit_settled": credit, "total_commission": total_commission,
            "identity_gap": identity_gap,
            "reason": reason, "stop": pos.stop, "target": pos.target, "atr": pos.atr,
            "rank": pos.rank, "cash_after": self.cash,
        }
        self.trades.append(trade)
        self.record(ts, "EXIT", figi=pos.figi, ticker=pos.ticker, qty=pos.qty,
                    raw=raw, fill=fill, net=exact_net, reason=reason, cash=self.cash)
        return trade

    def mark(self, ts, marks: dict, max_age: timedelta = timedelta(minutes=15)):
        """Оценка портфеля на закрытии. Отсутствующая/устаревшая метка делает equity неизвестным."""
        known_value = 0.0
        missing, carried = [], []
        for figi, pos in self.positions.items():
            got = marks.get(figi)
            if got is None:
                missing.append(figi)
                pos.path_uncertainty = True
                continue
            price, observed_at = got
            age = ts - observed_at
            if age < timedelta(0) or age > max_age:
                missing.append(figi)
                pos.path_uncertainty = True
                continue
            if age > timedelta(0):
                carried.append({"figi": figi, "age_sec": age.total_seconds()})
            known_value += pos.qty * price
        self.equity.append({
            "ts": (ts + timedelta(minutes=1)).isoformat(),
            "bar_ts": ts.isoformat(),
            "cash": self.cash,
            "known_positions_value": known_value,
            "equity": None if missing else self.cash + known_value,
            "known_equity_partial": self.cash + known_value,
            "missing_figis": missing,
            "carried_marks": carried,
            "unknown_figis": [{"figi": f, "ticker": self.positions[f].ticker,
                               "qty": self.positions[f].qty} for f in missing],
            "open_positions": len(self.positions),
        })
        return None if missing else self.cash + known_value
