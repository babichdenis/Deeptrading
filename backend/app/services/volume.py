"""Volume Exhaustion сигналы (VOLUME_EXHAUSTION_2026.md §2).

Единый источник volume_ratio для ансамбля (не дублирует RegimeDetector).
Все детекторы считаются по ЗАКРЫТОМУ бару (без look-ahead).
"""

from dataclasses import dataclass


def _norm(candles):
    out = []
    for c in candles:
        out.append({
            "ts": c.ts,
            "open": float(c.open or 0),
            "high": float(c.high or 0),
            "low": float(c.low or 0),
            "close": float(c.close or 0),
            "volume": float(c.volume or 0),
        })
    return out


@dataclass
class VolumeParams:
    """Стартовые пороги (план §2); потом вынести в optuna."""
    ma_len: int = 20          # MA объёма
    dryup_ratio: float = 0.5  # V1: затишье
    climax_ratio: float = 3.0  # V3: всплеск
    drop_ratio: float = 1.5    # V4: падение на объёме
    div_n: int = 20            # V2/V5: окно «новый экстремум»
    wick_frac: float = 0.5     # V3: доля тени в диапазоне


def volume_features(candles, p: VolumeParams | None = None) -> list[dict]:
    """Фичи по каждому закрытому 5m-бару.

    Returns: [{ts, vol_ratio, dryup, divergence_bear, divergence_bull,
               climax_long, climax_short, volume_on_drop, ...}]
    """
    if not candles:
        return []
    p = p or VolumeParams()
    data = _norm(candles)
    n = len(data)
    iss = {d["ts"]: i for i, d in enumerate(data)}
    # MA объёма (скользящее среднее прошлых баров).
    ma_vol: list[float] = []
    for i in range(n):
        w = data[max(0, i - p.ma_len):i]
        ma_vol.append(sum(x["volume"] for x in w) / len(w) if w else 1.0)

    # Скользящие максимумы/минимумы + средние объёмы прошлых баров.
    out: list[dict] = []
    for i in range(n):
        d = data[i]
        rng = d["high"] - d["low"]
        vr = d["volume"] / max(ma_vol[i], 1e-9)
        f = {
            "ts": d["ts"].isoformat(),
            "vol_ratio": round(vr, 3),
            "dryup": vr < p.dryup_ratio,
            "divergence_bear": False,
            "divergence_bull": False,
            "climax_long": False,   # блок LONG (длинная верхняя тень на росте)
            "climax_short": False,  # блок SHORT (длинная нижняя тень на падении)
            "volume_on_drop": False,
            "upper_wick": 0.0,
            "lower_wick": 0.0,
        }
        if rng > 1e-9:
            f["upper_wick"] = round(max(0.0, d["high"] - max(d["open"], d["close"])) / rng, 3)
            f["lower_wick"] = round(max(0.0, min(d["open"], d["close"]) - d["low"]) / rng, 3)
        # V4: close ниже prev_close и всплеск объёма.
        if i >= 1 and d["close"] < data[i - 1]["close"] and vr > p.drop_ratio:
            f["volume_on_drop"] = True
        # V3: climax — всплеск объёма + длинная тень.
        if vr > p.climax_ratio:
            if f["upper_wick"] > p.wick_frac and d["close"] >= d["open"]:
                f["climax_long"] = True
            if f["lower_wick"] > p.wick_frac and d["close"] <= d["open"]:
                f["climax_short"] = True
        # V2/V5: дивергенция цена-объём на новом экстремуме.
        if i >= p.div_n:
            prev = data[i - p.div_n:i]
            prev_high = max(x["high"] for x in prev) if prev else 0.0
            prev_low = min(x["low"] for x in prev) if prev else 0.0
            prev_vol = sum(x["volume"] for x in prev) / len(prev) if prev else 1.0
            # Новый максимум с объёмом НИЖЕ среднего прошлых → покупатели истощены.
            if d["high"] > prev_high and prev_vol > 0 and d["volume"] < prev_vol:
                f["divergence_bear"] = True
            # Новый минимум с объёмом НИЖЕ среднего прошлых → продавцы истощены.
            if d["low"] < prev_low and prev_vol > 0 and d["volume"] < prev_vol:
                f["divergence_bull"] = True
        out.append(f)
    return out


def volume_at(features: list[dict], ts) -> dict | None:
    """Фичи для момента ts: последний ЗАКРЫТЫЙ бар <= ts."""
    if not features:
        return None
    try:
        from datetime import datetime
        if isinstance(ts, datetime):
            ts_key = ts.isoformat()
        else:
            ts_key = str(ts)
    except Exception:
        ts_key = str(ts)
    best = None
    for f in features:
        if f["ts"] <= ts_key:
            best = f
        else:
            break
    return best