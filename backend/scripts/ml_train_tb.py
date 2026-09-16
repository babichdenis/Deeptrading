#!/usr/bin/env python3
"""Универсальная 1m-модель Triple Barrier (LONG/SHORT симметрия).

Разметка:
- база = 1m свечи, барьеры SL = SL_ATR*ATR14, TP = TP_ATR*ATR14, горизонт HORIZON_BARS.
- label_tb=+1 если TP первым, -1 если SL первым, 0 если за горизонт не коснулся.
- SHORT-сэмплы: зеркальная свеча (close->1/close), фичи пересчитываются на зеркале.
- y (для классификатора) = (label_tb > 0); mfe/mae в ATR-ед. для регрессоров.

Фичи: FEATS из ml_ensemble_filter (+ tcode как индекс тикера).
Split: train/val/oos по UTC.
Порог keep: не 0.5, а квантиль валидационного распределения p (--quorum-th, default top-2%).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)
sys.path.insert(0, os.path.join(_BACKEND, "scripts"))

from app.services.ml_ensemble_filter import FEATS, _compute_features  # noqa: E402
from app.engine.models import Candle as EngineCandle  # noqa: E402

TZ = timezone.utc

# ---- Barriers ----
TP_ATR = 4.0
SL_ATR = 2.0
HORIZON_BARS = 60          # сколько 1m баров вперёд смотрим
ATR_PERIOD = 14
START_INDEX = 70           # warmup: первые 70 баров не размечаем
WARMUP = START_INDEX

# ---- Universe ----
TICKERS = ["SBER", "GAZP", "LKOH", "ROSN", "RUAL", "SNGSP", "AFLT", "MVID",
           "NLMK", "SMLT", "NVTK", "MAGN", "TRNFP", "T", "ASTR", "MTSS",
           "CHMF", "LENT", "SFIN", "GMKN", "VKCO", "YDEX", "RNFT", "IRKT"]
TCODE = {t: i for i, t in enumerate(TICKERS)}

# ---- Split ----
TRAIN_FROM = datetime(2025, 1, 1, 0, 0, tzinfo=TZ)
TRAIN_TO = datetime(2026, 5, 31, 23, 59, tzinfo=TZ)
VAL_FROM = datetime(2026, 6, 1, 0, 0, tzinfo=TZ)
VAL_TO = datetime(2026, 8, 14, 23, 59, tzinfo=TZ)
OOS_FROM = datetime(2026, 8, 15, 0, 0, tzinfo=TZ)
OOS_TO = datetime(2026, 9, 12, 23, 59, tzinfo=TZ)

DB = dict(host="127.0.0.1", port=5432, dbname="deeptrading",
          user="deeptrading", password="deeptrading")

FEAT_COLS = FEATS + ["tcode"]


def load_1m_candles() -> dict[str, list[EngineCandle]]:
    """Загрузка 1m свечей по тикерам (инструменты -> figi -> candles)."""
    import psycopg2
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    out: dict[str, list[EngineCandle]] = {}
    for t in TICKERS:
        cur.execute("SELECT figi FROM instruments WHERE ticker = %s", (t,))
        row = cur.fetchone()
        if not row:
            continue
        figi = row[0]
        cur.execute(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE figi = %s AND interval = 1 AND ts >= %s AND ts < %s "
            "ORDER BY ts",
            (figi, TRAIN_FROM, OOS_TO),
        )
        rows = cur.fetchall()
        if not rows:
            continue
        out[t] = [EngineCandle(ts=r[0], open=r[1], high=r[2], low=r[3],
                               close=r[4], volume=r[5]) for r in rows]
    cur.close()
    conn.close()
    return out


def _df_from_candles(c5: list[EngineCandle]) -> pd.DataFrame:
    return pd.DataFrame({
        "ts": [c.ts for c in c5],
        "open": [float(c.open) for c in c5],
        "high": [float(c.high) for c in c5],
        "low": [float(c.low) for c in c5],
        "close": [float(c.close) for c in c5],
        "volume": [float(c.volume) for c in c5],
    })


def _compute_features_dir(df: pd.DataFrame) -> dict[str, np.ndarray]:
    c5 = [EngineCandle(ts=row.ts, open=row.open, high=row.high, low=row.low,
                       close=row.close, volume=row.volume)
          for row in df.itertuples(index=False)]
    f = _compute_features(c5)
    return {k: f[k].to_numpy(dtype=np.float64) for k in FEATS}


def _atr_values(df: pd.DataFrame) -> np.ndarray:
    h = df["high"].to_numpy(np.float64)
    l = df["low"].to_numpy(np.float64)
    c = df["close"].to_numpy(np.float64)
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    tr[0] = h[0] - l[0]
    s = pd.Series(tr)
    return s.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean().to_numpy(np.float64)


def _make_mirror(df: pd.DataFrame) -> pd.DataFrame:
    m = df.copy()
    for col in ("open", "high", "low", "close"):
        p = df[col].to_numpy(np.float64)
        m[col] = np.where(p > 0, 1.0 / p, np.nan)
    tmp = m["high"].copy()
    m["high"] = m["low"]
    m["low"] = tmp
    return m


def triple_barrier(df: pd.DataFrame, atr: np.ndarray):
    """Векторизованная разметка Triple Barrier.

    Возвращает:
      lab: np.int8  (+1 TP первым / -1 SL первым / 0 горизонт)
      mfe: np.float64  макс экскурсия в сторону (в ATR на баре входа)
      mae: np.float64  макс экскурсия против (в ATR на баре входа)
    Бары без полного горизонта (последние HORIZON_BARS) -> mfe/mae NaN.
    """
    c = df["close"].to_numpy(np.float64)
    h = df["high"].to_numpy(np.float64)
    l = df["low"].to_numpy(np.float64)
    n = len(c)
    W = HORIZON_BARS
    lab = np.zeros(n, dtype=np.int8)
    mfe = np.full(n, np.nan, dtype=np.float64)
    mae = np.full(n, np.nan, dtype=np.float64)
    if n <= W + 1:
        return lab, mfe, mae

    hw = np.lib.stride_tricks.sliding_window_view(h, W + 1)[:, 1:]  # (n-W, W)
    lw = np.lib.stride_tricks.sliding_window_view(l, W + 1)[:, 1:]
    h_win = np.concatenate([hw.max(axis=1), np.full(W, np.nan)])
    l_win = np.concatenate([lw.min(axis=1), np.full(W, np.nan)])

    tp = c[: n - W] + atr[: n - W] * TP_ATR
    sl = c[: n - W] - atr[: n - W] * SL_ATR
    has = np.isfinite(tp) & np.isfinite(sl) & (atr[: n - W] > 0)

    hit_tp = hw >= tp[:, None]      # (n-W, W) — коснулись TP когда-либо
    hit_sl = lw <= sl[:, None]
    any_tp = hit_tp.any(axis=1)
    any_sl = hit_sl.any(axis=1)
    first_tp = np.argmax(hit_tp, axis=1).astype(np.int32)
    first_sl = np.argmax(hit_sl, axis=1).astype(np.int32)

    lab_v = np.zeros(n - W, dtype=np.int8)
    lab_v[any_tp & ~any_sl] = 1
    lab_v[any_sl & ~any_tp] = -1
    both = any_tp & any_sl
    lab_v[both & (first_tp <= first_sl)] = 1
    lab_v[both & (first_tp > first_sl)] = -1
    lab_v[~has] = 0

    a_safe = np.where(has, atr[: n - W], np.nan)
    mfe_v = np.where(a_safe > 0, (h_win[: n - W] - c[: n - W]) / a_safe, np.nan)
    mae_v = np.where(a_safe > 0, (c[: n - W] - l_win[: n - W]) / a_safe, np.nan)

    lab[: n - W] = lab_v
    mfe[: n - W] = mfe_v
    mae[: n - W] = mae_v
    return lab, mfe, mae


def build_dataset(c1: dict[str, list[EngineCandle]], ticker_order: list[str]):
    frames: list[pd.DataFrame] = []
    per: dict[str, dict] = {}
    for ticker in ticker_order:
        t0 = time.time()
        df_ = _df_from_candles(c1[ticker])
        n = len(df_)
        f_ = _compute_features_dir(df_)          # LONG фичи
        atr = _atr_values(df_)
        lab, mfe, mae = triple_barrier(df_, atr)

        dfm = _make_mirror(df_)
        fm = _compute_features_dir(dfm)          # SHORT фичи (на зеркале)
        atm = _atr_values(dfm)
        lab_m, mfe_m, mae_m = triple_barrier(dfm, atm)

        ts_arr = df_["ts"].values.astype("datetime64[ns]").astype("int64") // 10 ** 9
        iv = np.arange(n) >= WARMUP
        tcode = TCODE[ticker]

        F_as = np.column_stack([f_[k][iv] for k in FEATS]).astype(np.float64)
        F_mir = np.column_stack([fm[k][iv] for k in FEATS]).astype(np.float64)

        base_long = {
            "ts": pd.to_datetime(ts_arr[iv], unit="s"),
            "ticker": np.repeat(ticker, int(iv.sum())),
            "tcode": np.full(int(iv.sum()), tcode, dtype=np.float32),
            "side": np.repeat("LONG", int(iv.sum())),
            "y": (lab[iv] > 0).astype(np.int8),
            "label_tb": lab[iv].astype(np.int8),
            "mfe": mfe[iv].astype(np.float32),
            "mae": mae[iv].astype(np.float32),
        }
        pairs_long = {k: F_as[:, j] for j, k in enumerate(FEATS)}
        pairs_long.update(base_long)

        base_short = {
            "ts": pd.to_datetime(ts_arr[iv], unit="s"),
            "ticker": np.repeat(ticker, int(iv.sum())),
            "tcode": np.full(int(iv.sum()), tcode, dtype=np.float32),
            "side": np.repeat("SHORT", int(iv.sum())),
            "y": (lab_m[iv] > 0).astype(np.int8),
            "label_tb": lab_m[iv].astype(np.int8),
            "mfe": mfe_m[iv].astype(np.float32),
            "mae": mae_m[iv].astype(np.float32),
        }
        pairs_short = {k: F_mir[:, j] for j, k in enumerate(FEATS)}
        pairs_short.update(base_short)

        frames.append(pd.DataFrame(pairs_long))
        frames.append(pd.DataFrame(pairs_short))
        per[ticker] = {"1m": n, "rows": 2 * int(iv.sum())}
        print(f"  {ticker}: {n} 1m баров ({time.time() - t0:.0f}s)", flush=True)

    df = pd.concat(frames, ignore_index=True)
    meta = {"per_ticker": per, "FEATS": FEATS,
            "TP_ATR": TP_ATR, "SL_ATR": SL_ATR,
            "HORIZON_BARS": HORIZON_BARS, "ATR_PERIOD": ATR_PERIOD, "base": "1m"}
    return df, meta


def main(args: argparse.Namespace | None = None) -> None:
    if args is None:
        ap = argparse.ArgumentParser()
        ap.add_argument("--out", default="reports/ml_tb_1m.joblib")
        ap.add_argument("--sample-every", type=int, default=1,
                        help="брать каждый N-й бар (для ускорения экспериментов)")
        ap.add_argument("--n-est", type=int, default=2000)
        ap.add_argument("--lr", type=float, default=0.1)
        ap.add_argument("--max-depth", type=int, default=6)
        ap.add_argument("--quorum-th", type=str, default="0.98",
                        help="квантиль val-p для боевого порога keep (top-x%)")
        ap.add_argument("--all-labels", action="store_true")
        args = ap.parse_args()
    t_start = time.time()
    print(f"Читаю 1m из БД ({TRAIN_FROM.date()}..{OOS_TO.date()})...", flush=True)
    c1 = load_1m_candles()
    print(f"Тикеров в БД: {len(c1)} ({time.time() - t_start:.0f}s)", flush=True)

    ticker_order = [t for t in TICKERS if t in c1]
    print(f"Тикеров для обучения: {len(ticker_order)}: {ticker_order}", flush=True)
    missing = [t for t in TICKERS if t not in c1]
    if missing:
        print(f"Нет данных (пропущены): {missing}", flush=True)

    print("Строю датасет (1m, симметричный Triple Barrier)...", flush=True)
    t1 = time.time()
    df, meta = build_dataset(c1, ticker_order)
    del c1
    print(f"Датасет: {len(df)} строк, {time.time() - t1:.0f}с", flush=True)

    if args.sample_every > 1:
        n0 = len(df)
        df = df.iloc[:: args.sample_every].reset_index(drop=True)
        print(f"Сэмплинг каждый {args.sample_every}-й бар: {len(df)} из {n0}", flush=True)

    if len(df) < 2000:
        print("Слишком мало строк — аборт")
        sys.exit(1)

    if not args.all_labels:
        n0 = len(df)
        df = df[df["label_tb"] != 0].reset_index(drop=True)
        print(f"Только signed (label_tb!=0): {len(df)} из {n0} строк", flush=True)
        if len(df) < 2000:
            print("Слишком мало signed — аборт")
            sys.exit(1)

    pos = int((df["y"] == 1).sum())
    neg = int((df["y"] == 0).sum())
    print(f"Class balance: pos={pos} ({pos / len(df) * 100:.1f}%), neg={neg} "
          f"({neg / len(df) * 100:.1f}%) [label_tb: +1={(df['label_tb'] == 1).sum()} / "
          f"-1={(df['label_tb'] == -1).sum()} / 0={(df['label_tb'] == 0).sum()}]",
          flush=True)

    def split_mask(lo: datetime, hi: datetime) -> np.ndarray:
        t = df["ts"].values.astype("datetime64[ns]")
        return (t >= np.datetime64(lo)) & (t < np.datetime64(hi))

    m_train = split_mask(TRAIN_FROM, TRAIN_TO)
    m_val = split_mask(VAL_FROM, VAL_TO)
    m_oos = split_mask(OOS_FROM, OOS_TO)
    print(f"train={m_train.sum()} val={m_val.sum()} oos={m_oos.sum()}", flush=True)

    X_all = df[FEAT_COLS].to_numpy(dtype=np.float64)
    X_all = np.nan_to_num(X_all, nan=0.0, posinf=0.0, neginf=0.0)

    X_train, X_val, X_oos = X_all[m_train], X_all[m_val], X_all[m_oos]
    X_train = X_train[:: args.sample_every]
    y_train = df["y"].values[m_train][:: args.sample_every]
    y_reg_mfe = df["mfe"].values
    y_reg_mae = df["mae"].values

    print("Обучаю (LightGBM classifier + MFE/MAE regressors)...", flush=True)
    import lightgbm as lgb

    scale_pos = float((y_train == 0).sum()) / max(1.0, float((y_train == 1).sum()))
    clf = lgb.LGBMClassifier(
        n_estimators=args.n_est, learning_rate=args.lr,
        max_depth=args.max_depth, num_leaves=min(2 ** args.max_depth, 31),
        scale_pos_weight=scale_pos, objective="binary",
        subsample=0.8, colsample_bytree=0.8, random_state=42,
        verbose=-1,
    )
    clf.fit(X_train, y_train,
            eval_set=[(X_val, df["y"].values[m_val])],
            eval_metric="auc",
            callbacks=[lgb.early_stopping(50, verbose=False)])

    reg_mfe = lgb.LGBMRegressor(
        n_estimators=args.n_est, learning_rate=args.lr,
        max_depth=args.max_depth, num_leaves=min(2 ** args.max_depth, 31),
        subsample=0.8, colsample_bytree=0.8, random_state=43, verbose=-1,
    )
    reg_mfe.fit(X_train, y_reg_mfe[m_train][:: args.sample_every],
                eval_set=[(X_val, y_reg_mfe[m_val])],
                callbacks=[lgb.early_stopping(50, verbose=False)])

    reg_mae = lgb.LGBMRegressor(
        n_estimators=args.n_est, learning_rate=args.lr,
        max_depth=args.max_depth, num_leaves=min(2 ** args.max_depth, 31),
        subsample=0.8, colsample_bytree=0.8, random_state=44, verbose=-1,
    )
    reg_mae.fit(X_train, y_reg_mae[m_train][:: args.sample_every],
                eval_set=[(X_val, y_reg_mae[m_val])],
                callbacks=[lgb.early_stopping(50, verbose=False)])

    def side_metrics(pred_p: np.ndarray, yb: np.ndarray, yt: np.ndarray,
                     mfe: np.ndarray, mae: np.ndarray, th: float) -> dict:
        n = len(yb)
        if n == 0:
            return {"n": 0, "th": th}
        acc_bin = (pred_p >= th).astype(int)
        acc = float((acc_bin == yb).sum()) / n
        signed = yt != 0
        wr_all = float((acc_bin[signed] == yb[signed]).sum()) / signed.sum() if signed.sum() else 0.0
        keep = int((pred_p >= th).sum())
        keep_signed = int((signed & (pred_p >= th)).sum())
        keep_wr = float(yb[pred_p >= th].mean()) if keep else 0.0
        return {"n": n, "th": th, "acc": round(acc, 4), "wr": round(wr_all, 4),
                "keep": keep, "keep_signed": keep_signed, "keep_wr": round(keep_wr, 4),
                "corr_mfe": float(np.corrcoef(pred_p, mfe)[0, 1]) if n > 2 and mfe.std() > 0 else 0.0,
                "corr_mae": float(np.corrcoef(pred_p, mae)[0, 1]) if n > 2 and mae.std() > 0 else 0.0}

    def summary(mask: np.ndarray, name: str, th: float) -> dict:
        sub = df[mask]
        p = clf.predict_proba(X_all[mask])[:, 1]
        out = {name: {}}
        lone = sub["side"].values == "LONG"
        out[name]["both"] = side_metrics(p, sub["y"].values, sub["label_tb"].values,
                                         sub["mfe"].values, sub["mae"].values, th)
        out[name]["long"] = side_metrics(p[lone], sub["y"].values[lone],
                                         sub["label_tb"].values[lone],
                                         sub["mfe"].values[lone], sub["mae"].values[lone], th)
        out[name]["short"] = side_metrics(p[~lone], sub["y"].values[~lone],
                                          sub["label_tb"].values[~lone],
                                          sub["mfe"].values[~lone], sub["mae"].values[~lone], th)
        return out

    Q = float(args.quorum_th)
    p_v = clf.predict_proba(X_val)[:, 1]
    th_v = float(np.quantile(p_v, Q))
    p_o = clf.predict_proba(X_oos)[:, 1]
    th_o = float(np.quantile(p_o, Q))
    mets = {**summary(m_oos, "oos", th_o), **summary(m_val, "val", th_v)}
    mets["val_threshold"] = th_v
    mets["oos_threshold"] = th_o
    mets["use_quantile"] = Q
    mets_user = {k: v for k, v in mets.items()}

    print("\n=== OOS/Val метрики ===")
    print(json.dumps(mets_user, ensure_ascii=False, indent=2, default=str))

    report = {
        "model_version": "ml_tb_1m_v1",
        "base": "1m",
        "barriers": {"TP_ATR": TP_ATR, "SL_ATR": SL_ATR, "horizon_bars": HORIZON_BARS,
                     "atr_period": ATR_PERIOD},
        "split": {
            "train": [TRAIN_FROM.isoformat(), TRAIN_TO.isoformat()],
            "val": [VAL_FROM.isoformat(), VAL_TO.isoformat()],
            "oos": [OOS_FROM.isoformat(), OOS_TO.isoformat()],
        },
        "n_train": int(m_train.sum()), "n_val": int(m_val.sum()), "n_oos": int(m_oos.sum()),
        "class_balance": {"pos": pos, "neg": neg, "scale_pos": round(scale_pos, 3)},
        "metrics": mets_user,
        "dataset": meta,
        "feature_importance": _fi(clf, FEAT_COLS),
    }

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    import joblib
    joblib.dump({
        "clf": clf, "reg_mfe": reg_mfe, "reg_mae": reg_mae,
        "features": FEAT_COLS, "TICKERS": ticker_order,
        "barriers": {"TP_ATR": TP_ATR, "SL_ATR": SL_ATR, "HORIZON_BARS": HORIZON_BARS,
                     "ATR_PERIOD": ATR_PERIOD},
        "report": report,
        "audit": {
            "X_val": X_val, "y_val": df["y"].values[m_val],
            "lab_val": df["label_tb"].values[m_val],
            "ts_val": df["ts"].values[m_val].astype("datetime64[s]").astype("int64"),
            "side_val": df["side"].values[m_val],
            "X_oos": X_oos, "y_oos": df["y"].values[m_oos],
            "lab_oos": df["label_tb"].values[m_oos],
            "ts_oos": df["ts"].values[m_oos].astype("datetime64[s]").astype("int64"),
            "side_oos": df["side"].values[m_oos],
            "th_v": th_v, "th_o": th_o, "p_v": p_v, "p_o": p_o,
        },
    }, args.out)
    print(f"\nСохранено: {args.out}", flush=True)


def _fi(clf, cols: list[str]) -> list[dict]:
    imp = getattr(clf, "feature_importances_", None)
    if imp is None:
        return []
    srt = sorted(zip(cols, imp), key=lambda kv: -kv[1])
    return [{"feature": n, "importance": float(v)} for n, v in srt]


if __name__ == "__main__":
    main()
