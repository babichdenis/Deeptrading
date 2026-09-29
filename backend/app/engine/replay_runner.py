"""L2.6 — инкрементальный replay-движок (OsEngine-style event processing).

Тот же код, что batch EngineRunner.run (методы _body/_poll один в один),
но состояние _RunState переносится между extend(), а история только растёт.
Эквивалентность batch run() над полной историей — точная, включая тонкости:

- опрос стратегии отложен на бар: бар j опрашивается, когда перестаёт быть
  последним (бывший гард `i + 1 < total`), на состоянии ровно post-body(j);
- тело первого бара ждёт второго (tf_minutes/session_active вычисляются по
  барам [0..1], как batch); вырожденный 1-барный прогон обрабатывается
  в finalize() с дефолтами — как batch run() над одним баром;
- END_OF_DATA — только в finalize(), промежуточные extend() позицию не рвут;
- regime-кэш runner'а инвалидируется по длине regime_bars (см. _regime_at);
- сигналы подаются предзагруженным ReplayStrategy (как _run_pipeline);
  поэтапная подача через ReplayStrategy.extend() — задача интеграции L2.7.

Контракты: история append-only (сброс = новый IncrementalRunner);
progress_cb(i, len, ts) — len известен только текущий (логирование).
"""
from __future__ import annotations

from app.engine.ledger import TradeLedger
from app.engine.models import ExitReason
from app.engine.runner import EngineRunner
from app.engine.views import CandleWindow


class IncrementalRunner:
    """Пошаговый прогон: extend() кормит бары, finalize() закрывает END_OF_DATA."""

    def __init__(self, strategy, exit_policy, config=None):
        self.runner = EngineRunner(strategy, exit_policy, config)
        self.history: list = []
        self.st = None
        self._body_done = 0
        self._poll_done = -1
        self._tf_final = False

    @property
    def strategy(self):
        """Стратегия раннера (для ReplayState.run_step: extend сигналами)."""
        return self.runner.strategy

    # ------------------------------------------------------------------ feed
    def extend(self, candles_prefix, progress_cb=None) -> None:
        """Принять историю целиком (растущий список), обработать новые бары."""
        prefix = list(candles_prefix)
        if len(prefix) < len(self.history):
            raise ValueError("история обязана только расти (сброс = новый runner)")
        self.history.extend(prefix[len(self.history):])
        if not self.history:
            return
        if self.st is None:
            self.st = self.runner._new_state(self.history)
        self._drain(progress_cb, allow_single=False)

    def _drain(self, progress_cb, allow_single: bool) -> None:
        st = self.st
        if st is None:
            return
        n = len(self.history)
        if not self._tf_final and n >= 2:
            # tf/session ровно как batch: по барам [0..1]; тел ещё не было
            delta = (self.history[1].ts - self.history[0].ts).total_seconds() / 60
            st.tf_minutes = max(1, round(delta))
            st.session_active = self.runner.session is not None and st.tf_minutes < 1440
            self._tf_final = True
        while self._body_done < n and (n >= 2 or allow_single):
            k = self._body_done
            if k - 1 > self._poll_done:
                if k - 1 >= st.warmup - 1:
                    self.runner._poll(k - 1, self.history, st)
                else:
                    # ENG-008: warmup feed — один в один с batch run():
                    # закрытые бары до warmup кормят stateful-стратегию,
                    # сигналы подавляются.
                    self.runner.strategy.on_bar(CandleWindow(self.history, 0, k))
                self._poll_done = k - 1
            bar = self.history[k]
            if progress_cb is not None and k > 0 and k % 500 == 0:
                progress_cb(k, n, bar.ts)
            self.runner._body(k, bar, self.history, st)
            self._body_done += 1

    # --------------------------------------------------------------- finalize
    def finalize(self) -> TradeLedger:
        """Дообработать остаток (вырожденный случай), закрыть END_OF_DATA."""
        if self.st is None:
            return TradeLedger()
        self._drain(None, allow_single=True)
        st = self.st
        n = len(self.history)
        if st.position is not None and n:
            last = self.history[-1]
            self.runner._close(n - 1, last.ts, st.position, last.close,
                               ExitReason.END_OF_DATA.value, st.ledger)
        return st.ledger

    # ---------------------------------------------------------------- helpers
    @property
    def ledger(self) -> TradeLedger | None:
        return None if self.st is None else self.st.ledger

    @property
    def exit_coverage(self) -> dict:
        return dict(self.runner.exit_coverage)

    @property
    def position(self):
        return None if self.st is None else self.st.position
