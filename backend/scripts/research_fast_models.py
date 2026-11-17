"""Обзор быстрых моделей для ARCH-04 (exploratory): правила, логрессия, дерево, boosting, MLP.

Схема фиксируется ДО замера, чтобы результат нельзя было подогнать:

  данные     : 1-минутные свечи из БД, 5-минутный ресемпл, 6 ликвидных бумаг
  target     : знак хода за H=3 бара вперёд, порог 0.15% (up / flat / down)
  признаки   : 15 штук, только прошлое (цена к EMA, MACD, RSI, ATR, объём,
               согласие ТФ, положение в канале, время сессии)
  walk-forward: 3 фолда расширяющегося окна, эмбарго 1 день между train и test
  метрики    : accuracy, доля сигналов, net после расходов (комиссия 0.3% + 2 бп),
               max drawdown по сделкам, latency p50/p95 на одну строку
  сравнение  : LONG / SHORT / пропуск — отдельно точность по каждому классу

Это НЕ попытка заменить ensemble и НЕ оптимизация: цель — понять, какой класс
моделей стоит времени на .8 (i5-4460, без GPU), и какая у них цена по latency.

Запуск: .venv\\Scripts\\python.exe scripts\\research_fast_models.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.candle import Candle as CandleRow  # noqa: E402

TICKERS = {
    "SBER": "BBG004730N88",
    "GAZP": "BBG004730RP0",
    "LKOH": "BBG004731032",
    "ROSN": "BBG004731354",
    "MTSS": "BBG004S681W1",
    "MVID": "BBG004S68CP5",
}
HORIZON = 3          # баров вперёд (5м × 3 = 15 мин)
THRESHOLD = 0.0015   # 0.15% — граница между up/down/flat
COMMISSION = 0.003   # 0.3% за сделку (как в конфиге контура)
SLIPPAGE = 0.0002    # 2 бп
DAYS = 120           # окно истории


async def load_frames() -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    async with SessionLocal() as db:
        for ticker, figi in TICKERS.items():
            rows = (await db.execute(
                select(CandleRow.ts, CandleRow.open, CandleRow.high,
                       CandleRow.low, CandleRow.close, CandleRow.volume)
                .where(CandleRow.figi == figi, CandleRow.interval == 1)
                .order_by(CandleRow.ts.desc()).limit(120_000)
            )).all()
            if not rows:
                print(f"{ticker}: данных нет, пропуск")
                continue
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            df = df.sort_values("ts").set_index("ts")
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            # 5м ресемпл, неполные бакеты (меньше 5 баров) отбрасываем
            g = df.resample("5min").agg(
                open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), volume=("volume", "sum"), count=("close", "size"))
            g = g[g["count"] >= 4].drop(columns=["count"]).dropna()
            # обрезаем по последним DAYS календарным дням
            cutoff = g.index.max() - pd.Timedelta(days=DAYS)
            out[ticker] = g[g.index >= cutoff]
            print(f"{ticker}: {len(out[ticker])} баров 5м")
    return out


def features(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    f = pd.DataFrame(index=df.index)
    f["ema12_dist"] = c / c.ewm(span=12, adjust=False).mean() - 1
    f["ema26_dist"] = c / c.ewm(span=26, adjust=False).mean() - 1
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()
    f["macd_scaled"] = macd / c
    f["macd_hist"] = (macd - sig) / c
    f["macd_sign"] = np.sign(macd - sig)
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    f["rsi14"] = 100 - 100 / (1 + rs)
    f["rsi14_gt50"] = (f["rsi14"] > 50).astype(float)
    f["rsi14_vs_70"] = (f["rsi14"] - 70) / 100
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - c.shift()).abs(),
                    (df["low"] - c.shift()).abs()], axis=1).max(axis=1)
    f["atr14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    f["atr_pct"] = f["atr14"] / c
    f["move_atr"] = (c - c.shift(HORIZON)) / f["atr14"].replace(0, np.nan)
    f["vol_ratio"] = df["volume"] / df["volume"].rolling(48).mean()
    ma12 = c.rolling(12).mean()
    ma48 = c.rolling(48).mean()
    f["tf_align"] = np.sign(ma12 - ma48)
    hi48 = df["high"].rolling(48).max()
    lo48 = df["low"].rolling(48).min()
    f["pos_in_channel"] = (c - lo48) / (hi48 - lo48).replace(0, np.nan)
    f["hour"] = df.index.hour + df.index.minute / 60
    return f


def build_dataset(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    parts = []
    for ticker, df in frames.items():
        f = features(df)
        fwd = df["close"].shift(-HORIZON) / df["close"] - 1
        y = pd.Series(1, index=f.index)          # flat
        y[fwd > THRESHOLD] = 2                    # up
        y[fwd < -THRESHOLD] = 0                   # down
        part = f.copy()
        part["y"] = y
        part["fwd"] = fwd
        part["ticker"] = ticker
        part = part.iloc[:-HORIZON]
        parts.append(part.dropna())
    data = pd.concat(parts).sort_index()
    data = data[data.index.duplicated(keep="first") | ~data.index.duplicated()]
    return data


FEATURES = ["ema12_dist", "ema26_dist", "macd_scaled", "macd_hist", "macd_sign",
            "rsi14", "rsi14_gt50", "rsi14_vs_70", "atr_pct", "move_atr",
            "vol_ratio", "tf_align", "pos_in_channel", "hour"]


def baseline_rules(row: pd.Series) -> int:
    """Простое правило без обучения: тренд по MACD + согласие ТФ + RSI."""
    if row["tf_align"] > 0 and row["macd_hist"] > 0 and row["rsi14"] < 70:
        return 2
    if row["tf_align"] < 0 and row["macd_hist"] < 0 and row["rsi14"] > 30:
        return 0
    return 1


def models() -> dict:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.tree import DecisionTreeClassifier

    return {
        "rules": None,
        "logreg": make_pipeline(StandardScaler(), LogisticRegression(max_iter=400)),
        "tree_d4": DecisionTreeClassifier(max_depth=4, min_samples_leaf=200, random_state=0),
        "hist_gbm": HistGradientBoostingClassifier(max_iter=120, max_depth=3,
                                                   learning_rate=0.06, random_state=0),
        "mlp_16": make_pipeline(StandardScaler(),
                                MLPClassifier(hidden_layer_sizes=(16,), max_iter=200,
                                              early_stopping=True, random_state=0)),
    }


def latency_us(predict_fn, X: np.ndarray, n: int = 200) -> tuple[float, float]:
    times = []
    for i in range(n):
        row = X[i % len(X)][None, :]
        t0 = time.perf_counter()
        predict_fn(row)
        times.append((time.perf_counter() - t0) * 1e6)
    times.sort()
    return times[len(times) // 2], times[int(len(times) * 0.95)]


def walk_forward(data: pd.DataFrame) -> dict:
    from sklearn.metrics import accuracy_score

    dates = np.array(sorted(data.index.unique()))
    folds = 3
    results: dict[str, dict] = {}
    models_map = models()
    for name, proto in models_map.items():
        print(f"\n=== {name}")
        accs, sig_share, nets, dds, lat50, lat95, per_class = [], [], [], [], [], [], []
        for k in range(folds):
            # расширяющееся окно: train до границы минус эмбарго, test — следующий блок
            span = len(dates) // (folds + 1)
            train_end = dates[span * (k + 1) - 1]
            embargo = pd.Timedelta(days=1)
            test_start = train_end + embargo
            test_end = dates[min(len(dates) - 1, span * (k + 2) - 1)]
            tr = data[data.index <= train_end]
            te = data[(data.index > test_start) & (data.index <= test_end)]
            if len(tr) < 500 or len(te) < 200:
                continue
            Xtr, ytr = tr[FEATURES].to_numpy(float), tr["y"].to_numpy(int)
            Xte, yte = te[FEATURES].to_numpy(float), te["y"].to_numpy(int)
            if proto is None:
                pred = np.array([baseline_rules(r) for _, r in te[FEATURES].iterrows()])
                def _predict(row_vec, _df=te):  # noqa: ANN001
                    s = pd.Series(row_vec[0], index=FEATURES)
                    return baseline_rules(s)
            else:
                import copy
                model = copy.deepcopy(proto)
                model.fit(Xtr, ytr)
                pred = model.predict(Xte)
                _predict = model.predict
            acc = accuracy_score(yte, pred)
            # сигналы = не flat
            sig_mask = pred != 1
            share = float(sig_mask.mean())
            # net по сделкам: движение за горизонт минус расходы, знак по классу
            fwd = te["fwd"].to_numpy(float)[sig_mask]
            direction = np.where(pred[sig_mask] == 2, 1.0, -1.0)
            gross = fwd * direction
            net = gross - (COMMISSION + SLIPPAGE)
            nets.append(float(net.sum()))
            equity = np.cumsum(net)
            dds.append(float((np.maximum.accumulate(equity) - equity).max()) if len(equity) else 0.0)
            accs.append(acc)
            sig_share.append(share)
            pc = {}
            for cls, lbl in ((2, "up"), (0, "down"), (1, "flat")):
                m = pred == cls
                if m.sum():
                    pc[lbl] = round(float((yte[m] == cls).mean()), 3)
            per_class.append(pc)
            l50, l95 = latency_us(_predict, Xte)
            lat50.append(l50)
            lat95.append(l95)
            print(f"  фолд {k+1}: acc={acc:.3f} сигналов={share:.1%} net={net.sum():+.4f} "
                  f"dd={dds[-1]:.4f} p50={l50:.0f}мкс p95={l95:.0f}мкс {pc}")
        results[name] = {
            "accuracy": round(float(np.mean(accs)), 4) if accs else None,
            "signal_share": round(float(np.mean(sig_share)), 4) if sig_share else None,
            "net_sum": round(float(np.sum(nets)), 4) if nets else None,
            "max_dd": round(float(np.max(dds)), 4) if dds else None,
            "latency_p50_us": round(float(np.mean(lat50)), 1) if lat50 else None,
            "latency_p95_us": round(float(np.mean(lat95)), 1) if lat95 else None,
            "per_class": per_class,
        }
    return results


def main() -> None:
    import asyncio
    frames = asyncio.run(load_frames())
    data = build_dataset(frames)
    print(f"\nвсего строк: {len(data)}, период {data.index.min()} .. {data.index.max()}")
    print(f"классы: {data['y'].value_counts(normalize=True).round(3).to_dict()}")
    results = walk_forward(data)
    out = Path(__file__).resolve().parents[1] / "reports" / "affected" / "fast_models_20261115"
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        json.dumps({"scheme": {"horizon_bars": HORIZON, "threshold": THRESHOLD,
                               "commission": COMMISSION, "slippage": SLIPPAGE,
                               "days": DAYS, "features": FEATURES,
                               "tickers": list(TICKERS)}, "results": results},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== сводка ===")
    for name, r in results.items():
        print(f"{name:9s} acc={r['accuracy']} сигналов={r['signal_share']} "
              f"net={r['net_sum']} dd={r['max_dd']} p50={r['latency_p50_us']}мкс "
              f"p95={r['latency_p95_us']}мкс")


if __name__ == "__main__":
    main()
