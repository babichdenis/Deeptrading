"""Triple-barrier labeling для MOEX 1m/5m candles.
Размечает датасет для ML: для каждой свечи определяет, куда движение уйдёт
(upper barrier = +1, lower barrier = -1, timeout = 0).

Использование:
  python3 h_label_dataset.py [--figi FIGI1,FIGI2,...] [--interval 1|5] [--atr-period 14]
                              [--atr-tp 2.0] [--atr-sl 1.0] [--max-hold 60]
                              [--out labeled.parquet]
"""
import asyncio, os, sys, json, argparse
from datetime import datetime, timezone, timedelta
import numpy as np
import polars as pl
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

ENG = "postgresql+asyncpg://deeptrading:deeptrading@192.168.1.54:5432/deeptrading"
REPORTS = "/Users/Denis/Dev/Deeptrading/backend/reports"


async def load_candles(figi, interval, t_from, t_to):
    eng = create_async_engine(ENG)
    try:
        async with eng.connect() as c:
            r = await c.execute(text(
                "SELECT ts, open, high, low, close, volume FROM candles "
                "WHERE figi=:f AND interval=:i AND ts>=:a AND ts<:b ORDER BY ts"),
                {"f": figi, "i": interval, "a": t_from, "b": t_to})
            rows = r.fetchall()
            if not rows:
                return None
            return pl.DataFrame({
                "ts": [x[0] for x in rows],
                "open": [float(x[1]) for x in rows],
                "high": [float(x[2]) for x in rows],
                "low": [float(x[3]) for x in rows],
                "close": [float(x[4]) for x in rows],
                "volume": [int(x[5]) for x in rows],
            })
    finally:
        await eng.dispose()


def compute_features(df):
    """Вычисляет базовые фичи: returns, ATR, RSI, Bollinger, volume z-score."""
    df = df.with_columns([
        (pl.col("close") / pl.col("close").shift(1) - 1).alias("ret_1"),
        (pl.col("close") / pl.col("close").shift(5) - 1).alias("ret_5"),
        (pl.col("close") / pl.col("close").shift(15) - 1).alias("ret_15"),
    ])
    # ATR14
    df = df.with_columns([
        (pl.col("high") - pl.col("low")).alias("tr_hl"),
        ((pl.col("high") - pl.col("close").shift(1)).abs()).alias("tr_hc"),
        ((pl.col("low") - pl.col("close").shift(1)).abs()).alias("tr_lc"),
    ])
    df = df.with_columns([
        pl.max_horizontal("tr_hl", "tr_hc", "tr_lc").alias("tr")
    ])
    df = df.with_columns([
        pl.col("tr").rolling_mean(window_size=14).alias("atr14")
    ])
    # NATR (ATR / close)
    df = df.with_columns([
        (pl.col("atr14") / pl.col("close")).alias("natr14")
    ])
    # RSI14
    df = df.with_columns([
        pl.col("close").diff().alias("_delta"),
    ])
    df = df.with_columns([
        pl.col("_delta").clip(lower_bound=0).rolling_mean(window_size=14).alias("_gain"),
        (-pl.col("_delta")).clip(lower_bound=0).rolling_mean(window_size=14).alias("_loss"),
    ])
    df = df.with_columns([
        (pl.col("_gain") / pl.col("_loss")).alias("_rs"),
        (100 - 100 / (1 + pl.col("_gain") / pl.col("_loss"))).alias("rsi14"),
    ])
    df = df.drop(["_delta", "_gain", "_loss", "_rs"])
    # Bollinger Bands
    df = df.with_columns([
        pl.col("close").rolling_mean(window_size=20).alias("sma20"),
        pl.col("close").rolling_std(window_size=20).alias("std20"),
    ])
    df = df.with_columns([
        (pl.col("sma20") + 2 * pl.col("std20")).alias("bb_upper"),
        (pl.col("sma20") - 2 * pl.col("std20")).alias("bb_lower"),
        ((pl.col("close") - pl.col("sma20")) / (2 * pl.col("std20"))).alias("bb_pct"),
    ])
    # Volume z-score
    df = df.with_columns([
        ((pl.col("volume") - pl.col("volume").rolling_mean(window_size=20))
         / pl.col("volume").rolling_std(window_size=20)).alias("vol_z"),
    ])
    # Hour of day (для intraday паттернов)
    df = df.with_columns([
        pl.col("ts").dt.hour().alias("hour"),
        pl.col("ts").dt.weekday().alias("weekday"),
    ])
    return df


