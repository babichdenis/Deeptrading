#!/usr/bin/env python3
"""ML-MTF-1 (B3, REWORK): предсказания -> исполнение -> портфельный ledger.

Адаптер predictions -> СУЩЕСТВУЮЩИЙ execution core (`app.engine.research_execution`
settle_entry/settle_exit) для PnL/затрат. НЕ копирует PnL-формулы/второй движок.

Исправлено по REWORK (пункты 2–5):
- ВХОД: decision_ts = закрытие 5m = ОТКРЫТИЕ следующего 1m-бара -> вход РОВНО на
  decision_ts (не +1м). Сессия frozen 10:00..18:44 MSK, вход строго до cutoff,
  weekday; timezone-aware UTC.
- ВЫХОД: min(entry+120м, cutoff 18:44) ТОЛЬКО по ТОЧНОЙ минуте; нет точной минуты
  -> INCOMPLETE (не выбор по наличию данных/будущему).
- СЛОТЫ (хронологически): SKIP не занимает капитал; повтор внутри открытой позиции
  IGNORE; после exit слот+кэш освобождаются; на равном времени exits перед entries;
  ticker-order для конкуренции; свободный кэш+fee проверяется при входе; одно FIGI
  одна открытая позиция; не выбирать будущий FILLED вместо INCOMPLETE.
- EQUITY: fixed slot notional (20% СТАРТОВОГО капитала) без молчаливого compounding;
  реальные cashflows+fees; полный final=None при неизвестном хвосте; last_known
  equity+ts отдельно; DD на подписанном известном префиксе.
- LEDGER: разделить signal_outcomes (descriptive) и executed portfolio ledger
  (row_id/identity/reject/entry/exit/qty/fees/net). Portfolio metrics от executed.

Fractions: fractional quantities (research, не live lots); short — следующая ветка.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import sys

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_BACKEND / "scripts") not in sys.path:
    sys.path.insert(0, str(_BACKEND / "scripts"))

from app.engine import research_execution as rx  # noqa: E402
from day_strategies_core import CostParams  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")
EXIT_HORIZON_MIN = 120
SESSION_START_MOD = 600   # 10:00 MSK
SESSION_END_MOD = 1124    # 18:44 MSK (cutoff; planned flat)
SLOT_COUNT = 5
SLOT_WEIGHT = 1.0 / SLOT_COUNT
COST_SCENARIOS = (("c5_s2", 5.0, 2.0), ("c10_s5", 10.0, 5.0))


def _msk_mod(ts: datetime) -> int:
    loc = ts.astimezone(MSK)
    return loc.hour * 60 + loc.minute


def _parse_ts(raw) -> datetime:
    if hasattr(raw, "to_pydatetime"):
        return raw.to_pydatetime().astimezone(timezone.utc)
    if isinstance(raw, datetime):
        return raw.astimezone(timezone.utc) if raw.tzinfo else raw.replace(
            tzinfo=timezone.utc)
    s = str(raw).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(
        tzinfo=timezone.utc)


def _entry_allowed(ts: datetime) -> tuple[bool, str]:
    """Сессионное окно frozen: weekday и 10:00 <= mod < 18:44 (вход до cutoff)."""
    if ts.astimezone(MSK).weekday() >= 5:
        return False, "weekend"
    mod = _msk_mod(ts)
    if mod < SESSION_START_MOD:
        return False, "before_session"
    if mod >= SESSION_END_MOD:
        return False, "at_or_after_cutoff"
    return True, ""


# ------------------------------------------------------------------ minutes

@dataclass
class MinuteStore:
    by_figi: dict[str, dict[datetime, tuple[float, float]]] = field(default_factory=dict)
    _sorted: dict[str, list] = field(default_factory=dict)

    def sorted_ts(self, figi: str) -> list:
        if figi not in self._sorted:
            self._sorted[figi] = sorted(self.by_figi.get(figi, {}))
        return self._sorted[figi]

    def add_csv(self, path: Path, figi: str) -> int:
        import pandas as _pd  # noqa: PLC0415
        try:
            df = _pd.read_csv(path, usecols=["ts", "open", "close", "partial"],
                              dtype={"partial": "string"})
        except ValueError:
            df = _pd.read_csv(path)
        if "partial" in df.columns:
            df = df[df["partial"].isna() | (df["partial"] == "")]
        df["open"] = _pd.to_numeric(df["open"], errors="coerce")
        df["close"] = _pd.to_numeric(df["close"], errors="coerce")
        df = df[(df["open"] > 0) & (df["close"] > 0)]
        ts = _pd.to_datetime(df["ts"], utc=True, format="ISO8601")
        store = self.by_figi.setdefault(figi, {})
        for t, o, c in zip(ts.dt.to_pydatetime(), df["open"].to_numpy(),
                           df["close"].to_numpy()):
            store[t] = (float(o), float(c))
        return len(df)


# ------------------------------------------------------------------ signal outcome

@dataclass
class SignalOutcome:
    """Один предсказанный сигнал (descriptive event study, НЕ портфельная сделка)."""
    row_id: str
    figi: str
    ticker: str
    decision_ts: datetime
    model_id: str
    split: str
    selected: bool
    status: str  # FILLED | INCOMPLETE | SKIP
    reason: str
    entry_ts: datetime | None = None
    exit_ts: datetime | None = None
    entry_base: float | None = None
    exit_base: float | None = None
    gross_per_unit: float | None = None
    net_per_unit: float | None = None
    costs_per_unit: float | None = None
    path_metrics_known: bool = False


def execute_signal(pred: dict, store: MinuteStore,
                   costs: CostParams) -> SignalOutcome:
    """Одно предсказание -> исход входа/выхода (per-unit, до портфельного слота)."""
    figi = pred["figi"]
    ticker = pred.get("ticker", figi)
    decision_ts = _parse_ts(pred["decision_ts"])
    model_id = pred.get("model_id", "")
    split = pred.get("split", "")
    row_id = pred.get("row_id", "")

    def _out(**kw) -> SignalOutcome:
        return SignalOutcome(row_id=row_id, figi=figi, ticker=ticker,
                             decision_ts=decision_ts, model_id=model_id,
                             split=split, **kw)

    # defect 4: предсказание должно быть доступно ДО входа (нет look-ahead)
    at = pred.get("available_at_ts")
    if at:
        try:
            at_ts = _parse_ts(at)
            if at_ts > decision_ts:
                return _out(selected=True, status="SKIP",
                            reason=f"lookahead_unavailable:{at_ts.isoformat()}")
        except Exception:
            pass
    selected = str(pred.get("selected_long", "")).lower() in ("true", "1", "yes")
    if not selected:
        return _out(selected=False, status="SKIP", reason="not_selected_long")

    ok, why = _entry_allowed(decision_ts)
    if not ok:
        return _out(selected=True, status="SKIP", reason=f"out_of_session:{why}")

    minutes = store.by_figi.get(figi, {})
    # вход РОВНО на decision_ts (закрытие 5m = открытие следующего 1m-бара)
    if decision_ts not in minutes:
        return _out(selected=True, status="SKIP", reason="missing_entry_minute")
    entry_base = minutes[decision_ts][0]

    # exit = min(entry+120м, cutoff 18:44 того же дня); ТОЛЬКО точная минута
    exit_target = decision_ts + timedelta(minutes=EXIT_HORIZON_MIN)
    if _msk_mod(exit_target) > SESSION_END_MOD:
        loc = decision_ts.astimezone(MSK)
        cutoff = loc.replace(hour=SESSION_END_MOD // 60,
                             minute=SESSION_END_MOD % 60, second=0,
                             microsecond=0).astimezone(timezone.utc)
        exit_target = cutoff
    if exit_target not in minutes:
        return _out(selected=True, status="INCOMPLETE",
                    reason="exit_minute_missing", entry_ts=decision_ts,
                    entry_base=entry_base)
    exit_base = minutes[exit_target][0]

    ef = rx.settle_entry(entry_base, 1, costs)
    settled = rx.settle_exit(entry_base, exit_base, 1, costs,
                             entry_fill=ef["entry_fill"],
                             commission_entry=ef["commission_entry"])
    # path metrics: проверяем непрерывность промежуточных минут (endpoint известен)
    path_ok = _path_continuous(minutes, decision_ts, exit_target)
    return _out(selected=True, status="FILLED", reason="time_exit_120_or_cutoff",
                entry_ts=decision_ts, exit_ts=exit_target,
                entry_base=entry_base, exit_base=exit_base,
                gross_per_unit=settled["gross"] / entry_base,
                net_per_unit=settled["net"] / entry_base,
                costs_per_unit=settled["costs_total"] / entry_base,
                path_metrics_known=path_ok)


def _path_continuous(minutes: dict, entry_ts: datetime, exit_ts: datetime) -> bool:
    t = entry_ts + timedelta(minutes=1)
    while t <= exit_ts:
        if t not in minutes:
            return False
        t += timedelta(minutes=1)
    return True


# ------------------------------------------------------------------ executed portfolio

@dataclass
class ExecTrade:
    """Исполненная (ЗАКРЫТАЯ) портфельная позиция (одна на FIGI, fixed slot notional).

    costs_total = commission_entry + commission_exit + qty*(slip_entry + slip_exit);
    тождество net == gross - costs_total по построению (см. research_execution).
    """
    row_id: str
    figi: str
    ticker: str
    model_id: str
    entry_ts: datetime
    exit_ts: datetime
    qty: float
    entry_fill: float
    exit_fill: float
    slip_entry: float
    slip_exit: float
    commission_entry: float
    commission_exit: float
    gross: float
    net: float
    entry_cost: float
    exit_proceeds: float

    @property
    def costs_total(self) -> float:
        # истинный slippage-cost на long = (entry_fill-entry_base) + (exit_base-exit_fill)
        # = slip_entry - slip_exit; совпадает с costs_total из research_execution
        # (ce + cx + gross - fill_delta), обеспечивая тождество net = gross - costs_total
        return (self.commission_entry + self.commission_exit
                + self.qty * (self.slip_entry - self.slip_exit))


@dataclass
class AcceptedOpen:
    """Принятая портфелем НЕЗАКРЫТАЯ позиция (INCOMPLETE: выход неизвестен).

    Отдельный ledger от executed: после первого accepted-open капитал/позиция
    остаются неизвестными, будущая реализуемая portfolio equity неизвестна.
    """
    row_id: str
    figi: str
    ticker: str
    model_id: str
    entry_ts: datetime
    entry_base: float
    qty: float
    entry_debit: float
    planned_exit_ts: datetime | None


@dataclass
class PortfolioResult:
    initial_capital: float
    executed: list[ExecTrade]
    accepted_open: list[AcceptedOpen]
    signal_outcomes: list[SignalOutcome]
    day_realized: dict  # date -> realized net cash flow (фикс. notional, без compounding)
    day_unknown: set    # дни с INCOMPLETE позицией (equity неизвестен после)
    equity_known: list  # [(date, equity)] известный префикс (все позиции закрыты)
    final_equity: float | None  # None если есть неизвестный хвост
    last_known_equity: float | None
    last_known_ts: date | None
    max_drawdown_known: float | None
    free_cash_end: float
    n_signal: int
    n_executed: int
    n_accepted_open: int
    n_skip: int
    n_ignore_repeat: int
    n_no_slot: int
    n_no_cash: int
    n_incomplete_outcomes: int  # signal-level (все INCOMPLETE исходы)


def run_portfolio(outcomes: list[SignalOutcome], costs: CostParams,
                  initial_capital: float = 1.0,
                  slot_count: int = SLOT_COUNT) -> PortfolioResult:
    """Хронологическое исполнение портфеля: fixed slot notional, реальные cashflows.

    SKIP не занимает капитал; повтор внутри открытой позиции IGNORE; после exit
    слот+кэш освобождаются; на равном времени exits перед entries; ticker-order.
    Одно FIGI — одна открытая позиция. Затраты через research_execution
    (settle_entry/settle_exit): costs_total включает slippage + обе комиссии.
    Cash-check: полный entry debit (notional + entry commission) проверяется ДО
    мутации free_cash/slots (defect 1). Исполненные (FILLED) и принятые открытые
    (INCOMPLETE) позиции ведутся отдельными ledgers.
    """
    slot_notional = SLOT_WEIGHT * initial_capital
    # события входа: только исходы с entry_ts (FILLED/INCOMPLETE-with-entry)
    entries = [o for o in outcomes if o.entry_ts is not None]
    entries.sort(key=lambda o: (o.entry_ts, o.figi))

    open_pos: dict[str, SignalOutcome] = {}   # figi -> open outcome (для IGNORE)
    exit_heap: list[tuple[datetime, str]] = []  # (exit_ts, figi) для FILLED
    import heapq  # noqa: PLC0415
    free_slots = slot_count
    free_cash = initial_capital
    realized_by_day: dict[date, float] = {}
    unknown_days: set[date] = set()
    executed: list[ExecTrade] = []
    accepted_open: list[AcceptedOpen] = []
    n_ignore = n_no_slot = n_no_cash = 0

    def _close(exit_ts: datetime, figi: str) -> None:
        nonlocal free_slots, free_cash
        o = open_pos.pop(figi, None)
        if o is None:
            return
        qty = slot_notional / o.entry_base
        ef = rx.settle_entry(o.entry_base, 1, costs)        # per-unit
        ce_unit = ef["commission_entry"]
        settled = rx.settle_exit(o.entry_base, o.exit_base, 1, costs,
                                entry_fill=ef["entry_fill"],
                                commission_entry=ce_unit)
        ce = ce_unit * qty
        cx = settled["commission_exit"] * qty
        gross = settled["gross"] * qty
        net = settled["net"] * qty
        entry_cost = qty * ef["entry_fill"] + ce
        exit_proceeds = qty * settled["exit_fill"] - cx
        free_cash += exit_proceeds
        free_slots += 1
        d = exit_ts.astimezone(MSK).date()
        realized_by_day[d] = realized_by_day.get(d, 0.0) + net
        executed.append(ExecTrade(
            row_id=o.row_id, figi=figi, ticker=o.ticker, model_id=o.model_id,
            entry_ts=o.entry_ts, exit_ts=o.exit_ts, qty=qty,
            entry_fill=ef["entry_fill"], exit_fill=settled["exit_fill"],
            slip_entry=ef["slip_entry"], slip_exit=settled["slip_exit"],
            commission_entry=ce, commission_exit=cx, gross=gross, net=net,
            entry_cost=entry_cost, exit_proceeds=exit_proceeds))

    def _try_open(o: SignalOutcome) -> bool:
        nonlocal free_slots, free_cash, n_ignore, n_no_slot, n_no_cash
        if o.figi in open_pos:
            n_ignore += 1
            return False
        if free_slots <= 0:
            n_no_slot += 1
            return False
        # полный entry debit ДО мутации free_cash/slots (defect 1)
        qty = slot_notional / o.entry_base
        ef = rx.settle_entry(o.entry_base, 1, costs)
        ce = costs.commission(ef["entry_fill"]) * qty
        entry_debit = qty * ef["entry_fill"] + ce
        if free_cash < entry_debit:
            n_no_cash += 1
            return False
        free_cash -= entry_debit
        free_slots -= 1
        open_pos[o.figi] = o
        if o.status == "INCOMPLETE":
            # вход известен, выход неизвестен -> позиция открыта, equity неизвестен
            unknown_days.add(o.entry_ts.astimezone(MSK).date())
            accepted_open.append(AcceptedOpen(
                row_id=o.row_id, figi=o.figi, ticker=o.ticker,
                model_id=o.model_id, entry_ts=o.entry_ts,
                entry_base=o.entry_base, qty=qty, entry_debit=entry_debit,
                planned_exit_ts=None))
        else:
            heapq.heappush(exit_heap, (o.exit_ts, o.figi))
        return True

    for o in entries:
        # закрыть всё, что вышло РАНЬШЕ или в момент входа (exits перед entries)
        while exit_heap and exit_heap[0][0] <= o.entry_ts:
            ets, efigi = heapq.heappop(exit_heap)
            _close(ets, efigi)
        _try_open(o)
    # закрыть оставшиеся FILLED (последний известный выход)
    while exit_heap:
        ets, efigi = heapq.heappop(exit_heap)
        _close(ets, efigi)
    # INCOMPLETE позиции остаются открытыми -> equity unknown после

    # equity curve на ОБЩЕЙ календарной оси: известный префикс = ВСЕ дни с
    # закрытыми (FILLED) сделками. INCOMPLETE не попадают в realized_by_day (выход
    # неизвестен) и НЕ обрывают кривую — первая INCOMPLETE не травит последующий
    # equity (defect 3). final_equity=None только если на конец окна остались
    # открытые INCOMPLETE-позиции: их выход неизвестен => истинный конечный equity
    # не определён (coverage limit, НЕ доказательство проигрыша кэшу).
    all_days = sorted({o.decision_ts.astimezone(MSK).date() for o in outcomes})
    eq = initial_capital
    equity_known: list[tuple[date, float]] = []
    for d in all_days:
        eq += realized_by_day.get(d, 0.0)
        equity_known.append((d, eq))
    has_open_at_end = len(accepted_open) > 0
    final_eq = equity_known[-1][1] if (equity_known and not has_open_at_end) else None
    last_known = equity_known[-1][1] if equity_known else initial_capital
    last_known_ts = equity_known[-1][0] if equity_known else None
    # DD на подписанном известном префиксе
    peak = initial_capital
    mdd = 0.0
    for _, e in equity_known:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)

    n_skip = sum(1 for o in outcomes if o.status == "SKIP")
    n_inc = sum(1 for o in outcomes if o.status == "INCOMPLETE")
    return PortfolioResult(
        initial_capital=initial_capital, executed=executed,
        accepted_open=accepted_open, signal_outcomes=outcomes,
        day_realized=realized_by_day, day_unknown=unknown_days,
        equity_known=equity_known, final_equity=final_eq,
        last_known_equity=last_known, last_known_ts=last_known_ts,
        max_drawdown_known=mdd, free_cash_end=free_cash,
        n_signal=len(outcomes),
        n_executed=len(executed), n_accepted_open=len(accepted_open),
        n_skip=n_skip, n_ignore_repeat=n_ignore, n_no_slot=n_no_slot,
        n_no_cash=n_no_cash, n_incomplete_outcomes=n_inc)


# ------------------------------------------------------------------ metrics

def portfolio_metrics(res: PortfolioResult, model_id: str, cost_id: str,
                      split: str) -> dict:
    nets = [t.net for t in res.executed]
    fees = [t.commission_entry + t.commission_exit for t in res.executed]
    slips = [t.qty * (t.slip_entry - t.slip_exit) for t in res.executed]
    totals = [t.costs_total for t in res.executed]
    gross = [t.gross for t in res.executed]
    wins = [n for n in nets if n > 0]
    n_exc = len(res.executed)
    gross_t = sum(gross)
    net_t = sum(nets)
    fees_t = sum(fees)
    slips_t = sum(slips)
    total_t = sum(totals)
    # тождество net = gross - costs_total (costs_total включает slippage + обе комиссии)
    identity_ok = abs((gross_t - total_t) - net_t) < 1e-9
    return {
        "model_id": model_id, "cost_id": cost_id, "split": split,
        "n_signal_outcomes": res.n_signal,
        "n_executed": res.n_executed,
        "n_accepted_open": res.n_accepted_open,
        "n_incomplete_outcomes": res.n_incomplete_outcomes,
        "n_skip": res.n_skip, "n_ignore_repeat": res.n_ignore_repeat,
        "n_no_slot": res.n_no_slot, "n_no_cash": res.n_no_cash,
        "gross_total": gross_t,
        "net_total": net_t,
        "fees_total": fees_t,
        "slippage_total": slips_t,
        "costs_total": total_t,
        "costs_identity_net_eq_gross_minus_costs": identity_ok,
        "net_mean_per_trade": (statistics.mean(nets) if nets else None),
        "win_rate": (len(wins) / n_exc if n_exc else None),
        "final_equity": res.final_equity,
        "last_known_equity": res.last_known_equity,
        "last_known_ts": (res.last_known_ts.isoformat()
                          if res.last_known_ts else None),
        "max_drawdown_known": res.max_drawdown_known,
        "unknown_tail": res.final_equity is None,
        "note": ("executed ledger + accepted-open ledger (fixed slot notional, "
                 "без compounding); costs_total = fees + slippage; final=None "
                 "при неизвестном хвосте — coverage limit, не автоматический "
                 "минус стратегии; accepted-open = незакрытые INCOMPLETE позиции"),
    }


def save_executed_ledger(res: PortfolioResult, path: Path) -> None:
    fields = ["row_id", "figi", "ticker", "model_id", "entry_ts", "exit_ts",
              "qty", "entry_fill", "exit_fill", "slip_entry", "slip_exit",
              "commission_entry", "commission_exit", "gross", "net",
              "costs_total", "entry_cost", "exit_proceeds"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for t in res.executed:
            row = {k: getattr(t, k) for k in fields}
            row["costs_total"] = t.costs_total
            w.writerow(row)


def save_accepted_open_ledger(res: PortfolioResult, path: Path) -> None:
    fields = ["row_id", "figi", "ticker", "model_id", "entry_ts",
              "entry_base", "qty", "entry_debit", "planned_exit_ts"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for o in res.accepted_open:
            w.writerow({k: getattr(o, k) for k in fields})


# ------------------------------------------------------------------ loading

def load_predictions(path: Path, model_id: str | None = None,
                     split: str | None = None) -> list[dict]:
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq  # noqa: PLC0415
        import pyarrow.compute as pc  # noqa: PLC0415
        cols = ["row_id", "figi", "decision_ts", "model_id", "split",
                "selected_long"]
        filt = None
        if model_id is not None:
            filt = pc.equal(pc.field("model_id"), model_id)
        if split is not None:
            sf = pc.equal(pc.field("split"), split)
            filt = sf if filt is None else (filt & sf)
        tbl = pq.read_table(path, columns=cols,
                            filters=filt) if filt is not None else \
            pq.read_table(path, columns=cols)
        return tbl.to_pylist()
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    import pandas as pd  # noqa: PLC0415
    df = pd.DataFrame(rows)
    if model_id:
        df = df[df["model_id"] == model_id]
    if split:
        df = df[df["split"] == split]
    return df.to_dict("records")


def load_minutes(root: Path, figi_map: dict[str, str]) -> MinuteStore:
    store = MinuteStore()
    for figi, ticker in figi_map.items():
        f = root / f"{ticker}__m1.csv"
        if f.exists():
            store.add_csv(f, figi)
    return store


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ML-MTF-1 evaluate (B3, REWORK v3)")
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--minutes-root", required=True)
    ap.add_argument("--figi-map", default=None, help="JSON {figi:ticker}")
    ap.add_argument("--model-id", required=True,
                    help="явный model_id (напр. dtree__all_tf_votes)")
    ap.add_argument("--split", default="eval", choices=("train", "val", "eval"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--initial-capital", type=float, default=1.0)
    ap.add_argument("--slots", type=int, default=SLOT_COUNT)
    ap.add_argument("--force-long", action="store_true",
                    help="arms: трактовать ВСЕ решения как selected_long (always_long)")
    ap.add_argument("--dataset-manifest", default=None,
                    help="B1 manifest.json (provenance/hash verification)")
    ap.add_argument("--model-manifest", default=None,
                    help="B2 model_manifest.json (primary_winner/version check)")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    preds = load_predictions(Path(args.predictions), args.model_id, args.split)
    if args.force_long:
        for p in preds:
            p["selected_long"] = "true"

    model_manifest = dataset_manifest = None
    if args.model_manifest:
        model_manifest = json.loads(
            Path(args.model_manifest).read_text(encoding="utf-8"))
        pw = model_manifest.get("primary_winner")
        if pw and pw != args.model_id:
            print(f"WARNING: --model-id {args.model_id} != "
                  f"model_manifest.primary_winner {pw}")
    if args.dataset_manifest:
        dataset_manifest = json.loads(
            Path(args.dataset_manifest).read_text(encoding="utf-8"))

    figi_map = (json.loads(Path(args.figi_map).read_text(encoding="utf-8"))
                if args.figi_map else
                {p["figi"]: p.get("ticker", p["figi"]) for p in preds})
    store = load_minutes(Path(args.minutes_root), figi_map)

    # provenance: хэши входов (воспроизводимость, defect 4)
    provenance = {
        "predictions": {"path": str(args.predictions),
                        "sha256": _sha256(Path(args.predictions))},
        "minutes_root": str(args.minutes_root),
        "minutes_files": sorted(
            f"{p.name}:{_sha256(p)}"
            for p in sorted(Path(args.minutes_root).glob("*__m1.csv"))),
    }
    if args.model_manifest:
        provenance["model_manifest"] = {
            "path": args.model_manifest,
            "sha256": _sha256(Path(args.model_manifest)),
            "primary_winner": model_manifest.get("primary_winner"),
            "data_version": model_manifest.get("data_version")}
    if args.dataset_manifest:
        provenance["dataset_manifest"] = {
            "path": args.dataset_manifest,
            "sha256": _sha256(Path(args.dataset_manifest)),
            "dataset_version": dataset_manifest.get("dataset_version")}

    manifest = {"predictions": str(args.predictions),
                "model_id": args.model_id, "split": args.split,
                "initial_capital": args.initial_capital, "slots": args.slots,
                "exit_horizon_min": EXIT_HORIZON_MIN,
                "session": [SESSION_START_MOD, SESSION_END_MOD],
                "cash_check": "full_entry_debit_before_mutation",
                "costs_total": "fees + slippage (via research_execution.settle_exit)",
                "cost_scenarios": [{"id": c, "commission_bps": m,
                                    "slippage_bps": s}
                                   for c, m, s in COST_SCENARIOS]}
    results = []
    for cost_id, comm, slip in COST_SCENARIOS:
        costs = CostParams(comm, slip)
        costs.validate()
        outcomes = [execute_signal(p, store, costs) for p in preds]
        res = run_portfolio(outcomes, costs, args.initial_capital, args.slots)
        results.append(portfolio_metrics(res, args.model_id, cost_id,
                                         args.split))
        save_executed_ledger(res, out / f"executed_ledger_{cost_id}.csv")
        save_accepted_open_ledger(res, out / f"accepted_open_{cost_id}.csv")
        with (out / f"signal_outcomes_{cost_id}.csv").open(
                "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "row_id", "figi", "ticker", "decision_ts", "model_id", "split",
                "selected", "status", "reason", "entry_ts", "exit_ts",
                "entry_base", "exit_base", "gross_per_unit", "net_per_unit",
                "costs_per_unit", "path_metrics_known"])
            w.writeheader()
            for o in outcomes:
                w.writerow({k: getattr(o, k) for k in w.fieldnames})

    payload = {"generated_utc": datetime.now(timezone.utc).isoformat(
        timespec="seconds"), "tool": "ml_mtf_evaluate", "manifest": manifest,
        "provenance": provenance, "metrics": results}
    (out / "evaluate_v2.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8")
    print(f"ml_mtf_evaluate: preds={len(preds)} model={args.model_id} "
          f"split={args.split} metrics={len(results)} "
          f"accepted_open={sum(r['n_accepted_open'] for r in results)}")
    print(f"  {out / 'evaluate_v2.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
