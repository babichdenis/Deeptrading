#!/usr/bin/env python3
"""RESEARCH-CORE-V2: портфельный и хронологический оценщик (B3).

Строит дневной портфельный ledger из trades (BATCH15) или events (B2 event-study).
Разделяет четыре вопроса: направление, влияние выхода, влияние costs, переносимость.

Ключевые инварианты (фикс архитектора):
- начальный капитал нормирован 1; gross exposure <= 1, без leverage;
- равные веса между сигналами ТЕКУЩЕГО candidate в день; невошедшая доля = cash;
- разные candidates — отдельные альтернативные портфели, НЕ 15 позиций на один капитал;
- два costs — отдельные портфели;
- fractional units — явное исследовательское упрощение; short borrow/availability
  UNKNOWN, не live-ready;
- если открытая позиция имеет UNKNOWN обязательный выход → дневной portfolio return
  UNKNOWN, cumulative equity/DD после этого НЕ выдумываются;
- known-only диагностика — отдельно, с явной маркировкой;
- drawdown только по определённой кривой;
- ценовой PnL разных акций НЕ складывается в один «доход» — считается портфельный
  return на нормированном капитале (это допустимо: return — безразмерный, в отличие
  от абсолютного ценового PnL).

Walk-forward: train >= 2 месяцев, следующий месяц — evaluation; selector фиксирован
(наибольший средний train day net при дорогом cost; <=0 или нет вариантов → CASH).

Arms A/B/C (ждут regime-фичи B2): интерфейс фильтра eligible-тикеров на день.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

TRADE_FIELDS = ("ticker", "candidate_id", "day", "side", "spec_hash",
                "decision_label", "intended_entry_time", "entry_ts", "entry_base",
                "exit_ts", "exit_base", "gross", "costs_total", "net",
                "status", "incomplete_reason", "event_id")

COST_IDS = ("c5_s2", "c10_s5")
TICKERS = ("GAZP", "LKOH", "SFIN", "VKCO")


# ------------------------------------------------------------------ loading

@dataclass
class Trade:
    ticker: str
    candidate_id: str
    day: date
    side: str  # long | short
    entry_ts: datetime
    exit_ts: datetime | None
    entry_base: float
    net_per_unit: float | None  # net / entry_base; None если UNKNOWN
    gross_per_unit: float | None
    costs_per_unit: float | None
    status: str  # FILLED | INCOMPLETE
    incomplete_reason: str
    event_id: str

    @property
    def is_known(self) -> bool:
        return self.status == "FILLED" and self.net_per_unit is not None


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def load_trades_csv(path: Path) -> list[Trade]:
    out: list[Trade] = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            entry_base = float(r["entry_base"]) if r["entry_base"] else None
            net = float(r["net"]) if r["net"] else None
            gross = float(r["gross"]) if r["gross"] else None
            costs = float(r["costs_total"]) if r["costs_total"] else None
            out.append(Trade(
                ticker=r["ticker"], candidate_id=r["candidate_id"],
                day=date.fromisoformat(r["day"]), side=r["side"],
                entry_ts=_parse_ts(r["entry_ts"]),
                exit_ts=_parse_ts(r["exit_ts"]),
                entry_base=entry_base or 0.0,
                net_per_unit=(net / entry_base) if (net is not None and entry_base) else None,
                gross_per_unit=(gross / entry_base) if (gross is not None and entry_base) else None,
                costs_per_unit=(costs / entry_base) if (costs is not None and entry_base) else None,
                status=r["status"], incomplete_reason=r.get("incomplete_reason", ""),
                event_id=r.get("event_id", "")))
    return out


def discover_batch15_cells(root: Path) -> dict[tuple[str, str, str], Path]:
    """-> {(candidate, ticker, cost): trades.csv path}.

    Ищет ячейки и на уровне bN/<run>/, и в подкаталогах bN/<run>/signals_a/
    (расширенный snapshot B1).
    """
    out: dict[tuple[str, str, str], Path] = {}
    for gd in ("b1", "b2", "b3"):
        for run_dir in sorted(root.glob(f"{gd}/[0-9]*")):
            for cell in sorted(run_dir.glob("[A-Z]*__*__*")):
                parts = cell.name.split("__")
                if len(parts) != 3:
                    continue
                trades = cell / "trades.csv"
                if trades.exists():
                    out[(parts[0], parts[1], parts[2])] = trades
            for sub in ("signals_a", "signals_b", "signals_c"):
                sub_dir = run_dir / sub
                if not sub_dir.is_dir():
                    continue
                for cell in sorted(sub_dir.glob("[A-Z]*__*__*")):
                    parts = cell.name.split("__")
                    if len(parts) != 3:
                        continue
                    trades = cell / "trades.csv"
                    if trades.exists():
                        out[(parts[0], parts[1], parts[2])] = trades
    return out


def _max_drawdown(curve: list[float], initial_capital: float) -> float | None:
    if not curve:
        return None
    peak = initial_capital
    max_dd = 0.0
    for eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    return max_dd


# ------------------------------------------------------------------ portfolio

@dataclass
class DaySlot:
    """Одна позиция кандидата в день: доля капитала, направление, исход."""
    ticker: str
    side: str
    weight: float  # доля начального equity дня (0..1), сумма <= 1
    known: bool
    return_per_unit: float | None  # net/entry_base; None если UNKNOWN


@dataclass
class DayLedger:
    day: date
    start_equity: float
    slots: list[DaySlot] = field(default_factory=list)
    cash_weight: float = 1.0
    has_unknown: bool = False
    return_daily: float | None = None  # None если has_unknown
    known_return_daily: float | None = None  # known-only диагностика
    capital_usage: float = 0.0

    def finalize(self) -> None:
        used = sum(s.weight for s in self.slots)
        self.capital_usage = used
        self.cash_weight = 1.0 - used
        self.has_unknown = any(not s.known for s in self.slots)
        if self.has_unknown:
            self.return_daily = None
        else:
            self.return_daily = sum(s.weight * (s.return_per_unit or 0.0)
                                    for s in self.slots)
        known = [s for s in self.slots if s.known]
        self.known_return_daily = sum(
            s.weight * (s.return_per_unit or 0.0) for s in known)


@dataclass
class PortfolioLedger:
    candidate_id: str
    cost_id: str
    days: list[DayLedger] = field(default_factory=list)
    initial_capital: float = 1.0

    def equity_curve(self) -> list[tuple[date, float | None]]:
        """СТРОГАЯ накопленная equity; None после первого UNKNOWN-дня.

        Не выдумывается: после дня с неизвестным исходом кривая обрывается.
        """
        out: list[tuple[date, float | None]] = []
        eq = self.initial_capital
        poisoned = False
        for d in self.days:
            if poisoned or d.return_daily is None:
                poisoned = True
                out.append((d.day, None))
                continue
            eq *= (1.0 + d.return_daily)
            out.append((d.day, eq))
        return out

    def known_only_equity_curve(self) -> list[tuple[date, float]]:
        """ИЗВЕСТНО-ONLY кривая: UNKNOWN-дни трактуются как cash (0 return).

        ЯВНАЯ ДИАГНОСТИКА (не основная): не заменяет строгую кривую и не
        утверждает, что equity после UNKNOWN известна. Полезна только чтобы
        увидеть поведение на определённой части.
        """
        out: list[tuple[date, float]] = []
        eq = self.initial_capital
        for d in self.days:
            eq *= (1.0 + (d.return_daily or 0.0))
            out.append((d.day, eq))
        return out

    def defined_drawdown(self) -> float | None:
        """Просадка СТРОГОЙ кривой (только определённая часть)."""
        curve = [eq for _, eq in self.equity_curve() if eq is not None]
        return _max_drawdown(curve, self.initial_capital)

    def known_only_drawdown(self) -> float | None:
        """Просадка known-only кривой (ЯВНАЯ диагностика, не основная)."""
        curve = [eq for _, eq in self.known_only_equity_curve()]
        return _max_drawdown(curve, self.initial_capital)


def build_portfolio(trades: list[Trade], candidate_id: str, cost_id: str,
                    initial_capital: float = 1.0,
                    eval_days: list[date] | None = None) -> PortfolioLedger:
    """Дневной ledger для одного candidate при одном cost.

    Равные веса между сигналами дня; невошедшая доля — cash. Если у позиции
    неизвестный исход (INCOMPLETE) — день помечается UNKNOWN, return_daily=None.
    Дни без сигналов включаются (eval_days), чтобы была видна доля cash.
    """
    by_day: dict[date, list[Trade]] = {}
    for t in trades:
        if t.candidate_id == candidate_id:
            by_day.setdefault(t.day, []).append(t)

    days_to_emit = sorted(set(eval_days) | set(by_day)) if eval_days else sorted(by_day)

    ledger = PortfolioLedger(candidate_id=candidate_id, cost_id=cost_id,
                             initial_capital=initial_capital)
    for day in days_to_emit:
        day_trades = by_day.get(day, [])
        n = len(day_trades)
        weight = 1.0 / n if n else 0.0
        slots = [DaySlot(
            ticker=t.ticker, side=t.side, weight=weight, known=t.is_known,
            return_per_unit=t.net_per_unit) for t in day_trades]
        dl = DayLedger(day=day, start_equity=initial_capital, slots=slots)
        dl.finalize()
        ledger.days.append(dl)
    return ledger


# ------------------------------------------------------------------ metrics

@dataclass
class PortfolioMetrics:
    candidate_id: str
    cost_id: str
    days_total: int
    days_known: int
    days_unknown: int
    known_days_return_mean: float | None
    known_days_return_sum: float | None
    defined_curve_points: int
    strict_final_equity: float | None  # None если кривая обрана UNKNOWN-ом
    strict_max_drawdown: float | None  # только определённая часть
    known_only_final_equity: float | None  # ЯВНАЯ диагностика (не основная)
    known_only_max_drawdown: float | None  # ЯВНАЯ диагностика
    known_only_label: str
    capital_usage_mean: float | None
    cash_weight_mean: float | None
    unknown_reasons: list[str] = field(default_factory=list)


def portfolio_metrics(ledger: PortfolioLedger) -> PortfolioMetrics:
    known_days = [d for d in ledger.days if d.return_daily is not None]
    unknown_days = [d for d in ledger.days if d.return_daily is None]
    returns = [d.return_daily for d in known_days]
    curve = ledger.equity_curve()
    defined_pts = sum(1 for _, eq in curve if eq is not None)
    strict_final = None
    for _, eq in reversed(curve):
        if eq is not None:
            strict_final = eq
            break
    ko_curve = ledger.known_only_equity_curve()
    ko_final = ko_curve[-1][1] if ko_curve else None
    usage = [d.capital_usage for d in ledger.days]
    reasons: list[str] = []
    for d in unknown_days:
        for s in d.slots:
            if not s.known and s.ticker not in reasons:
                reasons.append(s.ticker)
    return PortfolioMetrics(
        candidate_id=ledger.candidate_id, cost_id=ledger.cost_id,
        days_total=len(ledger.days), days_known=len(known_days),
        days_unknown=len(unknown_days),
        known_days_return_mean=(statistics.mean(returns) if returns else None),
        known_days_return_sum=(sum(returns) if returns else None),
        defined_curve_points=defined_pts,
        strict_final_equity=strict_final,
        strict_max_drawdown=ledger.defined_drawdown(),
        known_only_final_equity=ko_final,
        known_only_max_drawdown=ledger.known_only_drawdown(),
        known_only_label=("known-only: UNKNOWN-дни=cash; НЕ основная кривая, "
                          "не утверждает что equity после UNKNOWN известна"),
        capital_usage_mean=(statistics.mean(usage) if usage else None),
        cash_weight_mean=(1.0 - statistics.mean(usage)) if usage else None,
        unknown_reasons=reasons[:10])


# ------------------------------------------------------------------ overnight

@dataclass
class OvernightSplit:
    """Разложение дневного return на intraday (open→close) и overnight (close→next_open).

    Ключевое: intraday-стратегия с входом на open и выходом на close может
    заработать ТОЛЬКО open-to-close часть. Overnight (гэп) — недостижима такими
    входами и не должна выдаваться за достижимую доходность intraday.
    """
    ticker: str
    days: int
    intraday_mean: float | None  # средний (close-open)/open
    overnight_mean: float | None  # средний (next_open-close)/close
    total_mean: float | None  # средний (next_open-open)/open
    intraday_positive_share: float | None
    overnight_positive_share: float | None
    overnight_dominates_share: float | None  # доля дней, где |overnight|>|intraday|
    partial_days_excluded: int
    note: str


def overnight_decomposition(rows: list[dict],
                            ticker: str) -> OvernightSplit:
    """Считает intraday vs overnight split из daily-строк.

    rows: словари с ts/open/close (+ опционально next_open, partial). next_open
    вычисляется как open следующей строки с известным open (не partial). Строки
    с partial исключаются из расчёта (partial daily помечается, не выдаётся за
    полный).
    """
    valid = [r for r in rows if not r.get("partial")]
    partial_n = len(rows) - len(valid)
    intraday: list[float] = []
    overnight: list[float] = []
    total: list[float] = []
    for i, r in enumerate(valid):
        try:
            o = float(r["open"]); c = float(r["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if o <= 0 or c <= 0:
            continue
        intraday.append((c - o) / o)
        nxt = r.get("next_open")
        if nxt is None:
            for r2 in valid[i + 1:]:
                if not r2.get("partial") and float(r2.get("open") or 0) > 0:
                    nxt = float(r2["open"]); break
        if nxt is not None and nxt > 0:
            overnight.append((nxt - c) / c)
            total.append((nxt - o) / o)
    n_int = len(intraday)
    n_ovn = len(overnight)
    return OvernightSplit(
        ticker=ticker, days=n_int,
        intraday_mean=(sum(intraday) / n_int) if n_int else None,
        overnight_mean=(sum(overnight) / n_ovn) if n_ovn else None,
        total_mean=(sum(total) / len(total)) if total else None,
        intraday_positive_share=(sum(1 for x in intraday if x > 0) / n_int
                                 if n_int else None),
        overnight_positive_share=(sum(1 for x in overnight if x > 0) / n_ovn
                                  if n_ovn else None),
        overnight_dominates_share=(
            sum(1 for a, b in zip(intraday, overnight) if abs(b) > abs(a))
            / n_ovn if n_ovn else None),
        partial_days_excluded=partial_n,
        note=("intraday=open→close (достижимо intraday-входом на open); "
              "overnight=close→next_open (гэп, недостижим такими входами); "
              "total=open→next_open. Partial-дни исключены."))


def signal_free_long_baseline(
    daily_by_ticker: dict[str, list[dict]], cost_per_side: float,
    initial_capital: float = 1.0,
) -> PortfolioLedger:
    """SIGNAL-FREE long-only equal-weight universe baseline.

    Вход на open ВСЕХ доступных тикеров дня, выход на close, равные веса,
    те же costs на сторону. Никакой логики сигналов — изолирует «что добавляет
    сигнал против просто long рынка». Partial-дни исключены. Тикеры с открытым
    в этот день — в портфеле; невошедшая доля (мало тикеров) — cash.
    """
    days_map: dict[date, list[tuple[str, float]]] = {}
    for ticker, rows in daily_by_ticker.items():
        for r in rows:
            if r.get("partial"):
                continue
            try:
                d = date.fromisoformat(r["ts"][:10])
                o = float(r["open"]); c = float(r["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if o <= 0 or c <= 0:
                continue
            ret = (c - o) / o - 2.0 * cost_per_side
            days_map.setdefault(d, []).append((ticker, ret))

    ledger = PortfolioLedger(candidate_id="SIGNAL_FREE_LONG", cost_id="baseline",
                             initial_capital=initial_capital)
    for day in sorted(days_map):
        entries = days_map[day]
        n = len(entries)
        weight = 1.0 / n if n else 0.0
        slots = [DaySlot(ticker=tk, side="long", weight=weight, known=True,
                         return_per_unit=ret) for tk, ret in entries]
        dl = DayLedger(day=day, start_equity=initial_capital, slots=slots)
        dl.finalize()
        ledger.days.append(dl)
    return ledger


# ------------------------------------------------------------------ trade quality

@dataclass
class TradeQuality:
    """Качество сделок кандидата×cost (архитектор просил в итогах).

    Каждая метрика со знаменателем и явным счётчиком unknown (INCOMPLETE без
    числового net). cost break-even = средний round-trip cost на единицу entry
    notional (сколько нужно gross, чтобы покрыть расходы).
    """
    candidate_id: str
    cost_id: str
    filled_total: int  # знаменатель win rate / avg
    incomplete_unknown: int  # исход неизвестен, не входит в знаменатель
    win_rate: float | None  # доля положительных net среди FILLED
    avg_win: float | None  # средний net/entry_base среди прибыльных FILLED
    avg_loss: float | None  # средний net/entry_base среди убыточных FILLED (отриц.)
    gross_expectancy: float | None  # средний gross/entry_base среди FILLED
    net_expectancy: float | None  # средний net/entry_base среди FILLED
    cost_drag: float | None  # средний costs/entry_base среди FILLED
    cost_break_even: float | None  # gross/тр, требуемый для покрытия cost
    payoff_ratio: float | None  # |avg_win| / |avg_loss|
    breakeven_win_rate: float | None  # win rate при котором expectancy=0
    denominator_note: str


def trade_quality(trades: list[Trade], candidate_id: str,
                  cost_id: str) -> TradeQuality:
    cand = [t for t in trades if t.candidate_id == candidate_id]
    filled = [t for t in cand if t.is_known]
    unknown = [t for t in cand if not t.is_known]
    n = len(filled)
    nets = [t.net_per_unit for t in filled if t.net_per_unit is not None]
    grosses = [t.gross_per_unit for t in filled if t.gross_per_unit is not None]
    costs = [t.costs_per_unit for t in filled if t.costs_per_unit is not None]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]

    win_rate = (len(wins) / n) if n else None
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    gross_exp = (sum(grosses) / len(grosses)) if grosses else None
    net_exp = (sum(nets) / len(nets)) if nets else None
    cost_drag = (sum(costs) / len(costs)) if costs else None
    payoff = (abs(avg_win) / abs(avg_loss)
              if (avg_win is not None and avg_loss is not None and avg_loss != 0)
              else None)
    be_wr = (1.0 / (1.0 + payoff)
             if (payoff is not None and payoff > 0) else None)
    return TradeQuality(
        candidate_id=candidate_id, cost_id=cost_id,
        filled_total=n, incomplete_unknown=len(unknown),
        win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss,
        gross_expectancy=gross_exp, net_expectancy=net_exp,
        cost_drag=cost_drag, cost_break_even=cost_drag,
        payoff_ratio=payoff, breakeven_win_rate=be_wr,
        denominator_note=(f"win_rate/avg берутся среди {n} FILLED; "
                          f"{len(unknown)} INCOMPLETE — исход неизвестен, "
                          f"в знаменатель не входит (не ноль)."))


# ------------------------------------------------------------------ walk-forward

@dataclass
class WalkForwardResult:
    eval_month: str
    chosen: str  # candidate_id или "CASH"
    train_mean_net: float | None
    reason: str
    eval_return: float | None = None  # факт(eval месяц) для выбранного; None=UNKNOWN
    eval_is_unknown: bool = False


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def walk_forward_select(ledgers: dict[tuple[str, str], PortfolioLedger],
                        cost_id: str, min_train_months: int = 2) -> list[WalkForwardResult]:
    """Хронологический выбор кандидата без lookahead.

    Среди candidates с полными train portfolio days выбирается наибольший средний
    train day net при дорогом cost; если <= 0 или нет допустимых → CASH.
    """
    cand_months: dict[str, list[str]] = {}
    for (cid, cost), ledger in ledgers.items():
        if cost != cost_id:
            continue
        months = sorted({_month_key(d.day) for d in ledger.days})
        cand_months[cid] = months

    all_months = sorted({m for ms in cand_months.values() for m in ms})
    results: list[WalkForwardResult] = []

    for i, eval_month in enumerate(all_months):
        if i < min_train_months:
            results.append(WalkForwardResult(eval_month, "CASH", None,
                                             "insufficient_train"))
            continue
        train_months = set(all_months[:i])
        best_cid = None
        best_mean = None
        for cid, months in cand_months.items():
            if not train_months.issubset(set(months)):
                continue
            ledger = ledgers.get((cid, cost_id))
            if ledger is None:
                continue
            train_returns = [d.return_daily for d in ledger.days
                             if _month_key(d.day) in train_months
                             and d.return_daily is not None]
            if not train_returns:
                continue
            mean_net = statistics.mean(train_returns)
            if best_mean is None or mean_net > best_mean:
                best_mean = mean_net
                best_cid = cid
        if best_cid is not None and best_mean is not None and best_mean > 0:
            ledger = ledgers.get((best_cid, cost_id))
            eval_ret = None
            eval_unknown = True
            if ledger is not None:
                eval_rets = [d.return_daily for d in ledger.days
                             if _month_key(d.day) == eval_month]
                if eval_rets and all(r is not None for r in eval_rets):
                    eval_ret = statistics.mean(eval_rets)
                    eval_unknown = False
            results.append(WalkForwardResult(eval_month, best_cid, best_mean,
                                             "selected", eval_ret, eval_unknown))
        else:
            results.append(WalkForwardResult(eval_month, "CASH", best_mean,
                                             "no_positive_candidate", 0.0, False))
    return results


# ------------------------------------------------------------------ baselines

def baseline_cash(days: list[date], initial_capital: float = 1.0) -> PortfolioLedger:
    ledger = PortfolioLedger(candidate_id="BASELINE_CASH", cost_id="c5_s2",
                             initial_capital=initial_capital)
    for day in days:
        dl = DayLedger(day=day, start_equity=initial_capital, slots=[])
        dl.finalize()
        ledger.days.append(dl)
    return ledger


def baseline_long_equal_weight(trades: list[Trade], cost_id: str,
                               initial_capital: float = 1.0) -> PortfolioLedger:
    """LONG-SIDE SUBSET кандидатов (НЕ signal-free baseline!).

    Это все long-сигналы всех кандидатов, равные веса. Позиции УЖЕ отфильтрованы
    логикой сигналов, поэтому это НЕ изолирует «что добавляет сигнал против
    просто long рынка». Настоящий signal-free universe baseline (вход на все
    тикеры по расписанию, без логики сигнала) требует raw цен/расписания из
    snapshot B1 — строится при DATA_READY. Здесь — только ориентир на long-части.
    """
    long_trades = [t for t in trades if t.side == "long"]
    ledger = PortfolioLedger(candidate_id="LONG_SIDE_SUBSET", cost_id=cost_id,
                             initial_capital=initial_capital)
    by_day: dict[date, list[Trade]] = {}
    for t in long_trades:
        by_day.setdefault(t.day, []).append(t)
    for day in sorted(by_day):
        day_trades = by_day[day]
        n = len(day_trades)
        weight = 1.0 / n if n else 0.0
        slots = [DaySlot(ticker=t.ticker, side=t.side, weight=weight,
                         known=t.is_known, return_per_unit=t.net_per_unit)
                 for t in day_trades]
        dl = DayLedger(day=day, start_equity=initial_capital, slots=slots)
        dl.finalize()
        ledger.days.append(dl)
    return ledger


# ------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RESEARCH-CORE-V2 portfolio evaluator (B3)")
    ap.add_argument("--batch15-root", required=True)
    ap.add_argument("--daily-root", default=None,
                    help="каталог с <TICKER>__m24.csv (db daily B1) для signal-free baseline")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    root = Path(args.batch15_root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cells = discover_batch15_cells(root)
    # строго по (candidate, cost): два сценария costs — отдельные портфели
    trades_by_key: dict[tuple[str, str], list[Trade]] = {}
    all_days: set[date] = set()
    for (cid, ticker, cost), path in cells.items():
        loaded = load_trades_csv(path)
        trades_by_key.setdefault((cid, cost), []).extend(loaded)
        all_days.update(t.day for t in loaded)

    universe_days = sorted(all_days)
    all_results = []
    ledgers: dict[tuple[str, str], PortfolioLedger] = {}
    for cid in sorted({k[0] for k in trades_by_key}):
        for cost in COST_IDS:
            cand_cost_trades = trades_by_key.get((cid, cost), [])
            if not cand_cost_trades:
                continue
            ledger = build_portfolio(cand_cost_trades, cid, cost,
                                     eval_days=universe_days)
            ledgers[(cid, cost)] = ledger
            m = portfolio_metrics(ledger)
            all_results.append(m)

    # walk-forward selector (по каждому cost отдельно; дорогой cost = c10_s5)
    wf_all = []
    for cost in COST_IDS:
        wf = walk_forward_select(ledgers, cost, min_train_months=2)
        wf_all.extend(w.__dict__ for w in wf)

    # качество сделок (win rate, expectancy, cost drag, break-even)
    tq_all = []
    for cid in sorted({k[0] for k in trades_by_key}):
        for cost in COST_IDS:
            if (cid, cost) in trades_by_key:
                tq_all.append(trade_quality(trades_by_key[(cid, cost)], cid,
                                            cost).__dict__)

    # baselines на всём объединённом universe (все кандидаты, dedupe по event_id)
    all_trades: list[Trade] = []
    seen_events: set[str] = set()
    for key_trades in trades_by_key.values():
        for t in key_trades:
            if t.event_id and t.event_id in seen_events:
                continue
            if t.event_id:
                seen_events.add(t.event_id)
            all_trades.append(t)
    baseline_cash_ledger = baseline_cash(universe_days)
    baseline_long_ledger = baseline_long_equal_weight(all_trades, "c5_s2")

    # signal-free long baseline из daily B1 (m24), если доступен
    sf_baseline = {"status": "PENDING_NO_DAILY",
                   "note": ("long-only equal-weight, вход open/выход close, все "
                            "тиickers дня, без логики сигналов, те же costs; "
                            "требует --daily-root с m24 CSV")}
    daily_root = Path(args.daily_root) if args.daily_root else None
    if daily_root and daily_root.is_dir():
        daily_by_ticker: dict[str, list[dict]] = {}
        for f in sorted(daily_root.glob("*__m24.csv")):
            tk = f.name.split("__")[0]
            with f.open(encoding="utf-8") as fh:
                daily_by_ticker[tk] = list(csv.DictReader(fh))
        if daily_by_ticker:
            sf = signal_free_long_baseline(daily_by_ticker, cost_per_side=0.0007)
            sf_m = portfolio_metrics(sf)
            sf_baseline = {"status": "OK", "metrics": sf_m.__dict__,
                           "tickers": len(daily_by_ticker),
                           "note": ("long-only equal-weight, open→close, все "
                                    "тиickers дня, cost 5bps/сторону, без сигналов")}

    baselines = {
        "cash": portfolio_metrics(baseline_cash_ledger).__dict__,
        "long_side_subset_not_signal_free": portfolio_metrics(
            baseline_long_ledger).__dict__,
        "signal_free_long_universe": sf_baseline,
    }

    result = {
        "generated_utc": datetime.utcnow().isoformat(timespec="seconds"),
        "tool": "research_portfolio_v2",
        "batch15_root": str(root),
        "candidates": sorted({k[0] for k in trades_by_key}),
        "cost_ids": list(COST_IDS),
        "universe_days": len(universe_days),
        "initial_capital": 1.0,
        "gross_exposure_cap": 1.0,
        "fractional_units_note": ("явное исследовательское упрощение; "
                                  "short borrow/availability UNKNOWN, не live-ready"),
        "metrics": [m.__dict__ for m in all_results],
        "trade_quality": tq_all,
        "walk_forward": wf_all,
        "baselines": baselines,
    }
    (out / "portfolio_v2.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8")
    print(f"portfolio_v2: candidates={len({k[0] for k in trades_by_key})} "
          f"metrics={len(all_results)} trade_quality={len(tq_all)} "
          f"universe_days={len(universe_days)} walk_forward={len(wf_all)} "
          f"baselines=2")
    print(f"  {out / 'portfolio_v2.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