def triple_barrier_labels(df, atr_tp=2.0, atr_sl=1.0, max_hold=60):
    """Triple-barrier labeling с ATR-барьерами.
    
    Для каждой свечи i:
    - Look forward max_hold баров
    - Upper: close[i] + atr_tp * atr14[i]
    - Lower: close[i] - atr_sl * atr14[i]
    - Если upper hit first → label=1
    - Если lower hit first → label=-1
    - Если timeout → label=0
    """
    n = len(df)
    labels = np.zeros(n, dtype=np.int8)
    entry_prices = np.zeros(n, dtype=np.float64)
    exit_prices = np.zeros(n, dtype=np.float64)
    hold_bars = np.zeros(n, dtype=np.int32)
    
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    atr = df["atr14"].to_numpy()
    
    for i in range(n):
        if np.isnan(atr[i]) or atr[i] <= 0:
            continue
        
        entry = close[i]
        upper = entry + atr_tp * atr[i]
        lower = entry - atr_sl * atr[i]
        
        entry_prices[i] = entry
        
        for j in range(i + 1, min(i + max_hold + 1, n)):
            if high[j] >= upper:
                labels[i] = 1
                exit_prices[i] = upper
                hold_bars[i] = j - i
                break
            elif low[j] <= lower:
                labels[i] = -1
                exit_prices[i] = lower
                hold_bars[i] = j - i
                break
        else:
            # timeout
            labels[i] = 0
            exit_prices[i] = close[min(i + max_hold, n - 1)]
            hold_bars[i] = min(max_hold, n - 1 - i)
    
    df = df.with_columns([
        pl.Series("label", labels),
        pl.Series("entry_price", entry_prices),
        pl.Series("exit_price", exit_prices),
        pl.Series("hold_bars", hold_bars),
    ])
    return df


async def main():
    parser = argparse.ArgumentParser(description="Triple-barrier labeling MOEX candles")
    parser.add_argument("--figi", default="BBG004730N88,BBG004730RP0,BBG004731032",
                        help="Comma-separated FIGIs (default: SBER,GAZP,LKOH)")
    parser.add_argument("--interval", type=int, default=5, help="1 or 5 (default: 5)")
    parser.add_argument("--atr-tp", type=float, default=2.0, help="ATR multiplier for take profit")
    parser.add_argument("--atr-sl", type=float, default=1.0, help="ATR multiplier for stop loss")
    parser.add_argument("--max-hold", type=int, default=60, help="Max holding period in bars")
    parser.add_argument("--t-from", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--t-to", default="2026-08-01", help="End date (YYYY-MM-DD)")
    parser.add_argument("--out", default=None, help="Output parquet path")
    args = parser.parse_args()
    
    figis = [f.strip() for f in args.figi.split(",")]
    t_from = datetime.fromisoformat(args.t_from).replace(tzinfo=timezone.utc)
    t_to = datetime.fromisoformat(args.t_to).replace(tzinfo=timezone.utc)
    
    all_dfs = []
    for figi in figis:
        print(f"Loading {figi}...", flush=True)
        df = await load_candles(figi, args.interval, t_from, t_to)
        if df is None or len(df) < 100:
            print(f"  SKIP {figi}: insufficient data")
            continue
        
        print(f"  {len(df)} bars, computing features...", flush=True)
        df = compute_features(df)
        
        print(f"  triple-barrier labeling (ATR TP={args.atr_tp}, SL={args.atr_sl}, max_hold={args.max_hold})...", flush=True)
        df = triple_barrier_labels(df, args.atr_tp, args.atr_sl, args.max_hold)
        
        # Добавляем FIGI как колонку
        df = df.with_columns([pl.lit(figi).alias("figi")])
        
        # Статистика
        label_counts = df["label"].value_counts().sort("label")
        print(f"  Labels: {dict(zip(label_counts['label'].to_list(), label_counts['count'].to_list()))}")
        print(f"  Mean hold_bars: {df['hold_bars'].mean():.1f}")
        
        all_dfs.append(df)
    
    if not all_dfs:
        print("No data loaded!")
        return
    
    result = pl.concat(all_dfs)
    
    # Сохраняем
    out_path = args.out or os.path.join(REPORTS, "labeled_dataset.parquet")
    result.write_parquet(out_path)
    print(f"\nSaved: {out_path}")
    print(f"Total rows: {len(result)}")
    print(f"Columns: {result.columns}")
    
    # Итоговая статистика
    label_counts = result["label"].value_counts().sort("label")
    print(f"\nOverall label distribution:")
    for row in label_counts.iter_rows():
        pct = 100 * row[1] / len(result)
        label_name = {1: "LONG (+1)", -1: "SHORT (-1)", 0: "TIMEOUT (0)"}.get(row[0], str(row[0]))
        print(f"  {label_name}: {row[1]} ({pct:.1f}%)")
    
    # Фичи для ML
    feature_cols = [c for c in result.columns if c.startswith(("ret_", "atr", "natr", "rsi", "bb_", "vol_z", "hour", "weekday"))]
    print(f"\nML features ({len(feature_cols)}): {feature_cols}")


if __name__ == "__main__":
    asyncio.run(main())
