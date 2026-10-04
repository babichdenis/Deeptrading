"""V2-стриминг с интерфейсом RegimeState (update/rows/evaluate) для replay.

Полный batch-пересчёт на каждом баре был бы O(n²) по времени; измеряем только
хвост хвоста: измерения последнего бара считаем по последним _TAIL барам
(percentile_window=200, EMA(50) за 260 баров сходится), а состояние гистерезиса
ведём инкрементально — так метки причинно-корректны и совпадают с batch-веткой
по построению (расхождение только от прогрева EMA в хвосте).
"""
from __future__ import annotations

from app.services.regime_calibration import session_of
from app.services.regime_v2.hysteresis import RegimeV2State
from app.services.regime_v2.measurements import RegimeV2Params, compute_measurements

_TAIL = 260


class RegimeV2StateStream:
    def __init__(self, tf_sec: int, window: int = 12, **legacy_kw):
        self.tf_sec = int(tf_sec)
        self._params = RegimeV2Params(window=window)
        self._st = RegimeV2State()
        self._bars: list = []
        self.rows: list[dict] = []

    def _measure_last(self, bars: list) -> dict | None:
        if not bars:
            return None
        ms = compute_measurements(bars[-_TAIL:], self._params)
        return ms[-1] if ms else None

    def update(self, bar) -> dict:
        self._bars.append(bar)
        m = self._measure_last(self._bars)
        if m is None:
            return {}
        obs, raw, label = self._st.update(m["ts"], self.tf_sec, m, session_of(m["ts"]))
        row = {"ts": m["ts"], "state": label, "reason": raw, "features": obs.to_dict()}
        self.rows.append(row)
        return row

    def evaluate(self, forming_bar) -> dict:
        m = self._measure_last(self._bars + [forming_bar])
        if m is None:
            return {}
        obs, raw, label = self._st.evaluate(m["ts"], self.tf_sec, m, session_of(m["ts"]))
        return {"ts": m["ts"], "state": label, "reason": raw, "features": obs.to_dict()}

    # совместимость с RegimeState (snapshot-механики пайплайна не требуют)
    def snapshot(self) -> dict:  # pragma: no cover - defensive
        return {"rows": [dict(r) for r in self.rows],
                "bars": list(self._bars), "st": self._st.snapshot()}

    def restore(self, snap: dict) -> RegimeV2StateStream:  # pragma: no cover
        self.rows = [dict(r) for r in snap["rows"]]
        self._bars = list(snap["bars"])
        self._st.restore(snap["st"])
        return self
