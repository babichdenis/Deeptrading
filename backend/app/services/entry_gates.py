"""Гейты входа ансамбля — отдельный модуль (рефакторинг 2026-09-26).

Директива: новые/исправленные блоки бота выносятся в ОТДЕЛЬНЫЕ модули,
а не дорастают инлайном в куче в ensemble.py. Здесь — гейты стадии signal,
перенесённые из приватных функций ensemble.py (_rsi_map / _rsi_entry_blocked /
_regime_bias_decision) без изменения логики.

Состав:
- rsi_map(bars, period)             — RSI (Wilder, past-only) по барам;
- rsi_entry_blocked(side, rsi, cfg) — инверсный RSI-гейт входа:
  BUY только при RSI < buy_max (умолч. 40), SELL только при RSI > sell_min
  (умолч. 60); зона min..max включительно — «мёртвая» (входов нет ни в одну
  сторону); явные ключи buy_max/sell_min перекрывают min/max;
- TREND_STATES                       — трендовые состояния режима;
- regime_bias_decision(side, state, bcfg, ts) — per-regime bias-гейт
  (off | pass | info | veto), bcfg = {"map", "tf_sec", "mode"}.

Подключение в ensemble.py (entry-loop), конфиг запроса:
- RSI-гейт:        req["rsi_filter"] = {"period": 14, "buy_max": 40, "sell_min": 60};
- Per-regime bias: req["bias_by_state"] = {"TREND_UP": {"tf": "30min", "period": 100,
                                                   "mode": "veto"}, ...}.

Тесты: backend/tests/test_regime_bias_rsi_gate.py.
"""
from __future__ import annotations


def rsi_map(bars, period: int = 14) -> dict:
    """RSI (Wilder, past-only) по барам → {ts: (rsi, prev_rsi | None)}."""
    closes = [b.close for b in bars]
    n = len(closes)
    if n <= period:
        return {}
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += d if d > 0 else 0.0
        losses -= d if d < 0 else 0.0
    avg_g = gains / period
    avg_l = losses / period
    out: dict = {}

    def _val(g: float, l: float) -> float:
        if l <= 0:
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)

    _prev = _val(avg_g, avg_l)
    out[bars[period].ts] = (_prev, None)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
        _cur = _val(avg_g, avg_l)
        out[bars[i].ts] = (_cur, _prev)
        _prev = _cur
    return out


def rsi_entry_blocked(side: str, rsi: float, cfg: dict) -> bool:
    """RSI-гейт входа. Два режима (cfg["mode"]):

    - "inverse" (по умолчанию): BUY только при RSI < buy_max (min), SELL только
      при RSI > sell_min (max); зона min..max включительно — «мёртвая»
      (buy_max/sell_min перекрывают min/max);
    - "neutral": вход только в нейтральной зоне min..max (границы разрешены);
      BUY блокируется при RSI > max (перегрето) и RSI < min (слабость),
      SELL — зеркально."""
    mode = str(cfg.get("mode") or "inverse").lower()
    lo = cfg.get("min", 40.0)
    hi = cfg.get("max", 60.0)
    if mode == "neutral":
        return (hi is not None and rsi > float(hi)) or (lo is not None and rsi < float(lo))
    if side == "BUY":
        thr = cfg.get("buy_max", lo)
        return thr is not None and rsi >= float(thr)
    thr = cfg.get("sell_min", hi)
    return thr is not None and rsi <= float(thr)


TREND_STATES = ("TREND_UP", "TREND_DOWN")


def regime_bias_decision(side: str, state: str, bcfg: dict | None, ts) -> tuple[str, str]:
    """Per-regime bias: off | pass | info | veto. bcfg = {"map", "tf_sec", "mode"}.

    mode None → veto для трендовых состояний, off для остальных.
    info — вход не блокируется, но помечается против bias (наблюдение)."""
    mode = str((bcfg or {}).get("mode") or ("veto" if state in TREND_STATES else "off"))
    if mode == "off":
        return "off", ""
    if bcfg is None:
        return "veto", f"REGIME_OFF:{state}"
    _btf = int(bcfg["tf_sec"])
    bv = int(bcfg["map"].get(int(ts.timestamp()) // _btf, 0))
    want = None
    if state == "TREND_UP" and bv == 1:
        want = "BUY"
    elif state == "TREND_DOWN" and bv == -1:
        want = "SELL"
    elif state in ("HIGH_VOLATILITY", "NEUTRAL", "RANGE"):
        want = "BUY" if bv == 1 else "SELL" if bv == -1 else None
    _dir = "BUY" if bv == 1 else "SELL" if bv == -1 else "нет(bias=0)"
    if mode == "info":
        if side == want:
            return "pass", ""
        return "info", f"BIAS_INFO:{side}->{want or '—'}:{state}:bias={_dir}({bv})"
    if want is None or side != want:
        return "veto", f"COMBO_BIAS:{side}->{want or 'запрет'}:{state}:bias={_dir}({bv})"
    return "pass", ""
