#!/usr/bin/env python3
"""ML-фильтр для ensemble: LightGBM (25 акций, 5m) как gate качества сигналов.

Роль: после merge_quorum каждый кандидат-вход оценивается моделью.
Модель обучена на 2024-2025 (label: forward 6x5m return > 0). Если
pred_prob < threshold — сигнал отклоняется (reason=ML_REJECTED).

Использование:
  f = MlEnsembleFilter(threshold=0.55, ticker="SBER")
  f.precompute(c5)            # один раз: фичи + proba на все 5m бары
  ok, why = f.accepts(ts)     # lookup по ts (datetime/iso)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd

from app.engine.models import Candle as EngineCandle

ML_PATH = "/Users/Denis/Dev/tinvest_study/ml_25stocks_2024.joblib"
MAPPING_PATH = "/Users/Denis/Dev/tinvest_study/ticker_mapping.txt"
FEATS = ["natr", "rsi", "rsid", "mhist", "bbpos", "donpos", "emadist",
         "vwapdev", "volz", "rvol", "fd", "hod", "vol_ratio", "dow", "bias_1h"]


def resample_to_5m(candles: list[EngineCandle]) -> list[EngineCandle]:
    out: list[EngineCandle] = []
    for c in candles:
        epoch = int(c.ts.timestamp())
        bucket = epoch - epoch % 300
        key = pd.Timestamp(bucket, unit="s", tz=timezone.utc).to_pydatetime()
        if out and out[-1].ts == key:
            prev = out[-1]
            out[-1] = EngineCandle(ts=key, open=prev.open, high=max(prev.high, c.high),
                                   low=min(prev.low, c.low), close=c.close,
                                   volume=prev.volume + c.volume)
        else:
            out.append(EngineCandle(ts=key, open=c.open, high=c.high, low=c.low,
                                    close=c.close, volume=c.volume))
    return out


def _compute_features(c5: list[EngineCandle]) -> pd.DataFrame:
    df = pd.DataFrame({
        "ts": [c.ts for c in c5],
        "open": [float(c.open) for c in c5],
        "high": [float(c.high) for c in c5],
        "low": [float(c.low) for c in c5],
        "close": [float(c.close) for c in c5],
        "volume": [float(c.volume) for c in c5],
    })
    c = df["close"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    lo = df["low"].values.astype(np.float64)
    v = df["volume"].values.astype(np.float64)
    ts = pd.to_datetime(df["ts"])
    n = len(c)
    cs = pd.Series(c)
    hs = pd.Series(h)
    los = pd.Series(lo)
    vs = pd.Series(v)
    out: dict[str, np.ndarray] = {}

    tr = np.maximum(h - lo, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(lo - np.roll(c, 1))))
    tr[0] = h[0] - lo[0]
    tr_s = pd.Series(tr)
    atr_w = tr_s.ewm(alpha=1/14, adjust=False).mean().values
    out["natr"] = np.where(c > 0, atr_w / c, np.nan)

    delta = cs.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    ag = gain.ewm(alpha=1/14, adjust=False).mean().values
    al = loss.ewm(alpha=1/14, adjust=False).mean().values
    rsi = np.where(al == 0, 100.0, 100.0 - 100.0 / (1.0 + ag / al))
    rsi[0] = np.nan
    out["rsi"] = rsi
    out["rsid"] = np.concatenate([[np.nan], np.diff(rsi)])

    macd_line = cs.ewm(span=12, adjust=False).mean() - cs.ewm(span=26, adjust=False).mean()
    out["mhist"] = (macd_line - macd_line.ewm(span=9, adjust=False).mean()).values

    bb_m = cs.rolling(20).mean().values
    bb_s = cs.rolling(20).std().values
    bb_bw = 4 * bb_s
    out["bbpos"] = np.where(bb_bw > 0, (c - (bb_m - 2 * bb_s)) / bb_bw, 0.5)

    dh = hs.rolling(20).max().values
    dl = los.rolling(20).min().values
    rng = dh - dl
    out["donpos"] = np.where(rng > 0, (c - dl) / rng, 0.5)

    e20 = cs.ewm(span=20, adjust=False).mean().values
    out["emadist"] = np.where(e20 > 0, (c - e20) / e20, np.nan)

    days = ts.dt.date.values
    vwap_dev = np.full(n, np.nan)
    for d in np.unique(days):
        idx = np.where(days == d)[0]
        if len(idx) == 0:
            continue
        tp = (h[idx] + lo[idx] + c[idx]) / 3.0
        vv = np.maximum(v[idx], 1.0)
        vwap = np.cumsum(tp * vv) / np.cumsum(vv)
        vwap_dev[idx] = np.where(vwap > 0, (c[idx] - vwap) / vwap, 0.0)
    out["vwapdev"] = vwap_dev

    vm = vs.rolling(20).mean().values
    vsd = vs.rolling(20).std().values
    out["volz"] = np.where(vsd > 0, (v - vm) / vsd, 0.0)

    log_ret = np.log(c / np.roll(c, 1))
    log_ret[0] = 0
    out["rvol"] = pd.Series(log_ret).rolling(21).std().values

    W = 64
    coeffs = np.zeros(W + 1)
    d_ = 0.5
    k = 1.0
    coeffs[0] = 1.0
    for j in range(1, W + 1):
        k = k * (d_ - j + 1) / j
        coeffs[j] = -k
    fd = np.convolve(c, coeffs[::-1], mode="full")[:n]
    fd[:W] = np.nan
    out["fd"] = fd

    out["hod"] = (ts.dt.hour.values * 60 + ts.dt.minute.values) % 1440

    vr = (vs / vs.rolling(20).mean().values).values
    out["vol_ratio"] = vr

    out["dow"] = ts.dt.dayofweek.values.astype(np.float64)

    ts_ns = ts.values.astype("datetime64[ns]").astype(np.int64)
    buckets = ts_ns - (ts_ns % 3_600_000_000_000)
    df_tmp = pd.DataFrame({"bucket": buckets, "c": c, "h": h, "lo": lo})
    h1 = df_tmp.groupby("bucket").agg(
        h1_open=("c", "first"), h1_high=("h", "max"), h1_low=("lo", "min"), h1_close=("c", "last")
    ).reset_index()
    h1["h1_ema"] = pd.Series(h1["h1_close"].values).ewm(span=50, adjust=False).mean().values
    h1_map = dict(zip(h1["bucket"], h1["h1_ema"]))
    h1_keys = h1["bucket"].values
    idx_search = np.searchsorted(h1_keys, buckets) - 1
    valid = idx_search >= 0
    ema_vals = np.array([h1_map.get(h1_keys[i], np.nan) for i in idx_search])
    bias1h = np.full(n, np.nan)
    bias1h[valid] = np.where(ema_vals[valid] != 0, (c[valid] - ema_vals[valid]) / ema_vals[valid], 0.0)
    out["bias_1h"] = bias1h

    for f in FEATS:
        df[f] = out[f]
    return df


class MlEnsembleFilter:
    def __init__(self, model_path: str = ML_PATH, mapping_path: str = MAPPING_PATH,
                 threshold: float = 0.55, ticker: str | None = None, figi: str | None = None):
        self.model = joblib.load(model_path)
        self.threshold = float(threshold)
        self._figi_code: dict[str, int] = {}
        self._ticker_code_map: dict[str, int] = {}
        self._load_mapping(mapping_path)
        self.ticker = ticker
        self.figi = figi
        self._proba: dict[str, float] = {}
        self._tcode: int | None = self._ticker_code_for(figi, ticker)

    def _load_mapping(self, path: str) -> None:
        if not os.path.exists(path):
            raise FileNotFoundError(f"ticker mapping not found: {path}")
        with open(path) as f:
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) != 3:
                    continue
                self._figi_code[parts[0]] = int(parts[2])
                self._ticker_code_map[parts[1]] = int(parts[2])

    def _ticker_code_for(self, figi: str | None, ticker: str | None) -> int | None:
        if ticker and ticker in self._ticker_code_map:
            return self._ticker_code_map[ticker]
        if figi and figi in self._figi_code:
            return self._figi_code[figi]
        return None

    @property
    def ticker_code(self) -> int | None:
        return self._tcode

    def precompute(self, c5: list[EngineCandle]) -> None:
        """Считает proba для всех 5m баров один раз. Хранит {ts_iso: prob}."""
        if not c5 or self._tcode is None:
            self._proba = {}
            return
        df = _compute_features(c5)
        X = df[FEATS].to_numpy()
        X = np.nan_to_num(X, nan=0.0)
        tcode = np.full(len(X), float(self._tcode))
        X = np.column_stack([X, tcode])
        proba = self.model.predict_proba(X)[:, 1]
        ts_iso = [t.isoformat() for t in df["ts"].dt.to_pydatetime()]
        self._proba = dict(zip(ts_iso, proba.astype(float)))

    def _norm(self, t) -> str:
        """Нормализует время к 5m bucket (модель считает на 5m барах)."""
        if isinstance(t, str):
            t = pd.Timestamp(t).to_pydatetime()
        epoch = int(t.timestamp())
        bucket = epoch - epoch % 300
        return datetime.fromtimestamp(bucket, tz=timezone.utc).isoformat()

    def prob_at(self, t) -> float | None:
        return self._proba.get(self._norm(t))

    def accepts(self, t) -> tuple[bool, str]:
        prob = self.prob_at(t)
        if prob is None:
            return False, "ML_NA"
        if prob >= self.threshold:
            return True, ""
        return False, "ML_REJECTED"
