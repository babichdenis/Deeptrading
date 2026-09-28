"""L2.7 — интеграция инкрементального replay-конвейера (OsEngine-style).

ReplayState НАСЛЕДУЕТ EnsembleContext (sync/валидация, bias, bar_stats,
resample — доказанные): _gensig/_res/_bstats диспетчируются как раньше,
плюс новые инкрементальные источники:

- StrategyState на (sid, params, tf): сигналы закрытых + зонд forming
  (набор фиксирован: req setups + adaptive setups; неизвестный sid — громкая
  ошибка, а не тихий fallback);
- RegimeState на regime-TF: строки закрытых + строка forming;
- RsiState-тройка (sig_tf, 1m, 5m): карты {ts: (rsi, prev)};
- MicroTracker на entry-TF: записи micro_breakout;
- IncrementalRunner на метку ("static"/"adaptive") + ReplayStrategy.extend;
- idx_by_ts по валидированным 1m.

Тяжёлые блоки (oracle zigzag, macd/stoch/volume/pullback/volgate/score/ml,
imoex) остаются batch-кодом — корректны по построению, просто медленнее.

Политика forming (L2.5): персистентное состояние двигается только по
закрытым барам; forming оценивается зондом и отбрасывается.

feed(): окно from/to/days фиксируется один раз (batch парсит границы при
каждом вызове); retro-drop битого дня (сжатие valid) → полный reset + refeed
из буфера (редко, корректно по построению).

run_step(): unseen accepted/exits докармливаются в ReplayStrategy (дедуп по
(ts_iso, side, kind)), runner.extend() префиксом; finalize=True → finalize()
с синтетическим END_OF_DATA (как batch). Возвращает (ledger, coverage).
"""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta, timezone

from app.engine.replay_runner import IncrementalRunner
from app.engine.wave1 import ReplayStrategy
from app.services.data_context import DataContext
from app.services.ensemble_ctx import EnsembleContext
from app.services.indicator_state import RsiState
from app.services.regime import regime_timeline
from app.services.regime_state import RegimeState
from app.services.strategy_state import StrategyState

_TF = {"1min": 60, "5min": 300, "10min": 600, "15min": 900,
       "30min": 1800, "hour": 3600}


def _tf_sec(name: str, default: int) -> int:
    return int(_TF.get(str(name), default))


def _ts_dt(ts):
    """ts как datetime (accepted хранит ISO-строки, exits — datetime)."""
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


class MicroTracker:
    """Инкрементальный micro_breakout: решение по бару зависит только от
    lookback предыдущих (high/low) — тот же перебор, что batch, по частям.

    Точная квирка batch: цикл идёт с i = lookback+1 (бар lookback пропускается
    всегда) — первые lookback+1 баров только наполняют окно.
    """

    def __init__(self, lookback: int):
        self.lookback = lookback
        self._win: deque = deque(maxlen=lookback)
        self._fed = 0
        self.entries: list[dict] = []

    def update(self, bar) -> dict | None:
        """Скормить закрытый бар; вернуть запись входа или None."""
        self._fed += 1
        if self._fed <= self.lookback + 1:
            self._win.append((bar.high, bar.low))
            return None
        sig = self._eval(bar, self._win)
        self._win.append((bar.high, bar.low))
        if sig is not None:
            self.entries.append(sig)
        return sig

    def probe(self, bar):
        """Оценка forming-бара без изменения состояния."""
        if self._fed <= self.lookback:
            return None
        return self._eval(bar, self._win)

    def _eval(self, bar, win) -> dict | None:
        if len(win) < self.lookback:
            return None
        highest = max(h for h, _ in win)
        lowest = min(l for _, l in win)
        if bar.close > highest:
            return {"ts": bar.ts, "side": "BUY", "reason": "micro_breakout_up",
                    "features": {"breakout_level": highest}}
        if bar.close < lowest:
            return {"ts": bar.ts, "side": "SELL", "reason": "micro_breakout_down",
                    "features": {"breakout_level": lowest}}
        return None


