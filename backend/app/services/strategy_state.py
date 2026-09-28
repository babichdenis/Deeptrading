"""L2.4 — персистентное состояние стратегий (OsEngine-style).

Принцип: ОДИН объект стратегии на (sid, params, TF) живёт между барами.
Закрытые бары скармливаются ровно один раз (update), forming-бар оценивается
зондом без изменения состояния (evaluate): dict-state стратегии сохраняется,
вызов делается на копии окна, состояние восстанавливается.

Почему это ТОЧНО равно batch (индукция, проверяется тестами):
- стратегии читают окно, но переходы состояния детерминированы той же
  последовательностью баров: persistent-после-closed[1..k] == fresh-replay;
- зонд = тот же последний переход, что batch делает на forming-бакете;
- warmup-правило (i <= warmup → пропуск) и форма сигнала дословно как
  _sig_batch (ensemble_ctx): {"ts","side","status","reason","features"}.

Снапшот — generic по vars(): скаляры как есть, list/deque/dict/set —
копией, params не трогаем (immutable). Новые поля стратегий подхватываются
автоматически; паритет-гейт поймает расхождение, если семантика поля
не копируется поверхностно.

Окно вызова — CandleWindow(closed, max(0,n-400), n), как _sig_batch:
Donchian читает срез [n-period-1:n-1], ему нужен настоящий хвост истории.
"""
from __future__ import annotations

from collections import deque

from app.engine.strategies import build_strategy
from app.engine.views import CandleWindow

_SKIP_ATTRS = frozenset({"params"})


def _snap(strat) -> dict:
    out = {}
    for k, v in vars(strat).items():
        if k in _SKIP_ATTRS:
            continue
        if isinstance(v, deque):
            # maxlen сохранять обязательно: unbounded копия ломает len()==period
            out[k] = deque(v, maxlen=v.maxlen)
        elif isinstance(v, list):
            out[k] = list(v)
        elif isinstance(v, (dict, set)):
            out[k] = v.copy()
        else:
            out[k] = v
    return out


def _restore(strat, snap: dict) -> None:
    for k in [k for k in vars(strat) if k not in _SKIP_ATTRS and k not in snap]:
        delattr(strat, k)
    for k, v in snap.items():
        setattr(strat, k, v)


def _to_dict(sig) -> dict:
    return {"ts": sig.time, "side": sig.side.value, "status": "CANDIDATE",
            "reason": sig.reason, "features": sig.features}


class StrategyState:
    """Персистентное состояние одного сетапа на одном ТФ."""

    def __init__(self, sid: str, params: dict | None = None,
                 tf_sec: int | None = None, window: int = 400):
        self.sid = sid
        self.strat = build_strategy(sid, params)
        self.warmup = self.strat.warmup_bars()
        self.tf_sec = tf_sec
        self.window = window
        self.sigs: list[dict] = []  # только закрытые бары, append-only
        self.n = 0                  # скормленных закрытых баров

    def update(self, closed_bars: list) -> dict | None:
        """Скормить новый закрытый бар (closed_bars — весь closed-список)."""
        n = len(closed_bars)
        lo = max(0, n - self.window)
        sig = self.strat.on_bar(CandleWindow(closed_bars, lo, n))
        self.n = n
        if sig is None or n <= self.warmup:
            return None
        d = _to_dict(sig)
        self.sigs.append(d)
        return d

    def evaluate(self, closed_bars: list, forming_bar) -> dict | None:
        """Сигнал forming-бара: состояние не меняется (снапшот/restore)."""
        save = _snap(self.strat)
        try:
            tail = list(closed_bars[max(0, len(closed_bars) - (self.window - 1)):])
            tail.append(forming_bar)
            sig = self.strat.on_bar(CandleWindow(tail, 0, len(tail)))
            n = len(closed_bars) + 1
            if sig is None or n <= self.warmup:
                return None
            return _to_dict(sig)
        finally:
            _restore(self.strat, save)