class ReplayState(EnsembleContext):
    """Персистентное состояние replay-прогона для одного инструмента+req."""

    def __init__(self, req: dict):
        super().__init__()
        self.req = req
        self._init_replay(req)

    # ------------------------------------------------------------------ init
    def _init_replay(self, req):
        from_ts = req.get("from_ts")
        to_ts = req.get("to_ts")
        self._fdt = (datetime.fromisoformat(from_ts.replace("Z", "+00:00"))
                     if from_ts else None)
        self._tdt = (datetime.fromisoformat(to_ts.replace("Z", "+00:00"))
                     if to_ts else None)
        days = int(req.get("days", 3))
        self._cut = (datetime.now(timezone.utc) - timedelta(days=days)
                     if self._fdt is None and self._tdt is None else None)
        tfs = {60, 300}
        for s in req.get("setups", []):
            tfs.add(_tf_sec(s.get("tf", "5min"), 300))
        for a in req.get("adaptive") or []:
            for s in ((a.get("config") or {}).get("setups") or []):
                tfs.add(_tf_sec(s.get("tf", "5min"), 300))
        tfs.add(_tf_sec(req.get("entry_tf", "1min"), 60))
        tfs.add(_tf_sec((req.get("regime") or {}).get("tf", "5min"), 300))
        tfs.add(_tf_sec(req.get("signal_tf") or "10min", 600))
        self.dctx = DataContext(tuple(sorted(tfs)))
        self._raw_n = 0
        self._raw_filt: list = []
        self._fed_valid = 0
        self._idx: dict = {}
        self.strategies: dict = {}

        def _reg(sid, params, tf):
            key = (sid, json.dumps(params or {}, sort_keys=True), tf)
            if key not in self.strategies:
                self.strategies[key] = StrategyState(sid, params, tf_sec=tf)

        for s in req.get("setups", []):
            _reg(s["strategy_id"], s.get("params"),
                 _tf_sec(s.get("tf", "5min"), 300))
        for a in req.get("adaptive") or []:
            for s in ((a.get("config") or {}).get("setups") or []):
                _reg(s["strategy_id"], s.get("params"),
                     _tf_sec(s.get("tf", "5min"), 300))
        self._setup_tfs: dict[int, list] = {}
        for (sid, pj, tf), st in self.strategies.items():
            self._setup_tfs.setdefault(tf, []).append(st)
        self._regime_tf: int | None = None
        self._regime_state: RegimeState | None = None
        self._regime_live: list = []
        self._regime_live_closed = 0
        self._regime_live_forming = False
        _rp = int(((req.get("rsi_filter") or {}).get("period", 14)))
        self._rsi: dict = {}
        for tf in (_tf_sec(req.get("signal_tf") or "10min", 600), 60, 300):
            self._rsi[tf] = {"st": RsiState(_rp), "map": {}, "keys": [],
                             "fed": 0}
        _etf = _tf_sec(req.get("entry_tf", "1min"), 60)
        _elb = int((req.get("entry") or {}).get("lookback", 1))
        self._micro_tf = _etf
        self._micro = MicroTracker(_elb)
        self.runners: dict = {}
        self._fed_sigs: set = set()
        self.fed_late = 0
        self.fed_late_samples = []  # первые 20 late (ts, side, kind) для отчета

    def reset(self):
        """Полный сброс (retro-drop битого дня) + refeed из буфера."""
        EnsembleContext.__init__(self)
        raw = list(self._raw_filt)
        self._init_replay(self.req)
        for b in raw:
            self._raw_filt.append(b)
        self._raw_n = len(raw)
        valid, _ = self.sync(self._raw_filt)
        for b in valid:
            self._ingest_valid(b)
        self._fed_valid = len(valid)

    # ------------------------------------------------------------------ feed
    def feed(self, raw_prefix):
        """Скормить растущий raw-буфер. Вернуть (validated, skipped) как sync."""
        for b in raw_prefix[self._raw_n:]:
            self._raw_n += 1
            if self._fdt is not None and b.ts < self._fdt:
                continue
            if self._tdt is not None and b.ts > self._tdt:
                continue
            if self._cut is not None and b.ts < self._cut:
                continue
            self._raw_filt.append(b)
        # NOTE: фильтр предполагает монотонный ts (replay всегда по времени).
        valid, skipped = self.sync(self._raw_filt)
        if len(valid) < self._fed_valid:
            self.reset()
            return self._valid, self._skipped
        for b in valid[self._fed_valid:]:
            self._ingest_valid(b)
        self._fed_valid = len(valid)
        return valid, skipped

    def _ingest_valid(self, b):
        self._idx[b.ts] = len(self._idx)
        events = self.dctx.append(b)
        for tf, ev in events.items():
            if ev != "closed":
                continue
            closed = self.dctx.closed(tf)
            new_bar = closed[-1]
            for st in self._setup_tfs.get(tf, []):
                st.update(closed)
            if self._regime_state is not None and tf == self._regime_tf:
                self._regime_state.update(new_bar)
            rc = self._rsi.get(tf)
            if rc is not None:
                rc["st"].update(new_bar)
                # batch-карта начинается с сид-бара: None-значения не храним
                if rc["st"].value is not None:
                    rc["map"][new_bar.ts] = (rc["st"].value, rc["st"].prev_value)
                    rc["keys"].append(new_bar.ts)
            if tf == self._micro_tf:
                self._micro.update(new_bar)

    # ---------------------------------------------------------------- sources
    def setup_sigs(self, sid, params, tf_sec):
        """Сигналы сетапа == _gensig batch: закрытые + зонд forming."""
        key = (sid, json.dumps(params or {}, sort_keys=True), tf_sec)
        try:
            st = self.strategies[key]
        except KeyError:
            raise RuntimeError(f"replay: неизвестный сетап {(sid, tf_sec)} "
                               f"(набор фиксирован req/adaptive)")
        closed = self.dctx.closed(tf_sec)
        # КОПИИ: гейты ниже мутируют записи (e["rsi"]=...); batch каждый вызов
        # работает со свежими dict из generate_signals — parity требует того же
        out = [dict(s) for s in st.sigs]
        forming = self.dctx.forming(tf_sec)
        if forming is not None:
            probe = st.evaluate(closed, forming)
            if probe is not None:
                out.append(probe)
        return out

    def regime(self, tf_sec, cfg):
        """(states, timeline, bars): закрытые + forming. states — живой объект."""
        if self._regime_state is None or self._regime_tf != tf_sec:
            kw = {k: v for k, v in (cfg or {}).items()
                  if k in ("slope_threshold", "adx_threshold",
                           "atr_percentile_threshold", "range_mult")}
            self._regime_state = RegimeState(**kw)
            self._regime_tf = tf_sec
            self._regime_live = []
            self._regime_live_closed = 0
            self._regime_live_forming = False
            for b in self.dctx.closed(tf_sec):
                self._regime_state.update(b)
        rows = self._regime_state.rows
        if self._regime_live_forming:
            self._regime_live.pop()
            self._regime_live_forming = False
        self._regime_live.extend(rows[self._regime_live_closed:])
        self._regime_live_closed = len(rows)
        forming = self.dctx.forming(tf_sec)
        if forming is not None:
            self._regime_live.append(self._regime_state.evaluate(forming))
            self._regime_live_forming = True
        return self._regime_live, regime_timeline(self._regime_live), \
            self.dctx.bars(tf_sec)

    def rsi_maps(self):
        """Три карты + ключи. Карты живые; ключи = closed + forming ts."""
        out = {}
        for tf, rc in self._rsi.items():
            forming = self.dctx.forming(tf)
            if forming is not None:
                fv = rc["st"].evaluate(forming)
                if fv is not None:
                    rc["map"][forming.ts] = (fv, rc["st"].value)
                    out[tf] = (rc["map"], rc["keys"] + [forming.ts])
                else:
                    out[tf] = (rc["map"], list(rc["keys"]))
            else:
                out[tf] = (rc["map"], list(rc["keys"]))
        return out

    def micro_entries(self):
        """Все micro-записи == batch: закрытые + зонд forming (копии)."""
        out = [dict(e) for e in self._micro.entries]
        forming = self.dctx.forming(self._micro_tf)
        if forming is not None:
            probe = self._micro.probe(forming)
            if probe is not None:
                out.append(probe)
        return out

    def idx_by_ts(self):
        return self._idx

    # ---------------------------------------------------------------- runner
    def run_step(self, label, exit_obj, cfg_engine, accepted, exits,
                 candles_prefix, final=False):
        """Докормить ReplayStrategy unseen-сигналами, extend раннера."""
        r = self.runners.get(label)
        if r is None:
            r = IncrementalRunner(strategy=ReplayStrategy([]),
                                  exit_policy=exit_obj, config=cfg_engine)
            self.runners[label] = r
        cur_ts = r.history[-1].ts if r.history else None
        new_e = []
        for a in accepted:
            k = (str(a.get("ts")), str(a.get("side")), "entry")
            if k not in self._fed_sigs:
                self._fed_sigs.add(k)
                new_e.append((a.get("ts"), a.get("side")))
                if cur_ts is not None and _ts_dt(a.get("ts")) < cur_ts:
                    self.fed_late += 1
                    if len(self.fed_late_samples) < 20:
                        self.fed_late_samples.append(
                            (str(a.get("ts")), str(a.get("side")), "entry"))
        new_x = []
        for e in exits:
            k = (str(e.get("ts")), str(e.get("side")), "exit")
            if k not in self._fed_sigs:
                self._fed_sigs.add(k)
                new_x.append((e.get("ts"), e.get("side")))
                if cur_ts is not None and _ts_dt(e.get("ts")) < cur_ts:
                    self.fed_late += 1
                    if len(self.fed_late_samples) < 20:
                        self.fed_late_samples.append(
                            (str(e.get("ts")), str(e.get("side")), "exit"))
        if new_e or new_x:
            r.strategy.extend(new_e, new_x)
        r.extend(candles_prefix)
        if final:
            ledger = r.finalize()
        else:
            ledger = r.ledger
        return ledger, dict(r.exit_coverage)
